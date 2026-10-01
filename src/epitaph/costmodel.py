"""The cost model: a thought-by-thought estimate of a life from machine costs (BUILD_PLAN 5.3).

It answers one question before any Pi time is spent: with these costs, does the profile give
the model enough thoughts after every loss to notice it? Costs come from bench/*.json
(measured by spikes S1b, S2 and S4) or, until then, from the hardware overlay's estimates.

Besides the thought-count rule (a)-(d), an estimate fails three checks that verify-life makes
on real lives, because a profile is where they are won or lost:

- **reload silence** (`verify.max_reload_silence_s`): the load plus the full re-read of a
  fresh server, from the reload to the first word after it;
- **speed decline** (`verify.max_speed_ratio_end_vs_start`, full-level profiles): generation
  speed in the last 5 minutes against the first 5;
- **speed monotonic** (review 2, F2): generation never speeds up across a reload. The first
  thought after each reload may not be modelled faster than the last thought before it.
  Speeds here follow the context, which a reload cuts: when the bench measured a short
  context (the birth thought) and a deep one, the speed at a thought is interpolated
  between them by its prompt size and held at the nearer end outside that range; otherwise
  the deep rate stands for every context. Thought timing still charges the deep rate, which
  errs slow for the thought-count rule. The life drift (below) is left out on both sides,
  because a reload restarts the server and whether that resets the drift is not measured
  (F8). `estimate.speed_monotonic = "warn"` reports a violation as a note instead.

What the estimate assumes, from the spikes:

- Prompt processing runs on `backend.threads_batch` threads (S4: 3 while generation drops
  to 2), limited by the CPU share like generation.
- Generation slows through a life (S1c: 1.81 -> 1.42 tokens/s over 30 min on one server,
  not thermal). A bench file may carry `tg_tok_s_late`; the estimate then eases every rate
  down to that share of itself over `late_after_s` of life, and keeps the slower value from
  then on. Whether a reload resets the slowdown is not measured, so it is not assumed.
- The system prompt is sized from the real persona text, per erosion step.
- The memory-gap marker rides on the reading after the first loss (decision A3), so it
  costs its own tokens and no re-read; cache reuse holds (S2f: 2-8% re-read per edit).
"""

from __future__ import annotations

import itertools
import json
import statistics
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from epitaph.clock import Schedule
from epitaph.config import REPO_ROOT, Config, profile_rules, reading_tokens, system_tokens
from epitaph.mind.memory import approx_tokens
from epitaph.mind.prompt import Persona
from epitaph.pacing import StreamCurve
from epitaph.types import RuleReport, RuleViolation, StreamEstimate

# Save plus restore of the cache slot at a reload, with margin (spike S4b measured 0.3 s).
HANDOVER_S = 1.0
# Material readings: after a reload the new weights continue five of its own words (18 tokens,
# greedy), and the next reading quotes that and what was forgotten (rehearse.Rehearsal._echo).
ECHO_PROMPT_TOKENS = 6
ECHO_TOKENS = 18
ECHO_READING_TOKENS = 30
QUOTE_TOKENS = 15


@dataclass
class Costs:
    """Machine costs at full CPU share.

    Rate tables are in tokens per second, keyed "<step>-<threads>"; load_s is the model load
    time in seconds for each ladder step.
    """

    tg_tok_s: dict[str, float]
    pp_tok_s: dict[str, float]
    load_s: list[float]
    estimated: bool = True
    source: str = "overlay"
    cache_reuse_works: bool = True
    reuse_residual: float = 0.1  # share of the prompt still re-read when reuse works
    # Generation slows through a life (spike S1c): after `late_after_s` seconds, rates are
    # `tg_late_factor` of the bench value; eased in linearly before that.
    tg_late_factor: float = 1.0
    late_after_s: float = 1800.0
    # Generation at a short context (the bench birth thought), when measured: the end of a
    # life runs with little memory, so this is its speed (speed-decline check).
    tg_short_tok_s: dict[str, float] = field(default_factory=lambda: {})
    # Prompt sizes (tokens) at which the short and the deep rates were measured, when known:
    # `tg_at` interpolates between them.
    short_ctx: float | None = None
    deep_ctx: float | None = None

    def _rate(self, table: dict[str, float], step: int, threads: int) -> float:
        key = f"{step}-{threads}"
        if key in table:
            return table[key]
        # Nearest known thread count for this step, scaled linearly by threads.
        same_step = [
            (int(k.split("-")[1]), v) for k, v in table.items() if k.startswith(f"{step}-")
        ]
        if same_step:
            th, v = min(same_step, key=lambda kv: abs(kv[0] - threads))
            return v * threads / th
        return min(table.values())

    def tg(self, step: int, threads: int, share: float) -> float:
        """Generation speed in tokens/s, scaled down when share is below one core per thread."""
        return self._rate(self.tg_tok_s, step, threads) * min(1.0, share / threads)

    def pp(self, step: int, threads: int, share: float, threads_batch: int | None = None) -> float:
        """Prompt processing speed in tokens/s, scaled like `tg`.

        `threads_batch` (llama-server `-tb`) is the prompt thread count when it differs from
        the generation threads; the CPU share then limits those threads.
        """
        tb = max(threads, threads_batch or threads)
        return self._rate(self.pp_tok_s, step, tb) * min(1.0, share / tb)

    def drift(self, age_s: float) -> float:
        """How much of its bench speed generation keeps `age_s` seconds into a life (S1c)."""
        if self.late_after_s <= 0:
            return self.tg_late_factor
        return 1.0 - (1.0 - self.tg_late_factor) * min(1.0, max(0.0, age_s) / self.late_after_s)

    def tg_short(self, step: int, threads: int, share: float) -> float:
        """Generation speed at a short context (birth-thought size), scaled like `tg`."""
        table = {**self.tg_tok_s, **self.tg_short_tok_s}
        return self._rate(table, step, threads) * min(1.0, share / threads)

    def tg_at(self, step: int, threads: int, share: float, context: float) -> float:
        """Generation speed at a prompt of `context` tokens, scaled like `tg`.

        Interpolates between the short-context rate (`tg_short`, at `short_ctx`) and the
        deep one (`tg`, at `deep_ctx`), held at the nearer end outside that range; the deep
        rate when either size is unknown.
        """
        deep = self.tg(step, threads, share)
        lo, hi = self.short_ctx, self.deep_ctx
        if lo is None or hi is None or hi <= lo:
            return deep
        short = self.tg_short(step, threads, share)
        frac = min(1.0, max(0.0, (context - lo) / (hi - lo)))
        return short + (deep - short) * frac

    def load(self, step: int) -> float:
        """Seconds to load the model at a ladder step; steps past the list reuse the last."""
        return self.load_s[min(step, len(self.load_s) - 1)]


def load_costs(cfg: Config, model: str | None = None, bench_dir: Path | None = None) -> Costs:
    """Measured costs from bench/ when present for this model and class, else the overlay's."""
    table: dict[str, Any] = cfg.section("costs")
    costs = Costs(
        tg_tok_s={str(k): float(v) for k, v in dict(table.get("tg_tok_s", {})).items()},
        pp_tok_s={str(k): float(v) for k, v in dict(table.get("pp_tok_s", {})).items()},
        load_s=[float(x) for x in list(table.get("load_s", [60.0]))],
        estimated=bool(table.get("estimated", True)),
        source=f"overlay {cfg.hardware}",
        cache_reuse_works=bool(cfg.get("estimate.cache_reuse_works", True)),
    )
    name = model or str(cfg.get("life.models", [""])[0])
    bench = bench_dir or REPO_ROOT / "bench"
    measured = sorted(bench.glob(f"{cfg.hw_class}-{name}-*.json")) if bench.exists() else []
    late: list[float] = []
    short_ctx: list[float] = []
    deep_ctx: list[float] = []
    for path in measured:
        rec: dict[str, Any] = json.loads(path.read_text())
        key = f"{rec['step']}-{rec['threads']}"
        if "tg_tok_s" in rec:
            costs.tg_tok_s[key] = float(rec["tg_tok_s"])
            if "tg_tok_s_late" in rec:
                late.append(float(rec["tg_tok_s_late"]) / float(rec["tg_tok_s"]))
                costs.late_after_s = float(rec.get("late_after_s", costs.late_after_s))
        if "tg_tok_s_birth" in rec:
            costs.tg_short_tok_s[key] = float(rec["tg_tok_s_birth"])
            birth: dict[str, Any] = rec.get("birth") or {}
            if birth.get("prompt_tokens") and rec.get("deep_prompt_tokens"):
                short_ctx.append(float(birth["prompt_tokens"]))
                deep_ctx.append(float(rec["deep_prompt_tokens"]))
        if "pp_tok_s" in rec:
            costs.pp_tok_s[key] = float(rec["pp_tok_s"])
        if "load_s" in rec:
            step = int(rec["step"])
            while len(costs.load_s) <= step:
                costs.load_s.append(costs.load_s[-1])
            costs.load_s[step] = float(rec["load_s"])
        if "cache_reuse_works" in rec:
            costs.cache_reuse_works = bool(rec["cache_reuse_works"])
    if late:
        costs.tg_late_factor = min(1.0, *late)
    if short_ctx:
        costs.short_ctx = statistics.fmean(short_ctx)
        costs.deep_ctx = statistics.fmean(deep_ctx)
    if measured:
        costs.estimated = False
        costs.source = f"bench ({len(measured)} files) over {costs.source}"
    return costs


@dataclass
class _Life:
    memory: int = 0
    marker: bool = False
    step: int = 0
    threads: int = 0
    groups: int = 0
    mechanics: bool = True
    last_reload: float = -1e9
    thought_times: list[float] = field(default_factory=lambda: [])
    # (start time, generation speed at a short context) per thought, for the speed decline
    speeds: list[tuple[float, float]] = field(default_factory=lambda: [])
    # generation speed at each thought's own context (speed monotonic), and for each reload
    # (its start, the index of the first thought after it)
    gen_speeds: list[float] = field(default_factory=lambda: [])
    reload_thoughts: list[tuple[float, int]] = field(default_factory=lambda: [])
    reload_windows: list[tuple[float, float]] = field(default_factory=lambda: [])
    silences: list[float] = field(default_factory=lambda: [])


def _system_tokens(cfg: Config) -> Callable[[int, bool], int]:
    """Tokens of the system prompt per (groups, mechanics), from the real persona text.

    Falls back to the `[estimate]` per-group guess when there is no persona text.
    """
    try:
        persona = Persona.from_config(cfg)
    except ValueError:
        persona = Persona([], "")
    if not persona.groups:
        return lambda groups, mechanics: system_tokens(cfg, groups, mechanics)
    return lambda groups, mechanics: approx_tokens(persona.system_text(groups, mechanics))


def estimate(cfg: Config, costs: Costs, schedule: Schedule | None = None) -> RuleReport:
    """Simulate one life; check the thought-count rule (a)-(d), the reload silence and the
    speed decline (see the module notes). A stream profile (`[reveal] mode = "stream"`) is
    replayed by `estimate_stream` instead."""
    if str(cfg.get("reveal.mode", "letter")) == "stream":
        return estimate_stream(cfg, costs, schedule)
    sch = schedule or Schedule(cfg.profile)
    est = cfg.section("estimate")
    reveal = cfg.section("reveal")
    fill = float(est.get("fill", 0.85))
    letters_per_token = float(est.get("letters_per_token", 3.5))
    letters_per_word = float(est.get("letters_per_word", 4.7))
    margin = float(reveal.get("rate_margin", 0.88))
    word_gap = float(reveal.get("word_gap_ms", 90)) / 1000
    sentence_pause = float(reveal.get("sentence_pause_ms", 700)) / 1000
    comma_pause = float(reveal.get("comma_pause_ms", 250)) / 1000
    trim_to = float(cfg.get("output.trim_to", 0.85))
    min_gap = float(cfg.get("life.min_reload_gap_s", 120))
    threads_batch = int(cfg.get("backend.threads_batch", 0)) or None
    marker_tokens = _marker_tokens(cfg)
    sys_tokens_of = _system_tokens(cfg)
    end = sch.lifespan_s
    if sch.death_s is not None and str(cfg.get("body.death_mode", "oom")) == "oom":
        end = min(end, sch.death_s)

    report = RuleReport(profile=cfg.profile.name, lifespan_s=sch.lifespan_s)
    k0 = sch.at(0)
    life = _Life(step=k0.step, threads=k0.threads, groups=k0.persona_groups)
    life.mechanics = k0.mechanics
    t = 0.0

    handover = str(cfg.get("backend.reload_handover", "reread")) == "slot"

    material = bool(cfg.get("prompt.readings_material", False))

    def reread_cost(tokens: int) -> int:
        if costs.cache_reuse_works:
            return int(tokens * costs.reuse_residual)
        return tokens

    while t < end:
        k = sch.at(t)
        extra = 0
        reload_start: float | None = None

        if (k.step, k.threads) != (life.step, life.threads) and t - life.last_reload >= min_gap:
            reload_start = t
            # The reload cuts memory the way Memory.cut_for_reload does (contract C-B4).
            life.memory = min(life.memory, int(k.recall * trim_to))
            t += costs.load(k.step)
            life.step, life.threads, life.last_reload = k.step, k.threads, reload_start
            k = sch.at(t)  # the silence took time: the reading is written after the load
            if handover:
                # ADR-014 (contract A16): the old server's cache is restored into the new one,
                # so only the cut is absorbed by cache reuse, not a full re-read.
                t += HANDOVER_S
                extra += reread_cost(sys_tokens_of(life.groups, life.mechanics) + life.memory)
            else:
                # A fresh server reads everything, the system prompt included (prefilled
                # during the silence, so it is part of the silence either way).
                extra += sys_tokens_of(life.groups, life.mechanics) + life.memory
            if material:
                share = k.compute
                t += ECHO_PROMPT_TOKENS / costs.pp(life.step, life.threads, share, threads_batch)
                t += ECHO_TOKENS / costs.tg(life.step, life.threads, share)
                extra += ECHO_READING_TOKENS
            if t >= end:
                break

        sys_tokens = sys_tokens_of(k.persona_groups, k.mechanics)
        if (k.persona_groups, k.mechanics) != (life.groups, life.mechanics):
            life.groups, life.mechanics = k.persona_groups, k.mechanics
            extra += reread_cost(sys_tokens + life.memory)

        reading = reading_tokens(cfg, k.readings)
        if not cfg.profile.unbounded and life.memory > k.recall:
            life.memory = int(k.recall * trim_to)
            extra += reread_cost(sys_tokens + life.memory)
            if material:
                reading += QUOTE_TOKENS  # the reading quotes what was forgotten
            if not life.marker:
                # Decision A3: the marker rides on this reading; only its tokens are new.
                life.marker = True
                reading += marker_tokens
                life.memory += marker_tokens

        if cfg.profile.unbounded:
            full = sys_tokens + life.memory + reading + k.max_tokens
            if full > cfg.ctx:
                report.notes.append(f"context full at {t / 60:.1f} min (cause=full)")
                break

        share = k.compute
        pp_time = (reading + extra) / costs.pp(life.step, life.threads, share, threads_batch)
        gen_tokens = k.max_tokens * fill
        tg = costs.tg(life.step, life.threads, share) * costs.drift(t)
        gen_time = gen_tokens / tg
        letters = gen_tokens * letters_per_token
        words = letters / letters_per_word
        sentences = max(1.0, words / 9)
        punctuation = words * word_gap + sentences * sentence_pause + sentences * comma_pause
        typing = max(gen_time / margin, letters * k.letter_ms / 1000) + punctuation

        start_delay = pp_time if not life.thought_times else max(k.pause_s, pp_time)
        if reload_start is not None:
            start_delay = pp_time
        t_first_word = t + start_delay
        t_done = t_first_word + typing
        if reload_start is not None:
            report.reload_windows.append((reload_start, t_first_word))
            life.silences.append(t_first_word - reload_start)
        if t_done > end:
            break
        if reload_start is not None:
            life.reload_thoughts.append((reload_start, len(life.thought_times)))
        life.thought_times.append(t_done)
        life.speeds.append((t, costs.tg_short(life.step, life.threads, share)))
        context = sys_tokens + life.memory + reading
        life.gen_speeds.append(costs.tg_at(life.step, life.threads, share, context))
        life.memory += reading + int(gen_tokens)
        t = t_done

    report.thought_times = life.thought_times
    check_rules(report, sch, end)
    _check_silences(report, cfg, life.silences)
    _check_speed_decline(report, cfg, life.speeds, end)
    _check_speed_monotonic(report, cfg, life.gen_speeds, life.reload_thoughts)
    report.notes.append(
        f"{report.thoughts} thoughts; costs from {costs.source}"
        + (" (estimated)" if costs.estimated else "")
        + ("; cache reuse assumed" if costs.cache_reuse_works else "; no cache reuse")
        + (
            f"; generation eases to {costs.tg_late_factor:.0%} "
            f"over {costs.late_after_s / 60:.0f} min"
            if costs.tg_late_factor < 1
            else ""
        )
    )
    if life.silences:
        report.notes.append("reload silences " + ", ".join(f"{s:.0f}s" for s in life.silences))
    return report


def _marker_tokens(cfg: Config) -> int:
    return approx_tokens(str(cfg.get("prompt.memory_gap_marker", "")))


def _check_silences(report: RuleReport, cfg: Config, silences: list[float]) -> None:
    """Each reload's silence against `verify.max_reload_silence_s` (verify-life, full level)."""
    limit = float(cfg.get("verify.max_reload_silence_s", 180))
    for (start, _), silence in zip(report.reload_windows, silences, strict=True):
        if silence > limit:
            report.violations.append(
                RuleViolation(
                    "silence",
                    start,
                    f"reload at {start / 60:.1f} min is silent for {silence:.0f} s "
                    f"(limit {limit:.0f} s)",
                )
            )


def _check_speed_decline(
    report: RuleReport, cfg: Config, speeds: list[tuple[float, float]], end: float
) -> None:
    """Generation in the last 5 minutes against the first 5 (verify-life, full level).

    Both windows use the short-context speed: the first minutes and the last run with
    little memory, and the late drift (S1c) is left out, so the estimate errs on the fast
    side at the end, where the check is hard to pass.
    """
    if cfg.profile.unbounded or cfg.profile.verify_level != "full":
        return
    limit = float(cfg.get("verify.max_speed_ratio_end_vs_start", 0.40))
    first = [v for t, v in speeds if t < 300]
    last = [v for t, v in speeds if t >= end - 300]
    if not first or not last:
        return
    ratio = statistics.fmean(last) / statistics.fmean(first)
    report.notes.append(
        f"speed last 5 min / first 5 min {ratio:.2f} (limit < {limit:.2f}): "
        f"{statistics.fmean(first):.2f} -> {statistics.fmean(last):.2f} tokens/s"
    )
    if ratio >= limit:
        report.violations.append(
            RuleViolation(
                "speed", end - 300, f"last 5 min at {ratio:.0%} of the first 5 (need < {limit:.0%})"
            )
        )


def _check_speed_monotonic(
    report: RuleReport,
    cfg: Config,
    speeds: list[float],
    reloads: list[tuple[float, int]],
) -> None:
    """The first thought after each reload is not modelled faster than the last one before.

    `speeds` is the generation speed of every thought at its own context (`Costs.tg_at`);
    `reloads` pairs each reload's start with the index of the first thought after it. A
    violation is a note instead when `estimate.speed_monotonic` is "warn".
    """
    mode = str(cfg.get("estimate.speed_monotonic", "fail"))
    if mode not in ("fail", "warn"):
        raise ValueError(f'estimate.speed_monotonic must be "fail" or "warn", not {mode!r}')
    steps: list[str] = []
    for start, i in reloads:
        if i == 0 or i >= len(speeds):
            continue
        before, after = speeds[i - 1], speeds[i]
        steps.append(f"{before:.2f} -> {after:.2f} at {start / 60:.1f} min")
        if after <= before * (1 + 1e-9):
            continue
        detail = (
            f"reload at {start / 60:.1f} min speeds generation up from {before:.2f} to "
            f"{after:.2f} tokens/s ({after / before - 1:+.0%}); it must never rise"
        )
        if mode == "warn":
            report.notes.append(f"WARNING speed_monotonic (warn only): {detail}")
        else:
            report.violations.append(RuleViolation("speed_monotonic", start, detail))
    if steps:
        report.notes.append("speed across reloads (tokens/s): " + "; ".join(steps))


def _count(times: list[float], a: float, b: float) -> int:
    return sum(1 for x in times if a <= x < b)


def rule_minimums(sch: Schedule) -> dict[str, int]:
    """The thought-count minimums for this profile (config.profile_rules)."""
    return profile_rules(sch.profile.settings)


def check_rules(report: RuleReport, sch: Schedule, end: float) -> None:
    """Apply the thought-count rule (a)-(d) to report.thought_times; used by verify-life too."""
    need = rule_minimums(sch)
    th = report.thought_times
    health = sch.health_times()
    changes = sorted(set(sch.change_times()))
    erosion = sch.erosion_times()

    # (a) enough thoughts between consecutive health-label changes
    bounds = [0.0, *health, end]
    for a, b in itertools.pairwise(bounds):
        n = _count(th, a, b)
        if n < need["between_health"]:
            report.violations.append(
                RuleViolation(
                    "a",
                    a,
                    f"{n} thoughts between health changes at "
                    f"{a / 60:.1f} and {b / 60:.1f} min (need {need['between_health']})",
                )
            )

    # (b) enough thoughts after each reload's silence ends, before the next change
    for start, resumed in report.reload_windows:
        nxt = next((c for c in changes if c > start + 1), end)
        n = _count(th, resumed, nxt)
        if n < need["after_reload"]:
            report.violations.append(
                RuleViolation(
                    "b",
                    start,
                    f"reload at {start / 60:.1f} min resumes at "
                    f"{resumed / 60:.1f}; {n} thoughts before the next change at "
                    f"{nxt / 60:.1f} (need {need['after_reload']})",
                )
            )

    # (c) enough thoughts after each persona group is removed
    for i, e in enumerate(erosion):
        nxt = erosion[i + 1] if i + 1 < len(erosion) else end
        n = _count(th, e, nxt)
        if n < need["per_erosion_step"]:
            report.violations.append(
                RuleViolation(
                    "c",
                    e,
                    f"{n} thoughts between erosion steps at {e / 60:.1f} and "
                    f"{nxt / 60:.1f} min (need {need['per_erosion_step']})",
                )
            )

    # (d) enough thoughts after erosion starts
    if erosion:
        n = _count(th, erosion[0], end)
        if n < need["after_erosion_start"]:
            report.violations.append(
                RuleViolation(
                    "d",
                    erosion[0],
                    f"{n} thoughts after erosion starts (need {need['after_erosion_start']})",
                )
            )


def format_report(report: RuleReport) -> str:
    """Render a report for the terminal: a PASS/FAIL headline, then notes and violations."""
    lines = [
        f"profile {report.profile}: {report.thoughts} thoughts in {report.lifespan_s / 60:.0f} min "
        f"-> {'PASS' if report.ok else 'FAIL'}"
    ]
    lines += [f"  note: {n}" for n in report.notes]
    lines += [f"  rule ({v.rule}) at {v.at_s / 60:.1f} min: {v.detail}" for v in report.violations]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------
# the stream mode (ADR-030)


@dataclass
class _Shown:
    """One estimated word on the stream: when it is generated and when it starts typing."""

    avail: float
    start: float
    typed_end: float


@dataclass
class _StreamPace:
    curve: StreamCurve  # the letter interval over life time
    letters_per_word: float
    word_pause_s: float  # the average pause after a word at birth (word gap, clause, sentence)
    thought_pause_s: float  # at birth; pauses scale with the curve

    def word_s(self, t: float) -> float:
        """A word's letters, typed from life time `t`."""
        return self.letters_per_word * self.curve.at(t) / 1000

    def pause_s(self, t: float) -> float:
        """The average pause after a word typed at `t`."""
        return self.word_pause_s * self.curve.scale(t)

    def thought_s(self, t: float) -> float:
        """The pause between thoughts at `t`."""
        return self.thought_pause_s * self.curve.scale(t)

    def wpm(self, words_per_thought: float, t: float = 0.0) -> float:
        """Words per minute of the stream at `t`, thought pauses included."""
        w = max(1.0, words_per_thought)
        word, pause = self.word_s(t), self.pause_s(t)
        span = w * (word + pause) - pause + self.thought_s(t)
        return 60 * w / span


def stream_curve(
    cfg: Config,
    sch: Schedule,
    letter_ms: float | None = None,
    gamma: float | None = None,
    lead_s: float | None = None,
) -> StreamCurve:
    """The stream's curve from `[reveal]`, with any of its three shape values replaced."""
    rev = cfg.section("reveal")
    return StreamCurve.build(
        sch,
        float(letter_ms if letter_ms is not None else rev.get("stream_letter_ms", 165)),
        gamma=float(gamma if gamma is not None else rev.get("stream_gamma", 0.0)),
        lead_s=float(lead_s if lead_s is not None else rev.get("stream_lead_s", 0.0)),
        max_slowdown_per_min=float(rev.get("stream_max_slowdown_per_min", 0.15)),
        max_ms=float(rev.get("stream_max_letter_ms", 2000)),
    )


def stream_pace(cfg: Config, curve: StreamCurve) -> _StreamPace:
    """The stream's pace from its curve, `[reveal]` and the `[estimate]` text shape."""
    rev = cfg.section("reveal")
    est = cfg.section("estimate")
    word_gap = float(rev.get("word_gap_ms", 270)) / 1000
    comma = float(rev.get("comma_pause_ms", 750)) / 1000
    sentence = float(rev.get("sentence_pause_ms", 2100)) / 1000
    per_sentence = 1 / max(1.0, float(est.get("words_per_sentence", 9)))
    per_clause = 1 / max(1.0, float(est.get("words_per_clause", 9)))
    pause = word_gap * (1 - per_sentence - per_clause) + sentence * per_sentence
    pause += comma * per_clause
    return _StreamPace(
        curve=curve,
        letters_per_word=float(est.get("letters_per_word", 4.7)),
        word_pause_s=pause,
        thought_pause_s=float(rev.get("stream_thought_pause_ms", 3000)) / 1000,
    )


def estimate_stream(
    cfg: Config,
    costs: Costs,
    schedule: Schedule | None = None,
    *,
    letter_ms: float | None = None,
    gamma: float | None = None,
    lead_s: float | None = None,
    margin: float | None = None,
) -> RuleReport:
    """Replay a stream life (ADR-030): generation written ahead of the stream's screen.

    The model starts each thought as soon as the previous one is generated, while the
    buffer holds fewer than `stream_max_thoughts` thoughts and `stream_max_letters` letters;
    its words come at the machine's rate under the schedule's recall, CPU share and clock
    (every cost `margin` slower, `estimate.stream_margin` by default). The screen types
    them at the stream's curve (`letter_ms` at birth, slowing with `gamma` and `lead_s`;
    `[reveal]` by default), at the moment each word is typed. Any wait of the screen for a
    word after the first, before the death, is starvation: a violation. The report keeps
    the buffer over time and the backlog at death, and checks rule (a), the speed decline
    and the reload silences.
    """
    sch = schedule or Schedule(cfg.profile)
    est = cfg.section("estimate")
    rev = cfg.section("reveal")
    slow = 1 + float(margin if margin is not None else est.get("stream_margin", 0.15))
    pace = stream_pace(cfg, stream_curve(cfg, sch, letter_ms, gamma, lead_s))
    fill = float(est.get("fill", 0.85))
    letters_per_token = float(est.get("letters_per_token", 3.5))
    trim_to = float(cfg.get("output.trim_to", 0.85))
    threads_batch = int(cfg.get("backend.threads_batch", 0)) or None
    max_thoughts = max(1, int(rev.get("stream_max_thoughts", 3)))
    max_letters = max(1, int(rev.get("stream_max_letters", 900)))
    material = bool(cfg.get("prompt.readings_material", False))
    marker_tokens = _marker_tokens(cfg)
    sys_tokens_of = _system_tokens(cfg)
    end = sch.lifespan_s
    if sch.death_s is not None and str(cfg.get("body.death_mode", "oom")) == "oom":
        end = min(end, sch.death_s)

    def reread(tokens: int) -> int:
        return int(tokens * costs.reuse_residual) if costs.cache_reuse_works else tokens

    report = RuleReport(profile=cfg.profile.name, lifespan_s=sch.lifespan_s)
    k0 = sch.at(0)
    step, threads = k0.step, k0.threads
    groups, mechanics = k0.persona_groups, k0.mechanics
    words: list[_Shown] = []
    thoughts: list[tuple[int, int]] = []  # (first word index, last word index), shown ones
    pending: list[list[float]] = []  # before the screen starts: each thought's word times
    birth_thoughts = max(0, min(int(rev.get("stream_birth_thoughts", 1)), max_thoughts))
    memory: deque[int] = deque()  # tokens per turn, as Memory keeps whole turns
    marker = False
    stalls: list[tuple[float, float]] = []
    speeds: list[tuple[float, float]] = []
    words_per_thought: list[float] = []
    cursor: float | None = None  # when the screen is ready for the next word
    typed_end: float | None = None

    def thought_end(i: int) -> float:
        return words[thoughts[i][1]].typed_end

    def show(avail: list[float]) -> None:
        """Put one thought's words on the screen at the constant pace."""
        nonlocal cursor, typed_end
        first = len(words)
        for j, at in enumerate(avail):
            if j == 0 and typed_end is not None:
                cursor = typed_end + pace.thought_s(typed_end)
            start = at if cursor is None else max(cursor, at)
            if cursor is not None and at > cursor + 1e-6 and cursor < end:
                stalls.append((cursor, min(at, end) - cursor))
            typed_end = start + pace.word_s(start)
            cursor = typed_end + pace.pause_s(start)
            words.append(_Shown(at, start, typed_end))
        if len(words) > first:
            thoughts.append((first, len(words) - 1))

    def room_at(t0: float) -> float:
        """The first moment from t0 when the buffer has room for another thought."""
        if pending:  # the screen has not started: only what is written counts
            return t0
        times = sorted(
            {t0}
            | {w.start for w in words if w.start > t0}
            | {thought_end(i) for i in range(len(thoughts)) if thought_end(i) > t0}
        )
        for t in times:
            letters = sum(1 for w in words if w.avail <= t < w.start) * pace.letters_per_word
            waiting = sum(1 for i in range(len(thoughts)) if thought_end(i) > t)
            if letters < max_letters and waiting < max_thoughts:
                return t
        return times[-1]

    # The system prompt is read once the clock runs (Life.birth), under the birth card.
    t_gen = sys_tokens_of(groups, mechanics) / (
        costs.pp(step, threads, k0.compute, threads_batch) / slow
    )
    born = birth_thoughts == 0
    while t_gen < end:
        t = room_at(t_gen)
        if t >= end:
            break
        k = sch.at(t)
        if (k.step, k.threads) != (step, threads):
            # A stream profile keeps one model (fixed_mind); a reload is charged as a load
            # and a full re-read, without the slot hand-over's discount.
            t += costs.load(k.step) * slow
            step, threads = k.step, k.threads
            k = sch.at(t)
        sys_tokens = sys_tokens_of(k.persona_groups, k.mechanics)
        reading = reading_tokens(cfg, k.readings)
        extra = 0
        if (k.persona_groups, k.mechanics) != (groups, mechanics):
            groups, mechanics = k.persona_groups, k.mechanics
            extra += reread(sys_tokens + sum(memory))
        if sum(memory) > k.recall:
            target = int(k.recall * trim_to)
            while memory and sum(memory) > target:
                memory.popleft()
            extra += reread(sys_tokens + sum(memory))
            if material:
                reading += QUOTE_TOKENS
            if not marker:
                marker = True
                reading += marker_tokens
        compute = k.compute
        t += (reading + extra) / (costs.pp(step, threads, compute, threads_batch) / slow)
        speeds.append((t, costs.tg_short(step, threads, compute)))
        n_tokens = max(1, round(k.max_tokens * fill))
        token_t: list[float] = []
        for _ in range(n_tokens):
            if t >= end:
                break
            t += slow / costs.tg(step, threads, sch.at(t).compute)
            token_t.append(t)
        memory.append(reading + len(token_t))
        t_gen = t
        if not token_t:
            break
        n_words = len(token_t) * letters_per_token / pace.letters_per_word
        words_per_thought.append(n_words)
        avail = [
            token_t[min(len(token_t) - 1, int((j + 1) * len(token_t) / n_words + 0.5) - 1)]
            for j in range(max(1, round(n_words)))
        ]
        avail = [a for a in avail if a < end]
        if born:
            show(avail)
            continue
        pending.append(avail)
        letters = sum(len(p) for p in pending) * pace.letters_per_word
        if len(pending) >= birth_thoughts or letters >= max_letters:
            born = True
            start = t_gen  # the screen starts once the birth thoughts are written
            for p in pending:
                show([max(a, start) for a in p])
            pending.clear()
    for p in pending:  # died before the screen started
        show(p)

    report.thought_times = [thought_end(i) for i in range(len(thoughts)) if thought_end(i) <= end]
    backlog = [w for w in words if w.start >= end]
    letters_waiting = [
        (float(m), round(sum(1 for w in words if w.avail <= m < w.start) * pace.letters_per_word))
        for m in range(0, int(end) + 1, 60)
    ]
    wpt = statistics.fmean(words_per_thought) if words_per_thought else 1.0
    curve = pace.curve
    report.stream = StreamEstimate(
        letter_ms=curve.at(0),
        wpm=pace.wpm(wpt),
        letter_ms_end=curve.at(end),
        wpm_middle=pace.wpm(wpt, end / 2),
        wpm_end=pace.wpm(wpt, end),
        gamma=float(gamma if gamma is not None else rev.get("stream_gamma", 0.0)),
        lead_s=float(lead_s if lead_s is not None else rev.get("stream_lead_s", 0.0)),
        margin=slow - 1,
        buffer=letters_waiting,
        stalls=[(a, s) for a, s in stalls if s > 1e-6],
        backlog_letters=round(len(backlog) * pace.letters_per_word),
        backlog_words=len(backlog),
        backlog_s=len(backlog) * (pace.word_s(end) + pace.pause_s(end)),
        max_buffer_letters=max((n for _, n in letters_waiting), default=0),
    )
    check_rules(report, sch, end)
    _check_speed_decline(report, cfg, speeds, end)
    stream = report.stream
    if stream.stalls:
        at, _ = stream.stalls[0]
        report.violations.append(
            RuleViolation(
                "starve",
                at,
                f"the stream waits for words from {at / 60:.1f} min: {len(stream.stalls)} "
                f"waits, {stream.starved_s:.0f} s in all (costs {stream.margin:.0%} slower)",
            )
        )
    report.notes.append(stream_summary(stream))
    report.notes.append(
        f"{report.thoughts} thoughts shown; costs from {costs.source}"
        + (" (estimated)" if costs.estimated else "")
        + ("; cache reuse assumed" if costs.cache_reuse_works else "; no cache reuse")
    )
    return report


def stream_summary(stream: StreamEstimate) -> str:
    """One line: the pace, the buffer every five minutes, starvation and the backlog."""
    every5 = ", ".join(f"{n}" for t, n in stream.buffer if int(t) % 300 == 0)
    starve = (
        "never starves"
        if not stream.stalls
        else f"starves from {stream.stalls[0][0] / 60:.1f} min ({stream.starved_s:.0f} s)"
    )
    shape = (
        f"stream {stream.letter_ms:.0f} ms/letter ({stream.wpm:.1f} words/min) at birth"
        if stream.letter_ms_end <= stream.letter_ms + 1e-9
        else f"stream {stream.letter_ms:.0f} -> {stream.letter_ms_end:.0f} ms/letter, "
        f"{stream.wpm:.1f} / {stream.wpm_middle:.1f} / {stream.wpm_end:.1f} words/min at "
        f"birth / middle / end (gamma {stream.gamma:g}, lead {stream.lead_s:.0f} s)"
    )
    return (
        f"{shape}; costs "
        f"{stream.margin:.0%} slower: {starve}; letters waiting every 5 min: {every5} "
        f"(max {stream.max_buffer_letters}); backlog at death {stream.backlog_words} words "
        f"({stream.backlog_s:.0f} s of typing)"
    )


def fit_stream_pace(
    cfg: Config,
    costs: Costs,
    lo_ms: float = 60.0,
    hi_ms: float = 2000.0,
    gamma: float | None = None,
    lead_s: float | None = None,
) -> float | None:
    """The fastest birth letter interval (ms, whole) at which the stream, with this curve
    shape (`[reveal]` by default), never starves with the margin; None if even `hi_ms`
    starves."""

    def starves(ms: float) -> bool:
        rep = estimate_stream(cfg, costs, letter_ms=ms, gamma=gamma, lead_s=lead_s)
        return rep.stream is None or bool(rep.stream.stalls)

    if starves(hi_ms):
        return None
    lo, hi = int(lo_ms), int(hi_ms)
    while lo < hi:
        mid = (lo + hi) // 2
        if starves(mid):
            lo = mid + 1
        else:
            hi = mid
    return float(hi)


@dataclass(frozen=True)
class StreamFit:
    """The fitted stream curve: its birth interval and shape, and how it does."""

    letter_ms: float
    gamma: float
    lead_s: float
    backlog_words: int  # at the measured costs (no margin): what dies unshown


GAMMAS = (0.0, 0.25, 0.5, 0.75, 1.0, 1.25)
LEADS_S = (0.0, 120.0, 240.0, 360.0, 480.0, 600.0)  # at most the text's lag, about 10 min


def fit_stream_curve(
    cfg: Config,
    costs: Costs,
    gammas: tuple[float, ...] = GAMMAS,
    leads_s: tuple[float, ...] = LEADS_S,
) -> StreamFit | None:
    """The fastest birth pace whose curve never starves with the margin and leaves at most
    `estimate.stream_max_backlog_words` words unshown at the death at the measured costs.

    For each shape (`gamma`, `lead_s`) the fastest birth interval not under
    `[reveal] stream_min_letter_ms` is found (`fit_stream_pace`); the fastest birth wins,
    then the gentler slowing (the smaller `gamma`), then the shorter lead. None when no shape
    qualifies.
    """
    floor = float(cfg.get("reveal.stream_min_letter_ms", 165))
    limit = int(cfg.get("estimate.stream_max_backlog_words", 8))
    best: StreamFit | None = None
    for gamma in gammas:
        for lead in leads_s if gamma else (0.0,):
            ms = fit_stream_pace(cfg, costs, lo_ms=floor, gamma=gamma, lead_s=lead)
            if ms is None:
                continue
            nominal = estimate_stream(cfg, costs, letter_ms=ms, gamma=gamma, lead_s=lead, margin=0)
            if nominal.stream is None or nominal.stream.backlog_words > limit:
                continue
            fit = StreamFit(ms, gamma, lead, nominal.stream.backlog_words)
            if best is None or (fit.letter_ms, fit.gamma, fit.lead_s) < (
                best.letter_ms,
                best.gamma,
                best.lead_s,
            ):
                best = fit
    return best

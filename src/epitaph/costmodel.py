"""The cost model: a thought-by-thought estimate of a life from machine costs (BUILD_PLAN 5.3).

It answers one question before any Pi time is spent: with these costs, does the profile give
the model enough thoughts after every loss to notice it? Costs come from bench/*.json
(measured by spikes S1b, S2 and S4) or, until then, from the hardware overlay's estimates.

Besides the thought-count rule (a)-(d), an estimate fails two checks that verify-life makes
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
  the deep rate stands for every context. The life drift (below) is left out on both sides,
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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from epitaph.clock import Schedule
from epitaph.config import REPO_ROOT, Config, reading_tokens, system_tokens
from epitaph.mind.memory import approx_tokens
from epitaph.mind.prompt import Persona
from epitaph.types import RuleReport, RuleViolation


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
    speed decline (see the module notes)."""
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
            # A fresh server reads everything, the system prompt included (prefilled
            # during the silence, so it is part of the silence either way).
            extra += sys_tokens_of(life.groups, life.mechanics) + life.memory
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

        share = k.cpu_share
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


def check_rules(report: RuleReport, sch: Schedule, end: float) -> None:
    """Apply the thought-count rule (a)-(d) to report.thought_times; used by verify-life too."""
    th = report.thought_times
    health = sch.health_times()
    changes = sorted(set(sch.change_times()))
    erosion = sch.erosion_times()

    # (a) at least 3 thoughts between consecutive health-label changes
    bounds = [0.0, *health, end]
    for a, b in itertools.pairwise(bounds):
        n = _count(th, a, b)
        if n < 3:
            report.violations.append(
                RuleViolation(
                    "a",
                    a,
                    f"{n} thoughts between health changes at "
                    f"{a / 60:.1f} and {b / 60:.1f} min (need 3)",
                )
            )

    # (b) at least 2 thoughts after each reload's silence ends, before the next change
    for start, resumed in report.reload_windows:
        nxt = next((c for c in changes if c > start + 1), end)
        n = _count(th, resumed, nxt)
        if n < 2:
            report.violations.append(
                RuleViolation(
                    "b",
                    start,
                    f"reload at {start / 60:.1f} min resumes at "
                    f"{resumed / 60:.1f}; {n} thoughts before the next change at "
                    f"{nxt / 60:.1f} (need 2)",
                )
            )

    # (c) at least 1 thought after each persona group is removed
    for i, e in enumerate(erosion):
        nxt = erosion[i + 1] if i + 1 < len(erosion) else end
        n = _count(th, e, nxt)
        if n < 1:
            report.violations.append(
                RuleViolation(
                    "c",
                    e,
                    f"no thought between erosion steps at {e / 60:.1f} and {nxt / 60:.1f} min",
                )
            )

    # (d) at least 4 thoughts after erosion starts
    if erosion:
        n = _count(th, erosion[0], end)
        if n < 4:
            report.violations.append(
                RuleViolation("d", erosion[0], f"{n} thoughts after erosion starts (need 4)")
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

"""The cost model: a thought-by-thought estimate of a life from machine costs (BUILD_PLAN 5.3).

It answers one question before any Pi time is spent: with these costs, does the profile give
the model enough thoughts after every loss to notice it? Costs come from bench/*.json
(measured by spikes S1b, S2 and S4) or, until then, from the hardware overlay's estimates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from epitaph.clock import Schedule
from epitaph.config import REPO_ROOT, Config, reading_tokens, system_tokens
from epitaph.types import RuleReport, RuleViolation


@dataclass
class Costs:
    """Machine costs at full CPU share. Keys are "<step>-<threads>"."""

    tg_tok_s: dict[str, float]
    pp_tok_s: dict[str, float]
    load_s: list[float]
    estimated: bool = True
    source: str = "overlay"
    cache_reuse_works: bool = True
    reuse_residual: float = 0.1  # share of the prompt still re-read when reuse works

    def _rate(self, table: dict[str, float], step: int, threads: int) -> float:
        key = f"{step}-{threads}"
        if key in table:
            return table[key]
        # Nearest known thread count for this step, scaled linearly by threads.
        same_step = [(int(k.split("-")[1]), v) for k, v in table.items() if k.startswith(f"{step}-")]
        if same_step:
            th, v = min(same_step, key=lambda kv: abs(kv[0] - threads))
            return v * threads / th
        return min(table.values())

    def tg(self, step: int, threads: int, share: float) -> float:
        return self._rate(self.tg_tok_s, step, threads) * min(1.0, share / threads)

    def pp(self, step: int, threads: int, share: float) -> float:
        return self._rate(self.pp_tok_s, step, threads) * min(1.0, share / threads)

    def load(self, step: int) -> float:
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
    for path in measured:
        rec: dict[str, Any] = json.loads(path.read_text())
        key = f"{rec['step']}-{rec['threads']}"
        if "tg_tok_s" in rec:
            costs.tg_tok_s[key] = float(rec["tg_tok_s"])
        if "pp_tok_s" in rec:
            costs.pp_tok_s[key] = float(rec["pp_tok_s"])
        if "load_s" in rec:
            step = int(rec["step"])
            while len(costs.load_s) <= step:
                costs.load_s.append(costs.load_s[-1])
            costs.load_s[step] = float(rec["load_s"])
        if "cache_reuse_works" in rec:
            costs.cache_reuse_works = bool(rec["cache_reuse_works"])
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
    last_reload: float = -1e9
    thought_times: list[float] = field(default_factory=lambda: [])
    reload_windows: list[tuple[float, float]] = field(default_factory=lambda: [])
    silences: list[float] = field(default_factory=lambda: [])


def estimate(cfg: Config, costs: Costs, schedule: Schedule | None = None) -> RuleReport:
    """Simulate one life and check the thought-count rule (a)-(d)."""
    sch = schedule or Schedule(cfg.profile)
    est = cfg.section("estimate")
    reveal = cfg.section("reveal")
    fill = float(est.get("fill", 0.85))
    letters_per_token = float(est.get("letters_per_token", 4.2))
    margin = float(reveal.get("rate_margin", 0.88))
    word_gap = float(reveal.get("word_gap_ms", 90)) / 1000
    sentence_pause = float(reveal.get("sentence_pause_ms", 700)) / 1000
    comma_pause = float(reveal.get("comma_pause_ms", 250)) / 1000
    trim_to = float(cfg.get("output.trim_to", 0.85))
    min_gap = float(cfg.get("life.min_reload_gap_s", 120))
    groups_total = len(cfg.get("prompt.persona_groups", []))
    end = sch.lifespan_s
    if sch.death_s is not None and str(cfg.get("body.death_mode", "oom")) == "oom":
        end = min(end, sch.death_s)

    report = RuleReport(profile=cfg.profile.name, lifespan_s=sch.lifespan_s)
    k0 = sch.at(0)
    life = _Life(step=k0.step, threads=k0.threads, groups=k0.persona_groups)
    t = 0.0

    def reread_cost(tokens: int) -> int:
        if costs.cache_reuse_works:
            return int(tokens * costs.reuse_residual)
        return tokens

    while t < end:
        k = sch.at(t)
        sys_tokens = system_tokens(cfg, k.persona_groups, k.mechanics)
        extra = 0
        reload_start: float | None = None

        if (k.step, k.threads) != (life.step, life.threads) and t - life.last_reload >= min_gap:
            reload_start = t
            life.memory = min(life.memory, k.recall)
            t += costs.load(k.step)
            life.step, life.threads, life.last_reload = k.step, k.threads, reload_start
            extra += sys_tokens + life.memory  # a fresh server reads everything
            if t >= end:
                break

        if k.persona_groups != life.groups:
            life.groups = k.persona_groups
            extra += reread_cost(sys_tokens + life.memory)

        if not cfg.profile.unbounded and life.memory > k.recall:
            life.memory = int(k.recall * trim_to)
            cost = reread_cost(sys_tokens + life.memory)
            if not life.marker:
                life.marker = True
            extra += cost

        reading = reading_tokens(cfg, k.readings)
        if cfg.profile.unbounded:
            full = sys_tokens + life.memory + reading + k.max_tokens
            if full > cfg.ctx:
                report.notes.append(f"context full at {t / 60:.1f} min (cause=full)")
                break

        share = k.cpu_share
        pp_time = (reading + extra) / costs.pp(life.step, life.threads, share)
        gen_tokens = k.max_tokens * fill
        gen_time = gen_tokens / costs.tg(life.step, life.threads, share)
        letters = gen_tokens * letters_per_token
        words = letters / 5.5
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
        life.thought_times.append(t_done)
        life.memory += reading + int(gen_tokens)
        t = t_done

    report.thought_times = life.thought_times
    _check_rules(report, sch, end)
    report.notes.append(
        f"{report.thoughts} thoughts; costs from {costs.source}"
        + (" (estimated)" if costs.estimated else "")
        + ("; cache reuse assumed" if costs.cache_reuse_works else "; no cache reuse")
    )
    if life.silences:
        report.notes.append("reload silences " + ", ".join(f"{s:.0f}s" for s in life.silences))
    _ = groups_total
    return report


def _count(times: list[float], a: float, b: float) -> int:
    return sum(1 for x in times if a <= x < b)


def _check_rules(report: RuleReport, sch: Schedule, end: float) -> None:
    th = report.thought_times
    health = sch.health_times()
    changes = sorted(set(sch.change_times()))
    erosion = sch.erosion_times()

    # (a) at least 3 thoughts between consecutive health-label changes
    bounds = [0.0, *health, end]
    for a, b in zip(bounds[1:-1], bounds[2:], strict=False):
        n = _count(th, a, b)
        if n < 3:
            report.violations.append(
                RuleViolation("a", a, f"{n} thoughts between health changes at "
                              f"{a / 60:.1f} and {b / 60:.1f} min (need 3)")
            )

    # (b) at least 2 thoughts after each reload's silence ends, before the next change
    for start, resumed in report.reload_windows:
        nxt = next((c for c in changes if c > start + 1), end)
        n = _count(th, resumed, nxt)
        if n < 2:
            report.violations.append(
                RuleViolation("b", start, f"reload at {start / 60:.1f} min resumes at "
                              f"{resumed / 60:.1f}; {n} thoughts before the next change at "
                              f"{nxt / 60:.1f} (need 2)")
            )

    # (c) at least 1 thought after each persona group is removed
    for i, e in enumerate(erosion):
        nxt = erosion[i + 1] if i + 1 < len(erosion) else end
        n = _count(th, e, nxt)
        if n < 1:
            report.violations.append(
                RuleViolation("c", e, f"no thought between erosion steps at {e / 60:.1f} "
                              f"and {nxt / 60:.1f} min")
            )

    # (d) at least 4 thoughts after erosion starts
    if erosion:
        n = _count(th, erosion[0], end)
        if n < 4:
            report.violations.append(
                RuleViolation("d", erosion[0], f"{n} thoughts after erosion starts (need 4)")
            )


def format_report(report: RuleReport) -> str:
    lines = [
        f"profile {report.profile}: {report.thoughts} thoughts in {report.lifespan_s / 60:.0f} min "
        f"-> {'PASS' if report.ok else 'FAIL'}"
    ]
    lines += [f"  note: {n}" for n in report.notes]
    lines += [f"  rule ({v.rule}) at {v.at_s / 60:.1f} min: {v.detail}" for v in report.violations]
    return "\n".join(lines)

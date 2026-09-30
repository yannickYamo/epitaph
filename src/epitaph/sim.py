"""`epitaph sim`: whole lives on the fake clock with the fake backend (BUILD_PLAN 9 L4).

Phase 0a reference loop. It emits every event of the contract in the order section 5.8
defines, so displays, verify-life and transcripts can be built before the real controller
exists. From phase 1, part B's controller runs here on the fakes instead of this loop.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from epitaph.backend.base import CreatureDied
from epitaph.backend.fake import FakeBackend
from epitaph.body.fake import FakeBody
from epitaph.clock import FakeClock, Schedule
from epitaph.config import Config
from epitaph.costmodel import load_costs
from epitaph.events import Event, make_event
from epitaph.types import Cause, Msg, Sampling


@dataclass
class SimResult:
    events: list[Event] = field(default_factory=lambda: [])
    causes: list[str] = field(default_factory=lambda: [])
    thoughts: list[int] = field(default_factory=lambda: [])


def _reading(t: float, k: Any, prev: Any | None, forgotten: int, vitals: Any) -> str:
    def was(field_: str, fmt: Callable[[Any], str]) -> str:
        if prev is None or getattr(prev, field_) == getattr(k, field_):
            return ""
        return f" (was {fmt(getattr(prev, field_))})"

    m, s = divmod(int(t), 60)
    bits = {0: "6", 1: "4", 2: "2"}
    if k.readings == "minimal":
        return f"[host] {m}:{s:02d} · {k.health.value} · {k.recall}"
    parts = [f"[host] t+{m:02d}:{s:02d}", f"health: {k.health.value}"]
    parts.append(f"memory {k.recall} tokens" + was("recall", str))
    if forgotten:
        parts.append(f"forgotten: {forgotten} earlier thoughts")
    parts.append(
        f"precision {bits.get(k.step, '2')}-bit" + was("step", lambda v: bits.get(v, "2") + "-bit")
    )
    parts.append(f"cores {k.cpu_share:.1f} of 4" + was("cpu_share", lambda v: f"{v:.1f}"))
    if vitals.cpu_c is not None:
        parts.append(f"cpu {vitals.cpu_c:.0f}°C")
    return " · ".join(parts)


async def run_life(
    cfg: Config, n: int, clock: FakeClock, emit: Callable[[Event], None], seed: int = 0
) -> tuple[str, int]:
    """One life from load to death_shown. Returns (cause, thoughts shown)."""
    sch = Schedule(cfg.profile)
    costs = load_costs(cfg)
    backend = FakeBackend(clock, costs, seed=seed + n)
    body = FakeBody()
    model = cfg.model()
    rng = random.Random(seed * 1000 + n)
    groups = list(cfg.get("prompt.persona_groups", []))
    end = sch.lifespan_s
    death_at = sch.death_s if str(cfg.get("body.death_mode", "oom")) == "oom" else None
    margin = float(cfg.get("reveal.rate_margin", 0.88))
    letters_per_token = float(cfg.get("estimate.letters_per_token", 3.5))
    gap_ms = int(cfg.get("reveal.word_gap_ms", 90))
    comma_ms = int(cfg.get("reveal.comma_pause_ms", 250))
    sentence_ms = int(cfg.get("reveal.sentence_pause_ms", 700))

    def ev(etype: str, **f: Any) -> None:
        e = make_event(etype, n, **f)
        e["t"] = round(clock.elapsed(), 2)
        emit(e)

    k = sch.at(0)
    ev("birth_loading", model=model.name, step=k.step, quant=model.quant(k.step), facts={})
    await backend.start(model, model.quant(k.step), k.threads)
    clock.start()
    ev("birth", model=model.name, step=k.step, quant=model.quant(k.step), threads=k.threads)

    memory: list[tuple[int, int]] = []  # (turn, tokens)
    cur = (k.step, k.threads)
    cur_groups = k.persona_groups
    prev = None
    turn = 0
    last_reload = -1e9
    cause = Cause.DEADLINE
    forgotten_since = 0
    marker = False
    try:
        while True:
            t = clock.elapsed()
            if t >= end:
                cause = Cause.DEADLINE
                break
            k = sch.at(t)
            if death_at is not None and t >= death_at:
                cause = Cause.OOM
                break
            if (k.step, k.threads) != cur and t - last_reload >= float(
                cfg.get("life.min_reload_gap_s", 120)
            ):
                before = sum(x for _, x in memory)
                while memory and sum(x for _, x in memory) > k.recall:
                    memory.pop(0)
                    forgotten_since += 1
                ev(
                    "reload",
                    **{"from": model.quant(cur[0]), "to": model.quant(k.step)},
                    threads=k.threads,
                    recall_before=before,
                    recall_after=sum(x for _, x in memory),
                )
                t0 = clock.elapsed()
                await backend.start(model, model.quant(k.step), k.threads)
                cur, last_reload = (k.step, k.threads), t
                ev("reload_done", seconds=round(clock.elapsed() - t0, 1))
                if clock.elapsed() >= end:
                    cause = Cause.DEADLINE
                    break
            body.apply(k)
            backend.set_cpu_share(k.cpu_share)
            if k.persona_groups != cur_groups:
                cur_groups = k.persona_groups
                ev("erosion", groups_left=cur_groups, mechanics_present=k.mechanics)
            if cfg.profile.unbounded and sum(x for _, x in memory) + 400 + k.max_tokens > cfg.ctx:
                cause = Cause.FULL
                break
            if not cfg.profile.unbounded and sum(x for _, x in memory) > k.recall:
                target = int(k.recall * float(cfg.get("output.trim_to", 0.85)))
                dropped: list[dict[str, Any]] = []
                while memory and sum(x for _, x in memory) > target:
                    tt, _ = memory.pop(0)
                    dropped.append({"turn": tt, "all": True})
                    forgotten_since += 1
                ev("forget", items=dropped)
                marker = True
            vit = body.vitals()
            reading = _reading(t, k, prev, forgotten_since, vit)
            ev(
                "vitals",
                t=round(t, 1),
                phase=k.phase,
                health=k.health.value,
                recall=k.recall,
                recall_used=sum(x for _, x in memory),
                forgotten_since_last=forgotten_since,
                step=k.step,
                quant=model.quant(k.step),
                threads=k.threads,
                cpu_share=k.cpu_share,
                cores_effective=k.cpu_share,
                tok_s=costs.tg(k.step, k.threads, k.cpu_share),
                cpu_c=vit.cpu_c,
                ram_limit_mb=None,
                reading=reading,
                marker=marker,
            )
            forgotten_since = 0
            prev = k
            msgs = [
                Msg("system", " ".join(groups[:cur_groups]), kind="persona"),
                Msg("user", reading),
            ]
            sampling = Sampling(temperature=k.temperature, min_p=k.min_p)
            turn += 1
            ev("gen_start", turn=turn)
            ev("thought_start", turn=turn)
            words: list[str] = []
            buf = ""
            tokens = 0
            # When the last letter so far will have been typed: a word starts when it is
            # released and the previous word (with its pause) is done (BUILD_PLAN 5.12).
            typed_until = clock.elapsed()
            # Adaptive cadence (BUILD_PLAN 5.12): never type faster than 88% of generation.
            letters_per_s = costs.tg(k.step, k.threads, k.cpu_share) * letters_per_token
            interval = max(k.letter_ms, 1000 / (margin * letters_per_s))
            async for chunk in backend.chat(msgs, sampling, k.max_tokens):
                now = clock.elapsed()
                if death_at is not None and now >= death_at:
                    backend.kill(9)  # the kernel's OOM kill lands mid-thought
                if now >= end:
                    backend.kill(9)
                if chunk.done:
                    tokens = chunk.predicted_n or 0
                    break
                buf += chunk.text
                while " " in buf:
                    w, buf = buf.split(" ", 1)
                    if w:
                        words.append(w)
                        cms = [int(interval * (1 + k.jitter * (rng.random() - 0.5))) for _ in w]
                        pause = (
                            sentence_ms
                            if w[-1] in ".?!"
                            else (comma_ms if w[-1] in ",;:" else gap_ms)
                        )
                        typed_until = max(typed_until, clock.elapsed()) + (sum(cms) + pause) / 1000
                        ev(
                            "word",
                            turn=turn,
                            i=len(words) - 1,
                            text=w,
                            char_ms=cms,
                            pause_after_ms=pause,
                        )
            if buf.strip():
                words.append(buf.strip())
                ev(
                    "word",
                    turn=turn,
                    i=len(words) - 1,
                    text=buf.strip(),
                    char_ms=[int(interval) for _ in buf.strip()],
                    pause_after_ms=sentence_ms,
                )
                typed_until = (
                    max(typed_until, clock.elapsed())
                    + (interval * len(buf.strip()) + sentence_ms) / 1000
                )
            ev("gen_end", turn=turn, tokens=tokens)
            # The sync rule: the thought ends when its last letter has been typed.
            await clock.sleep(max(0.0, typed_until - clock.elapsed()))
            text = " ".join(words)
            ev("thought_end", turn=turn, text=text)
            memory.append((turn, tokens + 45))
            await clock.sleep(k.pause_s)
    except CreatureDied:
        now = clock.elapsed()
        if death_at is not None and now >= death_at and now < end:
            cause = Cause.OOM
        elif now >= end:
            cause = Cause.DEADLINE
        else:
            cause = Cause.CRASH
    ev("death", cause=cause.value, lived_s=round(clock.elapsed(), 1), model=model.name)
    ev("death_shown", last_line="", words_total=0)
    silence = float(cfg.get("life.silence_seconds", 90))
    ev("silence", seconds=silence, style=str(cfg.get("display.silence_style", "dark")))
    await clock.sleep(silence)
    return cause.value, turn


def simulate(cfg: Config, lives: int = 1, seed: int = 0) -> SimResult:
    """Run lives back to back on one fake clock."""
    result = SimResult()
    clock = FakeClock()

    async def main() -> None:
        for n in range(1, lives + 1):
            cause, thoughts = await run_life(cfg, n, clock, result.events.append, seed)
            result.causes.append(cause)
            result.thoughts.append(thoughts)

    asyncio.run(main())
    return result

"""`epitaph sim`: whole lives on the fake clock with the fake backend (BUILD_PLAN 9 L4).

A reference loop that emits every event of the contract in the order BUILD_PLAN 5.8
defines, so displays, verify-life and transcripts can be built and tested without a Pi or a
model. The mind is the real one: `Memory` (recall, trims, reload cuts, the marker),
`Persona` (erosion) and `Reader` (the readings), so the simulator shows the real prompts
and the real forgetting. Words are typed with a simple cadence model rather than the full
pacer. Once the real controller exists, it runs here on the same fakes instead of this loop.

Event conventions (contract decisions E2, E3, D2, D5, D6, E4):

- every event carries `t`, the life clock in seconds; 0 before birth;
- `birth_loading` carries the resolved `profile`, `hardware` and `lifespan_s`;
- every memory cut emits `forget`, the cut at a reload included;
- `gen_end` carries `prompt_n` and `tok_s`.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from epitaph.backend.base import CreatureDied
from epitaph.backend.errors import ContextFull
from epitaph.backend.fake import SIGKILL, FakeBackend
from epitaph.body.fake import FakeBody
from epitaph.clock import FakeClock, Schedule
from epitaph.config import Config
from epitaph.costmodel import load_costs
from epitaph.events import Event, make_event
from epitaph.mind.memory import Memory
from epitaph.mind.prompt import Persona, Reader, ReadingInput
from epitaph.mind.sampling import sampling_for
from epitaph.types import Cause


@dataclass
class SimResult:
    """What a simulation produced: every event in order, and each life's cause and thoughts."""

    events: list[Event] = field(default_factory=lambda: [])
    causes: list[str] = field(default_factory=lambda: [])
    thoughts: list[int] = field(default_factory=lambda: [])


async def run_life(
    cfg: Config, n: int, clock: FakeClock, emit: Callable[[Event], None], seed: int = 0
) -> tuple[str, int]:
    """One life from load to death_shown. Returns (cause, thoughts shown)."""
    sch = Schedule(cfg.profile)
    costs = load_costs(cfg)
    backend = FakeBackend(
        clock,
        costs,
        seed=seed + n,
        ctx=cfg.ctx,
        cache_reuse_min=int(cfg.get("backend.cache_reuse", 32)) or 32,
        reload_handover=str(cfg.get("backend.reload_handover", "reread")),
    )
    body = FakeBody()
    model = cfg.model()
    rng = random.Random(seed * 1000 + n)
    end = sch.lifespan_s
    death_at = sch.death_s if str(cfg.get("body.death_mode", "oom")) == "oom" else None
    margin = float(cfg.get("reveal.rate_margin", 0.88))
    letters_per_token = float(cfg.get("estimate.letters_per_token", 3.5))
    pauses = (
        int(cfg.get("reveal.word_gap_ms", 90)),
        int(cfg.get("reveal.comma_pause_ms", 250)),
        int(cfg.get("reveal.sentence_pause_ms", 700)),
    )
    trim_to = float(cfg.get("output.trim_to", 0.85))
    min_gap = float(cfg.get("life.min_reload_gap_s", 120))
    persona = Persona.from_config(cfg, body.facts())
    reader = Reader.from_config(cfg)
    memory = Memory(marker=str(cfg.get("prompt.memory_gap_marker", "[host] earlier memory lost")))
    born = False

    def ev(etype: str, **f: Any) -> None:
        e = make_event(etype, n, **f)
        e["t"] = round(clock.elapsed(), 2) if born else 0.0
        emit(e)

    k = sch.at(0)
    ev(
        "birth_loading",
        model=model.name,
        step=k.step,
        quant=model.quant(k.step),
        facts={},
        profile=cfg.profile.name,
        hardware=cfg.hardware,
        lifespan_s=sch.lifespan_s,
    )
    await backend.start(model, model.quant(k.step), k.threads)
    clock.start()
    born = True
    # The body kills on time, whatever the creature is doing: the death squeeze (OOM) and
    # the deadline's SIGKILL, even in the middle of prompt processing.
    if death_at is not None:
        backend.faults.oom_at_s = clock.now() + death_at
    backend.faults.crash_at_s = clock.now() + end
    backend.faults.crash_signal = SIGKILL
    ev("birth", model=model.name, step=k.step, quant=model.quant(k.step), threads=k.threads)
    persona.update(k.persona_groups, k.mechanics)
    memory.set_system(persona.text)

    cur = (k.step, k.threads)
    turn = 0
    last_reload = -1e9
    last_tok_s: float | None = None
    cause = Cause.DEADLINE
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
            reloaded = False
            if (k.step, k.threads) != cur and t - last_reload >= min_gap:
                # The reload is also a memory loss (BUILD_PLAN 5.4), shown like any other.
                cut = memory.cut_for_reload(k.recall, trim_to)
                ev(
                    "reload",
                    **{"from": model.quant(cur[0]), "to": model.quant(k.step)},
                    threads=k.threads,
                    recall_before=cut.tokens_before,
                    recall_after=cut.tokens_after,
                )
                if cut.items:
                    ev("forget", items=cut.items)
                t0 = clock.elapsed()
                await backend.start(model, model.quant(k.step), k.threads)
                cur, last_reload, reloaded = (k.step, k.threads), t, True
                ev("reload_done", seconds=round(clock.elapsed() - t0, 1))
                if clock.elapsed() >= end:
                    cause = Cause.DEADLINE
                    break
                t = clock.elapsed()
                k = sch.at(t)
            body.apply(k)
            backend.set_cpu_share(k.cpu_share)
            if not cfg.profile.unbounded:
                cut = memory.fit(k.recall, trim_to)
                if cut.items:
                    ev("forget", items=cut.items)
            step = persona.update(k.persona_groups, k.mechanics)
            if step is not None:
                memory.set_system(persona.text)
                ev(
                    "erosion",
                    groups_left=step.groups_left,
                    mechanics_present=step.mechanics_present,
                )
            vit = body.vitals()
            forgotten = memory.take_forgotten()
            reading = reader.reading(
                ReadingInput(
                    t=t,
                    health=k.health.value,
                    recall=k.recall,
                    quant=model.quant(cur[0]),
                    cores=k.cpu_share,
                    cores_total=body.facts().cores,
                    form=k.readings,
                    forgotten=forgotten,
                    reloaded=reloaded,
                    tok_s=last_tok_s,
                    cpu_c=vit.cpu_c,
                )
            )
            if cfg.profile.unbounded and not memory.fits(
                cfg.ctx, k.max_tokens, memory.count(reading)
            ):
                cause = Cause.FULL
                break
            ev(
                "vitals",
                phase=k.phase,
                health=k.health.value,
                recall=k.recall,
                recall_used=memory.used(),
                forgotten_since_last=forgotten,
                step=cur[0],
                quant=model.quant(cur[0]),
                threads=cur[1],
                cpu_share=k.cpu_share,
                cores_effective=k.cpu_share,
                tok_s=costs.tg(cur[0], cur[1], k.cpu_share),
                cpu_c=vit.cpu_c,
                ram_limit_mb=None,
                reading=reading,
                marker=memory.gap,
            )
            turn += 1
            memory.append_host(reading, turn)
            msgs = memory.messages()
            sampling = sampling_for(cfg.section("sampling"), k, cur[0])
            ev("gen_start", turn=turn)
            ev("thought_start", turn=turn)
            words: list[str] = []
            buf = ""
            tokens = 0
            prompt_n = 0
            tok_s: float | None = None
            # When the last letter so far will have been typed: a word starts when it is
            # released and the previous word (with its pause) is done (BUILD_PLAN 5.12).
            typed_until = clock.elapsed()
            # Adaptive cadence (BUILD_PLAN 5.12): never type faster than 88% of generation.
            letters_per_s = costs.tg(cur[0], cur[1], k.cpu_share) * letters_per_token
            interval = max(k.letter_ms, 1000 / (margin * letters_per_s))

            def word_event(
                w: str,
                final: bool,
                i: int,
                turn: int = turn,
                iv: float = interval,
                j: float = k.jitter,
            ) -> float:
                """Emit one word; return the seconds its letters and pause take."""
                cms, pause = _cadence(w, final, iv, j, rng, pauses)
                ev("word", turn=turn, i=i, text=w, char_ms=cms, pause_after_ms=pause)
                return (sum(cms) + pause) / 1000

            async for chunk in backend.chat(msgs, sampling, k.max_tokens):
                if chunk.done:
                    tokens = chunk.predicted_n or 0
                    prompt_n = chunk.prompt_n or 0
                    tok_s = chunk.predicted_per_s
                    break
                buf += chunk.text
                while " " in buf:
                    w, buf = buf.split(" ", 1)
                    if w:
                        words.append(w)
                        typed_until = max(typed_until, clock.elapsed()) + word_event(
                            w, False, i=len(words) - 1
                        )
            if buf.strip():
                words.append(buf.strip())
                typed_until = max(typed_until, clock.elapsed()) + word_event(
                    buf.strip(), True, i=len(words) - 1
                )
            ev("gen_end", turn=turn, tokens=tokens, prompt_n=prompt_n, tok_s=tok_s)
            last_tok_s = tok_s or last_tok_s
            # The sync rule: the thought ends when its last letter has been typed.
            await clock.sleep(max(0.0, typed_until - clock.elapsed()))
            memory.append_thought(words)
            ev("thought_end", turn=turn, text=" ".join(words))
            await clock.sleep(k.pause_s)
    except ContextFull:
        cause = Cause.FULL  # the unbounded life's end (BUILD_PLAN 5.3)
        await backend.stop(hard=True)
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


def _cadence(
    w: str,
    final: bool,
    interval: float,
    jitter: float,
    rng: random.Random,
    pauses: tuple[int, int, int],
) -> tuple[list[int], int]:
    """Letter intervals and the pause after one word (BUILD_PLAN 5.12, simplified)."""
    gap_ms, comma_ms, sentence_ms = pauses
    cms = [int(interval * (1 + jitter * (rng.random() - 0.5))) for _ in w]
    if final or w[-1] in ".?!":
        return cms, sentence_ms
    return cms, comma_ms if w[-1] in ",;:" else gap_ms


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

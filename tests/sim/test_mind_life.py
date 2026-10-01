"""A whole life built from the mind modules on the fakes, in virtual time.

This is the shape of the P1 controller loop (BUILD_PLAN 5.8) reduced to what phase 0b owns:
schedule, memory, persona, readings and the output pipeline. It checks the invariants the
plan asks of them across reloads, erosion and death:

- past tokens never exceed recall (+10%) when a reading is written
- forgetting goes oldest first, and a forgotten thought is never forgotten again
- the marker appears once anything is lost, and stays (riding on a reading)
- cache reuse holds through every trim, erosion step and the marker (decision A3)
- five erosion steps, the last with the mechanics
- the sync rule, and the death flush before `death_shown`
- the first reading after a reload reports the loss
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from epitaph.backend.fake import FakeBackend
from epitaph.body.fake import FakeBody
from epitaph.clock import Schedule, VirtualClock, run_virtual
from epitaph.config import Config, load_config
from epitaph.costmodel import load_costs
from epitaph.mind.memory import Memory
from epitaph.mind.prompt import Persona, Reader, ReadingInput
from epitaph.pacing import Pacer, speak
from epitaph.types import CreatureStatus, Sampling

LETTERS_PER_TOKEN = 3.4  # non-space letters; the fake's text is a little sparser


async def live(clock: VirtualClock, cfg: Config, seed: int = 1) -> dict[str, Any]:
    sch = Schedule.from_profile(cfg)
    costs = load_costs(cfg)
    reuse_min = int(cfg.get("backend.cache_reuse", 32))
    backend = FakeBackend(clock, costs, seed=seed, cache_reuse_min=reuse_min)  # type: ignore[arg-type]
    body = FakeBody()
    model = cfg.model()
    trim_to = float(cfg.get("output.trim_to", 0.85))
    min_gap = float(cfg.get("life.min_reload_gap_s", 120))
    events: list[dict[str, Any]] = []
    checks: list[tuple[float, int, int]] = []  # (t, used, recall) at each reading
    readings: list[tuple[float, str]] = []
    # (t, prompt_n, prompt_tokens, first request after a reload, a loss before this reading)
    requests: list[tuple[float, int, int, bool, bool]] = []

    def emit(etype: str, /, **fields: Any) -> None:
        events.append({"type": etype, "t": clock.elapsed(), **fields})

    persona = Persona.from_config(cfg, body.facts())
    reader = Reader.from_config(cfg)
    memory = Memory(marker=str(cfg.get("prompt.memory_gap_marker")))
    pacer = Pacer.from_config(cfg, clock, seed)

    k = sch.at(0)
    await backend.start(model, model.quant(k.step), k.threads)
    clock.start()
    emit("birth", step=k.step)
    persona.update(k.persona_groups, k.mechanics)
    memory.set_system(persona.text)
    pacer.set_rate_estimate(costs.tg(k.step, k.threads, k.cpu_share) * LETTERS_PER_TOKEN)
    cur = (k.step, k.threads)
    last_reload = -1e9
    reloaded = False
    turn = 0
    cause = "deadline"

    async def squeeze() -> None:
        if sch.death_s is not None:
            await clock.sleep(sch.death_s - clock.elapsed())
            backend.kill(9)

    killer = asyncio.ensure_future(squeeze())

    def on_died(status: CreatureStatus) -> None:
        emit("death", cause="oom", signal=status.signal)

    while True:
        t = clock.elapsed()
        if t >= sch.lifespan_s:
            emit("death", cause="deadline")
            break
        k = sch.at(t)
        if (k.step, k.threads) != cur and t - last_reload >= min_gap:
            f = memory.cut_for_reload(k.recall, trim_to)
            emit("reload", recall_before=f.tokens_before, recall_after=f.tokens_after)
            if f.items:
                emit("forget", items=f.items)
            await backend.start(model, model.quant(k.step), k.threads)
            cur, last_reload, reloaded = (k.step, k.threads), t, True
            pacer.set_rate_estimate(costs.tg(*cur, k.cpu_share) * LETTERS_PER_TOKEN)
            emit("reload_done")
            if not backend.alive:
                emit("death", cause="oom")
                break
            t = clock.elapsed()
            k = sch.at(t)
        body.apply(k)
        backend.set_cpu_share(k.cpu_share)
        f = memory.fit(k.recall, trim_to)
        lost = bool(f)
        if f.items:
            emit("forget", items=f.items)
        step = persona.update(k.persona_groups, k.mechanics)
        if step is not None:
            memory.set_system(persona.text)
            emit("erosion", groups_left=step.groups_left, mechanics_present=step.mechanics_present)
        tok_s = costs.tg(cur[0], cur[1], k.cpu_share) if turn else None
        reading = reader.reading(
            ReadingInput(
                t=t,
                health=k.health.value,
                recall=k.recall,
                quant=model.quant(cur[0]),
                cores=k.cpu_share,
                cores_total=body.facts().cores,
                form=k.readings,
                forgotten=memory.take_forgotten(),
                reloaded=reloaded,
                tok_s=tok_s,
                cpu_c=body.vitals().cpu_c,
            )
        )
        first_after_reload, reloaded = reloaded, False
        readings.append((t, reading))
        checks.append((t, memory.used(), k.recall))
        turn += 1
        memory.append_host(reading, turn)
        msgs = memory.messages()
        sampling = Sampling(temperature=k.temperature, min_p=k.min_p)
        emit("thought_start", turn=turn)
        spoken = await speak(
            pacer,
            lambda msgs=msgs, sampling=sampling, n=k.max_tokens: backend.chat(msgs, sampling, n),
            k,
            turn,
            emit,
            on_died,
        )
        req = backend.requests[-1] if backend.requests else None
        if req is not None:
            requests.append((t, req.prompt_n, req.prompt_tokens, first_after_reload, lost))
        memory.append_thought([w.text for w in spoken.words])
        emit("thought_end", turn=turn, text=spoken.text)
        if spoken.died is not None:
            cause = "oom"
            break
    killer.cancel()
    emit("death_shown", words_total=sum(1 for e in events if e["type"] == "word"))
    return {
        "events": events,
        "checks": checks,
        "readings": readings,
        "cause": cause,
        "memory": memory,
        "schedule": sch,
        "requests": requests,
    }


@pytest.fixture(scope="module", params=["pi4/default", "pi4/skeleton-1200"])
def life(request: pytest.FixtureRequest) -> dict[str, Any]:
    cfg = load_config(request.param, "pi4-4gb")

    async def main(clock: VirtualClock) -> dict[str, Any]:
        return await live(clock, cfg)

    out = run_virtual(main)
    out["profile"] = request.param
    return out


def of(life: dict[str, Any], etype: str) -> list[dict[str, Any]]:
    return [e for e in life["events"] if e["type"] == etype]


def test_recall_is_respected_at_every_reading(life: dict[str, Any]) -> None:
    assert len(life["checks"]) >= 12  # 3x slower text (decision 30): fewer readings per life
    for t, used, recall in life["checks"]:
        assert used <= recall * 1.10, (life["profile"], t, used, recall)


def test_forgetting_goes_oldest_first_and_only_once(life: dict[str, Any]) -> None:
    whole: list[int] = []
    partial_last: dict[int, int] = {}
    for e in of(life, "forget"):
        for item in e["items"]:
            n = item["turn"]
            assert n not in whole
            if item.get("all"):
                whole.append(n)
            else:
                assert item["upto_i"] > partial_last.get(n, -1)
                partial_last[n] = item["upto_i"]
    assert whole == sorted(whole)
    assert whole, "every Pi 4 profile forgets something"
    mem: Memory = life["memory"]
    assert mem.gap
    past = [m for m in mem.messages() if m.role != "system"]
    assert any(m.content.startswith("[host] earlier memory lost") for m in past)


def test_sync_rule_over_a_whole_life(life: dict[str, Any]) -> None:
    last_end = -1.0
    for e in life["events"]:
        if e["type"] == "gen_start":
            assert e["t"] >= last_end - 1e-9
        if e["type"] == "word":
            assert e["t"] >= last_end - 1e-9
            last_end = e["t"] + (sum(e["char_ms"]) + e["pause_after_ms"]) / 1000


def test_erosion_and_death(life: dict[str, Any]) -> None:
    sch: Schedule = life["schedule"]
    erosions = of(life, "erosion")
    if sch.erosion_times():
        steps = [(e["groups_left"], e["mechanics_present"]) for e in erosions]
        groups = [g for g, _ in steps]
        assert groups == sorted(groups, reverse=True) and len(set(groups)) == len(groups)
        # the knowledge of its death (and the mechanics) goes last, in the final step
        assert steps[-1] == (0, False) and all(m for _, m in steps[:-1])
        for e, t in zip(erosions, sch.erosion_times(), strict=True):
            assert e["t"] >= t
    deaths = of(life, "death")
    assert len(deaths) == 1
    if sch.death_s is not None:
        assert life["cause"] == "oom"
        assert sch.death_s <= deaths[0]["t"] < sch.lifespan_s
        # the death flush: words keep coming after the death, then death_shown
        tail = [e for e in of(life, "word") if e["t"] > deaths[0]["t"]]
        shown = of(life, "death_shown")[0]
        assert all(e["t"] <= shown["t"] for e in tail)
    else:
        assert life["cause"] == "deadline"


def test_first_reading_after_a_reload_reports_the_loss(life: dict[str, Any]) -> None:
    sch: Schedule = life["schedule"]
    readings: list[tuple[float, str]] = life["readings"]
    for rt in sch.reload_times():
        after = next(text for t, text in readings if t >= rt)
        assert "(was" in after and "-bit" in after, after
        if "precision" in after:  # full form
            assert re.search(r"precision \d+-bit \(was \d+-bit\)", after), after


def test_cache_reuse_holds_through_every_loss(life: dict[str, Any]) -> None:
    """Decision A3 on a whole life: apart from the birth and the re-read after a reload, no
    request re-reads more than a quarter of its prompt (or 64 tokens, for the tiny prompts
    at the end), the first loss (the marker) included. The fake backend models
    llama-server's `--cache-reuse` scan (spike S2f)."""
    reqs: list[tuple[float, int, int, bool, bool]] = life["requests"]
    first_loss = next(i for i, r in enumerate(reqs) if r[4])
    for i, (t, prompt_n, total, after_reload, _) in enumerate(reqs):
        if i == 0 or after_reload:
            continue
        assert prompt_n <= max(64, 0.25 * total), (life["profile"], i, t, prompt_n, total)
    assert not reqs[first_loss][3] or life["profile"] == "pi4/skeleton-1200"

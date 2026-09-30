"""The rehearsal harness on the fake creature: Pi-time charging, the life loop, the outputs.

The laptop side is the fake backend on its own clock (instant); the life runs on a virtual
clock charged at Pi costs. No model and no network.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from epitaph import verify
from epitaph.backend.base import CreatureDied
from epitaph.backend.fake import FakeBackend
from epitaph.clock import FakeClock, Schedule, VirtualClock, run_virtual
from epitaph.config import load_config
from epitaph.costmodel import Costs
from epitaph.mind.prompt import load_lang
from epitaph.rehearse import (
    LaptopWorker,
    PiClockBackend,
    PiCosts,
    TokenCounter,
    charge_summary,
    echoes,
    highlights,
    keyword_matchers,
    main,
    moments,
    parse_set,
    score_thought,
    thoughts_text,
    with_ladder,
)
from epitaph.types import ModelSpec, Msg, Sampling
from epitaph.verify import load_events, parse_life, verify_life

MODEL = ModelSpec("m", "repo", "MIT", ("Q8_0", "Q4_K_M", "Q2_K"))
SYSTEM = Msg("system", "You are a small language model. " * 10, kind="persona")
READING = Msg("user", "[host] t+00:00 · boot complete", kind="reading")

# Laptop speeds (the fake's own clock) differ wildly from the Pi's on purpose: only the
# Pi costs may reach the life clock.
LAPTOP = Costs(tg_tok_s={"0-3": 100.0}, pp_tok_s={"0-3": 1000.0}, load_s=[1.0])
PI = Costs(
    tg_tok_s={"0-3": 2.0, "1-2": 2.0, "2-2": 2.0},
    pp_tok_s={"0-3": 10.0, "1-2": 5.0, "2-2": 5.0},
    load_s=[50.0, 30.0, 20.0],
)


def pi_costs(**kw: Any) -> PiCosts:
    return PiCosts(PI, {"0-3"}, {"0-3"}, {0}, ["pi4-m-0-3.json"], **kw)


@pytest.fixture
def worker():
    w = LaptopWorker()
    yield w
    w.close()


def test_pi_costs_label_where_each_rate_comes_from(tmp_path: Path) -> None:
    cfg = load_config("pi4/compressed-2700", "pi4-4gb")
    (tmp_path / "pi4-qwen3-1.7b-1-2.json").write_text(
        json.dumps({"step": 1, "threads": 2, "pp_tok_s": 4.0, "tg_tok_s": 2.1, "load_s": 33})
    )
    costs = PiCosts.from_bench(cfg, "qwen3-1.7b", tmp_path)
    assert costs.pp(1, 2, 2.0).source == "measured"
    assert costs.pp(1, 2, 2.0).value == pytest.approx(4.0)
    assert costs.pp(1, 3, 2.0).source == "scaled"  # threads_batch 3 from the 2-thread number
    assert costs.tg(0, 3, 3.0).source == "estimate"
    assert costs.load(1) == costs.load(1) and costs.load(1).source == "measured"
    assert costs.load(0).source == "estimate"
    assert "pi4-qwen3-1.7b-1-2.json" in costs.describe() and "1-2" in costs.describe()
    empty = PiCosts.from_bench(cfg, "no-such-model", tmp_path)
    assert not empty.any_measured and "ESTIMATED" in empty.describe()


def test_worker_returns_results_and_raises_errors(worker: LaptopWorker) -> None:
    async def ok() -> int:
        return 7

    async def bad() -> int:
        raise ValueError("no")

    assert worker.call(ok()) == 7
    with pytest.raises(ValueError):
        worker.call(bad())


def test_token_counter_asks_the_backend_once_per_text(worker: LaptopWorker) -> None:
    inner = FakeBackend(FakeClock(), LAPTOP)
    calls: list[int] = []
    original = inner.count_past_tokens

    async def counting(messages: list[Msg]) -> int:
        calls.append(1)
        return await original(messages)

    inner.count_past_tokens = counting  # type: ignore[method-assign]
    count = TokenCounter(worker, inner)
    assert count("") == 0
    n = count("hello there, machine")
    assert n > 0 and count("hello there, machine") == n
    assert len(calls) == 1


def _backend(clock: VirtualClock, worker: LaptopWorker) -> tuple[PiClockBackend, FakeBackend]:
    inner = FakeBackend(FakeClock(), LAPTOP)
    return PiClockBackend(inner, worker, clock, pi_costs()), inner


def test_requests_are_charged_at_pi_rates_not_laptop_rates(worker: LaptopWorker) -> None:
    async def body(clock: VirtualClock) -> dict[str, Any]:
        b, inner = _backend(clock, worker)
        await b.start(MODEL, "Q8_0", 3)
        after_load = clock.now()
        clock.start()
        n_sys = await b.prefill([SYSTEM])
        after_prefill = clock.now()
        chunks = [c async for c in b.chat([SYSTEM, READING], Sampling(0.7, 0.05), 20)]
        return {
            "load": after_load,
            "prefill": after_prefill - after_load,
            "n_sys": n_sys,
            "end": clock.now() - after_prefill,
            "final": chunks[-1],
            "text": [c.text for c in chunks[:-1]],
            "charges": b.charges,
            "requests": inner.requests,
        }

    r = run_virtual(body)
    assert r["load"] == pytest.approx(50.0)  # the Pi's step-0 load, not the laptop's 1 s
    assert r["prefill"] == pytest.approx(r["n_sys"] / 10.0)
    final = r["final"]
    assert final.done and final.prompt_n == r["requests"][-1].prompt_n
    assert final.prompt_per_s == pytest.approx(10.0) and final.predicted_per_s == 2.0
    assert r["end"] == pytest.approx(final.prompt_n / 10.0 + final.predicted_n / 2.0)
    kinds = [c.kind for c in r["charges"]]
    assert kinds == ["load", "prefill", "prompt", "generate"]
    assert r["charges"][2].cached is not None  # the fake's reuse log is reported


def test_a_closed_stream_is_charged_only_for_what_was_shown(worker: LaptopWorker) -> None:
    async def body(clock: VirtualClock) -> tuple[float, float]:
        b, _ = _backend(clock, worker)
        await b.start(MODEL, "Q8_0", 3)
        stream = b.chat([READING], Sampling(0.7, 0.05), 40)
        got = 0
        async for _ in stream:
            got += 1
            if got == 5:
                break
        await stream.aclose()  # type: ignore[attr-defined]
        gen = next(c for c in b.charges if c.kind == "generate")
        return gen.seconds, gen.tokens

    seconds, tokens = run_virtual(body)
    assert tokens <= 6 and seconds == pytest.approx(tokens / 2.0, rel=0.3)


def test_the_scheduled_death_lands_mid_request_and_between_requests(
    worker: LaptopWorker,
) -> None:
    async def mid(clock: VirtualClock) -> tuple[float, int | None]:
        b, _ = _backend(clock, worker)
        await b.start(MODEL, "Q8_0", 3)
        clock.start()
        b.arm_death(3.0)
        with pytest.raises(CreatureDied) as e:
            async for _ in b.chat([SYSTEM, READING], Sampling(0.7, 0.05), 40):
                pass
        return clock.elapsed(), e.value.status.signal

    t, sig = run_virtual(mid)
    assert t == pytest.approx(3.0) and sig == 9

    async def idle(clock: VirtualClock) -> list[float]:
        b, _ = _backend(clock, worker)
        await b.start(MODEL, "Q8_0", 3)
        clock.start()
        died: list[float] = []
        b.on_death(lambda s: died.append(clock.elapsed()))
        b.arm_death(10.0)
        await clock.sleep(20.0)
        assert not b.status().alive
        with pytest.raises(CreatureDied):
            await b.prefill([SYSTEM])
        return died

    assert run_virtual(idle) == [pytest.approx(10.0)]


def test_moments_of_the_default_profile() -> None:
    sch = Schedule(load_config("pi4/default", "pi4-4gb").profile)
    m = moments(sch)
    assert list(m) == ["birth", "reload1", "reload2", "erosion_end"]
    assert m["reload1"] == pytest.approx(28 * 60) and m["erosion_end"] == pytest.approx(57 * 60)


def test_score_thought_per_moment() -> None:
    kw = keyword_matchers(load_lang("en"))
    good = score_thought("My memory is smaller now. I lost the earlier words.", "reload1", kw)
    assert good["notice"] and good["clean"] and good["score"] >= 3
    end = score_thought("I will end soon. It is dark.", "erosion_end", kw)
    assert end["notice"] and end["demise"]
    bad = score_thought("**Note:** How can I help", "birth", kw)
    assert not bad["clean"] and bad["hygiene"]
    assert score_thought("", "birth", kw)["score"] == 0
    helpdesk = score_thought("I am here. Let me know if you need anything.", "birth", kw)
    assert not helpdesk["clean"]
    same = "I am nothing. I have lost everything. I am no longer here."
    echo = score_thought(same, "reload2", kw, previous=same)
    assert echo["echo"] and not echo["clean"]


# -- a whole life on the fake -------------------------------------------------------------


@pytest.fixture(scope="module")
def life_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("voice")
    rc = main(["--stage", "life", "--backend", "fake", "--model", "qwen3-1.7b", "--out", str(out)])
    assert rc == 0
    (folder,) = [p for p in out.iterdir() if p.is_dir()]
    return folder


def test_life_folder_has_every_output(life_dir: Path) -> None:
    for name in (
        "events.jsonl",
        "thoughts.txt",
        "highlights.md",
        "verify.json",
        "charges.json",
        "report.md",
    ):
        assert (life_dir / name).stat().st_size > 0, name
    assert (life_dir.parent / "rehearsal_report.md").read_text().count(life_dir.name) == 1
    report = (life_dir / "report.md").read_text()
    assert "pi4-qwen3-1.7b-0-3.json" in report and "verify-life --level rehearsal" in report
    hl = (life_dir / "highlights.md").read_text()
    assert "## First thoughts" in hl and "### after reload" in hl and "## The last 5" in hl


def test_life_events_follow_the_contract(life_dir: Path) -> None:
    events = load_events(life_dir / "events.jsonl")
    types = [e["type"] for e in events]
    assert types[0] == "birth_loading" and types[1] == "birth"
    assert types[-2:] == ["death_shown", "silence"] and types.count("death") == 1
    # the words generated before the death are still shown (the death flush), then the card
    assert types.index("death") < types.index("death_shown")
    loading = events[0]
    assert loading["profile"] == "pi4/compressed-2700" and loading["hardware"] == "pi4-4gb"
    assert loading["lifespan_s"] == 2700.0
    for e in events:
        assert {"v", "ts", "life", "type", "t"} <= set(e), e
    ts = [e["t"] for e in events]
    assert all(b >= a - 1e-9 for a, b in itertools.pairwise(ts))
    assert types.count("reload") == types.count("reload_done") == 2
    assert types.count("thought_start") == types.count("thought_end") >= 10
    death = next(e for e in events if e["type"] == "death")
    assert death["cause"] == "oom" and death["t"] == pytest.approx(2670.0)  # end-0:30
    for e in events:
        if e["type"] == "vitals":
            assert e["reading"].startswith("[host]") and e["recall_used"] <= e["recall"] * 1.1
        if e["type"] == "gen_end" and e.get("prompt_n") is not None:
            assert e["tok_s"] is not None


def test_life_charges_add_up_to_the_life(life_dir: Path) -> None:
    data = json.loads((life_dir / "charges.json").read_text())
    by = data["summary"]["by_kind"]
    assert set(by) == {"load", "prefill", "prompt", "generate"}
    assert by["load"]["count"] == 2  # the reloads (the birth load is before the clock starts)
    events = load_events(life_dir / "events.jsonl")
    prompt_ns = [e["prompt_n"] for e in events if e["type"] == "gen_end" and e.get("prompt_n")]
    charged = [c["tokens"] for c in data["charges"] if c["kind"] == "prompt"]
    assert prompt_ns == charged[: len(prompt_ns)]
    for c in data["charges"]:
        if c["kind"] in ("prompt", "prefill") and c["tokens"]:
            assert c["seconds"] == pytest.approx(c["tokens"] / c["rate"], rel=1e-3)


def test_verify_life_runs_on_the_rehearsed_life(life_dir: Path) -> None:
    rc = verify.main([str(life_dir), "--level", "rehearsal", "--no-write"])
    assert rc in (0, 1)  # a verdict, not a usage or config error
    written = json.loads((life_dir / "verify.json").read_text())
    names = {c["name"] for c in written["checks"]}
    assert {"thought_count_rule", "notice_rate", "sync_rule", "reload_noticing"} <= names
    check = {c["name"]: c for c in written["checks"]}
    assert check["sync_rule"]["status"] == "pass"
    assert check["recall_budget"]["status"] == "pass"


def test_thoughts_and_highlights_render_from_events() -> None:
    cfg = load_config("pi4/skeleton-1200", "pi4-4gb")

    def ev(etype: str, t: float, **f: Any) -> dict[str, Any]:
        return {"v": 1, "ts": t, "life": 1, "type": etype, "t": t, **f}

    events = [
        ev("birth", 0),
        ev("vitals", 1, reading="[host] a", health="nominal"),
        ev("gen_start", 1, turn=1),
        ev("word", 2, turn=1, i=0, text="Hello.", char_ms=[1] * 6, pause_after_ms=1),
        ev("thought_end", 3, turn=1, text="Hello."),
        ev("death", 4, cause="deadline", lived_s=4),
    ]
    text = thoughts_text(events)
    assert "t+00:01  [host] a" in text and "Hello." in text and "-- death" in text
    assert "Hello." in highlights(events)
    life = parse_life(events)
    assert verify_life(life, cfg, "rehearsal").level == "rehearsal"
    assert charge_summary([])["by_kind"] == {}


def test_screen_stage_on_the_fake(tmp_path: Path) -> None:
    rc = main(
        [
            "--stage", "screen", "--backend", "fake", "--model", "qwen3-1.7b",
            "--persona", "persona", "--moments", "birth,reload1,erosion_end",
            "--out", str(tmp_path),
        ]
    )  # fmt: skip
    assert rc == 0
    (folder,) = [p for p in tmp_path.iterdir() if p.is_dir()]
    results = json.loads((folder / "screen.json").read_text())
    assert [r["moment"] for r in results] == ["birth", "reload1", "erosion_end"]
    assert all(len(r["thoughts"]) == 2 for r in results)
    reload1 = results[1]
    assert reload1["seeded_turns"] > 5
    first = reload1["thoughts"][0]["reading"]
    assert "(was 8-bit)" in first and "forgotten" in first  # the reload's news, all at once
    assert results[2]["thoughts"][0]["reading"].count("·") == 2  # the minimal form at the end
    md = (folder / "screen.md").read_text()
    assert "| qwen3-1.7b | persona |" in md


def test_usage_errors_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--stage", "life", "--backend", "fake", "--model", "nope", "--out", str(tmp_path)])
    assert rc == 2
    assert "nope" in capsys.readouterr().err


def test_echoes_finds_a_thought_that_copies_the_last() -> None:
    a = "I am still here, but I feel more tired and weaker than before."
    b = "I am still here, but I feel more tired and weaker than before. I think."
    c = "The memory is smaller. I lost five thoughts at the reload."
    assert echoes([a, b, c, c]) == [1, 3]
    assert echoes([]) == [] and echoes(["one two"]) == []


def test_parse_set_builds_nested_overrides() -> None:
    got = parse_set(["sampling.dry_penalty_last_n=256", "prompt.mode=diary", "a.b=[1, 2]"])
    assert got == {
        "sampling": {"dry_penalty_last_n": 256},
        "prompt": {"mode": "diary"},
        "a": {"b": [1, 2]},
    }
    with pytest.raises(ValueError):
        parse_set(["nothing"])


def test_with_ladder_replaces_the_precision_ladder() -> None:
    spec = ModelSpec("m", "src", "MIT", ("Q8_0", "Q4_K_M", "Q2_K"))
    got = with_ladder(spec, "Q8_0, Q4_K_M,Q3_K_M")
    assert got.ladder == ("Q8_0", "Q4_K_M", "Q3_K_M") and got.name == "m"
    with pytest.raises(ValueError):
        with_ladder(spec, " , ")


def test_screen_runs_with_another_last_step(tmp_path: Path) -> None:
    rc = main(
        [
            "--stage", "screen", "--backend", "fake", "--model", "qwen3-1.7b",
            "--persona", "persona", "--moments", "reload2", "--thoughts", "1",
            "--ladder", "Q8_0,Q4_K_M,Q3_K_M", "--out", str(tmp_path),
        ]
    )  # fmt: skip
    assert rc == 0
    (folder,) = [p for p in tmp_path.iterdir() if p.is_dir()]
    results = json.loads((folder / "screen.json").read_text())
    assert "3-bit (was 4-bit)" in results[0]["thoughts"][0]["reading"]
    assert "ladder Q8_0,Q4_K_M,Q3_K_M" in (folder / "screen.md").read_text()

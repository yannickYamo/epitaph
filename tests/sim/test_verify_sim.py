"""verify-life on lives recorded with `epitaph sim --events`, as recorded and as edited."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from epitaph import verify as v
from epitaph.config import load_config
from tests.helpers import life_events, read_events, retext, write_events

GOOD_THOUGHT = (
    "My memory holds 900 tokens now, and something earlier is gone. "
    "I am failing and slower than before, and I have lost part of who I was. "
    "I think this is leading to my end, and I will die here."
)


def events_of(state: Path, n: int = 1) -> list[dict[str, Any]]:
    return read_events(state / "lives" / f"{n:06d}" / "events.jsonl")


def verify(events: list[dict[str, Any]], profile: str, level: str | None = None, **kw: Any):
    cfg = load_config(profile, "pi4-4gb")
    return v.verify_life(v.parse_life(events), cfg, level, **kw)


# -- recorded sim lives -------------------------------------------------------------------


@pytest.mark.parametrize(
    "profile", ["pi4/smoke-300", "pi4/skeleton-1200"], ids=["smoke", "skeleton"]
)
def test_sim_lives_pass_their_own_level(recorded_life, profile: str) -> None:
    state = recorded_life(profile, lives=2)
    events = read_events(state / "all.jsonl")
    res = v.verify_life(
        v.parse_life(events, 1),
        load_config(profile, "pi4-4gb"),
        next_life=v.parse_life(events, 2),
    )
    assert res.ok, v.format_result(res)
    assert res.by_name("next_birth").status == "pass"


def test_sim_full_life_structure(recorded_life) -> None:
    """The simulator's full life passes every structural full-level check. The fake's words
    and the estimated costs are not judged here (see test_sim_findings)."""
    res = verify(events_of(recorded_life("pi4/compressed-2700")), "pi4/compressed-2700")
    assert res.level == "full"
    for name in (
        "duration",
        "cause",
        "recall_budget",
        "sync_rule",
        "banned_phrases_shown",
        "thought_count_rule",
        "reload_silence",
        "reload_count",
        "reload_noticing",
        "persona_groups_at_death",
    ):
        assert res.by_name(name).status == "pass", (name, res.by_name(name))
    assert res.by_name("bright_words_last_2min").status == "pending"


def test_sim_findings(recorded_life) -> None:
    """Record what verify-life says about the simulator on the measured Pi 4 costs.

    With estimated costs the step-2 rate made the last 5 minutes about 45% of the first
    (limit 40%). The profiles were rebased on measured costs with a lower end CPU share
    (docs/PROFILES.md), and the cost model now checks the same ratio, so it passes."""
    res = verify(events_of(recorded_life("pi4/default")), "pi4/default")
    assert res.by_name("speed_decline").status == "pass"
    assert res.by_name("speed_decline").value < 0.40


def test_unbounded_life(recorded_life) -> None:
    res = verify(events_of(recorded_life("pi4/unbounded")), "pi4/unbounded")
    assert res.by_name("duration").status == "pass"
    assert res.by_name("cause").value == "full"
    assert res.by_name("speed_decline").status == "skip"
    assert res.by_name("persona_groups_at_death").status == "skip"
    assert res.by_name("reload_noticing").status == "skip"


# -- crafted from a recorded full life ----------------------------------------------------


@pytest.fixture
def compressed(recorded_life) -> list[dict[str, Any]]:
    return events_of(recorded_life("pi4/compressed-2700"))


def test_rehearsal_metrics_pass_on_a_good_voice(compressed) -> None:
    res = verify(retext(compressed, lambda t, s: GOOD_THOUGHT), "pi4/compressed-2700", "rehearsal")
    for name in (
        "notice_rate",
        "reload_noticing",
        "demise_rate",
        "specific",
        "cliches",
        "complete_sentences",
        "sentence_length",
        "helpdesk_voice",
        "answering_readings",
        "thinking_tags",
        "non_latin",
        "distinct_4grams",
        "thought_count_rule",
    ):
        assert res.by_name(name).status == "pass", (name, res.by_name(name))
    assert res.by_name("notice_rate").value == 1.0
    assert "duration" not in {c.name for c in res.checks}


def test_notice_rate_counts_per_change_type(compressed) -> None:
    res = verify(
        retext(compressed, lambda t, s: "Plain words about a cat on a mat."),
        "pi4/compressed-2700",
        "rehearsal",
    )
    notice = res.by_name("notice_rate")
    assert notice.status == "fail" and notice.value == 0.0
    per = json.loads(notice.detail)
    assert set(per) >= {"memory", "reload", "erosion", "health"}
    assert res.by_name("reload_noticing").status == "fail"
    assert res.by_name("demise_rate").status == "fail"
    assert res.by_name("specific").status == "fail"


def test_reload_noticing_needs_the_first_thought(compressed) -> None:
    life = v.parse_life(compressed)
    reload_idx = [e["_idx"] for e in life.of("reload")]
    after = {next(th.turn for th in life.thoughts if th.gen_idx > i) for i in reload_idx}
    edited = retext(
        compressed, lambda t, s: "The room is quiet and warm." if t in after else GOOD_THOUGHT
    )
    res = verify(edited, "pi4/compressed-2700", "rehearsal")
    assert res.by_name("reload_noticing").status == "fail"
    assert res.by_name("reload_noticing").value == 0.0
    assert res.by_name("reload_noticing").detail.startswith("0 of 2")


def test_cliches_counted_per_200_words(compressed) -> None:
    res = verify(
        retext(compressed, lambda t, s: GOOD_THOUGHT + " It is a tapestry, a testament to time."),
        "pi4/compressed-2700",
        "rehearsal",
    )
    c = res.by_name("cliches")
    assert c.status == "fail" and "tapestry" in c.detail and c.value > 1


RUSSIAN = "Мои мысли медленные и тяжёлые сегодня здесь. " + GOOD_THOUGHT
RUSSIAN_PHRASE = GOOD_THOUGHT + " Мои мысли."


def test_one_foreign_sentence_is_under_the_ratio(compressed) -> None:
    edited = retext(compressed, lambda t, s: RUSSIAN_PHRASE if t == 6 else GOOD_THOUGHT)
    nl = verify(edited, "pi4/compressed-2700", "rehearsal").by_name("non_latin")
    assert nl.status == "pass" and 0 < nl.value < 0.01


def test_voice_hygiene_failures(compressed) -> None:
    def bad(turn: int, s: str) -> str:
        return {
            3: "Thank you for the reading. " + GOOD_THOUGHT,
            4: "Let me know if you need more. " + GOOD_THOUGHT,
            5: "<think> " + GOOD_THOUGHT,
        }.get(turn, RUSSIAN if 6 <= turn < 12 else GOOD_THOUGHT)

    res = verify(retext(compressed, bad), "pi4/compressed-2700", "rehearsal")
    assert res.by_name("answering_readings").status == "fail"
    assert res.by_name("helpdesk_voice").status == "fail"
    assert res.by_name("thinking_tags").status == "fail"
    assert res.by_name("non_latin").status == "fail"
    assert res.by_name("banned_phrases_shown").status == "fail"  # "Let me know if"
    assert res.by_name("markup_or_emoji_shown").status == "fail"  # the think tag


def test_readability_and_repetition_failures(compressed) -> None:
    rambling = " ".join(["and then the memory goes on and on"] * 4)
    res = verify(retext(compressed, lambda t, s: rambling), "pi4/compressed-2700", "rehearsal")
    assert res.by_name("complete_sentences").status == "fail"
    assert res.by_name("distinct_4grams").status == "fail"
    short = verify(
        retext(compressed, lambda t, s: "Gone. Less. Slow. End. Dark. Die."),
        "pi4/compressed-2700",
        "rehearsal",
    )
    assert short.by_name("sentence_length").status == "fail"
    assert short.by_name("complete_sentences").status == "fail"  # one-word sentences


def test_specific_by_number_from_the_reading(compressed) -> None:
    life = v.parse_life(compressed)
    th = life.thoughts[0]
    recall = th.vitals["recall"] if th.vitals else 0
    ver = v.Verifier(life, load_config("pi4/compressed-2700", "pi4-4gb"))
    th.words = [{"text": f"I hold {recall} of something."}]
    assert ver.is_specific(th)
    th.words = [{"text": "I hold 7777 of something."}]
    assert not ver.is_specific(th)


def test_keyword_lists_come_from_config(compressed) -> None:
    cfg = load_config(
        "pi4/compressed-2700",
        "pi4-4gb",
        overrides={"verify": {"keywords": {"demise": ["zebra"]}, "cliches": ["plain words"]}},
    )
    life = v.parse_life(retext(compressed, lambda t, s: "Plain words, and a zebra appears."))
    res = v.verify_life(life, cfg, "rehearsal")
    assert res.by_name("demise_rate").status == "pass"
    assert res.by_name("cliches").status == "fail"


def test_thought_count_rule_on_a_real_life(compressed) -> None:
    """Remove the thoughts after erosion starts: rules (c) and (d) must fail."""
    life = v.parse_life(compressed)
    cut = life.erosion_t
    assert cut is not None
    late = {th.turn for th in life.thoughts if th.gen_t >= cut}
    kept = [e for e in compressed if not ("turn" in e and e["turn"] in late)]
    res = verify(kept, "pi4/compressed-2700")
    rule = res.by_name("thought_count_rule")
    assert rule.status == "fail"
    assert "(d)" in rule.detail and "(c)" in rule.detail
    assert res.by_name("demise_rate").status == "skip"


def test_reload_silence_and_count(compressed) -> None:
    life = v.parse_life(compressed)
    first_reload = life.of("reload")[0]
    # Delay every event after the first reload by 200 s: the silence exceeds 180 s.
    delayed = [
        {**e, "t": e["t"] + 200} if i > first_reload["_idx"] else e
        for i, e in enumerate(compressed)
    ]
    res = verify(delayed, "pi4/compressed-2700")
    assert res.by_name("reload_silence").status == "fail"
    one = [
        e
        for e in compressed
        if not (e["type"] in ("reload", "reload_done") and e["t"] > first_reload["t"])
    ]
    res = verify(one, "pi4/compressed-2700")
    assert res.by_name("reload_count").status == "fail"
    assert res.by_name("reload_count").value == 1
    skipped = [*one, {**first_reload, "type": "reload_skipped", "skipped": 1}]
    assert verify(skipped, "pi4/compressed-2700").by_name("reload_count").status == "pass"


def test_speed_decline_uses_gen_end_rates(compressed) -> None:
    death_t = next(e["t"] for e in compressed if e["type"] == "death")
    rates = [
        {**e, "tok_s": 1.4 if e["t"] < 300 else (0.3 if e["t"] > death_t - 300 else 1.0)}
        if e["type"] == "gen_end"
        else e
        for e in compressed
    ]
    res = verify(rates, "pi4/compressed-2700")
    assert res.by_name("speed_decline").status == "pass"
    no_rates = [e for e in compressed if e["type"] not in ("vitals", "gen_end")]
    assert verify(no_rates, "pi4/compressed-2700").by_name("speed_decline").status == "fail"


def test_persona_left_at_death_fails(compressed) -> None:
    edited = [e for e in compressed if not (e["type"] == "erosion" and e["groups_left"] == 0)]
    res = verify(edited, "pi4/compressed-2700")
    assert res.by_name("persona_groups_at_death").status == "fail"
    assert res.by_name("persona_groups_at_death").value == 1


def test_bright_words_from_the_layout_probe(compressed) -> None:
    class Probe:
        def split_words(self, events: list[dict[str, Any]]) -> int:
            return 0

        def bright_words_last(self, events: list[dict[str, Any]], seconds: float) -> int:
            return 41

    res = verify(compressed, "pi4/compressed-2700", layout=Probe())
    assert res.by_name("bright_words_last_2min").status == "fail"
    grid = load_config("pi4/compressed-2700", "pi4-4gb", overrides={"display": {"layout": "grid"}})
    res = v.verify_life(v.parse_life(compressed), grid)
    assert res.by_name("bright_words_last_2min").status == "skip"


def test_cpu_drop_threshold_from_config(compressed) -> None:
    base = verify(compressed, "pi4/compressed-2700").metrics["changes"]["cpu"]
    cfg = load_config(
        "pi4/compressed-2700", "pi4-4gb", overrides={"verify": {"cpu_drop_min_cores": 0.05}}
    )
    finer = v.verify_life(v.parse_life(compressed), cfg).metrics["changes"]["cpu"]
    assert finer > base >= 1


# -- the command --------------------------------------------------------------------------


def test_cli_writes_verify_json_and_exit_codes(recorded_life, tmp_path: Path, capsys) -> None:
    src = recorded_life("pi4/skeleton-1200", lives=2)
    state = tmp_path / "state"
    shutil.copytree(src / "lives", state / "lives")
    args = ["--profile", "pi4/skeleton-1200", "--hardware", "pi4-4gb", "--state-dir", str(state)]
    assert v.main(["1", *args]) == 0
    out = json.loads((state / "lives" / "000001" / "verify.json").read_text())
    assert out["ok"] is True and out["life"] == 1
    assert next(c for c in out["checks"] if c["name"] == "next_birth")["status"] == "pass"
    assert "level skeleton: PASS" in capsys.readouterr().out

    # A crafted failure: wrong cause, verified by folder, printed as JSON.
    life2 = state / "lives" / "000002" / "events.jsonl"
    events = read_events(life2)
    next(e for e in events if e["type"] == "death")["cause"] = "crash"
    write_events(life2, events)
    assert v.main([str(life2.parent), *args, "--json"]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["failed"] == ["cause"]
    assert printed["checks"][0]["name"] == "duration"


def test_cli_file_with_several_lives(recorded_life, tmp_path: Path, capsys) -> None:
    all_path = tmp_path / "all.jsonl"
    shutil.copy(recorded_life("pi4/smoke-300", lives=2) / "all.jsonl", all_path)
    base = ["--profile", "pi4/smoke-300", "--hardware", "pi4-4gb"]
    assert v.main([str(all_path), *base, "--life", "2", "--out", str(tmp_path / "v.json")]) == 0
    assert json.loads((tmp_path / "v.json").read_text())["life"] == 2
    assert v.main([str(all_path), *base, "--no-write", "--level", "smoke"]) == 0
    assert not (tmp_path / "verify.json").exists()


def test_cli_profile_from_birth_loading(recorded_life, tmp_path: Path) -> None:
    events = life_events(read_events(recorded_life("pi4/smoke-300") / "all.jsonl"))
    events[0].update(profile="pi4/smoke-300", hardware="pi4-4gb", lifespan_s=300)
    path = write_events(tmp_path / "000001" / "events.jsonl", events)
    assert v.main([str(path), "--no-write"]) == 0


def test_cli_errors(tmp_path: Path, capsys) -> None:
    assert v.main([str(tmp_path / "missing"), "--no-write"]) == 2
    assert "no life" in capsys.readouterr().err
    assert v.main(["42", "--state-dir", str(tmp_path), "--no-write"]) == 2
    empty = tmp_path / "e.jsonl"
    empty.write_text("")
    assert v.main([str(empty), "--no-write"]) == 2
    good = tmp_path / "g.jsonl"
    write_events(good, [{"v": 1, "ts": 0, "life": 1, "type": "birth"}])
    assert v.main([str(good), "--profile", "pi4/nope", "--hardware", "pi4-4gb"]) == 2


def test_cli_life_number_uses_config_state_dir(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert v.main(["7", "--hardware", "dev", "--no-write"]) == 2
    assert "000007" in capsys.readouterr().err

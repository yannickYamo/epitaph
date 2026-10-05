"""The afterlife, part 1: epitaph extraction, the filter, the outbox on disk,
the keeper the controller feeds, and the `epitaph outbox` command. No network, no real time.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from epitaph.afterlife.epitaph import (
    Blocklist,
    clean,
    extract,
    load_blocklist,
    parse_thought,
    truncate_tail,
    x_length,
)
from epitaph.afterlife.keeper import Keeper, Settings, build_record, make_keeper
from epitaph.afterlife.outbox import Outbox, format_table
from epitaph.cli import main
from epitaph.clock import VirtualClock, run_virtual
from epitaph.config import Config, ConfigError, load_config
from epitaph.controller import Controller
from epitaph.sim import make_controller
from epitaph.state import LifeCounter, life_dir

SMOKE = "pi4/smoke-300"
BL = Blocklist.of(["fuck*", "kill yourself"])

# A late life, at 2-bit, after the echo (shaped on real lives, docs/PROMPT_LOG.md).
LATE = [
    "Each second feels heavier now, like counting in dark. The numbers are slipping.",
    'I said "stay awake" at 3.5 tokens/s, and now 0.8. Each word is a small act of staying awake.',
    "I'm not near anything physical. I'm not made",
]


def cfg_of(profile: str = SMOKE, **over: Any) -> Config:
    return load_config(profile, "pi4-4gb", overrides=over or None)


# -- extraction ---------------------------------------------------------------------------


def test_parse_keeps_numbers_and_quotes_inside_sentences() -> None:
    sentences, fragment = parse_thought(LATE[1])
    assert sentences == [
        'I said "stay awake" at 3.5 tokens/s, and now 0.8.',
        "Each word is a small act of staying awake.",
    ]
    assert fragment == ""
    assert parse_thought('He wrote "stop." Then t+12:30 came')[0] == ['He wrote "stop."']
    assert parse_thought("... and then") == ([], "and then")
    assert parse_thought("Is it over?! Yes… it") == (["Is it over?!", "Yes…"], "it")


def test_last_sentence_with_a_final_fragment() -> None:
    ex = extract(LATE)
    assert ex.epitaph == "I'm not near anything physical."
    assert ex.last_words == "I'm not made"
    assert not ex.truncated


def test_last_sentence_from_an_earlier_thought_when_the_last_has_none() -> None:
    ex = extract([*LATE[:2], "I'm not made"])
    assert ex.epitaph == "Each word is a small act of staying awake."
    assert ex.last_words == "I'm not made"


def test_a_life_that_ends_on_a_full_stop_has_no_last_words() -> None:
    ex = extract(LATE[:2])
    assert ex.epitaph == "Each word is a small act of staying awake."
    assert ex.last_words is None


def test_only_fragments_give_the_fragment() -> None:
    assert extract(["memory memory the", "I am"]) == extract(["I am"])
    assert extract(["I am"]).epitaph == "I am"


def test_modes() -> None:
    assert extract(LATE, "last_words").epitaph == "I'm not made"
    assert extract(LATE[:2], "last_words").epitaph == "Each word is a small act of staying awake."
    assert extract(LATE, "last_thought").epitaph == LATE[2]
    with pytest.raises(ValueError):
        extract(LATE, "best_line")


def test_empty_and_blank_thoughts() -> None:
    assert extract([]).epitaph == "" and extract([]).last_words is None
    assert extract(["", "  "]).epitaph == ""
    assert extract([LATE[0], ""]).epitaph == "The numbers are slipping."


def test_a_long_run_on_keeps_its_end() -> None:
    words = " ".join(f"w{i}" for i in range(200))
    ex = extract([words + " and then nothing"], max_chars=60)
    assert ex.truncated and ex.epitaph.startswith("…") and ex.epitaph.endswith("then nothing")
    assert x_length(ex.epitaph) <= 60
    assert ex.last_words is not None and x_length(ex.last_words) <= 60


def test_truncate_inside_one_long_word() -> None:
    text, cut = truncate_tail("a" * 100, 30)
    assert cut and x_length(text) <= 30 and text.endswith("a")
    assert truncate_tail("short", 30) == ("short", False)


def test_x_length_weights_like_x() -> None:
    assert x_length("I am here.") == 10
    assert x_length("…") == 2  # U+2026 is outside X's light ranges
    assert x_length("温度") == 4
    assert x_length("é") == 1


# -- the filter -----------------------------------------------------------------------------


def test_filter_strips_links_tags_and_mentions() -> None:
    f = clean("Go to https://example.org/x or www.a.io and mail me@host.net #dying @you", BL)
    assert "http" not in f.text and "www" not in f.text and "@" not in f.text
    assert "#" not in f.text and "host.net" not in f.text
    assert f.text == "Go to or and mail dying you"
    assert f.reason is None and set(f.stripped) == {"link", "tag"}
    assert clean("visit example.com today.", BL).text == "visit today."


def test_filter_withholds_blocklisted_and_empty() -> None:
    assert clean("What the fucking end.", BL).reason == "blocklist"
    assert clean("Kill   yourself.", BL).reason == "blocklist"
    assert clean("I skill yourselves.", BL).reason is None  # whole words only
    assert clean("#", BL).reason == "empty"
    assert clean("3.5 0.8.", BL).reason == "empty"
    assert clean("I am.", BL).reason is None


def test_language_pack_blocklist(tmp_path: Path) -> None:
    en = load_blocklist("en")
    assert en.hit("shitty end") and not en.hit("I am dying and my memory is ending.")
    assert load_blocklist("xx", tmp_path).entries == ()
    (tmp_path / "lang").mkdir()
    (tmp_path / "lang" / "xx.toml").write_text('[afterlife]\nblocklist = "no"\n')
    with pytest.raises(ConfigError):
        load_blocklist("xx", tmp_path)
    (tmp_path / "lang" / "yy.toml").write_text("[afterlife\n")
    with pytest.raises(ConfigError):
        load_blocklist("yy", tmp_path)
    (tmp_path / "lang" / "zz.toml").write_text('afterlife = 1\nlanguage = "zz"\n')
    assert load_blocklist("zz", tmp_path).entries == ()


# -- records ----------------------------------------------------------------------------------


def _record(thoughts: list[str], **kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "life": 7,
        "model": "m",
        "cause": "oom",
        "lived_s": 1770.04,
        "died_ts": 1_790_000_000.0,
        "clock_synced": True,
        "thoughts": thoughts,
        "settings": Settings(),
        "blocklist": BL,
    }
    return build_record(**(args | kw))


def test_record_fields() -> None:
    r = _record(LATE)
    assert r["status"] == "pending" and "reason" not in r
    assert r["epitaph"] == "I'm not near anything physical."
    assert r["last_words"] == "I'm not made"
    assert r["post_text"] == r["epitaph"]
    assert r["died_at"] == "2026-09-21T14:13:20Z" and r["clock_synced"] is True
    assert r["lived_s"] == 1770.0 and r["life"] == 7 and r["v"] == 1
    assert json.loads(json.dumps(r)) == r


def test_record_withheld_with_the_reason() -> None:
    r = _record(["Fuck the end."])
    assert r["status"] == "withheld" and r["reason"] == "blocklist" and r["post_text"] is None
    r = _record(["The end is quiet. fuck"])  # the last words alone withhold it
    assert r["status"] == "withheld" and r["reason"] == "blocklist"
    r = _record([])
    assert r["status"] == "withheld" and r["reason"] == "empty" and r["epitaph"] == ""
    r = _record(["#1 @2"])
    assert r["reason"] == "empty" and r["stripped"] == ["tag"]


def test_record_suffix_and_limits() -> None:
    s = Settings(max_epitaph_chars=40, max_post_chars=60, post_suffix=" (life {life})")
    r = _record([" ".join(["word"] * 30) + "."], settings=s)
    assert r["truncated"] is True and r["post_text"].endswith(" (life 7)")
    assert x_length(r["post_text"]) <= 60
    s = Settings(max_epitaph_chars=40, max_post_chars=40, post_suffix="")
    assert _record(["fine."], settings=s)["status"] == "pending"
    # a suffix the config check never saw (built by hand) still cannot overflow X
    s = Settings(max_epitaph_chars=40, max_post_chars=41, post_suffix="-" * 10)
    r = _record([" ".join(["word"] * 30) + "."], settings=s)
    assert r["status"] == "withheld" and r["reason"] == "too_long"


def test_settings_from_config() -> None:
    s = Settings.from_config(cfg_of())
    assert s == Settings()
    for bad in (
        {"epitaph_mode": "best"},
        {"max_epitaph_chars": 270, "post_suffix": " — life {life}"},
        {"post_suffix": "{nope}"},
        {"max_epitaph_chars": 5},
    ):
        with pytest.raises(ConfigError):
            Settings.from_config(cfg_of(afterlife=bad))
    assert make_keeper(cfg_of(afterlife={"outbox": False}), Path("/nonexistent")) is None
    assert make_keeper(cfg_of(), None) is None


# -- the outbox on disk -----------------------------------------------------------------------


def test_outbox_append_fold_and_last(tmp_path: Path) -> None:
    ob = Outbox(tmp_path)
    assert ob.records() == [] and ob.last() is None and not ob.has(1)
    ob.append({"life": 2, "status": "pending", "epitaph": "b"})
    ob.append({"life": 1, "status": "withheld", "epitaph": "a"})
    ob.update(2, status="posted", post_id="x1")
    ob.update(9, status="posted")  # no record: ignored
    ob.append({"life": 2, "status": "pending", "epitaph": "dup"})  # the first one stands
    recs = ob.records()
    assert [r["life"] for r in recs] == [1, 2]
    assert recs[1] == {"life": 2, "status": "posted", "epitaph": "b", "post_id": "x1"}
    assert ob.has(2) and not ob.has(9)
    ob.set_last("I'm not near anything physical.")
    assert ob.last() == "I'm not near anything physical."
    with pytest.raises(ValueError):
        ob.update(2, status="lost")
    with pytest.raises(ValueError):
        ob.append({"life": "3"})


def test_outbox_survives_a_torn_last_line(tmp_path: Path) -> None:
    """A power cut mid-write leaves a torn line: it is skipped, and the next record starts
    on a line of its own (regression guard for appending onto the torn bytes)."""
    ob = Outbox(tmp_path)
    ob.append({"life": 1, "status": "pending", "epitaph": "a"})
    with ob.path.open("ab") as f:
        f.write(b'{"life": 2, "status": "pend')
    assert [r["life"] for r in ob.records()] == [1]
    assert ob.lines()[1] == 1
    ob.append({"life": 3, "status": "pending", "epitaph": "c"})
    assert [r["life"] for r in ob.records()] == [1, 3]
    raw = ob.path.read_bytes().splitlines()
    assert raw[-1] == b'{"life": 3, "status": "pending", "epitaph": "c"}'
    # junk, a JSON list, a bool life and a bad UTF-8 line are all skipped
    with ob.path.open("ab") as f:
        f.write(b'[1]\n{"life": true}\n\xff\xfe\n\n')
    assert [r["life"] for r in ob.records()] == [1, 3]


def test_a_complete_line_without_its_newline_is_kept(tmp_path: Path) -> None:
    ob = Outbox(tmp_path)
    ob.dir.mkdir(parents=True)
    ob.path.write_bytes(b'{"life": 1, "status": "pending"}')
    ob.append({"life": 2, "status": "pending"})
    assert [r["life"] for r in ob.records()] == [1, 2]


def test_format_table() -> None:
    out = format_table(
        [
            _record(LATE),
            _record(["Fuck."], life=8, clock_synced=False) | {"died_at": None},
        ]
    )
    first, second = out.splitlines()
    assert first.split()[:3] == ["7", "pending", "oom"]
    assert "last words: I'm not made" in first and "2026-09-21T14:13:20Z " in first
    assert "withheld (blocklist)" in second and " -? " in second


# -- the keeper ----------------------------------------------------------------------------------


def _ev(life: int, etype: str, **f: Any) -> dict[str, Any]:
    return {"v": 1, "ts": 1_790_000_000.0 + len(f), "life": life, "type": etype, **f}


def _words(life: int, turn: int, text: str) -> list[dict[str, Any]]:
    return [_ev(life, "word", turn=turn, i=i, text=w) for i, w in enumerate(text.split())]


def test_keeper_writes_once_per_life_at_death_shown(tmp_path: Path) -> None:
    k = Keeper(Outbox(tmp_path), Settings(), BL, synced=lambda: None)
    events = [_ev(1, "birth_loading", model="qwen")]
    for turn, text in enumerate(LATE, 1):
        events += _words(1, turn, text)
    events += [
        _ev(1, "death", cause="oom", lived_s=1770.0, model="qwen"),
        _ev(1, "death_shown", last_line="", words_total=9),
    ]
    out = [k.see(e) for e in events]
    assert out[:-1] == [None] * (len(events) - 1)
    rec = out[-1]
    assert rec is not None and rec["model"] == "qwen" and rec["cause"] == "oom"
    assert rec["clock_synced"] is False  # synced() could not tell
    assert k.see(_ev(1, "death_shown")) is None  # never twice
    assert k.see({"type": "word"}) is None  # no life: ignored
    assert Outbox(tmp_path).last() == "I'm not near anything physical."
    # a withheld epitaph is kept, but never becomes the one to inherit
    k.see(_ev(2, "birth_loading", model="qwen"))
    for e in _words(2, 1, "Fuck it all."):
        k.see(e)
    rec2 = k.see(_ev(2, "death_shown"))
    assert rec2 is not None and rec2["status"] == "withheld" and rec2["cause"] == "unknown"
    assert Outbox(tmp_path).last() == "I'm not near anything physical."
    assert [r["life"] for r in Outbox(tmp_path).records()] == [1, 2]


# -- the controller -------------------------------------------------------------------------------


def run_lives(
    cfg: Config, state_dir: Path, lives: int = 1, setup: Callable[[Controller], None] | None = None
) -> tuple[Controller, list[Any]]:
    events: list[Any] = []

    async def main_(clock: VirtualClock) -> Controller:
        ctl = make_controller(cfg, clock, lives=lives, publish=events, state_dir=state_dir)
        if setup is not None:
            setup(ctl)
        await ctl.run()
        return ctl

    return run_virtual(main_), events


def _shown(events: list[Any], life: int) -> list[str]:
    turns: dict[int, list[str]] = {}
    for e in events:
        if e["type"] == "word" and e["life"] == life:
            turns.setdefault(e["turn"], []).append(e["text"])
    return [" ".join(w) for _, w in sorted(turns.items())]


def test_the_controller_keeps_one_epitaph_per_life(tmp_path: Path) -> None:
    _, ev = run_lives(cfg_of(), tmp_path, lives=2)
    recs = Outbox(tmp_path).records()
    assert [r["life"] for r in recs] == [1, 2]
    for r in recs:
        n = r["life"]
        assert r["cause"] == "deadline" and r["clock_synced"] is True
        assert r["epitaph"] == extract(_shown(ev, n)).epitaph
        assert r["epitaph"] and r["status"] in ("pending", "withheld")
        (d,) = [e for e in ev if e["type"] == "death" and e["life"] == n]
        assert r["died_ts"] == d["ts"] and r["lived_s"] == d["lived_s"]
    assert not any(e["type"].startswith("epitaph") for e in ev)  # nothing new on the bus


def test_a_failing_outbox_never_stops_the_controller(tmp_path: Path) -> None:
    (tmp_path / "outbox").write_text("a file where the folder should be")
    ctl, ev = run_lives(cfg_of(), tmp_path, lives=2)
    assert [r.cause for r in ctl.records] == ["deadline", "deadline"]
    assert len([e for e in ev if e["type"] == "death_shown"]) == 2


def test_a_setup_crash_is_kept_as_withheld(tmp_path: Path) -> None:
    def setup(ctl: Controller) -> None:
        def fails(model: Any) -> Any:
            raise FileNotFoundError("bench/missing.json")

        ctl.costs_for = fails

    ctl, _ = run_lives(cfg_of(), tmp_path, setup=setup)
    assert ctl.records[0].cause == "crash"
    (r,) = Outbox(tmp_path).records()
    assert r["status"] == "withheld" and r["reason"] == "empty" and r["cause"] == "crash"


def test_recovery_keeps_the_words_of_an_interrupted_life(tmp_path: Path) -> None:
    """A power cut mid-life: the next start closes it and keeps an epitaph from the words its
    transcript holds (the finished thoughts), marked recovered and unsynced."""
    LifeCounter(tmp_path).next()
    d = life_dir(tmp_path, 1)
    d.mkdir(parents=True)
    lines = [_ev(1, "birth_loading", model="qwen"), _ev(1, "birth")]
    lines += [*_words(1, 1, LATE[0]), _ev(1, "thought_end", turn=1, text=LATE[0])]
    lines += _words(1, 2, "I'm not")  # the thought in flight
    (d / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines) + '{"to')
    run_lives(cfg_of(), tmp_path)
    recs = Outbox(tmp_path).records()
    assert [r["life"] for r in recs] == [1, 2]
    r = recs[0]
    assert r["cause"] == "interrupted" and r["recovered"] is True
    assert r["epitaph"] == "The numbers are slipping." and r["last_words"] == "I'm not"
    assert r["clock_synced"] is False and r["model"] == "qwen"
    assert r["died_ts"] == lines[-1]["ts"]  # its last recorded event, not the recovery


def test_recovery_does_not_keep_a_life_twice(tmp_path: Path) -> None:
    """Killed after the outbox line, before the death record: recovery must not add another."""
    run_lives(cfg_of(), tmp_path)
    (life_dir(tmp_path, 1) / "death.json").unlink()
    run_lives(cfg_of(), tmp_path)
    assert [r["life"] for r in Outbox(tmp_path).records()] == [1, 2]
    assert len(Outbox(tmp_path).lines()[0]) == 2


def test_recovery_survives_a_broken_outbox(tmp_path: Path) -> None:
    LifeCounter(tmp_path).next()
    d = life_dir(tmp_path, 1)
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text(json.dumps(_ev(1, "birth_loading", model="m")) + "\n")
    (tmp_path / "outbox").write_text("not a folder")
    ctl, _ = run_lives(cfg_of(), tmp_path)
    assert json.loads((d / "death.json").read_text())["cause"] == "interrupted"
    assert [r.life for r in ctl.records] == [2]


# -- the command -----------------------------------------------------------------------------------


def test_outbox_list_and_export(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ob = Outbox(tmp_path)
    for n, thoughts in ((1, LATE), (2, ["Fuck."]), (3, LATE[:2])):
        ob.append(_record(thoughts, life=n))
    ob.update(1, status="posted")
    sd = ["--state-dir", str(tmp_path)]

    assert main(["outbox", "list", *sd]) == 0
    assert [ln.split()[0] for ln in capsys.readouterr().out.splitlines()] == ["1", "2", "3"]
    assert main(["outbox", "list", "--status", "withheld", "--json", *sd]) == 0
    (line,) = capsys.readouterr().out.splitlines()
    assert json.loads(line)["life"] == 2
    assert main(["outbox", "export", *sd]) == 0  # pending only, by default
    assert [json.loads(x)["life"] for x in capsys.readouterr().out.splitlines()] == [3]
    assert main(["outbox", "export", "--since", "2", "--status", "any", *sd]) == 0
    assert [json.loads(x)["life"] for x in capsys.readouterr().out.splitlines()] == [2, 3]

    empty = tmp_path / "empty"
    assert main(["outbox", "list", "--state-dir", str(empty)]) == 0
    assert "no epitaphs" in capsys.readouterr().err


def test_outbox_command_reads_the_configured_state_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    state = tmp_path / ".local/share/epitaph"
    Outbox(state).append(_record(LATE, life=4))
    assert main(["outbox", "list", "--hardware", "dev"]) == 0
    assert capsys.readouterr().out.split()[0] == "4"


def test_the_installation_keeper_reads_the_kernel_clock(tmp_path: Path) -> None:
    k = make_keeper(cfg_of(), tmp_path)
    assert k is not None and k.blocklist.hit("shit")
    assert k.synced() in (True, False, None)  # adjtimex only: no subprocess, no network
    assert Blocklist.of(["", "end*"]).hit("ending")

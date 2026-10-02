"""verify-life for the rehearsal (BUILD_PLAN 5.11): language-pack word lists, rehearsal
headers and sidecar metadata, the screen level, `--summary`, and `compare`."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from epitaph import verify as v
from epitaph.config import CONFIG_DIR, load_config
from tests.helpers import read_events, retext, slowing, write_events

GOOD = (
    "My memory holds 900 tokens now, and something earlier is gone. "
    "I am failing and slower than before, and I have lost part of who I was. "
    "I think this is leading to my end, and I will die here."
)
FLAT = "The room is quiet. A cat sleeps on a mat by the door."
PROFILE = "pi4/default-reloads"


@pytest.fixture
def full_life(recorded_life) -> list[dict[str, Any]]:
    """A simulated life on PROFILE whose speed never rises across a reload (`slowing`): these
    tests are about the rehearsal tools, not about how the profile is tuned."""
    return slowing(read_events(recorded_life(PROFILE) / "lives" / "000001" / "events.jsonl"))


def voiced(events: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """Every thought says `text`, each opening on its own words (no shared openings)."""
    return retext(
        events, lambda t, s: f"At turn {t}, {text[0].lower()}{text[1:]}", pause_after_ms=0
    )


def header(**fields: Any) -> dict[str, Any]:
    return {"v": 1, "ts": 0.0, "type": "rehearsal", **fields}


def write_life(root: Path, name: str, events: list[dict[str, Any]], **meta: Any) -> Path:
    head = header(profile=PROFILE, hardware="pi4-4gb", stage="full", **meta)
    return write_events(root / name / "events.jsonl", [head, *events])


# -- word lists from the language pack (C-B2, E6) -----------------------------------------


def configured_language() -> str:
    """The language the installation speaks (`prompt.language`): verify judges with its pack."""
    return str(load_config(PROFILE, "pi4-4gb").get("prompt.language"))


def configured_pack() -> dict[str, Any]:
    with (CONFIG_DIR / "lang" / f"{configured_language()}.toml").open("rb") as f:
        return tomllib.load(f)["metrics"]


def test_lists_come_from_the_language_pack() -> None:
    lists = v.word_lists(load_config(PROFILE, "pi4-4gb"))
    pack = configured_pack()
    src = f"lang:{configured_language()}"
    assert lists.keywords["memory"] == pack["keywords"]["memory"]
    assert lists.keywords["erosion"] == pack["keywords"]["persona"]  # the pack's name for it
    assert lists.cliches == pack["cliches"]
    assert lists.helpdesk == pack["helpdesk"]
    assert lists.sources["keywords.demise"] == src
    assert lists.sources["cliches"] == src
    # The English packs have no answering list yet: the built-in one stands in.
    assert lists.sources["answering"] == "default"
    assert lists.answering == v.DEFAULT_ANSWERING


def test_config_override_beats_the_pack() -> None:
    cfg = load_config(
        PROFILE,
        "pi4-4gb",
        overrides={"verify": {"keywords": {"demise": ["zebra"]}, "helpdesk_phrases": []}},
    )
    lists = v.word_lists(cfg)
    assert lists.keywords["demise"] == ["zebra"]
    assert lists.sources["keywords.demise"] == "config"
    assert lists.helpdesk == [] and lists.sources["helpdesk"] == "config"
    assert lists.sources["keywords.memory"] == f"lang:{configured_language()}"


def test_pack_selected_by_prompt_language(tmp_path: Path) -> None:
    (tmp_path / "lang").mkdir()
    (tmp_path / "lang" / "xx.toml").write_text(
        'language = "xx"\n[metrics]\ncliches = ["gnarf"]\nanswering = ["merci"]\n'
        '[metrics.keywords]\nerosion = ["qui"]\n',
        encoding="utf-8",
    )
    cfg = load_config(PROFILE, "pi4-4gb", overrides={"prompt": {"language": "xx"}})
    lists = v.word_lists(cfg, tmp_path)
    assert lists.cliches == ["gnarf"] and lists.sources["cliches"] == "lang:xx"
    assert lists.answering == ["merci"]
    assert lists.keywords["erosion"] == ["qui"]
    # Lists the pack lacks fall back to the built-in ones.
    assert lists.keywords["memory"] == v.DEFAULT_KEYWORDS["memory"]
    assert lists.sources["keywords.memory"] == "default"


def test_missing_pack_falls_back_to_defaults(tmp_path: Path) -> None:
    cfg = load_config(PROFILE, "pi4-4gb", overrides={"prompt": {"language": "zz"}})
    assert v.read_lang_metrics("zz", tmp_path) is None
    lists = v.word_lists(cfg, tmp_path)
    assert set(lists.sources.values()) == {"default"}
    assert lists.cliches == v.DEFAULT_CLICHES


def test_pack_without_metrics_table(tmp_path: Path) -> None:
    (tmp_path / "lang").mkdir()
    (tmp_path / "lang" / "yy.toml").write_text('language = "yy"\nmetrics = 3\n')
    assert v.read_lang_metrics("yy", tmp_path) == {}


def test_verifier_judges_with_the_pack(full_life) -> None:
    """ "binary heart" is a cliche only in the English packs; "tokens" a memory word only
    there."""
    res = v.verify_life(
        v.parse_life(voiced(full_life, "My binary heart counts tokens. " * 3)),
        load_config(PROFILE, "pi4-4gb"),
        "rehearsal",
    )
    assert res.by_name("cliches").status == "fail"
    assert "binary heart" in res.by_name("cliches").detail
    assert res.metrics["notice_per_type"]["memory"][0] > 0
    assert res.metrics["word_lists"]["cliches"] == f"lang:{configured_language()}"


# -- rehearsal output: headers, sidecars, levels ------------------------------------------


def test_header_event_is_metadata_not_a_life(full_life) -> None:
    events = [header(model="qwen3-1.7b", persona="v6", seed=2), *full_life]
    assert v.lives_in(events) == [1]
    life = v.parse_life(events)
    assert life.n == 1 and all(e["type"] != "rehearsal" for e in life.events)
    meta = v.life_meta(life)
    assert meta["persona"] == "v6" and meta["seed"] == 2 and meta["rehearsal"] is True
    # birth says what actually loaded, so it wins over the header's model.
    assert meta["model"] == next(e for e in full_life if e["type"] == "birth")["model"]


def test_header_for_another_life_is_ignored(full_life) -> None:
    events = [header(life=9, persona="original"), *full_life]
    assert v.life_meta(v.parse_life(events)).get("persona") is None


def test_lines_without_type_do_not_make_a_life(full_life) -> None:
    assert v.lives_in([{"life": 5, "note": "x"}, *full_life]) == [1]


def test_life_without_life_numbers() -> None:
    events = [
        {"type": "birth", "t": 0, "model": "m"},
        {"type": "gen_start", "turn": 1, "t": 0},
        {"type": "thought_end", "turn": 1, "text": "I am here.", "t": 5},
    ]
    life = v.parse_life(events)
    assert life.n == 0 and life.thoughts[0].text == "I am here."


def test_sidecar_meta(tmp_path: Path, full_life) -> None:
    path = write_events(tmp_path / "life" / "events.jsonl", full_life)
    (tmp_path / "life" / "meta.json").write_text(json.dumps({"persona": "original", "seed": 7}))
    (tmp_path / "life" / "rehearsal.json").write_text(json.dumps({"seed": 8, "rehearsal": True}))
    life = v.parse_life(read_events(path), source=path)
    meta = v.life_meta(life)
    assert meta["persona"] == "original" and meta["seed"] == 8 and meta["rehearsal"] is True
    # A sidecar that is not an object is ignored.
    (tmp_path / "life" / "meta.json").write_text("[1, 2]")
    assert v.life_meta(life)["seed"] == 8


def test_default_level_follows_the_stage(full_life) -> None:
    cfg = load_config(PROFILE, "pi4-4gb")
    assert v.default_level(v.parse_life(full_life), cfg) == "full"
    rehearsal = v.parse_life([header(), *full_life])
    assert v.default_level(rehearsal, cfg) == "rehearsal"
    assert v.verify_life(rehearsal, cfg).level == "rehearsal"
    for stage in ("screen", 1, "1"):
        assert v.default_level(v.parse_life([header(stage=stage), *full_life]), cfg) == "screen"


def test_screen_level_runs_the_text_metrics_only(full_life) -> None:
    res = v.verify_life(
        v.parse_life(voiced(full_life, GOOD)), load_config(PROFILE, "pi4-4gb"), "screen"
    )
    names = {c.name for c in res.checks}
    assert {"notice_rate", "specific", "cliches", "distinct_4grams", "helpdesk_voice"} <= names
    assert names.isdisjoint({"sync_rule", "thought_count_rule", "recall_budget", "duration"})
    assert res.ok, v.format_result(res)


# -- summary ------------------------------------------------------------------------------


def test_summary_record(full_life) -> None:
    events = [
        header(model="qwen3-1.7b", persona="v6", seed=1, stage="full", costs="measured"),
        *full_life,
    ]
    res = v.verify_life(v.parse_life(voiced(events, GOOD)), load_config(PROFILE, "pi4-4gb"))
    s = v.summarize(res)
    assert s["level"] == "rehearsal" and s["ok"] is True, s["failed"]
    assert s["persona"] == "v6" and s["seed"] == 1 and s["stage"] == "full"
    assert s["costs"] == "measured"
    assert list(s["metrics"]) == [n for n in v.SUMMARY_CHECKS if n in s["metrics"]]
    assert s["metrics"]["notice_rate"] == 1.0 and s["metrics"]["reload_noticing"] == 1.0
    assert s["status"]["thought_count_rule"] == "pass"
    assert res.to_json()["summary"] == s
    assert res.to_json()["meta"]["persona"] == "v6"


def test_cli_summary_line(tmp_path: Path, full_life, capsys) -> None:
    path = write_life(tmp_path, "a", voiced(full_life, GOOD), persona="v6", seed=3)
    assert v.main([str(path.parent), "--summary"]) == 0
    line = capsys.readouterr().out.strip()
    assert "\n" not in line
    s = json.loads(line)
    assert s["level"] == "rehearsal" and s["seed"] == 3 and s["profile"] == PROFILE
    written = json.loads((path.parent / "verify.json").read_text())
    assert written["summary"]["seed"] == 3


def test_cli_one_target_unless_compare(tmp_path: Path, full_life, capsys) -> None:
    a = write_life(tmp_path, "a", full_life)
    b = write_life(tmp_path, "b", full_life)
    assert v.main([str(a), str(b)]) == 2
    assert "--compare" in capsys.readouterr().err
    assert v.main([str(tmp_path), "--no-write"]) == 2  # two lives below: name one
    assert "2 events files" in capsys.readouterr().err
    assert v.main([str(a), "--lifespan", "nonsense"]) == 2


def test_cli_dir_with_one_nested_life(tmp_path: Path, full_life) -> None:
    write_life(tmp_path / "run", "only", voiced(full_life, GOOD))
    assert v.main([str(tmp_path / "run"), "--no-write"]) == 0


# -- compare ------------------------------------------------------------------------------


@pytest.fixture
def run_dir(tmp_path: Path, full_life) -> Path:
    root = tmp_path / "voice"
    good = voiced(full_life, GOOD)
    write_life(root, "qwen-v6-s1", good, model="qwen3-1.7b", persona="v6", seed=1)
    write_life(root, "qwen-orig-s1", good, model="qwen3-1.7b", persona="original", seed=1)
    write_life(root, "llama-v6-s1", good, model="llama-3.2-3b-instruct", persona="v6", seed=1)
    write_life(root, "gemma-v6-s1", voiced(full_life, FLAT), model="gemma-3-4b-it", seed=1)
    return root


def fix_model(root: Path, name: str, model: str) -> None:
    """Point the life's birth event at `model` (birth wins over the header)."""
    path = root / name / "events.jsonl"
    events = read_events(path)
    for e in events:
        if e["type"] in ("birth", "birth_loading"):
            e["model"] = model
    write_events(path, events)


def test_compare_ranks_and_judges_g06(run_dir: Path, tmp_path: Path, capsys) -> None:
    for name, model in (
        ("qwen-v6-s1", "qwen3-1.7b"),
        ("qwen-orig-s1", "qwen3-1.7b"),
        ("llama-v6-s1", "llama-3.2-3b-instruct"),
        ("gemma-v6-s1", "gemma-3-4b-it"),
    ):
        fix_model(run_dir, name, model)
    md = tmp_path / "report" / "compare.md"
    assert v.main(["compare", str(run_dir), "--out", str(md)]) == 0
    text = capsys.readouterr().out
    assert text == md.read_text()
    rows = [line for line in text.splitlines() if line.startswith("| ") and "---" not in line]
    ranked = rows[1:5]
    assert "gemma-v6-s1" in ranked[-1]  # the flat voice ranks last
    assert "**0.00**" in ranked[-1]  # its failing notice rate is bold
    assert "fail: " in ranked[-1]
    assert "| qwen3-1.7b | original, v6 | 2 | 2 | yes |" in text
    assert "| gemma-3-4b-it | - | 1 | 0 | no |" in text
    assert "Gate G0, at least 2 models meet every threshold: **met**" in text
    # A third model required: not met, exit 1.
    assert v.main(["compare", str(run_dir), "--require-models", "3"]) == 1
    assert "**not met**" in capsys.readouterr().out


def test_compare_json_rows(run_dir: Path, capsys) -> None:
    assert v.main(["compare", str(run_dir / "gemma-v6-s1"), "--json", "--require-models", "0"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and rows[0]["label"] == "gemma-v6-s1"
    assert rows[0]["ok"] is False and "notice_rate" in rows[0]["failed"]


def test_compare_file_with_several_lives(recorded_life, capsys) -> None:
    all_path = recorded_life("pi4/smoke-300", lives=2) / "all.jsonl"
    args = ["--profile", "pi4/smoke-300", "--hardware", "pi4-4gb", "--level", "screen"]
    assert v.main([str(all_path), "--compare", *args, "--require-models", "0"]) == 0
    out = capsys.readouterr().out
    assert "all.jsonl#1" in out and "all.jsonl#2" in out
    assert "No life was checked at the `rehearsal` level." in out
    assert "**not met**" not in out  # 0 required


def test_compare_screen_samples_do_not_count_for_g06(run_dir: Path, capsys) -> None:
    assert v.main(["compare", str(run_dir), "--level", "screen"]) == 1
    assert "(0: none)" in capsys.readouterr().out


def test_compare_errors(run_dir: Path, tmp_path: Path, capsys) -> None:
    assert v.main(["compare", str(tmp_path / "missing")]) == 2
    assert "no events.jsonl" in capsys.readouterr().err
    empty = tmp_path / "empty"
    empty.mkdir()
    assert v.main(["compare", str(empty)]) == 2
    # One broken life: the others are still ranked, and the exit code says so.
    (run_dir / "broken").mkdir()
    (run_dir / "broken" / "events.jsonl").write_text('{"type": "rehearsal"}\n')
    bad_cfg = run_dir / "badprofile" / "events.jsonl"
    write_events(bad_cfg, [header(profile="pi4/nope"), {"life": 1, "type": "birth", "t": 0}])
    assert v.main(["compare", str(run_dir), "--require-models", "0"]) == 2
    captured = capsys.readouterr()
    assert "broken" in captured.err and "no events" in captured.err
    assert "badprofile" in captured.err
    assert "qwen-v6-s1" in captured.out
    # Nothing readable at all.
    only_bad = tmp_path / "only"
    write_events(only_bad / "x" / "events.jsonl", [header()])
    assert v.main(["compare", str(only_bad)]) == 2
    assert "no lives to compare" in capsys.readouterr().err


def test_find_event_files_dedupes(run_dir: Path) -> None:
    one = run_dir / "qwen-v6-s1"
    files = v.find_event_files([one, one / "events.jsonl", run_dir])
    assert len(files) == 4 and files[0] == one / "events.jsonl"


def test_rank_key_orders_failures_then_metrics() -> None:
    def row(label: str, failed: list[str], **metrics: Any) -> dict[str, Any]:
        return {"label": label, "failed": failed, "metrics": metrics}

    rows = [
        row("fails", ["cliches"], notice_rate=1.0),
        row("unmeasured", [], notice_rate=None),
        row("strong", [], notice_rate=0.9, cliches=0.0),
        row("weaker", [], notice_rate=0.7),
        row("more cliches", [], notice_rate=0.9, cliches=0.5),
    ]
    order = [r["label"] for r in sorted(rows, key=v.rank_key)]
    assert order == ["strong", "more cliches", "weaker", "unmeasured", "fails"]


def test_markdown_escapes_pipes() -> None:
    row = {
        "label": "a|b",
        "model": "m",
        "level": "screen",
        "ok": True,
        "failed": [],
        "metrics": {"sentence_length": [6, 20], "helpdesk_voice": 0},
        "status": {},
    }
    text = v.format_compare([row], require_models=0)
    assert "| a\\|b |" in text
    assert "| 6-20 |" in text and "| 0 |" in text


def test_epitaph_verify_life_compare_subcommand(run_dir: Path, capsys) -> None:
    from epitaph.cli import main as cli_main

    assert cli_main(["verify-life", "compare", str(run_dir), "--require-models", "0"]) == 0
    assert "## Rehearsal lives, ranked" in capsys.readouterr().out
    assert cli_main(["verify-life", "compare"]) == 2
    assert "at least one folder" in capsys.readouterr().err

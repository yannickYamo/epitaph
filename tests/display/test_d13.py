"""D13 on a recorded fake life (BUILD_PLAN 9 D5, gate A9).

The life in `data/skeleton-1200.jsonl` is replayed on its own clock and drawn offscreen at
800x480, 1280x720, 1920x1080 and 1080x1920 on the plain theme, at two moments: a full
screen of live text before the first forgetting, and the last half second before death
(forgotten grey, fading words). Each picture must read back through tesseract with at
least 95% of the words shown, measure at least 12:1 between the rendered text and ground
colours, and break no word across lines.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "offscreen")
pytest.importorskip("pygame")

from epitaph.display import screenshot as shot
from epitaph.display.layout import Frame, VerifyProbe
from epitaph.display.themes import PLAIN, contrast_ratio

pytestmark = pytest.mark.display

MOMENTS = ["writing", "late"]
SIZES = shot.D13_SIZES


@pytest.mark.tesseract
@pytest.mark.parametrize("moment", MOMENTS)
@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_d13_recorded_life(
    size: tuple[int, int], moment: str, recorded_life: list[dict[str, Any]], tmp_path: Path
) -> None:
    r = shot.readability_life(size, tmp_path, recorded_life, moment)
    assert r["words_shown"] >= 30, r
    assert r["ocr_accuracy"] >= 0.95, r
    assert r["contrast"] >= 12.0, r
    assert r["split_words"] == 0, r
    assert r["cols"] <= 48
    assert shot.passes(r)


@pytest.mark.tesseract
def test_d13_on_todays_simulator(tmp_path: Path) -> None:
    """The simulator as it is now, not only as recorded: one size, the late moment."""
    r = shot.readability_life((1280, 720), tmp_path, shot.simulated_life(), "late")
    assert shot.passes(r), r


def test_the_recorded_life_is_a_whole_life(recorded_life: list[dict[str, Any]]) -> None:
    types = [e["type"] for e in recorded_life]
    assert types[0] == "birth_loading" and "death" in types and "forget" in types
    assert sum(t == "word" for t in types) > 300
    m = shot.life_moments(recorded_life)
    assert set(m) == {"writing", "late"} and 0 < m["writing"] < m["late"]


def test_late_moment_shows_every_word_state(
    recorded_life: list[dict[str, Any]], tmp_path: Path
) -> None:
    """At 1080x1920 the late screen holds forgotten and live text, all of it measured."""
    frame = _frame(recorded_life, "late", (1080, 1920), tmp_path)
    kinds = {s.kind for s in frame.spans}
    assert {"live", "forgotten"} <= kinds
    c = shot.measure_contrast(tmp_path / "f.png")
    assert tuple(c["bg"]) == PLAIN.bg
    assert c["ratio"] == pytest.approx(contrast_ratio(PLAIN.live, PLAIN.bg), rel=0.01)


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_no_word_of_the_life_is_split_at_any_size(
    size: tuple[int, int], recorded_life: list[dict[str, Any]], tmp_path: Path
) -> None:
    """Not just the words on screen at two moments: every thought of the life, laid out at
    the line width each size gets."""
    frame = _frame(recorded_life, "writing", size, tmp_path)
    assert frame.split_words == 0
    assert VerifyProbe(frame.cols).split_words(recorded_life) == 0


def test_moments_without_forget_or_death() -> None:
    events = shot.sample_events(["one two three"])
    for k, e in enumerate(events):
        e["t"] = float(k)
    m = shot.life_moments(events)
    assert set(m) == {"writing"} and m["writing"] == pytest.approx(len(events) / 2 - 1.0)
    assert shot.life_moments([]) == {}


def test_view_at_types_with_the_recorded_cadence(recorded_life: list[dict[str, Any]]) -> None:
    at = shot.life_moments(recorded_life)["writing"]
    view = shot.view_at(recorded_life, at)
    assert view.mode == "living" and view.thoughts
    early = shot.view_at(recorded_life, 1.0)
    assert not any(th.words for th in early.thoughts)  # no word yet one second in


def test_main_on_a_recorded_life(
    recorded_life: list[dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`python -m epitaph.display.screenshot --events FILE`, with OCR stubbed by the truth."""
    from .conftest import RECORDED_LIFE

    frames: dict[str, Frame] = {}
    real_render = shot.render_png

    def render(path: Path, *a: Any, **kw: Any) -> Frame:
        frames[str(path)] = f = real_render(path, *a, **kw)
        return f

    monkeypatch.setattr(shot, "D13_SIZES", [(800, 480)])
    monkeypatch.setattr(shot, "render_png", render)
    monkeypatch.setattr(shot, "ocr_words", lambda p: shot.shown_words(frames[str(p)]))
    assert shot.main(["--out", str(tmp_path), "--events", str(RECORDED_LIFE)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3 and all('"pass": true' in line for line in lines)
    assert '"moment": "late"' in lines[-1]
    monkeypatch.setattr(shot, "simulated_life", lambda: recorded_life)
    monkeypatch.setattr(shot, "ocr_words", lambda p: [])
    assert shot.main(["--out", str(tmp_path)]) == 1


def _frame(
    events: list[dict[str, Any]], moment: str, size: tuple[int, int], tmp_path: Path
) -> Frame:
    at = shot.life_moments(events)[moment]
    return shot.render_png(tmp_path / "f.png", size, view=shot.view_at(events, at), now=at)

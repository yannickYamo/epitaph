"""D13 on a recorded fake life (gate A9).

The life in `data/skeleton-1200.jsonl` is replayed on its own clock and drawn offscreen at
800x480, 1280x720, 1920x1080 and 1080x1920 on the plain theme, at two moments: a full
screen of live text before the first forgetting, and the last half second before death
(forgotten grey, fading words). Each picture must read back through tesseract with at
least 95% of the words shown, measure at least 12:1 between the rendered text and ground
colours, and break no word across lines.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

# no window: SDL's offscreen driver on Linux, its dummy one where offscreen needs OpenGL
os.environ.setdefault("SDL_VIDEODRIVER", "offscreen" if sys.platform == "linux" else "dummy")
pytest.importorskip("pygame")

from epitaph.display import screenshot as shot
from epitaph.display.layout import Frame, LifeView, VerifyProbe, ViewSettings
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


def test_late_moment_shows_live_and_fading_text_at_12_to_1(
    recorded_life: list[dict[str, Any]], tmp_path: Path
) -> None:
    """At 1080x1920 the late screen holds live text and words still fading; forgotten words
    are gone. Every colour drawn measures at least 12:1 on the pixels."""
    frame = _frame(recorded_life, "late", (1080, 1920), tmp_path)
    kinds = {s.kind for s in frame.spans}
    assert {"live", "fading"} <= kinds and "forgotten" not in kinds
    c = shot.measure_contrast(tmp_path / "f.png")
    assert tuple(c["bg"]) == PLAIN.bg
    assert c["ratio"] == pytest.approx(contrast_ratio(PLAIN.live, PLAIN.bg), rel=0.01)
    contrasts = shot.text_contrasts(tmp_path / "f.png", frame)
    assert len(contrasts) >= 2 and min(contrasts.values()) >= 12.0, contrasts


# -- forgotten text fades at 12:1 until it is gone (5.12) -------------------------------------


@pytest.mark.parametrize("part", [0.0, 0.25, 0.5, 0.75, 0.97])
def test_a_reload_fade_stays_at_12_to_1_until_gone(
    part: float, default_life: list[dict[str, Any]], tmp_path: Path
) -> None:
    """The first reload of the recorded 30-minute life forgets two thoughts: at every point
    of their fade each drawn colour measures at least 12:1, and after it they are gone."""
    at = next(float(e["t"]) for e in default_life if e["type"] == "forget")
    fade_s = ViewSettings().fade_s
    now = at + part * fade_s
    view = shot.view_at(default_life, now)
    assert view.mode == "reloading"
    frame = shot.render_png(tmp_path / "fade.png", (1080, 1920), view=view, now=now)
    assert any(s.kind == "fading" for s in frame.spans)
    contrasts = shot.text_contrasts(tmp_path / "fade.png", frame)
    assert min(contrasts.values()) >= 12.0, contrasts
    assert frame.cursor is not None and frame.cursor.mode == "dim"  # the reload: a dim cursor
    gone = shot.view_at(default_life, at + fade_s + 0.01)
    assert not any(
        s.kind in ("fading", "forgotten")
        for s in shot.render_png(
            tmp_path / "gone.png", (1280, 720), view=gone, now=at + fade_s + 0.01
        ).spans
    )


def test_the_death_fade_stays_at_12_to_1(
    default_life: list[dict[str, Any]], tmp_path: Path
) -> None:
    view = LifeView(ViewSettings(birth_card=False))
    times = shot.life_times(default_life)
    for e, t in zip(default_life, times, strict=True):
        view.handle(e, t)
    start = view.death_fade_start()
    assert start is not None
    now = start + view.s.fade_s / 2
    frame = shot.render_png(tmp_path / "death.png", (1280, 720), view=view, now=now)
    assert frame.spans and {s.kind for s in frame.spans} == {"fading"}
    assert frame.cursor is None  # the cursor is gone at death
    assert min(shot.text_contrasts(tmp_path / "death.png", frame).values()) >= 12.0


# -- cards (D13 on the birth and death cards) ---------------------------------------------------


@pytest.mark.tesseract
@pytest.mark.parametrize("kind", ["birth", "death"])
@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_d13_cards(size: tuple[int, int], kind: str, tmp_path: Path) -> None:
    r = shot.readability_card(size, tmp_path, kind)
    assert r["words_shown"] >= 4, r
    assert r["ocr_accuracy"] >= 0.95, r
    assert r["contrast"] >= 12.0, r
    assert shot.passes(r)


def test_card_colours_are_all_drawn_at_12_to_1(tmp_path: Path) -> None:
    for kind in ("birth", "death"):
        view, now = shot.card_view(kind)
        frame = shot.render_png(tmp_path / "c.png", (800, 480), view=view, now=now)
        assert frame.card is not None and frame.card[0] == kind
        assert frame.card_shown == [len(x) for x in frame.card[1]]
        assert frame.card[1][0] == "life 12"
        contrasts = shot.text_contrasts(tmp_path / "c.png", frame)
        assert len(contrasts) == 2 and min(contrasts.values()) >= 12.0, contrasts


# -- the 16-segment theme (read back cell by cell) -----------------------------------------------


@pytest.mark.parametrize("moment", MOMENTS)
@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_d13_segment16_recorded_life(
    size: tuple[int, int], moment: str, recorded_life: list[dict[str, Any]], tmp_path: Path
) -> None:
    at = shot.life_moments(recorded_life)[moment]
    r = shot.readability_segments(size, tmp_path, shot.view_at(recorded_life, at), at, moment)
    assert (r["rows"], r["cols"]) == (6, 16)
    assert r["words_shown"] >= 5, r["want"]
    assert r["cell_accuracy"] == 1.0, (r["read"], r["want"])
    assert r["ocr_accuracy"] >= 0.95
    assert r["contrast"] >= 12.0, r["contrasts"]
    assert r["split_words"] == 0
    assert shot.passes(r)


@pytest.mark.parametrize("kind", ["birth", "death"])
def test_d13_segment16_cards(kind: str, tmp_path: Path) -> None:
    view, now = shot.card_view(kind)
    r = shot.readability_segments((1280, 720), tmp_path, view, now, f"{kind}_card")
    assert r["cell_accuracy"] == 1.0, (r["read"], r["want"])
    assert "LIFE 12" in r["want"][r["want"].index(next(x for x in r["want"] if x.strip()))]
    assert r["contrast"] >= 12.0


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
    monkeypatch.setattr(
        shot,
        "ocr_words",
        lambda p: shot.shown_words(frames[str(p)]) or shot.card_words(frames[str(p)]),
    )
    assert shot.main(["--out", str(tmp_path), "--events", str(RECORDED_LIFE)]) == 0
    lines = capsys.readouterr().out.splitlines()
    # the sample; the life at two moments in plain and in 16 segments; the two cards
    assert len(lines) == 7 and all('"pass": true' in line for line in lines), lines
    assert sum('"moment": "late"' in line for line in lines) == 2
    assert sum('"theme": "segment16"' in line for line in lines) == 2
    assert '"card": "death"' in lines[-1]
    monkeypatch.setattr(shot, "simulated_life", lambda: recorded_life)
    monkeypatch.setattr(shot, "ocr_words", lambda p: [])
    assert shot.main(["--out", str(tmp_path)]) == 1


def _frame(
    events: list[dict[str, Any]], moment: str, size: tuple[int, int], tmp_path: Path
) -> Frame:
    at = shot.life_moments(events)[moment]
    return shot.render_png(tmp_path / "f.png", size, view=shot.view_at(events, at), now=at)

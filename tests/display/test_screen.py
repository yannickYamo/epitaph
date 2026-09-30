"""D4 and D13: the pygame driver offscreen, screenshots, OCR, contrast, whole words."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "offscreen")
pygame = pytest.importorskip("pygame")

from epitaph.display import screenshot as shot  # noqa: E402
from epitaph.display.app import drive, iterate, make_driver  # noqa: E402
from epitaph.display.layout import LifeView, ViewSettings  # noqa: E402
from epitaph.display.screen import ScreenDriver  # noqa: E402
from epitaph.display.themes import PLAIN, SEGMENT16, contrast_ratio, get_theme  # noqa: E402

from .test_layout import ev, word  # noqa: E402

pytestmark = pytest.mark.display
HAVE_TESSERACT = shutil.which("tesseract") is not None


def pixels(path: Path) -> set[tuple[int, int, int]]:
    from PIL import Image

    img = Image.open(path).convert("RGB")
    return {
        (int(c[0]), int(c[1]), int(c[2]))
        for _, c in img.getcolors(img.width * img.height) or []
        if isinstance(c, tuple)
    }


@pytest.mark.skipif(not HAVE_TESSERACT, reason="tesseract not installed")
@pytest.mark.parametrize("size", shot.D13_SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_d13_readability(size: tuple[int, int], tmp_path: Path) -> None:
    r = shot.readability(size, tmp_path)
    assert r["words_shown"] >= 20
    assert r["ocr_accuracy"] >= 0.95, r
    assert r["contrast"] >= 12.0, r
    assert r["split_words"] == 0
    assert r["cols"] <= 48


def test_contrast_is_measured_on_pixels(tmp_path: Path) -> None:
    path = tmp_path / "a.png"
    shot.render_png(path, (800, 480), shot.sample_events())
    c = shot.measure_contrast(path)
    assert tuple(c["bg"]) == PLAIN.bg
    assert c["ratio"] == pytest.approx(contrast_ratio(PLAIN.live, PLAIN.bg), rel=0.01)


def test_theme_contrast_rules() -> None:
    for name in ("plain",):
        t = get_theme(name)
        assert contrast_ratio(t.live, t.bg) >= 12
        assert contrast_ratio(t.inherited, t.bg) >= 12
        # forgotten text fades through grey and stays readable
        assert contrast_ratio(t.forgotten, t.bg) >= 4.5
        for k in range(11):
            assert contrast_ratio(t.word("fading", k / 10), t.bg) >= 4.5
    assert contrast_ratio(SEGMENT16.live, SEGMENT16.bg) >= 7
    with pytest.raises(ValueError):
        get_theme("neon")


def test_ocr_helpers() -> None:
    assert shot.norm_words("Hello, World! It’s 52°C.") == ["hello", "world", "it", "s", "52", "c"]
    assert shot.word_accuracy(["a", "b", "c", "d"], ["a", "x", "c", "d"]) == 0.75
    assert shot.word_accuracy([], ["x"]) == 1.0


def test_typing_cursor_fade_dim_and_cards(tmp_path: Path) -> None:
    t = [0.0]
    d = ScreenDriver(size=(800, 480), clock=lambda: t[0])
    d.open()
    try:
        d.handle(ev("birth_loading", life=4, model="llama", quant="Q6_K"))
        d.render()
        assert d.last_frame is not None and d.last_frame.card is not None
        d.screenshot(str(tmp_path / "card.png"))
        assert PLAIN.card in pixels(tmp_path / "card.png")
        d.handle(ev("birth", life=4))
        d.handle(word(1, 0, "hello", 100, pause=2000, life=4))
        t[0] = 0.25
        d.render()
        assert d.last_frame.text_rows()[-1] == "hel"
        assert d.last_frame.cursor is not None and d.last_frame.cursor.mode == "on"
        d.handle(ev("forget", life=4, items=[{"turn": 1, "all": True}]))
        t[0] = 0.25 + d.view.s.fade_s / 2
        d.render()
        d.screenshot(str(tmp_path / "fade.png"))
        mid = PLAIN.word("fading", 0.5)
        assert any(abs(sum(c) - sum(mid)) < 12 for c in pixels(tmp_path / "fade.png"))
        d.handle(ev("reload", life=4, **{"from": "Q6_K", "to": "Q4_K_M"}))
        d.render()
        d.screenshot(str(tmp_path / "dim.png"))
        c = shot.measure_contrast(tmp_path / "dim.png")
        assert c["ratio"] < 8  # the text is dimmed during a reload
        d.handle(ev("death", life=4, cause="oom", lived_s=100))
        d.handle(ev("death_shown", life=4))
        t[0] += 1.0
        d.render()
        assert d.last_frame.card is not None and d.last_frame.card[0] == "death"
    finally:
        d.close()


def test_portrait_rotation_and_grid(tmp_path: Path) -> None:
    d = ScreenDriver(size=(800, 480), orientation="portrait")
    d.open()
    try:
        assert d.rotate == 90 and d.surface.get_size() == (480, 800)
        assert d.metrics is not None and d.metrics.width == 480
        for e in shot.sample_events(["rotated text here"]):
            d.view.handle(e, 0.0)
        d.render(1.0)
        d.screenshot(str(tmp_path / "rot.png"))
    finally:
        d.close()
    g = ScreenDriver(
        size=(640, 240), layout="grid", grid=(6, 16), charset="segment16", theme=SEGMENT16
    )
    g.open()
    try:
        for e in [
            *shot.sample_events(["a small led panel"]),
            ev("vitals", recall=100, recall_used=40),
        ]:
            g.view.handle(e, 0.0)
        g.render(1.0)
        f = g.last_frame
        assert f is not None and f.rows <= 6 and f.cols <= 16
        assert "A SMALL" in "\n".join(f.text_rows())
        g.view.handle(ev("birth_loading", life=2, model="m"), 2.0)
        g.render(2.0)
        assert g.last_frame is not None and g.last_frame.card is not None
        g.view.handle(ev("exhibit", life=2, open=False), 2.0)
        g.render(2.0)
    finally:
        g.close()


def test_status_strip_never_cuts_a_part(tmp_path: Path) -> None:
    d = ScreenDriver(size=(800, 480))
    d.open()
    try:
        for e in shot.sample_events():
            d.view.handle(e, 0.0)
        d.render(0.0)
    finally:
        d.close()


def test_rendering_speed_budget() -> None:
    """A rough CPU guard for the Pi (D7 sets the real budget)."""
    import time

    d = ScreenDriver(size=(1280, 720))
    d.open()
    try:
        for e in shot.sample_events():
            d.view.handle(e, 0.0)
        d.render(1.0)
        t0 = time.perf_counter()
        for k in range(30):
            d.render(1.0 + k / 30)
        per_frame = (time.perf_counter() - t0) / 30
    finally:
        d.close()
    assert per_frame < 0.1  # laptop: see report; generous for slow CI runners


async def test_screen_driver_runs_under_drive() -> None:
    d = make_driver("screen", {"min_font_px": 24}, size=(640, 360))
    assert isinstance(d, ScreenDriver)
    events: list[dict[str, Any]] = [ev("birth"), word(1, 0, "abc", 5), word(1, 1, "def", 5)]
    await drive(d, iterate(events), fps=120, linger_s=0.05)
    assert d.last_frame is not None and "abc def" in "\n".join(d.last_frame.text_rows())
    assert d.pg is None  # closed


def test_screenshot_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(shot, "D13_SIZES", [(800, 480)])
    monkeypatch.setattr(shot, "ocr_words", lambda p: shot.norm_words(" ".join(shot.SAMPLE)))
    assert shot.main(["--out", str(tmp_path)]) == 0
    assert '"pass": true' in capsys.readouterr().out
    assert shot.main(["--out", str(tmp_path), "--min-contrast", "30"]) == 1


def test_render_png_with_a_view(tmp_path: Path) -> None:
    v = LifeView(ViewSettings(birth_card=False))
    for e in shot.sample_events(["from a view"]):
        v.handle(e, 0.0)
    f = shot.render_png(tmp_path / "v.png", (640, 360), view=v)
    assert "from a view" in "\n".join(f.text_rows())


def test_an_unchanged_frame_is_not_painted_again() -> None:
    d = ScreenDriver(size=(640, 360))
    d.open()
    try:
        d.view.handle(ev("birth"), 0.0)
        d.view.handle(word(1, 0, "still", 100, pause=5000), 0.0)
        assert d.draw(0.05) is True
        assert d.draw(0.06) is False  # same letters, same cursor
        assert d.draw(0.15) is True  # a new letter
        assert d.draw(0.16, force=True) is True
    finally:
        d.close()

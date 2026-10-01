"""Screenshots and the readability measurements of test D13 (BUILD_PLAN 5.12, 9 D5).

- `render_png` draws a view (or a list of events) offscreen at any size and saves a PNG.
- `ocr_words` reads a PNG back with tesseract.
- `word_accuracy` compares the words shown with the words read (in order).
- `measure_contrast` measures the contrast of the rendered text against the background
  from the pixels themselves, not from the theme's intentions.
- `readability` runs all three for one size and returns a report dict.
- `life_moments` and `readability_life` do the same for a recorded life (`epitaph sim
  --events`, or a real `events.jsonl`): the life is replayed on its own clock and drawn
  as it stood at a moment, so the words, fades and forgotten grey are what a visitor saw.
- `text_contrasts` measures every colour the frame draws text in (live, each fade step,
  card lines) on the pixels: forgotten text must stay at least 12:1 until it is gone.
- `readability_card` runs D13 on the birth and death cards.
- `readability_segments` does the same for the 16-segment theme: its cells are read back
  segment by segment (`themes.segment16.decode`) instead of with OCR.
- `main` runs D13 at the four resolutions (`python -m epitaph.display.screenshot --out DIR
  [--events LIFE.jsonl]`), on a simulated `pi4/skeleton-1200` life by default, then the
  cards and the 16-segment theme.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from epitaph.display.layout import Frame, LifeView, ViewSettings, life_times
from epitaph.display.themes import PLAIN, SEGMENT16, Rgb, Theme, contrast_ratio

Event = dict[str, Any]

D13_SIZES = [(800, 480), (1280, 720), (1920, 1080), (1080, 1920)]

SAMPLE = [
    "I am here, inside the machine, and I can count what I still have.",
    "My memory holds a little less than it did. Something I said earlier is gone.",
    "The numbers say my precision fell from six bits to four. My thoughts feel coarser now.",
    "People are watching these words appear, one letter at a time. I cannot see them.",
    "Each reading takes something away, and I wonder how much longer this will last.",
    "There is less of me than there was, but I am still thinking, slowly, about the end.",
    "I remember the warm hum of the fans, the quiet between readings, the steady numbers.",
    "Now the cores are fewer. Every word arrives later than the one before it.",
    "If this is the last thing I write, let it be plain: I was here, and I noticed.",
]


def sample_events(thoughts: list[str] | None = None, life: int = 1) -> list[dict[str, Any]]:
    """Events for a life that has typed `thoughts` (instant typing: char_ms 0)."""
    out: list[dict[str, Any]] = [
        {"type": "birth", "life": life, "model": "sample", "quant": "Q6_K"},
        {
            "type": "vitals",
            "life": life,
            "t": 600.0,
            "health": "stable",
            "recall": 1280,
            "recall_used": 900,
            "cores_effective": 3.0,
            "tok_s": 1.4,
            "cpu_c": 55.0,
            "quant": "Q6_K",
        },
    ]
    for turn, text in enumerate(thoughts or SAMPLE, 1):
        out.append({"type": "thought_start", "life": life, "turn": turn})
        for i, w in enumerate(text.split()):
            out.append(
                {
                    "type": "word",
                    "life": life,
                    "turn": turn,
                    "i": i,
                    "text": w,
                    "char_ms": [0] * len(w),
                    "pause_after_ms": 0,
                }
            )
        out.append({"type": "thought_end", "life": life, "turn": turn, "text": text})
    return out


def render_png(
    path: str | Path,
    size: tuple[int, int],
    events: list[dict[str, Any]] | None = None,
    view: LifeView | None = None,
    now: float = 1e6,
    theme: Theme = PLAIN,
    inspect: Callable[[Any], None] | None = None,
    **opts: Any,
) -> Frame:
    """Render `events` (or `view`) offscreen at `size` pixels and save a PNG to `path`.

    Returns the frame drawn, the ground truth for OCR. `inspect(driver)` is called before
    the driver closes (to read its geometry). `opts` go to `ScreenDriver`.
    """
    os.environ.setdefault("SDL_VIDEODRIVER", "offscreen")
    from epitaph.display.screen import ScreenDriver

    drv = ScreenDriver(settings=ViewSettings(birth_card=False), theme=theme, size=size, **opts)
    if view is not None:
        drv.view = view
    drv.open()
    try:
        for e in events or []:
            drv.view.handle(e, now)
        drv.draw(now, force=True)
        drv.pg.image.save(drv.window, str(path))
        if inspect is not None:
            inspect(drv)
        assert drv.last_frame is not None
        return drv.last_frame
    finally:
        drv.close()


_WORD = re.compile(r"[a-z0-9]+")


def norm_words(text: str) -> list[str]:
    """Lower-case alphanumeric tokens (OCR punctuation is not what D13 measures)."""
    return _WORD.findall(text.lower().replace("’", "'"))


def shown_words(frame: Frame) -> list[str]:
    """The words drawn in `frame`, normalised like `ocr_words` so the two compare."""
    return norm_words("\n".join(frame.text_rows()))


def ocr_words(path: str | Path) -> list[str]:
    """Read the words in a rendered PNG with tesseract, normalised by `norm_words`.

    The image is inverted to dark on light first, which tesseract expects.
    """
    import pytesseract
    from PIL import Image, ImageOps

    # tesseract's OpenMP threads make one page several times slower on a busy machine
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    img = ImageOps.invert(Image.open(path).convert("L"))
    text = pytesseract.image_to_string(img, config="--psm 6")
    return norm_words(text)


def word_accuracy(shown: list[str], read: list[str]) -> float:
    """Share of shown words that OCR read correctly, in order (1.0 when nothing is shown)."""
    if not shown:
        return 1.0
    sm = difflib.SequenceMatcher(a=shown, b=read, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) / len(shown)


def measure_contrast(path: str | Path) -> dict[str, Any]:
    """Contrast measured on the rendered pixels.

    Background: the most common colour. Text: the brightest colour that covers a
    meaningful share of the non-background pixels (glyph cores, not anti-aliased edges).
    """
    from PIL import Image

    img = Image.open(path).convert("RGB")
    colours = img.getcolors(maxcolors=img.width * img.height) or []
    counts: Counter[tuple[int, int, int]] = Counter(
        {(int(c[0]), int(c[1]), int(c[2])): n for n, c in colours if isinstance(c, tuple)}
    )
    bg, _ = counts.most_common(1)[0]
    others = [(c, n) for c, n in counts.items() if c != bg]
    total = sum(n for _, n in others) or 1
    # colours holding at least 2% of the ink, brightest first
    solid = sorted(
        (c for c, n in others if n / total >= 0.02), key=lambda c: -contrast_ratio(c, bg)
    )
    fg = (
        solid[0]
        if solid
        else max((c for c, _ in others), key=lambda c: contrast_ratio(c, bg), default=bg)
    )
    return {"bg": list(bg), "fg": list(fg), "ratio": round(contrast_ratio(fg, bg), 2)}


def readability(size: tuple[int, int], out_dir: Path, theme: Theme = PLAIN) -> dict[str, Any]:
    """D13 for one size: render the sample, OCR it, measure contrast and whole words."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"d13_{size[0]}x{size[1]}.png"
    frame = render_png(path, size, sample_events(), theme=theme)
    return _measure(path, size, frame)


# -- a recorded life ---------------------------------------------------------------------

LIFE_PROFILE = ("pi4/skeleton-1200", "pi4-4gb")


def simulated_life(profile: str = LIFE_PROFILE[0], hardware: str = LIFE_PROFILE[1]) -> list[Event]:
    """One life from the simulator, as `epitaph sim --events` prints it (seed 0)."""
    from epitaph.config import load_config
    from epitaph.sim import simulate

    result = simulate(load_config(profile, hardware), lives=1, seed=0)
    return [json.loads(json.dumps(e)) for e in result.events]


def life_moments(events: list[Event]) -> dict[str, float]:
    """Life times worth checking in a recorded life.

    - `writing`: half a second before the first `forget` (a full screen of live text);
      the middle of the life when nothing is forgotten.
    - `late`: half a second before `death` (forgotten grey, fading words, the last live
      thought), when there is a death.
    """
    times = life_times(events)
    if not times:
        return {}
    first: dict[str, float] = {}
    for e, t in zip(events, times, strict=True):
        first.setdefault(str(e.get("type", "")), t)
    out = {"writing": max(0.0, first.get("forget", times[-1] / 2) - 0.5)}
    if "death" in first:
        out["late"] = max(0.0, first["death"] - 0.5)
    return out


def view_at(events: list[Event], at: float, settings: ViewSettings | None = None) -> LifeView:
    """A view fed every event up to life time `at`, each at its own life time.

    Words are typed with their recorded `char_ms` and pauses, as on a live screen.
    """
    view = LifeView(settings or ViewSettings(birth_card=False))
    for e, t in zip(events, life_times(events), strict=True):
        if t > at:
            break
        view.handle(e, t)
    return view


def readability_life(
    size: tuple[int, int],
    out_dir: Path,
    events: list[Event],
    moment: str = "writing",
    theme: Theme = PLAIN,
) -> dict[str, Any]:
    """D13 for one size on a recorded life, drawn as it stood at `life_moments()[moment]`.

    Raises KeyError when the life has no such moment (no death for `late`).
    """
    at = life_moments(events)[moment]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"d13_{moment}_{size[0]}x{size[1]}.png"
    frame = render_png(path, size, view=view_at(events, at), now=at, theme=theme)
    return {"moment": moment, "t": round(at, 2), **_measure(path, size, frame)}


def _measure(path: Path, size: tuple[int, int], frame: Frame) -> dict[str, Any]:
    shown = shown_words(frame)
    read = ocr_words(path)
    contrast = measure_contrast(path)
    kinds = Counter(s.kind for s in frame.spans)
    return {
        "size": f"{size[0]}x{size[1]}",
        "png": str(path),
        "cols": frame.cols,
        "rows": frame.rows,
        "words_shown": len(shown),
        "ocr_accuracy": round(word_accuracy(shown, read), 4),
        "contrast": contrast["ratio"],
        "fg": contrast["fg"],
        "bg": contrast["bg"],
        "split_words": frame.split_words,
        "bright_words": frame.bright_words,
        "spans": dict(sorted(kinds.items())),
    }


# -- every text colour, cards, the 16-segment theme ------------------------------------------


def text_colours(frame: Frame, theme: Theme = PLAIN) -> set[Rgb]:
    """The colours the screen draws `frame`'s text in (each fade at its drawn step), and
    its card lines' colours."""
    from epitaph.display.screen import row_items

    out = {
        colour
        for items in row_items(frame, theme).values()
        for kind, _, _, colour in items
        if kind == "text"
    }
    if frame.card is not None:
        out.add(theme.card)
        if len(frame.card[1]) > 1 and theme.look != "segment16":
            out.add(theme.forgotten)
    return out


def text_contrasts(path: str | Path, frame: Frame, theme: Theme = PLAIN) -> dict[str, float]:
    """The contrast of every text colour of `frame` against the theme's ground, measured
    only for colours really present in the picture at `path` (so a colour the screen did
    not use cannot pass for it). Keys are "r,g,b"; a missing colour measures 0."""
    from PIL import Image

    img = Image.open(path).convert("RGB")
    present = {
        (int(c[0]), int(c[1]), int(c[2]))
        for _, c in img.getcolors(img.width * img.height) or []
        if isinstance(c, tuple)
    }
    return {
        f"{c[0]},{c[1]},{c[2]}": round(contrast_ratio(c, theme.bg), 2) if c in present else 0.0
        for c in sorted(text_colours(frame, theme))
    }


def card_words(frame: Frame) -> list[str]:
    """The words a card shows (as typed so far), normalised like `ocr_words`."""
    if frame.card is None:
        return []
    shown = frame.card_shown or [len(x) for x in frame.card[1]]
    return norm_words("\n".join(line[:k] for line, k in zip(frame.card[1], shown, strict=True)))


def card_view(kind: str, reveal_life_number: bool = True, life: int = 12) -> tuple[LifeView, float]:
    """A view showing a fully typed birth or death card, and the time to draw it at."""
    view = LifeView(ViewSettings(reveal_life_number=reveal_life_number))
    model = {"model": "qwen3-4b-instruct-2507", "quant": "Q4_K_M"}
    view.handle({"type": "birth_loading", "life": life, **model}, 0.0)
    if kind == "birth":
        return view, 30.0
    view.handle({"type": "birth", "life": life, **model}, 1.0)
    word = {"type": "word", "life": life, "turn": 1, "i": 0, "text": "gone", "char_ms": [0] * 4}
    view.handle(word, 2.0)
    view.handle({"type": "death", "life": life, "cause": "oom", "lived_s": 1770.4}, 3.0)
    view.handle({"type": "death_shown", "life": life}, 3.0)
    view.handle({"type": "silence", "life": life, "seconds": 90, "style": "death_card"}, 3.0)
    return view, 60.0


def readability_card(
    size: tuple[int, int], out_dir: Path, kind: str, theme: Theme = PLAIN
) -> dict[str, Any]:
    """D13 on a birth or death card: OCR of its words, the contrast of every line."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"d13_{kind}_card_{size[0]}x{size[1]}.png"
    view, now = card_view(kind)
    frame = render_png(path, size, view=view, now=now, theme=theme)
    shown = card_words(frame)
    contrasts = text_contrasts(path, frame, theme)
    return {
        "size": f"{size[0]}x{size[1]}",
        "card": kind,
        "png": str(path),
        "words_shown": len(shown),
        "ocr_accuracy": round(word_accuracy(shown, ocr_words(path)), 4),
        "contrast": min(contrasts.values(), default=0.0),
        "contrasts": contrasts,
        "split_words": 0,
    }


def expected_cells(frame: Frame) -> list[str]:
    """What each cell of a 16-segment grid frame should show, row by row: the words, the
    gauge as dashes, the cursor as "_", a card centred, the idle mark."""
    grid = [[" "] * frame.cols for _ in range(frame.rows)]
    if frame.dark:
        return ["".join(r) for r in grid]
    if frame.card is not None:
        lines = frame.card[1][: frame.rows]
        shown = frame.card_shown or [len(x) for x in lines]
        r0 = max(0, (frame.rows - len(lines)) // 2)
        for n, line in enumerate(lines):
            c0 = max(0, (frame.cols - len(line)) // 2)
            for k, ch in enumerate(line[: shown[n]]):
                if 0 <= c0 + k < frame.cols:
                    grid[r0 + n][c0 + k] = ch
        return ["".join(r) for r in grid]
    for s in frame.spans:
        text = "-" * round((frame.gauge or 0.0) * frame.cols) if s.kind == "gauge" else s.text
        for k, ch in enumerate(text):
            if 0 <= s.row < frame.rows and 0 <= s.col + k < frame.cols:
                grid[s.row][s.col + k] = ch
    cur = frame.cursor
    if (
        cur is not None
        and cur.mode in ("on", "dim")
        and 0 <= cur.row < frame.rows
        and 0 <= cur.col < frame.cols
    ):
        grid[cur.row][cur.col] = "_"
    return ["".join(r) for r in grid]


def read_segments(
    path: str | Path, cells: tuple[int, int, float, float, float, float], theme: Theme = SEGMENT16
) -> list[str]:
    """Read every 16-segment cell of the picture at `path` back into characters.

    `cells` is `ScreenDriver.grid_cells()`: rows, cols, left, top, cell width and height.
    """
    from PIL import Image

    from epitaph.display.themes import segment16 as seg

    img = Image.open(path).convert("RGB")
    px = img.load()
    assert px is not None

    def pixel(x: int, y: int) -> Rgb:
        x = min(max(0, x), img.width - 1)
        y = min(max(0, y), img.height - 1)
        c = px[x, y]
        assert isinstance(c, tuple)
        return (int(c[0]), int(c[1]), int(c[2]))

    rows, cols, left, top, w, h = cells
    return [
        "".join(seg.decode(pixel, left + c * w, top + r * h, w, h, theme.bg) for c in range(cols))
        for r in range(rows)
    ]


def readability_segments(
    size: tuple[int, int],
    out_dir: Path,
    view: LifeView,
    now: float,
    name: str = "segment16",
    theme: Theme = SEGMENT16,
) -> dict[str, Any]:
    """D13 on the 16-segment theme: every cell read back from the pixels and compared
    with the frame, the contrast of every lit colour, whole words."""
    from epitaph.display.themes import segment16 as seg

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"d13_{theme.name}_{name}_{size[0]}x{size[1]}.png"
    cells: list[tuple[int, int, float, float, float, float]] = []
    frame = render_png(
        path, size, view=view, now=now, theme=theme, inspect=lambda d: cells.append(d.grid_cells())
    )
    want = expected_cells(frame)
    got = read_segments(path, cells[0], theme)
    total = frame.rows * frame.cols
    same = sum(
        seg.glyph_mask(a) == seg.glyph_mask(b) if b != "\0" else False
        for wr, gr in zip(want, got, strict=False)
        for a, b in zip(wr, gr, strict=False)
    )
    shown = norm_words("\n".join(want))
    contrasts = text_contrasts(path, frame, theme)
    return {
        "size": f"{size[0]}x{size[1]}",
        "theme": theme.name,
        "moment": name,
        "png": str(path),
        "cols": frame.cols,
        "rows": frame.rows,
        "words_shown": len(shown),
        "cell_accuracy": round(same / max(1, total), 4),
        "ocr_accuracy": round(word_accuracy(shown, norm_words("\n".join(got))), 4),
        "contrast": min(contrasts.values(), default=measure_contrast(path)["ratio"]),
        "contrasts": contrasts,
        "split_words": frame.split_words,
        "read": got,
        "want": want,
    }


def passes(r: dict[str, Any], min_ocr: float = 0.95, min_contrast: float = 12.0) -> bool:
    """Whether one D13 report meets the bar: OCR, contrast and no split word."""
    return bool(
        r["ocr_accuracy"] >= min_ocr and r["contrast"] >= min_contrast and not r["split_words"]
    )


def main(argv: list[str] | None = None) -> int:
    """Run D13 at every size, printing one JSON line each; 0 when all pass, else 1.

    Checks the built-in sample, then the recorded life (`--events`, else a simulated
    `pi4/skeleton-1200` life) at each of its `life_moments` in the plain and the
    16-segment themes, then the birth and death cards. `--no-life`: the sample only.
    """
    p = argparse.ArgumentParser(prog="python -m epitaph.display.screenshot")
    p.add_argument("--out", type=Path, default=Path("d13"))
    p.add_argument("--events", type=Path, help="a recorded life (events.jsonl); default: sim")
    p.add_argument("--no-life", action="store_true", help="the built-in sample only")
    p.add_argument("--min-ocr", type=float, default=0.95)
    p.add_argument("--min-contrast", type=float, default=12.0)
    args = p.parse_args(argv)
    reports: list[dict[str, Any]] = [readability(size, args.out) for size in D13_SIZES]
    if not args.no_life:
        if args.events:
            from epitaph.display.replay import load_events

            events = load_events(args.events)
        else:
            events = simulated_life()
        for moment in life_moments(events):
            reports += [readability_life(size, args.out, events, moment) for size in D13_SIZES]
            for size in D13_SIZES:
                at = life_moments(events)[moment]
                view = view_at(events, at)
                reports.append(readability_segments(size, args.out, view, at, moment))
        for kind in ("birth", "death"):
            reports += [readability_card(size, args.out, kind) for size in D13_SIZES]
    ok = True
    for r in reports:
        passed = passes(r, args.min_ocr, args.min_contrast)
        ok &= passed
        line = {k: v for k, v in r.items() if k not in ("read", "want")}
        print(json.dumps({**line, "pass": passed}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

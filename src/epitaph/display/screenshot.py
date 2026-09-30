"""Screenshots and the readability measurements of test D13 (BUILD_PLAN 5.12, 9 D5).

- `render_png` draws a view (or a list of events) offscreen at any size and saves a PNG.
- `ocr_words` reads a PNG back with tesseract.
- `word_accuracy` compares the words shown with the words read (in order).
- `measure_contrast` measures the contrast of the rendered text against the background
  from the pixels themselves, not from the theme's intentions.
- `readability` runs all three for one size and returns a report dict; `main` runs it at
  the four D13 resolutions (`python -m epitaph.display.screenshot --out DIR`).
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from epitaph.display.layout import Frame, LifeView, ViewSettings
from epitaph.display.themes import PLAIN, Theme, contrast_ratio

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
    **opts: Any,
) -> Frame:
    """Render offscreen and save a PNG. Returns the frame drawn (the ground truth)."""
    os.environ.setdefault("SDL_VIDEODRIVER", "offscreen")
    from epitaph.display.screen import ScreenDriver

    drv = ScreenDriver(settings=ViewSettings(birth_card=False), theme=theme, size=size, **opts)
    if view is not None:
        drv.view = view
    drv.open()
    try:
        for e in events or []:
            drv.view.handle(e, now)
        drv.draw(now)
        drv.pg.image.save(drv.window, str(path))
        assert drv.last_frame is not None
        return drv.last_frame
    finally:
        drv.close()


_WORD = re.compile(r"[a-z0-9]+")


def norm_words(text: str) -> list[str]:
    """Lower-case alphanumeric tokens (OCR punctuation is not what D13 measures)."""
    return _WORD.findall(text.lower().replace("’", "'"))


def shown_words(frame: Frame) -> list[str]:
    return norm_words("\n".join(frame.text_rows()))


def ocr_words(path: str | Path) -> list[str]:
    """Tesseract on the rendered PNG (inverted to dark on light, which tesseract expects)."""
    import pytesseract
    from PIL import Image, ImageOps

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
    shown = shown_words(frame)
    read = ocr_words(path)
    contrast = measure_contrast(path)
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
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m epitaph.display.screenshot")
    p.add_argument("--out", type=Path, default=Path("d13"))
    p.add_argument("--min-ocr", type=float, default=0.95)
    p.add_argument("--min-contrast", type=float, default=12.0)
    args = p.parse_args(argv)
    ok = True
    for size in D13_SIZES:
        r = readability(size, args.out)
        passed = (
            r["ocr_accuracy"] >= args.min_ocr
            and r["contrast"] >= args.min_contrast
            and not r["split_words"]
        )
        ok &= passed
        print(json.dumps({**r, "pass": passed}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

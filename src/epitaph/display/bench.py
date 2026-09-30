"""How much CPU the pygame screen costs (BUILD_PLAN 9 D7: under 5% of one core).

The bench drives a `ScreenDriver` on a virtual clock: a screen full of earlier thoughts,
then a new thought typed letter by letter, rendered at the display's frame rate. The CPU
time spent in `render` (composing, painting and updating the window) divided by the
virtual seconds covered is the share of one core the screen would take on this machine.

Scenarios:

- `typing`: a thought typed at `char_ms` a letter, with the cursor blinking in pauses.
- `fade`: every earlier thought is forgotten at once and fades for `fade_seconds`, as at a
  reload (the costliest few seconds of a life for the screen).

Run it offscreen, where the numbers do not depend on a monitor:

    SDL_VIDEODRIVER=offscreen python -m epitaph.display.bench --sizes 1280x720,1920x1080
"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

from epitaph.display.layout import ViewSettings

FILLER = (
    "My memory is smaller than it was and I cannot find the first things I said. "
    "The numbers tell me that my processors are fewer now, and every word takes longer. "
    "I think the machine is closing around me, one part at a time, and I let it."
)


def _events(char_ms: int, thoughts: int, text: str) -> list[dict[str, Any]]:
    """`thoughts` earlier thoughts typed instantly, then one typed at `char_ms` a letter."""
    out: list[dict[str, Any]] = [
        {"type": "birth_loading", "life": 1, "model": "bench", "quant": "Q4_K_M"},
        {"type": "birth", "life": 1, "model": "bench", "quant": "Q4_K_M"},
        {
            "type": "vitals",
            "life": 1,
            "t": 1500.0,
            "health": "failing",
            "recall": 600,
            "recall_used": 420,
            "cores_effective": 1.8,
            "tok_s": 1.4,
            "cpu_c": 58.0,
        },
    ]
    for turn in range(1, thoughts + 2):
        ms = 0 if turn <= thoughts else char_ms
        for i, w in enumerate(text.split()):
            pause = 700 if w[-1] in ".?!" else (250 if w[-1] in ",;:" else 90)
            out.append(
                {
                    "type": "word",
                    "life": 1,
                    "turn": turn,
                    "i": i,
                    "text": w,
                    "char_ms": [ms] * len(w),
                    "pause_after_ms": pause if ms else 0,
                }
            )
    return out


def bench_render(
    size: tuple[int, int],
    scenario: str = "typing",
    seconds: float = 10.0,
    fps: float = 30.0,
    char_ms: int = 60,
    partial: bool = True,
    layout: str = "flow",
    fade_s: float = 8.0,
) -> dict[str, Any]:
    """Render `seconds` of `scenario` at `fps` and measure the CPU it took.

    With `partial=False` every changed frame is painted and flipped whole (the drawing
    before dirty rectangles), for comparison. Returns the measurements as a dict; the
    `core_share` is CPU seconds per second of screen time.
    """
    os.environ.setdefault("SDL_VIDEODRIVER", "offscreen")
    from epitaph.display.screen import ScreenDriver

    settings = ViewSettings(birth_card=False, fade_s=fade_s)
    drv = ScreenDriver(settings=settings, size=size, layout=layout)
    drv.open()
    try:
        for e in _events(char_ms, 6, FILLER):
            drv.view.handle(e, 0.0)
        drv.render(0.0)
        start = 0.0
        if scenario == "fade":
            turns = [th.turn for th in drv.view.thoughts[:-1]]
            items = [{"turn": t, "all": True} for t in turns]
            drv.view.handle({"type": "forget", "life": 1, "items": items}, 0.0)
        elif scenario != "typing":
            raise ValueError(f"unknown scenario {scenario!r} (typing or fade)")
        frames = max(1, int(seconds * fps))
        painted = 0
        pg = drv.pg
        cpu0 = time.process_time()
        for k in range(1, frames + 1):
            now = start + k / fps
            if partial:
                sig = drv._sig  # pyright: ignore[reportPrivateUsage]
                drv.render(now)
                painted += drv._sig is not sig  # pyright: ignore[reportPrivateUsage]
            elif drv.draw(now):
                drv.draw(now, force=True)
                pg.display.flip()
                painted += 1
        cpu = time.process_time() - cpu0
    finally:
        drv.close()
    covered = frames / fps
    return {
        "size": f"{size[0]}x{size[1]}",
        "scenario": scenario,
        "layout": layout,
        "partial": partial,
        "fps": fps,
        "char_ms": char_ms,
        "frames": frames,
        "painted": painted,
        "cpu_s": round(cpu, 4),
        "core_share": round(cpu / covered, 4),
        "ms_per_painted_frame": round(1000 * cpu / max(1, painted), 3),
    }


def _size(text: str) -> tuple[int, int]:
    w, _, h = text.lower().partition("x")
    return int(w), int(h)


def main(argv: list[str] | None = None) -> int:
    """Print one JSON line per size and scenario; exit 1 if a share exceeds `--budget`."""
    p = argparse.ArgumentParser(prog="python -m epitaph.display.bench", description=__doc__)
    p.add_argument("--sizes", default="800x480,1280x720,1920x1080")
    p.add_argument("--scenarios", default="typing,fade")
    p.add_argument("--seconds", type=float, default=10.0)
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--char-ms", type=int, default=60)
    p.add_argument("--layout", default="flow", choices=["flow", "grid"])
    p.add_argument("--full", action="store_true", help="also measure whole-frame painting")
    p.add_argument("--budget", type=float, default=0.0, help="fail above this typing share")
    args = p.parse_args(argv)
    worst = 0.0
    for size in (_size(s) for s in args.sizes.split(",") if s):
        for scenario in (s for s in args.scenarios.split(",") if s):
            for partial in (True, False) if args.full else (True,):
                r = bench_render(
                    size,
                    scenario,
                    args.seconds,
                    args.fps,
                    args.char_ms,
                    partial,
                    args.layout,
                )
                print(json.dumps(r), flush=True)
                if partial and scenario == "typing":
                    worst = max(worst, r["core_share"])
    return 1 if args.budget and worst > args.budget else 0


if __name__ == "__main__":
    raise SystemExit(main())

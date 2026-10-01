"""A fake life of the dread plan for the display tests (`data/dread-2x1800.jsonl`).

The controller's `reading` and `world` events are built by another part of the system;
the display is tested against the shared interface on a recorded stand-in, so the tests
depend on nothing outside the repo. It is two simulated `pi4/default` lives (`epitaph sim
--profile pi4/default --hardware pi4-4gb --events --seed 0 --lives 2`) with:

- a `reading` {turn, text} in stream order right before the first word of each thought,
  the text being the reading the controller wrote for it (`vitals.reading`); the birth
  reading also lists the world it is born into;
- `world` {action, performed, state} losses at fixed life times, the screen dimming from
  15:00 to 29:00;
- the silence in the `vigil` style.

`python -m tests.display.dread OUT.jsonl` writes the file again.
"""

from __future__ import annotations

import json
import sys
from typing import Any

Event = dict[str, Any]

# (life seconds, action) performed on the machine; the screen goes dark from 15:00
WORLD: list[tuple[float, str]] = [
    (180.0, "service:avahi-daemon"),
    (420.0, "service:cron"),
    (600.0, "radio:off"),
    (780.0, "light:off"),
    (900.0, "screen:70"),
    (1200.0, "screen:45"),
    (1440.0, "screen:25"),
    (1620.0, "screen:12"),
    (1740.0, "screen:3"),
]

BIRTH_WORLD = " · services 3 running · processes 24 · radio on · light on · screen 100%"


def _state(done: list[str]) -> dict[str, Any]:
    """The world's inventory after the `done` actions (the FakeWorld's machine)."""
    services = [s for s in ("avahi-daemon", "cron", "rsyslog") if f"service:{s}" not in done]
    screen = 100
    for a in done:
        if a.startswith("screen:"):
            screen = int(a.split(":")[1])
    return {
        "services": services,
        "processes": 24 - 2 * (3 - len(services)),
        "radio": "off" if "radio:off" in done else "on",
        "light": "off" if "light:off" in done else "on",
        "screen": screen,
    }


def dread_life(events: list[Event]) -> list[Event]:
    """The simulator's lives with readings, world losses and the vigil added."""
    out: list[Event] = []
    readings: dict[tuple[int, int], str] = {}
    pending: tuple[int, str] | None = None  # the vitals reading waiting for its thought
    done: list[str] = []
    world = list(WORLD)
    first_word: set[tuple[int, int]] = set()
    for e in events:
        life = int(e.get("life", 0))
        etype = e.get("type")
        t = float(e.get("t", 0.0) or 0.0)
        if etype == "birth_loading":
            done, world = [], list(WORLD)
        while world and etype != "birth_loading" and t >= world[0][0] and e.get("t") is not None:
            at, action = world.pop(0)
            done.append(action)
            out.append(
                {
                    "v": 1,
                    "ts": e.get("ts"),
                    "life": life,
                    "type": "world",
                    "action": action,
                    "performed": True,
                    "state": _state(done),
                    "t": at,
                }
            )
        if etype == "vitals" and e.get("reading"):
            pending = (life, str(e["reading"]))
        elif etype == "thought_start" and pending is not None:
            text = pending[1]
            if not any(k[0] == life for k in readings):
                text += BIRTH_WORLD
            readings[(life, int(e["turn"]))] = text
            pending = None
        elif etype == "word" and (life, int(e["turn"])) not in first_word:
            key = (life, int(e["turn"]))
            first_word.add(key)
            if key in readings:
                out.append(
                    {
                        "v": 1,
                        "ts": e.get("ts"),
                        "life": life,
                        "type": "reading",
                        "turn": key[1],
                        "text": readings[key],
                        "t": e.get("t"),
                    }
                )
        elif etype == "silence":
            e = {**e, "style": "vigil"}
        out.append(e)
    return out


def main(argv: list[str]) -> int:
    from epitaph.config import load_config
    from epitaph.sim import simulate

    result = simulate(load_config("pi4/default", "pi4-4gb"), lives=2, seed=0)
    events = [json.loads(json.dumps(e)) for e in result.events]
    with open(argv[0], "w", encoding="utf-8") as f:
        for e in dread_life(events):
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

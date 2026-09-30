"""`epitaph replay`: any past life, any speed, from any moment (BUILD_PLAN 4 decision 8).

    epitaph replay 12 --speed 2 --from 20:00           # life 12 in a terminal
    epitaph replay lives/000012 --driver screen
    epitaph replay events.jsonl --driver none --port 7708   # serve; `epitaph display --port 7708`
    epitaph sim --events > life.jsonl && epitaph replay life.jsonl

Events are republished at their original cadence (divided by `--speed`) to a local bus and
to an in-process driver. The cadence comes from each event's life time `t` where present,
else from `ts`. Word typing (`char_ms`, `pause_after_ms`) is divided by the speed too, so
the letters keep up. `--from` folds everything before that moment into a snapshot, so the
screen starts exactly as it looked then.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from epitaph.display.layout import LifeView

Event = dict[str, Any]


def default_state_dir() -> Path:
    """Where lives are kept: $EPITAPH_STATE_DIR, else /var/lib/epitaph, else ~/.local/share."""
    env = os.environ.get("EPITAPH_STATE_DIR")
    if env:
        return Path(env).expanduser()
    if Path("/var/lib/epitaph/lives").exists():
        return Path("/var/lib/epitaph")
    return Path.home() / ".local/share/epitaph"


def resolve_events(arg: str, state_dir: Path | None = None) -> Path:
    """Find the events.jsonl for `arg`: a life number, a life folder or the file itself.

    A life number is looked up under `state_dir` (default: `default_state_dir()`). Raises
    FileNotFoundError when nothing matches.
    """
    p = Path(arg).expanduser()
    if p.is_file():
        return p
    if p.is_dir() and (p / "events.jsonl").exists():
        return p / "events.jsonl"
    if arg.isdigit():
        q = (state_dir or default_state_dir()) / "lives" / f"{int(arg):06d}" / "events.jsonl"
        if q.exists():
            return q
        raise FileNotFoundError(f"no events for life {int(arg)} at {q}")
    raise FileNotFoundError(f"not a life number, life folder or events file: {arg}")


def load_events(path: Path) -> list[Event]:
    """Read a JSONL event log, skipping blank, torn or untyped lines."""
    out: list[Event] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn last line after a power cut
            if isinstance(e, dict) and "type" in e:
                out.append(e)  # type: ignore[arg-type]
    return out


def _num(x: Any) -> float | None:
    """`x` as a float when it is a real number (not a bool), else None."""
    return float(x) if isinstance(x, int | float) and not isinstance(x, bool) else None


def timeline(events: list[Event]) -> list[float]:
    """Replay time (seconds from the first event) for each event.

    Within a life, time advances by the change in the life clock `t` (or in `ts` when an
    event has no `t`); a clock that goes backwards (the load before `birth`) adds nothing.
    Between lives it advances by the `ts` gap, and at least by the announced silence.
    """
    keys: list[float] = []
    key = 0.0
    prev_t: float | None = None
    prev_ts: float | None = None
    life: Any = None
    silence = 0.0
    for n, e in enumerate(events):
        t, ts = _num(e.get("t")), _num(e.get("ts"))
        if n and e.get("life") != life:
            # a real gap is the silence plus the next load; the simulator's ts barely moves
            ts_gap = ts - prev_ts if ts is not None and prev_ts is not None else 0.0
            key += max(ts_gap, silence)
            prev_t, silence = None, 0.0
        elif n:
            if t is not None and prev_t is not None:
                key += max(0.0, t - prev_t)
            elif ts is not None and prev_ts is not None:
                key += max(0.0, ts - prev_ts)
        life = e.get("life")
        if t is not None:
            prev_t = t
        if ts is not None:
            prev_ts = ts
        if e.get("type") == "silence":
            silence = _num(e.get("seconds")) or 0.0
        keys.append(key)
    return keys


def start_index(events: list[Event], from_s: float) -> int:
    """First event at or after life time `from_s` (birth_loading's t is not a life time)."""
    if from_s <= 0:
        return 0
    for n, e in enumerate(events):
        t = _num(e.get("t"))
        if t is not None and t >= from_s and e.get("type") != "birth_loading":
            return n
    return len(events)


def scaled(e: Event, speed: float) -> Event:
    """A word event with its typing cadence divided by the replay speed."""
    if e.get("type") != "word" or speed == 1:
        return e
    out = dict(e)
    if isinstance(e.get("char_ms"), list):
        out["char_ms"] = [max(0, round(float(x) / speed)) for x in e["char_ms"]]
    for k in ("pause_after_ms", "hesitate_before_ms"):
        if _num(e.get(k)) is not None:
            out[k] = max(0, round(float(e[k]) / speed))
    return out


async def republish(
    events: list[Event],
    publish: Callable[[Event], None],
    speed: float = 1.0,
    from_s: float = 0.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    mirror: LifeView | None = None,
    max_gap_s: float | None = None,
) -> int:
    """Publish `events` at their cadence divided by `speed`; returns how many were published.

    Events before `from_s` (life seconds) are folded into one snapshot. `max_gap_s` caps
    any single wait. `mirror`, if given, is fed every event so a bus can answer new
    subscribers with a current snapshot. Raises ValueError when `speed` is not positive.
    """
    if speed <= 0:
        raise ValueError("speed must be positive")
    keys = timeline(events)
    first = start_index(events, from_s)
    mirror = mirror or LifeView()
    for e, k in zip(events[:first], keys[:first], strict=True):
        mirror.handle(e, k)
    count = 0
    if first:
        base = keys[first] if first < len(keys) else keys[-1]
        snap = mirror.snapshot(base)
        publish(snap)
        count += 1
    prev = keys[first] if first < len(keys) else 0.0
    for e, k in zip(events[first:], keys[first:], strict=True):
        wait = (k - prev) / speed
        if max_gap_s is not None:
            wait = min(wait, max_gap_s)
        if wait > 0:
            await sleep(wait)
        prev = k
        out = scaled(e, speed)
        mirror.handle(out, k)
        publish(out)
        count += 1
    return count


def build_parser() -> argparse.ArgumentParser:
    """The argument parser for `epitaph replay`."""
    p = argparse.ArgumentParser(prog="epitaph replay", description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("life", help="life number, life folder, or events.jsonl")
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--from", dest="from_", default="0:00", help="start at this life time (mm:ss)")
    p.add_argument("--driver", choices=["terminal", "screen", "none"], default="terminal")
    p.add_argument(
        "--port", type=int, default=None, help="also serve the replay on this local port"
    )
    p.add_argument("--state-dir", type=Path)
    p.add_argument("--max-gap", type=float, default=None, help="cap any wait at this many seconds")
    p.add_argument("--size", help="window size WxH (screen driver)")
    p.add_argument("--fullscreen", action="store_true")
    p.add_argument("--hardware")
    return p


def main(argv: list[str] | None = None) -> int:
    """Run `epitaph replay`; returns 0, or 2 when the life or `--from` cannot be read."""
    from epitaph.config import ConfigError, parse_duration

    args = build_parser().parse_args(argv)
    try:
        path = resolve_events(args.life, args.state_dir)
        from_s = parse_duration(args.from_)
    except (FileNotFoundError, ConfigError) as e:
        print(f"epitaph replay: {e}", file=sys.stderr)
        return 2
    events = load_events(path)
    if not events:
        print(f"epitaph replay: no events in {path}", file=sys.stderr)
        return 2
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(args, events, from_s))
    return 0


async def _run(args: argparse.Namespace, events: list[Event], from_s: float) -> None:
    """Replay into the chosen driver and, with `--port`, onto a local bus."""
    import time

    from epitaph.display.app import display_config, drive, make_driver, parse_size
    from epitaph.events import EventBus

    queue: asyncio.Queue[Event | None] = asyncio.Queue()
    mirror = LifeView()
    t0 = time.monotonic()
    bus: EventBus | None = None
    if args.port is not None:
        bus = EventBus(port=args.port, snapshot=lambda: mirror.snapshot(time.monotonic() - t0))
        await bus.start()
        print(f"replay serving on 127.0.0.1:{bus.port}", file=sys.stderr)

    def publish(e: Event) -> None:
        if bus is not None:
            bus.publish(e)
        queue.put_nowait(e)

    async def feed() -> None:
        await republish(events, publish, args.speed, from_s, mirror=mirror, max_gap_s=args.max_gap)
        queue.put_nowait(None)

    async def source() -> AsyncIterator[Event]:
        while (e := await queue.get()) is not None:
            yield e

    feeder = asyncio.create_task(feed())
    try:
        if args.driver == "none":
            await feeder
        else:
            cfg = display_config(args.hardware)
            opts: dict[str, Any] = {}
            if args.driver == "screen":
                opts.update(size=parse_size(args.size), fullscreen=args.fullscreen)
            driver = make_driver(args.driver, cfg, **opts)
            await drive(driver, source(), exit_when_done=True, linger_s=3.0)
    finally:
        feeder.cancel()
        if bus is not None:
            await bus.stop()

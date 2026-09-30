"""`epitaph display`: a driver fed by the controller's event bus, locally or through an
SSH tunnel (BUILD_PLAN 4 decision 7, 9 D3).

    epitaph display                          # on the Pi: 127.0.0.1:7707
    epitaph display --connect pi             # on the laptop: ssh -N -L <free>:127.0.0.1:7707 pi
    epitaph display --connect pi --driver screen

The bus never listens on the network; the tunnel carries it. When the connection drops
(the controller restarts, Wi-Fi blinks, the tunnel dies) the view says so in the status
strip, reconnects with backoff (restarting ssh if needed) and redraws from the snapshot
the bus sends first on every subscription.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from epitaph.events import subscribe

Event = dict[str, Any]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def tunnel_argv(host: str, local_port: int, remote_port: int = 7707, ssh: str = "ssh") -> list[str]:
    """The ssh command for the tunnel. BatchMode: never prompt (keys only)."""
    return [
        ssh,
        "-N",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=3",
        "-o", "BatchMode=yes",
        "-L", f"{local_port}:127.0.0.1:{remote_port}",
        host,
    ]  # fmt: skip


async def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError):
        return False
    w.close()
    with contextlib.suppress(Exception):
        await w.wait_closed()
    return True


class Tunnel:
    """An `ssh -N -L` process kept alive across reconnects."""

    def __init__(
        self,
        host: str,
        remote_port: int = 7707,
        local_port: int = 0,
        argv: Callable[[str, int, int], list[str]] = tunnel_argv,
        ready_timeout: float = 20.0,
    ) -> None:
        self.host = host
        self.remote_port = remote_port
        self.local_port = local_port or free_port()
        self.argv = argv
        self.ready_timeout = ready_timeout
        self.proc: asyncio.subprocess.Process | None = None
        self.starts = 0

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def ensure(self) -> tuple[str, int]:
        """Start ssh if it is not running and wait until the local end accepts."""
        if not self.alive:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv(self.host, self.local_port, self.remote_port),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            self.starts += 1
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.ready_timeout
            while not await port_open("127.0.0.1", self.local_port):
                if not self.alive:
                    err = b""
                    if self.proc.stderr is not None:
                        err = await self.proc.stderr.read()
                    raise ConnectionError(
                        f"ssh {self.host} exited: {err.decode(errors='replace').strip()}"
                    )
                if loop.time() > deadline:
                    await self.close()
                    raise ConnectionError(f"ssh tunnel to {self.host} not ready")
                await asyncio.sleep(0.2)
        return "127.0.0.1", self.local_port

    async def close(self) -> None:
        if self.alive and self.proc is not None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()


async def reconnecting(
    connect: Callable[[], Awaitable[tuple[str, int]]],
    on_state: Callable[[bool], None] = lambda _up: None,
    backoff: tuple[float, float] = (0.5, 5.0),
    stop: asyncio.Event | None = None,
) -> AsyncIterator[Event]:
    """Events from the bus forever: subscribe, and on any drop wait and subscribe again.
    Each subscription starts with a snapshot, which redraws the view."""
    delay = backoff[0]
    while stop is None or not stop.is_set():
        got_any = False
        try:
            host, port = await connect()
            async for event in subscribe(host, port):
                if not got_any:
                    got_any = True
                    delay = backoff[0]
                    on_state(True)
                yield event
        except (
            OSError,
            ConnectionError,
            asyncio.IncompleteReadError,
            json.JSONDecodeError,
            ValueError,
        ):
            pass
        on_state(False)
        await asyncio.sleep(delay)
        delay = min(backoff[1], delay * 2)


def screen_present(drm: Path = Path("/sys/class/drm")) -> bool:
    """True when a display connector reports `connected` (the unit's ExecCondition, D6)."""
    for status in sorted(drm.glob("card*-*/status")):
        with contextlib.suppress(OSError):
            if status.read_text().strip() == "connected":
                return True
    return False


def pick_driver(configured: str, remote: bool, present: Callable[[], bool] = screen_present) -> str:
    """`auto`: the terminal for a remote view, the screen when one is connected locally."""
    if configured in ("terminal", "screen"):
        return configured
    if remote:
        return "terminal"
    return "screen" if present() else "terminal"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="epitaph display", description=(__doc__ or "").split("\n\n")[0]
    )
    p.add_argument("--connect", metavar="HOST", help="watch a remote controller through ssh -L")
    p.add_argument("--driver", choices=["terminal", "screen"], default=None)
    p.add_argument("--port", type=int, default=None, help="bus port on the controller (7707)")
    p.add_argument("--local-port", type=int, default=0, help="local end of the tunnel (free port)")
    p.add_argument("--layout", choices=["flow", "grid"])
    p.add_argument("--theme")
    p.add_argument("--size", help="window size WxH (screen driver)")
    p.add_argument("--fullscreen", action="store_true")
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--hardware", help="hardware overlay for the [display] settings")
    p.add_argument("--profile")
    p.add_argument("--screen-present", action="store_true", help="exit 0 if a screen is connected")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.screen_present:
        return 0 if screen_present() else 1
    from epitaph.display.app import display_config, drive, make_driver, parse_size

    cfg = display_config(args.hardware, args.profile)
    port = args.port or int(cfg.get("events_port", 7707))
    name = args.driver or pick_driver(str(cfg.get("driver", "auto")), remote=bool(args.connect))
    opts: dict[str, Any] = {"layout": args.layout, "theme": args.theme}
    if name == "screen":
        opts.update(size=parse_size(args.size), fullscreen=args.fullscreen)
    driver = make_driver(name, cfg, **opts)
    tunnel = Tunnel(args.connect, port, args.local_port) if args.connect else None

    async def connect() -> tuple[str, int]:
        return await tunnel.ensure() if tunnel else ("127.0.0.1", port)

    def on_state(up: bool) -> None:
        driver.view.connected = up

    async def run() -> None:
        try:
            await drive(driver, reconnecting(connect, on_state), fps=args.fps, exit_when_done=False)
        finally:
            if tunnel:
                await tunnel.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except ConnectionError as e:
        print(f"epitaph display: {e}", file=sys.stderr)
        return 2
    return 0

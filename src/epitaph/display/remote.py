"""`epitaph display`: a driver fed by the controller's event bus, locally or through an
SSH tunnel.

    epitaph display                          # on the Pi: 127.0.0.1:7707
    epitaph display --connect pi             # on the laptop: ssh -N -L <free>:127.0.0.1:7707 pi
    epitaph display --connect pi --driver screen
    epitaph display --connect pi,pi-eth      # try pi (Wi-Fi), then pi-eth (the cable)

The bus never listens on the network; the tunnel carries it. When the connection drops
(the controller restarts, Wi-Fi blinks, the tunnel dies) the view says so in the status
strip, reconnects with backoff (restarting ssh if needed) and redraws from the snapshot
the bus sends first on every subscription.

`--connect` takes SSH aliases in order of preference. Each (re)start of the tunnel tries
them in turn, so the view moves to the cable when mDNS fails and back to Wi-Fi when it
returns. The bare alias `pi` implies `pi,pi-eth`, the two aliases every
tool uses (docs/PI_FACTS.md).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import json
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from epitaph.display import presence
from epitaph.events import subscribe

Event = dict[str, Any]

# The laptop's aliases for the Pi (docs/PI_FACTS.md): Wi-Fi via mDNS first, then the cable.
DEFAULT_FALLBACKS: dict[str, tuple[str, ...]] = {"pi": ("pi-eth",)}


def parse_hosts(spec: str) -> list[str]:
    """SSH aliases from a `--connect` value, in order of preference.

    `"a,b"` gives `["a", "b"]`; a single alias with known fallbacks (`pi`) gets them appended.
    Raises ValueError when no alias is given.
    """
    hosts = [h.strip() for h in spec.split(",") if h.strip()]
    if not hosts:
        raise ValueError(f"no ssh host in {spec!r}")
    if len(hosts) == 1:
        hosts += [h for h in DEFAULT_FALLBACKS.get(hosts[0], ()) if h not in hosts]
    return hosts


def free_port() -> int:
    """Ask the OS for a free TCP port on 127.0.0.1 (the local end of the tunnel)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def tunnel_argv(host: str, local_port: int, remote_port: int = 7707, ssh: str = "ssh") -> list[str]:
    """The ssh command for the tunnel. BatchMode: never prompt (keys only)."""
    return [
        ssh,
        "-N",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ConnectTimeout=8",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=3",
        "-o", "BatchMode=yes",
        "-L", f"{local_port}:127.0.0.1:{remote_port}",
        host,
    ]  # fmt: skip


async def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """Whether a TCP connection to `host`:`port` succeeds within `timeout` seconds."""
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError):
        return False
    w.close()
    with contextlib.suppress(Exception):
        await w.wait_closed()
    return True


class Tunnel:
    """An `ssh -N -L` process kept alive across reconnects, with fallback hosts."""

    def __init__(
        self,
        host: str,
        remote_port: int = 7707,
        local_port: int = 0,
        argv: Callable[[str, int, int], list[str]] = tunnel_argv,
        ready_timeout: float = 20.0,
        fallbacks: Sequence[str] = (),
    ) -> None:
        """Prepare, but do not start, a tunnel from `local_port` to `host`:`remote_port`.

        `local_port` 0 picks a free port. `argv` builds the ssh command (swapped in tests).
        `ready_timeout` is how long `ensure` waits, in seconds, for the local end to accept.
        `fallbacks` are further aliases for the same machine, tried in order when `host`
        fails; every restart begins again with `host`.
        """
        self.hosts = [host, *(h for h in fallbacks if h != host)]
        self.host = host
        self.remote_port = remote_port
        self.local_port = local_port or free_port()
        self.argv = argv
        self.ready_timeout = ready_timeout
        self.proc: asyncio.subprocess.Process | None = None
        self.starts = 0

    @property
    def alive(self) -> bool:
        """Whether the ssh process has been started and has not exited."""
        return self.proc is not None and self.proc.returncode is None

    async def ensure(self) -> tuple[str, int]:
        """Start ssh if it is not running and wait until the local end accepts.

        Tries each host in `hosts` in order; `host` is the one that answered. Returns the
        local (host, port) to subscribe to. Raises ConnectionError, with every host's
        reason, when none of them gives a working tunnel.
        """
        if not self.alive:
            errors: list[str] = []
            for host in self.hosts:
                try:
                    await self._start(host)
                except ConnectionError as e:
                    errors.append(str(e))
                    continue
                self.host = host
                break
            else:
                raise ConnectionError("; ".join(errors))
        return "127.0.0.1", self.local_port

    async def _start(self, host: str) -> None:
        """Start ssh to `host` and wait for the local end; ConnectionError if it fails."""
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv(host, self.local_port, self.remote_port),
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
                raise ConnectionError(f"ssh {host} exited: {err.decode(errors='replace').strip()}")
            if loop.time() > deadline:
                await self.close()
                raise ConnectionError(f"ssh tunnel to {host} not ready")
            await asyncio.sleep(0.2)

    async def close(self) -> None:
        """Stop ssh: terminate, then kill if it has not exited after 3 seconds."""
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
    fail_fast: bool = False,
) -> AsyncIterator[Event]:
    """Yield bus events until `stop` is set, resubscribing after every drop.

    Each subscription starts with a snapshot, which redraws the view. `on_state` is called
    with True on the first event of a subscription and False after each drop; the retry
    delay doubles from `backoff[0]` to `backoff[1]` seconds and resets once events flow.

    With `fail_fast`, a ConnectionError from `connect` before any event has ever arrived
    is raised (a tunnel that cannot be set up at all: wrong alias, no key); once the view
    has been connected, every failure is retried. A refused or dropped bus is always
    retried: the controller may simply not be up yet.
    """
    delay = backoff[0]
    ever = False
    while stop is None or not stop.is_set():
        got_any = False
        try:
            address = await connect()
        except ConnectionError:
            if fail_fast and not ever:
                raise
            address = None
        except OSError:
            address = None
        if address is not None:
            try:
                async for event in subscribe(*address):
                    if not got_any:
                        got_any = ever = True
                        delay = backoff[0]
                        on_state(True)
                    yield event
            except (OSError, asyncio.IncompleteReadError, json.JSONDecodeError, ValueError):
                pass
        on_state(False)
        await asyncio.sleep(delay)
        delay = min(backoff[1], delay * 2)


def screen_present(drm: Path | None = None) -> bool:
    """True when a display connector reports `connected` (see `presence`; no overrides)."""
    return presence.screen_present(drm)


def pick_driver(configured: str, remote: bool, present: Callable[[], bool] | None = None) -> str:
    """`auto`: the terminal for a remote view, the screen when one is connected locally.

    `present` answers whether a screen is connected (default: `presence.detect`, which
    honours `EPITAPH_SCREEN` and `[display] screen`).
    """
    if configured in ("terminal", "screen"):
        return configured
    if remote:
        return "terminal"
    is_present = present or (lambda: presence.detect().present)
    return "screen" if is_present() else "terminal"


def add_arguments(p: argparse.ArgumentParser) -> None:
    """Add the `epitaph display` flags to `p` (the CLI's subparser, or `build_parser`)."""
    p.add_argument(
        "--connect",
        metavar="HOST[,HOST...]",
        help="watch a remote controller through ssh -L; later hosts are fallbacks (pi = pi,pi-eth)",
    )
    p.add_argument("--driver", choices=["terminal", "screen"], default=None)
    p.add_argument("--port", type=int, default=None, help="bus port on the controller (7707)")
    p.add_argument("--local-port", type=int, default=0, help="local end of the tunnel (free port)")
    p.add_argument("--ssh", default="ssh", help="ssh program for the tunnel (default: ssh)")
    p.add_argument("--layout", choices=["flow", "grid"])
    p.add_argument("--theme")
    p.add_argument("--size", help="window size WxH (screen driver)")
    p.add_argument("--fullscreen", action="store_true")
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--hardware", help="hardware overlay for the [display] settings")
    p.add_argument("--profile")
    p.add_argument(
        "--screen-present",
        action="store_true",
        help="exit 0 if a screen is connected, else 1 (the display unit's ExecCondition)",
    )


def build_parser() -> argparse.ArgumentParser:
    """The argument parser for `epitaph display`."""
    p = argparse.ArgumentParser(
        prog="epitaph display", description=(__doc__ or "").split("\n\n")[0]
    )
    add_arguments(p)
    return p


def check_screen(hardware: str | None = None, drm: Path | None = None) -> int:
    """`--screen-present`: print why, and return 0 when a screen is connected, else 1.

    Never raises: anything unexpected is printed on one line and counts as no screen, so a
    headless boot skips the display unit instead of logging a traceback.
    """
    try:
        found = presence.detect(drm, hardware=hardware)
    except Exception as e:  # the ExecCondition must answer, whatever happens
        print(f"screen: no (error: {e})")
        return 1
    print(found.line())
    return 0 if found.present else 1


def run(args: argparse.Namespace) -> int:
    """Run `epitaph display` with parsed `args` until closed or interrupted.

    Returns the exit code: 0 on a normal exit, 2 when the ssh tunnel cannot be set up at
    the start (after that, drops are retried forever), and for `--screen-present`, 0 if a
    screen is connected and 1 if not.
    """
    if args.screen_present:
        return check_screen(args.hardware)
    from epitaph.display.app import display_config, drive, make_driver, parse_size

    cfg = display_config(args.hardware, args.profile)
    port = args.port or int(cfg.get("events_port", 7707))
    name = args.driver or pick_driver(str(cfg.get("driver", "auto")), remote=bool(args.connect))
    opts: dict[str, Any] = {"layout": args.layout, "theme": args.theme}
    if name == "screen":
        opts.update(size=parse_size(args.size), fullscreen=args.fullscreen)
    driver = make_driver(name, cfg, **opts)
    tunnel = None
    if args.connect:
        first, *rest = parse_hosts(args.connect)
        argv = functools.partial(tunnel_argv, ssh=args.ssh)
        tunnel = Tunnel(first, port, args.local_port, argv=argv, fallbacks=rest)

    async def connect() -> tuple[str, int]:
        return await tunnel.ensure() if tunnel else ("127.0.0.1", port)

    def on_state(up: bool) -> None:
        driver.view.connected = up

    async def main_loop() -> None:
        try:
            source = reconnecting(connect, on_state, fail_fast=tunnel is not None)
            await drive(driver, source, fps=args.fps, exit_when_done=False)
        finally:
            if tunnel:
                await tunnel.close()

    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass
    except ConnectionError as e:
        print(f"epitaph display: {e}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    """`python -m epitaph.display.remote`: parse `argv` and `run`."""
    return run(build_parser().parse_args(argv))

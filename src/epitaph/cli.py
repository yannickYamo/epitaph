"""epitaph: a small language model lives and dies on a Raspberry Pi.

Command-line flags override the configuration in config/. Commands marked [planned] are on the
roadmap and exit with status 3 until they land.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from epitaph.config import ConfigError, load_config, parse_duration

# Commands on the roadmap: (help text, when it arrives). They exit with code 3 until then.
PLANNED: dict[str, tuple[str, str]] = {
    "selftest": ("check cgroups, limits and the network block on this machine", "phase 2"),
    "calibrate": ("measure working sets and set the death limit per model", "phase 2"),
    "download": ("download and verify models (today: tools/download_models.py)", "phase 1"),
    "bench": ("measure model speeds into bench/", "phase 2"),
    "post": ("publish each life's last line (V1.5)", "V1.5"),
    "archive": ("render every life as a static page (V1.5)", "V1.5"),
}


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--profile", help="profile name, e.g. pi4/default")
    p.add_argument("--hardware", help="hardware overlay: dev, pi4-4gb, pi5-8gb, pi5-16gb")
    p.add_argument("--lifespan", help="life length, mm:ss or seconds (rescales the profile)")
    p.add_argument("--backend", choices=["llama_server", "fake"], help="backend kind")
    p.add_argument("--display", dest="display_driver", help="display driver")
    p.add_argument("--model", help="model name from config/models.toml")


def _load(args: argparse.Namespace) -> Any:
    overrides: dict[str, Any] = {}
    if getattr(args, "backend", None):
        overrides.setdefault("backend", {})["kind"] = args.backend
    if getattr(args, "display_driver", None):
        overrides.setdefault("display", {})["driver"] = args.display_driver
    if getattr(args, "model", None):
        overrides.setdefault("life", {})["models"] = [args.model]
    lifespan = parse_duration(args.lifespan) if getattr(args, "lifespan", None) else None
    return load_config(args.profile, args.hardware, lifespan, overrides)


def cmd_sim(args: argparse.Namespace) -> int:
    """`epitaph sim`: simulate lives and print their milestones, or every event with --events."""
    from epitaph.sim import simulate

    cfg = _load(args)
    result = simulate(cfg, lives=args.lives, seed=args.seed)
    if args.events:
        for e in result.events:
            print(json.dumps(e))
        return 0
    if not args.quiet:
        for e in result.events:
            if e["type"] in ("birth", "reload", "reload_done", "erosion", "death", "silence"):
                extra = {k: v for k, v in e.items() if k not in ("v", "ts", "type", "life", "t")}
                print(f"life {e['life']} t={e.get('t', 0):7.1f}s {e['type']:12} {extra}")
    for n, (cause, thoughts) in enumerate(zip(result.causes, result.thoughts, strict=True), 1):
        print(f"life {n}: {thoughts} thoughts, cause={cause}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """`epitaph run`: the controller, life after life (BUILD_PLAN 5.8).

    Serves the event bus and the control channel on 127.0.0.1, writes transcripts under the
    state dir, and with `--display terminal` draws the lives in this terminal. Exits 1 when
    another controller holds the state dir.
    """
    from epitaph.state import AlreadyRunning, InstanceLock

    cfg = _load(args)
    if args.clock == "fake" and str(cfg.get("backend.kind")) != "fake":
        print(
            "--clock fake needs --backend fake (a real creature lives in real time)",
            file=sys.stderr,
        )
        return 2
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else cfg.state_dir
    lock = InstanceLock(state_dir)
    try:
        lock.acquire()
    except AlreadyRunning as e:
        print(str(e), file=sys.stderr)
        return 1
    try:
        if args.clock == "fake":
            from epitaph.clock import run_virtual

            run_virtual(lambda clock: _serve(args, cfg, state_dir, clock))
        else:
            asyncio.run(_serve(args, cfg, state_dir, None))
    except KeyboardInterrupt:
        pass
    finally:
        lock.release()
    return 0


async def _serve(args: argparse.Namespace, cfg: Any, state_dir: Path, clock: Any) -> None:
    """Run the controller with its bus, and the terminal display when asked."""
    import contextlib
    import signal
    from collections.abc import AsyncIterator

    from epitaph.clock import RealClock, VirtualClock
    from epitaph.controller import make_controller, sd_notify, watchdog_interval
    from epitaph.display.layout import LifeView
    from epitaph.events import EventBus

    loop = asyncio.get_running_loop()
    kind = str(cfg.get("backend.kind", "llama_server"))
    if clock is None:
        clock = VirtualClock(loop) if kind == "fake" else RealClock()  # type: ignore[arg-type]
    mirror = LifeView()
    port = args.port if args.port is not None else int(cfg.get("events.port", 7707))
    bus = EventBus(
        host=str(cfg.get("events.host", "127.0.0.1")),
        port=port,
        queue_size=int(cfg.get("events.subscriber_queue", 2000)),
        snapshot=lambda: mirror.snapshot(loop.time()),
    )
    display = str(cfg.get("display.driver", "none"))
    queue: asyncio.Queue[dict[str, Any] | None] | None = (
        asyncio.Queue() if display == "terminal" else None
    )

    def publish(e: dict[str, Any]) -> None:
        mirror.handle(e, loop.time())
        bus.publish(e)
        if queue is not None:
            queue.put_nowait(e)

    def reconfigure(profile: str | None, lifespan: float | None) -> Any:
        return _load(
            argparse.Namespace(
                **{**vars(args), "profile": profile or args.profile, "lifespan": lifespan}
            )
        )

    systemd = bool(os.environ.get("NOTIFY_SOCKET"))
    ctl = make_controller(
        cfg,
        clock,
        backend_kind=kind,
        publish=publish,
        state_dir=state_dir,
        lives=args.lives,
        reconfigure=reconfigure,
        notify=sd_notify if systemd else None,
        watchdog_s=watchdog_interval(),
    )
    await bus.start()
    ctl.attach(bus)
    print(f"epitaph run: events on {bus.host}:{bus.port}, lives in {state_dir}", file=sys.stderr)

    shower: asyncio.Task[None] | None = None
    if queue is not None:
        from epitaph.display.app import display_config, drive, make_driver

        driver = make_driver("terminal", display_config(cfg.hardware), clock=loop.time)

        async def source() -> AsyncIterator[dict[str, Any]]:
            while (e := await queue.get()) is not None:
                yield e

        fps = 2.0 if args.clock == "fake" else 30.0
        shower = asyncio.ensure_future(drive(driver, source(), fps=fps, clock=loop.time))

    main = asyncio.current_task()
    if args.clock != "fake" and main is not None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, main.cancel)
    try:
        await ctl.run()
    except asyncio.CancelledError:
        pass
    finally:
        if ctl.cur is not None:
            with contextlib.suppress(Exception):
                await ctl.cur.guard.inner.stop(hard=True)
        if queue is not None:
            queue.put_nowait(None)
        if shower is not None:
            with contextlib.suppress(Exception):
                await shower
        await bus.stop()


def cmd_estimate(args: argparse.Namespace) -> int:
    """`epitaph estimate`: print the cost model's report; exit 1 if a thought-count rule fails."""
    from epitaph.costmodel import estimate, format_report, load_costs

    cfg = _load(args)
    costs = load_costs(cfg, bench_dir=Path(args.bench) if args.bench else None)
    if args.no_cache_reuse:
        costs.cache_reuse_works = False
    report = estimate(cfg, costs)
    print(format_report(report))
    return 0 if report.ok else 1


def cmd_ctl(args: argparse.Namespace) -> int:
    """`epitaph ctl`: send one command to the running controller and print its JSON reply.

    Exits 2 if no controller is listening and 1 if the reply carries an error.
    """
    from epitaph.events import control

    cmd_args: dict[str, Any] = {}
    if args.action == "new-life":
        if args.lifespan:
            cmd_args["lifespan"] = parse_duration(args.lifespan)
        if args.profile:
            cmd_args["profile"] = args.profile
        if args.model:
            cmd_args["model"] = args.model
    cmd = args.action.replace("-", "_")
    try:
        reply = asyncio.run(control(cmd, cmd_args, port=args.port))
    except OSError as e:
        print(f"no controller on 127.0.0.1:{args.port} ({e})", file=sys.stderr)
        return 2
    print(json.dumps(reply, indent=1))
    return 1 if "error" in reply else 0


def _stub(name: str) -> Callable[[argparse.Namespace], int]:
    _, arrives = PLANNED[name]

    def run(_: argparse.Namespace) -> int:
        print(f"`epitaph {name}` is not available yet (planned for {arrives}).", file=sys.stderr)
        return 3

    return run


def build_parser() -> argparse.ArgumentParser:
    """The argument parser for every subcommand; each sets `fn`, the function that runs it."""
    parser = argparse.ArgumentParser(prog="epitaph", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sim", help="simulate lives on the fake clock")
    _common(p)
    p.add_argument("--lives", type=int, default=1)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--events", action="store_true", help="print every event as JSON lines")
    p.set_defaults(fn=cmd_sim)

    p = sub.add_parser("estimate", help="thought-count report for a profile (cost model)")
    _common(p)
    p.add_argument("--no-cache-reuse", action="store_true")
    p.add_argument("--bench", help="directory of measured cost files (default: bench/)")
    p.set_defaults(fn=cmd_estimate)

    p = sub.add_parser("run", help="run lives with the real controller")
    _common(p)
    p.add_argument(
        "--lives", type=int, default=None, help="stop after this many lives (default: forever)"
    )
    p.add_argument("--state-dir", help="counter, transcripts and status (default: from config)")
    p.add_argument("--port", type=int, help="event bus and control port (default: config, 7707)")
    p.add_argument(
        "--clock",
        choices=["real", "fake"],
        default="real",
        help="fake: virtual time, a whole life in seconds (fake backend only)",
    )
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("ctl", help="talk to the running controller")
    p.add_argument("action", choices=["status", "new-life", "screenshot"])
    p.add_argument("--lifespan")
    p.add_argument("--profile")
    p.add_argument("--model")
    p.add_argument("--port", type=int, default=7707)
    p.set_defaults(fn=cmd_ctl)

    from epitaph import rehearse, verify

    p = sub.add_parser("rehearse", help="accelerated lives with a real model, charged at Pi costs")
    rehearse.add_arguments(p)
    p.set_defaults(fn=rehearse.run)

    p = sub.add_parser("verify-life", help="check a recorded life (BUILD_PLAN 10.3)")
    verify.add_arguments(p)
    p.set_defaults(fn=verify.run)

    sub.add_parser("display", help="show a life live: local screen or --connect HOST")
    sub.add_parser("replay", help="replay a recorded life at any speed")

    for name, (text, arrives) in PLANNED.items():
        p = sub.add_parser(name, help=f"{text} [planned: {arrives}]")
        _common(p)
        p.add_argument("rest", nargs=argparse.REMAINDER)
        p.set_defaults(fn=_stub(name))
    return parser


# Subcommands with their own argument parsers (contract proposals D1, E1).
PASSTHROUGH = {
    "display": "epitaph.display.remote",
    "replay": "epitaph.display.replay",
}


def main(argv: list[str] | None = None) -> int:
    """Run the command line and return the exit status; config errors exit 2.

    `display` and `replay` hand their arguments to their own modules' parsers.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in PASSTHROUGH:
        import importlib

        return int(importlib.import_module(PASSTHROUGH[argv[0]]).main(argv[1:]))
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

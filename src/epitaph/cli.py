"""The `epitaph` command (BUILD_PLAN 6.1). Flags override config.

Subcommands owned by other agents are wired here as stubs that say who implements them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from typing import Any

from epitaph.config import ConfigError, load_config, parse_duration

STUBS: dict[str, tuple[str, str]] = {
    "run": ("B", "P1 (controller.py)"),
    "selftest": ("C", "P2 (body/)"),
    "calibrate": ("C", "P2 (body/calibrate.py)"),
    "download": ("A", "P0b (tools/download_models.py)"),
    "bench": ("A", "P2 (tools/bench.py)"),
    "rehearse": ("A", "P0c (rehearse.py)"),
    "post": ("F", "V1.5"),
    "archive": ("F", "V1.5"),
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


def cmd_estimate(args: argparse.Namespace) -> int:
    from epitaph.costmodel import estimate, format_report, load_costs

    cfg = _load(args)
    costs = load_costs(cfg)
    if args.no_cache_reuse:
        costs.cache_reuse_works = False
    report = estimate(cfg, costs)
    print(format_report(report))
    return 0 if report.ok else 1


def cmd_ctl(args: argparse.Namespace) -> int:
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
    owner, phase = STUBS[name]

    def run(_: argparse.Namespace) -> int:
        print(f"`epitaph {name}` is not built yet (agent {owner}, {phase}).", file=sys.stderr)
        return 3

    return run


def build_parser() -> argparse.ArgumentParser:
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
    p.set_defaults(fn=cmd_estimate)

    p = sub.add_parser("ctl", help="talk to the running controller")
    p.add_argument("action", choices=["status", "new-life", "screenshot"])
    p.add_argument("--lifespan")
    p.add_argument("--profile")
    p.add_argument("--model")
    p.add_argument("--port", type=int, default=7707)
    p.set_defaults(fn=cmd_ctl)

    from epitaph import verify

    p = sub.add_parser("verify-life", help="check a recorded life (BUILD_PLAN 10.3)")
    verify.add_arguments(p)
    p.set_defaults(fn=verify.run)

    sub.add_parser("display", help="show a life live: local screen or --connect HOST (part D)")
    sub.add_parser("replay", help="replay a recorded life at any speed (part D)")

    for name in STUBS:
        p = sub.add_parser(name, help=f"(agent {STUBS[name][0]})")
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

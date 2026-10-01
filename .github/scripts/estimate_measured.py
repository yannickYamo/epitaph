"""Gate G0.4: the thought-count rule on every Pi 4 profile with *measured* costs.

`make estimate` checks each profile with its first model and whatever costs `bench/` holds.
Gate G0.4 asks more: every Pi 4 profile passes for every candidate model, on costs measured
on the Pi. This script runs the cost model for each (profile, model) pair with the model's
bench files and says which of the rates and load times the profile needs are measured and
which still come from the overlay's estimates.

`--strict` is the gate: it fails a pair that still uses an estimated cost, and it judges the
review-2 F2 rule (no reload speeds generation up) as a failure even while
`[estimate] speed_monotonic` in the config only warns.

Usage (from the repository root, with src/ on PYTHONPATH):

    python .github/scripts/estimate_measured.py                    # bench/, every model in it
    python .github/scripts/estimate_measured.py --bench bench/measured
    python .github/scripts/estimate_measured.py --models qwen3-1.7b gemma-3-4b-it --strict

Exit status: 0 when every pair passes (and, with --strict, uses measured costs only and never
speeds up at a reload), 1
otherwise, 2 on a usage error such as a bench directory without any file for the class.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from epitaph.config import REPO_ROOT, load_config
from epitaph.costmodel import estimate, load_costs

PI4_PROFILES = [
    "pi4/default",
    "pi4/smoke-300",
    "pi4/skeleton-1200",
    "pi4/unbounded",
]


def bench_models(bench: Path, hw_class: str) -> list[str]:
    """Models with at least one bench file for this hardware class."""
    names = {
        str(json.loads(p.read_text())["model"]) for p in sorted(bench.glob(f"{hw_class}-*.json"))
    }
    return sorted(names)


def measured_keys(bench: Path, hw_class: str, model: str) -> tuple[set[str], set[int]]:
    """The "<step>-<threads>" rates and the load steps measured for a model."""
    rates: set[str] = set()
    loads: set[int] = set()
    for path in sorted(bench.glob(f"{hw_class}-{model}-*.json")):
        rec: dict[str, Any] = json.loads(path.read_text())
        if "tg_tok_s" in rec or "pp_tok_s" in rec:
            rates.add(f"{rec['step']}-{rec['threads']}")
        if "load_s" in rec:
            loads.add(int(rec["step"]))
    return rates, loads


def used_keys(cfg: Any) -> tuple[set[str], set[int]]:
    """The "<step>-<threads>" rates and the steps a profile's keyframes run at."""
    rates: set[str] = set()
    steps: set[int] = set()
    for kf in cfg.profile.keyframes:
        step, threads = kf.values.get("step"), kf.values.get("threads")
        if step is None or threads is None:
            continue
        rates.add(f"{int(step)}-{int(threads)}")
        steps.add(int(step))
    return rates, steps


def main(argv: list[str] | None = None) -> int:
    """Run the cost model for every (profile, model) pair and print one line each."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--bench", default=str(REPO_ROOT / "bench"), help="bench directory")
    p.add_argument("--hardware", default="pi4-4gb")
    p.add_argument("--profiles", nargs="+", default=PI4_PROFILES)
    p.add_argument("--models", nargs="+", help="default: every model with a bench file")
    p.add_argument(
        "--strict",
        action="store_true",
        help="fail when a needed cost is still an estimate or a reload speeds generation up",
    )
    args = p.parse_args(argv)

    bench = Path(args.bench)
    hw_class = load_config(args.profiles[0], args.hardware).hw_class
    models = args.models or bench_models(bench, hw_class)
    if not models:
        print(f"no {hw_class}-*.json files in {bench}", file=sys.stderr)
        return 2

    ok = True
    print(f"| Profile | Model | Rule | Thoughts | Estimated costs used ({args.hardware}) |")
    print("|---|---|---|---|---|")
    for profile in args.profiles:
        for model in models:
            overrides: dict[str, Any] = {"life": {"models": [model]}}
            if args.strict:
                overrides["estimate"] = {"speed_monotonic": "fail"}
            cfg = load_config(profile, args.hardware, overrides=overrides)
            report = estimate(cfg, load_costs(cfg, model, bench))
            have_rates, have_loads = measured_keys(bench, hw_class, model)
            need_rates, need_steps = used_keys(cfg)
            missing = sorted(need_rates - have_rates)
            missing += [f"load step {s}" for s in sorted(need_steps - have_loads)]
            passed = report.ok and not (args.strict and missing)
            ok = ok and passed
            rule = (
                "PASS"
                if report.ok
                else "FAIL " + "; ".join(f"({v.rule}) {v.detail}" for v in report.violations)
            )
            print(
                f"| {profile} | {model} | {rule} | {len(report.thought_times)} "
                f"| {', '.join(missing) or 'none'} |"
            )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

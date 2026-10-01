# pyright: strict
"""`epitaph calibrate`: the creature's working set and its death level, per model and step
(BUILD_PLAN 5.5, 9 C7).

The RAM death takes `memory.max` below the creature's working set at `end-0:30`. Spike S3
showed that a limit below its *anonymous* memory kills it in about a second when the weights
are loaded by direct I/O (ADR-009), and the first real life died that way at 29:30. Calibrate
makes it a measured fact for each model and ladder step on this machine:

1. Load the step in the creature cgroup, exactly as a life does (same flags, same cgroup).
2. Generate a few tokens, so every buffer is touched, and read the working set from the
   cgroup: `anon`, `file`, `memory.current` (and `memory.peak`, the cgroup's own maximum).
3. Set `memory.max` to `death_fraction` x anon, and time the kill (`memory.events` must show
   the kernel's OOM kill). A kill within `kill_within_s` (10 s) is a good trial.
4. Repeat from 1 until `trials` good trials in a row (5 for the step in force at the death,
   1 for the others); after a bad trial the fraction drops by 30% and the count restarts.

The result goes to `<state_dir>/calibration/<class>-<model>.json` (the body reads it at every
start) and, copied into the repository, to `bench/calibration/`. The body then squeezes to
the calibrated level when it is below the creature's anonymous memory, else to the fraction.

Like selftest, it needs a delegated cgroup: from a shell it relaunches itself as a transient
`Delegate=yes` unit for the service user. It takes the controller's instance lock, so it
refuses to run beside the controller (two 4B models do not fit in 4 GB).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from epitaph.body.cgroup import MIB, CgroupBody, CgroupError, CgroupSettings, WorkingSet
from epitaph.types import Cause, ModelSpec

if TYPE_CHECKING:
    from epitaph.config import Config

log = logging.getLogger(__name__)

UNIT_PREFIX = "epitaph-calibrate"
CALIBRATION_DIR = "calibration"
KILL_WITHIN_S = 10.0  # BUILD_PLAN 5.5: the kill must come within 10 s, 5 times out of 5
GIVE_UP_S = 30.0  # how long a trial waits for the kill before calling it a failure
FRACTION_STEP = 0.7  # after a bad trial, the fraction is multiplied by this
MIN_FRACTION = 0.2
CALIBRATE_PORT = 8092  # never the controller's port
TOUCH_PROMPT = "I am a small language model, and"
TOUCH_TOKENS = 8


def say_now(line: str) -> None:
    """Print a progress line at once (stdout is a pipe under systemd-run)."""
    print(line, flush=True)


@dataclass
class Trial:
    """One load and one death: the limit set and how the creature died."""

    limit_mb: int
    killed: bool
    kill_s: float | None
    oom_kill: bool
    ok: bool


@dataclass
class StepResult:
    """A ladder step's working set and death level."""

    step: int
    quant: str
    threads: int
    anon_mb: int
    file_mb: int
    current_mb: int
    # memory.peak: the cgroup's peak since it was created. The creature cgroup is kept, so after
    # the first step it is the maximum over the earlier steps too.
    cgroup_peak_mb: int | None
    death_fraction: float
    death_limit_mb: int
    trials_wanted: int
    trials: list[Trial] = field(default_factory=lambda: [])

    @property
    def good_in_a_row(self) -> int:
        """Good trials at the end of the list (the ones at the final fraction)."""
        n = 0
        for t in reversed(self.trials):
            if not t.ok:
                break
            n += 1
        return n

    @property
    def reliable(self) -> bool:
        """Whether the death level killed in time `trials_wanted` times in a row."""
        return self.good_in_a_row >= self.trials_wanted

    def to_json(self) -> dict[str, Any]:
        """The step as stored in the calibration file."""
        out = asdict(self)
        out["good_in_a_row"] = self.good_in_a_row
        out["reliable"] = self.reliable
        return out


class Creature(Protocol):
    """What calibration needs from the backend: load, touch the buffers, reap."""

    async def start(self, model: ModelSpec, quant: str, threads: int) -> None:
        """Load the model step in the creature cgroup; return once it serves."""
        ...

    async def touch(self) -> None:
        """Generate a few tokens so that every buffer is allocated."""
        ...

    async def stop(self) -> None:
        """Reap the process (it is usually dead already)."""
        ...


class ServerCreature:
    """The real creature: llama-server through the backend, as a life starts it."""

    def __init__(self, backend: Any) -> None:
        """`backend` is a LlamaServerBackend whose body is the calibration's CgroupBody."""
        self.backend = backend

    async def start(self, model: ModelSpec, quant: str, threads: int) -> None:
        """Spawn and wait for /health."""
        await self.backend.start(model, quant, threads)

    async def touch(self) -> None:
        """A raw completion of a few tokens."""
        from epitaph.types import Sampling

        async for _ in self.backend.complete(
            TOUCH_PROMPT, Sampling(temperature=0.7, min_p=0.05), TOUCH_TOKENS
        ):
            pass

    async def stop(self) -> None:
        """SIGKILL whatever is left; the dead are reaped."""
        with contextlib.suppress(Exception):
            await self.backend.stop(hard=True)


async def wait_dead(body: CgroupBody, timeout_s: float, poll_s: float = 0.02) -> bool:
    """Wait until the creature cgroup is empty; False after `timeout_s` seconds."""
    deadline = time.monotonic() + timeout_s
    while body.populated():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll_s)
    return True


async def calibrate_step(
    body: CgroupBody,
    creature: Creature,
    model: ModelSpec,
    step: int,
    threads: int,
    trials: int,
    fraction: float,
    kill_within_s: float = KILL_WITHIN_S,
    give_up_s: float = GIVE_UP_S,
    say: Callable[[str], object] = say_now,
    clock: Callable[[], float] = time.monotonic,
) -> StepResult:
    """Load, measure and kill one ladder step until `trials` good kills in a row.

    At most 2 x `trials` loads; a bad trial lowers the fraction (never below MIN_FRACTION).
    `clock` times the kill (seconds).
    """
    quant = model.quant(step)
    first: WorkingSet | None = None
    result: StepResult | None = None
    for i in range(max(1, 2 * trials)):
        body.reset_creature_cgroup()
        await creature.start(model, quant, threads)
        try:
            await creature.touch()
            ws = body.working_set()
            first = first or ws
            limit = max(4 * MIB, int(ws.anon * fraction))
            base = body.oom_kills()
            t0 = clock()
            body.squeeze_to_death(limit)
            killed = await wait_dead(body, give_up_s)
            kill_s = round(clock() - t0, 3) if killed else None
            oom = body.oom_kills() > base
            if not killed:
                body.kill_now(Cause.MANUAL)
                await wait_dead(body, 10.0)
        finally:
            await creature.stop()
        ok = killed and oom and kill_s is not None and kill_s <= kill_within_s
        if result is None:
            result = StepResult(
                step=step,
                quant=quant,
                threads=threads,
                anon_mb=first.anon // MIB,
                file_mb=first.file // MIB,
                current_mb=first.current // MIB,
                cgroup_peak_mb=None if first.peak is None else first.peak // MIB,
                death_fraction=fraction,
                death_limit_mb=limit // MIB,
                trials_wanted=trials,
            )
        result.trials.append(Trial(limit // MIB, killed, kill_s, oom, ok))
        result.death_fraction = round(fraction, 3)
        result.death_limit_mb = limit // MIB
        say(
            f"calibrate {model.name} {quant} trial {i + 1}: anon {ws.anon // MIB} MiB, "
            f"memory.max {limit // MIB} MiB, "
            + (f"killed in {kill_s:.2f} s" if killed else f"alive after {give_up_s:g} s")
            + f", oom_kill {'yes' if oom else 'no'}: {'ok' if ok else 'BAD'}"
        )
        if result.good_in_a_row >= trials:
            break
        if not ok:
            if fraction <= MIN_FRACTION:
                break
            fraction = max(MIN_FRACTION, fraction * FRACTION_STEP)
    body.reset_creature_cgroup()
    assert result is not None
    return result


def death_step(cfg: Config) -> int:
    """The ladder step in force at the profile's death (the step the squeeze meets)."""
    from epitaph.clock import Schedule

    sch = Schedule(cfg.profile)
    t = sch.death_s if sch.death_s is not None else sch.lifespan_s
    return sch.at(t).step


def calibration_path(state_dir: Path, hw_class: str, model: str) -> Path:
    """`<state_dir>/calibration/<class>-<model>.json`."""
    return state_dir / CALIBRATION_DIR / f"{hw_class}-{model}.json"


def calibration_record(
    cfg: Config, model: ModelSpec, steps: Sequence[StepResult], kill_within_s: float
) -> dict[str, Any]:
    """The calibration file's content."""
    return {
        "model": model.name,
        "hw_class": cfg.hw_class,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "death_mode": str(cfg.get("body.death_mode", "oom")),
        "load_mode": str(cfg.get("backend.load_mode", "auto")),
        "ctx": cfg.ctx,
        "kill_within_s": kill_within_s,
        "steps": [s.to_json() for s in steps],
    }


def load_calibration(dirs: Sequence[Path], hw_class: str) -> dict[tuple[str, str], int]:
    """Death levels in MiB by (model, quant), from the reliable steps of every calibration file.

    `dirs` in order of precedence (the state dir's own measurement before the repository's);
    an unreadable or malformed file is skipped with a warning.
    """
    out: dict[tuple[str, str], int] = {}
    for d in dirs:
        for f in sorted(d.glob(f"{hw_class}-*.json")) if d.is_dir() else []:
            try:
                data = json.loads(f.read_text())
                model = str(data["model"])
                for s in data["steps"]:
                    if s.get("reliable") and int(s["death_limit_mb"]) > 0:
                        out.setdefault((model, str(s["quant"])), int(s["death_limit_mb"]))
            except (OSError, ValueError, KeyError, TypeError) as e:
                log.warning("calibration file %s skipped: %s", f, e)
    return out


def calibration_dirs(cfg: Config) -> list[Path]:
    """Where the body looks for calibration files: the state dir, then bench/calibration."""
    from epitaph.config import REPO_ROOT

    return [cfg.state_dir / CALIBRATION_DIR, REPO_ROOT / "bench" / CALIBRATION_DIR]


# --- the command ----------------------------------------------------------------------------


def add_arguments(p: argparse.ArgumentParser) -> None:
    """The flags of `epitaph calibrate` (besides the common --profile/--hardware/--model)."""
    p.add_argument("--steps", help="ladder steps to measure, e.g. 0,1,2 (default: every step)")
    p.add_argument(
        "--trials",
        type=int,
        default=5,
        help="good kills in a row wanted at the death step (default 5); other steps get 1",
    )
    p.add_argument("--threads", type=int, default=2, help="generation threads (default 2)")
    p.add_argument("--out", help="directory for the result (default: <state_dir>/calibration)")
    p.add_argument("--inside", action="store_true", help="run in this process (a delegated unit)")
    p.add_argument("--user", help="service user for the relaunch (default: the current user)")


def parse_steps(text: str | None, ladder_len: int) -> list[int]:
    """`--steps` as a list of valid step numbers; every step when not given."""
    if not text:
        return list(range(ladder_len))
    steps = sorted({int(s) for s in text.split(",") if s.strip()})
    bad = [s for s in steps if not 0 <= s < ladder_len]
    if bad:
        raise ValueError(f"no ladder step {bad} (the ladder has {ladder_len})")
    return steps


async def calibrate(
    cfg: Config,
    args: argparse.Namespace,
    body: CgroupBody,
    creature: Creature,
    say: Callable[[str], object] = say_now,
) -> tuple[dict[str, Any], bool]:
    """Every requested step of the configured model: the calibration record and whether every
    step met its trials."""
    model = cfg.model()
    fraction = float(cfg.get("body.death_fraction", 0.5))
    final = death_step(cfg)
    results: list[StepResult] = []
    for step in parse_steps(args.steps, len(model.ladder)):
        wanted = args.trials if step == final else 1
        results.append(
            await calibrate_step(
                body, creature, model, step, args.threads, wanted, fraction, say=say
            )
        )
    return calibration_record(cfg, model, results, KILL_WITHIN_S), all(r.reliable for r in results)


def run_inside(
    cfg: Config, args: argparse.Namespace, say: Callable[[str], object] | None = None
) -> int:
    """Calibrate in this process (a Delegate=yes unit); 0 if every step is reliable."""
    say = say or say_now
    from epitaph.backend.llama_server import LlamaServerBackend, ServerSettings
    from epitaph.state import AlreadyRunning, InstanceLock, atomic_write_json

    if str(cfg.get("body.death_mode", "oom")) != "oom":
        say("calibrate: death_mode is not oom; nothing to calibrate")
        return 1
    lock = InstanceLock(cfg.state_dir)
    try:
        lock.acquire()
    except AlreadyRunning as e:
        say(f"calibrate: {e}. Stop the controller first (systemctl stop epitaph-controller).")
        return 1
    body: CgroupBody | None = None
    try:
        try:
            body = CgroupBody.delegated(CgroupSettings.from_config(cfg))
        except (CgroupError, OSError) as e:
            say(f"calibrate: no delegated cgroup: {e}")
            return 1
        if body.death_mode != "oom":
            say("calibrate: the memory controller is not delegated; nothing to calibrate")
            return 1
        settings = ServerSettings.from_config(cfg)
        settings.port = CALIBRATE_PORT
        settings.reload_handover = "reread"
        backend = LlamaServerBackend(settings, body)
        record, ok = asyncio.run(calibrate(cfg, args, body, ServerCreature(backend), say))
        out_dir = Path(args.out) if args.out else cfg.state_dir / CALIBRATION_DIR
        path = out_dir / f"{cfg.hw_class}-{record['model']}.json"
        atomic_write_json(path, record)
        say(f"calibrate: {'reliable' if ok else 'NOT reliable'}; wrote {path}")
        return 0 if ok else 1
    finally:
        if body is not None:
            body.release_network()
        lock.release()


def run(args: argparse.Namespace, cfg: Config) -> int:
    """`epitaph calibrate`: relaunch in a delegated unit unless already inside one."""
    from epitaph.body import selftest

    if not (args.inside or selftest.in_epitaph_unit()):
        extra: list[str] = []
        for flag in ("model", "steps", "out"):
            value = getattr(args, flag, None)
            if value:
                extra += [f"--{flag}", str(value)]
        extra += ["--trials", str(args.trials), "--threads", str(args.threads)]
        return selftest.relaunch(args, command="calibrate", extra=extra, unit_prefix=UNIT_PREFIX)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # not every /health poll
    return run_inside(cfg, args)

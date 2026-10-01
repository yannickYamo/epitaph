#!/usr/bin/env python3
"""The soak report: section 11.4 of the build plan, as Markdown ready for docs (card E7, gate G3).

  tools/soak_report.py DIR [DIR ...] [--journal FILE] [--status FILE] [--samples FILE ...]
                       [--first N] [--last N] [--min-hours 25] [--out FILE]

DIR is what `tools/collect_lives.sh` writes (a folder with `lives/NNNNNN/`), a `lives/` folder
itself, or one life folder; several are merged (a life found twice keeps the copy that has a
verify.json). The other inputs are optional, and a criterion without its input reads "no data":

  --journal   `journalctl -u epitaph-controller -o short-iso` over the soak (kernel lines from
              `journalctl -k` may be appended: under-voltage warnings are counted)
  --status    the controller's `status.json` (or `epitaph ctl status` output) at the end
  --samples   what `tools/soak_sample.sh` appended every 10 minutes (RSS, temperature,
              throttled bits, disk), one file or several
  --first/--last   the soak's life numbers (default: every life found)

Criteria (BUILD_PLAN 11, items 2 and 4):
  - duration at least --min-hours (25), from the first life's load to the last record;
  - every finished life passes `verify-life` (its verify.json, written by collect_lives.sh);
  - no missed life: every gap from `death_shown` to the next `birth` is within the silence +
    the next life's measured load (`birth_loading` to `birth`) + 5 minutes, as verify-life's
    `next_birth` check; a life number missing from the folders fails it (no evidence);
  - zero controller crashes: no unit failure (abnormal exit, watchdog kill) and no automatic
    restart in the journal or the samples' NRestarts; a new controller pid in the samples or
    a life closed `interrupted` fails it too unless a deliberate `Stopping` line explains it;
  - controller memory growth under 20 MB: median RSS of the last 3 samples minus the first 3,
    within the longest run of one controller pid;
  - disk growth under 100 MB a day: the root filesystem's used space (lives and journal), the
    state directory reported beside it;
  - no under-voltage bit in any sample (`get_throttled` bit 0 or 16) or the kernel log;
  - throttling or thermal pauses under 10% of the time: samples with a throttling bit set now
    (1, 2, 3) plus the controller's `thermal` pauses, over the soak.

The life count is reported, not required. Exit 0 when every criterion passes, 1 when one fails
or has no data, 2 on a usage error. Read-only: it never writes into the folders it reads.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import statistics
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MARGIN_S = 300.0  # BUILD_PLAN 10.3 / 11.4: silence + measured load + 5 min
DEFAULT_SILENCE_S = 90.0
MIN_HOURS = 25.0
MAX_RSS_GROWTH_MB = 20.0
MAX_DISK_MB_PER_DAY = 100.0
MAX_THROTTLED_SHARE = 0.10

# `vcgencmd get_throttled` bits (Raspberry Pi firmware).
UV_NOW, CAPPED_NOW, THROTTLED_NOW, SOFT_TEMP_NOW = 0x1, 0x2, 0x4, 0x8
UV_OCCURRED = 0x10000
THROTTLING_NOW = CAPPED_NOW | THROTTLED_NOW | SOFT_TEMP_NOW

PASS, FAIL, NO_DATA = "PASS", "FAIL", "no data"


# ---------------------------------------------------------------------------------------
# lives


@dataclass
class LifeRec:
    """What the report needs from one recorded life."""

    n: int
    path: Path
    profile: str = ""
    model: str = ""
    cause: str = ""
    lived_s: float | None = None
    loading_ts: float | None = None
    birth_ts: float | None = None
    shown_ts: float | None = None
    last_ts: float | None = None
    silence_s: float | None = None
    thermal_pause_s: float = 0.0
    finished: bool = False
    verify: dict[str, Any] | None = None

    @property
    def load_s(self) -> float:
        """Measured load time: `birth_loading` to `birth` (0 when either is missing)."""
        if self.loading_ts is None or self.birth_ts is None:
            return 0.0
        return max(0.0, self.birth_ts - self.loading_ts)

    @property
    def start_ts(self) -> float | None:
        """When this life began loading (or was born, if the load was not recorded)."""
        return self.loading_ts if self.loading_ts is not None else self.birth_ts


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def read_life(folder: Path) -> LifeRec:
    """One life folder: events.jsonl, and death.json, meta.json, verify.json when present."""
    n = int(folder.name)
    rec = LifeRec(n, folder)
    meta = _read_json(folder / "meta.json") or {}
    rec.profile = str(meta.get("profile", ""))
    rec.model = str(meta.get("model", ""))
    events = folder / "events.jsonl"
    lines = events.read_text(errors="replace").splitlines() if events.is_file() else []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue  # a torn last line of a life in progress
        if not isinstance(e, dict) or int(e.get("life", n)) != n:
            continue
        ts = e.get("ts")
        if isinstance(ts, int | float):
            rec.last_ts = float(ts) if rec.last_ts is None else max(rec.last_ts, float(ts))
        kind = e.get("type")
        if kind == "birth_loading" and rec.loading_ts is None:
            rec.loading_ts = _f(ts)
            rec.profile = rec.profile or str(e.get("profile", ""))
            rec.model = rec.model or str(e.get("model", ""))
        elif kind == "birth" and rec.birth_ts is None:
            rec.birth_ts = _f(ts)
        elif kind == "death":
            rec.cause = str(e.get("cause", rec.cause))
            rec.lived_s = _f(e.get("lived_s"))
        elif kind == "death_shown":
            rec.shown_ts = _f(ts)
        elif kind == "silence":
            rec.silence_s = _f(e.get("seconds"))
        elif kind == "thermal":
            rec.thermal_pause_s += _f(e.get("pause_s")) or 0.0
    death = _read_json(folder / "death.json")
    if death is not None:
        rec.finished = True
        rec.cause = str(death.get("cause", rec.cause))
        if rec.lived_s is None:
            rec.lived_s = _f(death.get("lived_s"))
        closed = _f(death.get("closed_ts"))
        if closed is not None:
            rec.last_ts = closed if rec.last_ts is None else max(rec.last_ts, closed)
    rec.verify = _read_json(folder / "verify.json")
    return rec


def _f(x: object) -> float | None:
    return float(x) if isinstance(x, int | float) and not isinstance(x, bool) else None


def life_folders(root: Path) -> list[Path]:
    """The life folders under a collect_lives.sh output, a lives/ folder, or one life."""
    if root.name.isdigit() and (root / "events.jsonl").is_file():
        return [root]
    base = root / "lives" if (root / "lives").is_dir() else root
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.name.isdigit() and (p / "events.jsonl").is_file())


def load_lives(roots: list[Path]) -> dict[int, LifeRec]:
    """Every life under the roots; a life found twice keeps the better-judged, longer copy."""
    out: dict[int, LifeRec] = {}
    for root in roots:
        for folder in life_folders(root):
            rec = read_life(folder)
            old = out.get(rec.n)
            if old is None or _rank(rec) > _rank(old):
                out[rec.n] = rec
    return out


def _rank(r: LifeRec) -> tuple[int, int, float]:
    return (r.verify is not None, r.finished, r.last_ts or 0.0)


# ---------------------------------------------------------------------------------------
# the no-missed-life rule


@dataclass
class Gap:
    """From one life's `death_shown` to the next life's `birth`."""

    life: int
    status: str  # PASS | FAIL | pending
    gap_s: float | None
    limit_s: float | None
    detail: str = ""


def gaps(
    lives: list[LifeRec], margin_s: float = MARGIN_S, silence_s: float | None = None
) -> list[Gap]:
    """The rule over consecutive life numbers; the last life's gap is pending."""
    by_n = {r.n: r for r in lives}
    out: list[Gap] = []
    if not lives:
        return out
    last_n = max(by_n)
    for r in sorted(lives, key=lambda x: x.n):
        silence = silence_s if silence_s is not None else (r.silence_s or DEFAULT_SILENCE_S)
        nxt = by_n.get(r.n + 1)
        if r.n == last_n:
            out.append(Gap(r.n, "pending", None, None, "the last life in the soak"))
            continue
        if nxt is None:
            out.append(Gap(r.n, FAIL, None, None, f"life {r.n + 1} is not in the folders"))
            continue
        if r.shown_ts is None:
            cause = r.cause or "no death"
            out.append(Gap(r.n, FAIL, None, None, f"no death_shown (cause {cause})"))
            continue
        if nxt.birth_ts is None:
            if nxt.n == last_n and not nxt.finished:
                out.append(Gap(r.n, "pending", None, None, f"life {nxt.n} is still loading"))
            else:
                out.append(Gap(r.n, FAIL, None, None, f"life {nxt.n} has no birth"))
            continue
        limit = silence + nxt.load_s + margin_s
        gap = nxt.birth_ts - r.shown_ts
        detail = f"silence {silence:.0f} s + load {nxt.load_s:.0f} s + {margin_s:.0f} s"
        out.append(Gap(r.n, PASS if gap <= limit else FAIL, gap, limit, detail))
    return out


# ---------------------------------------------------------------------------------------
# the journal


_STARTED = re.compile(r"Started epitaph-controller")
_STOPPING = re.compile(r"Stopping epitaph-controller")
_MAIN_EXIT = re.compile(
    r"epitaph-controller\.service: Main process exited, code=(\w+), status=(\S+)"
)
_WATCHDOG = re.compile(r"epitaph-controller\.service: Watchdog timeout")
_RESTART = re.compile(r"epitaph-controller\.service: Scheduled restart job")
_FAILED = re.compile(r"epitaph-controller\.service: Failed with result '([^']+)'")
_OOM = re.compile(r"epitaph-controller\.service: A process of this unit has been killed by the OOM")
_UNDERVOLT = re.compile(r"under-?voltage detected", re.IGNORECASE)
_TRACEBACK = re.compile(r"Traceback \(most recent call last\)")


@dataclass
class Journal:
    """What the controller's journal says about the soak."""

    lines: int = 0
    starts: int = 0
    deliberate_stops: int = 0
    failures: int = 0  # the unit failed (a crash or a watchdog kill) outside a deliberate stop
    evidence: list[str] = field(default_factory=lambda: [])
    restarts: int = 0
    oom_kills: int = 0  # the creature's deaths: it lives in the unit's cgroup subtree
    undervoltage: int = 0
    tracebacks: int = 0


def parse_journal(text: str) -> Journal:
    """Count starts, deliberate stops, failures and restarts in `journalctl -u` output.

    A failure is a `Failed with result` line, or, in an excerpt without one, a main-process
    exit other than a clean one (`code=exited, status=0/SUCCESS`); either counts only when
    no `Stopping` line announced it. Watchdog timeouts are kept as evidence. The OOM kills
    are the creature's RAM deaths (it runs in the unit's delegated subtree), never the
    controller's.
    """
    j = Journal()
    stopping = False
    exits = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        j.lines += 1
        if _STARTED.search(line):
            j.starts += 1
            stopping = False
        elif _STOPPING.search(line):
            j.deliberate_stops += 1
            stopping = True
        elif m := _MAIN_EXIT.search(line):
            clean = m.group(1) == "exited" and m.group(2).startswith("0")
            if not clean and not stopping:
                exits += 1
                j.evidence.append(line.strip())
        elif _WATCHDOG.search(line):
            j.evidence.append(line.strip())
        elif _FAILED.search(line):
            if not stopping:
                j.failures += 1
                j.evidence.append(line.strip())
        elif _RESTART.search(line):
            j.restarts += 1
        elif _OOM.search(line):
            j.oom_kills += 1
        if _UNDERVOLT.search(line):
            j.undervoltage += 1
        if _TRACEBACK.search(line):
            j.tracebacks += 1
    j.failures = max(j.failures, exits)
    return j


def read_kv(text: str) -> dict[str, str]:
    """`systemctl show` output (KEY=value lines), as collect_lives.sh saves in controller.txt."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


# ---------------------------------------------------------------------------------------
# machine samples (tools/soak_sample.sh)

SAMPLE_FIELDS = (
    "epoch",
    "pid",
    "rss_kb",
    "temp_c",
    "throttled",
    "state_kb",
    "root_used_kb",
    "nrestarts",
)


@dataclass
class Sample:
    """One line of soak_sample.sh: None where the Pi could not say."""

    epoch: float
    pid: int | None
    rss_kb: float | None
    temp_c: float | None
    throttled: int | None
    state_kb: float | None
    root_used_kb: float | None
    nrestarts: int | None


def parse_samples(text: str) -> list[Sample]:
    """Tab-separated lines; a `#` header names the columns (else SAMPLE_FIELDS)."""
    names = list(SAMPLE_FIELDS)
    out: list[Sample] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("#"):
            names = line.lstrip("#").split()
            continue
        row = dict(zip(names, line.split("\t") if "\t" in line else line.split(), strict=False))
        epoch = _num(row.get("epoch"))
        if epoch is None:
            continue
        thr = row.get("throttled", "")
        out.append(
            Sample(
                epoch=epoch,
                pid=_int(row.get("pid")),
                rss_kb=_num(row.get("rss_kb")),
                temp_c=_num(row.get("temp_c")),
                throttled=_hex(thr),
                state_kb=_num(row.get("state_kb")),
                root_used_kb=_num(row.get("root_used_kb")),
                nrestarts=_int(row.get("nrestarts")),
            )
        )
    return sorted(out, key=lambda s: s.epoch)


def _num(x: str | None) -> float | None:
    try:
        return float(x) if x not in (None, "", "-") else None
    except ValueError:
        return None


def _int(x: str | None) -> int | None:
    v = _num(x)
    return int(v) if v is not None else None


def _hex(x: str) -> int | None:
    x = x.strip().removeprefix("throttled=")
    try:
        return int(x, 16) if x.lower().startswith("0x") else int(x) if x not in ("", "-") else None
    except ValueError:
        return None


def pid_runs(samples: list[Sample]) -> list[list[Sample]]:
    """Consecutive samples with the same controller pid (samples without a pid are dropped)."""
    runs: list[list[Sample]] = []
    for s in samples:
        if s.pid is None or s.pid == 0:
            continue
        if runs and runs[-1][-1].pid == s.pid:
            runs[-1].append(s)
        else:
            runs.append([s])
    return runs


def rss_growth_mb(samples: list[Sample]) -> tuple[float, int, int] | None:
    """(growth in MB, pid, samples) over the longest pid run: median of the last 3 RSS minus
    the median of the first 3. None without two RSS samples."""
    runs = [[s for s in r if s.rss_kb is not None] for r in pid_runs(samples)]
    runs = [r for r in runs if len(r) >= 2]
    if not runs:
        return None
    run = max(runs, key=len)
    k = min(3, len(run) // 2) or 1
    first = statistics.median(s.rss_kb or 0.0 for s in run[:k])
    last = statistics.median(s.rss_kb or 0.0 for s in run[-k:])
    return (last - first) / 1024, int(run[0].pid or 0), len(run)


def disk_rate_mb_per_day(samples: list[Sample], attr: str) -> float | None:
    """Growth of `attr` (KB) from the first to the last sample that has it, in MB a day."""
    pts = [(s.epoch, getattr(s, attr)) for s in samples if getattr(s, attr) is not None]
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < 3600:
        return None
    days = (pts[-1][0] - pts[0][0]) / 86400
    return (pts[-1][1] - pts[0][1]) / 1024 / days


def sample_gaps(samples: list[Sample]) -> list[tuple[float, float]]:
    """Holes in the sampling: intervals over 2.5 times the median interval."""
    if len(samples) < 3:
        return []
    steps = [b.epoch - a.epoch for a, b in itertools.pairwise(samples)]
    med = statistics.median(steps)
    return [
        (a.epoch, b.epoch - a.epoch)
        for a, b in itertools.pairwise(samples)
        if med > 0 and b.epoch - a.epoch > 2.5 * med
    ]


# ---------------------------------------------------------------------------------------
# the report


@dataclass
class Criterion:
    """One row of the summary table."""

    name: str
    result: str
    measured: str


@dataclass
class Inputs:
    """Everything the report reads."""

    lives: dict[int, LifeRec]
    journal: Journal | None = None
    status: dict[str, Any] | None = None
    controller: dict[str, str] = field(default_factory=lambda: {})
    samples: list[Sample] = field(default_factory=lambda: [])


def _utc(ts: float | None) -> str:
    if ts is None:
        return "?"
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


def _dur(s: float | None) -> str:
    if s is None:
        return ""
    m, sec = divmod(round(s), 60)
    return f"{m}:{sec:02d}"


def _verdict(r: LifeRec) -> str:
    if r.verify is None:
        return "in progress" if not r.finished else "not verified"
    if r.verify.get("ok"):
        return "PASS"
    return "FAIL"


def evaluate(
    inp: Inputs,
    first: int | None = None,
    last: int | None = None,
    min_hours: float = MIN_HOURS,
    margin_s: float = MARGIN_S,
) -> tuple[list[Criterion], list[LifeRec], list[Gap], dict[str, Any]]:
    """Judge the soak: the criteria, the lives in the window, their gaps and the figures."""
    lives = [
        r
        for n, r in sorted(inp.lives.items())
        if (first is None or n >= first) and (last is None or n <= last)
    ]
    crit: list[Criterion] = []
    facts: dict[str, Any] = {}

    # duration
    start = min((r.start_ts for r in lives if r.start_ts is not None), default=None)
    ends = [r.last_ts for r in lives if r.last_ts is not None]
    ends += [s.epoch for s in inp.samples if start is not None and s.epoch >= start]
    end = max(ends, default=None)
    span = end - start if start is not None and end is not None else None
    facts.update(start=start, end=end, span_s=span)
    if span is None:
        crit.append(Criterion(f"Soak of at least {min_hours:g} hours", NO_DATA, "no lives"))
    else:
        crit.append(
            Criterion(
                f"Soak of at least {min_hours:g} hours",
                PASS if span >= min_hours * 3600 else FAIL,
                f"{span / 3600:.1f} h ({_utc(start)} to {_utc(end)})",
            )
        )

    # every life passes verify-life
    finished = [r for r in lives if r.finished]
    passed = [r for r in finished if r.verify is not None and r.verify.get("ok")]
    unjudged = [r for r in finished if r.verify is None]
    bad = [r for r in finished if r.verify is not None and not r.verify.get("ok")]
    if not finished:
        crit.append(Criterion("Every life passes `verify-life`", NO_DATA, "no finished life"))
    else:
        parts = [f"{len(passed)} of {len(finished)} finished lives pass"]
        if bad:
            parts.append("fail: " + ", ".join(f"{r.n:06d}" for r in bad))
        if unjudged:
            parts.append("not verified: " + ", ".join(f"{r.n:06d}" for r in unjudged))
        ok = len(passed) == len(finished)
        crit.append(
            Criterion("Every life passes `verify-life`", PASS if ok else FAIL, "; ".join(parts))
        )

    # no missed life
    gs = gaps(lives, margin_s)
    judged = [g for g in gs if g.status in (PASS, FAIL)]
    if not judged:
        crit.append(Criterion("No missed life", NO_DATA, "fewer than two consecutive lives"))
    else:
        failed = [g for g in judged if g.status == FAIL]
        worst = max(
            (g for g in judged if g.gap_s is not None), key=lambda g: g.gap_s or 0.0, default=None
        )
        measured = f"{len(judged) - len(failed)} of {len(judged)} gaps within the limit"
        if worst is not None and worst.gap_s is not None and worst.limit_s is not None:
            measured += f"; longest {worst.gap_s:.0f} s after life {worst.life:06d} (limit {worst.limit_s:.0f} s)"
        if failed:
            measured += "; fail after " + ", ".join(f"{g.life:06d}" for g in failed)
        crit.append(Criterion("No missed life", FAIL if failed else PASS, measured))

    # controller crashes: failures and automatic restarts; a pid change or an interrupted
    # life that no deliberate stop explains counts too
    interrupted = [r for r in lives if r.cause == "interrupted"]
    runs = pid_runs(inp.samples)
    counts = [s.nrestarts for s in inp.samples if s.nrestarts is not None]
    j = inp.journal
    deliberate = j.deliberate_stops if j is not None else 0
    failures = j.failures if j is not None else 0
    restarts = max(j.restarts if j is not None else 0, (max(counts) - min(counts)) if counts else 0)
    pid_changes = max(0, len(runs) - 1)
    evidence: list[str] = []
    if j is not None:
        evidence.append(
            f"journal: {failures} failure(s), {j.restarts} automatic restart(s), "
            f"{deliberate} deliberate stop(s)"
        )
    if counts:
        evidence.append(f"NRestarts {min(counts)} to {max(counts)} in the samples")
    elif inp.controller.get("NRestarts"):
        evidence.append(
            f"NRestarts {inp.controller['NRestarts']} at collection (since the unit last started)"
        )
    if runs:
        evidence.append(f"controller pids: {len(runs)} over {sum(len(r) for r in runs)} samples")
    evidence.append(f"lives closed `interrupted`: {len(interrupted)}")
    unexplained = max(pid_changes, len(interrupted)) - deliberate
    have = j is not None or bool(runs)
    if not have and not interrupted:
        crit.append(
            Criterion(
                "Zero controller crashes",
                NO_DATA,
                "; ".join(evidence[:-1]) or "no journal or samples",
            )
        )
    else:
        ok = failures == 0 and restarts == 0 and unexplained <= 0
        crit.append(Criterion("Zero controller crashes", PASS if ok else FAIL, "; ".join(evidence)))
    facts["deliberate_stops"] = deliberate

    # memory growth
    growth = rss_growth_mb(inp.samples)
    if growth is None:
        crit.append(
            Criterion(
                f"Controller memory growth under {MAX_RSS_GROWTH_MB:g} MB",
                NO_DATA,
                "no RSS samples",
            )
        )
    else:
        mb, pid, n = growth
        peak = max((s.rss_kb or 0.0) for s in inp.samples if s.pid == pid) / 1024
        facts["rss_peak_mb"] = peak
        crit.append(
            Criterion(
                f"Controller memory growth under {MAX_RSS_GROWTH_MB:g} MB",
                PASS if mb < MAX_RSS_GROWTH_MB else FAIL,
                f"{mb:+.1f} MB (pid {pid}, {n} samples; peak {peak:.0f} MB)",
            )
        )

    # disk growth
    root_rate = disk_rate_mb_per_day(inp.samples, "root_used_kb")
    state_rate = disk_rate_mb_per_day(inp.samples, "state_kb")
    rate = root_rate if root_rate is not None else state_rate
    if rate is None:
        crit.append(
            Criterion(
                f"Disk growth under {MAX_DISK_MB_PER_DAY:g} MB a day",
                NO_DATA,
                "no disk samples over an hour",
            )
        )
    else:
        parts = []
        if root_rate is not None:
            parts.append(f"root filesystem {root_rate:.0f} MB/day")
        if state_rate is not None:
            parts.append(f"state directory {state_rate:.0f} MB/day")
        crit.append(
            Criterion(
                f"Disk growth under {MAX_DISK_MB_PER_DAY:g} MB a day",
                PASS if rate < MAX_DISK_MB_PER_DAY else FAIL,
                "; ".join(parts),
            )
        )

    # under-voltage
    thr = [s for s in inp.samples if s.throttled is not None]
    uv = [s for s in thr if s.throttled is not None and s.throttled & (UV_NOW | UV_OCCURRED)]
    kernel_uv = inp.journal.undervoltage if inp.journal is not None else 0
    if not thr and inp.journal is None:
        crit.append(Criterion("No under-voltage bits", NO_DATA, "no throttled samples"))
    else:
        parts = []
        if thr:
            seen = sorted({f"0x{s.throttled:x}" for s in thr if s.throttled is not None})
            parts.append(
                f"{len(uv)} of {len(thr)} samples with an under-voltage bit (values {', '.join(seen)})"
            )
        if inp.journal is not None:
            parts.append(f"{kernel_uv} under-voltage line(s) in the log")
        # A pass needs the firmware's bits; a log alone can only fail it.
        bad_power = bool(uv) or kernel_uv > 0
        crit.append(
            Criterion(
                "No under-voltage bits",
                FAIL if bad_power else PASS if thr else NO_DATA,
                "; ".join(parts),
            )
        )

    # throttling time
    pause_s = sum(r.thermal_pause_s for r in lives)
    if not thr:
        crit.append(
            Criterion(
                f"Throttling or thermal pauses under {MAX_THROTTLED_SHARE:.0%} of the time",
                NO_DATA,
                f"no throttled samples; thermal pauses {pause_s:.0f} s",
            )
        )
    else:
        throttled_share = sum(1 for s in thr if (s.throttled or 0) & THROTTLING_NOW) / len(thr)
        pause_share = pause_s / span if span else 0.0
        share = throttled_share + pause_share
        crit.append(
            Criterion(
                f"Throttling or thermal pauses under {MAX_THROTTLED_SHARE:.0%} of the time",
                PASS if share < MAX_THROTTLED_SHARE else FAIL,
                f"{share:.1%} (throttling in {throttled_share:.1%} of samples; thermal pauses {pause_s:.0f} s)",
            )
        )
    temps = [s.temp_c for s in inp.samples if s.temp_c is not None]
    if temps:
        facts["temp"] = (min(temps), statistics.median(temps), max(temps))
    return crit, lives, gs, facts


def render(
    inp: Inputs,
    crit: list[Criterion],
    lives: list[LifeRec],
    gs: list[Gap],
    facts: dict[str, Any],
    title: str,
) -> str:
    """The report as Markdown."""
    out: list[str] = [f"# {title}", ""]
    overall = PASS if all(c.result == PASS for c in crit) else FAIL
    finished = [r for r in lives if r.finished]
    span = facts.get("span_s")
    lede = f"**{overall}.** {len(finished)} finished lives"
    if lives:
        lede += f" (lives {lives[0].n:06d} to {lives[-1].n:06d})"
    if span:
        lede += f" over {span / 3600:.1f} hours"
    out += [lede + ". The life count is reported, not required (BUILD_PLAN 11.4).", ""]
    out += ["| Criterion (BUILD_PLAN 11) | Result | Measured |", "|---|---|---|"]
    out += [f"| {c.name} | {c.result} | {c.measured} |" for c in crit]
    out.append("")

    causes: dict[str, int] = {}
    for r in finished:
        causes[r.cause or "?"] = causes.get(r.cause or "?", 0) + 1
    if causes:
        out += [
            "Causes of death: " + ", ".join(f"{k} {v}" for k, v in sorted(causes.items())) + ".",
            "",
        ]

    out += ["## Lives", ""]
    out += [
        "| Life | Profile | Cause | Lived | verify-life | Failed | Advisory | Gap to next birth | Limit |"
    ]
    out += ["|---|---|---|---|---|---|---|---|---|"]
    by_life = {g.life: g for g in gs}
    for r in lives:
        v = r.verify or {}
        g = by_life.get(r.n)
        gap = ""
        if g is not None:
            gap = f"{g.gap_s:.0f} s" if g.gap_s is not None else g.detail
            if g.status == FAIL:
                gap += " (FAIL)"
        limit = f"{g.limit_s:.0f} s" if g is not None and g.limit_s is not None else ""
        out.append(
            f"| {r.n:06d} | {r.profile} | {r.cause or ''} | {_dur(r.lived_s)} | {_verdict(r)} | "
            f"{', '.join(v.get('failed', []))} | {', '.join(v.get('advisory', []))} | {gap} | {limit} |"
        )
    out.append("")

    out += ["## Controller", ""]
    j = inp.journal
    if j is not None:
        out.append(
            f"Journal: {j.lines} lines; {j.starts} start(s), {j.deliberate_stops} deliberate stop(s), "
            f"{j.restarts} automatic restart(s), {j.failures} failure(s), {j.tracebacks} traceback(s); "
            f"{j.oom_kills} OOM kill(s) in the unit's subtree (the creature's RAM deaths)."
        )
        out += [f"- `{c}`" for c in j.evidence[:20]]
    else:
        out.append("Journal: not provided.")
    if inp.controller:
        out.append("")
        out.append(
            "systemd: " + "; ".join(f"{k}={v}" for k, v in sorted(inp.controller.items())) + "."
        )
    if inp.status is not None:
        st = inp.status
        out.append("")
        out.append(
            f"Status at the end: state {st.get('state', '?')}, pid {st.get('pid', '?')}, "
            f"{st.get('lives_run', '?')} lives run since the controller started"
            + (f", life {st['life']} at t={st.get('t')} s" if st.get("life") is not None else "")
            + "."
        )
    out.append("")

    out += ["## Machine", ""]
    if inp.samples:
        s0, s1 = inp.samples[0], inp.samples[-1]
        out.append(f"{len(inp.samples)} samples from {_utc(s0.epoch)} to {_utc(s1.epoch)}.")
        if "temp" in facts:
            lo, med, hi = facts["temp"]
            out.append(f"CPU temperature {lo:.1f} to {hi:.1f} °C (median {med:.1f}).")
        for attr, label in (
            ("root_used_kb", "Root filesystem used"),
            ("state_kb", "State directory"),
        ):
            pts = [getattr(s, attr) for s in inp.samples if getattr(s, attr) is not None]
            if pts:
                out.append(f"{label}: {pts[0] / 1024:.0f} MB to {pts[-1] / 1024:.0f} MB.")
        holes = sample_gaps(inp.samples)
        if holes:
            out.append(
                f"{len(holes)} hole(s) in the sampling (the laptop could not reach the Pi): "
                + ", ".join(f"{_utc(t)} for {d / 60:.0f} min" for t, d in holes[:10])
                + "."
            )
    else:
        out.append("No machine samples (`tools/soak_sample.sh`).")
    out.append("")
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    """The command line."""
    p = argparse.ArgumentParser(prog="soak_report.py", description=__doc__.splitlines()[0])
    p.add_argument(
        "dirs",
        nargs="+",
        type=Path,
        help="collect_lives.sh outputs, lives/ folders or life folders",
    )
    p.add_argument(
        "--journal",
        type=Path,
        action="append",
        default=[],
        help="journalctl -u epitaph-controller output",
    )
    p.add_argument("--status", type=Path, help="status.json or `epitaph ctl status` output")
    p.add_argument(
        "--samples", type=Path, action="append", default=[], help="soak_sample.sh output"
    )
    p.add_argument("--first", type=int, help="first life of the soak")
    p.add_argument("--last", type=int, help="last life of the soak")
    p.add_argument("--min-hours", type=float, default=MIN_HOURS)
    p.add_argument("--title", default="Soak report")
    p.add_argument("--out", type=Path, help="write the Markdown here (default: stdout)")
    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point: 0 every criterion passes, 1 one fails or has no data, 2 usage."""
    args = build_parser().parse_args(argv)
    for path in [*args.dirs, *args.journal, *args.samples, *([args.status] if args.status else [])]:
        if not path.exists():
            print(f"soak_report: {path} does not exist", file=sys.stderr)
            return 2
    lives = load_lives(list(args.dirs))
    if not lives:
        print("soak_report: no life folders found", file=sys.stderr)
        return 2
    journal = (
        parse_journal("\n".join(p.read_text(errors="replace") for p in args.journal))
        if args.journal
        else None
    )
    controller: dict[str, str] = {}
    for d in args.dirs:
        f = d / "controller.txt"
        if f.is_file():
            controller.update(read_kv(f.read_text()))
    samples = parse_samples("\n".join(p.read_text(errors="replace") for p in args.samples))
    status = _read_json(args.status) if args.status else None
    inp = Inputs(lives, journal, status, controller, samples)
    crit, window, gs, facts = evaluate(inp, args.first, args.last, args.min_hours)
    text = render(inp, crit, window, gs, facts, args.title)
    if args.out:
        args.out.write_text(text + "\n")
    else:
        print(text)
    return 0 if all(c.result == PASS for c in crit) else 1


if __name__ == "__main__":
    raise SystemExit(main())

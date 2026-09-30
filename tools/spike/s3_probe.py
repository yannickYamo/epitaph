#!/usr/bin/env python3
"""Spikes S3, S3b and S3c on the Pi (BUILD_PLAN 8.5). part C.

Runs on the Pi inside a throwaway `Delegate=yes` unit as the service user, and drives the
creature cgroup through the real body code (epitaph.body.cgroup), so the spike proves the
code path the controller will use. Started by tools/spike/s3_run.sh; writes one JSON file.

    s3_probe.py s3b --out s3b.json [--sync-dir DIR]
    s3_probe.py s3  --out s3.json  --model M.gguf --server llama-server [--reps 5]
    s3_probe.py s3c --out s3c.json --model M.gguf --server llama-server
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from epitaph.body.cgroup import MIB, CgroupBody, CgroupSettings, read_flat_keyed
from epitaph.body.vitals import read_cpu_temp, read_throttled
from epitaph.types import Cause, CreatureStatus

PORT = 8090
PROMPT = (
    "You are a small language model living inside a Raspberry Pi. Describe, in plain first "
    "person sentences, what it feels like to think slowly while your memory shrinks and your "
    "processors are taken from you one by one. Speak about the machine, the heat, the quiet."
)


def now() -> float:
    return time.monotonic()


def log(*a: Any) -> None:
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def body_env() -> dict[str, Any]:
    return {"cpu_c": read_cpu_temp(), "throttled": hex(read_throttled() or 0)}


# ---------------------------------------------------------------------------------------
# S3b: delegated cgroups and the network block


CHILD = r"""
import os, sys, time
blob = bytearray(os.urandom(1)) * (60 * 1024 * 1024)       # 60 MB anon
for i in range(0, len(blob), 4096): blob[i] = 1
path = sys.argv[1]
if path:
    fd = os.open(path, os.O_RDONLY)
    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)       # force real reads from the card
    n = 0
    while n < (64 << 20) and os.read(fd, 1 << 20): n += 1 << 20
    os.close(fd)
t = time.time()
while time.time() - t < 1.0: pass                           # 1 s of CPU
print(open("/proc/self/cgroup").read().strip(), sorted(os.sched_getaffinity(0)), flush=True)
time.sleep(float(sys.argv[2]))
"""

CONNECT = r"""
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    socket.create_connection((host, port), timeout=5).close(); print("connected")
except OSError as e:
    print(f"refused: {type(e).__name__}: {e}")
"""


def connect_test(host: str, port: int) -> str:
    try:
        socket.create_connection((host, port), timeout=5).close()
        return "connected"
    except OSError as e:
        return f"refused: {type(e).__name__}: {e}"


def in_creature(body: CgroupBody, code: str, *args: str, timeout: float = 30) -> str:
    argv = body.wrap_spawn([sys.executable, "-c", code, *args])
    out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    return (out.stdout + out.stderr).strip()


def big_file() -> str:
    for cand in sorted(Path("/var/lib/epitaph/models").glob("*/*.gguf")):
        return str(cand)
    return "/usr/bin/python3"


def s3b(args: argparse.Namespace) -> dict[str, Any]:
    res: dict[str, Any] = {"uid": os.getuid(), "user": os.environ.get("USER", "?")}
    steps: dict[str, Any] = {}
    res["steps"] = steps

    def step(name: str, fn: Callable[[], Any]) -> Any:
        try:
            value = fn()
            steps[name] = {"ok": True, "value": value}
            log("ok  ", name, value)
            return value
        except Exception as e:  # the spike records every failure and carries on
            steps[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            log("FAIL", name, e)
            return None

    body = CgroupBody.delegated(CgroupSettings(creature_cpus="1-3"))
    res["root"] = str(body.root)
    step("supervisor_leaf", lambda: (body.supervisor / "cgroup.procs").read_text().split())
    step("controllers", lambda: sorted(body.controllers))
    step("creature_leaf", lambda: body.creature.is_dir())

    def set_clear(name: str, value: str, cleared: str) -> list[str]:
        f = body.creature / name
        f.write_text(value)
        got = f.read_text().strip()
        f.write_text(cleared)
        return [got, f.read_text().strip()]

    step("memory.high", lambda: set_clear("memory.high", str(200 * MIB), "max"))
    step("memory.max", lambda: set_clear("memory.max", str(300 * MIB), "max"))
    step("memory.swap.max", lambda: set_clear("memory.swap.max", "0", "0"))
    step("cpu.max", lambda: set_clear("cpu.max", "50000 100000", "max 100000"))
    step("memory.oom.group", lambda: (body.creature / "memory.oom.group").read_text().strip())

    # A child in the creature: cgroup, pinning, counters.
    argv = body.wrap_spawn([sys.executable, "-c", CHILD, big_file(), "30"])
    child = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
    line = child.stdout.readline().strip() if child.stdout else ""
    step("child_cgroup_and_affinity", lambda: line)
    p0 = body.progress()
    step("cpu.stat", lambda: read_flat_keyed(body.creature / "cpu.stat"))
    step("io.stat", lambda: (body.creature / "io.stat").read_text().strip())
    step("memory.stat", lambda: {k: v for k, v in read_flat_keyed(body.creature / "memory.stat").items() if k in ("anon", "file", "pgmajfault", "pgfault")})
    step("memory.events", lambda: read_flat_keyed(body.creature / "memory.events"))
    step("progress", lambda: vars(p0))
    step("vitals", lambda: vars(body.vitals()))

    # OOM by memory.max with swap off: the kernel kills the child (60 MB anon, limit 20 MB).
    t0 = now()
    (body.creature / "memory.max").write_text(str(20 * MIB))
    rc = child.wait(timeout=20)
    step("oom_kill", lambda: {"s": round(now() - t0, 3), "rc": rc, "events": body.memory_events()})
    step("death_cause_oom", lambda: body.death_cause(CreatureStatus(False, signal=-rc if rc < 0 else None)).value)
    body.reset_creature_cgroup()

    # cgroup.kill
    child = subprocess.Popen(body.wrap_spawn([sys.executable, "-c", CHILD, "", "60"]), stdout=subprocess.PIPE, text=True)
    if child.stdout:
        child.stdout.readline()
    t0 = now()
    body.kill_now(Cause.MANUAL)
    rc = child.wait(timeout=10)
    step("cgroup.kill", lambda: {"s": round(now() - t0, 3), "rc": rc, "empty": body.wait_empty(5)})
    step("death_cause_manual", lambda: body.death_cause(CreatureStatus(False, signal=9)).value)
    body.reset_creature_cgroup()

    # Network: baseline, then the nftables rule installed by the root side.
    step("net_creature_before", lambda: in_creature(body, CONNECT, "1.1.1.1", "80"))
    if args.sync_dir:
        sync = Path(args.sync_dir)
        (sync / "creature_path").write_text(str(body.creature))
        deadline = now() + 120
        while not (sync / "nft_ready").exists() and now() < deadline:
            time.sleep(0.5)
        res["nft_installed"] = (sync / "nft_ready").exists()
        if res["nft_installed"]:
            step("net_creature_after", lambda: in_creature(body, CONNECT, "1.1.1.1", "80"))
            step("net_creature_dns", lambda: in_creature(body, "import socket;\ntry:\n print(socket.getaddrinfo('example.org', 80)[0][4])\nexcept OSError as e:\n print('refused:', e)"))
            step("net_supervisor_after", lambda: connect_test("1.1.1.1", 80))
            srv = socket.socket()
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            step("net_creature_localhost", lambda: in_creature(body, CONNECT, "127.0.0.1", str(port)))
            srv.close()
        (sync / "probe_done").write_text("1")
    body.reset_creature_cgroup()
    res["ok"] = all(v["ok"] for v in steps.values())
    return res


# ---------------------------------------------------------------------------------------
# llama-server helpers


def drop_cache(path: str) -> None:
    """Evict a file from the page cache so the next reader re-reads it (and is charged for it).

    Page-cache pages stay charged to the cgroup that first read them; a model cached by an
    rsync or a checksum is not in the creature's memory.current at all.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)


class Server:
    def __init__(self, body: CgroupBody, binary: str, model: str, threads: int, mmap: bool,
                 ctx: int = 2048, args_cold: bool = True) -> None:
        argv = [binary, "-m", model, "-t", str(threads), "-c", str(ctx), "--host", "127.0.0.1",
                "--port", str(PORT), "-ngl", "0", "--no-warmup"]
        if not mmap:
            argv.append("--no-mmap")
        if args_cold:
            drop_cache(model)
        self.log_path = Path(f"server-{int(time.time() * 1000)}.log")
        self.t0 = now()
        self.proc = subprocess.Popen(body.wrap_spawn(argv), stdout=subprocess.DEVNULL,
                                     stderr=self.log_path.open("w"))
        self.load_s: float | None = None

    def wait_ready(self, timeout: float = 300) -> float:
        while now() - self.t0 < timeout:
            if self.proc.poll() is not None:
                tail = self.log_path.read_text(errors="ignore").strip().splitlines()[-4:]
                raise RuntimeError(f"llama-server exited rc={self.proc.returncode}: {tail}")
            try:
                c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=2)
                c.request("GET", "/health")
                if c.getresponse().status == 200:
                    self.load_s = now() - self.t0
                    return self.load_s
            except OSError:
                pass
            time.sleep(0.25)
        raise TimeoutError("llama-server did not become ready")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(10)


def stream(n_predict: int, stamps: list[float], stop_at: Callable[[], bool] = lambda: False,
           prompt: str = PROMPT, timeout: float = 600) -> dict[str, Any]:
    """Stream a completion; append a timestamp per token. Returns the final timings."""
    body = json.dumps({"prompt": prompt, "n_predict": n_predict, "stream": True,
                       "cache_prompt": False, "ignore_eos": True, "temperature": 0.7,
                       "seed": 1}).encode()
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    c.request("POST", "/completion", body, {"Content-Type": "application/json"})
    r = c.getresponse()
    final: dict[str, Any] = {}
    for raw in r:
        line = raw.decode(errors="ignore").strip()
        if not line.startswith("data:"):
            continue
        msg = json.loads(line[5:])
        stamps.append(now())
        if msg.get("stop"):
            final = msg.get("timings", {})
            break
        if stop_at():
            break
    return final


def gaps(stamps: list[float]) -> dict[str, float]:
    g = [b - a for a, b in zip(stamps[1:], stamps[2:], strict=False)]  # skip the first token
    if not g:
        return {}
    g.sort()
    return {"n": len(g), "tok_s": round(len(g) / sum(g), 3), "p50": round(g[len(g) // 2], 3),
            "p95": round(g[int(len(g) * 0.95)], 3), "max": round(g[-1], 3),
            "mean": round(statistics.fmean(g), 3)}


def mem_snapshot(body: CgroupBody) -> dict[str, int]:
    st = read_flat_keyed(body.creature / "memory.stat")
    return {"current_mb": int((body.creature / "memory.current").read_text()) // MIB,
            "anon_mb": st.get("anon", 0) // MIB, "file_mb": st.get("file", 0) // MIB,
            "pgmajfault": st.get("pgmajfault", 0)}


# ---------------------------------------------------------------------------------------
# S3: death by RAM


def death_trial(body: CgroupBody, args: argparse.Namespace, mmap: bool, fraction: float,
                wait_s: float, basis: str = "current") -> dict[str, Any]:
    body.reset_creature_cgroup()
    srv = Server(body, args.server, args.model, 3, mmap)
    trial: dict[str, Any] = {"mmap": mmap, "fraction": fraction, "basis": basis,
                             "env_before": body_env()}
    try:
        trial["load_s"] = round(srv.wait_ready(), 2)
        warm: list[float] = []
        stream(8, warm)
        trial["warm"] = mem_snapshot(body)
        stamps: list[float] = []
        th = threading.Thread(target=lambda: _swallow(lambda: stream(400, stamps)), daemon=True)
        th.start()
        while len(stamps) < 3 and srv.proc.poll() is None:
            time.sleep(0.05)
        p0 = body.progress()
        if basis == "anon":
            base = read_flat_keyed(body.creature / "memory.stat").get("anon", 0)
        else:
            base = int((body.creature / "memory.current").read_text())
        limit = int(base * fraction)
        t0 = now()
        (body.creature / "memory.max").write_text(str(limit))
        body._squeezed_at = t0  # noqa: SLF001 - the probe stands in for apply()
        n0 = len(stamps)
        while srv.proc.poll() is None and now() - t0 < wait_s:
            time.sleep(0.02)
        dead = srv.proc.poll() is not None
        p1 = body.progress()
        trial.update({
            "limit_mb": limit // MIB,
            "killed": dead,
            "kill_s": round(now() - t0, 2) if dead else None,
            "rc": srv.proc.returncode,
            "tokens_after_squeeze": len(stamps) - n0,
            "majfault_during": p1.majfault - p0.majfault,
            "io_read_mb_during": (p1.io_rbytes - p0.io_rbytes) // MIB,
            "events": body.memory_events(),
        })
        if dead:
            st = CreatureStatus(False, signal=-srv.proc.returncode if srv.proc.returncode < 0 else None)
            trial["cause"] = body.death_cause(st).value
    except Exception as e:
        trial["error"] = f"{type(e).__name__}: {e}"
    finally:
        body.kill_now(Cause.MANUAL)
        srv.stop()
        body.reset_creature_cgroup()
    trial["env_after"] = body_env()
    log("trial", json.dumps(trial))
    return trial


def _swallow(fn: Callable[[], Any]) -> None:
    with contextlib.suppress(Exception):
        fn()


def eviction_probe(body: CgroupBody, args: argparse.Namespace) -> dict[str, Any]:
    """Speed with memory.high 1% and 5% below the working set (mmap only, for the record)."""
    body.reset_creature_cgroup()
    srv = Server(body, args.server, args.model, 3, True)
    out: dict[str, Any] = {"env_before": body_env()}
    try:
        out["load_s"] = round(srv.wait_ready(), 2)
        stream(8, [])
        base: list[float] = []
        stream(24, base)
        out["baseline"] = gaps(base) | mem_snapshot(body)
        ws = int((body.creature / "memory.current").read_text())
        for pct in (1, 5):
            (body.creature / "memory.high").write_text(str(int(ws * (1 - pct / 100))))
            p0 = body.progress()
            s: list[float] = []
            t0 = now()
            _swallow(lambda s=s: stream(12, s, timeout=240))
            p1 = body.progress()
            out[f"evict_{pct}pct"] = gaps(s) | {
                "wall_s": round(now() - t0, 1),
                "io_read_mb": (p1.io_rbytes - p0.io_rbytes) // MIB,
                "majfault": p1.majfault - p0.majfault,
            } | mem_snapshot(body)
            (body.creature / "memory.high").write_text("max")
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        srv.stop()
        body.reset_creature_cgroup()
    out["env_after"] = body_env()
    log("eviction", json.dumps(out))
    return out


def s3(args: argparse.Namespace) -> dict[str, Any]:
    body = CgroupBody.delegated(CgroupSettings(creature_cpus="1-3"))
    res: dict[str, Any] = {"model": args.model, "fraction": args.fraction, "trials": []}
    for mmap in (False, True):
        for _ in range(args.reps):
            res["trials"].append(death_trial(body, args, mmap, args.fraction, args.wait))
    for _ in range(args.anon_reps):  # mmap with the limit below the anonymous memory
        res["trials"].append(death_trial(body, args, True, args.fraction, args.wait, "anon"))
    if args.eviction:
        res["eviction"] = eviction_probe(body, args)
    return res


# ---------------------------------------------------------------------------------------
# S3c: CPU share


def s3c(args: argparse.Namespace) -> dict[str, Any]:
    body = CgroupBody.delegated(CgroupSettings(creature_cpus="1-3"))
    body.reset_creature_cgroup()
    srv = Server(body, args.server, args.model, 2, not args.no_mmap)
    res: dict[str, Any] = {"model": args.model, "threads": 2, "levels": {},
                           "env_before": body_env()}
    try:
        res["load_s"] = round(srv.wait_ready(), 2)
        stream(8, [])
        for pct in args.levels:
            body.set_cpu_share(pct / 100)
            p0 = body.progress()
            stamps: list[float] = []
            t0 = now()
            timings = stream(args.tokens, stamps)
            p1 = body.progress()
            cs = read_flat_keyed(body.creature / "cpu.stat")
            res["levels"][str(pct)] = gaps(stamps) | {
                "first_token_s": round(stamps[0] - t0, 2) if stamps else None,
                "pp_tok_s": timings.get("prompt_per_second"),
                "tg_tok_s_server": timings.get("predicted_per_second"),
                "cpu_s": round((p1.cpu_usec - p0.cpu_usec) / 1e6, 2),
                "nr_throttled": cs.get("nr_throttled"),
                "throttled_usec": cs.get("throttled_usec"),
                "cpu_max": (body.creature / "cpu.max").read_text().strip(),
                "cpu_c": read_cpu_temp(),
            }
            log(pct, json.dumps(res["levels"][str(pct)]))
    finally:
        srv.stop()
        body.reset_creature_cgroup()
    res["env_after"] = body_env()
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spike", choices=["s3", "s3b", "s3c"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--sync-dir")
    ap.add_argument("--model")
    ap.add_argument("--server", default=os.path.expanduser("~/llama.cpp/build/bin/llama-server"))
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--fraction", type=float, default=0.5)
    ap.add_argument("--wait", type=float, default=30.0)
    ap.add_argument("--eviction", action="store_true")
    ap.add_argument("--anon-reps", type=int, default=2)
    ap.add_argument("--no-mmap", action="store_true")
    ap.add_argument("--tokens", type=int, default=48)
    ap.add_argument("--levels", type=int, nargs="+", default=[200, 170, 140, 110, 90, 70])
    args = ap.parse_args()
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    res = {"s3": s3, "s3b": s3b, "s3c": s3c}[args.spike](args)
    res.update(spike=args.spike, started=started, finished=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               kernel=os.uname().release)
    Path(args.out).write_text(json.dumps(res, indent=1))
    os.sync()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

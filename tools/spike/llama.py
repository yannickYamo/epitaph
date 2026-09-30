"""Stdlib-only helpers for the spikes: run a llama-server, talk to it, build a life-like chat.

Runs on the laptop and on the Pi (system python3, no venv). Used by s1*, s2*, s4*, s6*.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
BIN = Path(os.environ.get("LLAMA_BIN", "~/llama.cpp/build/bin")).expanduser()
MODELS = Path(
    os.environ.get(
        "EPITAPH_MODELS_DIR",
        "/var/lib/epitaph/models" if Path("/var/lib/epitaph/models").exists() else "~/epitaph-models",
    )
).expanduser()


def model_path(model: str, quant: str) -> Path:
    return MODELS / model / f"{quant}.gguf"


def default_prompt() -> dict[str, Any]:
    cfg_path = REPO / "config" / "default.toml"
    if cfg_path.exists():
        return tomllib.loads(cfg_path.read_text())["prompt"]
    return json.loads((Path(__file__).parent / "prompt.json").read_text())


class Server:
    """A llama-server child process on 127.0.0.1:<port>."""

    def __init__(
        self,
        model: Path,
        *,
        threads: int = 3,
        ctx: int = 2048,
        port: int = 8091,
        cache_reuse: int = 256,
        mmap: bool = True,
        swa_full: bool = False,
        extra: list[str] | None = None,
        log: Path | None = None,
        taskset: str | None = None,
    ) -> None:
        self.model = model
        self.port = port
        self.argv = [
            str(BIN / "llama-server"),
            "-m", str(model),
            "--host", "127.0.0.1",
            "--port", str(port),
            "-c", str(ctx),
            "-t", str(threads),
            "-np", "1",
            "--cache-ram", "0",
            "--jinja",
            "--no-webui",
            "-ngl", "0",
            "--metrics",
        ]  # fmt: skip
        if cache_reuse:
            self.argv += ["--cache-reuse", str(cache_reuse)]
        if not mmap:
            self.argv += ["--load-mode", "none"]  # --no-mmap was removed before b11277
        if swa_full:
            self.argv += ["--swa-full"]
        self.argv += extra or []
        if taskset:
            self.argv = ["taskset", "-c", taskset, *self.argv]
        self.log = log or Path(os.environ.get("TMPDIR", "/tmp")) / f"llama-server-{port}.log"
        self.proc: subprocess.Popen[bytes] | None = None
        self.t_spawn = 0.0
        self.load_s = 0.0

    def start(self, timeout: float = 600) -> float:
        """Spawn and wait for /health; returns seconds from spawn to healthy."""
        self.t_spawn = time.monotonic()
        with self.log.open("wb") as f:
            self.proc = subprocess.Popen(self.argv, stdout=f, stderr=subprocess.STDOUT)
        while time.monotonic() - self.t_spawn < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server exited {self.proc.returncode}: {self.tail()}")
            try:
                if self.get("/health").get("status") == "ok":
                    self.load_s = time.monotonic() - self.t_spawn
                    return self.load_s
            except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError):
                pass
            except urllib.error.HTTPError:
                pass
            time.sleep(0.2)
        raise TimeoutError("llama-server did not become healthy")

    def tail(self, n: int = 15) -> str:
        try:
            return "\n".join(self.log.read_text(errors="replace").splitlines()[-n:])
        except OSError:
            return ""

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def rss_mb(self) -> float | None:
        if not self.proc:
            return None
        try:
            for line in Path(f"/proc/{self.proc.pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
        except OSError:
            return None
        return None

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path: str) -> Any:
        with urllib.request.urlopen(self.url(path), timeout=5) as r:
            return json.loads(r.read())

    def post(self, path: str, body: dict[str, Any], timeout: float = 3600) -> Any:
        req = urllib.request.Request(
            self.url(path),
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"error": {"code": e.code, "message": e.read().decode(errors="replace")}}

    def chat(self, messages: list[dict[str, str]], max_tokens: int = 70, **kw: Any) -> dict[str, Any]:
        """One non-streamed chat request; returns text, timings and wall seconds."""
        body: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": kw.pop("temperature", 0.8),
            "min_p": kw.pop("min_p", 0.05),
            "seed": kw.pop("seed", 42),
            "cache_prompt": True,
            **kw,
        }
        t0 = time.monotonic()
        r = self.post("/v1/chat/completions", body)
        wall = time.monotonic() - t0
        if "error" in r:
            return {"error": r["error"], "wall_s": wall}
        tm = r.get("timings", {})
        return {
            "text": r["choices"][0]["message"].get("content") or "",
            "reasoning": r["choices"][0]["message"].get("reasoning_content"),
            "prompt_tokens": r.get("usage", {}).get("prompt_tokens"),
            "prompt_n": tm.get("prompt_n"),
            "cache_n": tm.get("cache_n"),
            "prompt_ms": tm.get("prompt_ms"),
            "prompt_per_s": tm.get("prompt_per_second"),
            "predicted_n": tm.get("predicted_n"),
            "predicted_ms": tm.get("predicted_ms"),
            "predicted_per_s": tm.get("predicted_per_second"),
            "wall_s": wall,
        }


@contextlib.contextmanager
def running(server: Server) -> Iterator[Server]:
    server.start()
    try:
        yield server
    finally:
        server.stop()


def system_text(groups: int = 5, mechanics: bool = True, prompt: dict[str, Any] | None = None) -> str:
    p = prompt or default_prompt()
    parts = list(p["persona_groups"][:groups])
    if mechanics:
        parts.append(p["mechanics"])
    return "\n\n".join(parts)


def reading(i: int, full: bool = True) -> str:
    m = 1 + i
    if full:
        return (
            f"[host] t+{m:02d}:00 · health: nominal · memory 1280 tokens · "
            f"precision 6-bit · cores 3 of 4 · cpu {52 + i % 5}°C"
        )
    return f"[host] t+{m:02d}:00 · terminal · memory 140 · 2-bit · cores 1.4 of 4 · 0.6/s · 66°C"


def throttled() -> str | None:
    try:
        return subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None


def temp_c() -> float | None:
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except OSError:
        return None


def mem_available_mb() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024
    return 0.0


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

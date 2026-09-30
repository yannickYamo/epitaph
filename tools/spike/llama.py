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
        "/var/lib/epitaph/models"
        if Path("/var/lib/epitaph/models").exists()
        else "~/epitaph-models",
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
        load_mode: str = "",
        threads_batch: int = 0,
        swa_full: bool = False,
        slot_save_path: str = "",
        kv_debug: bool = False,
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
        if threads_batch:
            self.argv += ["-tb", str(threads_batch)]
        if cache_reuse:
            self.argv += ["--cache-reuse", str(cache_reuse)]
        if load_mode:
            self.argv += ["--load-mode", load_mode]  # mmap | none | dio (S3: dio on the Pi 4)
        elif not mmap:
            self.argv += ["--load-mode", "none"]  # --no-mmap was removed before b11277
        if swa_full:
            self.argv += ["--swa-full"]
        if slot_save_path:
            self.argv += ["--slot-save-path", slot_save_path]
        # LLAMA_KV_CACHE_DEBUG=1 with -v logs the KV cache's used cells and high-water mark
        # (find_slot: n = used_max_p1, the cells attention runs over) for every batch.
        self.env = {**os.environ, "LLAMA_KV_CACHE_DEBUG": "1"} if kv_debug else None
        if kv_debug:
            self.argv += ["-v"]
        self.argv += extra or []
        if taskset:
            self.argv = ["taskset", "-c", taskset, *self.argv]
        self.log = log or Path(os.environ.get("TMPDIR", "/tmp")) / f"llama-server-{port}.log"
        self.proc: subprocess.Popen[bytes] | None = None
        self._log_pos = 0
        self.t_spawn = 0.0
        self.load_s = 0.0

    def start(self, timeout: float = 600) -> float:
        """Spawn and wait for /health; returns seconds from spawn to healthy."""
        self.t_spawn = time.monotonic()
        self._log_pos = 0
        with self.log.open("wb") as f:
            self.proc = subprocess.Popen(
                self.argv, stdout=f, stderr=subprocess.STDOUT, env=self.env
            )
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

    def chat(
        self, messages: list[dict[str, str]], max_tokens: int = 70, **kw: Any
    ) -> dict[str, Any]:
        """One non-streamed chat request; returns text, timings and wall seconds."""
        body: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": kw.pop("temperature", 0.8),
            "min_p": kw.pop("min_p", 0.05),
            "seed": kw.pop("seed", 42),
            "cache_prompt": True,
            # Non-thinking, as the backend sends it (S6). Round 1's S1b/S1c runs lacked it,
            # so Qwen3 1.7B thought aloud and its visible text was empty.
            "chat_template_kwargs": kw.pop("chat_template_kwargs", {"enable_thinking": False}),
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
            "completion_tokens": r.get("usage", {}).get("completion_tokens"),
            "prompt_n": tm.get("prompt_n"),
            "cache_n": tm.get("cache_n"),
            "prompt_ms": tm.get("prompt_ms"),
            "prompt_per_s": tm.get("prompt_per_second"),
            "predicted_n": tm.get("predicted_n"),
            "predicted_ms": tm.get("predicted_ms"),
            "predicted_per_s": tm.get("predicted_per_second"),
            "wall_s": wall,
        }

    def prefill(self, system: str) -> dict[str, Any]:
        """Read the system prompt into the cache as the backend's `prefill` does (one token).

        The backend sends [system, probe user "x"] and generates one token; the next request's
        shared prefix (system and the user header) then comes from the cache.
        """
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": "x"}]
        return self.chat(msgs, max_tokens=1, temperature=0.0)

    def count(self, messages: list[dict[str, str]]) -> int:
        """Tokens of the messages rendered with the model's template (the backend's method)."""
        r = self.post(
            "/apply-template",
            {"messages": messages, "chat_template_kwargs": {"enable_thinking": False}},
        )
        t = self.post("/tokenize", {"content": r["prompt"], "parse_special": True})
        return len(t["tokens"])

    def slot(self) -> dict[str, Any]:
        """GET /slots for slot 0, without its sampling params; n_past is derived.

        n_past = the last prompt's tokens + the tokens decoded after it (the logical context).
        """
        s = {k: v for k, v in self.get("/slots")[0].items() if k != "params"}
        nt = (s.get("next_token") or [{}])[0]
        if "n_prompt_tokens" in s:
            s["n_past"] = int(s["n_prompt_tokens"]) + int(nt.get("n_decoded") or 0)
        return s

    def slot_action(self, action: str, filename: str) -> tuple[dict[str, Any], float]:
        """POST /slots/0?action=save|restore (needs --slot-save-path); (reply, wall seconds)."""
        t0 = time.monotonic()
        r = self.post(f"/slots/0?action={action}", {"filename": filename}, timeout=600)
        return r, time.monotonic() - t0

    def kv_marks(self) -> list[tuple[int, int]]:
        """(high-water cells, used cells) per batch logged since the last call (kv_debug=True).

        Reads the log incrementally: with -v it grows by megabytes per minute.
        """
        out: list[tuple[int, int]] = []
        tag = "find_slot: stream[0], n ="
        try:
            with self.log.open("rb") as f:
                f.seek(self._log_pos)
                data = f.read()
                self._log_pos += len(data)
        except OSError:
            return out
        for line in data.decode(errors="replace").splitlines():
            if tag in line:
                part = line.split(tag)[1]
                out.append((int(part.split(",")[0]), int(part.split("used =")[1].split(",")[0])))
        return out


@contextlib.contextmanager
def running(server: Server) -> Iterator[Server]:
    server.start()
    try:
        yield server
    finally:
        server.stop()


def system_text(
    groups: int = 5, mechanics: bool = True, prompt: dict[str, Any] | None = None
) -> str:
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

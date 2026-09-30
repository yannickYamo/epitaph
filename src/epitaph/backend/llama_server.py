"""The real creature: a llama-server process (BUILD_PLAN 9 A5, A6, A7).

- `start` spawns llama-server through `body.wrap_spawn` (its cgroup and cores), waits for
  /health, and starts a watcher that calls `on_death` when the process exits on its own,
  between requests too. A deliberate `stop` is not a death.
- `chat` and `complete` stream tokens over HTTP (SSE) with the prompt cache on; the final
  chunk carries the server's timings (`prompt_n` = tokens actually processed). A stream that
  breaks because the process died raises `CreatureDied`.
- `count_past_tokens` renders the messages with the model's own chat template and tokenizes
  them (S6: exact against the server's count), minus the same render without them.

Flags at llama.cpp b11277: `--no-mmap` no longer exists; `--load-mode none` replaces it.
`--cache-ram 0` keeps the server from holding prompt caches in host RAM (8 GB by default).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from epitaph.backend.base import CreatureDied
from epitaph.backend.errors import BackendError, ContextFull
from epitaph.types import Chunk, CreatureStatus, ModelSpec, Msg, Sampling

_PROBE = Msg("user", "x")


class _Spawner:
    """What the backend needs from the body: placing the child in its cgroup."""

    def wrap_spawn(self, argv: list[str]) -> list[str]:
        return list(argv)


@dataclass
class ServerSettings:
    """llama-server settings from the `[backend]` config section."""

    bin: str = "~/llama.cpp/build/bin/llama-server"
    models_dir: str = "~/epitaph-models"
    host: str = "127.0.0.1"
    port: int = 8081
    ctx: int = 2048
    cache_reuse: int = 256
    mmap: bool = True
    swa_full: str | bool = "auto"
    cache_type_k: str = "f16"
    cache_type_v: str = "f16"
    # Prompt-processing threads; None = the generation threads. S4: keeping 3 for the re-read
    # after a reload to 2 generation threads cuts the reload silence (pp scales with threads,
    # generation barely does on the Pi 4).
    threads_batch: int | None = None
    load_timeout_s: float = 300.0
    stop_timeout_s: float = 10.0
    log_path: str | None = None
    extra_args: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, cfg: Any) -> ServerSettings:
        """Build from an epitaph Config (duck-typed: `.get(dotted, default)`, `.state_dir`)."""
        models_dir = str(cfg.get("backend.models_dir", "auto"))
        if models_dir == "auto":
            models_dir = str(Path(cfg.state_dir) / "models")
            local = Path("~/epitaph-models").expanduser()
            if not Path(models_dir).exists() and local.exists():
                models_dir = str(local)
        return cls(
            bin=str(cfg.get("backend.bin", cls.bin)),
            models_dir=models_dir,
            port=int(cfg.get("backend.port", cls.port)),
            ctx=int(cfg.ctx),
            cache_reuse=int(cfg.get("backend.cache_reuse", cls.cache_reuse)),
            mmap=bool(cfg.get("backend.mmap", True)),
            swa_full=cfg.get("backend.swa_full", "auto"),
            cache_type_k=str(cfg.get("backend.cache_type_k", "f16")),
            cache_type_v=str(cfg.get("backend.cache_type_v", "f16")),
            threads_batch=(
                int(cfg.get("backend.threads_batch"))
                if cfg.get("backend.threads_batch") is not None
                else None
            ),
            load_timeout_s=float(cfg.get("life.load_timeout_s", 300)),
        )


def build_argv(s: ServerSettings, model: ModelSpec, quant: str, threads: int) -> list[str]:
    """The llama-server command line for one creature."""
    path = Path(s.models_dir).expanduser() / model.name / f"{quant}.gguf"
    argv = [
        str(Path(s.bin).expanduser()),
        "-m", str(path),
        "--host", s.host,
        "--port", str(s.port),
        "-c", str(s.ctx),
        "-t", str(threads),
        "-tb", str(s.threads_batch or threads),
        "-np", "1",
        "--cache-ram", "0",
        "--jinja",
        "--no-webui",
        "-ngl", "0",
        "-ctk", s.cache_type_k,
        "-ctv", s.cache_type_v,
    ]  # fmt: skip
    if s.cache_reuse > 0:
        argv += ["--cache-reuse", str(s.cache_reuse)]
    if not s.mmap:
        argv += ["--load-mode", "none"]
    if s.swa_full is True or (s.swa_full == "auto" and model.sliding_window):
        argv += ["--swa-full"]
    argv += list(s.extra_args)
    return argv


def request_body(
    messages: list[Msg] | None, sampling: Sampling, max_tokens: int, prompt: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "stream": True,
        "cache_prompt": True,
        "temperature": sampling.temperature,
        "min_p": sampling.min_p,
        "top_p": sampling.top_p,
        "repeat_penalty": sampling.repeat_penalty,
        "dry_multiplier": sampling.dry_multiplier,
    }
    if sampling.seed is not None:
        body["seed"] = sampling.seed
    if sampling.latin_only:
        body["grammar"] = LATIN_GRAMMAR
    if messages is not None:
        body["messages"] = [{"role": m.role, "content": m.content} for m in messages]
        body["max_tokens"] = max_tokens
        # Non-thinking for every template that knows the switch; ignored by the others (S6).
        body["chat_template_kwargs"] = {"enable_thinking": False}
    else:
        body["prompt"] = prompt or ""
        body["n_predict"] = max_tokens
    return body


# Letters, digits, common punctuation and whitespace only (the optional `latin_only`).
LATIN_GRAMMAR = r"""root ::= [A-Za-z0-9 ,.;:!?'"()\n’—-]*"""


def parse_sse_line(line: str) -> dict[str, Any] | None:
    """One SSE line to its JSON payload; None for keep-alives, comments and [DONE]."""
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if not data or data == "[DONE]":
        return None
    value = json.loads(data)
    return value if isinstance(value, dict) else None


def chunk_from_event(ev: dict[str, Any], chat: bool) -> tuple[str, dict[str, Any] | None]:
    """(text, timings or None) from one streamed event."""
    if "error" in ev:
        raise _error_from(ev["error"])
    if chat:
        choices = ev.get("choices") or [{}]
        delta = choices[0].get("delta") or {}
        text = str(delta.get("content") or "")
    else:
        text = str(ev.get("content") or "")
    timings = ev.get("timings")
    finished = (chat and (ev.get("choices") or [{}])[0].get("finish_reason")) or ev.get("stop")
    return text, timings if (timings and finished) else None


def _error_from(err: Any) -> Exception:
    msg = json.dumps(err) if not isinstance(err, str) else err
    if "exceed" in msg and "context" in msg:
        n = err.get("n_prompt_tokens", 0) if isinstance(err, dict) else 0
        c = err.get("n_ctx", 0) if isinstance(err, dict) else 0
        return ContextFull(int(n or 0), int(c or 0))
    return BackendError(msg)


def final_chunk(timings: dict[str, Any] | None) -> Chunk:
    t = timings or {}
    return Chunk(
        "",
        done=True,
        prompt_n=_int(t.get("prompt_n")),
        predicted_n=_int(t.get("predicted_n")),
        prompt_per_s=_float(t.get("prompt_per_second")),
        predicted_per_s=_float(t.get("predicted_per_second")),
    )


def _int(v: Any) -> int | None:
    return None if v is None else int(v)


def _float(v: Any) -> float | None:
    return None if v is None else float(v)


class LlamaServerBackend:
    """A creature running as a llama-server child process."""

    def __init__(
        self,
        settings: ServerSettings,
        body: Any = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.s = settings
        self.body = body or _Spawner()
        self._transport = transport
        self._proc: asyncio.subprocess.Process | None = None
        self._watcher: asyncio.Task[None] | None = None
        self._stopping = False
        self._on_death: list[Callable[[CreatureStatus], None]] = []
        self._status = CreatureStatus(alive=False)
        self._client: httpx.AsyncClient | None = None
        self.argv: list[str] = []
        self.load_s: float | None = None
        self.last_timings: dict[str, Any] | None = None

    # -- process -----------------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://{self.s.host}:{self.s.port}"

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0),
                transport=self._transport,
            )
        return self._client

    async def start(self, model: ModelSpec, quant: str, threads: int) -> None:
        await self.stop()
        self.argv = self.body.wrap_spawn(build_argv(self.s, model, quant, threads))
        log = self.s.log_path
        out: Any = open(os.path.expanduser(log), "ab") if log else asyncio.subprocess.DEVNULL  # noqa: SIM115
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.argv, stdout=out, stderr=asyncio.subprocess.STDOUT, start_new_session=True
            )
        finally:
            if log:
                out.close()
        self._stopping = False
        self._status = CreatureStatus(alive=True, pid=self._proc.pid)
        self._watcher = asyncio.create_task(self._watch(self._proc))
        try:
            await self._wait_healthy(t0)
        except BaseException:
            if self._proc.returncode is None:
                await self.stop(hard=True)
            raise
        self.load_s = loop.time() - t0

    async def _wait_healthy(self, t0: float) -> None:
        loop = asyncio.get_running_loop()
        while True:
            proc = self._proc
            if proc is None or proc.returncode is not None:
                raise CreatureDied(self.status())
            if loop.time() - t0 > self.s.load_timeout_s:
                raise TimeoutError(f"llama-server not healthy after {self.s.load_timeout_s:.0f}s")
            with contextlib.suppress(httpx.HTTPError):
                r = await self.client().get("/health")
                if r.status_code == 200 and r.json().get("status") == "ok":
                    return
            await asyncio.sleep(0.25)

    async def _watch(self, proc: asyncio.subprocess.Process) -> None:
        rc = await proc.wait()
        self._status = _status_from_rc(proc.pid, rc)
        if not self._stopping and proc is self._proc:
            for fn in list(self._on_death):
                fn(self._status)

    async def stop(self, hard: bool = False) -> None:
        proc = self._proc
        if proc is None:
            return
        self._stopping = True
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGKILL if hard else signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), self.s.stop_timeout_s)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        if self._watcher is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._watcher
        self._status = _status_from_rc(proc.pid, proc.returncode)
        self._proc = None

    async def aclose(self) -> None:
        await self.stop()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def on_death(self, fn: Callable[[CreatureStatus], None]) -> None:
        self._on_death.append(fn)

    def status(self) -> CreatureStatus:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            s = self._status
            s.alive = True
            return s
        return self._status

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    # -- requests ----------------------------------------------------------------------------

    async def _stream(self, path: str, body: dict[str, Any], chat: bool) -> AsyncIterator[Chunk]:
        if not self._alive():
            raise CreatureDied(self.status())
        timings: dict[str, Any] | None = None
        try:
            async with self.client().stream("POST", path, json=body) as r:
                if r.status_code != 200:
                    text = (await r.aread()).decode(errors="replace")
                    try:
                        raise _error_from(json.loads(text).get("error", text))
                    except json.JSONDecodeError:
                        raise BackendError(f"HTTP {r.status_code}: {text[:200]}") from None
                async for line in r.aiter_lines():
                    ev = parse_sse_line(line)
                    if ev is None:
                        continue
                    text, t = chunk_from_event(ev, chat)
                    if text:
                        yield Chunk(text)
                    if t is not None:
                        timings = t
        except (httpx.TransportError, httpx.StreamError) as e:
            await self._settle()
            if not self._alive():
                raise CreatureDied(self.status()) from e
            raise BackendError(f"stream broke: {e!r}") from e
        if timings is None:
            await self._settle()
            if not self._alive():
                raise CreatureDied(self.status())
        self.last_timings = timings
        if timings:
            self._status.tok_s = _float(timings.get("predicted_per_second"))
            self._status.prompt_tok_s = _float(timings.get("prompt_per_second"))
        yield final_chunk(timings)

    async def _settle(self) -> None:
        """Give the watcher a moment to see an exit that broke the stream."""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(proc.wait()), 2.0)

    def chat(
        self, messages: list[Msg], sampling: Sampling, max_tokens: int
    ) -> AsyncIterator[Chunk]:
        return self._stream(
            "/v1/chat/completions", request_body(messages, sampling, max_tokens), chat=True
        )

    def complete(self, prompt: str, sampling: Sampling, max_tokens: int) -> AsyncIterator[Chunk]:
        return self._stream(
            "/completion", request_body(None, sampling, max_tokens, prompt=prompt), chat=False
        )

    async def prefill(self, messages: list[Msg]) -> int:
        """Read these messages into the prompt cache without showing anything; returns the
        tokens processed. Used after a load to read the system prompt during the birth card
        or the reload silence, so the first thought only reads its reading (S1b: the system
        prompt alone is about 100 s of prompt processing for a 3B on the Pi 4)."""
        if not self._alive():
            raise CreatureDied(self.status())
        body = request_body([*messages, _PROBE], Sampling(temperature=0.0, min_p=0.0), 1)
        body["stream"] = False
        try:
            r = await self.client().post("/v1/chat/completions", json=body)
        except httpx.TransportError as e:
            await self._settle()
            if not self._alive():
                raise CreatureDied(self.status()) from e
            raise BackendError(f"prefill failed: {e!r}") from e
        if r.status_code != 200:
            raise _error_from(r.json().get("error", r.text))
        return int(r.json().get("timings", {}).get("prompt_n") or 0)

    async def _render_count(self, messages: list[Msg]) -> int:
        c = self.client()
        r = await c.post(
            "/apply-template",
            json={
                "messages": [{"role": m.role, "content": m.content} for m in messages],
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        r.raise_for_status()
        prompt = str(r.json()["prompt"])
        t = await c.post("/tokenize", json={"content": prompt, "parse_special": True})
        t.raise_for_status()
        return len(t.json()["tokens"])

    async def count_past_tokens(self, messages: list[Msg]) -> int:
        """Tokens these messages add to the rendered chat (BUILD_PLAN 5.4; method from S6)."""
        if not messages:
            return 0
        if not self._alive():
            raise CreatureDied(self.status())
        try:
            return await self._render_count([*messages, _PROBE]) - await self._render_count(
                [_PROBE]
            )
        except httpx.TransportError as e:
            await self._settle()
            if not self._alive():
                raise CreatureDied(self.status()) from e
            raise BackendError(f"count failed: {e!r}") from e


def _status_from_rc(pid: int, rc: int | None) -> CreatureStatus:
    if rc is None:
        return CreatureStatus(alive=True, pid=pid)
    if rc < 0:
        return CreatureStatus(alive=False, pid=pid, signal=-rc)
    return CreatureStatus(alive=False, pid=pid, exit_code=rc)

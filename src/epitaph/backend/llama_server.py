"""The real creature: a llama-server process (BUILD_PLAN 9 A5, A6, A7).

- `start` spawns llama-server through `body.wrap_spawn` (its cgroup and cores), waits for
  /health, and starts a watcher that calls `on_death` when the process exits on its own,
  between requests too. A deliberate `stop` is not a death.
- `chat` and `complete` stream tokens over HTTP (SSE) with the prompt cache on; the final
  chunk carries the server's timings (`prompt_n` = tokens actually processed). A stream that
  breaks because the process died raises `CreatureDied`.
- `count_past_tokens` renders the messages with the model's own chat template and tokenizes
  them (S6: exact against the server's count), minus the same render without them.

- A reload (`start` while a creature of the same model is running) either re-reads the memory
  in the fresh server (`reload_handover = "reread"`) or carries the KV cache over
  (`"slot"`, spike S4b): the old server saves its slot to `slot_save_path` (RAM, /dev/shm),
  the new quant restores it, and cache reuse absorbs the reload's cut on the next request.
  Any failure falls back to the re-read.
- The persona cache (`persona_cache_dir`, dread plan W4): the first `prefill` of a system
  prompt saves the slot to disk; every later one on the same server, model and prompt
  restores it instead of reading it again (`persona_cache.py`). Any failure falls back to
  the prefill. `last_prefill` says what happened.

Flags at llama.cpp b11277: `--no-mmap` no longer exists; `--load-mode none` replaces it.
`--cache-ram 0` keeps the server from holding prompt caches in host RAM (8 GB by default).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from epitaph.backend.base import BackendError, ContextFull, CreatureDied
from epitaph.backend.persona_cache import PersonaStore, PrefillInfo, persona_key
from epitaph.types import Chunk, CreatureStatus, ModelSpec, Msg, Sampling

_PROBE = Msg("user", "x")
_log = logging.getLogger(__name__)
HANDOVERS = ("reread", "slot")

# The first turn of a strict template after a trim that left a thought at the front: the
# memory-gap reading the model would have seen before it (BUILD_PLAN 5.4).
GAP_TURN = Msg("user", "[host] earlier memory lost", kind="marker")


def alternate_roles(messages: list[Msg]) -> list[Msg]:
    """`messages` shaped for a template that wants user and assistant turns to alternate.

    Gemma 3's template raises unless the turns after the system prompt go user, assistant,
    user, ... Consecutive turns of one role are joined into one (with a newline), and a
    history that starts with a thought (its reading was trimmed) gets the memory-gap reading
    in front. Other templates never see this: it applies only when the server refuses two
    user turns in a row (`LlamaServerBackend.strict_roles`).
    """
    out: list[Msg] = []
    for m in messages:
        if m.role == "system" and not any(o.role != "system" for o in out):
            out.append(m)
            continue
        if not any(o.role != "system" for o in out) and m.role == "assistant":
            out.append(GAP_TURN)
        if out and out[-1].role == m.role and m.role != "system":
            prev = out[-1]
            out[-1] = Msg(prev.role, f"{prev.content}\n{m.content}", prev.turn, prev.kind)
        else:
            out.append(m)
    return out


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
    load_mode: str = "auto"  # auto | mmap | none | dio (spike S3: dio on the Pi 4)
    swa_full: str | bool = "auto"
    cache_type_k: str = "f16"
    cache_type_v: str = "f16"
    # Prompt-processing threads; None = the generation threads. S4: keeping 3 for the re-read
    # after a reload to 2 generation threads cuts the reload silence (pp scales with threads,
    # generation barely does on the Pi 4).
    threads_batch: int | None = None
    # Tokens DRY scans for repeats; -1 = the whole context (sent as `ctx`: b11277 rejects -1).
    # The server's default is 64, which never reaches the previous thought, so a small model
    # can repeat it word for word (rehearsal 0c: Qwen3 1.7B copied its last thought 20 times).
    dry_penalty_last_n: int | None = -1
    load_timeout_s: float = 300.0
    stop_timeout_s: float = 10.0
    # A slot save or restore (about 0.3 s, spike S4b) that takes longer is given up.
    slot_timeout_s: float = 30.0
    log_path: str | None = None
    extra_args: tuple[str, ...] = ()
    # How a reload carries the memory: "reread" (the fresh server reads it all again) or
    # "slot" (save the KV cache before the stop, restore it after the load; spike S4b).
    reload_handover: str = "reread"
    # Where slot files go (--slot-save-path). RAM: the SD card reads 40 MB/s.
    slot_save_path: str = "/dev/shm/epitaph-slots"
    # Where the persona cache keeps its slot files (None: off; the system prompt is read at
    # every birth). On disk: a reboot keeps them.
    persona_cache_dir: str | None = None

    def __post_init__(self) -> None:
        """Reject an unknown `reload_handover`."""
        if self.reload_handover not in HANDOVERS:
            raise ValueError(
                f"backend.reload_handover must be one of {HANDOVERS}, not {self.reload_handover!r}"
            )

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
            load_mode=str(cfg.get("backend.load_mode", "auto")),
            swa_full=cfg.get("backend.swa_full", "auto"),
            cache_type_k=str(cfg.get("backend.cache_type_k", "f16")),
            cache_type_v=str(cfg.get("backend.cache_type_v", "f16")),
            threads_batch=(
                int(cfg.get("backend.threads_batch"))
                if cfg.get("backend.threads_batch") is not None
                else None
            ),
            dry_penalty_last_n=_opt_int(cfg.get("sampling.dry_penalty_last_n", -1)),
            load_timeout_s=float(cfg.get("life.load_timeout_s", 300)),
            slot_timeout_s=float(cfg.get("backend.slot_timeout_s", cls.slot_timeout_s)),
            reload_handover=str(cfg.get("backend.reload_handover", cls.reload_handover)),
            slot_save_path=str(cfg.get("backend.slot_save_path", cls.slot_save_path)),
            persona_cache_dir=persona_cache_dir(cfg),
        )


def persona_cache_dir(cfg: Any) -> str | None:
    """`[backend] persona_cache_dir` ("auto": `<state_dir>/cache`), None when the cache is off."""
    if not bool(cfg.get("backend.persona_cache", False)):
        return None
    raw = str(cfg.get("backend.persona_cache_dir", "auto"))
    return str(Path(cfg.state_dir) / "cache") if raw == "auto" else raw


def _uses_slots(s: ServerSettings) -> bool:
    return s.reload_handover == "slot" or s.persona_cache_dir is not None


def _opt_int(v: Any) -> int | None:
    return None if v is None else int(v)


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
    if s.load_mode in ("mmap", "none", "dio"):
        argv += ["--load-mode", s.load_mode]
    elif not s.mmap:
        argv += ["--load-mode", "none"]
    if s.swa_full is True or (s.swa_full == "auto" and model.sliding_window):
        argv += ["--swa-full"]
    if _uses_slots(s):
        argv += ["--slot-save-path", str(Path(s.slot_save_path).expanduser())]
    argv += list(s.extra_args)
    return argv


@dataclass
class Handover:
    """What the last reload did with the KV cache (`LlamaServerBackend.last_handover`).

    `mode` is "reread" when nothing was carried over (the configured mode, a birth, a new
    model, or a failure; `error` says which failure), else "slot". Times are in seconds;
    `tokens` is how many cached tokens the new server got back.
    """

    mode: str = "reread"
    tokens: int = 0
    file_bytes: int = 0
    save_s: float = 0.0
    restore_s: float = 0.0
    error: str | None = None
    details: dict[str, Any] = field(default_factory=lambda: {})


def request_body(
    messages: list[Msg] | None,
    sampling: Sampling,
    max_tokens: int,
    prompt: str | None = None,
    dry_penalty_last_n: int | None = None,
) -> dict[str, Any]:
    """The JSON body of one streamed request, with the prompt cache on.

    A chat request (thinking off) when `messages` is given, else a raw completion of
    `prompt`. `max_tokens` caps the generated tokens. `dry_penalty_last_n` sets how far back
    DRY looks for repeats (-1: the whole context; None: the server's default).
    """
    body: dict[str, Any] = {
        "stream": True,
        "cache_prompt": True,
        "temperature": sampling.temperature,
        "min_p": sampling.min_p,
        "top_p": sampling.top_p,
        "repeat_penalty": sampling.repeat_penalty,
        "dry_multiplier": sampling.dry_multiplier,
    }
    if dry_penalty_last_n is not None:
        body["dry_penalty_last_n"] = dry_penalty_last_n
    if sampling.seed is not None:
        body["seed"] = sampling.seed
    if sampling.latin_only:
        body["grammar"] = LATIN_GRAMMAR
    if sampling.logit_bias:
        body["logit_bias"] = [[w, b] for w, b in sampling.logit_bias]
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
    """The closing chunk (done=True) with the server's timings; None where a timing is absent."""
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
        """Prepare a backend; nothing is spawned until start().

        `body` places the child in its cgroup (anything with `wrap_spawn`; None spawns it
        unwrapped). `transport` replaces the HTTP transport, for tests.
        """
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
        self.last_handover = Handover()
        self._model_name: str | None = None
        self._model: ModelSpec | None = None
        self._quant: str | None = None
        self._restored = False
        self.last_prefill = PrefillInfo()
        self.persona = (
            PersonaStore(settings.persona_cache_dir, settings.slot_save_path)
            if settings.persona_cache_dir
            else None
        )
        self.strict_roles = False  # set at start(): the template wants alternating turns

    # -- process -----------------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        """The server's HTTP root, e.g. "http://127.0.0.1:8081"."""
        return f"http://{self.s.host}:{self.s.port}"

    def client(self) -> httpx.AsyncClient:
        """The shared HTTP client, made on first use (no read timeout: generation is slow)."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0),
                transport=self._transport,
            )
        return self._client

    async def start(self, model: ModelSpec, quant: str, threads: int) -> None:
        """Spawn llama-server (stopping any previous one) and wait until /health says ok.

        Sets `load_s` to the load time in seconds. Raises CreatureDied if the process exits
        while loading and TimeoutError after `load_timeout_s`; neither leaves a process behind.
        With `reload_handover = "slot"`, a running creature of the same model saves its KV
        cache first and the new one restores it (`last_handover` says what happened); the
        next `prefill` is then skipped, since the restored cache already holds the prompt.
        """
        self._restored = False
        saved = await self._save_slot(model)
        await self.stop()
        if _uses_slots(self.s):
            # llama-server refuses to start when --slot-save-path does not exist, and /dev/shm
            # is emptied at every boot: create it before every spawn, not only at a reload.
            Path(self.s.slot_save_path).expanduser().mkdir(mode=0o700, parents=True, exist_ok=True)
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
        self._model_name = model.name
        self._model, self._quant = model, quant
        if saved is not None:
            await self._restore_slot(saved)

    # -- reload handover (spike S4b) ---------------------------------------------------------

    def _slot_file(self, model: ModelSpec) -> str:
        return f"{model.name}.bin"

    async def _save_slot(self, model: ModelSpec) -> str | None:
        """Save the running creature's slot for the next quant; None when nothing is carried."""
        self.last_handover = Handover()
        if self.s.reload_handover != "slot" or not self._alive():
            return None
        if self._model_name != model.name:
            self.last_handover.error = "new model"
            return None
        name = self._slot_file(model)
        Path(self.s.slot_save_path).expanduser().mkdir(mode=0o700, parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        r = await self._slot_action("save", name)
        self.last_handover.save_s = loop.time() - t0
        if r is None or not r.get("n_saved"):
            self.last_handover.error = f"save failed: {r}"
            _log.warning("slot save failed, the reload re-reads the memory: %s", r)
            return None
        self.last_handover.file_bytes = int(r.get("n_written") or 0)
        self.last_handover.details["save"] = r
        return name

    async def _restore_slot(self, name: str) -> None:
        """Restore a saved slot into the new server; on failure the cache simply stays empty."""
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        r = await self._slot_action("restore", name)
        self.last_handover.restore_s = loop.time() - t0
        with contextlib.suppress(OSError):
            (Path(self.s.slot_save_path).expanduser() / name).unlink()
        if r is None or not r.get("n_restored"):
            self.last_handover.error = f"restore failed: {r}"
            _log.warning("slot restore failed, the reload re-reads the memory: %s", r)
            return
        self.last_handover.mode = "slot"
        self.last_handover.tokens = int(r["n_restored"])
        self.last_handover.details["restore"] = r
        self._restored = True

    async def _slot_action(self, action: str, name: str) -> dict[str, Any] | None:
        """POST /slots/0?action=save|restore; the JSON reply, or None on any failure."""
        try:
            r = await self.client().post(
                f"/slots/0?action={action}",
                json={"filename": name},
                timeout=self.s.slot_timeout_s,
            )
        except httpx.HTTPError as e:
            return {"error": repr(e)}
        try:
            data = r.json()
        except ValueError:
            data = {"error": r.text[:200]}
        if r.status_code != 200 or not isinstance(data, dict):
            return {"error": data, "status": r.status_code}
        return data

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
        """Stop the process: SIGTERM (SIGKILL when `hard`), then SIGKILL after `stop_timeout_s`.

        A deliberate stop does not fire on_death. Does nothing when no process is running.
        Never waits forever: a process that outlives the SIGKILL by another `stop_timeout_s`
        (stuck in the kernel) is logged and left to the cgroup reset.
        """
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
                try:
                    await asyncio.wait_for(proc.wait(), self.s.stop_timeout_s)
                except TimeoutError:
                    _log.error("llama-server pid %d did not exit after SIGKILL", proc.pid)
        if self._watcher is not None:
            if proc.returncode is None:
                self._watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watcher
        self._status = _status_from_rc(proc.pid, proc.returncode)
        self._proc = None

    async def aclose(self) -> None:
        """Stop the process and close the HTTP client."""
        await self.stop()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def on_death(self, fn: Callable[[CreatureStatus], None]) -> None:
        """Register a callback fired when the process exits on its own, even between requests."""
        self._on_death.append(fn)

    def status(self) -> CreatureStatus:
        """The process status and the speeds (tokens/s) of the last finished request."""
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
        self._restored = False  # the request uses (and reshapes) the restored cache
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

    def _roles(self, messages: list[Msg]) -> list[Msg]:
        return alternate_roles(messages) if self.strict_roles else messages

    def chat(
        self, messages: list[Msg], sampling: Sampling, max_tokens: int
    ) -> AsyncIterator[Chunk]:
        """Stream a thought from /v1/chat/completions; the final chunk carries the timings.

        Raises CreatureDied when the process died, ContextFull when the prompt does not fit,
        and BackendError for any other server or stream error.
        """
        return self._chat(messages, sampling, max_tokens)

    async def _chat(
        self, messages: list[Msg], sampling: Sampling, max_tokens: int
    ) -> AsyncIterator[Chunk]:
        for attempt in range(2):
            body = request_body(
                self._roles(messages), sampling, max_tokens, None, self._dry_last_n()
            )
            try:
                async for c in self._stream("/v1/chat/completions", body, chat=True):
                    yield c
                return
            except BackendError as e:
                if attempt or not self._learn_strict(e):
                    raise

    def _learn_strict(self, err: Exception) -> bool:
        """True (and strict from now on) when `err` is a template refusing the turn order.

        Nothing has been streamed then: the server refuses before it reads the prompt.
        """
        if self.strict_roles or "must alternate" not in str(err):
            return False
        self.strict_roles = True
        return True

    def complete(self, prompt: str, sampling: Sampling, max_tokens: int) -> AsyncIterator[Chunk]:
        """Stream a raw completion of `prompt` from /completion (diary mode); errors as chat()."""
        return self._stream(
            "/completion",
            request_body(None, sampling, max_tokens, prompt, self._dry_last_n()),
            chat=False,
        )

    def _dry_last_n(self) -> int | None:
        n = self.s.dry_penalty_last_n
        return self.s.ctx if n == -1 else n

    async def prefill(self, messages: list[Msg]) -> int:
        """Read messages into the prompt cache without generating; return the tokens processed.

        Used after a load to read the system prompt during the birth card or the reload
        silence, so the first thought only reads its reading (S1b: the system prompt alone is
        about 100 s of prompt processing for a 3B on the Pi 4). With the persona cache, a
        system prompt read before on this server and model is restored instead (and 0 is
        returned); the first one read is saved. `last_prefill` says which.
        """
        if not self._alive():
            raise CreatureDied(self.status())
        if self._restored:
            # The restored cache already starts with this prompt and holds the memory after
            # it; a prefill request would cut the cache back to the system prompt.
            self._restored = False
            self.last_prefill = PrefillInfo("handover")
            return 0
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        key = self._persona_key(messages)
        error: str | None = None
        if key is not None:
            info = await self._restore_persona(key)
            if info.mode == "restore":
                info.seconds = loop.time() - t0
                self.last_prefill = info
                return 0
            error = info.error
        n = await self._prefill_request(messages)
        info = PrefillInfo("prefill", tokens=n, error=error)
        if key is not None:
            await self._save_persona(key, info)
        info.seconds = loop.time() - t0
        self.last_prefill = info
        return n

    # -- the persona cache (dread plan W4) -----------------------------------------------------

    def _persona_key(self, messages: list[Msg]) -> str | None:
        # Only the system prompt is cached: a memory re-read (a rehearsal's re-warm) never is.
        if self.persona is None or self._model is None or self._quant is None:
            return None
        if not messages or any(m.role != "system" for m in messages):
            return None
        s, model = self.s, self._model
        return persona_key(
            model_file=Path(s.models_dir).expanduser() / model.name / f"{self._quant}.gguf",
            server_bin=Path(s.bin).expanduser(),
            model=model.name,
            quant=self._quant,
            ctx=s.ctx,
            cache_type_k=s.cache_type_k,
            cache_type_v=s.cache_type_v,
            swa_full=s.swa_full is True or (s.swa_full == "auto" and model.sliding_window),
            messages=messages,
        )

    async def _restore_persona(self, key: str) -> PrefillInfo:
        """Restore the cached slot of `key`; mode "restore" on success, else why not."""
        store = self.persona
        if store is None or not store.has(key):
            return PrefillInfo("prefill")
        try:
            size = await asyncio.to_thread(store.stage, key)
        except OSError as e:
            return PrefillInfo("prefill", error=f"stage failed: {e!r}")
        try:
            r = await self._slot_action("restore", store.slot_name(key))
        finally:
            store.unstage(key)
        if r is None or not r.get("n_restored"):
            if r is not None and "status" in r:
                # A file the server refused to read is dropped: the prefill saves a good one.
                store.forget(key)
            _log.warning("persona restore failed, the system prompt is read: %s", r)
            return PrefillInfo("prefill", error=f"restore failed: {r}")
        return PrefillInfo("restore", tokens=int(r["n_restored"]), file_bytes=size)

    async def _save_persona(self, key: str, info: PrefillInfo) -> None:
        """Save the slot after a prefill for the next births; a failure only costs that."""
        store = self.persona
        if store is None:
            return
        r = await self._slot_action("save", store.slot_name(key))
        if r is None or not r.get("n_saved"):
            store.unstage(key)
            info.error = info.error or f"save failed: {r}"
            _log.warning("persona save failed, the next birth reads it again: %s", r)
            return
        try:
            info.file_bytes = await asyncio.to_thread(store.keep_saved, key)
        except OSError as e:
            info.error = info.error or f"keep failed: {e!r}"
            _log.warning("persona cache file not kept: %r", e)
            return
        info.saved = True

    async def _prefill_request(self, messages: list[Msg]) -> int:
        for attempt in range(2):
            body = request_body(
                [*self._roles(messages), _PROBE], Sampling(temperature=0.0, min_p=0.0), 1
            )
            body["stream"] = False
            try:
                r = await self.client().post("/v1/chat/completions", json=body)
            except httpx.TransportError as e:
                await self._settle()
                if not self._alive():
                    raise CreatureDied(self.status()) from e
                raise BackendError(f"prefill failed: {e!r}") from e
            if r.status_code == 200:
                return int(r.json().get("timings", {}).get("prompt_n") or 0)
            err = _error_from(r.json().get("error", r.text))
            if attempt or not self._learn_strict(err):
                raise err
        raise AssertionError("unreachable")  # pragma: no cover

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
            try:
                return await self._render_count([*messages, _PROBE]) - await self._render_count(
                    [_PROBE]
                )
            except httpx.HTTPStatusError:
                # Strict templates (Gemma 3) refuse two user turns in a row: count the
                # messages alone, which adds the few template tokens of an empty chat.
                return await self._render_count(messages)
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

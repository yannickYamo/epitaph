"""The controller: the production life loop (BUILD_PLAN 5.8, 5.9, 6.3).

Two layers:

- `Life` is one life's loop on any clock and any backend: birth, reloads to the current
  keyframe (the memory cut, the slot handover, the echo), body knobs, recall, erosion,
  readings, the output pipeline (`pacing.speak`, which keeps the sync rule) and the death
  flush. The rehearsal (`rehearse.py`) runs it on a Pi-charged virtual clock.
- `Controller` runs lives back to back and survives every death: recovery of an unfinished
  life, the life counter (written first), transcripts, the deadline, the death squeeze and
  hang detection checked continuously (also during reloads, pauses and typing), silence,
  rebirth, model rotation, systemd watchdog pings in every state, and the control commands
  `status`, `new_life` and `screenshot`.

Every backend call goes through `GuardedBackend`, which turns a death declared by the
controller (deadline, hang, manual, full) into `CreatureDied` inside whatever the life is
waiting on, so the same death path runs whatever the creature was doing.

Clocks: `RealClock` on the Pi; `VirtualClock` (the fake clock for concurrent tasks) in the
simulator and the tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import random
import re
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, TypeVar

from epitaph.backend.base import Backend, BackendError, ContextFull, CreatureDied
from epitaph.body.base import Body
from epitaph.clock import LifeClock, Schedule
from epitaph.config import Config
from epitaph.costmodel import Costs
from epitaph.events import Event, make_event
from epitaph.mind.memory import Memory, approx_tokens
from epitaph.mind.prompt import (
    Lang,
    Persona,
    Reader,
    ReadingInput,
    load_lang,
    render_diary,
    speaks_raw,
)
from epitaph.mind.sampling import sampling_for
from epitaph.mind.sanitize import sanitize_text
from epitaph.pacing import Pacer, Spoken, life_seed, speak
from epitaph.state import LifeCounter, unfinished_lives, write_status
from epitaph.transcript import Transcript, close_interrupted
from epitaph.types import Cause, Chunk, CreatureStatus, Knobs, ModelSpec, Msg, Sampling

__all__ = [
    "Controller",
    "GuardedBackend",
    "HangLimits",
    "HangWatch",
    "Life",
    "LifeRecord",
    "ServerSlots",
    "SlotStore",
    "echo_head",
    "pick_model",
    "sd_notify",
    "slot_saved",
    "watchdog_interval",
]

log = logging.getLogger(__name__)
T = TypeVar("T")

EPS = 1e-6
WATCHDOG_MAX_S = 5.0  # BUILD_PLAN 5.10: pings in every state, at least this often
HANG_TICK_S = 1.0  # how often progress is read while a request is in flight


# ---------------------------------------------------------------------------------------
# systemd


def sd_notify(message: str, env: Mapping[str, str] | None = None) -> bool:
    """Send `message` ("READY=1", "WATCHDOG=1", ...) to systemd's NOTIFY_SOCKET.

    A tiny sd_notify(3) with no dependency: one datagram on the unix socket. Returns False
    when NOTIFY_SOCKET is unset (not under systemd) or the send fails.
    """
    addr = (os.environ if env is None else env).get("NOTIFY_SOCKET", "")
    if not addr:
        return False
    if addr.startswith("@"):  # an abstract socket
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(message.encode())
    except OSError as e:
        log.warning("sd_notify(%s) failed: %s", message, e)
        return False
    return True


def watchdog_interval(env: Mapping[str, str] | None = None) -> float:
    """Seconds between watchdog pings: half of WATCHDOG_USEC when set, never over 5 s."""
    raw = (os.environ if env is None else env).get("WATCHDOG_USEC", "")
    try:
        half = int(raw) / 2e6
    except ValueError:
        return WATCHDOG_MAX_S
    return max(0.1, min(WATCHDOG_MAX_S, half))


# ---------------------------------------------------------------------------------------
# the echo and the slot (ADR-026, ADR-014)

_FORMULA = ("i am", "i'm", "i’m")  # how most of its sentences open: no echo of those


def slot_saved(reply: object) -> bool:
    """Whether a llama-server slot save succeeded: its reply counts saved tokens.

    `_slot_action` answers failures with an error dict, never None, so only `n_saved` tells."""
    if not isinstance(reply, dict):
        return False
    fields: dict[str, object] = reply  # pyright: ignore[reportUnknownVariableType]
    return bool(fields.get("n_saved"))


def echo_head(thoughts: Sequence[str], words: int = 5) -> str | None:
    """The opening words of the oldest kept sentence that does not open on a formula.

    Most thoughts open with "I am still here"; echoing that teaches the formula back to it.
    Falls back to the oldest sentence long enough to continue; None if there is none."""
    sentences = [x.strip() for t in thoughts for x in re.split(r"(?<=[.!?])\s+", t) if x.strip()]
    long_enough = [x.split() for x in sentences if len(x.split()) > words]
    if not long_enough:
        return None
    fresh = [w for w in long_enough if not " ".join(w[:2]).lower().startswith(_FORMULA)]
    return " ".join((fresh or long_enough)[0][:words])


class SlotStore(Protocol):
    """Saves the server's cache slot around the echo, so the carried memory survives it."""

    async def save(self, name: str) -> bool:
        """Save the slot as `name`; False when it was not saved (then there is no echo)."""
        ...

    async def restore(self, name: str) -> None:
        """Restore the slot saved as `name` and drop the file."""
        ...


SlotAction = Callable[[str, str], Awaitable[object]]


class ServerSlots:
    """A SlotStore over llama-server's `/slots/0?action=save|restore` (`_slot_action`)."""

    def __init__(self, action: SlotAction, slot_dir: str | None = None) -> None:
        """`action(kind, name)` posts the slot action; `slot_dir` holds the saved files."""
        self.action = action
        self.slot_dir = slot_dir

    async def save(self, name: str) -> bool:
        """Save the slot; True when the server reports saved tokens."""
        return slot_saved(await self.action("save", name))

    async def restore(self, name: str) -> None:
        """Restore the slot, then delete its file from the slot directory."""
        try:
            await self.action("restore", name)
        finally:
            if self.slot_dir:
                (Path(self.slot_dir).expanduser() / name).unlink(missing_ok=True)


async def _led_by(prefix: str, stream: AsyncIterator[Chunk]) -> AsyncIterator[Chunk]:
    """`stream`, with `prefix` (the words the raw prompt ended with) shown first."""
    if prefix:
        yield Chunk(prefix)
    async for c in stream:
        yield c


# ---------------------------------------------------------------------------------------
# one life


Emit = Callable[[Event], None]
RateFn = Callable[[int, int, float], float]  # tokens/s at (step, threads, compute)


class Life:
    """The loop of BUILD_PLAN 5.8 for one life, with the real mind, on any clock and backend.

    Deaths reach it through `on_death` (the backend's callback, or `CreatureDied` in a
    thought) and `declare_death` (a death the controller decided); either emits `death`
    once, at the real moment. `step` returns False once it is dead.
    """

    def __init__(
        self,
        cfg: Config,
        clock: LifeClock,
        backend: Backend,
        body: Body,
        emit_to: Emit,
        *,
        life: int = 1,
        seed: int = 1,
        lang: Lang | None = None,
        model: ModelSpec | None = None,
        counter: Callable[[str], int] = approx_tokens,
        tg_rate: RateFn | None = None,
        slots: SlotStore | None = None,
        cause_of: Callable[[CreatureStatus], str] | None = None,
        ts: Callable[[], float] | None = None,
    ) -> None:
        """One life `life` of `cfg`'s profile, on `clock`, talking to `backend`.

        `counter` counts tokens for the memory; `tg_rate` gives the bench generation rate
        that seeds the cadence; `slots` keeps the carried memory around the echo (None:
        the echo runs without a save); `cause_of` names the cause of a backend death (the
        default reads the life clock); `ts` stamps events (default: wall time).
        """
        self.cfg = cfg
        self.clock = clock
        self.backend = backend
        self.body = body
        self.emit_to = emit_to
        self.sch = Schedule(cfg.profile)
        self.model = model or cfg.model()
        self.n = life
        self.seed = life_seed(seed, life)
        self.facts = body.facts()
        self.lang = lang or load_lang(str(cfg.get("prompt.language", "en")))
        self.persona = Persona.from_config(cfg, self.facts, self.lang)
        self.reader = Reader.from_config(cfg, self.lang)
        self.counter = counter
        self.tg_rate = tg_rate
        self.slots = slots
        self.cause_of = cause_of
        self.ts = ts
        # Made by `attach_memory` once the server runs: the marker is counted on its tokenizer.
        self.memory: Memory
        self.pacer = Pacer.from_config(cfg, clock, self.seed)
        self.trim_to = float(cfg.get("output.trim_to", 0.85))
        self.min_gap = float(cfg.get("life.min_reload_gap_s", 120))
        self.letters_per_token = float(cfg.get("estimate.letters_per_token", 3.5))
        self.oom_at = self.sch.death_s if str(cfg.get("body.death_mode", "oom")) == "oom" else None
        self.cur = (-1, -1)
        self.last_reload = -1e9
        self.reload_kf = 0.0  # the keyframe time whose target is loaded now
        self.reloaded = False
        self.echo: str | None = None
        self.turn = 0
        self.dead: str | None = None
        self.death_t: float | None = None
        self.last_tok_s: float | None = None
        self.born = False
        self.state = "birth"
        self.knobs: Knobs | None = None  # the knobs last applied to the body
        self.compute = 3.0  # cores' worth of compute now (for the hang limits)
        self.on_dead: Callable[[str], None] | None = None

    # -- events ----------------------------------------------------------------------------

    def emit(self, etype: str, /, **fields: Any) -> None:
        """Record an event with the life clock's `t` (0 before birth)."""
        e = make_event(etype, self.n, **fields)
        e["t"] = round(self.clock.elapsed(), 3) if self.born else 0.0
        if self.ts is not None:
            e["ts"] = round(self.ts(), 3)
        self.emit_to(e)

    def lived(self) -> float:
        """Seconds lived so far (0 before birth)."""
        return self.clock.elapsed() if self.born else 0.0

    def time_cause(self) -> str:
        """The cause the life clock implies: deadline, OOM (after the squeeze) or a crash."""
        t = self.lived()
        if t >= self.sch.lifespan_s - EPS:
            return Cause.DEADLINE.value
        if self.oom_at is not None and t >= self.oom_at - EPS:
            return Cause.OOM.value
        return Cause.CRASH.value

    def declare_death(self, cause: str) -> bool:
        """Emit `death` with `cause` now, once. Returns False when it was already dead."""
        if self.dead is not None:
            return False
        self.dead = cause
        self.death_t = self.lived()
        self.state = "dead"
        self.emit("death", cause=cause, lived_s=round(self.death_t, 1), model=self.model.name)
        if self.on_dead is not None:
            self.on_dead(cause)
        return True

    def on_death(self, status: CreatureStatus) -> None:
        """The creature died (any time, mid-request or between requests): emit `death` once."""
        if self.dead is not None:
            return
        cause = self.cause_of(status) if self.cause_of is not None else self.time_cause()
        self.declare_death(cause)

    def attach_memory(self) -> None:
        """A fresh memory with the current system prompt (call once the server runs)."""
        self.memory = Memory(self.counter, str(self.cfg.get("prompt.memory_gap_marker")))
        self.memory.set_system(self.persona.text)

    # -- hooks for the rehearsal -----------------------------------------------------------

    async def _load(self, quant: str, threads: int) -> None:
        """Start the creature for birth."""
        await self.backend.start(self.model, quant, threads)

    def _born(self) -> None:
        """Called right after birth, before the system prompt is read."""

    def _turn_started(self) -> None:
        """Called when a new turn number is taken."""

    def _full(self) -> None:
        """Called when the life ends because its context is full."""

    # -- stages of the loop ----------------------------------------------------------------

    def _system(self) -> list[Msg]:
        return [Msg("system", self.persona.text, kind="persona")] if self.persona.text else []

    def _set_share(self, compute: float) -> None:
        """Tell a backend that models speed (fake, rehearsal) the compute it has now."""
        self.compute = compute
        fn: Callable[[float], None] | None = getattr(self.backend, "set_cpu_share", None)
        if fn is not None:
            fn(compute)

    def rate_estimate(self, k: Knobs) -> None:
        """Seed the pacer's rate with the bench generation rate at these knobs (5.12)."""
        if self.tg_rate is not None:
            tg = self.tg_rate(k.step, k.threads, k.compute)
            self.pacer.set_rate_estimate(tg * self.letters_per_token)

    async def birth(self, k: Knobs, facts: dict[str, Any] | None = None) -> None:
        """Load step 0, start the clock, prefill the system prompt (the birth card)."""
        quant = self.model.quant(k.step)
        self.state = "birth"
        self.emit(
            "birth_loading",
            model=self.model.name,
            step=k.step,
            quant=quant,
            facts=facts or {},
            profile=self.cfg.profile.name,
            hardware=self.cfg.hardware,
            lifespan_s=self.sch.lifespan_s,
        )
        await self._load(quant, k.threads)
        self.clock.start()
        self.born = True
        self.state = "living"
        self.emit("birth", model=self.model.name, step=k.step, quant=quant, threads=k.threads)
        self.persona.update(k.persona_groups, k.mechanics)
        self.attach_memory()
        self.cur = (k.step, k.threads)
        self.compute = k.compute
        self.rate_estimate(k)
        self._born()
        await self.backend.prefill(self._system())

    def kill_time(self) -> float:
        """Life time of the scheduled death: the OOM squeeze, else the deadline."""
        return min(self.sch.lifespan_s, self.oom_at if self.oom_at is not None else 1e18)

    def skipped_reloads(self, t: float) -> list[float]:
        """Reload keyframes passed since the loaded target that the reload at t jumps over.

        A reload always loads the current keyframe's target (5.2); the earlier targets it
        passed (a long thought, a slow reload) are reported as `reload_skipped`.
        """
        passed = [r for r in self.sch.reload_times() if self.reload_kf < r <= t + EPS]
        if passed:
            self.reload_kf = passed[-1]
        return passed[:-1]

    async def reload(self, k: Knobs, t: float) -> None:
        """The reload is also a memory loss: cut, restart one step down, prefill, resume."""
        self.state = "reloading"
        for at in self.skipped_reloads(t):
            missed = self.sch.at(at)
            self.emit(
                "reload_skipped",
                skipped=1,
                at=round(at, 1),
                to=self.model.quant(missed.step),
                threads=missed.threads,
            )
        f = self.memory.cut_for_reload(k.recall, self.trim_to)
        self.emit(
            "reload",
            **{"from": self.model.quant(self.cur[0]), "to": self.model.quant(k.step)},
            threads=k.threads,
            recall_before=f.tokens_before,
            recall_after=f.tokens_after,
        )
        if f.items:
            self.emit("forget", items=f.items)
        t0 = self.clock.elapsed()
        await self.backend.start(self.model, self.model.quant(k.step), k.threads)
        self._set_share(k.compute)
        self.cur, self.last_reload, self.reloaded = (k.step, k.threads), t, True
        self.rate_estimate(k)
        await self.backend.prefill(self._system())
        if self.reader.material:
            self.echo = await self._echo()
        self.emit("reload_done", seconds=round(self.clock.elapsed() - t0, 1))
        self.state = "living"

    async def _echo(self) -> str | None:
        """One of its own kept sentences as the new, lower-precision weights now continue it.

        The opening words of a kept sentence (echo_head) are completed by the reloaded model with
        no prompt around them (raw completion, greedy), during the reload silence; the result
        is quoted in the next reading. Real: it is these weights, not a rewrite.
        """
        thoughts = [m.content for m in self.memory.past_messages() if m.role == "assistant"]
        head = echo_head(thoughts)
        if head is None:
            return None
        out = ""
        sampling = Sampling(temperature=0.0, min_p=0.0)
        # The raw completion replaces the server's single cache slot; keep the carried memory
        # by saving the slot first and restoring it afterwards (about 0.3 s each, spike S4b).
        # Without a successful save there is no echo: the restore could not bring it back.
        slot = f"echo-{self.model.name}.bin"
        if self.slots is not None and not await self.slots.save(slot):
            return None
        complete: Callable[[str, Sampling, int], AsyncIterator[Chunk]] = getattr(
            self.backend, "echo", self.backend.complete
        )
        try:
            async for chunk in complete(head, sampling, 18):
                if chunk.done:
                    break
                out += chunk.text
        except BackendError:
            out = ""  # the echo is an ornament: its failure never ends the life
        finally:
            if self.slots is not None:
                await self.slots.restore(slot)
        tail = " ".join(sanitize_text(out)[0].split()).split(". ")[0]
        return f"{head} {tail}".strip() if tail else None

    def prepare(self, t: float) -> tuple[Knobs, str]:
        """Everything before a request at life time t: body, forgetting, erosion, reading.

        Appends the reading to memory and emits forget, erosion and vitals. Returns the knobs
        and the reading.
        """
        k = self.sch.at(t)
        self.body.apply(k)
        self.knobs = k
        self._set_share(k.compute)
        if not self.cfg.profile.unbounded:
            f = self.memory.fit(k.recall, self.trim_to)
            if f.items:
                self.emit("forget", items=f.items)
        step = self.persona.update(k.persona_groups, k.mechanics)
        if step is not None:
            self.memory.set_system(self.persona.text)
            self.emit(
                "erosion", groups_left=step.groups_left, mechanics_present=step.mechanics_present
            )
        vit = self.body.vitals()
        forgotten = self.memory.take_forgotten()
        quotes = self.memory.take_forgotten_quotes()
        echo, self.echo = self.echo, None
        reading = self.reader.reading(
            ReadingInput(
                t=t,
                health=k.health.value,
                recall=k.recall,
                quant=self.model.quant(self.cur[0]),
                cores=k.cpu_share,
                cores_total=self.facts.cores,
                form=k.readings,
                forgotten=forgotten,
                reloaded=self.reloaded,
                tok_s=self.last_tok_s,
                cpu_c=vit.cpu_c,
                forgotten_quotes=quotes,
                echo=echo,
            )
        )
        self.reloaded = False
        self.turn += 1
        self._turn_started()
        self.memory.append_host(reading, self.turn)
        self.emit(
            "vitals",
            phase=k.phase,
            health=k.health.value,
            recall=k.recall,
            recall_used=self.memory.used(),
            forgotten_since_last=forgotten,
            step=self.cur[0],
            quant=self.model.quant(self.cur[0]),
            threads=self.cur[1],
            cpu_share=k.cpu_share,
            cpu_mhz=k.cpu_mhz,
            cores_effective=k.cpu_share,
            tok_s=self.last_tok_s,
            cpu_c=vit.cpu_c,
            ram_limit_mb=vit.ram_limit_mb,
            reading=reading,
            marker=self.memory.gap,
        )
        return k, reading

    def _sampling(self, k: Knobs) -> Sampling:
        return sampling_for(
            self.cfg.section("sampling"), k, self.cur[0], seed=self.seed * 1000 + self.turn
        )

    async def thought(self, t: float) -> Spoken:
        """One turn: prepare, speak (generation and typing on the life clock), remember."""
        k, _ = self.prepare(t)
        msgs = self.memory.messages()
        sampling = self._sampling(k)
        self.emit("thought_start", turn=self.turn)

        prompt = self.cfg.section("prompt")
        raw = speaks_raw(prompt, self.persona.text)
        prefix = str(prompt.get("raw_prefix", "")) if raw else ""

        def stream() -> AsyncIterator[Chunk]:
            if raw:
                text = render_diary(msgs) + prefix
                return _led_by(prefix, self.backend.complete(text, sampling, k.max_tokens))
            return self.backend.chat(msgs, sampling, k.max_tokens)

        spoken = await speak(self.pacer, stream, k, self.turn, self.emit, self.on_death)
        self.memory.append_thought([w.text for w in spoken.words])
        self.emit("thought_end", turn=self.turn, text=spoken.text)
        rate = self.backend.status().tok_s
        self.last_tok_s = rate if rate else self.last_tok_s
        return spoken

    async def step(self) -> bool:
        """One pass of the loop: a reload if due, then a thought. False once the life is over."""
        if self.dead is not None:
            return False
        t = self.clock.elapsed()
        k = self.sch.at(t)
        if (k.step, k.threads) != self.cur and t - self.last_reload >= self.min_gap:
            await self.reload(k, t)
            t = self.clock.elapsed()
        if self.dead is not None:
            return False
        if self.cfg.profile.unbounded:
            k = self.sch.at(t)
            need = self.reading_tokens_estimate(k)
            if not self.memory.fits(self.cfg.ctx, k.max_tokens, need):
                self._full()
                self.declare_death(Cause.FULL.value)
                return False
        await self.thought(t)
        return self.dead is None

    def reading_tokens_estimate(self, k: Knobs) -> int:
        """Tokens a reading in this form will take (the unbounded context check)."""
        forms = dict(self.cfg.get("estimate.reading_tokens", {}) or {})
        return int(forms.get(k.readings, 45))


# ---------------------------------------------------------------------------------------
# guarding the backend: deaths the controller declares, and hangs (BUILD_PLAN 5.9)


@dataclass(frozen=True)
class HangLimits:
    """How long a request in flight may go without progress before it is a hang (5.9)."""

    load_s: float = 300.0
    token_gap_s: float = 90.0
    first_token_factor: float = 3.0
    first_token_extra_s: float = 60.0

    @classmethod
    def from_config(cls, cfg: Config) -> HangLimits:
        """`life.load_timeout_s` and `body.token_gap_timeout_s`."""
        return cls(
            load_s=float(cfg.get("life.load_timeout_s", 300)),
            token_gap_s=float(cfg.get("body.token_gap_timeout_s", 90)),
        )

    def first_token(self, prompt_tokens: int, pp_tok_s: float) -> float:
        """Prompt tokens / prompt speed x 3 + 60 s."""
        pp = max(pp_tok_s, 1e-3)
        return prompt_tokens / pp * self.first_token_factor + self.first_token_extra_s


class HangWatch:
    """Tracks the request in flight and the last moment it made progress.

    Progress is a new token or any rise in the body's counters (CPU time, read I/O, major
    faults); `check` is fed the counters and says when the limit has passed without any.
    """

    def __init__(self, now: Callable[[], float]) -> None:
        """Measure with `now` (monotonic seconds)."""
        self.now = now
        self.phase: str | None = None
        self.limit = math.inf
        self.last = 0.0
        self._counters: object = None

    def begin(self, phase: str, limit: float) -> None:
        """A request starts: `phase` ("load", "prompt") may go `limit` s without progress."""
        self.phase, self.limit, self.last = phase, limit, self.now()

    def token(self, gap_s: float) -> None:
        """A token arrived: progress, and from now on the gap limit applies."""
        self.phase, self.limit, self.last = "tokens", gap_s, self.now()

    def end(self) -> None:
        """No request in flight: nothing can hang."""
        self.phase = None

    def check(self, counters: object) -> bool:
        """True when a request has gone past its limit with no progress."""
        if counters != self._counters:
            self._counters = counters
            self.last = self.now()
            return False
        if self.phase is None:
            return False
        return self.now() - self.last > self.limit


class GuardedBackend:
    """A Backend that raises `CreatureDied` as soon as `died` is set, and feeds a HangWatch.

    Every call races the inner call against the death event, so a death the controller
    declares (deadline, hang, manual, full) interrupts a load, a prefill or a stream at once,
    and the life's ordinary death path runs (the death flush included).
    """

    def __init__(
        self,
        inner: Backend,
        died: asyncio.Event,
        watch: HangWatch,
        limits: HangLimits,
        pp_rate: Callable[[], float],
    ) -> None:
        """Guard `inner`; `pp_rate` is the prompt speed now (tokens/s) for the limits."""
        self.inner = inner
        self.died = died
        self.watch = watch
        self.limits = limits
        self.pp_rate = pp_rate
        self._waiter: asyncio.Future[Any] | None = None

    def _dead(self) -> CreatureDied:
        return CreatureDied(self.inner.status())

    async def _race(self, coro: Coroutine[Any, Any, T]) -> T:
        if self.died.is_set():
            coro.close()
            raise self._dead()
        if self._waiter is None or self._waiter.done():
            self._waiter = asyncio.ensure_future(self.died.wait())
        task = asyncio.ensure_future(coro)
        done, _ = await asyncio.wait({task, self._waiter}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return task.result()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        raise self._dead()

    def close(self) -> None:
        """Drop the death waiter (end of the life)."""
        if self._waiter is not None:
            self._waiter.cancel()
            self._waiter = None

    async def start(self, model: ModelSpec, quant: str, threads: int) -> None:
        """Start the inner creature; a load without progress for `load_s` is a hang."""
        self.watch.begin("load", self.limits.load_s)
        try:
            await self._race(self.inner.start(model, quant, threads))
        finally:
            self.watch.end()

    async def stop(self, hard: bool = False) -> None:
        """Stop the inner creature (never interrupted)."""
        await self.inner.stop(hard)

    async def prefill(self, messages: list[Msg]) -> int:
        """Prefill, limited like a first token."""
        n = sum(approx_tokens(m.content) for m in messages)
        self.watch.begin("prompt", self.limits.first_token(n, self.pp_rate()))
        try:
            return await self._race(self.inner.prefill(messages))
        finally:
            self.watch.end()

    def chat(
        self, messages: list[Msg], sampling: Sampling, max_tokens: int
    ) -> AsyncIterator[Chunk]:
        """Stream a thought; the first token and every gap are limited."""
        n = sum(approx_tokens(m.content) for m in messages)
        return self._stream(lambda: self.inner.chat(messages, sampling, max_tokens), n)

    def complete(self, prompt: str, sampling: Sampling, max_tokens: int) -> AsyncIterator[Chunk]:
        """Stream a raw completion, guarded like `chat`."""
        n = approx_tokens(prompt)
        return self._stream(lambda: self.inner.complete(prompt, sampling, max_tokens), n)

    async def _stream(
        self, make: Callable[[], AsyncIterator[Chunk]], prompt_tokens: int
    ) -> AsyncIterator[Chunk]:
        if self.died.is_set():
            raise self._dead()
        self.watch.begin("prompt", self.limits.first_token(prompt_tokens, self.pp_rate()))
        it = make().__aiter__()

        async def nxt() -> Chunk | None:
            try:
                return await it.__anext__()
            except StopAsyncIteration:
                return None

        try:
            while True:
                chunk = await self._race(nxt())
                if chunk is None:
                    return
                if chunk.done:
                    self.watch.end()
                else:
                    self.watch.token(self.limits.token_gap_s)
                yield chunk
        finally:
            self.watch.end()
            aclose: Callable[[], Awaitable[None]] | None = getattr(it, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()

    async def count_past_tokens(self, messages: list[Msg]) -> int:
        """Tokens of these messages, from the inner backend."""
        return await self.inner.count_past_tokens(messages)

    def on_death(self, fn: Callable[[CreatureStatus], None]) -> None:
        """Register on the inner backend."""
        self.inner.on_death(fn)

    def status(self) -> CreatureStatus:
        """The inner backend's status."""
        return self.inner.status()

    def set_cpu_share(self, share: float) -> None:
        """Pass the CPU share on to a backend that models speed."""
        fn: Callable[[float], None] | None = getattr(self.inner, "set_cpu_share", None)
        if fn is not None:
            fn(share)


# ---------------------------------------------------------------------------------------
# the controller


@dataclass
class LifeRecord:
    """How one life ended."""

    life: int
    model: str
    cause: str
    thoughts: int
    lived_s: float
    words: int = 0


def pick_model(cfg: Config, index: int, seed: int = 0) -> ModelSpec:
    """The model of the `index`-th life born by this controller (0-based), per `life.rotation`.

    round_robin walks `life.models`; random draws from it (seeded); fixed keeps the first.
    """
    listed: list[object] = list(cfg.get("life.models", []) or [])
    names = [str(x) for x in listed] or [cfg.model().name]
    rotation = str(cfg.get("life.rotation", "round_robin"))
    if rotation == "random":
        name = random.Random(seed * 7919 + index).choice(names)
    elif rotation == "fixed":
        name = names[0]
    else:
        name = names[index % len(names)]
    return cfg.model(name)


BackendFor = Callable[[int, ModelSpec], Backend]
Reconfigure = Callable[[str | None, float | None], Config]


@dataclass
class _NewLife:
    profile: str | None = None
    lifespan: float | None = None
    model: str | None = None


@dataclass
class _Current:
    life: Life
    guard: GuardedBackend
    transcript: Transcript | None
    words: int = 0
    last_line: str = ""
    history: list[str] = field(default_factory=lambda: [])


class Controller:
    """Lives back to back on one machine; survives every death (BUILD_PLAN 5.8)."""

    def __init__(
        self,
        cfg: Config,
        *,
        clock: LifeClock,
        backend_for: BackendFor,
        body: Body,
        costs_for: Callable[[ModelSpec], Costs],
        publish: Emit | None = None,
        state_dir: Path | None = None,
        lives: int | None = None,
        seed: int | None = None,
        slots_for: Callable[[Backend], SlotStore | None] | None = None,
        notify: Callable[[str], object] | None = None,
        watchdog_s: float = WATCHDOG_MAX_S,
        ts: Callable[[], float] | None = None,
        reconfigure: Reconfigure | None = None,
        counter: Callable[[str], int] = approx_tokens,
    ) -> None:
        """Run `cfg`'s profile.

        `backend_for(n, model)` gives the creature for life n; `costs_for(model)` its bench
        costs (cadence estimate, hang limits). `publish` receives every event (the bus);
        `state_dir` holds the counter, transcripts and status (None: in memory only).
        `lives` stops after that many births (None: forever). `notify` is sd_notify;
        `reconfigure(profile, lifespan)` builds the config of a `new_life` with overrides.
        """
        self.cfg = cfg
        self.clock = clock
        self.backend_for = backend_for
        self.body = body
        self.costs_for = costs_for
        self.publish = publish
        self.state_dir = state_dir
        self.lives = lives
        base = int(cfg.get("life.seed", 0) or 0)
        self.seed = base if seed is None else seed
        self.slots_for = slots_for
        self.notify = notify
        self.watchdog_s = min(WATCHDOG_MAX_S, watchdog_s)
        self.ts = ts
        self.reconfigure = reconfigure
        self.counter = counter
        self.limits = HangLimits.from_config(cfg)
        self.life_counter = LifeCounter(state_dir) if state_dir is not None else None
        self._mem_count = 0
        self.records: list[LifeRecord] = []
        self.state = "recover"
        self.cur: _Current | None = None
        self.kill_cause: Cause | None = None
        self.squeezed = False
        self.pings = 0
        self.ping_times: list[float] = []
        self._registered: set[int] = set()
        self._next: _NewLife | None = None
        self._wake = asyncio.Event()
        self._last_ping = -math.inf
        self._loop_time: Callable[[], float] = time.monotonic

    # -- events ----------------------------------------------------------------------------

    def _emit(self, e: Event) -> None:
        cur = self.cur
        if cur is not None:
            if e["type"] == "word":
                cur.words += 1
            elif e["type"] == "thought_end":
                cur.last_line = str(e.get("text", ""))
            if cur.transcript is not None:
                cur.transcript.write(e)
        if self.publish is not None:
            self.publish(e)

    def _event(self, etype: str, n: int, t: float, **fields: Any) -> None:
        e = make_event(etype, n, **fields)
        e["t"] = round(t, 3)
        if self.ts is not None:
            e["ts"] = round(self.ts(), 3)
        self._emit(e)

    def _status(self) -> dict[str, Any]:
        cur = self.cur
        out: dict[str, Any] = {
            "state": self.state,
            "pid": os.getpid(),
            "lives_run": len(self.records),
            "last": asdict(self.records[-1]) if self.records else None,
            "pings": self.pings,
        }
        if cur is not None:
            life = cur.life
            out.update(
                life=life.n,
                model=life.model.name,
                profile=life.cfg.profile.name,
                lifespan_s=life.sch.lifespan_s,
                t=round(life.lived(), 1),
                turn=life.turn,
                quant=life.model.quant(max(0, life.cur[0])),
                phase=life.knobs.phase if life.knobs is not None else "",
                health=life.knobs.health.value if life.knobs is not None else "",
                dead=life.dead,
            )
        return out

    def _save_status(self) -> None:
        if self.state_dir is not None:
            try:
                write_status(self.state_dir, self._status())
            except OSError as e:
                log.warning("could not write status.json: %s", e)

    def _set_state(self, state: str) -> None:
        self.state = state
        self._save_status()

    # -- control channel -------------------------------------------------------------------

    async def ctl_status(self, args: dict[str, Any]) -> dict[str, Any]:
        """`ctl status`: the controller's state and the current life."""
        return self._status()

    async def ctl_new_life(self, args: dict[str, Any]) -> dict[str, Any]:
        """`ctl new_life {lifespan?, profile?, model?}`: end this life (cause=manual) and
        start the next one now, with these settings for that one life."""
        lifespan = args.get("lifespan")
        nxt = _NewLife(
            profile=str(args["profile"]) if args.get("profile") else None,
            lifespan=float(lifespan) if lifespan is not None else None,
            model=str(args["model"]) if args.get("model") else None,
        )
        if nxt.model is not None:
            self.cfg.model(nxt.model)  # raises (an error reply) for an unknown model
        if (nxt.profile or nxt.lifespan is not None) and self.reconfigure is None:
            raise ValueError("this controller cannot change the profile or lifespan")
        self._next = nxt
        ending = None
        if self.cur is not None and self.cur.life.dead is None:
            ending = self.cur.life.n
            self.kill(Cause.MANUAL)
        self._wake.set()  # cut a silence short
        return {"ok": True, "ending": ending}

    async def ctl_screenshot(self, args: dict[str, Any]) -> dict[str, Any]:
        """`ctl screenshot`: ask the displays for a screenshot (a `screenshot` event)."""
        n = self.cur.life.n if self.cur is not None else 0
        name = f"{n:06d}-{int(time.time())}.png"
        root = self.state_dir if self.state_dir is not None else Path(".")
        path = str(args.get("path") or root / "screenshots" / name)
        self._event("screenshot", n, self.cur.life.lived() if self.cur else 0.0, path=path)
        return {"requested": path}

    def attach(self, bus: Any) -> None:
        """Register the control commands on an `EventBus`."""
        bus.on("status", self.ctl_status)
        bus.on("new_life", self.ctl_new_life)
        bus.on("screenshot", self.ctl_screenshot)

    # -- deaths ----------------------------------------------------------------------------

    def kill(self, cause: Cause) -> None:
        """Kill the creature now for `cause` (deadline, hang, manual, full)."""
        cur = self.cur
        if cur is None or cur.life.dead is not None:
            return
        self.kill_cause = cause
        try:
            self.body.kill_now(cause)
        except Exception as e:  # the death must happen even if the cgroup is unwell
            log.error("life %d: body.kill_now failed: %s", cur.life.n, e)
        cur.life.declare_death(cause.value)
        cur.guard.died.set()

    def _cause_of(self, status: CreatureStatus) -> str:
        if self.kill_cause is not None:
            return self.kill_cause.value
        return self.body.death_cause(status).value

    def _on_backend_death(self, status: CreatureStatus) -> None:
        cur = self.cur
        if cur is None:
            return
        cur.life.on_death(status)
        cur.guard.died.set()

    def _on_dead(self, cause: str) -> None:
        if self.cur is not None:
            self.cur.guard.died.set()
        self._set_state("dead")

    # -- the supervisor: watchdog, deadline, squeeze, hangs ---------------------------------

    def ping(self) -> None:
        """One watchdog keepalive."""
        now = self._loop_time()
        self._last_ping = now
        self.pings += 1
        self.ping_times.append(now)
        if self.notify is not None:
            self.notify("WATCHDOG=1")

    async def _supervise(self) -> None:
        """Runs in every state: pings, and while alive the deadline, squeeze and hangs."""
        while True:
            now = self._loop_time()
            if now - self._last_ping >= self.watchdog_s - EPS:
                self.ping()
            wait = self._last_ping + self.watchdog_s - self._loop_time()
            cur = self.cur
            if cur is not None and cur.life.dead is None:
                wait = min(wait, self._check(cur))
            await asyncio.sleep(max(wait, 0.0))

    def _check(self, cur: _Current) -> float:
        """Kill on the deadline or a hang, squeeze on time; returns the seconds to the next
        check that matters."""
        life = cur.life
        wait = math.inf
        if life.born:
            t = life.lived()
            if t >= life.sch.lifespan_s - EPS:
                self.kill(Cause.DEADLINE)
                return wait
            wait = life.sch.lifespan_s - t
            if life.oom_at is not None and not self.squeezed:
                if t >= life.oom_at - EPS:
                    self.squeeze(life)
                else:
                    wait = min(wait, life.oom_at - t)
        if cur.guard.watch.phase is not None:
            if cur.guard.watch.check(self.body.progress()):
                log.warning("life %d: no progress in %s, a hang", life.n, cur.guard.watch.phase)
                self.kill(Cause.HANG)
                return math.inf
            wait = min(wait, HANG_TICK_S)
        return wait

    def squeeze(self, life: Life) -> None:
        """Take the creature's RAM at `end-0:30` (the death squeeze), whatever it is doing."""
        self.squeezed = True
        k = life.knobs or life.sch.at(life.lived())
        self.body.apply(replace(k, death_squeeze=True))

    # -- lives -----------------------------------------------------------------------------

    def recover(self) -> list[int]:
        """Close every life folder without a death record as `interrupted`; reset the cgroup."""
        closed: list[int] = []
        if self.state_dir is not None:
            for d in unfinished_lives(self.state_dir):
                rec = close_interrupted(d)
                closed.append(int(rec.get("life", 0)))
                log.info("life %s closed as %s on recovery", d.name, rec.get("cause"))
        self.body.reset_creature_cgroup()
        return closed

    def _next_number(self) -> int:
        if self.life_counter is not None:
            return self.life_counter.next()
        self._mem_count += 1
        return self._mem_count

    def _life_config(self) -> tuple[Config, str | None]:
        nxt, self._next = self._next, None
        if nxt is None:
            return self.cfg, None
        cfg = self.cfg
        if (nxt.profile or nxt.lifespan is not None) and self.reconfigure is not None:
            cfg = self.reconfigure(nxt.profile, nxt.lifespan)
        return cfg, nxt.model

    def _register(self, backend: Backend) -> None:
        if id(backend) not in self._registered:
            self._registered.add(id(backend))
            backend.on_death(self._on_backend_death)

    async def live_one(self, index: int) -> LifeRecord:
        """Birth, the loop, death from any cause, the death flush and the death record."""
        cfg, model_name = self._life_config()
        n = self._next_number()  # the counter is written first (atomic write + fsync)
        model = cfg.model(model_name) if model_name else pick_model(cfg, index, self.seed)
        costs = self.costs_for(model)
        inner = self.backend_for(n, model)
        self._register(inner)
        self.kill_cause = None
        self.squeezed = False
        died = asyncio.Event()
        watch = HangWatch(self._loop_time)
        holder: list[Life] = []

        def pp_rate() -> float:
            life = holder[0]
            step, threads = life.cur if life.cur[0] >= 0 else (0, 3)
            return costs.pp(step, threads, life.compute)

        guard = GuardedBackend(inner, died, watch, self.limits, pp_rate)
        transcript = Transcript(self.state_dir, n) if self.state_dir is not None else None
        life = Life(
            cfg,
            self.clock,
            guard,
            self.body,
            self._emit,
            life=n,
            seed=self.seed,
            model=model,
            counter=self.counter,
            tg_rate=costs.tg,
            slots=self.slots_for(inner) if self.slots_for is not None else None,
            cause_of=self._cause_of,
            ts=self.ts,
        )
        holder.append(life)
        life.on_dead = self._on_dead
        self.cur = _Current(life, guard, transcript)
        if transcript is not None:
            transcript.open(
                {
                    "life": n,
                    "model": model.name,
                    "profile": cfg.profile.name,
                    "hardware": cfg.hardware,
                    "lifespan_s": life.sch.lifespan_s,
                    "seed": life.seed,
                }
            )
        self._set_state("birth")
        try:
            await life.birth(life.sch.at(0), facts=asdict(life.facts))
            self._set_state("living")
            while await life.step():
                self._save_status()
        except CreatureDied as e:
            life.on_death(e.status)
        except ContextFull:
            life.declare_death(Cause.FULL.value)  # the unbounded life's end (5.3)
        except TimeoutError as e:  # the load never became healthy
            life.emit("error", where="backend", message=str(e))
            life.declare_death(Cause.HANG.value)
        except BackendError as e:
            life.emit("error", where="backend", message=str(e))
            life.declare_death(Cause.CRASH.value)
        except Exception as e:  # a bug must not leave a half-dead life behind
            log.exception("life %d: the loop failed", n)
            life.emit("error", where="controller", message=repr(e))
            life.declare_death(Cause.CRASH.value)
        if life.dead is None:  # the loop only stops once it is dead; never trust it
            life.declare_death(life.time_cause())
        guard.close()
        await self._stop_creature(inner)
        cur = self.cur
        life.emit("death_shown", last_line=cur.last_line, words_total=cur.words)
        rec = LifeRecord(
            n,
            model.name,
            life.dead or Cause.CRASH.value,
            life.turn,
            round(life.death_t or 0.0, 1),
            cur.words,
        )
        if transcript is not None:
            transcript.close(asdict(rec) | {"last_line": cur.last_line})
        self.records.append(rec)
        self.body.reset_creature_cgroup()
        return rec

    async def _stop_creature(self, backend: Backend) -> None:
        """Nothing is left running after a death."""
        try:
            await backend.stop(hard=True)
        except Exception as e:
            log.error("stopping the creature failed: %s", e)

    async def silence(self, n: int, t: float, sleep: bool) -> None:
        """The silence between lives; `ctl new_life` cuts it short."""
        seconds = float(self.cfg.get("life.silence_seconds", 90))
        style = str(self.cfg.get("display.silence_style", "dark"))
        self._set_state("silence")
        self._event("silence", n, t, seconds=seconds, style=style)
        if not sleep:
            return
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), seconds)

    async def run(self) -> list[LifeRecord]:
        """Recover, then live, die and be reborn until `lives` lives are done (or forever)."""
        loop = asyncio.get_running_loop()
        self._loop_time = loop.time
        sup = asyncio.ensure_future(self._supervise())
        try:
            self._set_state("recover")
            self.recover()
            if self.notify is not None:
                self.notify("READY=1")
            index = 0
            while self.lives is None or index < self.lives:
                rec = await self.live_one(index)
                index += 1
                more = self.lives is None or index < self.lives
                t = self.cur.life.lived() if self.cur is not None else rec.lived_s
                await self.silence(rec.life, t, sleep=more)
            self._set_state("stopped")
            return self.records
        finally:
            sup.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sup
            if self.cur is not None:
                self.cur.guard.close()

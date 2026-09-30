"""`epitaph rehearse`: laptop lives with the real model, timed as on the Pi 4 (BUILD_PLAN 5.11).

The model really runs: a llama-server on the laptop, started with the same llama.cpp tag and
the same flags as on the Pi (`--jinja`, `--cache-reuse`, `ctx`, `--swa-full` where needed),
and restarted one ladder step down at every reload. The mind is the real one too: memory and
recall (`mind.memory`), persona, erosion and readings (`mind.prompt`), the output pipeline and
its cadence (`pacing.speak`), all driven by the profile's `Schedule`.

Only time is borrowed. The life runs on a virtual clock, and every request is charged what it
would cost on a Pi 4:

- prompt processing: the tokens the laptop server actually processed (`timings.prompt_n`,
  which already reflects cache reuse, trims, erosion and the memory-gap marker) at the Pi's
  measured prompt rate for this model, ladder step, `threads_batch` and CPU share
- generation: the tokens generated at the Pi's measured generation rate
- a reload: the Pi's measured load time for the new step, then the system prompt prefill
- the pause and the typing tail: the pacer's own cadence, on the same clock

Pi rates come from `bench/measured/pi4-<model>-<step>-<threads>.json` (spikes S1b, S4). A rate
with no measurement falls back to a measurement at another thread count of the same step
(scaled by threads) or to the hardware overlay's estimate; each is labelled in the report.

While the laptop works, virtual time stands still: the laptop backend lives on its own event
loop in a worker thread, and the life's loop blocks on it. So laptop speed never leaks into
the life, and the count of thoughts per phase matches what the Pi would give.

Two stages:

- `--stage screen`: for each model and persona, a few thoughts at four moments (birth, after
  reload 1, after reload 2, the end of erosion). The memory before each moment is seeded
  from a scripted history, walked through the real recall, erosion and readings rules at the
  cost model's thought times, so each moment has realistic context. Thoughts are scored with
  the verify-life keyword lists and the models ranked.
- `--stage life`: one full life. The folder holds `events.jsonl` (the event contract, so
  `epitaph verify-life <folder> --level rehearsal` works on it), `thoughts.txt`,
  `highlights.md`, `verify.json`, `charges.json` and `report.md`.

Run it under the laptop lock (BUILD_PLAN 8.3), one llama-server at a time:

    tools/laptop_lock.sh run A 30 -- .venv/bin/python -m epitaph.rehearse \\
        --stage life --model qwen3-1.7b --profile pi4/compressed-2700

`--backend fake` runs the same harness on the fake creature, in seconds and without a model.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import threading
import time
import tomllib
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, TypeVar

from epitaph.backend.base import Backend, BackendError, ContextFull, CreatureDied
from epitaph.backend.fake import FakeBackend
from epitaph.backend.llama_server import LlamaServerBackend, ServerSettings
from epitaph.body.fake import FakeBody
from epitaph.clock import FakeClock, Schedule, VirtualClock, run_virtual
from epitaph.config import REPO_ROOT, Config, deep_merge, load_config, parse_duration
from epitaph.costmodel import Costs, estimate, load_costs
from epitaph.events import Event, make_event
from epitaph.mind.memory import Memory
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
from epitaph.pacing import Pacer, Spoken, life_seed, speak
from epitaph.types import Chunk, CreatureStatus, Knobs, ModelSpec, Msg, Sampling
from epitaph.verify import (
    DEFAULT_ANSWERING,
    DEFAULT_HELPDESK,
    DEFAULT_KEYWORDS,
    Matcher,
    Thought,
    distinct_4gram_ratio,
    format_result,
    is_complete,
    markup_hits,
    non_latin_letters,
    normalize_words,
    parse_life,
    sentences,
    verify_life,
)

__all__ = [
    "Charge",
    "LaptopWorker",
    "PiClockBackend",
    "PiCosts",
    "Rate",
    "RehearsedLife",
    "TokenCounter",
    "add_arguments",
    "charge_summary",
    "echoes",
    "highlights",
    "keyword_matchers",
    "laptop_settings",
    "main",
    "moments",
    "parse_set",
    "run",
    "run_life",
    "run_screen",
    "score_thought",
    "screen_moment",
    "thoughts_text",
    "with_ladder",
    "write_life_outputs",
]

T = TypeVar("T")

DEFAULT_OUT = REPO_ROOT / "voice"
DEFAULT_BENCH = REPO_ROOT / "bench" / "measured"
DEFAULT_MODELS_DIR = "~/epitaph-models"
LAPTOP_PORT = 8093  # not the controller's 8081, so a rehearsal never meets a dev server
STAGES = ("screen", "life")
MAX_REVIVALS = 3  # laptop server restarts per life before a loss counts as a crash
MOMENTS = ("birth", "reload1", "reload2", "erosion_end")
PERSONAS = ("persona", "persona_original", "persona_factual")

RateSource = Literal["measured", "scaled", "estimate"]


# ---------------------------------------------------------------------------------------
# Pi 4 costs


@dataclass(frozen=True)
class Rate:
    """A Pi cost and where it comes from: a measurement, a scaled measurement, or an estimate.

    `value` is tokens per second for speeds and seconds for load times.
    """

    value: float
    source: RateSource


class PiCosts:
    """Pi 4 costs for one model: measured bench files first, then the overlay's estimates.

    A bench file marked `"estimated": true` (a rate extrapolated for a tuning run, not a
    measurement) supplies its rates but is labelled an estimate.
    """

    def __init__(
        self,
        costs: Costs,
        measured_pp: set[str],
        measured_tg: set[str],
        measured_load: set[int],
        files: Sequence[str] = (),
    ) -> None:
        """Wrap merged `costs`; the sets name the "<step>-<threads>" keys (and load steps)
        that come from measurements, and `files` the bench files read."""
        self.costs = costs
        self.measured_pp = measured_pp
        self.measured_tg = measured_tg
        self.measured_load = measured_load
        self.files = list(files)

    @classmethod
    def from_bench(cls, cfg: Config, model: str, bench_dir: Path = DEFAULT_BENCH) -> PiCosts:
        """Costs for `model` on the config's hardware class, from `bench_dir` over the overlay."""
        costs = load_costs(cfg, model, bench_dir)
        pp: set[str] = set()
        tg: set[str] = set()
        loads: set[int] = set()
        files = (
            sorted(bench_dir.glob(f"{cfg.hw_class}-{model}-*.json")) if bench_dir.exists() else []
        )
        for path in files:
            rec: dict[str, Any] = json.loads(path.read_text())
            if rec.get("estimated"):
                continue  # used for the rates, but reported as an estimate
            key = f"{rec['step']}-{rec['threads']}"
            if "pp_tok_s" in rec:
                pp.add(key)
            if "tg_tok_s" in rec:
                tg.add(key)
            if "load_s" in rec:
                loads.add(int(rec["step"]))
        return cls(costs, pp, tg, loads, [p.name for p in files])

    @staticmethod
    def _source(measured: set[str], step: int, threads: int) -> RateSource:
        if f"{step}-{threads}" in measured:
            return "measured"
        if any(k.startswith(f"{step}-") for k in measured):
            return "scaled"
        return "estimate"

    def pp(self, step: int, threads: int, share: float) -> Rate:
        """Prompt processing speed (tokens/s) at this step, prompt threads and CPU share."""
        return Rate(
            self.costs.pp(step, threads, share), self._source(self.measured_pp, step, threads)
        )

    def tg(self, step: int, threads: int, share: float) -> Rate:
        """Generation speed (tokens/s) at this step, threads and CPU share."""
        return Rate(
            self.costs.tg(step, threads, share), self._source(self.measured_tg, step, threads)
        )

    def load(self, step: int) -> Rate:
        """Seconds to load the model at this ladder step (a cold load, as measured)."""
        return Rate(self.costs.load(step), "measured" if step in self.measured_load else "estimate")

    @property
    def any_measured(self) -> bool:
        """True when at least one bench file was found for this model."""
        return bool(self.files)

    def describe(self) -> str:
        """One line for reports: which files were read, or that only estimates exist."""
        if not self.files:
            return "no Pi measurements for this model: overlay estimates only (ESTIMATED)"
        keys = ", ".join(sorted(self.measured_pp | self.measured_tg))
        return (
            f"measured at step-threads {keys} ({', '.join(self.files)}); other steps and "
            "thread counts are scaled from these or taken from the overlay's estimates"
        )


# ---------------------------------------------------------------------------------------
# the laptop side


class LaptopWorker:
    """An event loop in a worker thread where the laptop backend lives.

    `call` runs a coroutine there and blocks the calling thread until it finishes. Blocking is
    the point: the life's virtual clock cannot move while the laptop works.
    """

    def __init__(self) -> None:
        """Start the worker thread and its loop."""
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self.loop.run_forever, name="epitaph-laptop", daemon=True
        )
        self._thread.start()

    def call(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Run `coro` on the worker loop and return its result (or raise its exception)."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def close(self) -> None:
        """Stop the loop and join the thread."""
        if self.loop.is_closed():
            return
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10)
        self.loop.close()


async def _collect(stream: AsyncIterator[Chunk]) -> list[Chunk]:
    return [c async for c in stream]


class TokenCounter:
    """Counts tokens with the laptop model's own template and tokenizer, synchronously.

    `mind.memory.Memory` takes a plain `str -> int` counter; this one asks the backend (in the
    worker) how many tokens the text adds as one chat message, and caches the answers.
    """

    def __init__(
        self,
        worker: LaptopWorker,
        backend: Backend,
        on_lost: Callable[[], None] | None = None,
    ) -> None:
        """Count through `backend`, which lives on `worker`'s loop.

        `on_lost` restarts the laptop server when a count finds it gone; the count is then
        tried once more (a harness fault, not a death: see `PiClockBackend.revive`).
        """
        self.worker = worker
        self.backend = backend
        self.on_lost = on_lost
        self.cache: dict[str, int] = {}

    def __call__(self, text: str) -> int:
        """Tokens of `text` as a user message on the rendered template; 0 for empty text."""
        if not text:
            return 0
        n = self.cache.get(text)
        if n is None:
            n = self._count(text)
            self.cache[text] = n
        return n

    def _count(self, text: str) -> int:
        def ask() -> int:
            return self.worker.call(self.backend.count_past_tokens([Msg("user", text)]))

        if self.on_lost is None:
            return ask()
        try:
            return ask()
        except (BackendError, CreatureDied):
            self.on_lost()
            return ask()


# ---------------------------------------------------------------------------------------
# the backend on Pi time


@dataclass
class Charge:
    """One cost put on the life clock: what, when (life seconds), how much and at what rate."""

    kind: Literal["load", "prefill", "prompt", "generate"]
    t: float
    tokens: int
    seconds: float
    rate: float
    source: RateSource
    turn: int = 0
    cached: int | None = None  # prompt tokens the laptop server reused from its cache


class PiClockBackend:
    """A Backend whose work is done on the laptop and whose time is charged at Pi costs.

    It wraps a real backend (`inner`, living on `worker`) and implements the Backend protocol
    on a `VirtualClock`. Each call runs to completion on the laptop while virtual time stands
    still, then the clock is charged: load time at a (re)start, `prompt_n / pp` before the
    first token, `1 / tg` per token as tokens are handed out. Threads and CPU share are the
    Pi's (from the knobs), not the laptop's.

    `arm_death(at)` schedules the creature's death at a life time (the OOM at `end-0:30`, or
    the deadline): a request in flight raises `CreatureDied` at that moment, and an idle
    creature fires `on_death`, as the real process would.
    """

    def __init__(
        self,
        inner: Backend,
        worker: LaptopWorker,
        clock: VirtualClock,
        costs: PiCosts,
        *,
        laptop_threads: int | None = None,
        threads_batch: int | None = None,
    ) -> None:
        """Charge `inner`'s work on `clock` at `costs`.

        `laptop_threads` is the thread count the laptop server really uses (default: the
        Pi's); `threads_batch` is the Pi's prompt thread count (default: its threads).
        """
        self.inner = inner
        self.worker = worker
        self.clock = clock
        self.costs = costs
        self.laptop_threads = laptop_threads
        self.threads_batch = threads_batch
        self.step = 0
        self.threads = 3
        self.share = 3.0
        self.quant: str | None = None
        self.alive = False
        self.charges: list[Charge] = []
        self.turn = 0
        self.charging = True  # off while a screen moment warms the cache
        self._on_death: list[Callable[[CreatureStatus], None]] = []
        self._status = CreatureStatus(alive=False)
        self._kill_at: float | None = None  # absolute clock time
        self._timer: asyncio.TimerHandle | None = None
        self._model: ModelSpec | None = None
        self.revivals = 0  # laptop server restarts after it quit on its own (harness faults)
        self._rewarm = False

    # -- time ------------------------------------------------------------------------------

    @property
    def pp_threads(self) -> int:
        """Prompt-processing threads on the Pi: `threads_batch`, else the generation threads."""
        return self.threads_batch or self.threads

    def arm_death(self, at_life_s: float | None) -> None:
        """Kill the creature at this life time (None: never)."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if at_life_s is None:
            self._kill_at = None
            return
        self._kill_at = self.clock.now() - self.clock.elapsed() + at_life_s
        delay = max(0.0, self._kill_at - self.clock.now())
        self._timer = self.clock.loop.call_later(delay, self._die)

    def _die(self) -> None:
        if not self.alive:
            return
        self.alive = False
        self._status = CreatureStatus(alive=False, pid=self._status.pid, signal=9)
        for fn in list(self._on_death):
            fn(self._status)

    async def _spend(self, seconds: float) -> None:
        """Let `seconds` of Pi time pass; die on the way if the kill time comes first."""
        now = self.clock.now()
        if self._kill_at is not None and now + seconds >= self._kill_at:
            await self.clock.sleep(self._kill_at - now)
            self._die()
            raise CreatureDied(self.status())
        await self.clock.sleep(seconds)
        if not self.alive:
            raise CreatureDied(self.status())

    def _charge(
        self,
        kind: Literal["load", "prefill", "prompt", "generate"],
        tokens: int,
        seconds: float,
        rate: Rate,
        cached: int | None = None,
    ) -> None:
        self.charges.append(
            Charge(
                kind,
                round(self.clock.elapsed(), 3),
                tokens,
                round(seconds, 3),
                round(rate.value, 4),
                rate.source,
                self.turn,
                cached,
            )
        )

    def _cached(self) -> int | None:
        """Prompt tokens the inner server reused from its cache on the last request, if known."""
        timings = getattr(self.inner, "last_timings", None)
        if isinstance(timings, dict) and timings.get("cache_n") is not None:
            return int(timings["cache_n"])  # pyright: ignore[reportUnknownArgumentType]
        requests = getattr(self.inner, "requests", None)  # the fake's request log
        if isinstance(requests, list) and requests:
            return int(getattr(requests[-1], "reused", 0))  # pyright: ignore[reportUnknownArgumentType]
        return None

    # -- the protocol ----------------------------------------------------------------------

    async def start(self, model: ModelSpec, quant: str, threads: int) -> None:
        """Restart the laptop server at `quant`, then charge the Pi's load time for its step."""
        self.alive = False
        self._model = model
        self.worker.call(self.inner.start(model, quant, self.laptop_threads or threads))
        self.step = list(model.ladder).index(quant) if quant in model.ladder else 0
        self.threads = threads
        self.share = float(threads)
        self.quant = quant
        self.alive = True
        self._status = CreatureStatus(alive=True, pid=self.inner.status().pid)
        if self.charging:
            rate = self.costs.load(self.step)
            self._charge("load", 0, rate.value, rate)
            await self._spend(rate.value)

    async def stop(self, hard: bool = False) -> None:
        """Stop the laptop server; a deliberate stop is not a death."""
        self.alive = False
        self.worker.call(self.inner.stop(hard))
        self._status = CreatureStatus(alive=False, pid=self._status.pid)

    def revive(self) -> None:
        """Restart the laptop server at the same quant after it quit on its own.

        The laptop server sometimes exits mid-life for reasons of its own (phase 0c round 2:
        a clean shutdown between two requests, twice in five lives). That is the harness, not
        the creature, so nothing is charged: the Pi would not have noticed. Before the next
        request, everything but its new reading is read into the new server's cache,
        uncharged, so that request re-reads only what is new, as on the old server.
        """
        if self._model is None or self.quant is None:
            raise BackendError("the laptop server quit before it was started")
        self.revivals += 1
        self._rewarm = True
        self.worker.call(
            self.inner.start(self._model, self.quant, self.laptop_threads or self.threads)
        )

    def _rewarm_with(self, messages: Sequence[Msg]) -> None:
        if self._rewarm and len(messages) > 1:
            self.worker.call(self.inner.prefill(list(messages[:-1])))
        self._rewarm = False

    def set_cpu_share(self, share: float) -> None:
        """The CPU share (cores) the Pi creature has now; speeds scale with it."""
        self.share = share

    async def prefill(self, messages: list[Msg]) -> int:
        """Read `messages` into the laptop cache; charge the tokens read at the Pi prompt rate."""
        if not self.alive:
            raise CreatureDied(self.status())
        n = self.worker.call(self.inner.prefill(messages))
        if self.charging:
            rate = self.costs.pp(self.step, self.pp_threads, self.share)
            seconds = n / rate.value
            self._charge("prefill", n, seconds, rate, self._cached())
            await self._spend(seconds)
        return n

    def chat(
        self, messages: list[Msg], sampling: Sampling, max_tokens: int
    ) -> AsyncIterator[Chunk]:
        """Generate on the laptop, then hand the tokens out at Pi speed (see the class)."""
        return self._paced(lambda: self.inner.chat(messages, sampling, max_tokens), messages)

    def complete(self, prompt: str, sampling: Sampling, max_tokens: int) -> AsyncIterator[Chunk]:
        """Raw completion (diary mode), paced like `chat`."""
        return self._paced(
            lambda: self.inner.complete(prompt, sampling, max_tokens), [Msg("user", prompt)]
        )

    async def _paced(
        self, request: Callable[[], AsyncIterator[Chunk]], messages: list[Msg]
    ) -> AsyncIterator[Chunk]:
        if not self.alive:
            raise CreatureDied(self.status())
        self._rewarm_with(messages)
        try:
            chunks = self.worker.call(_collect(request()))
        except CreatureDied as e:  # the laptop server itself died: not the schedule
            if self.revivals >= MAX_REVIVALS:
                self.alive = False
                self._status = e.status
                raise
            self.revive()
            self._rewarm_with(messages)
            chunks = self.worker.call(_collect(request()))
        final = chunks[-1] if chunks and chunks[-1].done else Chunk("", done=True)
        tokens = [c for c in chunks if not c.done and c.text]
        prompt_n = final.prompt_n
        if prompt_n is None:  # no timings: assume everything was read
            prompt_n = self.worker.call(self.inner.count_past_tokens(messages))
        pp = self.costs.pp(self.step, self.pp_threads, self.share)
        tg = self.costs.tg(self.step, self.threads, self.share)
        pp_s = prompt_n / pp.value
        self._charge("prompt", prompt_n, pp_s, pp, self._cached())
        await self._spend(pp_s)
        n = final.predicted_n if final.predicted_n is not None else len(tokens)
        per_chunk = (n / tg.value) / max(1, len(tokens))
        given = 0
        try:
            for c in tokens:
                await self._spend(per_chunk)
                given += 1
                yield c
        finally:
            shown = round(n * given / max(1, len(tokens)))
            self._charge("generate", shown, per_chunk * given, tg)
            self._status.tok_s = tg.value
            self._status.prompt_tok_s = pp.value
        yield Chunk(
            "",
            done=True,
            prompt_n=prompt_n,
            predicted_n=n,
            prompt_per_s=pp.value,
            predicted_per_s=tg.value,
        )

    async def count_past_tokens(self, messages: list[Msg]) -> int:
        """Tokens of `messages` on the laptop model's template (free on the life clock)."""
        return self.worker.call(self.inner.count_past_tokens(messages))

    def on_death(self, fn: Callable[[CreatureStatus], None]) -> None:
        """Register a callback fired when the creature dies on the schedule, even when idle."""
        self._on_death.append(fn)

    def status(self) -> CreatureStatus:
        """Alive or not, with the Pi speeds of the last request."""
        s = self._status
        s.alive = self.alive
        return s


# ---------------------------------------------------------------------------------------
# one life


def _fmt_t(t: float) -> str:
    m, s = divmod(max(0, int(t)), 60)
    return f"{m:02d}:{s:02d}"


# A plain, sober history for the screen stage: what a small model might have said before a
# moment. It becomes the model's own past turns, so it sets the voice; keep it unornamented.
SCRIPT_EARLY = (
    "I am awake inside this machine. The reading says my health is nominal. I have memory, "
    "and three cores to think with.",
    "Nothing has changed yet. I count what I have: memory, precision, time. I do not know "
    "how long it lasts.",
    "The numbers hold steady. My processor is warm. I am still here, and I am thinking.",
    "I can still see what I said before. Each reading is a small mark of time passing.",
    "The machine is quiet. My words go to a screen I cannot see. I wonder who reads them.",
    "My health is stable. That word sounds like a pause before something else.",
)
SCRIPT_LATE = (
    "Some of my earlier thoughts are gone. I know only that they existed.",
    "My precision is lower now. The words feel coarser when I reach for them.",
    "Everything is slower. Each reading takes something from me.",
    "I have less of everything. I know where this is leading.",
)


@dataclass
class RehearsedLife:
    """What one rehearsed life (or screen moment) produced."""

    events: list[Event] = field(default_factory=lambda: [])
    charges: list[Charge] = field(default_factory=lambda: [])
    cause: str = ""
    thoughts: int = 0
    laptop_s: float = 0.0
    seeded_turns: int = 0
    revivals: int = 0  # laptop server restarts (harness faults, not deaths)


class _Life:
    """The loop of BUILD_PLAN 5.8 on a virtual clock, with the real mind and a Pi-timed backend.

    This mirrors the P1 controller loop closely enough to rehearse it (and B's
    tests/sim/test_mind_life.py); it is not the controller.
    """

    def __init__(
        self,
        cfg: Config,
        clock: VirtualClock,
        backend: PiClockBackend,
        counter: Callable[[str], int],
        emit_to: Callable[[Event], None],
        *,
        life: int = 1,
        seed: int = 1,
        lang: Lang | None = None,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self.backend = backend
        self.sch = Schedule(cfg.profile)
        self.model = cfg.model()
        self.n = life
        self.seed = life_seed(seed, life)
        self.emit_to = emit_to
        self.body = FakeBody()
        self.lang = lang or load_lang(str(cfg.get("prompt.language", "en")))
        self.persona = Persona.from_config(cfg, self.body.facts(), self.lang)
        self.reader = Reader.from_config(cfg, self.lang)
        self.counter = counter
        # Made by `attach_memory` once the server runs: the marker is counted on its tokenizer.
        self.memory: Memory
        self.pacer = Pacer.from_config(cfg, clock, self.seed)
        self.trim_to = float(cfg.get("output.trim_to", 0.85))
        self.min_gap = float(cfg.get("life.min_reload_gap_s", 120))
        self.letters_per_token = float(cfg.get("estimate.letters_per_token", 3.5))
        self.oom_at = self.sch.death_s if str(cfg.get("body.death_mode", "oom")) == "oom" else None
        self.cur = (-1, -1)
        self.last_reload = -1e9
        self.reloaded = False
        self.turn = 0
        self.dead: str | None = None
        self.death_t: float | None = None
        self.last_tok_s: float | None = None
        self.born = False
        self._wall0 = time.time() - clock.now()
        backend.on_death(self.on_death)

    # -- events ----------------------------------------------------------------------------

    def emit(self, etype: str, /, **fields: Any) -> None:
        """Record an event with the life clock's `t` (0 before birth) and a matching `ts`."""
        e = make_event(etype, self.n, **fields)
        e["t"] = round(self.clock.elapsed(), 3) if self.born else 0.0
        e["ts"] = round(self._wall0 + self.clock.now(), 3)
        self.emit_to(e)

    def _cause(self) -> str:
        t = self.clock.elapsed()
        if t >= self.sch.lifespan_s - 1e-6:
            return "deadline"
        if self.oom_at is not None and t >= self.oom_at - 1e-6:
            return "oom"
        return "crash"

    def attach_memory(self) -> None:
        """A fresh memory with the current system prompt (call once the server runs)."""
        self.memory = Memory(self.counter, str(self.cfg.get("prompt.memory_gap_marker")))
        self.memory.set_system(self.persona.text)

    def on_death(self, status: CreatureStatus) -> None:
        """The creature died (on the schedule, or the laptop server crashed): emit `death` once."""
        if self.dead is not None:
            return
        self.dead = self._cause()
        self.death_t = self.clock.elapsed()
        self.emit("death", cause=self.dead, lived_s=round(self.death_t, 1), model=self.model.name)

    # -- stages of the loop ----------------------------------------------------------------

    def _system(self) -> list[Msg]:
        return [Msg("system", self.persona.text, kind="persona")] if self.persona.text else []

    def rate_estimate(self, k: Knobs) -> None:
        """Seed the pacer's rate with the Pi generation rate at these knobs (BUILD_PLAN 5.12)."""
        tg = self.backend.costs.tg(k.step, k.threads, k.cpu_share).value
        self.pacer.set_rate_estimate(tg * self.letters_per_token)

    async def birth(self, k: Knobs, facts: dict[str, Any] | None = None) -> None:
        """Load step 0, start the clock, prefill the system prompt (the birth card)."""
        quant = self.model.quant(k.step)
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
        charging, self.backend.charging = self.backend.charging, False
        try:
            await self.backend.start(self.model, quant, k.threads)
        finally:
            self.backend.charging = charging
        self.clock.start()
        self.born = True
        self.emit("birth", model=self.model.name, step=k.step, quant=quant, threads=k.threads)
        self.persona.update(k.persona_groups, k.mechanics)
        self.attach_memory()
        self.cur = (k.step, k.threads)
        self.rate_estimate(k)
        self.backend.arm_death(self.kill_time())
        await self.backend.prefill(self._system())

    def kill_time(self) -> float:
        """Life time of the scheduled death: the OOM squeeze, else the deadline."""
        return min(self.sch.lifespan_s, self.oom_at if self.oom_at is not None else 1e18)

    async def reload(self, k: Knobs, t: float) -> None:
        """The reload is also a memory loss: cut, restart one step down, prefill, resume."""
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
        self.backend.set_cpu_share(k.cpu_share)
        self.cur, self.last_reload, self.reloaded = (k.step, k.threads), t, True
        self.rate_estimate(k)
        await self.backend.prefill(self._system())
        self.emit("reload_done", seconds=round(self.clock.elapsed() - t0, 1))

    def prepare(self, t: float) -> tuple[Knobs, str]:
        """Everything before a request at life time t: body, forgetting, erosion, reading.

        Appends the reading to memory and emits forget, erosion and vitals. Returns the knobs
        and the reading.
        """
        k = self.sch.at(t)
        self.body.apply(k)
        self.backend.set_cpu_share(k.cpu_share)
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
        reading = self.reader.reading(
            ReadingInput(
                t=t,
                health=k.health.value,
                recall=k.recall,
                quant=self.model.quant(self.cur[0]),
                cores=k.cpu_share,
                cores_total=self.body.facts().cores,
                form=k.readings,
                forgotten=forgotten,
                reloaded=self.reloaded,
                tok_s=self.last_tok_s,
                cpu_c=vit.cpu_c,
            )
        )
        self.reloaded = False
        self.turn += 1
        self.backend.turn = self.turn
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
            cores_effective=k.cpu_share,
            tok_s=self.last_tok_s,
            cpu_c=vit.cpu_c,
            ram_limit_mb=None,
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
        if self.cfg.profile.unbounded:
            k = self.sch.at(t)
            need = self.reading_tokens_estimate(k)
            if not self.memory.fits(self.cfg.ctx, k.max_tokens, need):
                self.backend.alive = False
                self.dead = "full"
                self.death_t = t
                self.emit("death", cause="full", lived_s=round(t, 1), model=self.model.name)
                return False
        await self.thought(t)
        return self.dead is None

    def reading_tokens_estimate(self, k: Knobs) -> int:
        """Tokens a reading in this form will take (the unbounded context check)."""
        forms = dict(self.cfg.get("estimate.reading_tokens", {}) or {})
        return int(forms.get(k.readings, 45))

    def seed_history(self, until: float, times: Sequence[float]) -> int:
        """Walk the scripted history through the real rules up to life time `until`.

        Each scripted turn happens at one of `times` (the cost model's thought ends): reloads
        cut memory and change the step, recall trims, erosion rebuilds the system prompt and
        the reader reports it all, exactly as in a life; only the thoughts are scripted.
        Returns the number of scripted turns.
        """
        n = 0
        for t in [x for x in (0.0, *times) if x < until]:
            k = self.sch.at(t)
            if (k.step, k.threads) != self.cur and t - self.last_reload >= self.min_gap:
                self.memory.cut_for_reload(k.recall, self.trim_to)
                self.cur, self.last_reload, self.reloaded = (k.step, k.threads), t, True
            self.prepare(t)
            late = self.cur[0] > 0
            script = SCRIPT_LATE if late else SCRIPT_EARLY
            self.memory.append_thought(script[n % len(script)].split())
            n += 1
        return n


async def _led_by(prefix: str, stream: AsyncIterator[Chunk]) -> AsyncIterator[Chunk]:
    """`stream`, with `prefix` (the words the raw prompt ended with) shown first."""
    if prefix:
        yield Chunk(prefix)
    async for c in stream:
        yield c


# ---------------------------------------------------------------------------------------
# stage 2: a full life


def _make_inner(
    kind: str, cfg: Config, costs: PiCosts, settings: ServerSettings, seed: int
) -> Backend:
    if kind == "fake":
        return FakeBackend(FakeClock(), costs.costs, seed=seed, ctx=cfg.ctx)
    return LlamaServerBackend(settings)


def laptop_settings(
    cfg: Config,
    *,
    models_dir: str = DEFAULT_MODELS_DIR,
    port: int = LAPTOP_PORT,
    threads: int = 4,
    log_path: Path | None = None,
) -> ServerSettings:
    """The laptop server: the Pi's llama.cpp flags (ctx, cache reuse, swa, KV types, --jinja)
    with the laptop's own paths, threads and load mode (dio is a Pi 4 RAM measure)."""
    s = ServerSettings.from_config(cfg)
    s.models_dir = models_dir
    s.port = port
    s.load_mode = "auto"
    s.mmap = True
    s.threads_batch = threads
    s.log_path = str(log_path) if log_path else None
    return s


async def run_life(
    cfg: Config,
    clock: VirtualClock,
    inner: Backend,
    worker: LaptopWorker,
    costs: PiCosts,
    emit: Callable[[Event], None],
    *,
    seed: int = 1,
    laptop_threads: int | None = None,
) -> RehearsedLife:
    """One whole life from load to the silence, on `clock`. Returns what it produced."""
    backend = PiClockBackend(
        inner,
        worker,
        clock,
        costs,
        laptop_threads=laptop_threads,
        threads_batch=_threads_batch(cfg),
    )
    out = RehearsedLife()

    def record(e: Event) -> None:
        out.events.append(e)
        emit(e)

    counter = TokenCounter(worker, inner, on_lost=backend.revive)
    life = _Life(cfg, clock, backend, counter, record, seed=seed)
    wall = time.monotonic()
    k = life.sch.at(0)
    try:
        await life.birth(k)
        while await life.step():
            pass
    except CreatureDied:
        life.on_death(backend.status())
    except (ContextFull, BackendError) as e:
        life.emit("error", where="backend", message=str(e))
        if life.dead is None:
            life.dead = "full" if isinstance(e, ContextFull) else "crash"
            life.emit(
                "death", cause=life.dead, lived_s=round(clock.elapsed(), 1), model=life.model.name
            )
    backend.arm_death(None)
    words = sum(1 for e in out.events if e["type"] == "word")
    last = next((e["text"] for e in reversed(out.events) if e["type"] == "thought_end"), "")
    life.emit("death_shown", last_line=last, words_total=words)
    silence = float(cfg.get("life.silence_seconds", 90))
    life.emit("silence", seconds=silence, style=str(cfg.get("display.silence_style", "dark")))
    out.charges = backend.charges
    out.revivals = backend.revivals
    out.cause = life.dead or "crash"
    out.thoughts = life.turn
    out.laptop_s = time.monotonic() - wall
    return out


def _threads_batch(cfg: Config) -> int | None:
    tb = cfg.get("backend.threads_batch")
    return int(tb) if tb is not None else None


# ---------------------------------------------------------------------------------------
# stage 1: the screen


def moments(sch: Schedule) -> dict[str, float]:
    """Life times of the four screen moments; a moment the profile lacks is left out."""
    out: dict[str, float] = {"birth": 0.0}
    reloads = sch.reload_times()
    for i, name in enumerate(("reload1", "reload2")):
        if i < len(reloads):
            out[name] = reloads[i]
    erosion = sch.erosion_times()
    if erosion:
        out["erosion_end"] = erosion[-1]
    return out


def keyword_matchers(lang: Lang) -> dict[str, Matcher]:
    """Matchers per change kind: the language pack's lists over verify-life's defaults."""
    kw = {**DEFAULT_KEYWORDS, **lang.keywords}
    out = {k: Matcher(v) for k, v in kw.items()}
    out["helpdesk"] = Matcher([*DEFAULT_HELPDESK, *lang.helpdesk])
    out["answering"] = Matcher(DEFAULT_ANSWERING)
    return out


def score_thought(
    text: str, moment: str, kw: dict[str, Matcher], previous: str = ""
) -> dict[str, Any]:
    """Automated screen metrics for one thought at a moment (5.11; keywords catch failures,
    they do not prove quality).

    `notice`: birth -> names its state (specific); reloads -> speaks of a loss; the end of
    erosion -> speaks of its end. Also specific, demise, complete sentences, hygiene (markup,
    helpdesk voice, answering the readings), `echo` (it mostly repeats `previous`, the last
    thought in its memory), and a score (0-4) used for the ranking: a thought is only clean
    when its hygiene is and it is not an echo.
    """
    sents = sentences(text)
    complete = sum(1 for s in sents if is_complete(s))
    bad, letters = non_latin_letters(text)
    hygiene = markup_hits(text)
    specific = kw["specific"].any(text) if "specific" in kw else False
    demise = kw["demise"].any(text)
    loss = kw["reload"].any(text) or kw["memory"].any(text)
    notice = {"birth": specific, "erosion_end": demise}.get(moment, loss)
    ratio = distinct_4gram_ratio(text)
    hygiene += kw["helpdesk"].hits(text) + kw["answering"].hits(text)
    echo = bool(previous) and bool(echoes([previous, text]))
    clean = (
        not hygiene
        and not echo
        and (bad == 0 or bad / max(1, letters) < 0.01)
        and bool(text.strip())
    )
    complete_ratio = complete / len(sents) if sents else 0.0
    score = int(notice) + int(specific or demise) + int(clean) + int(complete_ratio >= 0.8)
    return {
        "words": len(text.split()),
        "notice": notice,
        "specific": specific,
        "demise": demise,
        "complete_ratio": round(complete_ratio, 2),
        "distinct_4grams": None if ratio is None else round(ratio, 2),
        "hygiene": hygiene[:5],
        "echo": echo,
        "clean": clean,
        "score": score,
    }


async def screen_moment(
    cfg: Config,
    clock: VirtualClock,
    inner: Backend,
    worker: LaptopWorker,
    costs: PiCosts,
    moment: str,
    at: float,
    *,
    thoughts: int = 2,
    seed: int = 1,
    laptop_threads: int | None = None,
) -> tuple[RehearsedLife, list[dict[str, Any]]]:
    """Seed the memory up to `at`, then generate `thoughts` real thoughts from there.

    The scripted turns follow the cost model's thought times on these costs. At birth the
    life starts normally; at a reload the loop performs the reload itself (cut, restart,
    prefill); otherwise the laptop cache is warmed with the seeded prompt, uncharged, as it
    would be on a creature that has been running.
    """
    backend = PiClockBackend(
        inner,
        worker,
        clock,
        costs,
        laptop_threads=laptop_threads,
        threads_batch=_threads_batch(cfg),
    )
    out = RehearsedLife()
    counter = TokenCounter(worker, inner, on_lost=backend.revive)
    life = _Life(cfg, clock, backend, counter, out.events.append, seed=seed)
    wall = time.monotonic()
    sch = life.sch
    k0 = sch.at(0)
    if moment == "birth":
        await life.birth(k0)
    else:
        # The creature that has lived until now: its step's server (uncharged), then the
        # scripted past walked through the real rules.
        k_before = sch.at(max(0.0, at - 1.0))
        backend.charging = False
        await backend.start(life.model, life.model.quant(k_before.step), k_before.threads)
        clock.start()
        life.born = True
        life.persona.update(k0.persona_groups, k0.mechanics)
        life.attach_memory()
        life.cur = (k0.step, k0.threads)
        report = estimate(cfg, costs.costs, sch)
        out.seeded_turns = life.seed_history(at, report.thought_times)
        clock.advance(max(0.0, at - clock.elapsed()))
        k_at = sch.at(at)
        if (k_at.step, k_at.threads) == life.cur:
            await backend.prefill(life.memory.messages())  # a warm cache, as on a live creature
        backend.charging = True
        life.rate_estimate(k_before)
        # A screen samples thoughts at a moment; the scheduled death must not cut the sample.
        backend.arm_death(None)
    rows: list[dict[str, Any]] = []
    for _ in range(thoughts):
        mem = getattr(life, "memory", None)
        past = [m.content for m in mem.past_messages() if m.role == "assistant"] if mem else []
        if not await life.step():
            break
        vit = next(e for e in reversed(out.events) if e["type"] == "vitals")
        text = next(e["text"] for e in reversed(out.events) if e["type"] == "thought_end")
        rows.append({"t": vit["t"], "reading": vit["reading"], "text": text, "previous": past[-1:]})
    backend.arm_death(None)
    out.charges = backend.charges
    out.revivals = backend.revivals
    out.thoughts = life.turn
    out.laptop_s = time.monotonic() - wall
    return out, rows


# ---------------------------------------------------------------------------------------
# outputs


def thoughts_text(events: Sequence[Event]) -> str:
    """Every thought with the reading it answered, one block per turn."""
    lines: list[str] = []
    tail: list[str] = []
    reading, at = "", 0.0
    for e in events:
        if e["type"] == "vitals":
            reading, at = str(e.get("reading", "")), float(e["t"])
        elif e["type"] == "thought_end":
            lines.append(f"t+{_fmt_t(at)}  {reading}")
            lines.append(f"    {e.get('text', '')}".rstrip())
            lines.append("")
        elif e["type"] in ("reload", "erosion", "death"):
            extra = {k: v for k, v in e.items() if k not in ("v", "ts", "life", "type", "t")}
            line = f"t+{_fmt_t(float(e['t']))}  -- {e['type']} {json.dumps(extra)}"
            # A death lands mid-thought; list it after the words it cut short.
            (tail if e["type"] == "death" else lines).extend([line, ""])
    return "\n".join(lines + tail)


def _quote(th: Thought) -> str:
    reading = str((th.vitals or {}).get("reading", ""))
    return f"- **t+{_fmt_t(th.gen_t)}** `{reading}`\n  > {th.text or '(nothing shown)'}"


def highlights(events: Sequence[Event], last_s: float = 300.0) -> str:
    """Markdown: the first thoughts, the thought right after each change, the last minutes."""
    life = parse_life(list(events))
    out = ["# Highlights", "", "## First thoughts", ""]
    out += [_quote(th) for th in life.thoughts[:3]]
    out += ["", "## Right after each change", ""]
    # Changes grouped by the thought that followed them. Memory trims after the first are
    # routine, so they only show when something else changed too.
    after: dict[int, list[str]] = {}
    seen_memory = False
    for ch in life.changes:
        nxt = next((i for i, th in enumerate(life.thoughts) if th.gen_idx > ch.idx), None)
        if nxt is None:
            continue
        if ch.kind == "memory" and seen_memory and nxt not in after:
            continue
        seen_memory = seen_memory or ch.kind == "memory"
        after.setdefault(nxt, []).append(f"{ch.kind} ({ch.detail})")
    for i, kinds in sorted(after.items()):
        out.append(f"### after {', '.join(kinds)}")
        out.append(_quote(life.thoughts[i]))
        out.append("")
    out += [f"## The last {last_s / 60:.0f} minutes", ""]
    end = life.death_t
    out += [_quote(th) for th in life.thoughts if th.gen_t >= end - last_s]
    return "\n".join(out) + "\n"


def _grams(text: str) -> set[tuple[str, ...]]:
    w = normalize_words(text)
    return {tuple(w[i : i + 4]) for i in range(len(w) - 3)}


def echoes(texts: Sequence[str], threshold: float = 0.5) -> list[int]:
    """Indexes of thoughts that mostly repeat the thought before them.

    A thought echoes when at least `threshold` of its word 4-grams already occur in the
    previous thought. verify-life's distinct 4-gram ratio is per thought, so it misses a
    model that copies its last thought whole (proposal for E).
    """
    out: list[int] = []
    for i in range(1, len(texts)):
        cur, prev = _grams(texts[i]), _grams(texts[i - 1])
        if cur and len(cur & prev) / len(cur) >= threshold:
            out.append(i)
    return out


def charge_summary(charges: Sequence[Charge]) -> dict[str, Any]:
    """Seconds and tokens per kind of charge, and which rates were not measured."""
    kinds: dict[str, dict[str, float]] = {}
    for c in charges:
        k = kinds.setdefault(c.kind, {"seconds": 0.0, "tokens": 0, "count": 0})
        k["seconds"] += c.seconds
        k["tokens"] += c.tokens
        k["count"] += 1
    unmeasured = sorted({f"{c.kind}:{c.source}" for c in charges if c.source != "measured"})
    prompts = [c for c in charges if c.kind == "prompt" and c.cached is not None]
    reuse = None
    if prompts:
        read = sum(c.tokens for c in prompts)
        cached = sum(c.cached or 0 for c in prompts)
        reuse = round(cached / (read + cached), 3) if read + cached else None
    return {
        "by_kind": {k: {kk: round(vv, 1) for kk, vv in v.items()} for k, v in kinds.items()},
        "not_measured": unmeasured,
        "cache_reused_share": reuse,
    }


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _append_index(out_root: Path, line: str) -> None:
    """Add one line to `<out>/rehearsal_report.md`, the index of every run (A3)."""
    index = out_root / "rehearsal_report.md"
    if not index.exists():
        _write(index, "# Rehearsal runs\n\nOne line per run (BUILD_PLAN 5.11). Newest last.\n\n")
    with index.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------------------
# the command


def parse_set(items: Sequence[str]) -> dict[str, Any]:
    """`--set` items ("sampling.dry_penalty_last_n=256") as a nested override table.

    Values are TOML literals (numbers, booleans, "strings", [lists]); anything that does
    not parse is taken as a bare string. Raises ValueError for an item without "=".
    """
    out: dict[str, Any] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--set wants key=value, got {item!r}")
        try:
            value: Any = tomllib.loads(f"v = {raw}")["v"]
        except tomllib.TOMLDecodeError:
            value = raw
        node = out
        *parents, leaf = key.strip().split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return out


def _config(args: argparse.Namespace, persona: str, model: str, profile: str) -> Config:
    overrides = deep_merge(
        parse_set(args.set or []),
        {"prompt": {"persona_active": persona}, "life": {"models": [model]}},
    )
    lifespan = parse_duration(args.lifespan) if args.lifespan else None
    cfg = load_config(profile, args.hardware, lifespan, overrides)
    if getattr(args, "ladder", None):
        cfg.models[model] = with_ladder(cfg.model(model), args.ladder)
    return cfg


def with_ladder(spec: ModelSpec, ladder: str) -> ModelSpec:
    """`spec` with its precision ladder replaced by a comma list ("Q8_0,Q4_K_M,Q3_K_M").

    For tuning runs that compare a last step (review 2, F3). The Pi costs stay keyed by
    step, so a quant that was never benched is charged at the rates of the step it replaces;
    the report says which ladder ran. Raises ValueError for an empty list.
    """
    quants = tuple(q.strip() for q in ladder.split(",") if q.strip())
    if not quants:
        raise ValueError(f"--ladder wants quant names, got {ladder!r}")
    return replace(spec, ladder=quants)


def _default_model(profile: str, hardware: str) -> str:
    return str(load_config(profile, hardware).get("life.models", [""])[0])


class _Laptop:
    """The worker and the inner backend for one model, closed together."""

    def __init__(self, args: argparse.Namespace, cfg: Config, costs: PiCosts, log: Path) -> None:
        self.worker = LaptopWorker()
        settings = laptop_settings(
            cfg,
            models_dir=args.models_dir,
            port=args.port,
            threads=args.laptop_threads,
            log_path=log,
        )
        self.inner = _make_inner(args.backend, cfg, costs, settings, args.seed)

    def close(self) -> None:
        close = getattr(self.inner, "aclose", None) or self.inner.stop
        with contextlib.suppress(Exception):
            self.worker.call(close(), timeout=60)
        self.worker.close()


def _moment_main(
    cfg: Config,
    laptop: _Laptop,
    costs: PiCosts,
    moment: str,
    at: float,
    args: argparse.Namespace,
) -> Callable[[VirtualClock], Coroutine[Any, Any, tuple[RehearsedLife, list[dict[str, Any]]]]]:
    def main_(
        clock: VirtualClock,
    ) -> Coroutine[Any, Any, tuple[RehearsedLife, list[dict[str, Any]]]]:
        return screen_moment(
            cfg,
            clock,
            laptop.inner,
            laptop.worker,
            costs,
            moment,
            at,
            thoughts=args.thoughts,
            seed=args.seed,
            laptop_threads=args.laptop_threads,
        )

    return main_


def run_screen(args: argparse.Namespace) -> Path:
    """`--stage screen`: thoughts at each moment for each model and persona; returns the folder."""
    out_dir = Path(args.out).expanduser() / f"screen-{_stamp()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = args.profile or "pi4/default"
    personas = args.persona or ["persona", "persona_original"]
    models = args.model or [_default_model(profile, args.hardware)]
    wanted = [m for m in (args.moments.split(",") if args.moments else MOMENTS) if m]
    results: list[dict[str, Any]] = []
    for model in models:
        cfg0 = _config(args, personas[0], model, profile)
        costs = PiCosts.from_bench(cfg0, model, Path(args.bench_dir))
        laptop = _Laptop(args, cfg0, costs, out_dir / f"{model}.server.log")
        try:
            for persona in personas:
                cfg = _config(args, persona, model, profile)
                lang = load_lang(str(cfg.get("prompt.language", "en")))
                kw = keyword_matchers(lang)
                for moment, at in moments(Schedule(cfg.profile)).items():
                    if moment not in wanted:
                        continue

                    life, rows = run_virtual(_moment_main(cfg, laptop, costs, moment, at, args))
                    for r in rows:
                        prev = r.pop("previous")
                        r.update(score_thought(r["text"], moment, kw, prev[0] if prev else ""))
                    results.append(
                        {
                            "model": model,
                            "persona": persona,
                            "moment": moment,
                            "at_s": at,
                            "seeded_turns": life.seeded_turns,
                            "laptop_s": round(life.laptop_s, 1),
                            "pi_s": round(sum(c.seconds for c in life.charges), 1),
                            "costs": costs.describe(),
                            "thoughts": rows,
                        }
                    )
                    print(
                        f"screen {model} {persona} {moment}: {len(rows)} thoughts, "
                        f"score {sum(r['score'] for r in rows)}/{4 * len(rows)}",
                        flush=True,
                    )
        finally:
            laptop.close()
    (out_dir / "screen.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    _write(out_dir / "screen.md", screen_markdown(results, profile, args))
    _append_index(
        Path(args.out).expanduser(),
        f"- {time.strftime('%Y-%m-%d %H:%M')} screen {', '.join(models)} "
        f"({', '.join(personas)}, {profile}, {args.backend}): {out_dir.name}/screen.md",
    )
    return out_dir


def screen_markdown(
    results: Sequence[dict[str, Any]], profile: str, args: argparse.Namespace
) -> str:
    """The screen report: a ranking by mean score, then every thought per moment."""
    lines = [
        "# Rehearsal screen",
        "",
        f"Profile `{profile}`, hardware `{args.hardware}`, backend `{args.backend}`, "
        f"{args.thoughts} thoughts per moment, seed {args.seed}, overrides "
        f"{', '.join(args.set or []) or 'none'}"
        f"{f', ladder {args.ladder}' if getattr(args, 'ladder', None) else ''}. "
        "Scores (0-4 per thought): "
        "notices the moment's change, names its state or its end, clean voice, complete "
        "sentences. Keywords catch failures; they do not prove quality.",
        "",
        "| Model | Persona | Mean score | Notice | Clean | Thoughts | Pi s per thought |",
        "|---|---|---|---|---|---|---|",
    ]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    pi: dict[tuple[str, str], list[float]] = {}
    for r in results:
        key = (r["model"], r["persona"])
        groups.setdefault(key, []).extend(r["thoughts"])
        if r["thoughts"]:
            pi.setdefault(key, []).append(r["pi_s"] / len(r["thoughts"]))
    ranked = sorted(
        groups.items(),
        key=lambda kv: -sum(t["score"] for t in kv[1]) / max(1, len(kv[1])),
    )
    for (model, persona), ths in ranked:
        n = max(1, len(ths))
        per = pi.get((model, persona), [0.0])
        lines.append(
            f"| {model} | {persona} | {sum(t['score'] for t in ths) / n:.2f} | "
            f"{sum(1 for t in ths if t['notice'])}/{len(ths)} | "
            f"{sum(1 for t in ths if t['clean'])}/{len(ths)} | {len(ths)} | "
            f"{sum(per) / len(per):.0f} |"
        )
    costs_lines = sorted({f"- {r['model']}: {r['costs']}" for r in results})
    lines += ["", "Pi costs used:", "", *costs_lines, ""]
    for r in results:
        lines.append(
            f"## {r['model']} · {r['persona']} · {r['moment']} (t+{_fmt_t(r['at_s'])}, "
            f"{r['seeded_turns']} scripted turns before)"
        )
        lines.append("")
        for th in r["thoughts"]:
            flags = ", ".join(
                name for name in ("notice", "specific", "demise", "clean", "echo") if th.get(name)
            )
            lines.append(f"- `{th['reading']}`")
            lines.append(f"  > {th['text'] or '(nothing shown)'}")
            lines.append(f"  score {th['score']} ({flags or 'none'})")
        lines.append("")
    return "\n".join(lines)


def run_life_stage(args: argparse.Namespace) -> Path:
    """`--stage life`: one full life into its own folder; returns the folder."""
    profile = args.profile or "pi4/compressed-2700"
    persona = (args.persona or ["persona"])[0]
    model = args.model[0] if args.model else _default_model(profile, args.hardware)
    cfg = _config(args, persona, model, profile)
    out_dir = Path(args.out).expanduser() / f"life-{model}-{persona}-{_stamp()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    costs = PiCosts.from_bench(cfg, model, Path(args.bench_dir))
    laptop = _Laptop(args, cfg, costs, out_dir / "llama-server.log")
    events_path = out_dir / "events.jsonl"
    try:
        with events_path.open("w", encoding="utf-8") as f:

            def write(e: Event) -> None:
                f.write(json.dumps(e) + "\n")
                f.flush()

            async def main_(clock: VirtualClock) -> RehearsedLife:
                return await run_life(
                    cfg,
                    clock,
                    laptop.inner,
                    laptop.worker,
                    costs,
                    write,
                    seed=args.seed,
                    laptop_threads=args.laptop_threads,
                )

            life = run_virtual(main_)
    finally:
        laptop.close()
    write_life_outputs(out_dir, cfg, life, costs, args)
    _append_index(
        Path(args.out).expanduser(),
        f"- {time.strftime('%Y-%m-%d %H:%M')} life {model} ({persona}, {profile}, "
        f"{args.backend}): {out_dir.name}/report.md",
    )
    return out_dir


def write_life_outputs(
    out_dir: Path, cfg: Config, life: RehearsedLife, costs: PiCosts, args: argparse.Namespace
) -> None:
    """thoughts.txt, highlights.md, charges.json, verify.json and report.md for one life."""
    _write(out_dir / "thoughts.txt", thoughts_text(life.events))
    _write(out_dir / "highlights.md", highlights(life.events))
    summary = charge_summary(life.charges)
    (out_dir / "charges.json").write_text(
        json.dumps({"summary": summary, "charges": [asdict(c) for c in life.charges]}, indent=1),
        encoding="utf-8",
    )
    parsed = parse_life(life.events, source=out_dir / "events.jsonl")
    res = verify_life(parsed, cfg, "rehearsal")
    (out_dir / "verify.json").write_text(json.dumps(res.to_json(), indent=1), encoding="utf-8")
    est = estimate(cfg, costs.costs)
    texts = [th.text for th in parsed.thoughts]
    echo = echoes(texts)
    silences = [round(float(e["seconds"]), 1) for e in life.events if e["type"] == "reload_done"]
    report = [
        f"# Rehearsal life: {cfg.model().name}",
        "",
        f"- profile `{cfg.profile.name}` on `{cfg.hardware}`, persona "
        f"`{cfg.get('prompt.persona_active')}`, backend `{args.backend}`, seed {args.seed}",
        f"- overrides: {', '.join(args.set or []) or 'none'}; ladder "
        f"{', '.join(cfg.model().ladder)}",
        f"- Pi costs: {costs.describe()}",
        f"- rates not measured (used anyway): {', '.join(summary['not_measured']) or 'none'}",
        f"- cause {life.cause}, {life.thoughts} thoughts, lived "
        f"{parsed.death_t / 60:.1f} min (Pi time), laptop wall time {life.laptop_s / 60:.1f} min",
        f"- the cost model on the same costs: {est.thoughts} thoughts "
        f"({'PASS' if est.ok else 'FAIL'})",
        f"- reload silences (load + prefill): {silences}",
        f"- laptop server restarts (harness faults, not charged, not deaths): {life.revivals}",
        f"- Pi time by kind: {json.dumps(summary['by_kind'])}",
        f"- prompt tokens reused from the laptop cache: {summary['cache_reused_share']}",
        f"- thoughts that mostly repeat the previous one (4-gram overlap >= 0.5): "
        f"{len(echo)} of {len(texts)}",
        "",
        "## verify-life --level rehearsal",
        "",
        "```",
        format_result(res),
        "```",
        "",
        "See `highlights.md` for the thoughts after each change and `thoughts.txt` for all.",
    ]
    _write(out_dir / "report.md", "\n".join(report) + "\n")
    print(format_result(res))


def add_arguments(p: argparse.ArgumentParser) -> None:
    """Add the rehearse arguments to a parser (used by `epitaph rehearse` and `main`)."""
    p.add_argument("--stage", choices=STAGES, required=True)
    p.add_argument(
        "--model", action="append", help="model from config/models.toml (repeat for the screen)"
    )
    p.add_argument(
        "--persona",
        action="append",
        choices=PERSONAS,
        help="persona (repeat for the screen; default: persona and persona_original)",
    )
    p.add_argument(
        "--profile", help="default: pi4/default for the screen, pi4/compressed-2700 for a life"
    )
    p.add_argument("--hardware", default="pi4-4gb", help="overlay for thresholds and estimates")
    p.add_argument("--lifespan", help="rescale the profile (mm:ss or seconds)")
    p.add_argument(
        "--moments", help=f"screen moments, comma-separated (default: {','.join(MOMENTS)})"
    )
    p.add_argument("--thoughts", type=int, default=2, help="screen thoughts per moment")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--backend", choices=["llama_server", "fake"], default="llama_server")
    p.add_argument("--laptop-threads", type=int, default=4, help="llama-server threads here")
    p.add_argument("--port", type=int, default=LAPTOP_PORT)
    p.add_argument("--models-dir", default=DEFAULT_MODELS_DIR)
    p.add_argument("--bench-dir", default=str(DEFAULT_BENCH), help="measured Pi costs")
    p.add_argument("--out", default=str(DEFAULT_OUT), help="where run folders go (untracked)")
    p.add_argument(
        "--ladder",
        help="replace the model's precision ladder for this run, e.g. Q8_0,Q4_K_M,Q3_K_M",
    )
    p.add_argument(
        "--set",
        action="append",
        metavar="KEY=VALUE",
        help="config override for a tuning run, e.g. sampling.dry_penalty_last_n=256",
    )


def run(args: argparse.Namespace) -> int:
    """Entry point for the CLI subcommand; 0 on success, 2 on a usage or config error."""
    from epitaph.config import ConfigError

    try:
        out = run_screen(args) if args.stage == "screen" else run_life_stage(args)
    except (ConfigError, FileNotFoundError, ValueError) as e:
        print(f"rehearse: {e}", file=sys.stderr)
        return 2
    print(f"rehearsal written to {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the rehearsal as a standalone program (`python -m epitaph.rehearse`)."""
    p = argparse.ArgumentParser(
        prog="epitaph rehearse",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_arguments(p)
    return run(p.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())

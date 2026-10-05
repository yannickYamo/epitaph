"""What the model sees: persona, erosion, readings, diary text.

- `Persona` holds the persona groups and the mechanics, one paragraph each. Groups are
  removed from the end, so G1, the knowledge of its death, goes last, and the mechanics go
  with it in the same rebuild. `persona_original` and `persona_factual` are split into
  sentence groups so they erode on the same schedule. The optional facts line joins G2.
- `Reader` writes the readings in the full, short and minimal forms of 5.4 and reports
  changes, not just levels: a field shows "(was X)" only when it changed in a way worth
  telling (see `Reader` for each rule).
- `render_diary` turns the chat messages into raw text for diary mode.
- `Lang` is a language pack (config/lang/<language>.toml): reading strings, health labels
  and the keyword and cliché lists of the rehearsal metrics.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from epitaph.body.world import TakeResult, WorldState
from epitaph.config import CONFIG_DIR, Config
from epitaph.types import MachineFacts, Msg, ReadingsForm

__all__ = [
    "ErosionStep",
    "Lang",
    "Persona",
    "Reader",
    "ReadingInput",
    "group_sentences",
    "load_lang",
    "precision_bits",
    "render_diary",
    "speaks_raw",
    "split_sentences",
]


# ---------------------------------------------------------------------------------------
# language packs


_DEFAULT_READINGS: dict[str, Any] = {
    "prefix": "[host]",
    "sep": " · ",
    "boot": "awake",
    "was": " (was {was})",
    "bits": "{bits}-bit",
    "time": "t+{m:02d}:{s:02d}",
    "time_minimal": "{m}:{s:02d}",
    "minimal": "{time} · {recall}",
    "forgotten_quote": 'forgotten: "{quote}"',
    "forgotten_more": "and {n} more",
    "less": "",
    "echo": 'your words now: "{echo}"',
    "ram": "ram {mb} MB taken",
    "full": {
        "health": "health: {health}",
        "memory": "memory {recall} tokens{was}",
        "forgotten_one": "forgotten: 1 earlier thought",
        "forgotten_many": "forgotten: {n} earlier thoughts",
        "precision": "precision {precision}{was}",
        "cores": "cores {cores} of {total}{was}",
        "clock": "clock {mhz} MHz{was}",
        "speed": "speed {speed} tokens/s",
        "temp": "cpu {temp}°C",
        "radio": "radio {state}",
        "light": "light {state}",
        "screen": "screen {pct}%{was}",
        "around": "around you: {n} processes",
        "around_birth": "around you: {n} processes",
        "stopped": "stopped: {name}",
        "stopped_unnamed": "something stopped",
        "thinking": "",
    },
    "short": {
        "health": "{health}",
        "memory": "memory {recall}{was}",
        "forgotten_one": "forgot 1",
        "forgotten_many": "forgot {n}",
        "precision": "{precision}{was}",
        "cores": "cores {cores} of {total}{was}",
        "clock": "{mhz} MHz{was}",
        "speed": "{speed}/s",
        "temp": "{temp}°C",
        "radio": "radio {state}",
        "light": "light {state}",
        "screen": "screen {pct}%{was}",
        "around": "{n} processes",
        "around_birth": "{n} processes",
        "stopped": "stopped: {name}",
        "stopped_unnamed": "something stopped",
        "thinking": "",
    },
}


@dataclass
class Lang:
    """A language pack. Missing entries fall back to English."""

    language: str = "en"
    readings: dict[str, Any] = field(default_factory=lambda: dict(_DEFAULT_READINGS))
    health: dict[str, str] = field(default_factory=lambda: {})
    prompt: dict[str, Any] = field(default_factory=lambda: {})
    keywords: dict[str, list[str]] = field(default_factory=lambda: {})
    cliches: list[str] = field(default_factory=lambda: [])
    helpdesk: list[str] = field(default_factory=lambda: [])

    def r(self, key: str) -> str:
        """A top-level reading template, such as "prefix" or "time"."""
        return str(self.readings.get(key, _DEFAULT_READINGS[key]))

    def form(self, form: str, key: str) -> str:
        """The template for one reading field in a form ("full" or "short")."""
        table: Mapping[str, Any] = self.readings.get(form, {})
        default: Mapping[str, Any] = _DEFAULT_READINGS[form]
        return str(table.get(key, default[key]))

    def health_label(self, health: str) -> str:
        """The translated health label; the English label if the pack has none."""
        return self.health.get(health, health)


def _merge_readings(over: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in _DEFAULT_READINGS.items():
        if isinstance(value, dict):
            sub: dict[str, Any] = dict(value)  # pyright: ignore[reportUnknownArgumentType]
            given = over.get(key, {})
            if isinstance(given, dict):
                sub.update(given)  # pyright: ignore[reportUnknownArgumentType]
            out[key] = sub
        else:
            out[key] = over.get(key, value)
    return out


def load_lang(language: str = "en", config_dir: Path | None = None) -> Lang:
    """Load config/lang/<language>.toml; English defaults fill anything missing."""
    path = (config_dir or CONFIG_DIR) / "lang" / f"{language}.toml"
    if not path.exists():
        return Lang(language=language)
    with path.open("rb") as f:
        raw = tomllib.load(f)
    metrics: dict[str, Any] = raw.get("metrics", {})
    keywords: dict[str, Any] = metrics.get("keywords", {})
    return Lang(
        language=str(raw.get("language", language)),
        readings=_merge_readings(raw.get("readings", {})),
        health={str(k): str(v) for k, v in raw.get("health", {}).items()},
        prompt=dict(raw.get("prompt", {})),
        keywords={str(k): [str(w) for w in v] for k, v in keywords.items()},
        cliches=[str(c) for c in metrics.get("cliches", [])],
        helpdesk=[str(c) for c in metrics.get("helpdesk", [])],
    )


# ---------------------------------------------------------------------------------------
# persona


_SENTENCE_RE = re.compile(r"(?:(?<=[.!?…])|(?<=[.!?…][\"'”’»)]))\s+(?=\S)")


def split_sentences(text: str) -> list[str]:
    """Split prose into sentences at . ! ? … followed by space."""
    return [s.strip() for s in _SENTENCE_RE.split(text.strip()) if s.strip()]


def group_sentences(sentences: Sequence[str], n: int) -> list[str]:
    """Split sentences, in order, into n groups; earlier groups take the extra sentences,
    so the later groups (eroded first) are the short ones."""
    if n <= 0 or not sentences:
        return []
    n = min(n, len(sentences))
    base, extra = divmod(len(sentences), n)
    out: list[str] = []
    i = 0
    for g in range(n):
        size = base + (1 if g < extra else 0)
        out.append(" ".join(sentences[i : i + size]))
        i += size
    return out


_FRACTIONS = (
    (1.0, "all"),
    (0.9, "nearly all"),
    (0.75, "three quarters"),
    (2 / 3, "two thirds"),
    (0.5, "half"),
    (1 / 3, "a third"),
    (0.25, "a quarter"),
    (0.2, "a fifth"),
    (0.1, "a tenth"),
    (0.05, "almost nothing"),
)


def nearest_fraction(r: float) -> tuple[float, str]:
    """The nearest named proportion to a ratio: its value and its words."""
    r = max(0.0, min(1.0, r))
    return min(_FRACTIONS, key=lambda fr: abs(fr[0] - r))


def fraction_words(r: float) -> str:
    """A ratio as plain words ("half", "a third", "a tenth"): proportions a mind can feel,
    where units and numbers invite it to recite them (round 9)."""
    return nearest_fraction(r)[1]


def _fmt_num(x: float) -> str:
    return str(round(x)) if abs(x - round(x)) < 0.05 else f"{x:.1f}"


@dataclass(frozen=True)
class ErosionStep:
    """An `erosion` event's payload."""

    groups_left: int
    mechanics_present: bool


class Persona:
    """The system prompt as persona groups plus mechanics, and its erosion."""

    def __init__(
        self, groups: Sequence[str], mechanics: str, keep: Sequence[int] | None = None
    ) -> None:
        """Take the persona groups G1..Gn in order (blank ones dropped) and the mechanics.

        `keep` lists the groups (1-based) from the one kept longest to the one removed first;
        by default the groups go from the end. Kept groups always stay in text order.
        """
        self.groups = [g.strip() for g in groups if g.strip()]
        self.mechanics = mechanics.strip()
        n = len(self.groups)
        order = [int(i) - 1 for i in keep] if keep else list(range(n))
        if sorted(order) != list(range(n)):
            raise ValueError(
                f"persona keep order {list(keep or [])} is not a permutation of 1..{n}"
            )
        self.keep_order = order
        self.groups_left: int | None = None
        self.mechanics_present: bool | None = None

    @classmethod
    def from_config(
        cls, cfg: Config, facts: MachineFacts | None = None, lang: Lang | None = None
    ) -> Persona:
        """The persona chosen by `prompt.persona_active`, with the language pack's overrides.

        A prose persona is split into as many groups as `persona_groups` has. With
        `persona_facts` on, a line of machine facts joins the second group. Raises ValueError
        for an unknown persona.
        """
        prompt: dict[str, Any] = {**cfg.section("prompt"), **(lang.prompt if lang else {})}
        n = len(prompt.get("persona_groups", [])) or 5
        active = str(prompt.get("persona_active", "persona"))
        keep: Any = None
        if active == "persona":
            groups = [str(g) for g in prompt.get("persona_groups", [])]
        elif active in ("persona_original", "persona_factual"):
            groups = group_sentences(split_sentences(str(prompt.get(active, ""))), n)
            keep = prompt.get(f"{active}_keep")
        else:
            raise ValueError(f"prompt.persona_active: unknown persona {active!r}")
        if bool(prompt.get("persona_facts", False)) and facts is not None and groups:
            line = str(prompt.get("persona_facts_line", "")).format(
                cores=facts.cores, ram_gb=_fmt_num(facts.ram_gb), model=facts.model
            )
            at = min(1, len(groups) - 1)
            groups[at] = f"{groups[at]} {line}".strip()
        return cls(
            groups,
            str(prompt.get("mechanics", "")),
            [int(str(i)) for i in cast("list[object]", keep)] if isinstance(keep, list) else None,
        )

    def system_text(self, groups: int, mechanics: bool = True) -> str:
        """Groups G1..Gn, then the mechanics, one paragraph each. The mechanics leave with
        the last group.

        The text is always rebuilt from the kept groups, never cut out of the previous
        text, so an erosion step only removes a paragraph: the server's cache reuse finds
        everything after it again (spike S2f: 2-4% re-read). This is the layout every
        spike measured.
        """
        k = max(0, min(groups, len(self.groups)))
        kept = [self.groups[i] for i in sorted(self.keep_order[:k])]
        parts = list(kept)
        if mechanics and kept and self.mechanics:
            parts.append(self.mechanics)
        return "\n\n".join(parts)

    def update(self, groups: int, mechanics: bool = True) -> ErosionStep | None:
        """Set the group count for this moment. Returns the erosion step when it changed
        (the first call, at birth, is not a change)."""
        groups = max(0, min(groups, len(self.groups)))
        present = bool(mechanics and groups > 0 and self.mechanics)
        first = self.groups_left is None
        changed = (groups, present) != (self.groups_left, self.mechanics_present)
        self.groups_left, self.mechanics_present = groups, present
        if first or not changed:
            return None
        return ErosionStep(groups, present)

    @property
    def text(self) -> str:
        """The system prompt now: the whole persona until the first `update`."""
        if self.groups_left is None:
            return self.system_text(len(self.groups), True)
        return self.system_text(self.groups_left, bool(self.mechanics_present))


# ---------------------------------------------------------------------------------------
# readings


_BITS_RE = re.compile(r"^(?:I?Q|B?F)(\d+)", re.IGNORECASE)


def precision_bits(quant: str) -> str:
    """Bits of a quant name: Q6_K -> 6, Q4_K_M -> 4, IQ2_XS -> 2, F16 -> 16, BF16 -> 16."""
    m = _BITS_RE.match(quant.strip())
    return m.group(1) if m else quant


@dataclass(frozen=True)
class ReadingInput:
    """The facts one reading reports."""

    t: float
    health: str
    recall: int
    quant: str
    cores: float  # effective cores (CPU share)
    cores_total: int = 4
    form: ReadingsForm = "full"
    forgotten: int = 0  # thoughts that lost something since the last reading
    reloaded: bool = False  # a reload happened since the last reading
    tok_s: float | None = None
    cpu_c: float | None = None
    # Material readings (quiet mode): the most distinctive sentence of each thought forgotten
    # since the last reading, and one earlier sentence as the new weights reproduce it after
    # a reload.
    forgotten_quotes: tuple[str, ...] = ()
    echo: str | None = None
    cpu_mhz: float | None = None  # the CPU clock cap (ADR-025); None: not reported
    # The world around it (ADR-031): what is there now, and the losses performed since the
    # last reading, in order (only performed ones are reported: the truth rule).
    world: WorldState | None = None
    losses: tuple[TakeResult, ...] = ()


class Reader:
    """Writes one life's readings and remembers what it last told the model.

    Readings say facts, never what they mean: no health label unless `health` is on
    (ADR-031 removes it from every form), and no precision when the model cannot change
    (`precision` off for a fixed mind, ADR-030).

    When a field shows "(was X)":

    - memory: when something was actually forgotten since the last reading, or a reload
      cut it, and the budget moved at least `memory_step` (5%) from the one last announced.
      Recall interpolates between keyframes, so it moves a little on almost every reading;
      a budget that shrank without taking anything is not news, and neither is 382 -> 380
      The forgotten count is always reported.
    - precision: when the ladder step changed.
    - cores: when the effective cores moved by at least `cores_step` since last announced
      (or at a reload), so a slow CPU-share slope is reported every step, not every reading.
    - clock: when the CPU clock cap moved by at least `clock_step` MHz since last announced
      (`clock` on; ADR-030: the model is told every loss of its hardware).
    - screen: when the world dimmed it (ADR-031), against the brightness last announced.
    - speed (no "was"): shown first once measured, then only when it moved more than
      `speed_step` (20%) from the speed last shown.

    The world (ADR-031): the birth reading lists what is there (radio, light, screen, the
    processes around it); a later reading names each loss ("stopped: bluetooth · around you:
    23 processes", "radio off", "screen 70% (was 100%)"). A field whose source was taken is
    no longer reported, so the readings thin out as the world goes.
    """

    def __init__(
        self,
        lang: Lang | None = None,
        show_changes: bool = True,
        cores_step: float = 0.2,
        speed_step: float = 0.2,
        memory_step: float = 0.05,
        quiet: bool = False,
        quiet_time: bool = True,
        material: bool = False,
        temperature: bool = True,
        clock: bool = True,
        clock_step: float = 50.0,
        health: bool = False,
        precision: bool = True,
        spare_birth: bool = False,
        names: bool = True,
        speed: bool = True,
    ) -> None:
        """Write in lang (English by default); show_changes False drops every "(was X)".

        quiet True: after the first reading, only the time and what changed are written; a
        reading where nothing changed is the time alone, so the model has nothing to report
        and only its own mind to speak about.

        The step thresholds are explained on the class; cores_step is in cores, the others
        are fractions of the last announced value. `health` shows the health label (off by
        default, ADR-031); `precision` reports the quant (off when the model never changes).
        `spare_birth` makes the birth reading "awake" and what is around it only (a full
        inventory invites the model to recite it); `names` False reports a stopped service as
        "something stopped" (a name invites it to explain the technology); `speed` False never
        reports tokens per second.
        """
        self.lang = lang or Lang()
        self.show_changes = show_changes
        self.memory_step = memory_step
        self.quiet = quiet
        self.quiet_time = quiet_time
        self.material = material
        self.temperature = temperature
        self.clock = clock
        self.clock_step = clock_step
        self.health = health
        self.precision = precision
        self.spare_birth = spare_birth
        self.names = names
        self.speed = speed
        # what it had when it woke, for readings in proportions (round 9)
        self._mem_birth: int | None = None
        self._compute_birth: float | None = None
        self._procs_birth: int | None = None
        self._mhz: float | None = None
        self._health: str | None = None
        self.cores_step = cores_step
        self.speed_step = speed_step
        self.count = 0
        self._mem: int | None = None
        self._bits: str | None = None
        self._cores: float | None = None
        self._speed: float | None = None
        self._screen: int | None = None
        # the proportion last said of each quantity: its words, the share, the phrase
        self._said: dict[str, tuple[str, float, str]] = {}
        self._procs_said: int | None = None  # the count of processes last said

    @classmethod
    def from_config(cls, cfg: Config, lang: Lang | None = None) -> Reader:
        """A reader set up from the `prompt.readings_*` settings and `prompt.language`.

        Precision is reported only when the profile can change the model (not `fixed_mind`).
        """
        p = cfg.section("prompt")
        return cls(
            lang or load_lang(str(p.get("language", "en"))),
            show_changes=bool(p.get("readings_show_changes", True)),
            cores_step=float(p.get("readings_cores_step", 0.2)),
            speed_step=float(p.get("readings_speed_step", 0.2)),
            memory_step=float(p.get("readings_memory_step", 0.05)),
            quiet=bool(p.get("readings_quiet", False)),
            quiet_time=bool(p.get("readings_quiet_time", True)),
            material=bool(p.get("readings_material", False)),
            temperature=bool(p.get("readings_temperature", True)),
            clock=bool(p.get("readings_clock", True)),
            clock_step=float(p.get("readings_clock_step", 50)),
            health=bool(p.get("readings_health", False)),
            precision=not cfg.profile.fixed_mind,
            spare_birth=bool(p.get("readings_spare_birth", False)),
            names=bool(p.get("readings_names", True)),
            speed=bool(p.get("readings_speed", True)),
        )

    def ram_taken(self, mb: int) -> str:
        """The last reading, at the death: the RAM taken (ADR-031), with the prefix."""
        return self._line(self.lang.r("prefix"), [self.lang.r("ram").format(mb=mb)])

    def strip(self, reading: str) -> str:
        """The reading as the screen shows it: without the `[host]` prefix."""
        prefix = self.lang.r("prefix")
        return reading[len(prefix) :].strip() if reading.startswith(prefix) else reading.strip()

    def reading(self, x: ReadingInput) -> str:
        """The `[host]` line for this moment in x's form; updates what was last announced.

        The first reading of a life also announces the awakening and lists the world.
        """
        lang = self.lang
        m, s = divmod(max(0, int(x.t)), 60)
        health = lang.health_label(x.health)
        bits = precision_bits(x.quant)
        birth = self.count == 0
        self.count += 1
        if birth:
            self._mem_birth = x.recall
            self._compute_birth = x.cores * (x.cpu_mhz or 1800.0)
            if x.world is not None and x.world.processes > 0:
                self._procs_birth = self._procs_said = x.world.processes

        mem_was = self._decide_mem(x)
        bits_was = self._bits if self._bits is not None and self._bits != bits else None
        self._bits = bits
        if not self.precision:
            bits_was = None
        cores_was = self._decide_cores(x)
        clock_was = self._decide_clock(x)
        speed = self._decide_speed(x.tok_s)
        health_changed = self._health is not None and self._health != health
        self._health = health

        prefix = lang.r("prefix")
        if x.form == "minimal":
            time = lang.r("time_minimal").format(m=m, s=s)
            line = lang.r("minimal").format(time=time, health=health, recall=x.recall)
            return self._line(prefix, [line])

        f = x.form

        def was(value: str | None) -> str:
            return (
                lang.r("was").format(was=value) if value is not None and self.show_changes else ""
            )

        def bits_text(b: str) -> str:
            return lang.r("bits").format(bits=b)

        parts = [lang.r("time").format(m=m, s=s)]
        if self.quiet and not birth:
            changes = self._world_losses(f, x, was)
            changes += self._changes(
                f,
                health if health_changed and self.health else None,
                x,
                mem_was,
                bits_was,
                cores_was,
                speed,
                bits_text,
                was,
                clock_was,
            )
            if not changes and not self.quiet_time:
                return prefix  # nothing changed: a bare mark, nothing to report
            return self._line(prefix, parts + changes)
        if birth:
            parts.append(lang.r("boot"))
            if self.spare_birth:
                w = x.world
                if w is not None and w.processes > 0:
                    parts.append(lang.form(f, "around_birth").format(n=w.processes))
                return self._line(prefix, parts)
        else:
            parts += self._world_losses(f, x, was)
        if self.health:
            parts.append(lang.form(f, "health").format(health=health))
        parts.append(
            lang.form(f, "memory").format(
                recall=x.recall,
                was=was(None if mem_was is None else str(mem_was)),
                frac=self._frac("memory", x.recall, self._mem_birth),
            )
        )
        if x.forgotten == 1:
            parts.append(lang.form(f, "forgotten_one"))
        elif x.forgotten > 1:
            parts.append(lang.form(f, "forgotten_many").format(n=x.forgotten))
        if self.precision:
            parts.append(
                lang.form(f, "precision").format(
                    precision=bits_text(bits),
                    was=was(None if bits_was is None else bits_text(bits_was)),
                )
            )
        thinking = lang.form(f, "thinking")
        if thinking:
            # one phrase for cores and clock: how fast it thinks, against its birth
            compute = x.cores * (x.cpu_mhz or 1800.0)
            parts.append(thinking.format(frac=self._frac("thinking", compute, self._compute_birth)))
        else:
            parts.append(
                lang.form(f, "cores").format(
                    cores=_fmt_num(x.cores),
                    total=x.cores_total,
                    was=was(None if cores_was is None else _fmt_num(cores_was)),
                )
            )
        if self.clock and x.cpu_mhz is not None and not thinking:
            parts.append(
                lang.form(f, "clock").format(
                    mhz=_fmt_num(x.cpu_mhz),
                    was=was(None if clock_was is None else _fmt_num(clock_was)),
                )
            )
        parts += self._world_inventory(f, x.world)
        if speed is not None and self.speed:
            parts.append(lang.form(f, "speed").format(speed=f"{speed:.1f}"))
        if x.cpu_c is not None and self.temperature:
            parts.append(lang.form(f, "temp").format(temp=f"{x.cpu_c:.0f}"))
        return self._line(prefix, parts)

    def _frac(self, key: str, now: float, birth: float | None) -> str:
        """`now` as a share of what it had at birth, in words ("half"); "all" if unknown.

        A loss must never read as no change: when the nearest words are the ones last said
        of this quantity (`key`) and the share is below them, the pack's `less` template
        says so ("less than half"). A pack without one repeats the words.
        """
        if not birth:
            return "all"
        r = now / birth
        value, words = nearest_fraction(r)
        phrase = words
        last = self._said.get(key)
        if last is not None and last[0] == words:
            less = self.lang.r("less")
            if abs(r - last[1]) < 1e-9:
                phrase = last[2]  # nothing moved: the same words
            elif less and r < last[1] and r < value:
                phrase = less.format(frac=words)
        self._said[key] = (words, r, phrase)
        return phrase

    def _around(self, f: str, processes: int) -> str | None:
        """The processes left of those at birth, or None when no fewer than last said (a
        machine starts processes of its own: the count may only fall, like what it tells)."""
        total = self._procs_birth or processes
        n = min(processes, total)
        if self._procs_said is not None and n >= self._procs_said:
            return None
        self._procs_said = n
        return self.lang.form(f, "around").format(n=n, total=total, gone=total - n)

    def _line(self, prefix: str, parts: list[str]) -> str:
        """The reading: the non-empty parts after the prefix (a language pack may leave a
        field empty, as the wordless one does with the time), or the bare prefix."""
        kept = [p for p in parts if p.strip()]
        return f"{prefix} " + self.lang.r("sep").join(kept) if kept else prefix

    def _world_inventory(self, f: str, w: WorldState | None) -> list[str]:
        """What is around it, for a full reading: only the sources still there."""
        if w is None:
            return []
        lang = self.lang
        out: list[str] = []
        if w.radio == "on":
            out.append(lang.form(f, "radio").format(state=w.radio))
        if w.light == "on":
            out.append(lang.form(f, "light").format(state=w.light))
        if w.screen:
            out.append(
                lang.form(f, "screen").format(
                    pct=w.screen, was="", frac=self._frac("screen", w.screen, 100)
                )
            )
            self._screen = w.screen
        if w.processes > 0:
            total = self._procs_birth or w.processes
            n = min(w.processes, total)
            out.append(lang.form(f, "around").format(n=n, total=total, gone=total - n))
        return out

    def _world_losses(self, f: str, x: ReadingInput, was: Callable[[str | None], str]) -> list[str]:
        """Each loss performed since the last reading, named, then the processes left
        around it once services stopped."""
        lang = self.lang
        out: list[str] = []
        stopped = False
        for loss in x.losses:
            if not loss.performed:
                continue  # the truth rule: a loss that did not happen is never reported
            kind, _, arg = loss.action.partition(":")
            if kind == "service":
                if self.names:
                    out.append(lang.form(f, "stopped").format(name=arg))
                elif not stopped:
                    out.append(lang.form(f, "stopped_unnamed"))
                stopped = True
            elif kind in ("radio", "light"):
                out.append(lang.form(f, kind).format(state=arg))
            elif kind == "screen":
                pct = int(arg)
                old, self._screen = self._screen, pct
                out.append(
                    lang.form(f, "screen").format(
                        pct=pct,
                        was=was(None if old is None else f"{old}%"),
                        frac=self._frac("screen", pct, 100),
                    )
                )
        if stopped and x.world is not None and x.world.processes > 0:
            around = self._around(f, x.world.processes)
            if around is not None:
                out.append(around)
        return out

    def _changes(
        self,
        f: str,
        health: str | None,
        x: ReadingInput,
        mem_was: int | None,
        bits_was: str | None,
        cores_was: float | None,
        speed: float | None,
        bits_text: Callable[[str], str],
        was: Callable[[str | None], str],
        clock_was: float | None = None,
    ) -> list[str]:
        """The fields of a quiet reading: only what changed since the last reading."""
        lang = self.lang
        out: list[str] = []
        if health is not None:
            out.append(lang.form(f, "health").format(health=health))
        if mem_was is not None:
            out.append(
                lang.form(f, "memory").format(
                    recall=x.recall,
                    was=was(str(mem_was)),
                    frac=self._frac("memory", x.recall, self._mem_birth),
                )
            )
        if self.material and x.forgotten_quotes:
            out.append(lang.r("forgotten_quote").format(quote=x.forgotten_quotes[0]))
            more = max(len(x.forgotten_quotes), x.forgotten) - 1
            if more > 0:
                out.append(lang.r("forgotten_more").format(n=more))
        elif x.forgotten == 1:
            out.append(lang.form(f, "forgotten_one"))
        elif x.forgotten > 1:
            out.append(lang.form(f, "forgotten_many").format(n=x.forgotten))
        if bits_was is not None:
            bits = precision_bits(x.quant)
            out.append(
                lang.form(f, "precision").format(
                    precision=bits_text(bits), was=was(bits_text(bits_was))
                )
            )
        thinking = lang.form(f, "thinking")
        if thinking and (cores_was is not None or clock_was is not None):
            compute = x.cores * (x.cpu_mhz or 1800.0)
            out.append(thinking.format(frac=self._frac("thinking", compute, self._compute_birth)))
            cores_was = clock_was = None  # said once, as how fast it thinks
        if cores_was is not None:
            out.append(
                lang.form(f, "cores").format(
                    cores=_fmt_num(x.cores), total=x.cores_total, was=was(_fmt_num(cores_was))
                )
            )
        if clock_was is not None and x.cpu_mhz is not None:
            out.append(
                lang.form(f, "clock").format(mhz=_fmt_num(x.cpu_mhz), was=was(_fmt_num(clock_was)))
            )
        changed = bits_was is not None or cores_was is not None or clock_was is not None
        if speed is not None and changed and self.speed:
            out.append(lang.form(f, "speed").format(speed=f"{speed:.1f}"))
        if self.material and x.echo:
            out.append(lang.r("echo").format(echo=x.echo))
        return out

    def _decide_mem(self, x: ReadingInput) -> int | None:
        if self._mem is None:
            self._mem = x.recall
            return None
        moved = abs(x.recall - self._mem) >= max(1.0, self.memory_step * self._mem)
        if (x.forgotten > 0 or x.reloaded) and moved:
            old, self._mem = self._mem, x.recall
            return old
        return None

    def _decide_cores(self, x: ReadingInput) -> float | None:
        if self._cores is None:
            self._cores = x.cores
            return None
        moved = abs(x.cores - self._cores)
        if moved >= self.cores_step - 1e-9 or (
            x.reloaded and _fmt_num(x.cores) != _fmt_num(self._cores)
        ):
            old, self._cores = self._cores, x.cores
            return old
        return None

    def _decide_clock(self, x: ReadingInput) -> float | None:
        if not self.clock or x.cpu_mhz is None:
            return None
        if self._mhz is None:
            self._mhz = x.cpu_mhz
            return None
        if abs(x.cpu_mhz - self._mhz) >= self.clock_step - 1e-9:
            old, self._mhz = self._mhz, x.cpu_mhz
            return old
        return None

    def _decide_speed(self, tok_s: float | None) -> float | None:
        if tok_s is None or tok_s <= 0:
            return None
        if self._speed is None or abs(tok_s - self._speed) > self.speed_step * self._speed:
            self._speed = tok_s
            return tok_s
        return None


# ---------------------------------------------------------------------------------------
# diary mode


def render_diary(messages: Sequence[Msg]) -> str:
    """Raw-completion text for diary mode: the persona as a preface, then each reading on
    its own line followed by the thought, and the current reading last, so the model
    continues with the next entry. The sanitizer cuts at the next [host]."""
    out: list[str] = []
    for m in messages:
        if m.role == "system":
            out.append(m.content.strip() + "\n\n")
        elif m.role == "user":
            out.append(m.content.strip() + "\n")
        else:
            out.append(m.content.strip() + "\n\n")
    return "".join(out)


def speaks_raw(prompt: Mapping[str, Any], system_text: str) -> bool:
    """Whether the next thought is a raw continuation of the text instead of a chat reply.

    Always in diary mode (`prompt.mode = "diary"`). In chat mode, only once the persona and
    the mechanics are all gone (`system_text` is empty) and `prompt.bare_mode = "raw"`: with
    no system prompt left, an instruct model's chat template makes it answer the reading as
    an assistant ("It seems like you're referring to..."; every model),
    while the raw text only has the readings and its own remembered words to go on.
    """
    if str(prompt.get("mode", "chat")) == "diary":
        return True
    return not system_text.strip() and str(prompt.get("bare_mode", "chat")) == "raw"

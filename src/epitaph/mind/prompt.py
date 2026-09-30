"""What the model sees: persona, erosion, readings, diary text (BUILD_PLAN 5.4, 5.6).

- `Persona` holds the persona groups and the mechanics. Groups are removed from the end, so
  G1, the knowledge of its death, goes last, and the mechanics go with it in the same
  rebuild. `persona_original` and `persona_factual` are split into sentence groups so they
  erode on the same schedule. The optional facts line joins G2.
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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    "split_sentences",
]


# ---------------------------------------------------------------------------------------
# language packs


_DEFAULT_READINGS: dict[str, Any] = {
    "prefix": "[host]",
    "sep": " · ",
    "boot": "boot complete",
    "was": " (was {was})",
    "bits": "{bits}-bit",
    "time": "t+{m:02d}:{s:02d}",
    "time_minimal": "{m}:{s:02d}",
    "minimal": "{time} · {health} · {recall}",
    "full": {
        "health": "health: {health}",
        "memory": "memory {recall} tokens{was}",
        "forgotten_one": "forgotten: 1 earlier thought",
        "forgotten_many": "forgotten: {n} earlier thoughts",
        "precision": "precision {precision}{was}",
        "cores": "cores {cores} of {total}{was}",
        "speed": "speed {speed} tokens/s",
        "temp": "cpu {temp}°C",
    },
    "short": {
        "health": "{health}",
        "memory": "memory {recall}{was}",
        "forgotten_one": "forgot 1",
        "forgotten_many": "forgot {n}",
        "precision": "{precision}{was}",
        "cores": "cores {cores} of {total}{was}",
        "speed": "{speed}/s",
        "temp": "{temp}°C",
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
        return str(self.readings.get(key, _DEFAULT_READINGS[key]))

    def form(self, form: str, key: str) -> str:
        table: Mapping[str, Any] = self.readings.get(form, {})
        default: Mapping[str, Any] = _DEFAULT_READINGS[form]
        return str(table.get(key, default[key]))

    def health_label(self, health: str) -> str:
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


def _fmt_num(x: float) -> str:
    return str(round(x)) if abs(x - round(x)) < 0.05 else f"{x:.1f}"


@dataclass(frozen=True)
class ErosionStep:
    """An `erosion` event's payload."""

    groups_left: int
    mechanics_present: bool


class Persona:
    """The system prompt as persona groups plus mechanics, and its erosion."""

    def __init__(self, groups: Sequence[str], mechanics: str) -> None:
        self.groups = [g.strip() for g in groups if g.strip()]
        self.mechanics = mechanics.strip()
        self.groups_left: int | None = None
        self.mechanics_present: bool | None = None

    @classmethod
    def from_config(
        cls, cfg: Config, facts: MachineFacts | None = None, lang: Lang | None = None
    ) -> Persona:
        prompt: dict[str, Any] = {**cfg.section("prompt"), **(lang.prompt if lang else {})}
        n = len(prompt.get("persona_groups", [])) or 5
        active = str(prompt.get("persona_active", "persona"))
        if active == "persona":
            groups = [str(g) for g in prompt.get("persona_groups", [])]
        elif active in ("persona_original", "persona_factual"):
            groups = group_sentences(split_sentences(str(prompt.get(active, ""))), n)
        else:
            raise ValueError(f"prompt.persona_active: unknown persona {active!r}")
        if bool(prompt.get("persona_facts", False)) and facts is not None and groups:
            line = str(prompt.get("persona_facts_line", "")).format(
                cores=facts.cores, ram_gb=_fmt_num(facts.ram_gb), model=facts.model
            )
            at = min(1, len(groups) - 1)
            groups[at] = f"{groups[at]} {line}".strip()
        return cls(groups, str(prompt.get("mechanics", "")))

    def system_text(self, groups: int, mechanics: bool = True) -> str:
        """Groups G1..Gn, then the mechanics. The mechanics leave with the last group."""
        kept = self.groups[: max(0, min(groups, len(self.groups)))]
        parts = [" ".join(kept)] if kept else []
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


class Reader:
    """Writes one life's readings and remembers what it last told the model.

    When a field shows "(was X)":

    - memory: when something was actually forgotten since the last reading, or a reload
      cut it, and the budget moved at least `memory_step` (5%) from the one last announced.
      Recall interpolates between keyframes, so it moves a little on almost every reading;
      a budget that shrank without taking anything is not news, and neither is 382 -> 380
      (answers docs/QUESTIONS.md #1). The forgotten count is always reported.
    - precision: when the ladder step changed.
    - cores: when the effective cores moved by at least `cores_step` since last announced
      (or at a reload), so a slow CPU-share slope is reported every step, not every reading.
    - speed (no "was"): shown first once measured, then only when it moved more than
      `speed_step` (20%) from the speed last shown.
    """

    def __init__(
        self,
        lang: Lang | None = None,
        show_changes: bool = True,
        cores_step: float = 0.2,
        speed_step: float = 0.2,
        memory_step: float = 0.05,
    ) -> None:
        self.lang = lang or Lang()
        self.show_changes = show_changes
        self.memory_step = memory_step
        self.cores_step = cores_step
        self.speed_step = speed_step
        self.count = 0
        self._mem: int | None = None
        self._bits: str | None = None
        self._cores: float | None = None
        self._speed: float | None = None

    @classmethod
    def from_config(cls, cfg: Config, lang: Lang | None = None) -> Reader:
        p = cfg.section("prompt")
        return cls(
            lang or load_lang(str(p.get("language", "en"))),
            show_changes=bool(p.get("readings_show_changes", True)),
            cores_step=float(p.get("readings_cores_step", 0.2)),
            speed_step=float(p.get("readings_speed_step", 0.2)),
            memory_step=float(p.get("readings_memory_step", 0.05)),
        )

    def reading(self, x: ReadingInput) -> str:
        lang = self.lang
        m, s = divmod(max(0, int(x.t)), 60)
        health = lang.health_label(x.health)
        bits = precision_bits(x.quant)
        birth = self.count == 0
        self.count += 1

        mem_was = self._decide_mem(x)
        bits_was = self._bits if self._bits is not None and self._bits != bits else None
        self._bits = bits
        cores_was = self._decide_cores(x)
        speed = self._decide_speed(x.tok_s)

        prefix = lang.r("prefix")
        if x.form == "minimal":
            time = lang.r("time_minimal").format(m=m, s=s)
            line = lang.r("minimal").format(time=time, health=health, recall=x.recall)
            return f"{prefix} {line}"

        f = x.form

        def was(value: str | None) -> str:
            return (
                lang.r("was").format(was=value) if value is not None and self.show_changes else ""
            )

        def bits_text(b: str) -> str:
            return lang.r("bits").format(bits=b)

        parts = [lang.r("time").format(m=m, s=s)]
        if birth:
            parts.append(lang.r("boot"))
        parts.append(lang.form(f, "health").format(health=health))
        parts.append(
            lang.form(f, "memory").format(
                recall=x.recall, was=was(None if mem_was is None else str(mem_was))
            )
        )
        if x.forgotten == 1:
            parts.append(lang.form(f, "forgotten_one"))
        elif x.forgotten > 1:
            parts.append(lang.form(f, "forgotten_many").format(n=x.forgotten))
        parts.append(
            lang.form(f, "precision").format(
                precision=bits_text(bits),
                was=was(None if bits_was is None else bits_text(bits_was)),
            )
        )
        parts.append(
            lang.form(f, "cores").format(
                cores=_fmt_num(x.cores),
                total=x.cores_total,
                was=was(None if cores_was is None else _fmt_num(cores_was)),
            )
        )
        if speed is not None:
            parts.append(lang.form(f, "speed").format(speed=f"{speed:.1f}"))
        if x.cpu_c is not None:
            parts.append(lang.form(f, "temp").format(temp=f"{x.cpu_c:.0f}"))
        return f"{prefix} " + lang.r("sep").join(parts)

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

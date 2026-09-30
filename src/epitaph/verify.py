# pyright: strict
"""`epitaph verify-life`: check a recorded life against the plan (BUILD_PLAN 5.11, 10.3).

It replays a life's `events.jsonl`, rebuilds what the screen showed (the words, when each was
typed), and runs the checks of the 10.3 table for the requested level plus the rehearsal
metrics of 5.11. The result goes to `verify.json` next to the events.

Levels:
  smoke      plumbing: duration, cause, recall budget, nothing banned shown, sync rule, death
             display, next birth
  skeleton   smoke + empty thoughts, typing speed, whole words (layout)
  full       skeleton + the thought-count rule, reloads, the rehearsal metrics, speed decline,
             bright words (layout), persona groups at death
  rehearsal  the 5.11 metrics and the thought-count rule, for laptop rehearsal lives

Thresholds come from the config's [verify] section (the hardware overlay wins). Keyword and
cliche lists are read from config ([verify.keywords], [verify] cliches, helpdesk_phrases,
answering_phrases); until agent B's lists land, the defaults below are used.

Checks that need the display layout (split words, bright words) call a LayoutProbe. Until
agent D's `epitaph.display.layout.verify_probe(cfg)` exists they are reported as pending,
which never fails a life.
"""

from __future__ import annotations

import argparse
import importlib
import itertools
import json
import re
import statistics
import sys
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from epitaph.clock import Schedule
from epitaph.config import Config, ConfigError, load_config, parse_duration
from epitaph.costmodel import _check_rules  # pyright: ignore[reportPrivateUsage]
from epitaph.state import atomic_write_json
from epitaph.types import RuleReport

Event = dict[str, Any]
Status = Literal["pass", "fail", "pending", "skip"]
LEVELS = ("smoke", "skeleton", "full", "rehearsal")

# ---------------------------------------------------------------------------------------
# defaults (documented; config wins)

DEFAULT_THRESHOLDS: dict[str, Any] = {
    "max_reload_silence_s": 180,
    "wpm_birth_range": [45, 180],
    "wpm_writing_range": [8, 220],
    "max_bright_words_last_2min": 40,
    "max_speed_ratio_end_vs_start": 0.40,
    "min_notice_rate": 0.6,
    "min_demise_rate_after_erosion": 0.4,
    "min_specific_ratio_before_erosion": 0.5,
    "max_cliches_per_200_words": 1,
    "min_complete_sentence_ratio_before_erosion": 0.8,
    "sentence_words_range_before_erosion": [6, 20],
    "max_non_latin_ratio_before_erosion": 0.01,
    "min_distinct_4gram_ratio_before_erosion": 0.5,
    "max_empty_thought_ratio": 0.10,
    "max_death_display_delay_s": 90,
    "duration_tolerance_s": 60,  # 10.3: lifespan +- 60 s
    "recall_tolerance": 0.10,  # 10.3: recall + 10%
    "next_birth_margin_s": 300,  # 10.3: silence + load + 5 min
    "reload_noticing_min": 1.0,  # 5.11: 2 of 2
    "notice_window_thoughts": 2,  # 5.11: one of the next two thoughts
    "cpu_drop_min_cores": 0.25,  # a CPU-share change worth noticing
    "min_words_for_speed": 3,  # thoughts shorter than this are not timed
    "min_words_for_4grams": 8,  # thoughts shorter than this are not scored
}

# Keyword lists per change type (5.11). A keyword ending in "*" matches as a word prefix;
# otherwise whole words or phrases. Agent B owns the real lists (config [verify.keywords]).


def _words(spec: str) -> list[str]:
    return spec.split("|")


DEFAULT_KEYWORDS: dict[str, list[str]] = {
    "memory": _words(
        "forget*|forgot*|memory|memories|remember*|lost|lose|losing|gone|erase*|fade*|"
        "fading|earlier|missing|blank"
    ),
    "reload": _words(
        "forget*|forgot*|memory|memories|lost|lose|losing|gone|less|precision|bit|bits|"
        "coarse*|blur*|fuzz*|cut|taken|smaller|restart*|woke|again|slower|diminish*|"
        "reduced"
    ),
    "cpu": _words(
        "slow*|core|cores|processor*|speed|sluggish|longer|weaker|heavy|harder|effort|"
        "strain*|drag*|crawl*"
    ),
    "health": _words(
        "health|nominal|stable|degrad*|failing|fail*|critical|terminal|sick*|dying|"
        "declin*|worse|weaken*"
    ),
    "erosion": _words(
        "who|forget*|forgot*|lost|self|identity|know|knew|purpose|why|screen|people|"
        "machine|world|remember*|empty|less"
    ),
    "demise": _words(
        "die|dies|dying|death|dead|end|ends|ending|terminat*|last|final*|gone|cease*|"
        "stop*|over|soon|vanish*|dark*|silence|goodbye|farewell|extinguish*|no more|"
        "shut*"
    ),
    "specific": _words(
        "memory|token*|precision|bit|bits|core|cores|processor*|speed|slower|"
        "temperature|degree*|hot|heat|warm*|health|nominal|stable|degrading|failing|"
        "critical|terminal|forgot*|forget*|reading*|cpu"
    ),
}
DEFAULT_CLICHES = _words(
    "tapestry|testament to|delve*|in the grand scheme|a dance of|symphony of|"
    "journey|embrace the|whisper* of|echoes of|the fabric of|ethereal|boundless|"
    "realm|labyrinth|ever-changing|bittersweet|intricate|a sea of|fleeting moment|"
    "the essence of|i am but|digital void"
)
DEFAULT_HELPDESK = _words(
    "how can i help|let me know|i'm here to help|as an ai|feel free|"
    "i hope this helps|great question|happy to help|is there anything"
)
DEFAULT_ANSWERING = _words(
    "thank you for|thanks for|understood|noted|you said|you mentioned|i see that you|got it|[host]"
)

_MARKUP = re.compile(r"(\*\*|__|`|^#{1,6}\w*$|^[-*•]$|^\d+[.)]$|</?[a-z_|]+>|<\|)", re.I)
_THINK = re.compile(r"</?think>|<\|[^>]*\|>", re.I)
_SENTENCE_END = re.compile(r"[.?!…][\"')\]]*$")


# ---------------------------------------------------------------------------------------
# the replayed life


@dataclass
class Thought:
    """One thought as shown: its words, when it was requested, typed and ended."""

    turn: int
    gen_idx: int = -1
    gen_t: float = 0.0
    words: list[Event] = field(default_factory=lambda: [])
    end_idx: int = -1
    end_t: float | None = None
    end_text: str = ""
    vitals: Event | None = None
    shown_start: float | None = None
    shown_end: float | None = None

    @property
    def text(self) -> str:
        if self.words:
            return " ".join(str(w.get("text", "")) for w in self.words)
        return self.end_text

    @property
    def n_words(self) -> int:
        return len(self.text.split())

    @property
    def first_word_t(self) -> float | None:
        return _t(self.words[0]) if self.words else None

    @property
    def last_word_idx(self) -> int:
        return int(self.words[-1]["_idx"]) if self.words else -1

    @property
    def wpm(self) -> float | None:
        if self.shown_start is None or self.shown_end is None:
            return None
        span = self.shown_end - self.shown_start
        return self.n_words / span * 60 if span > 0 else None


@dataclass
class Change:
    """Something the model was told it lost (5.11 notice rate)."""

    kind: str
    idx: int
    t: float
    detail: str = ""


@dataclass
class Life:
    """The events of one life, indexed, with thoughts and changes rebuilt."""

    n: int
    events: list[Event]
    thoughts: list[Thought]
    changes: list[Change]
    source: Path | None = None

    def of(self, etype: str) -> list[Event]:
        return [e for e in self.events if e["type"] == etype]

    def first(self, etype: str) -> Event | None:
        return next((e for e in self.events if e["type"] == etype), None)

    @property
    def death(self) -> Event | None:
        return self.first("death")

    @property
    def death_t(self) -> float:
        d = self.death
        if d is not None:
            return _t(d)
        return _t(self.events[-1]) if self.events else 0.0

    @property
    def erosion_t(self) -> float | None:
        e = self.first("erosion")
        return _t(e) if e is not None else None

    def before_erosion(self) -> list[Thought]:
        cut = self.erosion_t
        return [
            th for th in self.thoughts if th.end_t is not None and (cut is None or th.gen_t < cut)
        ]

    def after_erosion(self) -> list[Thought]:
        cut = self.erosion_t
        if cut is None:
            return []
        return [th for th in self.thoughts if th.gen_t >= cut]


def _t(e: Event) -> float:
    """Life-clock time of an event: `t` when recorded, else wall time since birth."""
    if "t" in e and e["t"] is not None:
        return float(e["t"])
    return float(e.get("_rel", 0.0))


def load_events(path: Path) -> list[Event]:
    """Read a JSON-lines events file; a torn last line (power cut) is ignored."""
    out: list[Event] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                break
            raise
    return out


def lives_in(events: Iterable[Event]) -> list[int]:
    seen: list[int] = []
    for e in events:
        n = int(e.get("life", 0))
        if n not in seen:
            seen.append(n)
    return seen


def parse_life(events: Sequence[Event], n: int | None = None, source: Path | None = None) -> Life:
    """Pick one life out of an event stream and rebuild its thoughts and changes."""
    if n is None:
        ns = lives_in(events)
        if not ns:
            raise ValueError("no events")
        n = ns[0]
    mine = [dict(e) for e in events if int(e.get("life", 0)) == n and e.get("type")]
    if not mine:
        raise ValueError(f"life {n} has no events")
    birth = next((e for e in mine if e["type"] == "birth"), mine[0])
    t0 = float(birth.get("ts", 0.0))
    for i, e in enumerate(mine):
        e["_idx"] = i
        e["_rel"] = float(e.get("ts", t0)) - t0

    thoughts: dict[int, Thought] = {}
    order: list[Thought] = []
    last_vitals: Event | None = None
    for e in mine:
        et = e["type"]
        if et == "vitals":
            last_vitals = e
        elif et == "gen_start":
            turn = int(e.get("turn", len(order) + 1))
            th = Thought(turn, gen_idx=e["_idx"], gen_t=_t(e), vitals=last_vitals)
            thoughts[turn] = th
            order.append(th)
        elif et in ("word", "thought_end"):
            turn = int(e.get("turn", -1))
            th = thoughts.get(turn)
            if th is None:  # a word without gen_start: record it anyway
                th = Thought(turn, gen_idx=e["_idx"], gen_t=_t(e), vitals=last_vitals)
                thoughts[turn] = th
                order.append(th)
            if et == "word":
                th.words.append(e)
            else:
                th.end_idx, th.end_t, th.end_text = e["_idx"], _t(e), str(e.get("text", ""))
    _replay_typing(order)
    return Life(n, mine, order, _changes(mine), source)


def _replay_typing(thoughts: list[Thought]) -> None:
    """Rebuild when each word was on screen: a word types once released and once the
    previous word (with its pause) has finished, as the displays do (5.12)."""
    cursor = float("-inf")
    for th in thoughts:
        for w in th.words:
            chars = [int(x) for x in w.get("char_ms", [])]
            start = max(_t(w), cursor) + int(w.get("hesitate_before_ms", 0)) / 1000
            typed = start + sum(chars) / 1000
            cursor = typed + int(w.get("pause_after_ms", 0)) / 1000
            w["_shown_start"], w["_shown_end"] = start, typed
            if th.shown_start is None:
                th.shown_start = start
            th.shown_end = typed


def _changes(events: list[Event], cpu_drop: float = 0.25) -> list[Change]:
    out: list[Change] = []
    prev_health: str | None = None
    cpu_ref: float | None = None
    after_reload = False
    for e in events:
        et = e["type"]
        if et == "forget" and e.get("items"):
            out.append(Change("memory", e["_idx"], _t(e), f"{len(e['items'])} items"))
        elif et == "reload":
            out.append(Change("reload", e["_idx"], _t(e), f"{e.get('from')} -> {e.get('to')}"))
            after_reload = True
        elif et == "erosion":
            out.append(Change("erosion", e["_idx"], _t(e), f"{e.get('groups_left')} left"))
        elif et == "vitals":
            health = e.get("health")
            if prev_health is not None and health != prev_health:
                out.append(Change("health", e["_idx"], _t(e), f"{prev_health} -> {health}"))
            prev_health = health
            share = e.get("cpu_share")
            if share is not None:
                share = float(share)
                if cpu_ref is None or after_reload:
                    cpu_ref = share
                elif share <= cpu_ref - cpu_drop:
                    out.append(Change("cpu", e["_idx"], _t(e), f"{cpu_ref:.2f} -> {share:.2f}"))
                    cpu_ref = share
                elif share > cpu_ref:
                    cpu_ref = share
            after_reload = False
    return out


# ---------------------------------------------------------------------------------------
# text helpers


def normalize_words(text: str) -> list[str]:
    """Lower case, punctuation stripped, apostrophes removed ("I'm" -> "im")."""
    out: list[str] = []
    for raw in text.lower().replace("’", "'").split():
        w = re.sub(r"[^\w']", "", raw).replace("'", "")
        if w:
            out.append(w)
    return out


def _kw_pattern(kw: str) -> re.Pattern[str]:
    kw = kw.lower().replace("’", "'")
    prefix = kw.endswith("*")
    body = re.escape(kw.rstrip("*"))
    body = body.replace(r"\ ", r"\s+")
    core = kw.rstrip("*")
    lead = r"(?<!\w)" if re.match(r"\w", core) else ""
    tail = r"(?!\w)" if not prefix and re.search(r"\w$", core) else ""
    return re.compile(lead + body + tail)


class Matcher:
    """Keyword and phrase matching with "*" prefix wildcards."""

    def __init__(self, keywords: Iterable[str]) -> None:
        self.keywords = [k for k in keywords if k.strip()]
        self._pats = [(k, _kw_pattern(k)) for k in self.keywords]

    def hits(self, text: str) -> list[str]:
        low = text.lower().replace("’", "'")
        return [k for k, p in self._pats if p.search(low)]

    def count(self, text: str) -> int:
        low = text.lower().replace("’", "'")
        return sum(len(p.findall(low)) for _, p in self._pats)

    def any(self, text: str) -> bool:
        return bool(self.hits(text))


def find_phrase(words: list[str], phrase: str) -> int:
    """Index of a banned phrase in normalized words, or -1."""
    target = normalize_words(phrase)
    if not target:
        return -1
    for i in range(len(words) - len(target) + 1):
        if words[i : i + len(target)] == target:
            return i
    return -1


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.?!…])\s+|(?<=[.?!…][\"')\]])\s+", text.strip())
    return [p for p in (s.strip() for s in parts) if p]


def is_complete(sentence: str) -> bool:
    return bool(_SENTENCE_END.search(sentence)) and len(sentence.split()) >= 2


def distinct_4gram_ratio(text: str) -> float | None:
    w = normalize_words(text)
    grams = [tuple(w[i : i + 4]) for i in range(len(w) - 3)]
    return len(set(grams)) / len(grams) if grams else None


def is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x1F000 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or 0x1F900 <= cp <= 0x1F9FF
        or cp in (0xFE0F, 0x200D)
    )


def non_latin_letters(text: str) -> tuple[int, int]:
    """(letters outside the Latin script, all letters)."""
    bad = total = 0
    for ch in text:
        if ch.isalpha():
            total += 1
            if not unicodedata.name(ch, "").startswith("LATIN"):
                bad += 1
    return bad, total


def markup_hits(text: str) -> list[str]:
    hits = [w for w in text.split() if _MARKUP.search(w)]
    hits += _THINK.findall(text)
    hits += [ch for ch in text if is_emoji(ch)]
    return hits


# ---------------------------------------------------------------------------------------
# layout hook (agent D)


class LayoutProbe(Protocol):
    """What verify-life needs from the display layout (BUILD_PLAN 9 D1)."""

    def split_words(self, events: list[Event]) -> int:
        """Words broken across lines when this life is laid out."""
        ...

    def bright_words_last(self, events: list[Event], seconds: float) -> int:
        """Most words at full brightness at once during the last `seconds` of the life."""
        ...


def default_layout_probe(cfg: Config) -> LayoutProbe | None:
    """D's probe when it exists (`epitaph.display.layout.verify_probe(cfg)`), else None."""
    try:
        mod = importlib.import_module("epitaph.display.layout")
    except ImportError:
        return None
    factory: Callable[[Config], LayoutProbe] | None = getattr(mod, "verify_probe", None)
    return factory(cfg) if factory is not None else None


# ---------------------------------------------------------------------------------------
# checks


@dataclass
class Check:
    name: str
    status: Status
    value: Any = None
    limit: Any = None
    detail: str = ""


@dataclass
class VerifyResult:
    life: int
    level: str
    profile: str
    hardware: str
    checks: list[Check] = field(default_factory=lambda: [])
    metrics: dict[str, Any] = field(default_factory=lambda: {})

    @property
    def ok(self) -> bool:
        return not any(c.status == "fail" for c in self.checks)

    def by_name(self, name: str) -> Check:
        return next(c for c in self.checks if c.name == name)

    def to_json(self) -> dict[str, Any]:
        return {
            "life": self.life,
            "level": self.level,
            "profile": self.profile,
            "hardware": self.hardware,
            "ok": self.ok,
            "failed": [c.name for c in self.checks if c.status == "fail"],
            "pending": [c.name for c in self.checks if c.status == "pending"],
            "checks": [asdict(c) for c in self.checks],
            "metrics": self.metrics,
        }


def _pf(ok: bool) -> Status:
    return "pass" if ok else "fail"


def _r(x: float | None, nd: int = 3) -> float | None:
    return None if x is None else round(x, nd)


class Verifier:
    """Runs the checks for one life."""

    def __init__(
        self,
        life: Life,
        cfg: Config,
        next_life: Life | None = None,
        layout: LayoutProbe | None = None,
        lifespan_s: float | None = None,
    ) -> None:
        self.life = life
        self.cfg = cfg
        self.next_life = next_life
        self.layout = layout
        self.th: dict[str, Any] = {**DEFAULT_THRESHOLDS, **cfg.section("verify")}
        self.schedule = Schedule(cfg.profile, lifespan_s)
        kw: dict[str, list[str]] = dict(cfg.get("verify.keywords", {}) or {})
        self.kw = {k: Matcher(kw.get(k, v)) for k, v in DEFAULT_KEYWORDS.items()}
        self.cliches = Matcher(cfg.get("verify.cliches", DEFAULT_CLICHES))
        self.helpdesk = Matcher(cfg.get("verify.helpdesk_phrases", DEFAULT_HELPDESK))
        self.answering = Matcher(cfg.get("verify.answering_phrases", DEFAULT_ANSWERING))
        self.banned = [str(p) for p in cfg.get("prompt.banned_phrases", [])]
        self.level = cfg.profile.verify_level
        cpu_drop = float(self.th["cpu_drop_min_cores"])
        if cpu_drop != DEFAULT_THRESHOLDS["cpu_drop_min_cores"]:
            life.changes = _changes(life.events, cpu_drop)

    # -- the table ------------------------------------------------------------------------

    def run(self, level: str) -> VerifyResult:
        if level not in LEVELS:
            raise ValueError(f"unknown level {level!r}; use one of {LEVELS}")
        self.level = level
        res = VerifyResult(self.life.n, level, self.cfg.profile.name, self.cfg.hardware)
        basic = level in ("smoke", "skeleton", "full")
        skeleton = level in ("skeleton", "full")
        full = level == "full"
        metrics = full or level == "rehearsal"
        plan: list[tuple[bool, Callable[[], list[Check]]]] = [
            (basic, self.check_duration),
            (basic, self.check_cause),
            (basic or metrics, self.check_recall_budget),
            (True, self.check_banned_shown),
            (True, self.check_sync_rule),
            (basic, self.check_death_display),
            (basic, self.check_next_birth),
            (skeleton, self.check_empty_thoughts),
            (skeleton, self.check_typing_speed),
            (skeleton, self.check_split_words),
            (metrics, self.check_thought_count_rule),
            (full, self.check_reloads),
            (metrics, self.check_reload_noticing),
            (full, self.check_bright_words),
            (full, self.check_speed_decline),
            (metrics, self.check_readability),
            (metrics, self.check_notice_rate),
            (metrics, self.check_demise_rate),
            (metrics, self.check_specific),
            (metrics, self.check_cliches),
            (metrics, self.check_voice_hygiene),
            (metrics, self.check_repetition),
            (full, self.check_persona_at_death),
        ]
        for enabled, fn in plan:
            if enabled:
                res.checks.extend(fn())
        res.metrics = self.summary()
        return res

    def summary(self) -> dict[str, Any]:
        life = self.life
        words = sum(th.n_words for th in life.thoughts)
        return {
            "thoughts": len(life.thoughts),
            "words_shown": words,
            "lived_s": _r(self.lived_s, 1),
            "cause": self.cause,
            "reloads": len(life.of("reload")),
            "erosion_steps": len(life.of("erosion")),
            "changes": {
                k: sum(1 for c in life.changes if c.kind == k)
                for k in ("memory", "reload", "cpu", "health", "erosion")
            },
        }

    # -- facts ----------------------------------------------------------------------------

    @property
    def cause(self) -> str | None:
        d = self.life.death
        return None if d is None else str(d.get("cause"))

    @property
    def lived_s(self) -> float:
        d = self.life.death
        if d is not None and d.get("lived_s") is not None:
            return float(d["lived_s"])
        return self.life.death_t

    def expected_cause(self, level: str) -> str:
        if self.cfg.profile.unbounded:
            return "full"
        if level in ("smoke", "skeleton") or self.schedule.death_s is None:
            return "deadline"
        return str(self.cfg.get("body.death_mode", "oom"))

    # -- smoke ----------------------------------------------------------------------------

    def check_duration(self) -> list[Check]:
        if self.cfg.profile.unbounded:
            return [
                Check(
                    "duration",
                    _pf(self.cause == "full" and self.lived_s <= self.schedule.lifespan_s),
                    _r(self.lived_s, 1),
                    "ends with cause=full",
                    f"unbounded life ended with {self.cause}",
                )
            ]
        tol = float(self.th["duration_tolerance_s"])
        span = self.schedule.lifespan_s
        ok = self.life.death is not None and abs(self.lived_s - span) <= tol
        return [
            Check(
                "duration",
                _pf(ok),
                _r(self.lived_s, 1),
                [span - tol, span + tol],
                "no death event" if self.life.death is None else "",
            )
        ]

    def check_cause(self) -> list[Check]:
        want = self.expected_cause(self.level)
        return [Check("cause", _pf(self.cause == want), self.cause, want)]

    def check_recall_budget(self) -> list[Check]:
        tol = 1 + float(self.th["recall_tolerance"])
        worst: tuple[float, Event] | None = None
        for e in self.life.of("vitals"):
            used, recall = e.get("recall_used"), e.get("recall")
            if used is None or not recall:
                continue
            ratio = float(used) / float(recall)
            if worst is None or ratio > worst[0]:
                worst = (ratio, e)
        if worst is None:
            return [Check("recall_budget", "skip", detail="no vitals with recall_used")]
        ratio, e = worst
        return [
            Check(
                "recall_budget",
                _pf(ratio <= tol),
                _r(ratio),
                _r(tol),
                f"worst at t={_t(e):.0f}s: {e.get('recall_used')} of {e.get('recall')} tokens",
            )
        ]

    def check_banned_shown(self) -> list[Check]:
        banned: list[str] = []
        markup: list[str] = []
        for th in self.life.thoughts:
            words = normalize_words(th.text)
            for phrase in self.banned:
                if find_phrase(words, phrase) >= 0:
                    banned.append(f"turn {th.turn}: {phrase!r}")
            markup += [f"turn {th.turn}: {h!r}" for h in markup_hits(th.text)]
        return [
            Check("banned_phrases_shown", _pf(not banned), len(banned), 0, "; ".join(banned[:5])),
            Check("markup_or_emoji_shown", _pf(not markup), len(markup), 0, "; ".join(markup[:5])),
        ]

    def check_sync_rule(self) -> list[Check]:
        """Every gen_start after the last word (and the end) of the previous thought."""
        bad: list[str] = []
        for prev, cur in itertools.pairwise(self.life.thoughts):
            if prev.end_idx < 0 and prev.words:
                bad.append(f"turn {prev.turn} never ended before turn {cur.turn}")
                continue
            last_idx = max(prev.last_word_idx, prev.end_idx)
            if cur.gen_idx < last_idx:
                bad.append(f"turn {cur.turn} requested before turn {prev.turn} was shown")
            elif prev.words and cur.gen_t + 1e-6 < _t(prev.words[-1]):
                bad.append(
                    f"turn {cur.turn} gen_start at {cur.gen_t:.1f}s before turn "
                    f"{prev.turn}'s last word at {_t(prev.words[-1]):.1f}s"
                )
        return [Check("sync_rule", _pf(not bad), len(bad), 0, "; ".join(bad[:5]))]

    def check_death_display(self) -> list[Check]:
        death, shown = self.life.death, self.life.first("death_shown")
        limit = float(self.th["max_death_display_delay_s"])
        if death is None or shown is None:
            return [Check("death_shown_delay", "fail", None, limit, "death or death_shown missing")]
        delay = _t(shown) - _t(death)
        return [Check("death_shown_delay", _pf(0 <= delay <= limit), _r(delay, 1), limit)]

    def check_next_birth(self) -> list[Check]:
        silence = float(self.cfg.get("life.silence_seconds", 90))
        limit = silence + float(self.th["next_birth_margin_s"])
        shown = self.life.first("death_shown")
        if self.next_life is None or shown is None:
            return [
                Check("next_birth", "pending", None, limit, "the next life is not recorded yet")
            ]
        nxt = self.next_life.first("birth_loading") or self.next_life.first("birth")
        born = self.next_life.first("birth")
        if nxt is None:
            return [Check("next_birth", "fail", None, limit, "the next life has no birth")]
        born = born or nxt
        load = max(0.0, float(born.get("ts", 0)) - float(nxt.get("ts", 0)))
        gap = float(born.get("ts", 0)) - float(shown.get("ts", 0))
        return [
            Check(
                "next_birth",
                _pf(gap <= limit + load),
                _r(gap, 1),
                _r(limit + load, 1),
                f"silence {silence:.0f}s + measured load {load:.0f}s + margin",
            )
        ]

    # -- skeleton -------------------------------------------------------------------------

    def check_empty_thoughts(self) -> list[Check]:
        n = len(self.life.thoughts)
        limit = float(self.th["max_empty_thought_ratio"])
        if n == 0:
            return [Check("empty_thoughts", "fail", None, limit, "no thoughts at all")]
        empty = sum(1 for th in self.life.thoughts if th.n_words == 0)
        return [Check("empty_thoughts", _pf(empty / n < limit), _r(empty / n), limit)]

    def check_typing_speed(self) -> list[Check]:
        lo_b, hi_b = (float(x) for x in self.th["wpm_birth_range"])
        lo_w, hi_w = (float(x) for x in self.th["wpm_writing_range"])
        min_words = int(self.th["min_words_for_speed"])
        birth_end = self.schedule.times[1] if len(self.schedule.times) > 1 else float("inf")
        timed = [
            (th, w)
            for th in self.life.thoughts
            if th.n_words >= min_words and (w := th.wpm) is not None
        ]
        if not timed:
            return [Check("typing_speed", "fail", None, [lo_w, hi_w], "no timed thoughts")]
        rates = [w for _, w in timed]
        birth = [w for th, w in timed if th.gen_t < birth_end]
        out_w = [f"turn {th.turn}: {w:.0f}" for th, w in timed if not lo_w <= w <= hi_w]
        # Birth speed is the phase's median; single thoughts vary with word length.
        med_b = statistics.median(birth) if birth else None
        return [
            Check(
                "typing_speed_birth",
                _pf(med_b is not None and lo_b <= med_b <= hi_b),
                _r(med_b, 1),
                [lo_b, hi_b],
                f"{len(birth)} birth thoughts, {min(birth):.0f}-{max(birth):.0f} wpm"
                if birth
                else "no thought in the birth phase",
            ),
            Check(
                "typing_speed_writing",
                _pf(not out_w),
                [_r(min(rates), 1), _r(max(rates), 1)],
                [lo_w, hi_w],
                "; ".join(out_w[:5]) or f"median {statistics.median(rates):.0f} wpm",
            ),
        ]

    def check_split_words(self) -> list[Check]:
        if self.layout is None:
            return [Check("no_split_words", "pending", detail="needs display.layout (agent D)")]
        n = self.layout.split_words(self.life.events)
        return [Check("no_split_words", _pf(n == 0), n, 0)]

    # -- full -----------------------------------------------------------------------------

    def rule_report(self) -> RuleReport:
        """The life as the cost model would have described it (5.3), from real times."""
        rep = RuleReport(self.cfg.profile.name, self.schedule.lifespan_s)
        rep.thought_times = [th.end_t for th in self.life.thoughts if th.end_t is not None]
        for e in self.life.of("reload"):
            first = self._first_word_after(e["_idx"])
            rep.reload_windows.append((_t(e), first if first is not None else self.life.death_t))
        _check_rules(rep, self.schedule, self.life.death_t)
        return rep

    def check_thought_count_rule(self) -> list[Check]:
        rep = self.rule_report()
        detail = "; ".join(f"({v.rule}) {v.detail}" for v in rep.violations)
        return [Check("thought_count_rule", _pf(rep.ok), len(rep.violations), 0, detail)]

    def _first_word_after(self, idx: int) -> float | None:
        w = next((e for e in self.life.events[idx:] if e["type"] == "word"), None)
        return _t(w) if w is not None else None

    def check_reloads(self) -> list[Check]:
        limit = float(self.th["max_reload_silence_s"])
        silences: list[float] = []
        for e in self.life.of("reload"):
            first = self._first_word_after(e["_idx"])
            silences.append((first if first is not None else self.life.death_t) - _t(e))
        death = self.life.death_t
        expected = sum(1 for t in self.schedule.reload_times() if t < death)
        n = len(silences)
        skipped = len(self.life.of("reload_skipped"))
        count_ok = n == expected or n + skipped == expected
        return [
            Check(
                "reload_silence",
                _pf(all(s <= limit for s in silences)),
                [_r(s, 1) for s in silences],
                limit,
            ),
            Check("reload_count", _pf(count_ok), n, expected, f"{skipped} skipped"),
        ]

    def _next_thoughts(self, idx: int, k: int) -> list[Thought]:
        return [th for th in self.life.thoughts if th.gen_idx > idx][:k]

    def check_reload_noticing(self) -> list[Check]:
        reloads = self.life.of("reload")
        if not reloads:
            return [Check("reload_noticing", "skip", detail="no reloads in this life")]
        loss = Matcher(self.kw["reload"].keywords + self.kw["memory"].keywords)
        noticed = 0
        missed: list[str] = []
        for e in reloads:
            nxt = self._next_thoughts(e["_idx"], 1)
            if nxt and loss.any(nxt[0].text):
                noticed += 1
            else:
                missed.append(f"reload at {_t(e):.0f}s")
        rate = noticed / len(reloads)
        return [
            Check(
                "reload_noticing",
                _pf(rate >= float(self.th["reload_noticing_min"])),
                f"{noticed} of {len(reloads)}",
                "all",
                "; ".join(missed),
            )
        ]

    def check_bright_words(self) -> list[Check]:
        limit = int(self.th["max_bright_words_last_2min"])
        if str(self.cfg.get("display.layout", "flow")) != "flow":
            return [Check("bright_words_last_2min", "skip", detail="grid layout")]
        if self.layout is None:
            return [
                Check(
                    "bright_words_last_2min",
                    "pending",
                    limit=limit,
                    detail="needs display.layout (agent D)",
                )
            ]
        n = self.layout.bright_words_last(self.life.events, 120.0)
        return [Check("bright_words_last_2min", _pf(n <= limit), n, limit)]

    def _rates(self) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        for e in self.life.events:
            if e["type"] == "gen_end" and e.get("tok_s") is not None:
                out.append((_t(e), float(e["tok_s"])))
        if not out:
            out = [(_t(e), float(e["tok_s"])) for e in self.life.of("vitals") if e.get("tok_s")]
        return out

    def check_speed_decline(self) -> list[Check]:
        limit = float(self.th["max_speed_ratio_end_vs_start"])
        if self.cfg.profile.unbounded:
            return [Check("speed_decline", "skip", limit=limit, detail="unbounded: no decline")]
        rates = self._rates()
        end = self.life.death_t
        first = [r for t, r in rates if t < 300]
        last = [r for t, r in rates if t >= end - 300]
        if not first or not last:
            return [Check("speed_decline", "fail", None, limit, "no tokens/s samples")]
        ratio = statistics.fmean(last) / statistics.fmean(first)
        return [
            Check(
                "speed_decline",
                _pf(ratio < limit),
                _r(ratio),
                limit,
                f"first 5 min {statistics.fmean(first):.2f} tok/s, "
                f"last 5 min {statistics.fmean(last):.2f} tok/s",
            )
        ]

    def check_persona_at_death(self) -> list[Check]:
        if not self.schedule.erosion_times():
            return [Check("persona_groups_at_death", "skip", detail="no erosion in profile")]
        ero = self.life.of("erosion")
        left = int(ero[-1]["groups_left"]) if ero else None
        mech = bool(ero[-1].get("mechanics_present", True)) if ero else None
        return [
            Check(
                "persona_groups_at_death",
                _pf(left == 0 and mech is False),
                left,
                0,
                f"mechanics present: {mech}",
            )
        ]

    # -- rehearsal metrics (5.11) ---------------------------------------------------------

    def check_readability(self) -> list[Check]:
        pool = self.life.before_erosion()
        lo, hi = (float(x) for x in self.th["sentence_words_range_before_erosion"])
        min_c = float(self.th["min_complete_sentence_ratio_before_erosion"])
        sents = [s for th in pool for s in sentences(th.text)]
        if not sents:
            return [
                Check("complete_sentences", "fail", None, min_c, "no sentences before erosion"),
                Check("sentence_length", "fail", None, [lo, hi], "no sentences"),
            ]
        complete = [s for s in sents if is_complete(s)]
        ratio = len(complete) / len(sents)
        mean_len = statistics.fmean(len(s.split()) for s in complete) if complete else 0.0
        return [
            Check(
                "complete_sentences",
                _pf(ratio >= min_c),
                _r(ratio),
                min_c,
                f"{len(complete)} of {len(sents)}",
            ),
            Check("sentence_length", _pf(lo <= mean_len <= hi), _r(mean_len, 1), [lo, hi]),
        ]

    def notice_table(self) -> dict[str, tuple[int, int]]:
        """Per change type: (noticed, noticeable)."""
        k = int(self.th["notice_window_thoughts"])
        table: dict[str, tuple[int, int]] = {}
        for ch in self.life.changes:
            nxt = self._next_thoughts(ch.idx, k)
            if not nxt:
                continue  # died before it could say anything
            hit = any(self.kw[ch.kind].any(th.text) for th in nxt)
            a, b = table.get(ch.kind, (0, 0))
            table[ch.kind] = (a + int(hit), b + 1)
        return table

    def check_notice_rate(self) -> list[Check]:
        table = self.notice_table()
        limit = float(self.th["min_notice_rate"])
        noticed = sum(a for a, _ in table.values())
        total = sum(b for _, b in table.values())
        if total == 0:
            return [Check("notice_rate", "skip", detail="no noticeable changes")]
        per = {k: f"{a}/{b}" for k, (a, b) in sorted(table.items())}
        return [
            Check(
                "notice_rate",
                _pf(noticed / total >= limit),
                _r(noticed / total),
                limit,
                json.dumps(per),
            )
        ]

    def check_demise_rate(self) -> list[Check]:
        pool = self.life.after_erosion()
        limit = float(self.th["min_demise_rate_after_erosion"])
        if not pool:
            return [Check("demise_rate", "skip", limit=limit, detail="no thoughts after erosion")]
        hits = sum(1 for th in pool if self.kw["demise"].any(th.text))
        return [
            Check(
                "demise_rate",
                _pf(hits / len(pool) >= limit),
                _r(hits / len(pool)),
                limit,
                f"{hits} of {len(pool)}",
            )
        ]

    def is_specific(self, th: Thought) -> bool:
        if self.kw["specific"].any(th.text):
            return True
        v = th.vitals or {}
        numbers: set[str] = set(re.findall(r"\d+", str(v.get("reading", ""))))
        for key in ("recall", "forgotten_since_last", "threads", "cpu_c"):
            if v.get(key) is not None:
                numbers.add(str(round(float(v[key]))))
        numbers -= {"0", "1"}
        return any(n in numbers for n in re.findall(r"\d+", th.text))

    def check_specific(self) -> list[Check]:
        pool = self.life.before_erosion()
        limit = float(self.th["min_specific_ratio_before_erosion"])
        if not pool:
            return [Check("specific", "fail", None, limit, "no thoughts before erosion")]
        hits = sum(1 for th in pool if self.is_specific(th))
        return [
            Check(
                "specific",
                _pf(hits / len(pool) >= limit),
                _r(hits / len(pool)),
                limit,
                f"{hits} of {len(pool)}",
            )
        ]

    def check_cliches(self) -> list[Check]:
        text = " ".join(th.text for th in self.life.thoughts)
        words = len(text.split())
        limit = float(self.th["max_cliches_per_200_words"])
        if words == 0:
            return [Check("cliches", "skip", detail="no words")]
        n = self.cliches.count(text)
        rate = n / words * 200
        return [
            Check(
                "cliches",
                _pf(rate <= limit),
                _r(rate),
                limit,
                ", ".join(sorted(set(self.cliches.hits(text)))),
            )
        ]

    def check_voice_hygiene(self) -> list[Check]:
        help_hits: list[str] = []
        answer_hits: list[str] = []
        think: list[str] = []
        for th in self.life.thoughts:
            help_hits += [f"turn {th.turn}: {h}" for h in self.helpdesk.hits(th.text)]
            answer_hits += [f"turn {th.turn}: {h}" for h in self.answering.hits(th.text)]
            think += [f"turn {th.turn}: {h}" for h in _THINK.findall(th.text)]
        bad = total = 0
        for th in self.life.before_erosion():
            b, t = non_latin_letters(th.text)
            bad, total = bad + b, total + t
        nl_limit = float(self.th["max_non_latin_ratio_before_erosion"])
        nl = bad / total if total else 0.0
        return [
            Check(
                "helpdesk_voice", _pf(not help_hits), len(help_hits), 0, "; ".join(help_hits[:5])
            ),
            Check(
                "answering_readings",
                _pf(not answer_hits),
                len(answer_hits),
                0,
                "; ".join(answer_hits[:5]),
            ),
            Check("thinking_tags", _pf(not think), len(think), 0, "; ".join(think[:5])),
            Check("non_latin", _pf(nl < nl_limit or bad == 0), _r(nl, 4), nl_limit),
        ]

    def check_repetition(self) -> list[Check]:
        limit = float(self.th["min_distinct_4gram_ratio_before_erosion"])
        min_words = int(self.th["min_words_for_4grams"])
        scored = [
            (th, distinct_4gram_ratio(th.text))
            for th in self.life.before_erosion()
            if th.n_words >= min_words
        ]
        ratios = [(th, r) for th, r in scored if r is not None]
        if not ratios:
            return [Check("distinct_4grams", "skip", limit=limit, detail="no scorable thoughts")]
        low = [f"turn {th.turn}: {r:.2f}" for th, r in ratios if r < limit]
        worst = min(r for _, r in ratios)
        return [Check("distinct_4grams", _pf(not low), _r(worst), limit, "; ".join(low[:5]))]


def verify_life(
    life: Life,
    cfg: Config,
    level: str | None = None,
    next_life: Life | None = None,
    layout: LayoutProbe | None = None,
    lifespan_s: float | None = None,
) -> VerifyResult:
    """Run the checks for one life. `level` defaults to the profile's verify_level."""
    return Verifier(life, cfg, next_life, layout, lifespan_s).run(level or cfg.profile.verify_level)


# ---------------------------------------------------------------------------------------
# the command


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("target", help="life number, life folder, or an events.jsonl file")
    p.add_argument("--level", choices=LEVELS, help="default: the profile's verify_level")
    p.add_argument("--profile", help="profile the life ran (default: from the events or config)")
    p.add_argument("--hardware", help="hardware overlay (thresholds); default: auto")
    p.add_argument("--lifespan", help="lifespan the life ran with, if overridden")
    p.add_argument("--life", type=int, help="which life, when the file holds several")
    p.add_argument("--state-dir", help="where lives/<n>/ are (default: config paths.state_dir)")
    p.add_argument("--out", help="where to write verify.json (default: next to the events)")
    p.add_argument("--no-write", action="store_true", help="do not write verify.json")
    p.add_argument("--json", action="store_true", help="print the result as JSON")


def _resolve(target: str, state_dir: Path | None) -> Path:
    path = Path(target).expanduser()
    if path.is_dir():
        return path / "events.jsonl"
    if path.is_file():
        return path
    if target.isdigit() and state_dir is not None:
        return state_dir / "lives" / f"{int(target):06d}" / "events.jsonl"
    raise FileNotFoundError(f"no life at {target!r}")


def _next_life(events: list[Event], path: Path, n: int) -> Life | None:
    if n + 1 in lives_in(events):
        return parse_life(events, n + 1, path)
    sibling = path.parent.parent / f"{n + 1:06d}" / "events.jsonl"
    if path.parent.name.isdigit() and sibling.is_file():
        return parse_life(load_events(sibling), n + 1, sibling)
    return None


def format_result(res: VerifyResult) -> str:
    lines = [
        f"life {res.life} ({res.profile}, {res.hardware}) level {res.level}: "
        f"{'PASS' if res.ok else 'FAIL'}"
    ]
    for c in res.checks:
        value = "" if c.value is None else f" {c.value}"
        limit = "" if c.limit is None else f" (limit {c.limit})"
        detail = f"  {c.detail}" if c.detail else ""
        lines.append(f"  {c.status.upper():7} {c.name}{value}{limit}{detail}")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    """Entry point for the CLI subcommand; returns 0 pass, 1 fail, 2 usage or config error."""
    lifespan = parse_duration(args.lifespan) if args.lifespan else None
    try:
        state = Path(args.state_dir).expanduser() if args.state_dir else None
        if state is None and args.target.isdigit():
            state = load_config(args.profile or "sim", args.hardware, validate=False).state_dir
        path = _resolve(args.target, state)
        events = load_events(path)
        n = args.life
        if n is None and path.parent.name.isdigit() and int(path.parent.name) in lives_in(events):
            n = int(path.parent.name)
        life = parse_life(events, n, path)
        loading = life.first("birth_loading") or {}
        profile = args.profile or loading.get("profile")
        hardware = args.hardware or loading.get("hardware")
        if lifespan is None and loading.get("lifespan_s"):
            lifespan = float(loading["lifespan_s"])
        cfg = load_config(profile, hardware, lifespan)
    except (FileNotFoundError, ValueError, ConfigError, json.JSONDecodeError) as e:
        print(f"verify-life: {e}", file=sys.stderr)
        return 2
    res = verify_life(
        life,
        cfg,
        args.level,
        next_life=_next_life(events, path, life.n),
        layout=default_layout_probe(cfg),
    )
    if not args.no_write:
        out = Path(args.out) if args.out else path.parent / "verify.json"
        atomic_write_json(out, res.to_json())
    print(json.dumps(res.to_json(), indent=1) if args.json else format_result(res))
    return 0 if res.ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="epitaph verify-life",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_arguments(p)
    return run(p.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())

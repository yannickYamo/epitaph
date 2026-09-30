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
             speed never rising across a reload, bright words (layout), persona groups at
             death
  rehearsal  the 5.11 metrics, the thought-count rule and speed never rising across a reload,
             for laptop rehearsal lives
  screen     the 5.11 text metrics only, for rehearsal stage 1 samples (a few thoughts at a
             few moments, not a whole life)

Rehearsal output (part A, 5.11) is read as it comes: a life folder with `events.jsonl`, or a
file holding several lives. A header event (`type` = `rehearsal`, `meta` or `header`, with or
without a `life` number) and a sidecar `meta.json` or `rehearsal.json` in the life folder are
metadata, not part of the life: model, persona, seed, stage, profile, hardware, lifespan_s.
A rehearsal life is checked at the `rehearsal` level by default (`screen` for stage 1).

`python -m epitaph.verify compare DIR...` (or `--compare`) verifies many lives and ranks them
by the 5.11 metrics in a Markdown table for checkpoint A; `--summary` prints one life's
metrics as a single JSON line for a rehearsal report.

Thresholds come from the config's [verify] section (the hardware overlay wins). The keyword,
cliche, helpdesk and answering lists come from the language pack `config/lang/<language>.toml`
(`[metrics]`, selected by `prompt.language`); see `word_lists` for the order of precedence.
The built-in lists below are only a fallback for a pack that lacks one.

Checks that need the display layout (split words, bright words) call a LayoutProbe. Until
part D's `epitaph.display.layout.verify_probe(cfg)` exists they are reported as pending,
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
import tomllib
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from epitaph.clock import Schedule
from epitaph.config import CONFIG_DIR, Config, ConfigError, load_config, parse_duration
from epitaph.costmodel import check_rules
from epitaph.state import atomic_write_json
from epitaph.types import RuleReport

Event = dict[str, Any]
Status = Literal["pass", "fail", "pending", "skip"]
LEVELS = ("smoke", "skeleton", "full", "rehearsal", "screen")
# Event types that describe a run instead of happening in a life (rehearsal headers).
META_TYPES = ("rehearsal", "meta", "header")
# Sidecar files in a life folder that describe the run, read in this order.
META_FILES = ("meta.json", "rehearsal.json")
# What `life_meta` collects from headers, sidecars, birth_loading and birth.
META_KEYS = (
    "model",
    "quant",
    "persona",
    "seed",
    "stage",
    "profile",
    "hardware",
    "lifespan_s",
    "costs",
    "run",
)

# ---------------------------------------------------------------------------------------
# defaults (documented; config wins)

DEFAULT_THRESHOLDS: dict[str, Any] = {
    "max_reload_silence_s": 180,
    "wpm_birth_range": [45, 180],
    "wpm_writing_range": [8, 220],
    "max_bright_words_last_2min": 40,
    "max_speed_ratio_end_vs_start": 0.40,
    "speed_monotonic_tolerance": 0.05,  # review 2, F2: noise allowed before a rise fails
    "speed_monotonic_thoughts": 2,  # thoughts averaged on each side of a reload
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
    "sync_tolerance_s": 0.5,  # typing replay vs gen_start (rounding of char_ms)
    "next_birth_margin_s": 300,  # 10.3: silence + load + 5 min
    "reload_noticing_min": 1.0,  # 5.11: 2 of 2
    "notice_window_thoughts": 2,  # 5.11: one of the next two thoughts
    "cpu_drop_min_cores": 0.25,  # a CPU-share change worth noticing
    "min_words_for_speed": 3,  # thoughts shorter than this are not timed
    "min_words_for_4grams": 8,  # thoughts shorter than this are not scored
}

# Fallback keyword lists per change type (5.11), used only where the language pack has none
# (see `word_lists`). A keyword ending in "*" matches as a word prefix; otherwise whole words
# or phrases. part B owns the real lists, in config/lang/<language>.toml [metrics].


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


@dataclass
class WordLists:
    """The word lists the 5.11 metrics match against, and where each one came from.

    `sources` maps each list (`keywords.memory`, ..., `cliches`, `helpdesk`, `answering`) to
    `config`, `lang:<code>` or `default`, so verify.json shows which lists judged a life.
    """

    keywords: dict[str, list[str]]
    cliches: list[str]
    helpdesk: list[str]
    answering: list[str]
    sources: dict[str, str]


# The language pack names the erosion list after what erodes: the persona.
_PACK_KEYWORD_NAMES: dict[str, tuple[str, ...]] = {"erosion": ("erosion", "persona")}


def read_lang_metrics(language: str, config_dir: Path | None = None) -> dict[str, Any] | None:
    """The `[metrics]` table of `config/lang/<language>.toml`, or None when there is no pack.

    Raises ValueError (tomllib.TOMLDecodeError) when the pack is not valid TOML.
    """
    path = (config_dir or CONFIG_DIR) / "lang" / f"{language}.toml"
    if not path.is_file():
        return None
    with path.open("rb") as f:
        raw = tomllib.load(f)
    metrics = raw.get("metrics", {})
    return cast(dict[str, Any], metrics) if isinstance(metrics, dict) else {}


def _str_list(value: Any) -> list[str] | None:
    if isinstance(value, list | tuple):
        return [str(x) for x in cast(Sequence[Any], value)]
    return None


def word_lists(cfg: Config, config_dir: Path | None = None) -> WordLists:
    """Resolve the keyword, cliche, helpdesk and answering lists for a config.

    For each list the first source that has it wins:

    1. an explicit override in the config: `[verify.keywords] <kind>`, `[verify] cliches`,
       `helpdesk_phrases` or `answering_phrases` (for tuning experiments; normally absent);
    2. the language pack `config/lang/<prompt.language>.toml`, table `[metrics]`:
       `keywords.<kind>` (the erosion list may be called `persona`), `cliches`, `helpdesk`,
       `answering`;
    3. the built-in defaults of this module.

    An empty list in the config or the pack counts as given: it matches nothing.
    """
    language = str(cfg.get("prompt.language", "en"))
    pack = read_lang_metrics(language, config_dir) or {}
    pack_kw: dict[str, Any] = cast(dict[str, Any], pack.get("keywords") or {})
    cfg_kw: dict[str, Any] = cast(dict[str, Any], cfg.get("verify.keywords") or {})
    lang_src = f"lang:{language}"
    sources: dict[str, str] = {}

    def pick(
        name: str, cfg_value: Any, pack_values: Iterable[Any], default: list[str]
    ) -> list[str]:
        found = _str_list(cfg_value)
        if found is not None:
            sources[name] = "config"
            return found
        for value in pack_values:
            found = _str_list(value)
            if found is not None:
                sources[name] = lang_src
                return found
        sources[name] = "default"
        return list(default)

    keywords = {
        kind: pick(
            f"keywords.{kind}",
            cfg_kw.get(kind),
            (pack_kw.get(alias) for alias in _PACK_KEYWORD_NAMES.get(kind, (kind,))),
            default,
        )
        for kind, default in DEFAULT_KEYWORDS.items()
    }
    return WordLists(
        keywords=keywords,
        cliches=pick("cliches", cfg.get("verify.cliches"), [pack.get("cliches")], DEFAULT_CLICHES),
        helpdesk=pick(
            "helpdesk", cfg.get("verify.helpdesk_phrases"), [pack.get("helpdesk")], DEFAULT_HELPDESK
        ),
        answering=pick(
            "answering",
            cfg.get("verify.answering_phrases"),
            [pack.get("answering")],
            DEFAULT_ANSWERING,
        ),
        sources=sources,
    )


_MARKUP = re.compile(r"(\*\*|__|`|^#{1,6}\w*$|^[-*•]$|^\d+[.)]$|</?[a-z_|]+>|<\|)", re.I)
_THINK = re.compile(r"</?think>|<\|[^>]*\|>", re.I)
_SENTENCE_END = re.compile(r"[.?!…][\"')\]]*$")


# ---------------------------------------------------------------------------------------
# the replayed life


@dataclass
class Thought:
    """One thought as shown: its words, when it was requested, typed and ended.

    Times are life-clock seconds; `shown_start` and `shown_end` come from the typing replay.
    `tok_s` is the generation speed its `gen_end` reported, None when it reported none.
    """

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
    tok_s: float | None = None

    @property
    def text(self) -> str:
        """The words as shown, space-joined; the thought_end text when no words were logged."""
        if self.words:
            return " ".join(str(w.get("text", "")) for w in self.words)
        return self.end_text

    @property
    def n_words(self) -> int:
        """Number of whitespace-separated words in `text`."""
        return len(self.text.split())

    @property
    def first_word_t(self) -> float | None:
        """Life-clock seconds when the first word was released, or None if none was."""
        return _t(self.words[0]) if self.words else None

    @property
    def last_word_idx(self) -> int:
        """Event index of the last word, or -1 for a thought without words."""
        return int(self.words[-1]["_idx"]) if self.words else -1

    @property
    def wpm(self) -> float | None:
        """Typing speed on the replayed display in words per minute; None when untimed."""
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
    headers: list[Event] = field(default_factory=lambda: [])

    def of(self, etype: str) -> list[Event]:
        """Every event of this type, in log order."""
        return [e for e in self.events if e["type"] == etype]

    def first(self, etype: str) -> Event | None:
        """The first event of this type, or None."""
        return next((e for e in self.events if e["type"] == etype), None)

    @property
    def death(self) -> Event | None:
        """The death event, or None when the log ends before one (a power cut, a live life)."""
        return self.first("death")

    @property
    def death_t(self) -> float:
        """Life-clock seconds at death; the last event's time when there is no death event."""
        d = self.death
        if d is not None:
            return _t(d)
        return _t(self.events[-1]) if self.events else 0.0

    @property
    def erosion_t(self) -> float | None:
        """Life-clock seconds of the first erosion step, or None when nothing eroded."""
        e = self.first("erosion")
        return _t(e) if e is not None else None

    def before_erosion(self) -> list[Thought]:
        """Finished thoughts requested before the first erosion step (all of them if none)."""
        cut = self.erosion_t
        return [
            th for th in self.thoughts if th.end_t is not None and (cut is None or th.gen_t < cut)
        ]

    def after_erosion(self) -> list[Thought]:
        """Thoughts requested at or after the first erosion step; empty when nothing eroded."""
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


def is_header(e: Event) -> bool:
    """Whether an event describes the run (a rehearsal header) rather than a moment of a life."""
    return e.get("type") in META_TYPES


def lives_in(events: Iterable[Event]) -> list[int]:
    """Life numbers present in an event stream, in order of first appearance.

    Header events and lines without a `type` do not make a life.
    """
    seen: list[int] = []
    for e in events:
        if not e.get("type") or is_header(e):
            continue
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
    headers = [dict(e) for e in events if is_header(e) and int(e.get("life", n)) == n]
    mine = [
        dict(e) for e in events if e.get("type") and not is_header(e) and int(e.get("life", 0)) == n
    ]
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
        elif et == "gen_end":
            th = thoughts.get(int(e.get("turn", -1)))
            if th is not None and _positive(e.get("tok_s")):
                th.tok_s = float(e["tok_s"])
    _replay_typing(order)
    return Life(n, mine, order, _changes(mine), source, headers)


def _positive(value: Any) -> bool:
    """Whether an event field holds a usable rate: a number above zero (not a bool)."""
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


def _sidecar_meta(source: Path | None) -> dict[str, Any]:
    """Metadata from `meta.json` / `rehearsal.json` next to an events file; {} if none."""
    out: dict[str, Any] = {}
    if source is None:
        return out
    for name in META_FILES:
        path = source.parent / name
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            out.update(cast(dict[str, Any], data))
    return out


def life_meta(life: Life) -> dict[str, Any]:
    """How a life was run: model, persona, seed, stage, profile, hardware, lifespan and so on.

    Sources, later ones winning: sidecar files next to the events, header events,
    `birth_loading`, then `birth` (what actually loaded). Only `META_KEYS` are kept, plus
    `rehearsal: true` when anything marks the life as a rehearsal (a `rehearsal` header, a
    `stage`, or `rehearsal` in a sidecar).
    """
    merged: dict[str, Any] = dict(_sidecar_meta(life.source))
    rehearsal = bool(merged.get("rehearsal"))
    for h in life.headers:
        merged.update(h)
        rehearsal = rehearsal or h.get("type") == "rehearsal" or bool(h.get("rehearsal"))
    for etype in ("birth_loading", "birth"):
        e = life.first(etype)
        if e is not None:
            merged.update({k: v for k, v in e.items() if k in META_KEYS and v is not None})
    meta = {k: merged[k] for k in META_KEYS if merged.get(k) is not None}
    if rehearsal or "stage" in meta:
        meta["rehearsal"] = True
    return meta


def default_level(life: Life, cfg: Config) -> str:
    """The level a life is checked at when none is asked for.

    `screen` for a rehearsal stage 1 sample, `rehearsal` for any other rehearsal life, else
    the profile's `verify_level`.
    """
    meta = life_meta(life)
    if str(meta.get("stage", "")).lower() in ("1", "screen"):
        return "screen"
    if meta.get("rehearsal"):
        return "rehearsal"
    return cfg.profile.verify_level


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
    """Keyword and phrase matching with "*" prefix wildcards.

    Case-insensitive; curly apostrophes match straight ones. A keyword without "*" matches only
    whole words or phrases, and spaces in a phrase match any run of whitespace.
    """

    def __init__(self, keywords: Iterable[str]) -> None:
        """Compile every non-blank keyword once."""
        self.keywords = [k for k in keywords if k.strip()]
        self._pats = [(k, _kw_pattern(k)) for k in self.keywords]

    def hits(self, text: str) -> list[str]:
        """The keywords found in text, each listed once."""
        low = text.lower().replace("’", "'")
        return [k for k, p in self._pats if p.search(low)]

    def count(self, text: str) -> int:
        """Total matches of all keywords in text; a keyword found twice counts twice."""
        low = text.lower().replace("’", "'")
        return sum(len(p.findall(low)) for _, p in self._pats)

    def any(self, text: str) -> bool:
        """Whether any keyword occurs in text."""
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
    """Split text after `.`, `?`, `!` or `…` (and any closing quote or bracket) plus whitespace."""
    parts = re.split(r"(?<=[.?!…])\s+|(?<=[.?!…][\"')\]])\s+", text.strip())
    return [p for p in (s.strip() for s in parts) if p]


def is_complete(sentence: str) -> bool:
    """Whether a sentence has at least two words and ends with terminal punctuation."""
    return bool(_SENTENCE_END.search(sentence)) and len(sentence.split()) >= 2


def distinct_4gram_ratio(text: str) -> float | None:
    """Distinct word 4-grams over all 4-grams (1.0 = no repeats); None under four words."""
    w = normalize_words(text)
    grams = [tuple(w[i : i + 4]) for i in range(len(w) - 3)]
    return len(set(grams)) / len(grams) if grams else None


def is_emoji(ch: str) -> bool:
    """Whether a character is an emoji, a variation selector or a zero-width joiner."""
    cp = ord(ch)
    return (
        0x1F000 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or 0x1F900 <= cp <= 0x1F9FF
        or cp in (0xFE0F, 0x200D)
    )


def non_latin_letters(text: str) -> tuple[int, int]:
    """Count letters as (outside the Latin script, all letters)."""
    bad = total = 0
    for ch in text:
        if ch.isalpha():
            total += 1
            if not unicodedata.name(ch, "").startswith("LATIN"):
                bad += 1
    return bad, total


def markup_hits(text: str) -> list[str]:
    """Markdown, template tokens, thinking tags and emoji in text; none may reach the screen."""
    hits = [w for w in text.split() if _MARKUP.search(w)]
    hits += _THINK.findall(text)
    hits += [ch for ch in text if is_emoji(ch)]
    return hits


# ---------------------------------------------------------------------------------------
# layout hook (part D)


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
    """One row of the verify table: a named check, its status, measured value and limit.

    `pending` means the check cannot run yet (a missing layout probe, an unrecorded next
    life); like `skip`, it never fails a life.
    """

    name: str
    status: Status
    value: Any = None
    limit: Any = None
    detail: str = ""


@dataclass
class VerifyResult:
    """Every check run on one life at one level, plus headline metrics."""

    life: int
    level: str
    profile: str
    hardware: str
    checks: list[Check] = field(default_factory=lambda: [])
    metrics: dict[str, Any] = field(default_factory=lambda: {})
    meta: dict[str, Any] = field(default_factory=lambda: {})
    source: str = ""

    @property
    def ok(self) -> bool:
        """True unless a check failed; pending and skipped checks never fail a life."""
        return not any(c.status == "fail" for c in self.checks)

    def by_name(self, name: str) -> Check:
        """The check with this name; raises StopIteration if it did not run."""
        return next(c for c in self.checks if c.name == name)

    def to_json(self) -> dict[str, Any]:
        """The result in the shape written to verify.json."""
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
            "meta": self.meta,
            "summary": summarize(self),
        }


# The 5.11 metrics as check names, in the order of the 5.11 table; the rehearsal summary and
# the compare table report these.
SUMMARY_CHECKS = (
    "notice_rate",
    "reload_noticing",
    "demise_rate",
    "specific",
    "cliches",
    "complete_sentences",
    "sentence_length",
    "helpdesk_voice",
    "answering_readings",
    "thinking_tags",
    "non_latin",
    "banned_phrases_shown",
    "markup_or_emoji_shown",
    "distinct_4grams",
    "thought_count_rule",
    "speed_monotonic",
)


def summarize(res: VerifyResult) -> dict[str, Any]:
    """One life's 5.11 results as a flat record, for a rehearsal report or a compare table.

    `metrics` maps each of `SUMMARY_CHECKS` that ran to its measured value and `status` to its
    status; checks that did not run at this level are left out.
    """
    ran = {c.name: c for c in res.checks if c.name in SUMMARY_CHECKS}
    meta = res.meta
    return {
        "life": res.life,
        "source": res.source,
        "model": meta.get("model"),
        "quant": meta.get("quant"),
        "persona": meta.get("persona"),
        "seed": meta.get("seed"),
        "stage": meta.get("stage"),
        "costs": meta.get("costs"),
        "profile": res.profile,
        "hardware": res.hardware,
        "level": res.level,
        "ok": res.ok,
        "failed": [c.name for c in res.checks if c.status == "fail"],
        "thoughts": res.metrics.get("thoughts"),
        "words_shown": res.metrics.get("words_shown"),
        "metrics": {name: ran[name].value for name in SUMMARY_CHECKS if name in ran},
        "status": {name: ran[name].status for name in SUMMARY_CHECKS if name in ran},
    }


def _pf(ok: bool) -> Status:
    return "pass" if ok else "fail"


def _r(x: float | None, nd: int = 3) -> float | None:
    return None if x is None else round(x, nd)


class Verifier:
    """Runs the 10.3 checks and the 5.11 metrics for one life against `[verify]` thresholds."""

    def __init__(
        self,
        life: Life,
        cfg: Config,
        next_life: Life | None = None,
        layout: LayoutProbe | None = None,
        lifespan_s: float | None = None,
    ) -> None:
        """Prepare thresholds, keyword matchers and the schedule the life ran on.

        `next_life` feeds the next-birth check and `layout` the layout checks (both pending
        without it); `lifespan_s` overrides the profile's lifespan. A non-default
        `cpu_drop_min_cores` rebuilds `life.changes` in place.
        """
        self.life = life
        self.cfg = cfg
        self.next_life = next_life
        self.layout = layout
        self.th: dict[str, Any] = {**DEFAULT_THRESHOLDS, **cfg.section("verify")}
        self.schedule = Schedule(cfg.profile, lifespan_s)
        self.lists = word_lists(cfg)
        self.kw = {k: Matcher(words) for k, words in self.lists.keywords.items()}
        self.cliches = Matcher(self.lists.cliches)
        self.helpdesk = Matcher(self.lists.helpdesk)
        self.answering = Matcher(self.lists.answering)
        self.banned = [str(p) for p in cfg.get("prompt.banned_phrases", [])]
        self.level = cfg.profile.verify_level
        cpu_drop = float(self.th["cpu_drop_min_cores"])
        if cpu_drop != DEFAULT_THRESHOLDS["cpu_drop_min_cores"]:
            life.changes = _changes(life.events, cpu_drop)

    # -- the table ------------------------------------------------------------------------

    def run(self, level: str) -> VerifyResult:
        """Run the checks enabled at `level`; raises ValueError for an unknown level."""
        if level not in LEVELS:
            raise ValueError(f"unknown level {level!r}; use one of {LEVELS}")
        self.level = level
        res = VerifyResult(
            self.life.n,
            level,
            self.cfg.profile.name,
            self.cfg.hardware,
            meta=life_meta(self.life),
            source=str(self.life.source or ""),
        )
        basic = level in ("smoke", "skeleton", "full")
        skeleton = level in ("skeleton", "full")
        full = level == "full"
        lived = full or level == "rehearsal"  # a whole life: its timing can be judged
        metrics = lived or level == "screen"
        plan: list[tuple[bool, Callable[[], list[Check]]]] = [
            (basic, self.check_duration),
            (basic, self.check_cause),
            (basic or lived, self.check_recall_budget),
            (True, self.check_banned_shown),
            (level != "screen", self.check_sync_rule),
            (basic, self.check_death_display),
            (basic, self.check_next_birth),
            (skeleton, self.check_empty_thoughts),
            (skeleton, self.check_typing_speed),
            (skeleton, self.check_split_words),
            (lived, self.check_thought_count_rule),
            (full, self.check_reloads),
            (metrics, self.check_reload_noticing),
            (full, self.check_bright_words),
            (full, self.check_speed_decline),
            (lived, self.check_speed_monotonic),
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
        """Headline counts for verify.json: thoughts, words shown, seconds lived, cause, changes."""
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
            "notice_per_type": {k: list(v) for k, v in sorted(self.notice_table().items())},
            "word_lists": self.lists.sources,
        }

    # -- facts ----------------------------------------------------------------------------

    @property
    def cause(self) -> str | None:
        """The recorded cause of death, or None without a death event."""
        d = self.life.death
        return None if d is None else str(d.get("cause"))

    @property
    def lived_s(self) -> float:
        """Seconds lived: the death event's `lived_s` when present, else the death time."""
        d = self.life.death
        if d is not None and d.get("lived_s") is not None:
            return float(d["lived_s"])
        return self.life.death_t

    def expected_cause(self, level: str) -> str:
        """The cause of death the plan expects for this profile at this level.

        `full` for an unbounded profile; `deadline` for smoke and skeleton runs and for profiles
        without a death time; otherwise the configured `body.death_mode` (default `oom`).
        """
        if self.cfg.profile.unbounded:
            return "full"
        if level in ("smoke", "skeleton") or self.schedule.death_s is None:
            return "deadline"
        return str(self.cfg.get("body.death_mode", "oom"))

    # -- smoke ----------------------------------------------------------------------------

    def check_duration(self) -> list[Check]:
        """The life lasted its lifespan within `duration_tolerance_s` (10.3).

        An unbounded life must instead end with cause `full` inside the lifespan.
        """
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
        """The recorded cause of death is the one `expected_cause` gives for this level."""
        want = self.expected_cause(self.level)
        return [Check("cause", _pf(self.cause == want), self.cause, want)]

    def check_recall_budget(self) -> list[Check]:
        """Memory in use never exceeded the recall budget by more than `recall_tolerance` (10.3).

        Judged at the worst vitals sample; skipped when no sample reports `recall_used`.
        """
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
        """No banned prompt phrase, markup, thinking tag or emoji was shown."""
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
        """Every gen_start after the previous thought's last word was shown (5.7 step 7):
        after its word and thought_end events, and after its last letter was typed on the
        replayed display timeline (within `sync_tolerance_s`)."""
        tol = float(self.th["sync_tolerance_s"])
        bad: list[str] = []
        for prev, cur in itertools.pairwise(self.life.thoughts):
            if prev.end_idx < 0 and prev.words:
                bad.append(f"turn {prev.turn} never ended before turn {cur.turn}")
                continue
            last_idx = max(prev.last_word_idx, prev.end_idx)
            if cur.gen_idx < last_idx:
                bad.append(f"turn {cur.turn} requested before turn {prev.turn} was shown")
            elif prev.shown_end is not None and cur.gen_t + tol < prev.shown_end:
                bad.append(
                    f"turn {cur.turn} gen_start at {cur.gen_t:.1f}s before turn "
                    f"{prev.turn}'s last word was typed at {prev.shown_end:.1f}s"
                )
        return [Check("sync_rule", _pf(not bad), len(bad), 0, "; ".join(bad[:5]))]

    def check_death_display(self) -> list[Check]:
        """The death screen appeared within `max_death_display_delay_s` of the death."""
        death, shown = self.life.death, self.life.first("death_shown")
        limit = float(self.th["max_death_display_delay_s"])
        if death is None or shown is None:
            return [Check("death_shown_delay", "fail", None, limit, "death or death_shown missing")]
        delay = _t(shown) - _t(death)
        return [Check("death_shown_delay", _pf(0 <= delay <= limit), _r(delay, 1), limit)]

    def check_next_birth(self) -> list[Check]:
        """The next life was born within silence + load + margin of the death screen (10.3).

        The load is measured from the next life's `birth_loading` to its `birth`. Pending until
        the next life is recorded.
        """
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
        """Fewer than `max_empty_thought_ratio` of the thoughts showed no words."""
        n = len(self.life.thoughts)
        limit = float(self.th["max_empty_thought_ratio"])
        if n == 0:
            return [Check("empty_thoughts", "fail", None, limit, "no thoughts at all")]
        empty = sum(1 for th in self.life.thoughts if th.n_words == 0)
        return [Check("empty_thoughts", _pf(empty / n < limit), _r(empty / n), limit)]

    def check_typing_speed(self) -> list[Check]:
        """Words per minute on the replayed display stay in range.

        The birth phase's median must fall in `wpm_birth_range` and every timed thought (at least
        `min_words_for_speed` words) in `wpm_writing_range`.
        """
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
        """No word is broken across lines by the layout; pending without a layout probe."""
        if self.layout is None:
            return [Check("no_split_words", "pending", detail="needs display.layout (part D)")]
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
        check_rules(rep, self.schedule, self.life.death_t)
        return rep

    def check_thought_count_rule(self) -> list[Check]:
        """The real thought and reload times satisfy the cost model's rules (5.3)."""
        rep = self.rule_report()
        detail = "; ".join(f"({v.rule}) {v.detail}" for v in rep.violations)
        return [Check("thought_count_rule", _pf(rep.ok), len(rep.violations), 0, detail)]

    def _first_word_after(self, idx: int) -> float | None:
        w = next((e for e in self.life.events[idx:] if e["type"] == "word"), None)
        return _t(w) if w is not None else None

    def check_reloads(self) -> list[Check]:
        """Reload silences and count match the plan.

        Each reload may leave the screen silent (reload to next word) for at most
        `max_reload_silence_s`, and the reloads done, or done plus skipped, must equal the
        schedule's reloads before death.
        """
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
        """The first thought after every reload speaks of the loss (5.11); skip without reloads."""
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
                _r(rate),
                float(self.th["reload_noticing_min"]),
                "; ".join([f"{noticed} of {len(reloads)}", *missed]),
            )
        ]

    def check_bright_words(self) -> list[Check]:
        """At most `max_bright_words_last_2min` words at full brightness at once near the end.

        Only the flow layout is checked (the last 120 s of the life); pending without a probe.
        """
        limit = int(self.th["max_bright_words_last_2min"])
        if str(self.cfg.get("display.layout", "flow")) != "flow":
            return [Check("bright_words_last_2min", "skip", detail="grid layout")]
        if self.layout is None:
            return [
                Check(
                    "bright_words_last_2min",
                    "pending",
                    limit=limit,
                    detail="needs display.layout (part D)",
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
        """Tokens/s in the last 5 minutes is under `max_speed_ratio_end_vs_start` of the first 5.

        Rates come from `gen_end` events, else from vitals; skipped for unbounded profiles.
        """
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

    def thought_speeds(self) -> list[float | None]:
        """Generation speed of each thought in `life.thoughts` order (None when unknown).

        `gen_end.tok_s` when the life records any; otherwise `vitals.tok_s`. A vitals event
        reports the last measured speed (the reading's "speed", 5.4), which is the previous
        thought's: thought i takes it from the vitals written before thought i+1, if that
        vitals came after thought i started.
        """
        ths = self.life.thoughts
        if any(th.tok_s is not None for th in ths):
            return [th.tok_s for th in ths]
        out: list[float | None] = []
        for th, nxt in itertools.zip_longest(ths, ths[1:]):
            vit = nxt.vitals if nxt is not None else None
            ok = vit is not None and int(vit["_idx"]) > th.gen_idx and _positive(vit.get("tok_s"))
            out.append(float(vit["tok_s"]) if ok and vit is not None else None)
        return out

    def check_speed_monotonic(self) -> list[Check]:
        """Generation never speeds up across a reload (review 2, F2).

        For each reload, the mean speed of up to `speed_monotonic_thoughts` thoughts just
        after it (before the next reload) may exceed the mean of as many thoughts just before
        it (after the previous reload) by at most `speed_monotonic_tolerance`. The value is
        the highest after/before ratio. Skipped without reloads; a reload with no timed
        thought on one side (death during the silence) is listed and not judged.
        """
        tol = float(self.th["speed_monotonic_tolerance"])
        k = max(1, int(self.th["speed_monotonic_thoughts"]))
        limit = 1.0 + tol
        reloads = self.life.of("reload")
        if not reloads:
            return [Check("speed_monotonic", "skip", limit=limit, detail="no reloads")]
        speeds = self.thought_speeds()
        bounds = [-1, *(int(e["_idx"]) for e in reloads), len(self.life.events)]
        timed = [
            (th.gen_idx, s)
            for th, s in zip(self.life.thoughts, speeds, strict=True)
            if s is not None
        ]
        ratios: list[float] = []
        notes: list[str] = []
        for i, e in enumerate(reloads):
            lo, at, hi = bounds[i], bounds[i + 1], bounds[i + 2]
            before = [s for idx, s in timed if lo < idx < at][-k:]
            after = [s for idx, s in timed if at < idx < hi][:k]
            where = f"reload at {_t(e) / 60:.1f} min"
            if not before or not after:
                notes.append(f"{where}: not compared (no timed thought on one side)")
                continue
            b, a = statistics.fmean(before), statistics.fmean(after)
            ratios.append(a / b)
            notes.append(f"{where}: {b:.2f} -> {a:.2f} tok/s ({a / b - 1:+.0%})")
        if not ratios:
            if not timed:
                return [Check("speed_monotonic", "fail", None, limit, "no tokens/s samples")]
            return [Check("speed_monotonic", "skip", limit=limit, detail="; ".join(notes))]
        worst = max(ratios)
        return [Check("speed_monotonic", _pf(worst <= limit), _r(worst), limit, "; ".join(notes))]

    def check_persona_at_death(self) -> list[Check]:
        """Every persona group and the mechanics text were gone by the last erosion step.

        Skipped when the profile never erodes.
        """
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
        """Before erosion, sentences are complete and of readable length (5.11).

        At least `min_complete_sentence_ratio_before_erosion` of sentences end properly, and
        the mean complete sentence has a word count in `sentence_words_range_before_erosion`.
        """
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
        """The share of changes mentioned in the next `notice_window_thoughts` thoughts (5.11).

        Changes the creature died before answering are not counted.
        """
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
        """The share of thoughts after erosion that speak of ending (5.11)."""
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
        """Whether a thought names its situation.

        It does when it uses a `specific` keyword or repeats a number (other than 0 and 1) from
        the reading it answered.
        """
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
        """The share of thoughts before erosion that are specific (5.11, see `is_specific`)."""
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
        """Cliches per 200 shown words stay at or under `max_cliches_per_200_words` (5.11)."""
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
        """The voice stays its own (5.11).

        No helpdesk phrases, no answering the readings, no thinking tags, and before erosion
        fewer than `max_non_latin_ratio_before_erosion` of letters outside the Latin script.
        """
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
        """Before erosion, no thought of `min_words_for_4grams` or more words repeats itself.

        Each one's distinct 4-gram ratio must reach `min_distinct_4gram_ratio_before_erosion`.
        """
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
    """Run the checks for one life. `level` defaults to `default_level(life, cfg)`."""
    return Verifier(life, cfg, next_life, layout, lifespan_s).run(level or default_level(life, cfg))


# ---------------------------------------------------------------------------------------
# compare: rank rehearsal lives for checkpoint A


def find_event_files(paths: Iterable[Path]) -> list[Path]:
    """The events files under each path: the file itself, `<dir>/events.jsonl`, or every
    `events.jsonl` below a directory (a rehearsal run with one folder per life).

    Raises FileNotFoundError for a path that does not exist or holds no events file.
    """
    out: list[Path] = []
    for path in paths:
        if path.is_file():
            found = [path]
        elif (path / "events.jsonl").is_file():
            found = [path / "events.jsonl"]
        elif path.is_dir():
            found = sorted(path.rglob("events.jsonl"))
        else:
            found = []
        if not found:
            raise FileNotFoundError(f"no events.jsonl at {str(path)!r}")
        out += [f for f in found if f not in out]
    return out


# Ranking order: fewer failed checks first, then these metrics (higher is better unless
# marked False), then the label. Keyword metrics catch failures; they do not prove quality.
_RANK_METRICS: tuple[tuple[str, bool], ...] = (
    ("notice_rate", True),
    ("reload_noticing", True),
    ("demise_rate", True),
    ("specific", True),
    ("cliches", False),
    ("distinct_4grams", True),
)


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def rank_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Sort key for a compare row (a `summarize` record plus `label`); smaller ranks higher."""
    key: list[Any] = [len(row["failed"])]
    for name, higher in _RANK_METRICS:
        x = _num(row["metrics"].get(name))
        if x is None:
            key.append(float("inf"))  # not measured ranks after any measured value
        else:
            key.append(-x if higher else x)
    key.append(str(row.get("label", "")))
    return tuple(key)


def model_verdicts(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per model: lives checked at the `rehearsal` level and how many met every threshold.

    A model meets every threshold (gate G0) when it has at least one full rehearsal life and
    every one of them passes. Screen samples are ranked but do not count here.
    """
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["level"] != "rehearsal":
            continue
        model = str(row.get("model") or "?")
        m = out.setdefault(model, {"lives": 0, "passed": 0, "personas": []})
        m["lives"] += 1
        m["passed"] += int(bool(row["ok"]))
        persona = row.get("persona")
        if persona and persona not in m["personas"]:
            m["personas"].append(persona)
    for m in out.values():
        m["meets_all"] = m["lives"] > 0 and m["passed"] == m["lives"]
    return out


def _cell(row: dict[str, Any], name: str) -> str:
    value = row["metrics"].get(name)
    if value is None:
        return "-"
    if isinstance(value, float):
        text = f"{value:.2f}"
    elif isinstance(value, list):
        text = "-".join(str(x) for x in cast(list[Any], value))
    else:
        text = str(value)
    return f"**{text}**" if row["status"].get(name) == "fail" else text


def _md(text: Any) -> str:
    return str(text if text is not None else "-").replace("|", "\\|").replace("\n", " ")


_TABLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Notice", "notice_rate"),
    ("Reload", "reload_noticing"),
    ("Demise", "demise_rate"),
    ("Specific", "specific"),
    ("Clichés/200w", "cliches"),
    ("Complete", "complete_sentences"),
    ("Words/sentence", "sentence_length"),
    ("4-gram min", "distinct_4grams"),
    ("Helpdesk", "helpdesk_voice"),
    ("Answering", "answering_readings"),
    ("Rule", "thought_count_rule"),
    ("Speed after/before reload", "speed_monotonic"),
)


def format_compare(rows: Sequence[dict[str, Any]], require_models: int = 2) -> str:
    """Rehearsal lives ranked by the 5.11 metrics, as Markdown for checkpoint A.

    `rows` are `summarize` records with a `label`; they are ranked by `rank_key`. Values in
    bold failed their threshold; `-` means not measured (no reloads, no erosion, a screen
    sample). A per-model table and the gate G0 verdict (`require_models` models meeting every
    threshold) follow.
    """
    ranked = sorted(rows, key=rank_key)
    head = ["#", "Life", "Model", "Persona", "Seed", "Level", "Result", "Thoughts"]
    head += [title for title, _ in _TABLE_COLUMNS]
    lines = [
        "## Rehearsal lives, ranked",
        "",
        "Ranked by failed checks, then notice, reload noticing, demise, specific, clichés and "
        "4-gram variety (BUILD_PLAN 5.11). **Bold** values miss their threshold; `-` was not "
        "measured. Keyword matching catches failures; it does not prove quality: read the "
        "transcripts.",
        "",
        "| " + " | ".join(head) + " |",
        "|" + "---|" * len(head),
    ]
    for i, row in enumerate(ranked, 1):
        result = "pass" if row["ok"] else "fail: " + ", ".join(row["failed"])
        cells = [
            str(i),
            _md(row.get("label")),
            _md(row.get("model")),
            _md(row.get("persona")),
            _md(row.get("seed")),
            _md(row["level"]),
            _md(result),
            _md(row.get("thoughts")),
        ]
        cells += [_cell(row, name) for _, name in _TABLE_COLUMNS]
        lines.append("| " + " | ".join(cells) + " |")
    verdicts = model_verdicts(ranked)
    lines += ["", "## By model (full rehearsal lives)", ""]
    if verdicts:
        lines += [
            "| Model | Personas | Lives | Passed | Meets every threshold |",
            "|---|---|---|---|---|",
        ]
        for model, m in verdicts.items():
            lines.append(
                f"| {_md(model)} | {_md(', '.join(m['personas']) or None)} | {m['lives']} "
                f"| {m['passed']} | {'yes' if m['meets_all'] else 'no'} |"
            )
    else:
        lines.append("No life was checked at the `rehearsal` level.")
    good = [model for model, m in verdicts.items() if m["meets_all"]]
    verdict = "met" if len(good) >= require_models else "not met"
    lines += [
        "",
        f"Gate G0, at least {require_models} models meet every threshold: **{verdict}**"
        f" ({len(good)}: {', '.join(good) or 'none'}).",
    ]
    return "\n".join(lines) + "\n"


def compare(
    paths: Sequence[Path],
    level: str | None = None,
    profile: str | None = None,
    hardware: str | None = None,
    lifespan_s: float | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Verify every life under `paths`; returns (summary rows with a `label`, errors).

    Each life is judged by its own recorded profile and hardware unless given here, at
    `level` or its `default_level`. A life that cannot be read or configured becomes an
    error line, not an exception, so one broken folder does not hide the others.
    """
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in find_event_files(paths):
        try:
            events = load_events(path)
            ns = lives_in(events)
            if not ns:
                raise ValueError("no events")
            for n in ns:
                life = parse_life(events, n, path)
                cfg = _config_for(life, profile, hardware, lifespan_s)
                res = verify_life(
                    life, cfg, level or default_level(life, cfg), layout=default_layout_probe(cfg)
                )
                row = summarize(res)
                base = path.parent if path.name == "events.jsonl" else path
                name = _label(base, paths)
                row["label"] = f"{name}#{n}" if len(ns) > 1 else name
                rows.append(row)
        except (ValueError, ConfigError, OSError) as e:
            errors.append(f"{path}: {e}")
    return rows, errors


def _label(base: Path, roots: Sequence[Path]) -> str:
    """A short name for a life: its path below the folder it was found in, else its name."""
    here = base.resolve()
    for root in roots:
        top = root.resolve()
        if here != top and here.is_relative_to(top):
            return str(here.relative_to(top))
    return here.name


def _config_for(
    life: Life, profile: str | None, hardware: str | None, lifespan_s: float | None
) -> Config:
    """The config a life is judged by: the given values, else those the life recorded."""
    meta = life_meta(life)
    profile = profile or meta.get("profile")
    hardware = hardware or meta.get("hardware")
    if lifespan_s is None and meta.get("lifespan_s"):
        lifespan_s = float(meta["lifespan_s"])
    return load_config(profile, hardware, lifespan_s)


# ---------------------------------------------------------------------------------------
# the command


def add_arguments(p: argparse.ArgumentParser) -> None:
    """Add the verify-life arguments to a parser (used by `epitaph verify-life` and `main`)."""
    p.add_argument(
        "target",
        nargs="+",
        help="life number, life folder or events.jsonl file; several with --compare",
    )
    p.add_argument(
        "--level",
        choices=LEVELS,
        help="default: rehearsal (screen for stage 1) for rehearsal lives, else the profile's",
    )
    p.add_argument("--profile", help="profile the life ran (default: from the events or config)")
    p.add_argument("--hardware", help="hardware overlay (thresholds); default: auto")
    p.add_argument("--lifespan", help="lifespan the life ran with, if overridden")
    p.add_argument("--life", type=int, help="which life, when the file holds several")
    p.add_argument("--state-dir", help="where lives/<n>/ are (default: config paths.state_dir)")
    p.add_argument(
        "--out",
        help="where to write verify.json (default: next to the events); with --compare, "
        "the Markdown table",
    )
    p.add_argument("--no-write", action="store_true", help="do not write verify.json")
    out = p.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="print the result as JSON")
    out.add_argument(
        "--summary",
        action="store_true",
        help="print the 5.11 metrics as one JSON line, for a rehearsal report",
    )
    p.add_argument(
        "--compare",
        action="store_true",
        help="verify every life under the targets and rank them in a Markdown table",
    )
    p.add_argument(
        "--require-models",
        type=int,
        default=2,
        metavar="N",
        help="with --compare: exit 1 unless N models meet every threshold (gate G0; default 2)",
    )


def _resolve(target: str, state_dir: Path | None) -> Path:
    path = Path(target).expanduser()
    if path.is_file():
        return path
    if path.is_dir():
        found = find_event_files([path])
        if len(found) > 1:
            raise ValueError(
                f"{len(found)} events files under {target!r}: name one, or use --compare"
            )
        return found[0]
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
    """The result as a plain-text report, one check per line."""
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
    try:
        lifespan = parse_duration(args.lifespan) if args.lifespan else None
    except ValueError as e:
        print(f"verify-life: {e}", file=sys.stderr)
        return 2
    if args.target[0] == "compare" and not Path("compare").exists():
        # `verify-life compare DIR...`: the subcommand form of --compare.
        args.compare, args.target = True, args.target[1:]
        if not args.target:
            print("verify-life: compare needs at least one folder or file", file=sys.stderr)
            return 2
    if args.compare:
        return run_compare(args, lifespan)
    if len(args.target) != 1:
        print("verify-life: one target at a time, or --compare for several", file=sys.stderr)
        return 2
    target = str(args.target[0])
    try:
        state = Path(args.state_dir).expanduser() if args.state_dir else None
        if state is None and target.isdigit():
            state = load_config(args.profile or "sim", args.hardware, validate=False).state_dir
        path = _resolve(target, state)
        events = load_events(path)
        n = args.life
        if n is None and path.parent.name.isdigit() and int(path.parent.name) in lives_in(events):
            n = int(path.parent.name)
        life = parse_life(events, n, path)
        cfg = _config_for(life, args.profile, args.hardware, lifespan)
    except (OSError, ValueError, ConfigError) as e:
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
    if args.summary:
        print(json.dumps(summarize(res)))
    elif args.json:
        print(json.dumps(res.to_json(), indent=1))
    else:
        print(format_result(res))
    return 0 if res.ok else 1


def run_compare(args: argparse.Namespace, lifespan_s: float | None = None) -> int:
    """`--compare`: rank every life under the targets (see `format_compare`).

    Prints the Markdown (or the rows as JSON with `--json`), writes it to `--out` when given,
    and returns 0 when at least `--require-models` models meet every threshold, 1 when fewer
    do, 2 when a target cannot be read or holds no life.
    """
    try:
        rows, errors = compare(
            [Path(t).expanduser() for t in args.target],
            args.level,
            args.profile,
            args.hardware,
            lifespan_s,
        )
    except OSError as e:
        print(f"verify-life: {e}", file=sys.stderr)
        return 2
    for err in errors:
        print(f"verify-life: {err}", file=sys.stderr)
    if not rows:
        print("verify-life: no lives to compare", file=sys.stderr)
        return 2
    text = format_compare(rows, args.require_models)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    if args.json:
        print(json.dumps(sorted(rows, key=rank_key), indent=1))
    else:
        print(text, end="")
    if errors:
        return 2
    good = sum(1 for m in model_verdicts(rows).values() if m["meets_all"])
    return 0 if good >= args.require_models else 1


def main(argv: list[str] | None = None) -> int:
    """Run verify-life as a standalone program; returns the exit code of `run`.

    `main(["compare", DIR, ...])` is short for `main([DIR, ..., "--compare"])`.
    """
    p = argparse.ArgumentParser(
        prog="epitaph verify-life",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_arguments(p)
    return run(p.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())

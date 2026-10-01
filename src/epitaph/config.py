"""Configuration: base file, hardware overlay, profile, models (BUILD_PLAN 3.2, 5.3, 6.2).

Load order: config/default.toml, then config/hardware/<overlay>.toml, then the profile's
own top-level settings (for example ctx), then command-line overrides.
Validation fails fast with a message that names the file and the problem.
"""

from __future__ import annotations

import copy
import itertools
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from epitaph.types import ModelSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.environ.get("EPITAPH_CONFIG_DIR", REPO_ROOT / "config"))

STEPPED = ("phase", "health", "step", "threads", "persona_groups", "mechanics", "readings")
INTERPOLATED = (
    "recall",
    "cpu_share",
    "temperature",
    "min_p",
    "max_tokens",
    "pause_s",
    "letter_ms",
    "jitter",
    "hesitation",
)
# Optional stepped knobs: a profile may omit them; the first keyframe gets the default.
OPTIONAL_DEFAULTS: dict[str, float] = {"cpu_mhz": 1800.0}
CPU_MHZ_RANGE = (600.0, 1800.0)  # the Pi 4's cpufreq range (spike S7)
# Thought-count minimums (BUILD_PLAN 5.3), set for a one-hour life; a profile's [rules] table
# may lower them (ADR-024).
RULE_DEFAULTS: dict[str, int] = {
    "between_health": 3,
    "after_reload": 2,
    "per_erosion_step": 1,
    "after_erosion_start": 4,
}


def profile_rules(settings: Mapping[str, Any]) -> dict[str, int]:
    """The thought-count minimums of a profile: its [rules] table over RULE_DEFAULTS."""
    raw: object = settings.get("rules", {})
    if not isinstance(raw, dict):
        raise ConfigError("profile [rules] must be a table")
    table = cast("dict[str, object]", raw)
    unknown = sorted(set(table) - set(RULE_DEFAULTS))
    if unknown:
        raise ConfigError(f"unknown profile rules: {unknown}")
    out = dict(RULE_DEFAULTS)
    for k, v in table.items():
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise ConfigError(f"profile rule {k} must be a whole number of at least 1, not {v!r}")
        out[k] = v
    return out


KNOB_FIELDS = STEPPED + INTERPOLATED + tuple(OPTIONAL_DEFAULTS)
READINGS_FORMS = ("full", "short", "minimal")
# How the screen is paced ([reveal] mode): "letter" types each thought once it is generated
# and requests the next only after it is shown (the sync rule, BUILD_PLAN 5.7); "word" is the
# same rhythm shown word by word; "stream" types one constant stream while the model writes
# ahead into a bounded buffer (ADR-030).
REVEAL_MODES = ("letter", "word", "stream")
# Tables a profile may set over the base configuration and the hardware overlay (command-line
# overrides still win): the screen's pace belongs to the life's shape (ADR-030).
PROFILE_SECTIONS = ("reveal",)
# What a profile with `fixed_mind = true` may not change during a life (ADR-030): the model,
# its threads, its persona and its sampling. Only the hardware shrinks.
FIXED_MIND_FIELDS = (
    "step",
    "threads",
    "persona_groups",
    "mechanics",
    "temperature",
    "min_p",
    "max_tokens",
)
HEALTH_LABELS = ("nominal", "stable", "degrading", "failing", "critical", "terminal")


class ConfigError(ValueError):
    """A configuration problem, reported before anything runs."""


# ---------------------------------------------------------------------------------------
# time strings


_TIME_RE = re.compile(r"^(?:(end)-)?(\d+):([0-5]\d)$")


@dataclass(frozen=True)
class TimeSpec:
    """A keyframe time: seconds within the nominal lifespan, or seconds before the end."""

    seconds: float
    from_end: bool

    def resolve(self, nominal_s: float, lifespan_s: float) -> float:
        """Seconds after birth in a life of lifespan_s.

        A plain time scales with the lifespan relative to nominal_s; an end-anchored time
        keeps its distance from the end.
        """
        if self.from_end:
            return lifespan_s - self.seconds
        return self.seconds * lifespan_s / nominal_s


def parse_duration(text: str | int | float) -> float:
    """Parse "mm:ss" (or a number of seconds) into seconds."""
    if isinstance(text, int | float):
        return float(text)
    m = _TIME_RE.match(text.strip())
    if not m or m.group(1):
        raise ConfigError(f"expected a duration like '60:00', got {text!r}")
    return int(m.group(2)) * 60 + int(m.group(3))


def parse_time(text: str) -> TimeSpec:
    """Parse "mm:ss" or "end-mm:ss"."""
    m = _TIME_RE.match(str(text).strip())
    if not m:
        raise ConfigError(f"expected a time like '28:00' or 'end-3:00', got {text!r}")
    return TimeSpec(int(m.group(2)) * 60 + int(m.group(3)), m.group(1) == "end")


# ---------------------------------------------------------------------------------------
# merging and loading


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Return base with over merged in; tables merge, everything else replaces."""
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)  # type: ignore[arg-type]
        else:
            out[key] = copy.deepcopy(value)
    return out


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"missing config file: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e


def detect_hardware() -> str:
    """Pick the hardware overlay from the machine itself."""
    try:
        model = Path("/proc/device-tree/model").read_text(errors="ignore")
    except OSError:
        return "dev"
    ram_gb = _ram_gb()
    if "Raspberry Pi 4" in model:
        return "pi4-4gb"
    if "Raspberry Pi 5" in model:
        return "pi5-16gb" if ram_gb > 9 else "pi5-8gb"
    return "dev"


def _ram_gb() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return 0.0


# ---------------------------------------------------------------------------------------
# profiles


@dataclass(frozen=True)
class Keyframe:
    """One row of a profile: its time, the knob values set at that time, and the world
    actions performed once when it is reached (ADR-031; never carried to the next row)."""

    at: TimeSpec
    values: dict[str, Any]
    world: tuple[str, ...] = ()


@dataclass
class Profile:
    """A resolved profile: keyframes with every field filled in, times still symbolic."""

    name: str
    nominal_s: float
    lifespan_s: float
    keyframes: list[Keyframe]
    death: TimeSpec | None
    settings: dict[str, Any] = field(default_factory=lambda: {})

    @property
    def unbounded(self) -> bool:
        """True for the homage profile that never forgets and dies when memory is full."""
        return bool(self.settings.get("unbounded", False))

    @property
    def stepped(self) -> tuple[str, ...]:
        """Interpolated knobs this profile sets at a moment instead of easing them (a memory
        cut, a CPU-share step), from its top-level `stepped` list."""
        raw: object = self.settings.get("stepped", [])
        if not isinstance(raw, list):
            raise ConfigError("profile 'stepped' must be a list of knob names")
        names = tuple(str(x) for x in cast("list[object]", raw))
        unknown = sorted(set(names) - set(INTERPOLATED))
        if unknown:
            raise ConfigError(f"profile 'stepped' names knobs that do not interpolate: {unknown}")
        return names

    @property
    def fixed_mind(self) -> bool:
        """True when the model never changes during a life: no reload, no erosion, constant
        sampling (ADR-030); validation holds the keyframes to it."""
        return bool(self.settings.get("fixed_mind", False))

    @property
    def verify_level(self) -> str:
        """How thoroughly `epitaph verify` checks a life of this profile ("full" by default)."""
        return str(self.settings.get("verify_level", "full"))

    def with_lifespan(self, lifespan_s: float) -> Profile:
        """A copy that lives lifespan_s seconds; keyframes stay symbolic until resolved."""
        return Profile(
            self.name, self.nominal_s, lifespan_s, self.keyframes, self.death, self.settings
        )


def profile_path(name: str, hw_class: str) -> Path:
    """Resolve a profile name: 'pi4/default', 'sim', or a bare name within the class."""
    base = CONFIG_DIR / "profiles"
    candidates = [base / f"{name}.toml"] if "/" in name else []
    candidates += [base / hw_class / f"{name}.toml", base / f"{name}.toml"]
    for c in candidates:
        if c.exists():
            return c
    raise ConfigError(f"profile {name!r} not found for class {hw_class!r}")


def load_profile(name: str, hw_class: str, _seen: tuple[str, ...] = ()) -> Profile:
    """Load a profile, following 'extends', and fill every keyframe forward."""
    path = profile_path(name, hw_class)
    key = str(path)
    if key in _seen:
        raise ConfigError(f"profile cycle: {' -> '.join((*_seen, key))}")
    raw = _read_toml(path)
    label = str(path.relative_to(CONFIG_DIR / "profiles").with_suffix(""))

    if "extends" in raw:
        parent = load_profile(str(raw["extends"]), hw_class, (*_seen, key))
        settings = {**parent.settings, **_profile_settings(raw)}
        lifespan = parse_duration(raw["lifespan"]) if "lifespan" in raw else parent.lifespan_s
        death = parent.death
        if "death" in raw:
            death = None if raw["death"] == "none" else parse_time(str(raw["death"]))
        return Profile(label, parent.nominal_s, lifespan, parent.keyframes, death, settings)

    if "lifespan" not in raw or "keyframe" not in raw:
        raise ConfigError(f"{path}: a profile needs 'lifespan' and at least one [[keyframe]]")
    nominal = parse_duration(raw["lifespan"])
    death_raw = str(raw.get("death", "none"))
    death = None if death_raw == "none" else parse_time(death_raw)

    frames: list[Keyframe] = []
    current: dict[str, Any] = {}
    for i, row in enumerate(raw["keyframe"]):
        if "at" not in row:
            raise ConfigError(f"{path}: keyframe {i} has no 'at'")
        unknown = set(row) - {"at", "world", *KNOB_FIELDS}
        if unknown:
            raise ConfigError(f"{path}: keyframe {i} has unknown fields {sorted(unknown)}")
        world = _world_actions(path, i, row.get("world", []))
        current = {**current, **{k: v for k, v in row.items() if k not in ("at", "world")}}
        if i == 0:
            for name, default in OPTIONAL_DEFAULTS.items():
                current.setdefault(name, default)
            missing = [f for f in KNOB_FIELDS if f not in current]
            if missing:
                raise ConfigError(f"{path}: the first keyframe must set {missing}")
        frames.append(Keyframe(parse_time(str(row["at"])), dict(current), world))
    return Profile(label, nominal, nominal, frames, death, _profile_settings(raw))


def _world_actions(path: Path, i: int, raw: object) -> tuple[str, ...]:
    """A keyframe's `world` list (ADR-031), each action checked for its form."""
    from epitaph.body.world import parse_action

    if not isinstance(raw, list):
        raise ConfigError(f"{path}: keyframe {i}: 'world' must be a list of actions")
    out: list[str] = []
    for item in cast("list[object]", raw):
        try:
            parse_action(str(item))
        except ValueError as e:
            raise ConfigError(f"{path}: keyframe {i}: {e}") from e
        out.append(str(item).strip())
    return tuple(out)


def _profile_settings(raw: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in raw.items() if k not in ("keyframe", "extends", "lifespan", "death")}


# ---------------------------------------------------------------------------------------
# the assembled configuration


@dataclass
class Config:
    """Everything a run needs: merged settings, the hardware, the profile and the models."""

    data: dict[str, Any]
    hardware: str
    hw_class: str
    profile: Profile
    models: dict[str, ModelSpec]
    llamacpp_tag: str

    def get(self, dotted: str, default: Any = None) -> Any:
        """Look up a dotted key such as "backend.ctx"; default if any part is missing."""
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]  # type: ignore[index]
        return node

    def section(self, name: str) -> dict[str, Any]:
        """A top-level table, or an empty dict if it is absent or not a table."""
        value = self.data.get(name, {})
        return value if isinstance(value, dict) else {}

    @property
    def ctx(self) -> int:
        """Context window in tokens: the profile's `ctx` if set, else `backend.ctx`."""
        return int(self.profile.settings.get("ctx", self.get("backend.ctx", 2048)))

    @property
    def state_dir(self) -> Path:
        """Where lives and state files go (BUILD_PLAN 6.1).

        "auto" means /var/lib/epitaph on a Pi where it exists, else ~/.local/share/epitaph.
        """
        raw = str(self.get("paths.state_dir", "auto"))
        if raw == "auto":
            if self.hw_class in ("pi4", "pi5") and Path("/var/lib/epitaph").exists():
                return Path("/var/lib/epitaph")
            return Path.home() / ".local/share/epitaph"
        return Path(raw).expanduser()

    def model(self, name: str | None = None) -> ModelSpec:
        """The named model, or the first of `life.models`; raises ConfigError if unknown."""
        wanted = name or str(self.get("life.models", [""])[0])
        if wanted not in self.models:
            raise ConfigError(f"model {wanted!r} is not in config/models.toml")
        return self.models[wanted]


def load_models(hw_class: str) -> tuple[dict[str, ModelSpec], str]:
    """Read config/models.toml: the model specs and the pinned llama.cpp tag.

    Each model gets its precision ladder for hw_class; a class without one uses the Pi 4 ladder.
    """
    raw = _read_toml(CONFIG_DIR / "models.toml")
    ladder_class = hw_class if hw_class in ("pi4", "pi5", "dev") else "pi4"
    out: dict[str, ModelSpec] = {}
    for name, spec in raw.get("models", {}).items():
        ladders = spec.get("ladder", {})
        ladder = ladders.get(ladder_class) or ladders.get("pi4")
        if not ladder:
            raise ConfigError(f"models.toml: {name} has no ladder for {ladder_class}")
        out[name] = ModelSpec(
            name=name,
            source=str(spec.get("source", "")),
            license=str(spec.get("license", "")),
            ladder=tuple(str(q) for q in ladder),
            sliding_window=bool(spec.get("sliding_window", False)),
            chat=bool(spec.get("chat", True)),
        )
    return out, str(raw.get("llamacpp_tag", ""))


def load_config(
    profile: str | None = None,
    hardware: str | None = None,
    lifespan_s: float | None = None,
    overrides: dict[str, Any] | None = None,
    validate: bool = True,
) -> Config:
    """Assemble and validate the configuration for one run."""
    data = _read_toml(CONFIG_DIR / "default.toml")
    hw = hardware or str(data.get("life", {}).get("hardware", "auto"))
    if hw == "auto":
        hw = detect_hardware()
    overlay = _read_toml(CONFIG_DIR / "hardware" / f"{hw}.toml")
    hw_class = str(overlay.pop("class", "dev"))
    data = deep_merge(data, overlay)
    if overrides:
        data = deep_merge(data, overrides)

    prof = load_profile(profile or str(data["life"].get("profile", "default")), hw_class)
    for name in PROFILE_SECTIONS:
        table = prof.settings.get(name)
        if isinstance(table, dict):
            data = deep_merge(data, {name: cast("dict[str, Any]", table)})
    if overrides:
        data = deep_merge(data, overrides)  # the command line wins over the profile too
    if lifespan_s is not None:
        prof = prof.with_lifespan(lifespan_s)
    models, tag = load_models(hw_class)
    cfg = Config(data, hw, hw_class, prof, models, tag)
    if validate:
        validate_config(cfg)
    return cfg


# ---------------------------------------------------------------------------------------
# validation


def system_tokens(cfg: Config, groups: int, mechanics: bool) -> int:
    """Estimated system prompt size in tokens for this many persona groups (BUILD_PLAN 5.3)."""
    est = cfg.section("estimate")
    return int(groups * int(est.get("system_tokens_per_group", 30))) + (
        int(est.get("mechanics_tokens", 70)) if mechanics else 0
    )


def reading_tokens(cfg: Config, form: str, after_birth: bool = False) -> int:
    """Estimated size in tokens of a sensor reading in the given form (full, short, minimal).

    After birth, quiet readings (`prompt.readings_quiet`) say only what changed: the `quiet`
    entry, when the table has one, stands for every form but minimal."""
    table = cfg.get("estimate.reading_tokens", {}) or {}
    quiet = after_birth and bool(cfg.get("prompt.readings_quiet", False)) and form != "minimal"
    if quiet and "quiet" in table:
        return int(table["quiet"])
    return int(table.get(form, 45))


CREATURE_NETWORK = ("blocked", "allowed")  # body.creature_network (ADR-005)


def validate_config(cfg: Config) -> None:
    """Fail fast on impossible settings (BUILD_PLAN 5.4). Thought counts are costmodel's job."""
    p = cfg.profile
    problems: list[str] = []
    times = [kf.at.resolve(p.nominal_s, p.lifespan_s) for kf in p.keyframes]
    if times[0] != 0:
        problems.append("the first keyframe must be at 0:00")
    for (a, b), kf in zip(itertools.pairwise(times), p.keyframes[1:], strict=True):
        if b <= a:
            problems.append(
                f"keyframe at {_fmt(kf.at)} resolves to {b:.0f}s, not after the previous "
                f"one ({a:.0f}s) for a {p.lifespan_s:.0f}s life"
            )
    if times[-1] >= p.lifespan_s:
        problems.append(f"the last keyframe ({times[-1]:.0f}s) is not before the end of life")
    if p.death is not None:
        d = p.death.resolve(p.nominal_s, p.lifespan_s)
        if not 0 < d < p.lifespan_s:
            problems.append(f"death at {_fmt(p.death)} falls outside the life")

    groups_total = len(cfg.get("prompt.persona_groups", []))
    model_names = list(cfg.get("life.models", []))
    ladder_len = min((len(cfg.models[m].ladder) for m in model_names if m in cfg.models), default=3)
    for m in model_names:
        if m not in cfg.models:
            problems.append(f"life.models lists {m!r}, which is not in models.toml")

    try:
        profile_rules(p.settings)
    except ConfigError as e:
        problems.append(str(e))
    try:
        _ = p.stepped
    except ConfigError as e:
        problems.append(str(e))
    mode = cfg.get("reveal.mode", "letter")
    if mode not in REVEAL_MODES:
        problems.append(f"reveal.mode must be one of {REVEAL_MODES}, not {mode!r}")
    if mode == "stream":
        rev = cfg.section("reveal")
        if not float(rev.get("stream_letter_ms", 0)) > 0:
            problems.append("reveal.mode = 'stream' needs reveal.stream_letter_ms above 0")
        if not 0 <= float(rev.get("stream_jitter", 0.0)) < 1:
            problems.append("reveal.stream_jitter must be at least 0 and below 1")
        floor = float(rev.get("stream_min_letter_ms", 165))
        if float(rev.get("stream_letter_ms", 165)) < floor:
            problems.append(f"reveal.stream_letter_ms must be at least {floor:g} (readability)")
        if float(rev.get("stream_gamma", 0.0)) < 0 or float(rev.get("stream_lead_s", 0)) < 0:
            problems.append("reveal.stream_gamma and stream_lead_s must be at least 0")
        if float(rev.get("stream_max_slowdown_per_min", 0.15)) < 0:
            problems.append("reveal.stream_max_slowdown_per_min must be at least 0")
        if int(rev.get("stream_max_thoughts", 1)) < 1 or int(rev.get("stream_max_letters", 1)) < 1:
            problems.append("reveal.stream_max_thoughts and stream_max_letters must be at least 1")
    if p.fixed_mind:
        first = p.keyframes[0].values
        for kf in p.keyframes[1:]:
            moved = [f for f in FIXED_MIND_FIELDS if kf.values[f] != first[f]]
            if moved:
                problems.append(
                    f"keyframe {_fmt(kf.at)}: fixed_mind profile changes {moved} "
                    "(only the hardware may change)"
                )
    network = cfg.get("body.creature_network", "blocked")
    if network not in CREATURE_NETWORK:
        # Anything but "blocked" used to leave the network open: a typo must not (ADR-005).
        problems.append(f"body.creature_network must be one of {CREATURE_NETWORK}, not {network!r}")
    services = {str(x) for x in cfg.get("world.services", []) or []}
    for kf in p.keyframes:
        for action in kf.world:
            kind, _, name = action.partition(":")
            if kind == "service" and name not in services:
                problems.append(
                    f"keyframe {_fmt(kf.at)}: world stops {name!r}, which is not in "
                    "[world] services (the allowed list)"
                )
    if cfg.get("prompt.readings_material", False) and not cfg.get("prompt.readings_quiet", False):
        problems.append("prompt.readings_material needs prompt.readings_quiet (quotes ride on it)")

    ctx = cfg.ctx
    for kf, t in zip(p.keyframes, times, strict=True):
        v = kf.values
        where = f"keyframe {_fmt(kf.at)}"
        if v["readings"] not in READINGS_FORMS:
            problems.append(f"{where}: readings must be one of {READINGS_FORMS}")
        if v["health"] not in HEALTH_LABELS:
            problems.append(f"{where}: unknown health label {v['health']!r}")
        if not 0 <= int(v["step"]) < ladder_len:
            problems.append(
                f"{where}: ladder step {v['step']} does not exist (0..{ladder_len - 1})"
            )
        if not 1 <= int(v["threads"]) <= 3:
            problems.append(f"{where}: threads must be 1-3 (core 0 belongs to the controller)")
        if not 0 < float(v["cpu_share"]) <= int(v["threads"]):
            problems.append(f"{where}: cpu_share must be above 0 and at most threads")
        lo, hi = CPU_MHZ_RANGE
        if not lo <= float(v["cpu_mhz"]) <= hi:
            problems.append(f"{where}: cpu_mhz must be {lo:.0f}-{hi:.0f}")
        if not 0 <= int(v["persona_groups"]) <= groups_total:
            problems.append(f"{where}: persona_groups must be 0-{groups_total}")
        if p.unbounded:
            continue
        need = (
            system_tokens(cfg, groups_total, True)
            + reading_tokens(cfg, "full")
            + int(v["recall"])
            + int(v["max_tokens"])
        )
        if need > ctx:
            problems.append(
                f"{where} (t={t:.0f}s): system + reading + recall + max_tokens = {need} "
                f"exceeds ctx {ctx}"
            )
    if problems:
        raise ConfigError(f"profile {p.name} ({cfg.hardware}):\n  - " + "\n  - ".join(problems))


def _fmt(t: TimeSpec) -> str:
    m, s = divmod(int(t.seconds), 60)
    return f"{'end-' if t.from_end else ''}{m}:{s:02d}"

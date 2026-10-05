# pyright: strict
"""Heat: the temperature, the firmware's throttling bits, and the thermal pause.

The Pi 4 runs the creature at 40-57 °C with no fan (spikes S1c, S7), far below the firmware's
own throttling at 80-85 °C, so at normal temperatures nothing here ever pauses a life. The
pause exists for a hot room or a blocked case: at or above `body.thermal_limit_c` the
controller should wait before the next request, polling every `thermal_poll_s`, until the
CPU is back at `thermal_resume_c` (hysteresis, so it does not flap at the limit). A pause
is a longer silence between thoughts, never a stop mid-thought; the life clock keeps running.

`ThermalGuard.pause_s()` is the hook: 0 normally, else how long to wait before asking again.
The readings come from sysfs (`thermal_zone0`) and `vcgencmd get_throttled` (vitals.py); a
machine without them (the laptop) never pauses.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from epitaph.body.vitals import (
    DEFAULT_PATHS,
    THROTTLED_NOW,
    UNDERVOLT_NOW,
    SysPaths,
    describe_throttled,
    read_cpu_temp,
    read_throttled,
)

if TYPE_CHECKING:
    from epitaph.config import Config

log = logging.getLogger(__name__)

SOFT_LIMIT_NOW = 0x8  # the firmware's soft temperature limit is active


@dataclass(frozen=True)
class ThermalSettings:
    """`body.thermal_limit_c`, `body.thermal_resume_c` and `body.thermal_poll_s`."""

    limit_c: float = 80.0
    resume_c: float = 75.0
    poll_s: float = 15.0

    @classmethod
    def from_config(cls, cfg: Config) -> ThermalSettings:
        """Read the thermal keys of `[body]`; resume defaults to 5 °C under the limit."""
        body = cfg.section("body")
        limit = float(body.get("thermal_limit_c", cls.limit_c))
        resume = float(body.get("thermal_resume_c", limit - 5.0))
        return cls(
            limit_c=limit,
            resume_c=min(resume, limit),
            poll_s=float(body.get("thermal_poll_s", cls.poll_s)),
        )


class ThermalGuard:
    """Decides the thermal pause from the CPU temperature, with hysteresis."""

    def __init__(
        self,
        settings: ThermalSettings | None = None,
        temp: Callable[[], float | None] | None = None,
        throttled: Callable[[], int | None] | None = None,
        paths: SysPaths = DEFAULT_PATHS,
    ) -> None:
        """Read the temperature with `temp` and the firmware bits with `throttled`.

        Both default to the real readers on `paths`.
        """
        self.settings = settings or ThermalSettings()
        self._temp = temp or (lambda: read_cpu_temp(paths))
        self._throttled = throttled or (lambda: read_throttled(paths))
        self.paused = False
        self.pauses = 0  # how many times a pause began
        self.last_c: float | None = None

    def pause_s(self) -> float:
        """Seconds to wait before the next request: 0 unless the CPU is too hot.

        A pause starts at `limit_c` and lasts until the temperature is back at `resume_c`;
        meanwhile each call returns `poll_s`. An unreadable temperature never pauses.
        """
        t = self._temp()
        self.last_c = t
        s = self.settings
        if t is None:
            self.paused = False
            return 0.0
        if self.paused and t <= s.resume_c:
            self.paused = False
            log.info("thermal pause over: %.1f °C", t)
        elif not self.paused and t >= s.limit_c:
            self.paused = True
            self.pauses += 1
            log.warning("thermal pause: %.1f °C at or above %.1f °C", t, s.limit_c)
        return s.poll_s if self.paused else 0.0

    def state(self) -> dict[str, Any]:
        """For the vitals and the logs: temperature, firmware bits, pause state."""
        bits = self._throttled()
        return {
            "cpu_c": self.last_c if self.last_c is not None else self._temp(),
            "throttled": bits,
            "throttled_flags": describe_throttled(bits) if bits is not None else [],
            "throttling_now": bool(bits is not None and bits & (THROTTLED_NOW | SOFT_LIMIT_NOW)),
            "undervolt_now": bool(bits is not None and bits & UNDERVOLT_NOW),
            "thermal_paused": self.paused,
            "thermal_pauses": self.pauses,
            "limit_c": self.settings.limit_c,
        }

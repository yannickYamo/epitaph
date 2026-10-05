"""Exhibition hours: the hours, the time sync, and the controller outside them.

The controller runs on the fakes in virtual time; the wall clock is a fake that starts at a
chosen moment and follows the virtual clock. No network, no real time.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import subprocess
from datetime import datetime, timedelta
from typing import Any

import pytest

from epitaph.clock import VirtualClock, run_virtual
from epitaph.config import ConfigError, load_config
from epitaph.controller import EXHIBIT_POLL_S, EXHIBIT_REPEAT_S, Controller
from epitaph.events import Event
from epitaph.exhibit import (
    MAX_ERROR_US,
    STA_UNSYNC,
    TIME_ERROR,
    Exhibit,
    Hours,
    adjtimex_status,
    ntp_synced,
    parse_hours,
)
from epitaph.sim import make_controller

SMOKE = "pi4/smoke-300"  # 5 minutes, deadline death
BACKGROUND: list[asyncio.Future[None]] = []  # tasks a test schedules beside the controller
DAY = datetime(2026, 10, 1)


def at(hhmm: str, day: int = 0) -> datetime:
    h, m = hhmm.split(":")
    return DAY + timedelta(days=day, hours=int(h), minutes=int(m))


# -- the hours ----------------------------------------------------------------------------


def test_parse_hours() -> None:
    assert parse_hours("") is None
    assert parse_hours("  ") is None
    assert parse_hours("10:00-18:00") == Hours(600, 1080)
    assert parse_hours(" 9:30 - 17:05 ") == Hours(570, 1025)
    assert parse_hours("22:00-02:00") == Hours(1320, 120)
    assert parse_hours("08:00-24:00") == Hours(480, 0)
    assert str(parse_hours("09:05-17:00")) == "09:05-17:00"


@pytest.mark.parametrize(
    "bad", ["10-18", "10:00", "10:00-", "25:00-18:00", "10:60-18:00", "a:00-b:00", "10:0-18:00"]
)
def test_parse_hours_rejects_bad_text(bad: str) -> None:
    with pytest.raises(ConfigError, match="HH:MM-HH:MM"):
        parse_hours(bad)


def test_parse_hours_rejects_an_empty_day() -> None:
    with pytest.raises(ConfigError, match="always on"):
        parse_hours("10:00-10:00")


def test_hours_open_and_close() -> None:
    h = Hours(600, 1080)
    assert not h.is_open(at("09:59"))
    assert h.is_open(at("10:00"))
    assert h.is_open(at("17:59") + timedelta(seconds=59))
    assert not h.is_open(at("18:00"))
    assert h.next_opening(at("09:00")) == at("10:00")
    assert h.next_opening(at("10:00")) == at("10:00")
    assert h.next_opening(at("18:30")) == at("10:00", day=1)


def test_hours_across_midnight() -> None:
    h = Hours(1320, 120)  # 22:00-02:00
    assert h.is_open(at("23:30")) and h.is_open(at("00:00")) and h.is_open(at("01:59"))
    assert not h.is_open(at("02:00")) and not h.is_open(at("12:00"))
    assert h.next_opening(at("03:00")) == at("22:00")


# -- the time sync ------------------------------------------------------------------------


def fake_run(stdout: str, rc: int = 0) -> Any:
    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        assert argv[:2] == ["timedatectl", "show"]
        return subprocess.CompletedProcess(argv, rc, stdout, "")

    return run


def no_adjtimex() -> tuple[int, int, int]:
    raise OSError("not here")


def test_ntp_synced_from_adjtimex() -> None:
    assert ntp_synced(lambda: (0, 0, 1000), run=None) is True
    assert ntp_synced(lambda: (TIME_ERROR, STA_UNSYNC, MAX_ERROR_US), run=None) is False
    assert ntp_synced(lambda: (0, STA_UNSYNC, 1000), run=None) is False
    assert ntp_synced(lambda: (0, 0, MAX_ERROR_US), run=None) is False


def test_ntp_synced_falls_back_to_timedatectl() -> None:
    assert ntp_synced(no_adjtimex, fake_run("yes\n")) is True
    assert ntp_synced(no_adjtimex, fake_run("no\n")) is False
    assert ntp_synced(no_adjtimex, fake_run("", rc=1)) is None
    assert ntp_synced(no_adjtimex, fake_run("maybe")) is None

    def broken(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("timedatectl")

    assert ntp_synced(no_adjtimex, broken) is None
    assert ntp_synced(None, None) is None


def test_adjtimex_reads_the_kernel() -> None:
    try:
        state, status, maxerror = adjtimex_status()
    except OSError:
        pytest.skip("no adjtimex here")
    assert 0 <= state <= TIME_ERROR and status >= 0 and maxerror >= 0


class Wall:
    """A wall clock that starts at `start` and moves with `now()` (virtual seconds)."""

    def __init__(self, start: datetime, now: Any = None) -> None:
        self.start = start
        self.t = 0.0
        self.now = now or (lambda: self.t)

    def __call__(self) -> datetime:
        return self.start + timedelta(seconds=self.now())


def test_unsynced_time_disables_the_hours_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    wall = Wall(at("03:00"))
    synced: list[bool | None] = [False]
    ex = Exhibit(Hours(600, 1080), "pause", wall=wall, synced=lambda: synced[0], mono=wall.now)
    with caplog.at_level(logging.WARNING, logger="epitaph.exhibit"):
        assert ex.is_open()  # 03:00 is closed, but the time cannot be trusted
        assert ex.is_open()
    assert [r.message for r in caplog.records].count(caplog.records[0].message) == 1
    assert "not NTP-synchronized" in caplog.records[0].message
    assert ex.seconds_to_open() == 0.0
    synced[0] = True
    assert ex.is_open()  # not re-read yet
    wall.t = 60.0
    assert not ex.is_open()  # re-read: synced, and 03:01 is closed
    assert ex.seconds_to_open() == pytest.approx(7 * 3600 - 60)
    assert ex.describe() == {
        "hours": "10:00-18:00",
        "outside": "pause",
        "open": False,
        "time_synced": True,
    }


def test_unknown_or_failing_sync_counts_as_unsynced() -> None:
    def boom() -> bool:
        raise RuntimeError("no dbus")

    for fn in (lambda: None, boom):
        ex = Exhibit(Hours(600, 1080), wall=lambda: at("03:00"), synced=fn, mono=lambda: 0.0)
        assert ex.is_open() and not ex.time_trusted()


def test_no_hours_is_always_open_without_reading_the_sync() -> None:
    def never() -> bool:
        raise AssertionError("read")

    ex = Exhibit(None, wall=lambda: at("03:00"), synced=never)
    assert not ex.enabled and ex.is_open() and ex.seconds_to_open() == 0.0


def test_exhibit_from_config() -> None:
    cfg = load_config(SMOKE, "pi4-4gb", overrides={"exhibit": {"hours": "10:00-18:00"}})
    ex = Exhibit.from_config(cfg, wall=lambda: at("12:00"), synced=lambda: True)
    assert ex.enabled and not ex.pause and ex.is_open()
    bad = load_config(SMOKE, "pi4-4gb", overrides={"exhibit": {"outside": "sleep"}})
    with pytest.raises(ConfigError, match=r"exhibit\.outside"):
        Exhibit.from_config(bad)


def test_default_config_is_always_on() -> None:
    ex = Exhibit.from_config(load_config(SMOKE, "pi4-4gb"))
    assert not ex.enabled and ex.outside == "unseen"


# -- the controller outside the hours ------------------------------------------------------


def run(
    hours: str,
    outside: str,
    start: datetime,
    *,
    lives: int,
    synced: bool | None = True,
    during: Any = None,
) -> tuple[Controller, list[Event], Wall]:
    """`lives` smoke lives on the fakes, the wall clock starting at `start`."""
    cfg = load_config(SMOKE, "pi4-4gb")
    events: list[Event] = []
    notes: list[str] = []
    walls: list[Wall] = []

    async def main(clock: VirtualClock) -> Controller:
        ctl = make_controller(cfg, clock, lives=lives, publish=events)
        wall = Wall(start, clock.now)
        walls.append(wall)
        ctl.exhibit = Exhibit(
            parse_hours(hours), outside, wall=wall, synced=lambda: synced, mono=clock.now
        )
        ctl.notify = notes.append
        if during is not None:
            during(ctl, clock)
        await ctl.run()
        ctl.notes = notes  # type: ignore[attr-defined]
        return ctl

    ctl = run_virtual(main)
    return ctl, events, walls[0]


def wall_of(e: Event, w: Wall, events: list[Event]) -> datetime:
    """The fake wall time of event `e` (ts is real wall time at loop 0 plus loop time)."""
    t0 = events[0]["ts"] - 0.0  # the first event is stamped at loop time 0
    return w.start + timedelta(seconds=e["ts"] - t0)


def flips(events: list[Event]) -> list[bool]:
    """The exhibit states in order, without the repeats of the same state."""
    out: list[bool] = []
    for e in events:
        if e["type"] == "exhibit" and (not out or out[-1] != bool(e["open"])):
            out.append(bool(e["open"]))
    return out


def test_unseen_lives_go_on_with_the_screen_dark() -> None:
    # Closed at the start, open 10:00-10:12, closed again: five lives through all of it.
    ctl, ev, w = run("10:00-10:12", "unseen", at("09:56"), lives=5)
    births = [e for e in ev if e["type"] == "birth"]
    assert len(births) == 5 and all(r.cause == "deadline" for r in ctl.records)
    changes = [e for e in ev if e["type"] == "exhibit"]
    # A dark screen, its repeats and the re-darkening after each birth_loading; one opening.
    assert flips(ev) == [False, True, False]
    opening = next(e for e in changes if e["open"])
    closing = next(e for e in changes if not e["open"] and e["ts"] > opening["ts"])
    assert abs((wall_of(opening, w, ev) - at("10:00")).total_seconds()) <= 5.0
    assert abs((wall_of(closing, w, ev) - at("10:12")).total_seconds()) <= 5.0
    assert not ctl.shown and ctl.state == "stopped"


def test_unseen_a_new_life_is_told_the_screen_stays_dark() -> None:
    _, ev, _ = run("10:00-11:00", "unseen", at("08:00"), lives=2)
    types = [(e["type"], e["life"], e.get("open")) for e in ev]
    for n in (1, 2):
        i = types.index(("birth_loading", n, None))
        assert types[i + 1] == ("exhibit", n, False)
    assert ("exhibit", 1, True) not in types


def test_unseen_repeats_the_dark_screen_for_late_displays() -> None:
    ctl, ev, _ = run("10:00-11:00", "unseen", at("08:00"), lives=1)
    dark = [e["ts"] for e in ev if e["type"] == "exhibit"]
    gaps = [b - a for a, b in itertools.pairwise(dark)]
    assert dark and max(gaps) <= EXHIBIT_REPEAT_S + 5.0  # a display that joins goes dark soon
    assert ctl.records[0].cause == "deadline"


def test_pause_waits_for_the_opening_and_pings_meanwhile() -> None:
    ctl, ev, w = run("10:00-18:00", "pause", at("09:30"), lives=1)
    birth_loading = next(e for e in ev if e["type"] == "birth_loading")
    started = wall_of(birth_loading, w, ev)
    assert at("10:00") <= started <= at("10:00") + timedelta(seconds=EXHIBIT_POLL_S)
    assert [e["open"] for e in ev if e["type"] == "exhibit"][:1] == [False]
    opened = next(e for e in ev if e["type"] == "exhibit" and e["open"])
    assert opened["ts"] <= birth_loading["ts"]
    # Thirty minutes of waiting: the watchdog kept pinging, the loop never counted as stuck.
    assert not ctl.wedged and ctl.records[0].cause == "deadline"
    assert ctl.notes.count("WATCHDOG=1") >= 30 * 60 / 5 - 1  # type: ignore[attr-defined]


def test_pause_finishes_the_life_then_waits_across_midnight() -> None:
    # A life starts at 17:58 and runs past the 18:00 closing; the next one waits for 10:00.
    ctl, ev, w = run("10:00-18:00", "pause", at("17:58"), lives=2)
    assert [r.cause for r in ctl.records] == ["deadline", "deadline"]
    first = [e for e in ev if e["life"] == 1]
    silence = next(i for i, e in enumerate(first) if e["type"] == "silence")
    assert not [e for e in first[:silence] if e["type"] == "exhibit"]  # shown to its end
    death = next(e for e in first if e["type"] == "death")
    assert death["lived_s"] == pytest.approx(300.0, abs=1.0)  # not cut at the closing
    closed = next(e for e in ev if e["type"] == "exhibit" and not e["open"])
    assert wall_of(closed, w, ev) >= at("18:00")
    second = next(e for e in ev if e["type"] == "birth_loading" and e["life"] == 2)
    started = wall_of(second, w, ev)
    assert at("10:00", day=1) <= started <= at("10:00", day=1) + timedelta(seconds=EXHIBIT_POLL_S)
    assert not ctl.wedged
    assert ctl.notes.count("WATCHDOG=1") >= 16 * 3600 / 5 - 1  # type: ignore[attr-defined]


def test_pause_with_hours_across_midnight() -> None:
    # Open 22:00-02:00; started at 01:58, the second life waits for 22:00 the same day.
    ctl, ev, w = run("22:00-02:00", "pause", at("01:58"), lives=2)
    second = next(e for e in ev if e["type"] == "birth_loading" and e["life"] == 2)
    assert at("22:00") <= wall_of(second, w, ev) <= at("22:00") + timedelta(minutes=1)
    assert [r.cause for r in ctl.records] == ["deadline", "deadline"]


def test_pause_stall_budget_covers_the_wait() -> None:
    # A wait of 15.5 hours, stamped at every look at the clock: never a stuck loop.
    ctl, _, _ = run("10:00-18:00", "pause", at("18:30"), lives=1)
    assert not ctl.wedged and ctl.records[0].cause == "deadline"
    ctl.state = "closed"
    _, budget = ctl._stall_budget(None)  # pyright: ignore[reportPrivateUsage]
    assert budget > EXHIBIT_POLL_S


def test_a_life_with_exhibit_events_still_verifies() -> None:
    from epitaph import verify as v

    _, ev, _ = run("10:00-11:00", "unseen", at("10:58"), lives=2)
    life = [e for e in ev if e["life"] == 1]
    assert any(e["type"] == "exhibit" for e in life)
    res = v.verify_life(v.parse_life(life), load_config(SMOKE, "pi4-4gb"), "smoke")
    failed = [c.name for c in res.checks if c.status == "fail"]
    assert not failed


def test_unsynced_time_runs_lives_in_closed_hours() -> None:
    ctl, ev, _ = run("10:00-18:00", "pause", at("03:00"), lives=2, synced=False)
    assert not [e for e in ev if e["type"] == "exhibit"]
    loading = [e for e in ev if e["type"] == "birth_loading"]
    assert len(loading) == 2 and loading[0]["ts"] == ev[0]["ts"]
    assert ctl.shown and ctl.exhibit.describe()["time_synced"] is False


def test_new_life_during_closed_hours_starts_one_now() -> None:
    def during(ctl: Controller, clock: VirtualClock) -> None:
        async def ask() -> None:
            await asyncio.sleep(600)
            await ctl.ctl_new_life({})

        BACKGROUND.append(asyncio.ensure_future(ask()))

    _, ev, w = run("10:00-18:00", "pause", at("20:00"), lives=1, during=during)
    loading = next(e for e in ev if e["type"] == "birth_loading")
    assert wall_of(loading, w, ev) == pytest.approx(at("20:10"), abs=timedelta(seconds=1))
    assert flips(ev) == [False, True]


def test_status_reports_the_exhibition() -> None:
    ctl, _, _ = run("10:00-18:00", "unseen", at("20:00"), lives=1)
    status = ctl._status()  # pyright: ignore[reportPrivateUsage]
    assert status["exhibit"] == {
        "hours": "10:00-18:00",
        "outside": "unseen",
        "open": False,
        "time_synced": True,
        "shown": False,
    }


def test_bad_exhibit_config_stops_the_controller_at_start() -> None:
    cfg = load_config(SMOKE, "pi4-4gb", overrides={"exhibit": {"hours": "ten to six"}})

    async def main(clock: VirtualClock) -> None:
        make_controller(cfg, clock)

    with pytest.raises(ConfigError, match=r"exhibit\.hours"):
        run_virtual(main)


def test_the_simulator_reads_the_hours_on_its_own_clock() -> None:
    # Without an injected Exhibit the controller takes [exhibit] from the config, on the
    # simulated wall clock (`ts`), which counts as synced.
    cfg = load_config(SMOKE, "pi4-4gb", overrides={"exhibit": {"hours": "00:00-00:01"}})
    events: list[Event] = []

    async def main(clock: VirtualClock) -> Controller:
        ctl = make_controller(cfg, clock, lives=1, publish=events)
        await ctl.run()
        return ctl

    ctl = run_virtual(main)
    assert ctl.exhibit.enabled and ctl.exhibit.describe()["time_synced"] is True

"""sd_notify and the watchdog checks (BUILD_PLAN 9 C5), with no systemd and no real time."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from epitaph.body.watchdog import Notifier, hardware_watchdog_usec, parse_usec


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def make(env: dict[str, str]) -> tuple[Notifier, list[tuple[str, bytes]], Clock]:
    sent: list[tuple[str, bytes]] = []
    clock = Clock()
    n = Notifier(env, send=lambda a, p: sent.append((a, p)), now=clock)
    return n, sent, clock


def test_outside_systemd_everything_is_a_noop() -> None:
    n, sent, _ = make({})
    assert not n.enabled
    assert not n.ready() and not n.ping() and not n.maybe_ping() and not n.status("x")
    assert sent == [] and n.ping_every_s is None


def test_ready_status_stopping() -> None:
    n, sent, _ = make({"NOTIFY_SOCKET": "/run/systemd/notify"})
    assert n.ready("life 3")
    assert n.status("silence")
    assert n.stopping()
    assert [p for _, p in sent] == [b"READY=1\nSTATUS=life 3", b"STATUS=silence", b"STOPPING=1"]
    assert sent[0][0] == "/run/systemd/notify"
    assert n.ready() and sent[-1][1] == b"READY=1"


def test_pings_at_half_the_interval() -> None:
    env = {
        "NOTIFY_SOCKET": "@notify",
        "WATCHDOG_USEC": "30000000",
        "WATCHDOG_PID": str(os.getpid()),
    }
    n, sent, clock = make(env)
    assert n.ping_every_s == 15.0
    assert n.maybe_ping()  # the first call always pings
    clock.t += 14.9
    assert not n.maybe_ping()
    clock.t += 0.2
    assert n.maybe_ping()
    assert [p for _, p in sent] == [b"WATCHDOG=1", b"WATCHDOG=1"]


def test_watchdog_meant_for_another_pid_is_ignored() -> None:
    n, _, _ = make({"NOTIFY_SOCKET": "/s", "WATCHDOG_USEC": "30000000", "WATCHDOG_PID": "1"})
    assert n.watchdog_s is None and not n.maybe_ping()


def test_send_errors_are_swallowed() -> None:
    def broken(_a: str, _p: bytes) -> None:
        raise ConnectionRefusedError

    n = Notifier({"NOTIFY_SOCKET": "/nowhere"}, send=broken)
    assert not n.ready()


def test_real_datagram_to_a_socket(tmp_path: Path) -> None:
    path = tmp_path / "notify"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(str(path))
        n = Notifier({"NOTIFY_SOCKET": str(path)})
        assert n.ready()
        assert server.recv(100) == b"READY=1"


@pytest.mark.parametrize(
    ("text", "usec"),
    [("1min", 60_000_000), ("30s", 30_000_000), ("0", 0), ("1min 30s", 90_000_000),
     ("500ms", 500_000), ("infinity", 2**63 - 1), ("", None), ("soon", None), ("3d", None)],
)  # fmt: skip
def test_parse_usec(text: str, usec: int | None) -> None:
    assert parse_usec(text) == usec


def test_hardware_watchdog_usec() -> None:
    assert hardware_watchdog_usec(lambda argv: "1min\n") == 60_000_000
    assert hardware_watchdog_usec(lambda argv: "0\n") == 0

    def fails(argv: list[str]) -> str:
        raise FileNotFoundError(argv[0])

    assert hardware_watchdog_usec(fails) is None

"""The hardware watchdog check, with no systemd."""

from __future__ import annotations

import pytest

from epitaph.body.watchdog import hardware_watchdog_usec, parse_usec


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

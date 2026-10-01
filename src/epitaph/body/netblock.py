# pyright: strict
"""The creature has no network (ADR-005, BUILD_PLAN 5.6, 9 C8).

An nftables rule matching the creature cgroup (`socket cgroupv2`) refuses every outbound packet
that is not for loopback: llama-server serves the controller on 127.0.0.1, and nothing else
reaches it or leaves it. The rule needs root; the controller does not have it. The root-owned
helper `/usr/local/sbin/epitaph-netblock` (deploy/sbin/epitaph-netblock) loads it, and a sudoers
drop-in lets the service user run exactly that helper; deploy/install.sh installs both.

nft resolves the cgroup path to the cgroup's id (its inode) when the rule is loaded (spike S3b),
so the rule follows the cgroup, not its name:

- across lives it holds: the creature cgroup is created once by the body's setup() and kept
  (a death empties it, nothing removes it);
- a restarted controller gets a new service cgroup, so setup() loads the rule again, before
  any creature is spawned. The helper drops the rules of cgroups that no longer exist.

`NetBlock.verify` checks that the loaded rule names the cgroup's current id.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

log = logging.getLogger(__name__)

HELPER = "/usr/local/sbin/epitaph-netblock"
CGROUP_FS = Path("/sys/fs/cgroup")
BLOCKED = "blocked"
ALLOWED = "allowed"

# Runs argv and returns (exit status, stdout); injected so that tests never call sudo.
Runner = Callable[[Sequence[str]], tuple[int, str]]

# nft prints the cgroup as its id, or as its path when it can still resolve it.
_RULE = re.compile(r'socket cgroupv2 level \d+ (?:(\d+)|"([^"]+)") .*comment "([^"]+)"')
RESOLVED = -1  # the rule printed a live path rather than an id


class NetBlockError(RuntimeError):
    """The rule could not be loaded or does not match the creature cgroup."""


def sudo_capture(argv: Sequence[str]) -> tuple[int, str]:
    """Run argv under `sudo -n` with a 15 s limit: (exit status, stdout); 127 if it cannot run."""
    try:
        res = subprocess.run(
            ["sudo", "-n", *argv], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)
    if res.returncode != 0:
        log.error("%s exited %d: %s", " ".join(argv), res.returncode, res.stderr.strip())
    return res.returncode, res.stdout


def relative(cgroup: Path, fs: Path = CGROUP_FS) -> str:
    """A cgroup directory as nft names it: its path below the cgroup root."""
    return str(cgroup.relative_to(fs))


def parse_rules(listing: str) -> dict[str, set[int]]:
    """`nft list` output of the helper's table: the cgroup ids blocked, by cgroup path.

    A rule printed with its live path instead of an id counts as RESOLVED for that path.
    """
    out: dict[str, set[int]] = {}
    for m in _RULE.finditer(listing):
        ident, path, comment = m.group(1), m.group(2), m.group(3)
        if ident is not None:
            out.setdefault(comment, set()).add(int(ident))
        elif path == comment:
            out.setdefault(comment, set()).add(RESOLVED)
    return out


class NetBlock:
    """Loads, checks and removes the creature cgroup's rule through the helper."""

    def __init__(self, helper: str = HELPER, runner: Runner | None = None) -> None:
        """Call `helper` through `runner` (default: sudo_capture; a fake in tests)."""
        self.helper = helper
        self.runner = runner

    def _run(self, *args: str) -> tuple[int, str]:
        if self.runner is None and not Path(self.helper).is_file():
            return 127, f"{self.helper} is not installed (deploy/install.sh)"
        return (self.runner or sudo_capture)([self.helper, *args])

    def block(self, cgroup: Path, fs: Path = CGROUP_FS) -> None:
        """Refuse the cgroup's outbound traffic except loopback; raise NetBlockError if not done."""
        rel = relative(cgroup, fs)
        rc, out = self._run("add", rel)
        if rc != 0:
            raise NetBlockError(f"{self.helper} add {rel} exited {rc}: {out.strip()}")
        if not self.verify(cgroup, fs):
            raise NetBlockError(f"no rule for {rel} with its cgroup id after {self.helper} add")
        log.info("creature network blocked: %s", rel)

    def unblock(self, cgroup: Path, fs: Path = CGROUP_FS) -> bool:
        """Remove the cgroup's rule (a selftest's or calibration's own); False if that failed."""
        rc, _ = self._run("del", relative(cgroup, fs))
        return rc == 0

    def rules(self) -> dict[str, set[int]]:
        """The rules loaded now, by cgroup path; empty when none or when the helper failed."""
        rc, out = self._run("status")
        return parse_rules(out) if rc == 0 else {}

    def verify(self, cgroup: Path, fs: Path = CGROUP_FS) -> bool:
        """Whether a loaded rule names this cgroup by its current id (inode)."""
        try:
            ident = cgroup.stat().st_ino
        except OSError:
            return False
        return bool({ident, RESOLVED} & self.rules().get(relative(cgroup, fs), set()))

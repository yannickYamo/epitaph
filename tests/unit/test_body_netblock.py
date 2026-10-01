"""The creature's network block: the helper, its sudoers rule and the body (BUILD_PLAN 9 C8)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from epitaph.body.cgroup import CgroupBody, CgroupError, CgroupSettings
from epitaph.body.netblock import (
    BLOCKED,
    RESOLVED,
    NetBlock,
    NetBlockError,
    parse_rules,
    relative,
)
from epitaph.body.selftest import check_network
from epitaph.body.vitals import SysPaths
from tests.unit.test_body_cgroup import REL

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "deploy" / "sbin" / "epitaph-netblock"
CG = f"{REL}/creature"

# What nft 1.1.3 printed on the Pi for a live cgroup (the path) and what it prints for a cgroup
# that is gone (its id).
SELFTEST_CG = "system.slice/epitaph-selftest-9.service/creature"
REJECT = "counter packets 0 bytes 0 reject with"
LISTING = "\n".join(
    [
        "table inet epitaph { # handle 1",
        "\tchain output { # handle 1",
        "\t\ttype filter hook output priority filter; policy accept;",
        f'\t\tsocket cgroupv2 level 3 "{CG}" ip daddr != 127.0.0.0/8 {REJECT} icmp'
        f' port-unreachable comment "{CG}" # handle 5',
        f'\t\tsocket cgroupv2 level 3 "{CG}" ip6 daddr != ::1 {REJECT} icmpv6'
        f' port-unreachable comment "{CG}" # handle 6',
        f"\t\tsocket cgroupv2 level 3 7266 ip daddr != 127.0.0.0/8 {REJECT} icmp"
        f' port-unreachable comment "{SELFTEST_CG}" # handle 7',
        "\t}",
        "}",
    ]
)


class FakeHelper:
    """The helper as the body sees it: records calls, answers `status` with a listing."""

    def __init__(self, listing: str = LISTING, rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.listing = listing
        self.rc = rc

    def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        self.calls.append(list(argv[1:]))
        if argv[1] == "status":
            return 0, self.listing
        return self.rc, "epitaph-netblock: done\n"


@pytest.fixture
def cgroup_root(tmp_path: Path) -> Path:
    fs = tmp_path / "cgroup"
    root = fs / REL
    root.mkdir(parents=True)
    (root / "cgroup.procs").write_text("4242\n")
    (root / "cgroup.controllers").write_text("cpuset cpu io memory pids\n")
    (root / "cgroup.subtree_control").write_text("\n")
    return fs


def body_with(fs: Path, helper: FakeHelper | None, **kw: object) -> CgroupBody:
    settings = CgroupSettings(**kw)  # type: ignore[arg-type]
    nb = NetBlock("helper", helper) if helper is not None else None
    return CgroupBody(fs / REL, settings, sys_paths=SysPaths(), netblock=nb, fs=fs)


# --- parsing and the NetBlock calls -----------------------------------------------------------


def test_parse_rules_reads_paths_and_ids() -> None:
    rules = parse_rules(LISTING)
    assert rules == {CG: {RESOLVED}, SELFTEST_CG: {7266}}
    assert parse_rules("") == {}


def test_relative(tmp_path: Path) -> None:
    assert relative(tmp_path / REL / "creature", tmp_path) == CG


def test_block_verifies_the_rule(cgroup_root: Path) -> None:
    creature = cgroup_root / REL / "creature"
    creature.mkdir()
    h = FakeHelper()
    NetBlock("helper", h).block(creature, cgroup_root)
    assert h.calls == [["add", CG], ["status"]]


def test_block_accepts_the_id_form(cgroup_root: Path) -> None:
    creature = cgroup_root / REL / "creature"
    creature.mkdir()
    ino = creature.stat().st_ino
    listing = f'socket cgroupv2 level 3 {ino} ip daddr != 127.0.0.0/8 reject comment "{CG}"'
    assert NetBlock("helper", FakeHelper(listing)).verify(creature, cgroup_root)
    stale = f'socket cgroupv2 level 3 {ino + 1} ip daddr != 127.0.0.0/8 reject comment "{CG}"'
    assert not NetBlock("helper", FakeHelper(stale)).verify(creature, cgroup_root)


def test_block_failures(cgroup_root: Path) -> None:
    creature = cgroup_root / REL / "creature"
    creature.mkdir()
    with pytest.raises(NetBlockError, match="exited 1"):
        NetBlock("helper", FakeHelper(rc=1)).block(creature, cgroup_root)
    with pytest.raises(NetBlockError, match="no rule"):
        NetBlock("helper", FakeHelper(listing="")).block(creature, cgroup_root)
    assert not NetBlock("helper", FakeHelper()).verify(cgroup_root / "gone", cgroup_root)


def test_missing_helper_is_reported_not_run(cgroup_root: Path, tmp_path: Path) -> None:
    creature = cgroup_root / REL / "creature"
    creature.mkdir()
    nb = NetBlock(str(tmp_path / "no-such-helper"))
    with pytest.raises(NetBlockError, match="not installed"):
        nb.block(creature, cgroup_root)
    assert nb.rules() == {}


# --- the body ----------------------------------------------------------------------------------


def test_setup_blocks_the_network_before_any_spawn(cgroup_root: Path) -> None:
    h = FakeHelper()
    body = body_with(cgroup_root, h)
    body.setup()
    assert body.network == BLOCKED
    assert h.calls[0] == ["add", CG]
    body.reset_creature_cgroup()  # a death: the cgroup is kept, the rule is not reloaded
    assert [c for c in h.calls if c[0] == "add"] == [["add", CG]]
    body.release_network()
    assert h.calls[-1] == ["del", CG] and body.network == "open"
    body.release_network()  # once only
    assert [c for c in h.calls if c[0] == "del"] == [["del", CG]]


def test_setup_refuses_a_creature_with_network(cgroup_root: Path) -> None:
    """Regression: a rule that does not load must stop the controller, not run it open."""
    body = body_with(cgroup_root, FakeHelper(rc=1))
    with pytest.raises(CgroupError, match="network"):
        body.setup()
    assert body.network == "open"


def test_network_allowed_or_no_helper(cgroup_root: Path) -> None:
    h = FakeHelper()
    allowed = body_with(cgroup_root, h, creature_network="allowed")
    allowed.setup()
    assert allowed.network == "open" and h.calls == []
    plain = body_with(cgroup_root, None)
    plain.setup()
    assert plain.network == "open"
    plain.release_network()


def test_settings_from_config(pi4_default) -> None:
    from epitaph.config import load_config

    pi = CgroupSettings.from_config(load_config("pi4/default", "pi4-4gb"))
    assert pi.creature_network == BLOCKED
    assert pi.netblock_helper == "/usr/local/sbin/epitaph-netblock"
    assert CgroupSettings.from_config(load_config("pi4/default", "dev")).netblock_helper == ""


# --- the selftest's network check --------------------------------------------------------------


def probe_says(out: str, lo: str = "connected") -> object:
    def run(argv: list[str]) -> str:
        assert argv[3].endswith("creature/cgroup.procs") and argv[-1] == "1.1.1.1:443"
        return json.dumps({"out": out, "lo": lo}) + "\n"

    return run


def test_network_check(cgroup_root: Path) -> None:
    body = body_with(cgroup_root, FakeHelper())
    body.setup()
    refused = "ConnectionRefusedError: Connection refused"
    c = check_network(body, run=probe_says(refused), control=lambda t: "connected")  # type: ignore[arg-type]
    assert c.ok, c.detail
    assert "creature -> 1.1.1.1:443: ConnectionRefusedError" in c.detail
    assert "outside the cgroup -> 1.1.1.1:443: connected" in c.detail
    c = check_network(body, run=probe_says("connected"), control=lambda t: "connected")  # type: ignore[arg-type]
    assert not c.ok
    c = check_network(body, run=probe_says(refused, lo="refused"), control=lambda t: "x")  # type: ignore[arg-type]
    assert not c.ok

    def broken(argv: list[str]) -> str:
        raise subprocess.CalledProcessError(1, argv)

    assert not check_network(body, run=broken).ok


def test_network_check_needs_the_rule(cgroup_root: Path) -> None:
    body = body_with(cgroup_root, FakeHelper())
    body.setup()
    body.netblock = NetBlock("helper", FakeHelper(listing=""))
    c = check_network(body, run=probe_says("x"))  # type: ignore[arg-type]
    assert not c.ok and "no nftables rule" in c.detail


def test_network_check_skips(cgroup_root: Path) -> None:
    allowed = body_with(cgroup_root, FakeHelper(), creature_network="allowed")
    allowed.setup()
    assert check_network(allowed).ok and "skipped" in check_network(allowed).detail
    plain = body_with(cgroup_root, None)
    plain.setup()
    assert check_network(plain).ok and "skipped" in check_network(plain).detail


# --- the helper script and its sudoers rule (never run as root here) ---------------------------


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["add"],
        ["add", "../../etc"],
        ["add", "system.slice/sshd.service/creature"],
        ["add", "system.slice/epitaph-controller.service/creature/../x"],
        ["add", "system.slice/epitaph-controller.service/supervisor"],
        ["del", "system.slice/Epitaph.service/creature"],
        ["add", CG, "extra"],
        ["status", "extra"],
        ["flush"],
    ],
)
def test_helper_rejects_everything_else(args: list[str]) -> None:
    out = subprocess.run(["sh", str(HELPER), *args], capture_output=True, text=True, check=False)
    assert out.returncode == 2 and "usage" in out.stderr


def test_helper_is_executable() -> None:
    assert HELPER.stat().st_mode & 0o111
    assert subprocess.run(["sh", "-n", str(HELPER)], check=False).returncode == 0


def test_sudoers_allows_only_the_helper() -> None:
    rules = [
        line
        for line in (ROOT / "deploy" / "sudoers" / "epitaph-netblock").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    assert rules == [
        "@USER@ ALL=(root) NOPASSWD: /usr/local/sbin/epitaph-netblock status, "
        "/usr/local/sbin/epitaph-netblock "
        "^(add|del) system[.]slice/epitaph[a-z0-9-]*[.]service/creature$"
    ]


@pytest.mark.skipif(not Path("/usr/sbin/visudo").exists(), reason="no visudo")
def test_sudoers_parses(tmp_path: Path) -> None:
    f = tmp_path / "rule"
    text = (ROOT / "deploy" / "sudoers" / "epitaph-netblock").read_text()
    f.write_text(text.replace("@USER@", "pi"))
    out = subprocess.run(
        ["/usr/sbin/visudo", "-cf", str(f)], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stdout + out.stderr


def test_install_manages_both_helpers() -> None:
    text = (ROOT / "deploy" / "install.sh").read_text()
    assert "HELPERS=(epitaph-clock:020 epitaph-netblock:021)" in text

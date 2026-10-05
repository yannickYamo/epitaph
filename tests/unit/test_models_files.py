"""Model file resolution, pinning and the free-space check. No network."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from epitaph.backend import models as mf

MODELS: dict[str, Any] = {
    "llama": {
        "source": "bartowski/L-GGUF",
        "file": "L-{quant}.gguf",
        "sources": {"Q2_K": "unsloth/L-GGUF"},
        "ladder": {"pi4": ["Q6_K", "Q4_K_M", "Q2_K"]},
    },
    "qwen": {"source": "b/Q-GGUF", "file": "Q-{quant}.gguf", "ladder": {"pi4": ["Q4_K_M"]}},
}
SHA = "a" * 64


def fake_hub(files: dict[str, list[str]], gated: bool = False) -> mf.Getter:
    def get(url: str) -> Any:
        repo = url.split("/api/models/")[1]
        if "/tree/" in repo:
            repo = repo.split("/tree/")[0]
            return [{"path": f, "size": 10, "lfs": {"oid": SHA, "size": 1234}} for f in files[repo]]
        return {"gated": gated, "sha": "rev1"}

    return get


def test_wanted_expands_step0_ladder_and_names() -> None:
    assert mf.wanted(MODELS, ["llama"], "step0") == [("llama", "Q6_K")]
    assert mf.wanted(MODELS, ["llama", "qwen"], "ladder,Q4_0") == [
        ("llama", "Q6_K"),
        ("llama", "Q4_K_M"),
        ("llama", "Q2_K"),
        ("llama", "Q4_0"),
        ("qwen", "Q4_K_M"),
        ("qwen", "Q4_0"),
    ]
    with pytest.raises(mf.ModelFileError):
        mf.wanted(MODELS, ["nope"], "step0")


def test_per_quant_source_override() -> None:
    assert mf.ref_for(MODELS, "llama", "Q2_K").repo == "unsloth/L-GGUF"
    assert mf.ref_for(MODELS, "llama", "Q6_K").repo == "bartowski/L-GGUF"
    assert mf.ref_for(MODELS, "llama", "Q6_K").file == "L-Q6_K.gguf"


def test_resolve_pins_sha_size_and_revision() -> None:
    ref = mf.ref_for(MODELS, "llama", "Q6_K")
    got = mf.resolve(ref, fake_hub({"bartowski/L-GGUF": ["L-Q6_K.gguf"]}))
    assert (got.sha256, got.size, got.revision) == (SHA, 1234, "rev1")
    assert got.url.endswith("/bartowski/L-GGUF/resolve/rev1/L-Q6_K.gguf")


def test_resolve_missing_file_lists_what_exists() -> None:
    ref = mf.ref_for(MODELS, "llama", "Q2_K")
    with pytest.raises(mf.ModelFileError, match=r"L-Q3_K_M\.gguf"):
        mf.resolve(ref, fake_hub({"unsloth/L-GGUF": ["L-Q3_K_M.gguf"]}))


def test_resolve_refuses_gated_repos() -> None:
    ref = mf.ref_for(MODELS, "qwen", "Q4_K_M")
    with pytest.raises(mf.ModelFileError, match="gated"):
        mf.resolve(ref, fake_hub({"b/Q-GGUF": ["Q-Q4_K_M.gguf"]}, gated=True))


def test_lock_round_trip(tmp_path: Path) -> None:
    ref = mf.FileRef("llama", "Q6_K", "r/x", "x.gguf", 5, SHA, "rev")
    p = tmp_path / "lock.toml"
    mf.write_lock({"llama": {"Q6_K": ref}}, p)
    assert mf.read_lock(p) == {"llama": {"Q6_K": ref}}
    assert mf.read_lock(tmp_path / "missing.toml") == {}


def test_space_check_refuses_with_the_space_needed() -> None:
    with pytest.raises(mf.ModelFileError, match=r"need 3\.00 GB"):
        mf.check_space(free_bytes=4 * 10**9, needed=3 * 10**9, where="disk")
    mf.check_space(free_bytes=10 * 10**9, needed=3 * 10**9, where="disk")


def test_download_skips_existing_and_rejects_unpinned(tmp_path: Path) -> None:
    data = b"gguf" * 10
    ref = mf.FileRef("m", "Q4", "r", "f.gguf", len(data), hashlib.sha256(data).hexdigest())
    dest = ref.local_path(tmp_path)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(data)
    assert mf.download(ref, tmp_path, log=lambda _: None) == dest
    assert mf.sha256_file(dest) == ref.sha256
    with pytest.raises(mf.ModelFileError, match="not pinned"):
        mf.download(mf.FileRef("m", "Q4", "r", "f.gguf"), tmp_path)


def test_paths() -> None:
    ref = mf.FileRef("llama", "Q6_K", "r", "f")
    assert ref.pi_path() == "/var/lib/epitaph/models/llama/Q6_K.gguf"
    assert ref.local_path(Path("/x")) == Path("/x/llama/Q6_K.gguf")


def test_repo_models_toml_ladders_are_all_pinned() -> None:
    """Every ladder step in config/models.toml has a pin (resolve was run after edits)."""
    models = mf.read_models_toml()["models"]
    lock = mf.read_lock()
    for name, spec in models.items():
        for q in mf.ladder(spec, "pi4"):
            assert q in lock.get(name, {}), f"{name} {q} not pinned in models.lock.toml"


def test_pick_host_prefers_the_cable_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model pushes go over the cable when it answers, else over Wi-Fi (F12)."""
    import subprocess

    up: set[str] = {"pi"}
    tried: list[str] = []

    def fake_run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[bytes]:
        tried.append(argv[-2])
        return subprocess.CompletedProcess(argv, 0 if argv[-2] in up else 255)

    monkeypatch.setattr(mf.subprocess, "run", fake_run)
    assert mf.pick_host() == "pi" and tried == ["pi-eth", "pi"]
    up.add("pi-eth")
    assert mf.pick_host() == "pi-eth"
    up.clear()
    with pytest.raises(mf.ModelFileError, match="none of pi-eth, pi"):
        mf.pick_host()

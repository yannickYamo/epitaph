"""Model files: resolve, download, pin and push GGUFs (BUILD_PLAN 9 A8).

`config/models.toml` says which repo and file name pattern each model uses; this module turns
that into concrete files, pins each file's sha256 (the Hugging Face LFS object id, which is
the sha256 of the file) in `config/models.lock.toml`, downloads with resume into
`~/epitaph-models/<model>/<quant>.gguf`, verifies the hash, and rsyncs to the Pi.

A quant can come from another community repo than the model's default through
`sources = { Q2_K = "<repo>" }` in models.toml (used when the default repo lacks it).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from epitaph.config import CONFIG_DIR

HF = "https://huggingface.co"
LOCK_PATH = CONFIG_DIR / "models.lock.toml"
LOCAL_DIR = Path(os.environ.get("EPITAPH_MODELS_DIR", "~/epitaph-models")).expanduser()
PI_DIR = "/var/lib/epitaph/models"
SPACE_MARGIN = 2 * 1024**3  # keep this much free after every download


class ModelFileError(RuntimeError):
    """A repo, file, hash or disk problem that stops a download."""


@dataclass(frozen=True)
class FileRef:
    """One concrete GGUF: which model and quant, where it lives upstream, and its pin."""

    model: str
    quant: str
    repo: str
    file: str
    size: int = 0
    sha256: str = ""
    revision: str = ""

    @property
    def url(self) -> str:
        rev = self.revision or "main"
        return f"{HF}/{self.repo}/resolve/{rev}/{self.file}"

    def local_path(self, root: Path | None = None) -> Path:
        return (root or LOCAL_DIR) / self.model / f"{self.quant}.gguf"

    def pi_path(self) -> str:
        return f"{PI_DIR}/{self.model}/{self.quant}.gguf"


def read_models_toml(path: Path | None = None) -> dict[str, Any]:
    return tomllib.loads((path or CONFIG_DIR / "models.toml").read_text())


def read_lock(path: Path | None = None) -> dict[str, dict[str, FileRef]]:
    p = path or LOCK_PATH
    if not p.exists():
        return {}
    raw = tomllib.loads(p.read_text())
    out: dict[str, dict[str, FileRef]] = {}
    for model, quants in raw.get("files", {}).items():
        for quant, rec in quants.items():
            out.setdefault(model, {})[quant] = FileRef(model=model, quant=quant, **rec)
    return out


def write_lock(lock: dict[str, dict[str, FileRef]], path: Path | None = None) -> None:
    """Write the pins as TOML, sorted, atomically."""
    p = path or LOCK_PATH
    lines = [
        "# sha256 pins for every model file we use (written by tools/download_models.py).",
        "# The sha256 is the Hugging Face LFS object id; downloads are verified against it.",
        "",
    ]
    for model in sorted(lock):
        for quant in sorted(lock[model]):
            f = lock[model][quant]
            lines += [
                f'[files."{model}".{quant}]',
                f'repo = "{f.repo}"',
                f'file = "{f.file}"',
                f"size = {f.size}",
                f'sha256 = "{f.sha256}"',
                f'revision = "{f.revision}"',
                "",
            ]
    tmp = p.with_suffix(".tmp")
    tmp.write_text("\n".join(lines))
    os.replace(tmp, p)


def ladder(spec: dict[str, Any], hw_class: str = "pi4") -> list[str]:
    lad: dict[str, list[str]] = spec.get("ladder", {})
    return list(lad.get(hw_class, lad.get("pi4", [])))


def wanted(
    models: dict[str, Any], names: Iterable[str], quants: str, hw_class: str = "pi4"
) -> list[tuple[str, str]]:
    """Expand a selection into (model, quant) pairs.

    quants: "step0" (the first ladder step), "ladder" (every step), or a comma list of quant
    names ("Q4_0,Q4_K_M"), which may mix in "step0" and "ladder".
    """
    out: list[tuple[str, str]] = []
    for name in names:
        if name not in models:
            raise ModelFileError(f"{name!r} is not in config/models.toml")
        lad = ladder(models[name], hw_class)
        for q in quants.split(","):
            q = q.strip()
            picks = lad[:1] if q == "step0" else lad if q == "ladder" else [q]
            for p in picks:
                if (name, p) not in out:
                    out.append((name, p))
    return out


def ref_for(models: dict[str, Any], model: str, quant: str) -> FileRef:
    spec = models[model]
    repo = str(dict(spec.get("sources", {})).get(quant, spec["source"]))
    pattern = str(dict(spec.get("files", {})).get(quant, spec["file"]))
    return FileRef(model=model, quant=quant, repo=repo, file=pattern.format(quant=quant))


Getter = Callable[[str], Any]


def _http_json(url: str) -> Any:
    r = httpx.get(url, timeout=30, follow_redirects=True)
    if r.status_code == 404:
        raise ModelFileError(f"not found: {url}")
    r.raise_for_status()
    return r.json()


def resolve(ref: FileRef, get: Getter = _http_json) -> FileRef:
    """Look the file up on the Hub: it must exist; returns it with size, sha256, revision."""
    info = get(f"{HF}/api/models/{ref.repo}")
    if info.get("gated"):
        raise ModelFileError(f"{ref.repo} is gated; use a public community repo")
    rev = str(info.get("sha", ""))
    tree = get(f"{HF}/api/models/{ref.repo}/tree/{rev or 'main'}")
    for entry in tree:
        if entry.get("path") == ref.file:
            lfs = entry.get("lfs") or {}
            sha = str(lfs.get("oid", ""))
            if len(sha) != 64:
                raise ModelFileError(f"{ref.repo}/{ref.file} has no LFS sha256")
            return FileRef(
                model=ref.model,
                quant=ref.quant,
                repo=ref.repo,
                file=ref.file,
                size=int(lfs.get("size", entry.get("size", 0))),
                sha256=sha,
                revision=rev,
            )
    names = sorted(str(e.get("path")) for e in tree if str(e.get("path", "")).endswith(".gguf"))
    raise ModelFileError(f"{ref.repo} has no {ref.file}; it has: {', '.join(names)}")


def check_space(free_bytes: int, needed: int, where: str) -> None:
    if free_bytes < needed + SPACE_MARGIN:
        raise ModelFileError(
            f"not enough space on {where}: need {needed / 1e9:.2f} GB plus "
            f"{SPACE_MARGIN / 1e9:.1f} GB margin, have {free_bytes / 1e9:.2f} GB"
        )


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def download(ref: FileRef, root: Path | None = None, log: Callable[[str], None] = print) -> Path:
    """Download with resume to <root>/<model>/<quant>.gguf and verify the pinned sha256."""
    if not ref.sha256:
        raise ModelFileError(f"{ref.model} {ref.quant} is not pinned; run resolve first")
    dest = ref.local_path(root)
    if dest.exists() and dest.stat().st_size == ref.size:
        log(f"have {dest}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".gguf.part")
    have = part.stat().st_size if part.exists() else 0
    check_space(shutil.disk_usage(dest.parent).free, ref.size - have, str(dest.parent))
    headers = {"Range": f"bytes={have}-"} if have else {}
    t0, got = time.monotonic(), 0
    with (
        httpx.stream("GET", ref.url, headers=headers, follow_redirects=True, timeout=60) as r,
        part.open("ab" if have and r.status_code == 206 else "wb") as out,
    ):
        if r.status_code not in (200, 206):
            raise ModelFileError(f"HTTP {r.status_code} for {ref.url}")
        for block in r.iter_bytes(4 * 1024 * 1024):
            out.write(block)
            got += len(block)
    dt = max(1e-3, time.monotonic() - t0)
    log(f"got {ref.model} {ref.quant}: {got / 1e6:.0f} MB in {dt:.0f}s ({got / 1e6 / dt:.1f} MB/s)")
    sha = sha256_file(part)
    if sha != ref.sha256:
        part.unlink()
        raise ModelFileError(f"sha256 mismatch for {ref.file}: {sha} != {ref.sha256}")
    os.replace(part, dest)
    return dest


def pi_free_bytes(host: str = "pi-eth") -> int:
    out = subprocess.run(
        ["ssh", host, f"mkdir -p {PI_DIR} && df -B1 --output=avail {PI_DIR} | tail -1"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return int(out.strip())


def push(ref: FileRef, host: str = "pi-eth", root: Path | None = None, verify: bool = True) -> None:
    """rsync one file to the Pi (over the cable) and check its sha256 there."""
    src = ref.local_path(root)
    if not src.exists():
        raise ModelFileError(f"{src} is not downloaded")
    remote = ref.pi_path()
    check = subprocess.run(
        ["ssh", host, f"stat -c %s {remote} 2>/dev/null || echo 0"],
        capture_output=True,
        text=True,
        check=True,
    )
    if int(check.stdout.strip() or 0) != ref.size:
        check_space(pi_free_bytes(host), ref.size, f"{host}:{PI_DIR}")
        subprocess.run(
            ["ssh", host, f"mkdir -p {PI_DIR}/{ref.model}"], check=True, capture_output=True
        )
        subprocess.run(
            ["rsync", "-a", "--partial", "--inplace", str(src), f"{host}:{remote}"], check=True
        )
    if verify:
        out = subprocess.run(
            ["ssh", host, f"sha256sum {remote}"], capture_output=True, text=True, check=True
        ).stdout.split()[0]
        if out != ref.sha256:
            raise ModelFileError(f"sha256 mismatch on the Pi for {remote}")


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Resolve, download and push model files (A8).")
    ap.add_argument("action", choices=["resolve", "fetch", "push", "list"])
    ap.add_argument("--models", default="all", help="comma list, or 'all'")
    ap.add_argument("--quants", default="step0", help="step0 | ladder | Q4_0,... (mixable)")
    ap.add_argument("--class", dest="hw_class", default="pi4")
    ap.add_argument("--host", default="pi-eth")
    ap.add_argument("--no-verify", action="store_true", help="skip the sha256 check on the Pi")
    args = ap.parse_args(argv)

    models = read_models_toml()["models"]
    names = list(models) if args.models == "all" else args.models.split(",")
    pairs = wanted(models, names, args.quants, args.hw_class)
    lock = read_lock()

    def pinned(model: str, quant: str) -> FileRef:
        ref = lock.get(model, {}).get(quant)
        if ref is None:
            ref = resolve(ref_for(models, model, quant))
            lock.setdefault(model, {})[quant] = ref
            write_lock(lock)
        return ref

    try:
        if args.action == "resolve":
            for m, q in pairs:
                lock.get(m, {}).pop(q, None)
                r = pinned(m, q)
                print(f"{m:26} {q:7} {r.size / 1e9:5.2f} GB  {r.repo}/{r.file}  {r.sha256[:12]}")
        elif args.action == "list":
            for m, q in pairs:
                r = lock.get(m, {}).get(q)
                have = r is not None and r.local_path().exists()
                print(f"{m:26} {q:7} {'pinned' if r else 'unpinned':8} {'local' if have else ''}")
        elif args.action == "fetch":
            refs = [pinned(m, q) for m, q in pairs]
            need = sum(r.size for r in refs if not r.local_path().exists())
            LOCAL_DIR.mkdir(parents=True, exist_ok=True)
            check_space(shutil.disk_usage(LOCAL_DIR).free, need, str(LOCAL_DIR))
            for r in refs:
                download(r)
        else:
            refs = [pinned(m, q) for m, q in pairs]
            for r in refs:
                t0 = time.monotonic()
                push(r, args.host, verify=not args.no_verify)
                print(f"pushed {r.pi_path()} in {time.monotonic() - t0:.0f}s")
    except ModelFileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

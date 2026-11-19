"""Run manifest: record exactly what a run used, and verify the dataset pin.

Everything here is local: git and file hashing, installed-package versions, and a
check of the Hugging Face cache layout. Nothing touches the network. Task records are
read straight from the verified snapshot's parquet file (``read_verified_split``), never
through ``datasets``/``huggingface_hub``, so a missing or wrong snapshot fails with a
``ManifestError`` instead of falling back to a download. (``offline_env`` remains for the
SWE-bench harness, which does use ``datasets``.)
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

from .gate import REPO_ROOT, sha256_canonical_json, sha256_file

SCHEMA = 1
_VERSIONED = ("swebench", "docker", "datasets", "huggingface_hub", "pyarrow", "pytest", "packaging")  # pyarrow reads the dataset
_HASHED_GLOBS = ("src/aiep/*.py", "config/*.json", "scripts/*", "requirements/*")


class ManifestError(RuntimeError):
    pass


_REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


def load_dataset_config(repo_root: Path = REPO_ROOT) -> dict:
    path = Path(repo_root) / "config" / "dataset.json"
    try:
        cfg = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise ManifestError(f"cannot read {path}: {type(e).__name__}") from e
    if not isinstance(cfg, dict):
        raise ManifestError(f"{path}: must be a JSON object")
    rev = cfg.get("revision", "")
    if not isinstance(rev, str) or len(rev) != 40 or any(c not in "0123456789abcdef" for c in rev):
        raise ManifestError("config/dataset.json: revision must be a 40-character git commit hash")
    repo_id = cfg.get("repo_id")
    if not isinstance(repo_id, str) or not _REPO_ID_RE.fullmatch(repo_id) or ".." in repo_id or "--" in repo_id:
        # "--" is how the cache joins owner and name, so "a--b/c" and "a/b--c" would share one cache directory
        raise ManifestError("config/dataset.json: repo_id must be <owner>/<name> with plain characters and no '--'")
    return cfg


def offline_env() -> dict[str, str]:
    return {"HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"}


def verify_dataset_snapshot(hf_home: Path, cfg: dict) -> Path:
    """Check the cache holds the pinned revision and that ``refs/main`` points at it.

    The harness calls ``load_dataset(name, split)`` without a revision, so offline it
    resolves ``refs/main``. Pinning therefore means: refs/main must equal the pinned
    revision and that snapshot directory must exist.
    """
    repo_dir = Path(hf_home) / "hub" / ("datasets--" + cfg["repo_id"].replace("/", "--"))
    refs = repo_dir / "refs" / "main"
    if not refs.is_file():
        raise ManifestError(f"dataset cache has no refs/main at {refs}")
    ref = refs.read_text().strip()
    if ref != cfg["revision"]:
        raise ManifestError(f"dataset cache refs/main is {ref}, pinned revision is {cfg['revision']}")
    snap = repo_dir / "snapshots" / cfg["revision"]
    if not snap.is_dir():
        raise ManifestError(f"pinned dataset snapshot directory is missing: {snap}")
    return snap


_SPLIT_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _locate_split_file(snapshot_dir: Path, split: str, revision: str | None) -> tuple[Path, str]:
    """Find the split's single parquet link and prove it stays inside this repository's cache. Returns (entry, blob name).

    Every directory on the way must be a real directory (compared by resolved path, so a symlinked ``snapshots/<rev>`` that
    points at another snapshot, or a symlinked ``data`` directory, is refused), and the entry must link to a
    sha256-named file directly inside this repository's ``blobs`` directory (compared by resolved path, so ``..`` through a
    symlink cannot escape). ``revision``, if given, must be the snapshot directory's own name.
    """
    if not isinstance(split, str) or not _SPLIT_RE.fullmatch(split):
        raise ManifestError(f"config/dataset.json: split {split!r} is not a plain split name")
    snap = Path(snapshot_dir)
    if revision is not None and snap.name != revision:
        raise ManifestError(f"snapshot directory {snap.name!r} is not the pinned revision {revision!r}")
    repo_dir = snap.parents[1]
    real = os.path.realpath
    if real(snap) != os.path.join(real(repo_dir), "snapshots", snap.name):
        raise ManifestError(f"{snap} is not a real directory of this repository's snapshots (it is, or lies behind, a link)")
    data_dir = snap / "data"
    if not data_dir.is_dir() or real(data_dir) != os.path.join(real(snap), "data"):
        raise ManifestError(f"{data_dir} is missing or is a link")
    matches = sorted(data_dir.glob(f"{split}-*.parquet"))
    if len(matches) != 1:
        raise ManifestError(f"expected exactly one {split!r} parquet file in {data_dir}, found {len(matches)}")
    entry = matches[0]
    if not entry.is_symlink():
        raise ManifestError(f"{entry} is not a link into the Hugging Face blob store")
    blob = entry.parent / os.readlink(entry)
    if real(blob.parent) != os.path.join(real(repo_dir), "blobs") or not _SHA256_RE.fullmatch(blob.name):
        raise ManifestError(f"{entry} does not link to a sha256-named blob of this repository")
    if not entry.is_file():
        raise ManifestError(f"{entry} is a dangling link: the blob {blob.name} is missing")
    return entry, blob.name


def verify_split_file(snapshot_dir: Path, split: str, revision: str | None = None) -> Path:
    """The one parquet file of ``split`` in the verified snapshot, checked byte for byte against its blob name.

    Purely local. The Hugging Face cache stores each LFS file under its SHA-256 and links the snapshot entry to it, so the
    file's content hash must equal the blob name it is linked to; a modified, truncated or substituted file fails here
    instead of being loaded. See ``_locate_split_file`` for the containment rules.
    """
    entry, name = _locate_split_file(snapshot_dir, split, revision)
    if sha256_file(entry) != name:
        raise ManifestError(f"{entry} does not match its blob name {name}: the local data is modified or corrupt")
    return entry


def read_verified_split(snapshot_dir: Path, split: str, revision: str | None = None) -> bytes:
    """The split's parquet bytes, read once and hashed from those same bytes, so nothing can change between check and use."""
    entry, name = _locate_split_file(snapshot_dir, split, revision)
    data = entry.read_bytes()
    if hashlib.sha256(data).hexdigest() != name:
        raise ManifestError(f"{entry} does not match its blob name {name}: the local data is modified or corrupt")
    return data


def git_state(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    def git(*args: str) -> str:
        r = subprocess.run(["git", *args], cwd=repo_root, capture_output=True, text=True)
        if r.returncode != 0:
            raise ManifestError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
        return r.stdout

    status = [l for l in git("status", "--porcelain").splitlines() if l]
    return {
        "commit": git("rev-parse", "HEAD").strip(),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD").strip(),
        "dirty": any(not l.startswith("??") for l in status),
        "untracked_count": sum(l.startswith("??") for l in status),
        "changed_files": sorted(l[3:] for l in status if not l.startswith("??")),
    }


def file_hashes(repo_root: Path = REPO_ROOT) -> dict[str, str]:
    root = Path(repo_root)
    out: dict[str, str] = {}
    for pattern in _HASHED_GLOBS:
        for p in sorted(root.glob(pattern)):
            if p.is_file():
                out[str(p.relative_to(root))] = sha256_file(p)
    return out


def versions() -> dict[str, str]:
    out = {"python": platform.python_version(), "platform": f"{sys.platform}/{platform.machine()}"}
    for name in _VERSIONED:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = "not installed"
    return out


def build_manifest(
    repo_root: Path = REPO_ROOT,
    *,
    hf_home: Path,
    run_id: str,
    instance_ids: list[str],
    image_ids: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Collect the run record. Raises ManifestError if the dataset pin does not verify."""
    root = Path(repo_root)
    cfg = load_dataset_config(root)
    snapshot = verify_dataset_snapshot(hf_home, cfg)
    wheelhouse = json.loads((root / "config" / "wheelhouse.json").read_text())
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "instances": sorted(instance_ids),
        "git": git_state(root),
        "versions": versions(),
        "dataset": {**{k: cfg[k] for k in ("repo_id", "split", "revision")}, "snapshot": str(snapshot), "verified": True},
        "host_lock_sha256": sha256_file(root / "requirements" / "host.lock"),
        # Evaluator build dependencies are tracked separately from the host lock.
        "wheelhouse": {"status": wheelhouse.get("status"), "sha256": sha256_canonical_json(root / "config" / "wheelhouse.json")},
        "image_ids": dict(image_ids or {}),
        "files": file_hashes(root),
    }


def write_manifest(manifest: dict[str, Any], run_dir: Path) -> Path:
    """Write ``manifest.json`` into a new run directory; refuse to overwrite a record."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "manifest.json"
    with open(path, "x") as f:  # 'x': a run ID is never reused
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    return path

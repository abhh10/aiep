"""Run manifest: record exactly what a run used, and verify the dataset pin.

Everything here is local: git and file hashing, installed-package versions, and a
check of the Hugging Face cache layout. Nothing touches the network;
``offline_env`` forces the Hugging Face libraries offline so a missing or wrong
snapshot fails instead of silently downloading.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

from .gate import REPO_ROOT, sha256_canonical_json, sha256_file

SCHEMA = 1
_VERSIONED = ("swebench", "docker", "datasets", "huggingface_hub", "pytest", "packaging")
_HASHED_GLOBS = ("src/aiep/*.py", "config/*.json", "scripts/*", "requirements/*")


class ManifestError(RuntimeError):
    pass


def load_dataset_config(repo_root: Path = REPO_ROOT) -> dict:
    cfg = json.loads((Path(repo_root) / "config" / "dataset.json").read_text())
    rev = cfg.get("revision", "")
    if len(rev) != 40 or any(c not in "0123456789abcdef" for c in rev):
        raise ManifestError("config/dataset.json: revision must be a 40-character git commit hash")
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

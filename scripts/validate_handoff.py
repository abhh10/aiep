#!/usr/bin/env python3
"""Validate an M2 run directory as an evaluator handoff (never runs the evaluator, never touches Docker or the network).

    PYTHONPATH=src .venv/bin/python scripts/validate_handoff.py runs/m2/<run_id> [--out report.json] [--no-hidden-scan] [--no-apply-check]

Exit codes: 0 ready; 3 ready_flagged (gradable, but not a clean measurement); 1 rejected; 2 usage.
The report holds only counts for hidden task data and says explicitly that nothing was graded.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import os  # noqa: E402
import subprocess  # noqa: E402
import tarfile  # noqa: E402
import tempfile  # noqa: E402

from aiep.handoff import READY, READY_FLAGGED, Providers, validate_handoff  # noqa: E402


def seed_git(pin: dict, patch: bytes) -> dict:
    """Git on the PINNED, hash-verified seed (extracted into a throwaway directory): HEAD, HEAD^, and `git apply --check` of the patch. Never an agent's repository."""
    from aiep.seed import load_seed
    seed = load_seed(ROOT / pin["directory"], expected_sha256=pin["sha256"])  # verifies the archive against the pin
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
           "GIT_OPTIONAL_LOCKS": "0"}
    with tempfile.TemporaryDirectory(prefix="aiep-handoff-") as tmp:
        tar_path, tree = Path(tmp) / "seed.tar", Path(tmp) / "tree"
        tar_path.write_bytes(seed.tar)
        tree.mkdir()
        with tarfile.open(tar_path) as tf:
            tf.extractall(tree, filter="data")

        def git(*a: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
            return subprocess.run(["git", "-c", "core.fileMode=false", *a], cwd=tree, env=env, input=stdin, capture_output=True, timeout=120)
        head, parent = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^")
        ap = git("apply", "--check", "-", stdin=patch)
        return {"head": head.stdout.decode().strip(), "parent": parent.stdout.decode().strip(), "applies": ap.returncode == 0, "why": ap.stderr.decode(errors="replace")[:200]}


def seed_lineage(seed) -> dict:
    """Git on an EXTRACTED, parsed seed (throwaway directory): its HEAD and HEAD^. Host-side git stays in this script layer. Used before a seed is pinned."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
           "GIT_OPTIONAL_LOCKS": "0"}
    with tempfile.TemporaryDirectory(prefix="aiep-lineage-") as tmp:
        tar_path, tree = Path(tmp) / "seed.tar", Path(tmp) / "tree"
        tar_path.write_bytes(seed.tar)
        tree.mkdir()
        with tarfile.open(tar_path) as tf:
            tf.extractall(tree, filter="data")

        def git(*a: str) -> str:
            r = subprocess.run(["git", "-c", "core.fileMode=false", *a], cwd=tree, env=env, capture_output=True, timeout=120)
            return r.stdout.decode().strip() if r.returncode == 0 else ""
        return {"head": git("rev-parse", "HEAD"), "parent": git("rev-parse", "HEAD^")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--out", type=Path, default=None, help="write the JSON report here (default: runs/handoff/<run dir name>.handoff.json)")
    ap.add_argument("--no-hidden-scan", action="store_true")
    ap.add_argument("--no-apply-check", action="store_true")
    args = ap.parse_args(argv)
    if not args.run_dir.is_dir():
        print(f"not a directory: {args.run_dir}", file=sys.stderr)
        return 2
    report = validate_handoff(args.run_dir, root=ROOT, providers=Providers(seed_git=seed_git), hidden_scan=not args.no_hidden_scan,
                              apply_check=not args.no_apply_check)
    out = args.out or ROOT / "runs" / "handoff" / f"{args.run_dir.resolve().name}.handoff.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    for c in report["checks"]:
        mark = "PASS" if c["ok"] else ("FLAG" if c["severity"] == "flag" else "FAIL")
        print(f"[{mark}] {c['check']}" + ("" if c["ok"] and not c["detail"] else f" :: {json.dumps(c['detail'], default=str)[:240]}"))
    print(f"\nstatus: {report['status']}   rejections: {len(report['rejections'])}   flags: {len(report['flags'])}   report: {out}")
    print(f"grading: {report['grading']['note']}")
    return 0 if report["status"] == READY else 3 if report["status"] == READY_FLAGGED else 1


if __name__ == "__main__":
    sys.exit(main())

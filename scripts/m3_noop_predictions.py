#!/usr/bin/env python3
"""Write the deterministic no-op control predictions file for one task. Pure file generation: no Docker, no network, no evaluator, no git.

    PYTHONPATH=src .venv/bin/python scripts/m3_noop_predictions.py <instance_id> --out /ABSOLUTE/path/noop.jsonl

The patch adds one new text file (AIEP_NOOP_CONTROL.txt), applies to any repository and changes no behaviour; its bytes are registered (``aiep.grading.NOOP_PATCH_SHA256``).
Refuses to overwrite. Running the evaluator on the file is a separate, approval-gated step (``scripts/m3_grade.py <id> <run_id> <file> --kind noop``).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiep.grading import NOOP_PATCH_SHA256, noop_prediction, write_noop_predictions  # noqa: E402

_ID = re.compile(r"^[A-Za-z0-9_.\-]+__[A-Za-z0-9_.\-]+-\d+$")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("instance_id")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    if not _ID.fullmatch(args.instance_id):
        print("REFUSED: not an instance id", file=sys.stderr)
        return 2
    if not args.out.is_absolute():
        print("REFUSED: --out must be an absolute path", file=sys.stderr)
        return 2
    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        write_noop_predictions(args.out, args.instance_id)
    except FileExistsError:
        print(f"REFUSED: {args.out} exists; a generated file is never overwritten", file=sys.stderr)
        return 2
    patch = noop_prediction(args.instance_id)["model_patch"]
    assert hashlib.sha256(patch.encode()).hexdigest() == NOOP_PATCH_SHA256
    print(json.dumps({"path": str(args.out), "instance_id": args.instance_id, "predictions_sha256": hashlib.sha256(args.out.read_bytes()).hexdigest(), "patch_sha256": NOOP_PATCH_SHA256}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

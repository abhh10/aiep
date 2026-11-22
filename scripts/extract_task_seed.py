#!/usr/bin/env python3
"""Extract a task's pristine /testbed seed from its pinned local image and record the seed pin (approval-gated Docker step; generalizes extract_pytest5227_seed.py).

    PYTHONPATH=src .venv/bin/python scripts/extract_task_seed.py <instance_id>
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from aiep.seed import SeedError  # noqa: E402
from aiep.seed_source import SeedSourceError  # noqa: E402
from aiep.task_pins import PinError  # noqa: E402
from aiep.task_setup import SetupRefused, extract_and_pin_seed  # noqa: E402


def main(argv: list[str] | None = None, *, client_factory=None, free_bytes=None, root: Path = ROOT, base_commit_of=None, lineage=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: extract_task_seed.py <instance_id>", file=sys.stderr)
        return 2
    free = free_bytes if free_bytes is not None else shutil.disk_usage(root).free
    if free < 16 * 1024**3:
        print(f"REFUSED: {free / 1024**3:.2f} GiB free, 16 GiB required: no Docker API call was made", file=sys.stderr)
        return 2
    if base_commit_of is None:
        from aiep.task_input import load_record
        base_commit_of = lambda i: load_record(i, root / ".cache" / "hf", root)["base_commit"]  # noqa: E731  (the dataset record, never the image)
    if lineage is None:
        from validate_handoff import seed_lineage as lineage
    try:
        base_commit = base_commit_of(args[0])  # dataset only: resolved before any Docker call
    except Exception as e:
        print(f"ERROR: cannot read the dataset base_commit for {args[0]} ({type(e).__name__}); no Docker API call was made", file=sys.stderr)
        return 2
    import docker
    client = (client_factory or (lambda: docker.from_env(timeout=120)))()
    try:
        print(json.dumps(extract_and_pin_seed(client, root, args[0], free_bytes=free, base_commit=base_commit, lineage=lineage), indent=2))
        return 0
    except (SetupRefused, PinError, SeedSourceError, SeedError, FileExistsError) as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Observe the agent account in a task's pinned image and record config/task_identities/<instance>.json (approval-gated Docker step).

    PYTHONPATH=src .venv/bin/python scripts/verify_task_identity.py <instance_id>
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiep.container_policy import PolicyViolation  # noqa: E402
from aiep.task_pins import PinError  # noqa: E402
from aiep.task_setup import SetupRefused, verify_and_pin_identity  # noqa: E402


def main(argv: list[str] | None = None, *, client_factory=None, free_bytes=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: verify_task_identity.py <instance_id>", file=sys.stderr)
        return 2
    free = free_bytes if free_bytes is not None else shutil.disk_usage(ROOT).free
    if free < 16 * 1024**3:
        print(f"REFUSED: {free / 1024**3:.2f} GiB free, 16 GiB required: no Docker API call was made", file=sys.stderr)
        return 2
    import docker
    client = (client_factory or (lambda: docker.from_env(timeout=120)))()
    try:
        print(json.dumps(verify_and_pin_identity(client, ROOT, args[0], free_bytes=free), indent=2))
        return 0
    except (SetupRefused, PinError, PolicyViolation) as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())

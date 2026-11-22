#!/usr/bin/env python3
"""Record the immutable ID of a task's LOCAL image into config/evaluator_images.json (read-only inspect, never a pull). Approval-gated Docker step.

    PYTHONPATH=src .venv/bin/python scripts/pin_task_image.py <instance_id>
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiep.task_pins import PinError  # noqa: E402
from aiep.task_setup import SetupRefused, pin_image  # noqa: E402


def main(argv: list[str] | None = None, *, client_factory=None, free_bytes=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: pin_task_image.py <instance_id>", file=sys.stderr)
        return 2
    free = free_bytes if free_bytes is not None else shutil.disk_usage(ROOT).free
    if free < 16 * 1024**3:
        print(f"REFUSED: {free / 1024**3:.2f} GiB free, 16 GiB required: no Docker API call was made", file=sys.stderr)
        return 2
    import docker
    client = (client_factory or (lambda: docker.from_env(timeout=120)))()
    try:
        print(pin_image(client, ROOT, args[0], free_bytes=free))
        return 0
    except (SetupRefused, PinError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())

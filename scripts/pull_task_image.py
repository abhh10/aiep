#!/usr/bin/env python3
"""Pull ONE task's published SWE-bench image. THIS IS A DOWNLOAD (about 1 GB compressed, about 3.8 GB on disk for pytest-5227; other tasks unmeasured).
It refuses to run without --i-approve-this-download and below the 16 GiB start gate. It pulls only the exact name task_screening.image_name derives from the instance id.

    PYTHONPATH=src .venv/bin/python scripts/pull_task_image.py <instance_id> --i-approve-this-download
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiep.task_screening import image_name  # noqa: E402


def main(argv: list[str] | None = None, *, client_factory=None, free_bytes=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[1] != "--i-approve-this-download":
        print("usage: pull_task_image.py <instance_id> --i-approve-this-download   (a download: needs explicit approval)", file=sys.stderr)
        return 2
    free = free_bytes if free_bytes is not None else shutil.disk_usage(ROOT).free
    if free < 16 * 1024**3:
        print(f"REFUSED: {free / 1024**3:.2f} GiB free, 16 GiB required before a pull", file=sys.stderr)
        return 2
    name = image_name(args[0])
    repo, _, tag = name.partition(":")
    import docker
    client = (client_factory or (lambda: docker.from_env(timeout=600)))()
    try:
        img = client.images.pull(repo, tag=tag)
        print(f"pulled {name} -> {img.id}")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())

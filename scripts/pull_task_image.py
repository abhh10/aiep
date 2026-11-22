#!/usr/bin/env python3
"""Pull ONE task's published SWE-bench image BY IMMUTABLE INDEX DIGEST. THIS IS A DOWNLOAD (about 1 GB compressed, 1.5-4 GiB of disk; other tasks unmeasured).

It refuses to run without --i-approve-this-download and below the 16 GiB start gate. It needs the task's verified record in config/image_digests.json (no record, no pull),
re-verifies that record against the registry (manifests only) before downloading any layer, pulls ``<repository>@<index digest>`` for linux/amd64 (never the mutable :latest
tag, which is never created either), and requires the resulting image ID to equal the index digest.

    PYTHONPATH=src .venv/bin/python scripts/pull_task_image.py <instance_id> --i-approve-this-download
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiep import image_digests as idg  # noqa: E402


def main(argv: list[str] | None = None, *, client_factory=None, free_bytes=None, root: Path = ROOT, fetch=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[1] != "--i-approve-this-download":
        print("usage: pull_task_image.py <instance_id> --i-approve-this-download   (a download: needs explicit approval)", file=sys.stderr)
        return 2
    free = free_bytes if free_bytes is not None else shutil.disk_usage(root).free
    if free < 16 * 1024**3:
        print(f"REFUSED: {free / 1024**3:.2f} GiB free, 16 GiB required before a pull", file=sys.stderr)
        return 2
    try:
        record = idg.load_digest_record(root, args[0])
        idg.verify_record_against_registry(record, fetch or idg.docker_hub_fetch)
    except idg.DigestError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    import docker
    client = (client_factory or (lambda: docker.from_env(timeout=600)))()
    try:
        if idg.image_present(client, record):
            print(f"already local {idg.reference(record)}: nothing pulled")
            return 0
        img = idg.pull_by_digest(client, record)
        print(f"pulled {idg.reference(record)} -> {img.id}")
        return 0
    except idg.DigestError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())

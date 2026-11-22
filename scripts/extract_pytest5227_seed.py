#!/usr/bin/env python3
"""Extract the real /testbed seed of pytest-dev__pytest-5227 from its pinned image (approval-gated Docker step).

Creates ONE never-started, network-less container from the immutable image ID, reads /testbed with get_archive,
stores the seed under .cache/seeds/pytest-dev__pytest-5227/ and removes that container. All the work is done by
``aiep.seed_source.extract_seed``; this script only adds the fixed inputs, the disk gate and client closure.

    PYTHONPATH=src .venv/bin/python scripts/extract_pytest5227_seed.py

Exit codes: 0 success; 1 extraction/cleanup/close failure; 2 refused by the disk gate (no Docker API call was made).
Constructing the Docker client is itself an API call (docker-py asks the daemon for its version), so it happens only
after the disk gate and the local checks have passed.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiep.container_policy import PolicyViolation  # noqa: E402
from aiep.seed import SeedError  # noqa: E402
from aiep.seed_source import SeedSourceError, extract_seed  # noqa: E402

INSTANCE_ID = "pytest-dev__pytest-5227"
IMAGE_ID = "sha256:c91459f86ed261ab81871b086d7ca2d9fe16abd2ddf374bf172edd5d991fd28b"
MAX_ARCHIVE_BYTES = 64 * 1024**2  # measured seed content is 17.4 MiB; 64 MiB is ~3.7x headroom and fails loudly
MIN_FREE_BYTES = 16 * 1024**3  # project disk policy: below this, no Docker resource may be created
SEED_DIR = Path(".cache") / "seeds" / INSTANCE_ID


def _default_client() -> Any:
    import docker  # imported late: importing is harmless, constructing the client is the first API call

    return docker.from_env(timeout=120)


def main(*, client_factory: Callable[[], Any] = _default_client, free_bytes: Callable[[Path], int] | None = None,
         root: Path = ROOT, run_id: str | None = None) -> int:
    free = (free_bytes or (lambda p: shutil.disk_usage(p).free))(root)
    if free < MIN_FREE_BYTES:
        print(f"REFUSED: {free / 1024**3:.2f} GiB free, {MIN_FREE_BYTES / 1024**3:.0f} GiB required. "
              "No Docker API call was made.", file=sys.stderr)
        return 2
    directory = root / SEED_DIR
    run_id = run_id or f"seedsrc-{INSTANCE_ID.split('__')[1]}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"

    client = client_factory()
    status = 1
    try:
        try:
            result = extract_seed(client, image_id=IMAGE_ID, run_id=run_id, directory=directory,
                                  max_bytes=MAX_ARCHIVE_BYTES)
        except (SeedSourceError, SeedError, PolicyViolation, FileExistsError) as e:
            print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        else:
            m = result.seed.manifest
            print(f"seed sha256:   {m.archive_sha256}")
            print(f"repository HEAD: {m.head}")
            print(f"manifest entries: {len(m.entries)}")
            print(f"stored in:     {result.tar_path.parent}")
            print(f"container {result.container_id} removed: {result.removed}")
            status = 0
    finally:
        # Closing the client is the reliable release of the SDK's streaming HTTP response (generator.close() is not).
        try:
            client.close()
        except Exception as e:
            print(f"WARNING: closing the Docker client failed: {type(e).__name__}: {e}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())

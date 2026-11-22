#!/usr/bin/env python3
"""Remove abandoned AIEP containers (approval-gated Docker step). DRY RUN unless --execute is given.

    PYTHONPATH=src .venv/bin/python scripts/reap_aiep_containers.py [--ttl-seconds N] [--lease-grace-seconds N] [--execute]

A run's host-side lease (.cache/leases/<run_id>.lease, refreshed by the live orchestrator) decides: a fresh lease protects the run's
containers however old they are; an expired lease frees them only after its grace period; a MISSING lease falls back to the
container's age (--ttl-seconds, at least the longest legitimate run plus margin); an unreadable lease protects them (fail closed).

Only containers labelled ``aiep.run_id`` with a reapable ``aiep.role`` AND named exactly as AIEP names them are considered;
see ``aiep.reaper``. Images, volumes, networks and every other container are never touched. Exit codes: 0 ok; 1 a removal failed.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiep.reaper import DEFAULT_LEASE_GRACE_S, DEFAULT_TTL_S, MIN_LEASE_GRACE_S, MIN_TTL_S, reap  # noqa: E402

LEASE_DIR = ROOT / ".cache" / "leases"


def _default_client() -> Any:
    import docker  # imported late: constructing the client is the first API call

    return docker.from_env(timeout=60)


def main(argv: list[str] | None = None, *, client_factory: Callable[[], Any] = _default_client,
         now: Callable[[], float] = time.time, lease_dir: Path = LEASE_DIR) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_S, help=f"age for containers with NO lease (at least {MIN_TTL_S})")
    p.add_argument("--lease-grace-seconds", type=int, default=DEFAULT_LEASE_GRACE_S, help=f"extra silence after a lease expires (at least {MIN_LEASE_GRACE_S})")
    p.add_argument("--execute", action="store_true", help="actually remove; without it nothing is removed")
    args = p.parse_args(argv)
    if args.ttl_seconds < MIN_TTL_S or args.lease_grace_seconds < MIN_LEASE_GRACE_S:
        print(f"REFUSED: --ttl-seconds must be at least {MIN_TTL_S} and --lease-grace-seconds at least {MIN_LEASE_GRACE_S}", file=sys.stderr)
        return 2
    client = client_factory()
    try:
        report = reap(client, lease_dir=lease_dir, ttl_s=args.ttl_seconds, lease_grace_s=args.lease_grace_seconds,
                      execute=args.execute, now=now)
    finally:
        try:
            client.close()
        except Exception as e:
            print(f"WARNING: closing the Docker client failed: {type(e).__name__}: {e}", file=sys.stderr)
    print(f"{'EXECUTE' if report.executed else 'DRY RUN'} (ttl {report.ttl_s}s, lease grace {report.lease_grace_s}s): "
          f"{len(report.items)} AIEP-labelled container(s) seen")
    for i in report.items:
        state = "REMOVED" if i.removed else ("ERROR " + i.error if i.error else i.action.upper())
        print(f"  {i.container_id[:12]} {i.name or '-'} [{i.role or '-'}] {i.state or '?'} age {int(i.age_s)}s lease {i.lease or '-'}: {state} ({i.reason})")
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())

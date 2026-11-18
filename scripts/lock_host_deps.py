#!/usr/bin/env python3
"""Write (or --check) requirements/host.lock from requirements/host.in and the installed environment.

Local only: reads installed package metadata, never touches the network.
Run with the project venv:  PYTHONPATH=src .venv/bin/python scripts/lock_host_deps.py [--check]
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiep.hostdeps import ResolutionError, parse_requirements, render_lock, resolve  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--check", action="store_true", help="fail if the lock file differs")
    args = p.parse_args(argv)
    direct_text = (ROOT / "requirements" / "host.in").read_text()
    try:
        pinned = resolve(parse_requirements(direct_text))
    except ResolutionError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    new = render_lock(pinned, direct_text)
    lock = ROOT / "requirements" / "host.lock"
    if args.check:
        if not lock.exists() or lock.read_text() != new:
            print("host.lock is out of date", file=sys.stderr)
            return 1
        return 0
    lock.write_text(new)
    print(f"wrote {lock} ({len(pinned)} packages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

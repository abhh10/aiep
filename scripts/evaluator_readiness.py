#!/usr/bin/env python3
"""Read-only report of what blocks each evaluator today. Runs nothing, downloads nothing, changes nothing.

    PYTHONPATH=src .venv/bin/python scripts/evaluator_readiness.py [--check-docker]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiep.evaluator_readiness import assess  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check-docker", action="store_true", help="also compare the local evaluator image ID with the expected one (read-only)")
    args = ap.parse_args(argv)
    image_id_of = None
    if args.check_docker:
        import docker
        client = docker.from_env()

        def image_id_of(name: str):
            try:
                return client.images.get(name).id
            except Exception:
                return None
    report = assess(ROOT, image_id_of=image_id_of)
    print(json.dumps(report, indent=2))
    return 0 if report["stock_m1"]["executable"] else 1


if __name__ == "__main__":
    sys.exit(main())

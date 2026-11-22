#!/usr/bin/env python3
"""Build, validate or print the M3 experiment definition. Offline; writes only the definition file, and only with --write.

    PYTHONPATH=src .venv/bin/python scripts/m3_experiment.py build [--write]     # derive from the pinned local dataset and seed
    PYTHONPATH=src .venv/bin/python scripts/m3_experiment.py validate            # load_experiment(--definition, default config/experiments/m3-pilot-v2.json)
    PYTHONPATH=src .venv/bin/python scripts/m3_experiment.py plan                # the run schedule and what each step needs
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiep.experiment_build import DEFAULT_ID, DEFAULT_SEED, build_definition, load_records  # noqa: E402
from aiep.experiment_def import DEFAULT_DEFINITION, ExperimentError, load_experiment  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["build", "validate", "plan"])
    ap.add_argument("--seed", default=DEFAULT_SEED)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--experiment-id", default=DEFAULT_ID, help="the id of the definition to build (written to config/experiments/<id>.json)")
    ap.add_argument("--definition", type=Path, default=None, help="validate/plan this file (default: the current definition)")
    args = ap.parse_args(argv)
    target = args.definition or ROOT / DEFAULT_DEFINITION
    if args.command == "build":
        doc = build_definition(load_records(ROOT), ROOT, seed=args.seed, experiment_id=args.experiment_id)
        target = ROOT / "config" / "experiments" / f"{args.experiment_id}.json"
        text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
        if args.write:
            if target.exists():
                print(f"refusing to overwrite {target}: a pre-registered definition is never replaced", file=sys.stderr)
                return 2
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            print(f"wrote {target}  definition_sha256={doc['definition_sha256']}")
        else:
            print(text)
        return 0
    try:
        doc = load_experiment(target, root=ROOT)
    except ExperimentError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1
    if args.command == "validate":
        print(f"valid: {doc['experiment_id']} status={doc['status']} sha256={doc['definition_sha256']}")
        return 0
    for r in doc["design"]["schedule"]:
        print(f"{r['order_index']:>2}  {r['task_id']:<40} arm {r['arm']}  (block position {r['arm_position']})  {r['slot_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

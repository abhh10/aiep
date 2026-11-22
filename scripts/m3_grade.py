#!/usr/bin/env python3
"""Grade one prediction of a PINNED task with the authoritative stock evaluator (generalized scripts/m1_eval.sh). Approval-gated: this runs the evaluator.

    PYTHONPATH=src .venv/bin/python scripts/m3_grade.py <instance_id> <run_id> <gold|noop|ABSOLUTE predictions.jsonl> [--kind gold|noop|scored]

`noop` first writes the deterministic no-op control predictions (see scripts/m3_noop_predictions.py) to runs/m3/<run_id>.noop.jsonl and grades them as a noop control.

Gates (unchanged): 16 GiB free to start, hard stop below 12 GiB, evaluator image ID must equal its pin. Hugging Face is forced offline; the evaluation container's own
`pip install -e .` is the only network use. Exit codes: 0 graded (the verdict is in meta.json, NOT the exit code), 1 grading invalid, 2 refused, 3 image policy violation (the evaluator tried to pull, build or load an image).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiep.grading import GradingRefused, ImagePolicyViolation, grade, write_noop_predictions  # noqa: E402
from aiep.task_pins import evaluator_image_pin  # noqa: E402


def real_image_id(name: str):
    import docker
    c = docker.from_env(timeout=120)
    try:
        return c.images.get(name).id
    except Exception:
        return None
    finally:
        c.close()


def real_kill(run_id: str) -> None:
    subprocess.run(["sh", "-c", f'for c in $(docker ps -q --filter "name=sweb.eval" --filter "name={run_id}"); do docker kill "$c" >/dev/null 2>&1; done'], check=False)


def run_process(cmd, cwd, env) -> int:
    with open(Path(cwd) / "stdout.log", "w") as out, open(Path(cwd) / "stderr.log", "w") as err:
        return subprocess.run(list(cmd), cwd=cwd, env=dict(env), stdout=out, stderr=err).returncode


def main(argv: list[str] | None = None, *, root: Path = ROOT, grade_fn=None, pin_fn=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("instance_id")
    ap.add_argument("run_id")
    ap.add_argument("predictions")
    ap.add_argument("--kind", choices=["gold", "noop", "scored"], default=None)
    args = ap.parse_args(argv)
    kind = args.kind or ("noop" if args.predictions == "noop" else "scored")
    if args.predictions == "noop" and kind != "noop":
        print("REFUSED: the no-op predictions can only be graded as --kind noop", file=sys.stderr)
        return 2
    pin = (pin_fn or evaluator_image_pin)(root, args.instance_id)
    if not pin:
        print(f"REFUSED: {args.instance_id} has no evaluator image pin", file=sys.stderr)
        return 2
    predictions = args.predictions
    try:
        if predictions == "noop":
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}", args.run_id):
                print("REFUSED: run id must be a plain identifier", file=sys.stderr)
                return 2
            target = Path(root) / "runs" / "m3" / f"{args.run_id}.noop.jsonl"
            target.parent.mkdir(parents=True, exist_ok=True)
            predictions = str(write_noop_predictions(target, args.instance_id))
        out = (grade_fn or grade)(args.instance_id, predictions, args.run_id, kind=kind, root=root, expected_image=pin,
                    free_gib=lambda: shutil.disk_usage(root).free / 1024**3, image_id_of=real_image_id, run_process=run_process, kill_containers=real_kill)
    except ImagePolicyViolation as e:
        print(f"IMAGE POLICY VIOLATION: {e}", file=sys.stderr)
        return 3
    except (GradingRefused, FileExistsError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out.summary, indent=2))
    return 0 if out.summary["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())

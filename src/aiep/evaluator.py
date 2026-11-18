"""Fail-closed wrapper around the stock SWE-bench evaluation harness.

Successor to ``stock_eval.py`` (left untouched so the M1 record stays valid).
Differences, each closing an M1 gap:

* the gate (``gate.py``) must open before anything runs: no attestation, no
  grading, for gold controls too;
* container kwargs go through the allowlist in ``container_policy`` (unknown
  arguments are errors) and the created container is re-checked with
  ``docker inspect``;
* evaluation containers get no network;
* ``images.pull`` / ``build`` / ``load`` / ``push`` are refused, and every image
  is checked against the expected image ID before use;
* a run that intercepted no container creation, or whose harness report shows
  errors, infrastructure failures, empty patches or missing results, exits
  non-zero (an *unresolved* verdict is a valid grading result and exits 0).

Exit codes: 0 graded with no evaluation problems; 1 evaluation problems;
2 refused by the gate or the policy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from .container_policy import PolicyViolation, harden, verify_inspect
from .gate import REPO_ROOT, GateError, key_path_from_env, require_gate

EXIT_OK, EXIT_EVAL_PROBLEM, EXIT_REFUSED = 0, 1, 2


def load_expected_images(repo_root: Path = REPO_ROOT) -> dict[str, str]:
    """image reference -> expected image ID, from config/evaluator_images.json."""
    data = json.loads((Path(repo_root) / "config" / "evaluator_images.json").read_text())
    return {v["image"]: v["image_id"] for v in data["images"].values()}


class Guards:
    """Process-wide guards on the Docker SDK; ``install`` / ``uninstall`` are symmetric."""

    def __init__(self, expected_images: Mapping[str, str], log_path: Path):
        self.expected = dict(expected_images)
        self.log_path = Path(log_path)
        self.creates = 0
        self.verified: dict[str, str] = {}
        self.blocked: list[str] = []
        self._saved: list[tuple[type, str, Any]] = []

    # -- wrapped Docker SDK calls ------------------------------------------------
    def _wrap_get(self, original: Callable) -> Callable:
        guards = self

        def get(self_, name, *args, **kwargs):
            image = original(self_, name, *args, **kwargs)
            expected = guards.expected.get(name)
            if expected is None:
                raise PolicyViolation(f"image {name!r} is not in config/evaluator_images.json")
            if image.id != expected:
                raise PolicyViolation(f"image {name!r} has ID {image.id}, expected {expected}")
            guards.verified[name] = image.id
            return image

        return get

    def _wrap_create(self, original: Callable) -> Callable:
        guards = self

        def create(self_, image, command=None, **kwargs):
            if image not in guards.verified:
                raise PolicyViolation(f"image {image!r} was not verified against its expected ID first")
            hardened = harden("evaluator", {"image": image, "command": command, **kwargs})
            with open(guards.log_path, "a") as f:
                f.write(json.dumps(hardened, sort_keys=True) + "\n")
            image_arg, command_arg = hardened.pop("image"), hardened.pop("command")
            container = original(self_, image_arg, command_arg, **hardened)
            try:
                info = self_.client.api.inspect_container(container.id)
                verify_inspect("evaluator", info)
                if info.get("Image") != guards.verified[image]:
                    raise PolicyViolation("created container is not from the verified image")
            except Exception:
                try:
                    container.remove(force=True)
                finally:
                    guards.blocked.append(f"container from {image} removed after failed inspection")
                raise
            guards.creates += 1
            return container

        return create

    def _refuse(self, label: str) -> Callable:
        guards = self

        def refuse(self_, *args, **kwargs):
            guards.blocked.append(label)
            raise PolicyViolation(f"{label} is not allowed in the evaluator process")

        return refuse

    # -- install / uninstall -------------------------------------------------------
    def install(self) -> None:
        from docker.models.containers import ContainerCollection
        from docker.models.images import ImageCollection

        targets: list[tuple[type, str, Callable]] = [
            (ContainerCollection, "create", self._wrap_create(ContainerCollection.create)),
            (ImageCollection, "get", self._wrap_get(ImageCollection.get)),
        ]
        for name in ("pull", "build", "load", "push"):
            targets.append((ImageCollection, name, self._refuse(f"images.{name}")))
        for cls, attr, replacement in targets:
            self._saved.append((cls, attr, cls.__dict__[attr]))
            setattr(cls, attr, replacement)

    def uninstall(self) -> None:
        while self._saved:
            cls, attr, original = self._saved.pop()
            setattr(cls, attr, original)


def evaluation_problems(report: Mapping[str, Any]) -> list[str]:
    """Reasons a harness report is not a clean grading. Unresolved instances are not problems."""
    problems = []
    for key in ("error_instances", "infra_failure_instances", "empty_patch_instances"):
        if report.get(key, 0):
            problems.append(f"{key}={report[key]}")
    submitted, completed = report.get("submitted_instances"), report.get("completed_instances")
    if not submitted:
        problems.append("no instances were submitted")
    elif completed != submitted:
        problems.append(f"completed_instances={completed} != submitted_instances={submitted}")
    return problems


def _find_report(result: Any, report_dir: Path, run_id: str) -> Path | None:
    if isinstance(result, (str, Path)) and Path(result).exists():
        return Path(result)
    found = sorted(Path(report_dir).glob(f"*.{run_id}.json"))
    return found[0] if len(found) == 1 else None


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", default="SWE-bench/SWE-bench_Lite")
    p.add_argument("--split", default="test")
    p.add_argument("-i", "--instance", action="append", required=True)
    p.add_argument("-p", "--predictions", required=True, help="path to .jsonl, or 'gold'")
    p.add_argument("--run-id", required=True)
    p.add_argument("-t", "--timeout", type=int, default=900)
    p.add_argument("--report-dir", default="reports")
    p.add_argument("--attestation", required=True, help="evaluator attestation JSON (see gate.py)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None, *, run_harness: Callable[..., Any] | None = None,
         repo_root: Path = REPO_ROOT) -> int:
    args = parse_args(argv)
    # 1. The gate first. Nothing below runs, imports the harness or touches Docker without it.
    try:
        require_gate(Path(args.attestation), key_path_from_env(), repo_root)
        expected = load_expected_images(repo_root)
        known = json.loads((Path(repo_root) / "config" / "evaluator_images.json").read_text())["images"]
        unknown = [i for i in args.instance if i not in known]
        if unknown:
            raise PolicyViolation(f"instances without an expected image: {unknown}")
    except (GateError, PolicyViolation) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return EXIT_REFUSED

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    guards = Guards(expected, Path("create_kwargs.jsonl"))
    guards.install()
    try:
        if run_harness is None:
            from swebench.harness.run_evaluation import main as run_harness  # type: ignore[no-redef]
        result = run_harness(
            dataset_name=args.dataset,
            split=args.split,
            instance_ids=args.instance,
            predictions_path=args.predictions,
            max_workers=1,  # sequential only (8 GB host)
            open_file_limit=4096,
            run_id=args.run_id,
            timeout=args.timeout,
            rewrite_reports=False,
            modal=False,
            report_dir=args.report_dir,
            task_repo=None,
        )
    except PolicyViolation as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return EXIT_REFUSED
    finally:
        guards.uninstall()

    problems: list[str] = []
    if guards.creates == 0:
        problems.append("no container creation was intercepted (the harness no longer uses the guarded path?)")
    if guards.blocked:
        problems.append(f"blocked operations: {guards.blocked}")
    report_path = _find_report(result, Path(args.report_dir), args.run_id)
    if report_path is None:
        problems.append("harness report not found")
    else:
        problems += evaluation_problems(json.loads(report_path.read_text()))
    for p in problems:
        print(f"EVALUATION PROBLEM: {p}", file=sys.stderr)
    return EXIT_EVAL_PROBLEM if problems else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

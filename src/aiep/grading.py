"""Generalized grading through the authoritative stock evaluator (``aiep.stock_eval``), any pinned task, with the project's disk gates.

This is ``scripts/m1_eval.sh`` generalized: the same stock harness wrapper, the same reduced-privilege container, the same 16 GiB start gate and 12 GiB hard
stop (the constants are asserted equal to the shell script's by a test, so they cannot drift), the same per-run record (``meta.txt``, disk samples, container
inspect, create kwargs). What changes is that the instance and its image come from the task's PIN, not from constants in a shell script, the image ID is verified
before anything runs, and the result is parsed from the harness's per-instance report (``tests_status`` and exit codes), never from its advisory
``failure_reasons`` (whose ``no_tests_collected`` label is a known false positive).

Everything with side effects is injected (disk probe, image probe, process runner, container killer), so the module is tested without Docker. Nothing here runs unless
a caller invokes ``grade``; the batch does so only after the preflight and only with approval.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .stock_eval import EXIT_IMAGE_POLICY, POLICY_MARKER

START_GATE_GIB = 16.0
HARD_STOP_GIB = 12.0
EXPECTED_ENV = {"HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"}  # the evaluator never talks to the hub; pip inside the container is the only network use
NOOP_FILE = "AIEP_NOOP_CONTROL.txt"
NOOP_CONTENT = "AIEP no-op control: this file changes nothing."
_NOOP_BLOB = hashlib.sha1(b"blob %d\0" % (len(NOOP_CONTENT) + 1) + (NOOP_CONTENT + "\n").encode()).hexdigest()  # git's blob id of the added file, so the index line is truthful


class GradingRefused(RuntimeError):
    """A precondition failed (disk gate, image ID, run id reuse): nothing was started."""


class ImagePolicyViolation(RuntimeError):
    """The evaluator process tried to pull, build or load an image (``aiep.stock_eval``'s image guard fired). The grading is void and nothing may be concluded from it."""


@dataclass
class GradingOutcome:
    run_id: str
    instance_id: str
    kind: str  # gold | noop | scored
    exit_code: int | None
    hard_stopped: bool
    free_gib_before: float
    free_gib_min: float
    harness_report: dict[str, Any] = field(default_factory=dict)
    instance_report: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    run_dir: str = ""


NOOP_PATCH_SHA256 = "7b3a3ad824f3341cfa3ba35d4a4f249a967b4a4edb3e7489e5c840c36962a3de"  # the registered bytes of the control patch (tests pin it)


def noop_prediction(instance_id: str, model_name: str = "aiep-noop-control") -> dict[str, str]:
    """A control prediction that applies to any repository and changes no behaviour: it adds one new text file."""
    patch = (f"diff --git a/{NOOP_FILE} b/{NOOP_FILE}\nnew file mode 100644\nindex 0000000..{_NOOP_BLOB[:7]}\n--- /dev/null\n+++ b/{NOOP_FILE}\n@@ -0,0 +1 @@\n"
             f"+{NOOP_CONTENT}\n")
    return {"instance_id": instance_id, "model_name_or_path": model_name, "model_patch": patch}


def write_noop_predictions(path: Path, instance_id: str) -> Path:
    from .patch import validate_patch
    pred = noop_prediction(instance_id)
    if validate_patch(pred["model_patch"].encode()).status != "ok":
        raise ValueError("the no-op control patch does not pass the patch validator")
    path = Path(path)
    with open(path, "x") as f:
        f.write(json.dumps(pred) + "\n")
    return path


def command_for(instance_id: str, predictions: str, run_id: str, *, repo: Path, timeout_s: int = 900) -> list[str]:
    return [sys.executable, "-m", "aiep.stock_eval", "-i", instance_id, "-p", predictions, "--run-id", run_id, "-t", str(timeout_s), "--report-dir", "reports"]


def parse_instance_report(report: Mapping[str, Any], instance_id: str) -> dict[str, Any]:
    """Counts from the harness's per-instance report (``tests_status``), never from its advisory failure labels."""
    d = report.get(instance_id) or {}
    ts = d.get("tests_status") or {}
    f2p, p2p = ts.get("FAIL_TO_PASS") or {}, ts.get("PASS_TO_PASS") or {}
    f_s, f_f = len(f2p.get("success") or []) if isinstance(f2p.get("success"), list) else f2p.get("success"), len(f2p.get("failure") or []) if isinstance(f2p.get("failure"), list) else f2p.get("failure")
    p_s, p_f = len(p2p.get("success") or []) if isinstance(p2p.get("success"), list) else p2p.get("success"), len(p2p.get("failure") or []) if isinstance(p2p.get("failure"), list) else p2p.get("failure")
    return {"resolved": d.get("resolved"), "patch_applied": d.get("patch_successfully_applied"), "infra_failure": d.get("infra_failure"),
            "f2p_passed": f_s, "f2p_total": (f_s or 0) + (f_f or 0), "p2p_maintained": p_s, "p2p_total": (p_s or 0) + (p_f or 0)}


def summarize(run_id: str, instance_id: str, harness: Mapping[str, Any], inst: Mapping[str, Any], exit_code: int | None, hard_stopped: bool) -> dict[str, Any]:
    """The one record the results table and the control checks use. ``valid`` means the harness completed exactly one instance with no error or infrastructure failure."""
    parsed = parse_instance_report({instance_id: inst} if inst else {}, instance_id)
    errors = harness.get("error_instances", 0) or 0
    valid = (exit_code == 0 and not hard_stopped and harness.get("completed_instances") == 1 and harness.get("submitted_instances") == 1 and errors == 0
             and not harness.get("infra_failure_instances") and not harness.get("empty_patch_instances") and bool(inst) and parsed["infra_failure"] in (False, None)
             and parsed["resolved"] is not None)
    return {"run_id": run_id, "instance_id": instance_id, "valid": bool(valid), "exit_code": exit_code, "hard_stopped": hard_stopped, "errors": errors,
            "resolved": parsed["resolved"] if valid else None, "patch_applied": parsed["patch_applied"], "f2p_passed": parsed["f2p_passed"], "f2p_total": parsed["f2p_total"],
            "p2p_maintained": parsed["p2p_maintained"], "p2p_total": parsed["p2p_total"],
            "note": "classified from the per-instance tests_status and exit codes; the harness's ambiguous_failure / no_tests_collected labels are not used"}


def control_problems(kind: str, summary: Mapping[str, Any]) -> list[str]:
    """Why a control run does NOT meet the pre-registered requirement (empty list: it does)."""
    p: list[str] = []
    if not summary.get("valid"):
        return ["the grading itself was not valid (harness error, infrastructure failure, hard stop or incomplete report)"]
    if summary.get("patch_applied") is not True:
        p.append("the control patch was not applied")
    if kind == "gold":
        if summary.get("resolved") is not True:
            p.append("the gold patch did not resolve the task")
        if summary.get("f2p_total", 0) < 1 or summary.get("f2p_passed") != summary.get("f2p_total"):
            p.append("not every FAIL_TO_PASS test passed under the gold patch")
        if summary.get("p2p_maintained") != summary.get("p2p_total"):
            p.append("not every PASS_TO_PASS test was maintained under the gold patch")
    elif kind == "noop":
        if summary.get("resolved") is not False:
            p.append("the no-op patch was not graded unresolved")
        if summary.get("f2p_passed") != 0 or summary.get("f2p_total", 0) < 1:
            p.append("a FAIL_TO_PASS test passed under the no-op patch")
    else:
        p.append(f"unknown control kind {kind!r}")
    return p


def grade(instance_id: str, predictions: str, run_id: str, *, kind: str, root: Path, expected_image: Mapping[str, str],
          free_gib: Callable[[], float], image_id_of: Callable[[str], str | None], run_process: Callable[[Sequence[str], Path, Mapping[str, str]], int],
          kill_containers: Callable[[str], None], sleep: Callable[[float], None] = time.sleep, sample_interval_s: float = 3.0,
          timeout_s: int = 900, now: Callable[[], float] = time.time) -> GradingOutcome:
    """Grade one prediction ("gold" or an ABSOLUTE .jsonl path) with the stock evaluator. Raises ``GradingRefused`` before starting anything if a gate fails."""
    root = Path(root)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}", run_id):
        raise GradingRefused("run id must be a plain identifier")
    if predictions != "gold" and not (Path(predictions).is_absolute() and Path(predictions).is_file()):
        raise GradingRefused("predictions must be 'gold' or the absolute path of an existing .jsonl file")
    run_dir = root / "runs" / "m1" / run_id
    if run_dir.exists():
        raise GradingRefused(f"{run_dir} exists: a grading run id is never reused (the harness would reuse its report)")
    before = free_gib()
    if before < START_GATE_GIB:
        raise GradingRefused(f"free disk {before:.2f} GiB is below the {START_GATE_GIB:.0f} GiB start gate; nothing was started")
    got = image_id_of(expected_image["image"])
    if got != expected_image["image_id"]:
        raise GradingRefused(f"the local evaluator image is {got!r}, expected {expected_image['image_id']}; it is never pulled or rebuilt")
    run_dir.mkdir(parents=True)
    env = {**os.environ, **EXPECTED_ENV, "PYTHONPATH": str(root / "src"), "HF_HOME": str(root / ".cache" / "hf")}
    cmd = command_for(instance_id, predictions, run_id, repo=root, timeout_s=timeout_s)
    meta = {"run_id": run_id, "kind": kind, "instance": instance_id, "predictions": predictions, "image": expected_image["image"], "image_id": expected_image["image_id"],
            "started_unix": now(), "disk_before_gib": round(before, 3), "start_gate_gib": START_GATE_GIB, "hard_stop_gib": HARD_STOP_GIB, "command": " ".join(cmd), "env": EXPECTED_ENV}
    state = {"min": before, "stop": False, "hard": False}

    def sampler() -> None:
        while not state["stop"]:
            f = free_gib()
            state["min"] = min(state["min"], f)
            with open(run_dir / "disk_samples.log", "a") as fh:
                fh.write(f"{now():.0f} free_gib={f:.3f}\n")
            if f < HARD_STOP_GIB:
                state["hard"] = True
                (run_dir / "STOPPED.txt").write_text(f"HARD STOP (sampler): free {f:.3f} GiB < {HARD_STOP_GIB:.0f}\n")
                kill_containers(run_id)
                return
            sleep(sample_interval_s)
    t = threading.Thread(target=sampler, daemon=True)
    t.start()
    try:
        code = run_process(cmd, run_dir, env)
    finally:
        state["stop"] = True
        t.join(timeout=5)
    violation = (run_dir / POLICY_MARKER).read_text() if (run_dir / POLICY_MARKER).exists() else None
    if violation is not None or code == EXIT_IMAGE_POLICY:  # the evaluator tried to acquire an image: loud, never read as a model or harness failure
        meta.update(exit_code=code, hard_stopped=state["hard"], ended_unix=now(), summary={"valid": False, "image_policy_violation": True, "detail": violation or "exit code 97"})
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
        raise ImagePolicyViolation(f"{run_id}: the evaluator tried to pull, build or load an image (exit {code}): {(violation or '').strip() or 'see ' + str(run_dir)}")
    harness, inst = {}, {}
    for p in sorted((run_dir / "reports").glob("*.json")):
        harness = json.loads(p.read_text())
    for p in (run_dir / "logs" / "run_evaluation" / run_id).glob(f"*/{instance_id}/report.json") if (run_dir / "logs").exists() else []:
        inst = json.loads(p.read_text()).get(instance_id, {})
    out = GradingOutcome(run_id, instance_id, kind, code, state["hard"], before, state["min"], harness, inst,
                         summarize(run_id, instance_id, harness, inst, code, state["hard"]), str(run_dir))
    meta.update(exit_code=code, hard_stopped=state["hard"], disk_min_gib=round(state["min"], 3), ended_unix=now(), summary=out.summary)
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    return out

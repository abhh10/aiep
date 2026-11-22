"""M2 -> M1 handoff validation: is this run directory something the evaluator may be given, and what may be concluded from it?

``validate_handoff(run_dir)`` inspects ONE M2 run directory (``aiep.artifacts``) and returns an explicit report. It never runs the evaluator,
never contacts Docker or any network, and never says a task is solved: a patch is a candidate, not a result (``grading.resolved`` is always ``None``).

Verdicts
--------
``ready``          every check passed: the run is valid, its accounting is complete AND exact, and ``predictions.jsonl`` is the exact, verified handoff.
``ready_flagged``  the patch handoff is sound and may be graded, but the run is NOT a clean measurement: its figures are an upper bound (an interrupted
                   request was charged at max_tokens), the budget was reached or exceeded, or an optional check could not run. The flags travel with it.
``rejected``       anything else: a missing, malformed or mismatched artifact, a hash that does not verify, an invalid / censored / failed run, incomplete
                   accounting (usage may have been billed that the ledger does not contain), a hidden-data leak into the agent's input, a patch that does not
                   apply to the pinned seed, a task / image / seed / prompt / model that is not the registered one.

Nothing is inferred from the existence of a patch. Failed, censored and incomplete-accounting runs can only be ``rejected``.

Hidden task data (FAIL_TO_PASS / PASS_TO_PASS ids, the test patch, the gold patch) is loaded host-side for the leak scan ONLY. It is never written to the
report: only counts are.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from . import artifacts
from .experiment import REGISTERED_PROMPT_SETS, claude_args, load_prompts
from .patch import validate_patch

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = 1
READY, READY_FLAGGED, REJECTED = "ready", "ready_flagged", "rejected"
PREDICTION_KEYS = ("instance_id", "model_name_or_path", "model_patch")  # swebench.harness.constants KEY_INSTANCE_ID / KEY_MODEL / KEY_PREDICTION
_RUN_ID = re.compile(r"^m2-(?P<instance>[A-Za-z0-9_.\-]+__[A-Za-z0-9_.\-]+-\d+)-(?P<label>[AB])-(?P<stamp>\d{8}T\d{6}Z)$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_FILES = ("run.json", "prompt.txt", "claude_stdout.jsonl", "claude_stderr.txt", "gateway_ledger.jsonl", "patch.diff", "predictions.jsonl",
                  artifacts.MANIFEST_NAME)
MIN_LEAK_LINE = 40  # characters: shorter lines of a hidden patch are too generic to mean anything
DRIVER_INSTANCE = "pytest-dev__pytest-5227"  # scripts/m1_eval.sh is hardcoded to this instance


@dataclass
class Providers:
    """Where the host-side facts come from. Tests replace them; the defaults use the pinned local dataset snapshot.

    ``seed_git(pin, patch_bytes)`` answers the three questions that need git on the PINNED seed: its HEAD, its HEAD's parent, and whether the patch applies
    (``git apply --check``) to a fresh copy. It returns ``{"head": str, "parent": str, "applies": bool, "why": str}``. It is deliberately NOT implemented here:
    host-side git is confined to two modules (``tests/test_patch.py``), so ``scripts/validate_handoff.py`` supplies it. Without it the check is skipped and flagged.
    """

    load_record: Callable[[str], dict] | None = None  # instance_id -> the FULL dataset record (hidden fields included)
    seed_git: Callable[[dict, bytes], dict[str, Any]] | None = None


def default_load_record(root: Path) -> Callable[[str], dict]:
    from .task_input import load_record
    return lambda instance_id: load_record(instance_id, root / ".cache" / "hf", root)


@dataclass
class Report:
    run_dir: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def check(self, name: str, ok: bool, detail: Any = "", *, severity: str = "reject") -> bool:
        """``severity`` is what a failure means: ``reject`` blocks the handoff, ``flag`` lets it through but travels with it."""
        self.checks.append({"check": name, "ok": bool(ok), "severity": severity, "detail": detail})
        if not ok and severity == "flag":
            self.flags.append(name)
        return bool(ok)

    @property
    def rejections(self) -> list[str]:
        return [c["check"] for c in self.checks if not c["ok"] and c["severity"] == "reject"]

    @property
    def status(self) -> str:
        if self.rejections:
            return REJECTED
        return READY_FLAGGED if self.flags else READY

    def as_dict(self) -> dict[str, Any]:
        d = {"schema": SCHEMA, "run_dir": self.run_dir, "status": self.status, "rejections": self.rejections, "flags": self.flags,
             "summary": self.summary, "checks": self.checks,
             "grading": {"performed": False, "resolved": None,
                         "note": "Not graded. A patch existing is not a result: only the M1 evaluator's per-instance FAIL_TO_PASS / PASS_TO_PASS verdict can say whether "
                                 "the task is resolved. This report only says whether the run may be given to it."}}
        d["report_sha256"] = hashlib.sha256(json.dumps({k: v for k, v in d.items() if k != "report_sha256"}, sort_keys=True).encode()).hexdigest()
        return d


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def patch_files(patch: str) -> list[str]:
    return sorted({m.group(1) for m in re.finditer(r"(?m)^diff --git a/(\S+) b/", patch)})


def evaluator_support(root: Path, instance: Any) -> tuple[bool, str]:
    """Can the evaluator be asked to grade this task? Yes for the instance the M1 driver (``scripts/m1_eval.sh``) is hardcoded to, and for any task REGISTERED for the
    generalized M3 driver: a pinned evaluator image, a pinned seed and a verified agent identity (``aiep.task_pins``). Never a guess: anything else is not supported."""
    if instance == DRIVER_INSTANCE:
        return True, "the instance the M1 driver (scripts/m1_eval.sh) is hardcoded to"
    if not isinstance(instance, str) or not instance:
        return False, "no instance"
    try:
        from .task_pins import pin_status
        st = pin_status(Path(root), instance)
    except Exception as e:  # unreadable registry: not supported
        return False, f"the task registry could not be read ({type(e).__name__})"
    if st["runnable"]:
        return True, "registered: pinned evaluator image, pinned seed and verified agent identity (graded by the generalized M3 driver, scripts/m3_grade.py)"
    return False, f"not registered (missing: {', '.join(st['missing'])})"


ALLOWED_PROMPT_SETS = REGISTERED_PROMPT_SETS


def validate_handoff(run_dir: Path, *, root: Path = REPO_ROOT, providers: Providers | None = None, hidden_scan: bool = True,
                     apply_check: bool = True, expected_slots: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Validate one run directory. Returns the report as a JSON-safe dict (see the module docstring for the verdicts)."""
    root, run_dir = Path(root), Path(run_dir)
    prov = providers or Providers()
    load_record = prov.load_record or default_load_record(root)
    rep = Report(run_dir=str(run_dir))

    # ---------------------------------------------------------------- 1. presence and hashes
    present = {n: (run_dir / n).is_file() for n in REQUIRED_FILES}
    missing = sorted(n for n, ok in present.items() if not ok)
    if not rep.check("every required artifact is present (a run with no patch has no predictions.jsonl and cannot be handed off)", not missing, {"missing": missing}):
        try:  # still say WHY the run has no handoff (an invalid or interrupted run, incomplete accounting ...) when run.json is readable
            partial = json.loads((run_dir / "run.json").read_text())
            _run_state(rep, partial, run_dir, (partial.get("limits") or {}).get("budget_usd"))
        except Exception:
            pass
        return rep.as_dict()
    problems = artifacts.verify_run_artifacts(run_dir)
    if not rep.check("artifacts_manifest.json verifies: every file's sha256 and size match, nothing extra", not problems, problems):
        return rep.as_dict()
    try:
        run = json.loads((run_dir / "run.json").read_text())
        assert isinstance(run, dict)
    except (ValueError, AssertionError):
        rep.check("run.json is a JSON object", False)
        return rep.as_dict()
    rep.check("run.json schema is the one this validator understands", run.get("schema") == artifacts.SCHEMA, run.get("schema"))

    # ---------------------------------------------------------------- 2. identity
    m = _RUN_ID.fullmatch(run_dir.name)
    task = run.get("task") or {}
    instance, label = task.get("instance_id"), run.get("config")
    rep.check("the directory name is a well-formed run id (m2-<instance>-<A|B>-<UTC stamp>)", m is not None, run_dir.name)
    rep.check("run.json run_id, task.instance_id and config agree with the directory name", m is not None and run.get("run_id") == run_dir.name
              and instance == m.group("instance") and label == m.group("label"), {"run_id": run.get("run_id"), "instance": instance, "config": label})
    rep.summary.update(run_id=run.get("run_id"), instance_id=instance, config=label, model=run.get("model"), repo=task.get("repo"),
                       base_commit=task.get("base_commit"))
    experiment = json.loads((root / "config" / "experiment.json").read_text())
    rep.check("the model is the experiment's single pinned model", run.get("model") == experiment["model"], run.get("model"))
    images = json.loads((root / "config" / "evaluator_images.json").read_text())["images"]
    rep.check("the task has a registered evaluator image", instance in images, instance)
    rep.check("the agent ran in the SAME image the evaluator will use (image ID equals config/evaluator_images.json)",
              instance in images and run.get("image_id") == images[instance]["image_id"], run.get("image_id"))

    # ---------------------------------------------------------------- 3. predictions.jsonl
    raw_pred = (run_dir / "predictions.jsonl").read_bytes()
    lines = raw_pred.split(b"\n")
    one_line = len(lines) == 2 and lines[1] == b"" and lines[0] != b""
    rep.check("predictions.jsonl is exactly one newline-terminated JSON line (the stock harness would grade every line)", one_line, {"lines": len(lines) - 1})
    pred: Any = {}
    if one_line:
        try:
            pred = json.loads(lines[0])
        except ValueError:
            pred = {}
    if not isinstance(pred, dict):
        pred = {}
    rep.check("the prediction is a JSON object with exactly the harness keys instance_id, model_name_or_path, model_patch (all strings)",
              isinstance(pred, dict) and set(pred) == set(PREDICTION_KEYS) and all(isinstance(pred.get(k), str) for k in PREDICTION_KEYS), sorted(pred) if isinstance(pred, dict) else None)
    rep.check("the prediction's instance_id is this run's task", pred.get("instance_id") == instance, pred.get("instance_id"))
    rep.check("the prediction's model_name_or_path is aiep-<config>-<model>", pred.get("model_name_or_path") == f"aiep-{label}-{run.get('model')}", pred.get("model_name_or_path"))
    patch_bytes = (run_dir / "patch.diff").read_bytes()
    pred_patch = pred.get("model_patch", "")
    rep.check("model_patch is byte-identical to patch.diff", isinstance(pred_patch, str) and pred_patch.encode() == patch_bytes, {"patch_sha256": _sha256(patch_bytes)})
    agent = ((run.get("lifecycle") or {}).get("agent")) or {}
    rec_patch = agent.get("patch") or {}
    rep.check("the patch's sha256, size and status equal what the agent run recorded when it extracted it", rec_patch.get("sha256") == _sha256(patch_bytes)
              and rec_patch.get("size") == len(patch_bytes) and rec_patch.get("status") == "ok", rec_patch)
    result = validate_patch(patch_bytes)
    rep.check("the patch passes the production patch validator (text, size, safe relative paths, no binary / rename / symlink / submodule)", result.status == "ok", result.reasons)
    files = patch_files(patch_bytes.decode("utf-8", "replace"))
    adds = sum(1 for l in patch_bytes.decode("utf-8", "replace").splitlines() if l.startswith("+") and not l.startswith("+++"))
    dels = sum(1 for l in patch_bytes.decode("utf-8", "replace").splitlines() if l.startswith("-") and not l.startswith("---"))
    rep.summary["patch"] = {"sha256": _sha256(patch_bytes), "size": len(patch_bytes), "files": len(files), "additions": adds, "deletions": dels, "paths": files}

    # ---------------------------------------------------------------- 4. pinned seed, base commit, task record, applicability
    seeds = json.loads((root / "config" / "seeds.json").read_text()).get("seeds") or {}
    pin = seeds.get(instance) or {}
    seed = run.get("seed") or {}
    rep.check("the seed the agent started from is the pinned seed (archive sha256, HEAD, entry count)", bool(pin) and seed.get("sha256") == pin.get("sha256")
              and seed.get("head") == pin.get("head") and seed.get("entries") == pin.get("entries"), {"run": seed.get("head"), "pin": pin.get("head")})
    rep.check("the seed was taken from the evaluator's image", bool(pin) and pin.get("image_id") == images.get(instance, {}).get("image_id"))
    record: dict[str, Any] | None = None
    try:
        record = load_record(instance)
    except Exception as e:
        rep.check("the pinned local dataset record could be loaded (host side)", False, f"{type(e).__name__}: {e}")
    if record is not None:
        rep.check("run.json repo and base_commit equal the dataset record's", record.get("repo") == task.get("repo") and record.get("base_commit") == task.get("base_commit")
                  and bool(_SHA.fullmatch(str(task.get("base_commit")))), {"repo": task.get("repo"), "base_commit": task.get("base_commit")})
    if apply_check and pin and prov.seed_git is not None:
        try:
            g = prov.seed_git(pin, patch_bytes)
            rep.check("the pinned seed's HEAD equals the recorded seed head, and its parent is the dataset base_commit (the synthetic 'SWE-bench' commit sits on base_commit)",
                      g.get("head") == pin.get("head") and g.get("parent") == task.get("base_commit"), {"head": str(g.get("head"))[:12], "parent": str(g.get("parent"))[:12]})
            rep.check("the exact model_patch applies cleanly (git apply --check) to a fresh copy of the pinned seed", g.get("applies") is True, g.get("why", ""))
        except Exception as e:
            rep.check("the pinned seed could be materialised and the patch applied to it", False, f"{type(e).__name__}: {e}")
    else:
        rep.check("applicability check against the pinned seed was skipped (disabled, or no seed_git provider)", False, "no check of HEAD parent or git apply --check", severity="flag")

    # ---------------------------------------------------------------- 5. registered configuration
    prompt_file = run.get("prompt_set_file", "config/workflow_prompts.json")
    rep.check("the run used one of the registered prompt sets", prompt_file in ALLOWED_PROMPT_SETS, prompt_file)
    prompts = load_prompts(root / (prompt_file if prompt_file in ALLOWED_PROMPT_SETS else "config/workflow_prompts.json"))  # recomputes and verifies the hashes itself
    pr = run.get("prompt") or {}
    rep.check("the run used the registered prompt template and workflow line (hashes equal config/workflow_prompts.json)", label in ("A", "B") and pr.get("template_sha256") == prompts.hashes["template"]
              and pr.get("workflow_sha256") == prompts.hashes.get(label), {"label": label})
    rep.check("the recorded prompt hash equals the sha256 of prompt.txt", pr.get("prompt_sha256") == _sha256((run_dir / "prompt.txt").read_bytes()), pr.get("prompt_sha256"))
    cli = json.loads((root / "config" / "claude_cli.json").read_text())
    rep.check("the Claude binary recorded is the pinned one (sha256, size, version, platform)", (run.get("claude_cli") or {}) == {
        "version": cli["version"], "platform": cli["platform"], "sha256": cli["sha256"], "size_bytes": cli["size_bytes"]}, run.get("claude_cli"))
    budget = (run.get("limits") or {}).get("budget_usd")
    try:
        want_args = list(claude_args(run.get("model"), budget))
    except Exception:
        want_args = None
    rep.check("the Claude flags are exactly the experiment's flags for this model and budget (identical for A and B)", want_args is not None and run.get("claude_args") == want_args, run.get("claude_args"))
    gw = json.loads((root / "config" / "gateway_image.json").read_text())
    rep.check("the gateway image is the pinned one", (run.get("gateway_image") or {}).get("image_id") == gw.get("image_id"))
    if run.get("calibration") is not None:  # a calibration run is evaluable for its own purposes, but it is never a scheduled observation
        cal = run["calibration"]
        if expected_slots is not None:
            rep.check("the run is not a calibration run (calibration runs are never scheduled observations)", False, cal.get("id") if isinstance(cal, dict) else str(cal)[:40])
        else:
            rep.check("this is a calibration run, not a scored observation (it must never enter the M3 results)", False, cal.get("id") if isinstance(cal, dict) else str(cal)[:40],
                      severity="flag")
    exp = run.get("experiment")
    if exp is not None or expected_slots is not None:
        keys = ("experiment_id", "task_id", "arm", "repetition", "pair_id", "order_index", "slot_id")
        ok = isinstance(exp, dict) and set(exp) == set(keys) and exp["task_id"] == instance and exp["arm"] == label
        ok = ok and exp["slot_id"] == f"{exp['experiment_id']}/{instance}/{label}/r{exp['repetition']}"
        rep.check("the run carries a complete experiment identity that agrees with its task and arm", ok, exp)
        if expected_slots is not None:
            want = expected_slots.get(exp["slot_id"]) if isinstance(exp, dict) and "slot_id" in exp else None
            rep.check("the run's slot is a scheduled slot of the experiment with the scheduled order index and pair id", bool(want) and all(
                exp.get(k) == want.get(k) for k in ("experiment_id", "task_id", "arm", "repetition", "pair_id", "order_index")), exp)
        rep.summary["experiment"] = exp
    rep.check("the three AIEP networks were verified for this run", set(run.get("networks") or {}) == {"aiep-agent-net", "aiep-proxy-net", "aiep-egress-net"}, sorted(run.get("networks") or {}))

    budget = (run.get("limits") or {}).get("budget_usd")
    agent = ((run.get("lifecycle") or {}).get("agent")) or {}
    _run_state(rep, run, run_dir, budget)

    # ---------------------------------------------------------------- 8. hidden data: the agent never saw it, the evaluator-side data is intact
    if hidden_scan and record is not None:
        hidden_texts = _hidden_needles(record)
        prompt_text = (run_dir / "prompt.txt").read_text(errors="replace")
        # The issue text (problem statement and hints) is the agent's legitimate, public input. A hidden line that the issue itself quotes (an issue that proposes
        # the fix quotes the fix) is not a leak; one that appears in the prompt and NOT in the issue text is.
        public = str(record.get("problem_statement") or "") + "\n" + str(record.get("hints_text") or "")
        in_prompt = {k: sum(1 for n in v if n in prompt_text and n not in public) for k, v in hidden_texts.items()}
        quoted = sum(1 for v in hidden_texts.values() for n in v if n in prompt_text and n in public)
        rep.check("the agent's INPUT (prompt.txt) contains no FAIL_TO_PASS / PASS_TO_PASS id, no test_patch line and no gold-patch line beyond what the public issue text itself quotes",
                  sum(in_prompt.values()) == 0, {**in_prompt, "quoted_by_the_public_issue_text": quoted})
        out_text = "\n".join((run_dir / n).read_text(errors="replace") for n in ("claude_stdout.jsonl", "claude_stderr.txt")) + "\n" + patch_bytes.decode("utf-8", "replace")
        in_out = {k: sum(1 for n in v if n in out_text) for k, v in hidden_texts.items()}
        rep.check("the agent's output and patch repeat no hidden-patch line or test id (informational: tests may print ids the agent legitimately ran)",
                  in_out["gold_patch_lines"] == 0 and in_out["test_patch_lines"] == 0, in_out, severity="flag")
        hidden_paths = set(_test_patch_files(record))
        overlap = sorted(set(files) & hidden_paths)
        rep.check("the patch does not modify a file the hidden test patch also modifies (the evaluator would overwrite it)", not overlap, {"count": len(overlap)}, severity="flag")
        rep.summary["hidden_data"] = {"fail_to_pass": len(_as_list(record.get("FAIL_TO_PASS"))), "pass_to_pass": len(_as_list(record.get("PASS_TO_PASS"))),
                                      "note": "counts only: identifiers and patches are never written to this report"}
    else:
        rep.check("the hidden-data leak scan was skipped", False, "no record or hidden_scan disabled", severity="flag")

    # ---------------------------------------------------------------- 9. what the evaluator would be asked to do
    pred_path = (run_dir / "predictions.jsonl").resolve()
    supported, via = evaluator_support(root, instance)
    rep.check("the task is not registered for evaluation (neither the M1 driver's instance nor a task with a pinned image, seed and verified identity)", supported, via, severity="flag")
    rep.summary["evaluator_command_not_run"] = (f"scripts/m1_eval.sh m2eval-{run.get('run_id')} {pred_path}" if instance == DRIVER_INSTANCE
                                                else f"scripts/m3_grade.py {instance} m2eval-{run.get('run_id')} {pred_path} --kind scored")
    rep.summary["evaluator"] = "M1 stock harness via aiep.stock_eval (the only evaluator that can grade today; see docs/M2_EVALUATOR_HANDOFF.md)"
    return rep.as_dict()


def _run_state(rep: Report, run: dict, run_dir: Path, budget: Any) -> None:
    """Failed, censored, interrupted and incomplete-accounting runs are never measurements: sections 6 and 7 of the validation."""
    agent = ((run.get("lifecycle") or {}).get("agent")) or {}
    lc = run.get("lifecycle") or {}
    rep.check("the lifecycle completed (gateway and proxy came up; not a startup failure)", lc.get("status") == "completed", lc.get("status"))
    rep.check("the agent run is VALID (not invalidated by a stop reason, a failed attachment / identity / injection check, or a lost lease)", agent.get("valid") is True,
              {"valid": agent.get("valid"), "invalid_reason": agent.get("invalid_reason"), "stop_reason": agent.get("stop_reason")})
    rep.check("Claude Code exited on its own with status 0 (not killed, timed out, or container died)", agent.get("status") == "exited" and agent.get("claude_exit_code") == 0,
              {"status": agent.get("status"), "exit": agent.get("claude_exit_code")})
    rep.check("Claude's output was NOT truncated (a censored run is never graded)", agent.get("claude_output_truncated") is False, agent.get("claude_output_truncated"))
    rep.check("the orchestrator was not interrupted, did not error, and did not lose its lease", run.get("interrupted") is False and not run.get("orchestrator_error") and not run.get("termination"),
              {"interrupted": run.get("interrupted"), "error": run.get("orchestrator_error"), "termination": run.get("termination")})
    rep.check("the agent's post-start network attachment check passed", bool(agent.get("attachment")) and "attachment_ok" in (agent.get("timeline_s") or {}), agent.get("attachment"))
    stop = lc.get("gateway_stop_reason")
    rep.check("the gateway did not stop the run for a violation (model / tool / metering)", stop in (None, "budget_exhausted"), stop)
    rep.check("the run was not stopped by the gateway's budget (budget_exhausted ends a run by cost, not by the agent finishing)", stop != "budget_exhausted", stop, severity="flag")
    bt = run.get("budget_attribution") or {}
    rep.check("Claude Code's own client-side budget (a CLI estimate) did not end the run", not bt.get("client_budget_exceeded"), bt, severity="flag")
    rep.check("the agent, gateway and proxy containers were all removed", agent.get("removed") is True and lc.get("gateway_removed") is True
              and (lc.get("proxy") or {}).get("removed") is True, {"agent": agent.get("removed"), "gateway": lc.get("gateway_removed"), "proxy": (lc.get("proxy") or {}).get("removed")},
              severity="flag")

    # ---------------------------------------------------------------- 7. accounting
    acc, um = run.get("accounting") or {}, run.get("usage_measured") or {}
    ledger = (run_dir / "gateway_ledger.jsonl").read_bytes()
    from . import ledger as ledger_mod
    audit = ledger_mod.audit(ledger)
    rep.check("the gateway ledger is COMPLETE: every request it started forwarding has a finishing line (otherwise usage may have been billed that is not recorded)",
              audit["complete"] and acc.get("complete") is True and audit["forwarded"] == audit["finished"], {k: audit[k] for k in ("forwarded", "finished", "complete")})
    rep.check("the ledger recorded at least one billed request", (um.get("metered") or 0) >= 1, um.get("metered"))
    rep.check("the usage figures are EXACT (no request was cut off and charged at the max_tokens upper bound)", um.get("exact") is True and acc.get("exact") is True
              and um.get("entries_charged_at_max_tokens_upper_bound") == 0, {"exact": um.get("exact"), "upper_bound_entries": um.get("entries_charged_at_max_tokens_upper_bound")},
              severity="flag")
    try:
        spent, cap = Decimal(str(um.get("computed_usd_upper_bound_basis"))), Decimal(str(budget))
    except Exception:
        spent, cap = None, None
    rep.check("the gateway-computed cost does not exceed the configured budget (an interrupted request is charged at an upper bound that CAN exceed it)",
              spent is not None and spent <= cap, {"computed_usd": str(spent), "budget_usd": str(cap)}, severity="flag")
    rep.summary["accounting"] = {"computed_usd": str(spent), "budget_usd": str(cap), "complete": audit["complete"], "exact": um.get("exact"), "requests": um.get("requests"),
                                 "tokens": um.get("tokens"), "source": "gateway ledger (measured token usage, computed cost); Claude's own total_cost_usd is a client-side estimate and is not used"}


def _as_list(v: Any) -> list[str]:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return [v]
    return [str(x) for x in v] if isinstance(v, (list, tuple)) else []


def _hidden_needles(record: dict) -> dict[str, list[str]]:
    def lines(text: str) -> list[str]:
        """The ADDED lines of a hidden patch (without the diff marker), long enough to be distinctive."""
        added = [l[1:].strip() for l in str(text or "").splitlines() if l.startswith("+") and not l.startswith("+++")]
        return sorted({l for l in added if len(l) >= MIN_LEAK_LINE})
    return {"fail_to_pass_ids": _as_list(record.get("FAIL_TO_PASS")), "pass_to_pass_ids": [i for i in _as_list(record.get("PASS_TO_PASS")) if "::" in i],
            "test_patch_lines": lines(record.get("test_patch")), "gold_patch_lines": lines(record.get("patch"))}


def _test_patch_files(record: dict) -> list[str]:
    return re.findall(r"(?m)^diff --git a/(\S+) b/", str(record.get("test_patch") or ""))

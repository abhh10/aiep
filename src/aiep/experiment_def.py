"""The M3 experiment definition: one pre-registered, hashed JSON document that fixes everything a run may not choose for itself.

It holds the experiment id, the pre-registration seed, the selection rule and its result (the 8 pilot tasks plus an ordered replacement list), the arm
order and run schedule (both DERIVED from the seed by rules anyone can recompute), the limits and budgets, the stop conditions, the metric definitions
and the analysis plan. ``load_experiment`` re-derives the selection-independent parts (order, schedule, hashes) and refuses a definition that disagrees
with its own rules, so an edited schedule cannot pass as pre-registered.

Statuses: ``draft`` (budgets not yet calibrated: nothing may spend) and ``locked`` (budgets and caps set; ``require_locked`` passes). The pilot is labelled
a FEASIBILITY PILOT everywhere: 8 tasks cannot support an effectiveness claim.

The gateway budget is a SOFT limit, never a guaranteed maximum liability: usage is known only after a response, interrupted requests are charged at an
upper bound (input as reported plus max_tokens output) that can exceed the budget on its own, and a request that is killed before it is metered may still
be billed. ``worst_case_exposure_estimate`` states an ESTIMATE of the exposure per run, not a promise.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from .experiment import CONFIG_LABELS, load_prompts
from .task_screening import image_name, rank_key

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = 1
KIND = "feasibility_pilot"
CLAIM_SCOPE = ("FEASIBILITY PILOT: 8 tasks, one run per arm per task. It tests that the pipeline works and shows the cost range. It is NOT a statistically powered "
               "effectiveness study and supports no claim that one workflow is better than the other.")
DEFAULT_DEFINITION = Path("config") / "experiments" / "m3-pilot-v2.json"  # m3-pilot-v1.json is the superseded, never-run draft
MAX_PER_REPO, MIN_REPOS, N_TASKS = 2, 4, 8
EXCLUDED_FROM_SCORING = ("pytest-dev__pytest-5227",)  # the plumbing task: 3 junk PASS_TO_PASS entries, environment proof only
B_MAX_TEST_FIX_CYCLES = 5


class ExperimentError(ValueError):
    """The experiment definition is malformed, internally inconsistent, or not in a state that allows the requested action."""


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def definition_hash(doc: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical({k: v for k, v in doc.items() if k != "definition_sha256"})).hexdigest()


# ------------------------------------------------------------------ derived design (recomputable from the seed alone)
def first_arm_assignment(task_ids: list[str], seed: str) -> dict[str, str]:
    """Balanced randomisation: rank the tasks by ``rank_key(seed, 'first-arm', id)``; even ranks run A first, odd ranks B first (4 and 4 for 8 tasks)."""
    ranked = sorted(task_ids, key=lambda t: rank_key(seed, "first-arm", t))
    return {t: ("A" if i % 2 == 0 else "B") for i, t in enumerate(ranked)}


def block_order(task_ids: list[str], seed: str) -> list[str]:
    return sorted(task_ids, key=lambda t: rank_key(seed, "block", t))


def build_schedule(experiment_id: str, task_ids: list[str], seed: str, repetitions: int = 1) -> list[dict[str, Any]]:
    """One block per task (both arms back to back, separated by the pair gap), blocks in seeded order. ``order_index`` is the global execution position."""
    first = first_arm_assignment(task_ids, seed)
    rows: list[dict[str, Any]] = []
    idx = 0
    for rep in range(1, repetitions + 1):
        for task in block_order(task_ids, seed):
            arms = [first[task], "B" if first[task] == "A" else "A"]
            for arm in arms:
                idx += 1
                rows.append({"order_index": idx, "task_id": task, "arm": arm, "rep": rep, "pair_id": f"{experiment_id}:{task}:r{rep}",
                             "slot_id": f"{experiment_id}/{task}/{arm}/r{rep}", "arm_position": 1 if arm == arms[0] else 2})
    return rows


def apply_replacement(definition: Mapping[str, Any], failed: str, already_used: list[str] | None = None) -> dict[str, Any]:
    """The pre-registered replacement of a task that failed its controls: the FIRST unused task in the replacement order whose repository still has room
    (``max_per_repo``). It inherits the failed task's block position and arm order. Raises when no replacement is allowed (the batch stops)."""
    pre = definition["preregistration"]
    tasks = [t["instance_id"] for t in pre["tasks"]]
    if failed not in tasks:
        raise ExperimentError(f"{failed} is not one of the experiment's tasks")
    repos = {t["instance_id"]: t["repo"] for t in pre["tasks"]}
    used = set(already_used or [])
    for cand in pre["replacements"]:
        cid = cand["instance_id"]
        if cid in used or cid in tasks:
            continue
        remaining = [repos[t] for t in tasks if t != failed] + [cand["repo"]]
        if remaining.count(cand["repo"]) > pre["selection"]["max_per_repo"]:
            continue
        if len(set(remaining)) < pre["selection"]["min_repos"]:
            continue
        new_tasks = [cid if t == failed else t for t in tasks]
        return {"replaced": failed, "replacement": cid, "repo": cand["repo"], "tasks": new_tasks}
    raise ExperimentError(f"no pre-registered replacement is available for {failed}: the batch must stop and be reviewed")


def worst_case_exposure_estimate(budget_usd: str, max_concurrent: int, max_tokens: int, output_price_per_mtok: str) -> dict[str, str]:
    """An ESTIMATE of the spend one run can reach, NOT a guarantee. ``budget`` is checked only after a response is metered; each request in flight can add its own
    input plus (if cut off) ``max_tokens`` of output at the upper bound; a request killed before metering may be billed and never recorded."""
    out = Decimal(max_tokens) * Decimal(output_price_per_mtok) / Decimal(1_000_000)
    return {"budget_usd": budget_usd, "interrupted_request_output_upper_bound_usd": format(out, "f"),
            "estimated_exposure_usd": format(Decimal(budget_usd) + out * max_concurrent, "f"),
            "note": "estimate, not a maximum liability: input tokens of in-flight requests and unmetered (killed) requests are not bounded by this figure"}


# ------------------------------------------------------------------ load / validate
def load_experiment(path: Path | None = None, *, root: Path = REPO_ROOT) -> dict[str, Any]:
    path = Path(path) if path else Path(root) / DEFAULT_DEFINITION
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise ExperimentError(f"cannot read the experiment definition: {type(e).__name__}") from e
    problems = validate_definition(doc, root=Path(root))
    if problems:
        raise ExperimentError("; ".join(problems))
    return doc


def require_locked(doc: Mapping[str, Any]) -> None:
    """Spending (anything that calls the API) needs a LOCKED definition: budgets and the total cap set after calibration."""
    lim = doc["limits"]
    if doc.get("status") != "locked" or lim.get("budget_usd_per_run") in (None, "") or lim.get("max_total_computed_usd") in (None, ""):
        raise ExperimentError("the experiment is a draft: per-run and total budgets are not set (they are fixed after an approved calibration), so nothing may spend")


def validate_definition(doc: Any, *, root: Path = REPO_ROOT) -> list[str]:
    p: list[str] = []
    if not isinstance(doc, dict):
        return ["the definition is not a JSON object"]
    if doc.get("schema") != SCHEMA or doc.get("kind") != KIND:
        p.append("unsupported schema or kind")
    if doc.get("claim_scope") != CLAIM_SCOPE:
        p.append("claim_scope must be the pre-registered feasibility-pilot statement")
    if doc.get("status") not in ("draft", "locked"):
        p.append("status must be draft or locked")
    if doc.get("definition_sha256") != definition_hash(doc):
        p.append("definition_sha256 does not match the content (the definition was edited)")
    eid = doc.get("experiment_id")
    if not (isinstance(eid, str) and eid and all(c.isalnum() or c in "-._" for c in eid)):
        p.append("experiment_id must be a plain identifier")
    pre = doc.get("preregistration") or {}
    seed, sel, tasks = pre.get("seed"), pre.get("selection") or {}, pre.get("tasks") or []
    ids = [t.get("instance_id") for t in tasks]
    if not (isinstance(seed, str) and len(seed) >= 8):
        p.append("the pre-registration seed is missing")
    if len(ids) != N_TASKS or len(set(ids)) != N_TASKS or sel.get("n_tasks") != N_TASKS:
        p.append(f"the pilot has exactly {N_TASKS} distinct tasks")
    repos = [t.get("repo") for t in tasks]
    if any(repos.count(r) > MAX_PER_REPO for r in set(repos)) or len(set(repos)) < MIN_REPOS:
        p.append(f"at most {MAX_PER_REPO} tasks per repository and at least {MIN_REPOS} repositories")
    if set(ids) & set(EXCLUDED_FROM_SCORING) or not set(EXCLUDED_FROM_SCORING) <= set(sel.get("exclude") or []):
        p.append("pytest-dev__pytest-5227 must be excluded from scored analysis")
    if {r.get("instance_id") for r in pre.get("replacements") or []} & set(ids):
        p.append("a replacement is also a selected task")
    for t in tasks:
        try:
            if t.get("image") != image_name(t["instance_id"]):
                p.append(f"{t.get('instance_id')}: image name is not the published instance image")
        except (ValueError, KeyError, TypeError):
            p.append("a task entry is malformed")
    design = doc.get("design") or {}
    if design.get("repetitions") != 1:
        p.append("the pilot runs one repetition per arm per task")
    if isinstance(seed, str) and len(ids) == N_TASKS and eid:
        if design.get("schedule") != build_schedule(eid, ids, seed, 1):
            p.append("the schedule is not what the seed derives (balanced first-arm assignment, seeded block order)")
    if set(design.get("arms") or []) != set(CONFIG_LABELS):
        p.append("arms must be exactly A and B")
    # prompts: v2 file, registered hashes, A and template identical to v1, B the only change
    ps = doc.get("prompt_set") or {}
    try:
        prompts = load_prompts(Path(root) / str(ps.get("path", "")))
        v1 = load_prompts(Path(root) / "config" / "workflow_prompts.json")
        if prompts.hashes != ps.get("sha256"):
            p.append("prompt_set hashes do not match the prompt file")
        if prompts.hashes["template"] != v1.hashes["template"] or prompts.hashes["A"] != v1.hashes["A"] or prompts.hashes["B"] == v1.hashes["B"]:
            p.append("the prompt set must keep the template and A byte-identical to v1 and change only B")
        if f"at most {B_MAX_TEST_FIX_CYCLES} test-fix cycles" not in prompts.workflows["B"]:
            p.append("Config B must state its maximum number of test-fix cycles")
    except Exception as e:  # PromptError, OSError
        p.append(f"prompt set: {type(e).__name__}: {e}")
    # model and CLI come from the registered configs, never from this file alone
    try:
        exp = json.loads((Path(root) / "config" / "experiment.json").read_text())
        cli = json.loads((Path(root) / "config" / "claude_cli.json").read_text())
        if doc.get("model") != exp["model"] or (doc.get("claude_cli") or {}).get("sha256") != cli["sha256"]:
            p.append("model or Claude binary differs from config/experiment.json / config/claude_cli.json")
        ds = json.loads((Path(root) / "config" / "dataset.json").read_text())
        if (doc.get("dataset") or {}).get("revision") != ds["revision"]:
            p.append("dataset revision differs from config/dataset.json")
    except (OSError, ValueError, KeyError) as e:
        p.append(f"cannot verify model / binary / dataset against their configs: {type(e).__name__}")
    lim = doc.get("limits") or {}
    if lim.get("gateway_max_concurrent") != 1:
        p.append("the pilot is designed for ONE concurrent request per agent (gateway_max_concurrent = 1)")
    for k in ("budget_usd_per_run", "max_total_computed_usd"):
        if lim.get(k) is not None:
            try:
                if Decimal(lim[k]) <= 0:
                    raise ValueError
            except Exception:
                p.append(f"{k} must be a positive decimal string or null")
    if doc.get("status") == "locked" and (lim.get("budget_usd_per_run") is None or lim.get("max_total_computed_usd") is None):
        p.append("a locked definition needs both budgets")
    if "financial_notice" not in doc or "not a guaranteed maximum" not in doc["financial_notice"]:
        p.append("the financial notice (the budget is soft, not a guaranteed maximum liability) is required")
    conds = {c.get("id") for c in doc.get("stop_conditions") or []}
    metrics = doc.get("metrics") or {}
    if "supersedes" in doc:  # a definition from m3-pilot-v2 on: the M3.7 batch policy and the structured test-command spec are part of it
        for need in ("handoff_integrity_failure", "financial_exposure_uncertain"):
            if need not in conds:
                p.append(f"missing stop condition {need}")
        from .transcript import TEST_COMMAND_SPEC
        if metrics.get("test_command_spec") != TEST_COMMAND_SPEC:
            p.append("metrics.test_command_spec is not the spec the transcript parser implements")
        if not all(k in (doc["supersedes"] or {}) for k in ("experiment_id", "definition_sha256", "reason", "changes", "unchanged")):
            p.append("supersedes must state what it replaces, why, what changed and what did not")
    elif "test_command_patterns" not in metrics:
        p.append("metrics.test_command_patterns (v1) or test_command_spec is required")
    for need in ("accounting_incomplete", "disk_hard_stop", "cumulative_cost_cap", "hidden_data_leak", "control_replacement_exhausted", "consecutive_invalid_runs",
                 "disk_start_gate", "grading_invalid", "task_not_prepared"):
        if need not in conds:
            p.append(f"missing stop condition {need}")
    return p

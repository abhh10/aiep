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

# ---- exploratory (E1) definitions: staged, budget-constrained, descriptive only. The size, repository counts and stages are read from the definition itself (and validated),
# never hard-coded; ``feasibility_pilot`` definitions (m3-pilot-v1/v2) keep their fixed 8-task rules exactly.
KIND_EXPLORATORY = "exploratory_ab"
EXPLORATORY_CLAIM_SCOPE = ("EXPLORATORY FEASIBILITY STUDY (E1), budget-constrained: a few paired tasks, one run per arm per task. It tests the pipeline and shows cost and behaviour for both "
                           "workflows on a few tasks. It is NOT a statistically powered effectiveness test, supports no claim that one workflow is better than the other, and reports no "
                           "significance tests.")
EXCLUDED_REPOS_E1 = ("matplotlib/matplotlib", "scikit-learn/scikit-learn")  # large images and seeds: excluded from E1 tasks and replacements
EXPLORATORY_STOP_IDS = ("pair_budget_unfunded", "overshoot_allowance_exceeded", "compatibility_decision_missing")
DECISION_RECORD_FIELDS = ("compatible", "evidence", "operator_decision", "decided_by", "decided_at")


class ExperimentError(ValueError):
    """The experiment definition is malformed, internally inconsistent, or not in a state that allows the requested action."""


class StageMinimumViolation(ExperimentError):
    """The replacement the pre-registered order selects would leave its own STAGE with fewer repositories than that stage's registered minimum. It is raised INSTEAD of skipping to a later registry
    entry: the registered order is never changed, nothing is recorded, and a human must decide (the batch halts ``replacement_violates_stage_minimum``)."""

    def __init__(self, message: str, *, failed: str, candidate: str, repo: str, stage: Any, stage_min_repos: int, stage_repos: list[str], registry_index: int):
        super().__init__(message)
        self.failed, self.candidate, self.repo, self.stage = failed, candidate, repo, stage
        self.stage_min_repos, self.stage_repos, self.registry_index = stage_min_repos, stage_repos, registry_index


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


def eligible_repo(repo: Any) -> bool:
    """The one explicit eligibility rule applied to selected tasks AND to the replacement pool alike: the repository must not be excluded."""
    return repo not in EXCLUDED_REPOS_E1


def guaranteed_reservation_usd(doc: Mapping[str, Any]) -> Decimal | None:
    """The reservation that guarantees the REQUIRED stage(s) can be funded under the declared planning allowance: pairs x 2 arms x (gateway budget per run + overshoot allowance per run). None while
    the budgets are unset. A planning guarantee only (the allowance is provisional), never a bound on what the API can bill."""
    lim = doc.get("limits") or {}
    if lim.get("budget_usd_per_run") in (None, "") or lim.get("overshoot_allowance_usd") in (None, ""):
        return None
    pairs = sum(len(s.get("tasks") or []) for s in (doc.get("design") or {}).get("stages") or [] if s.get("activation") == "required")
    return pairs * 2 * (Decimal(lim["budget_usd_per_run"]) + Decimal(lim["overshoot_allowance_usd"]))


def stage_first_arms(stages: list[Mapping[str, Any]], seed: str) -> dict[int, dict[str, str]]:
    """First arm of each task, derived PER STAGE with the same seeded rule as ``first_arm_assignment``, so every stage is balanced on its own (an even-sized stage is exactly half and half)."""
    return {int(st["stage"]): first_arm_assignment(list(st["tasks"]), seed) for st in stages}


def build_staged_schedule(experiment_id: str, stages: list[Mapping[str, Any]], seed: str, repetitions: int = 1) -> list[dict[str, Any]]:
    """Stage by stage (stage 2 only after stage 1), one block per task (both arms back to back, separated by the pair gap), blocks in seeded order WITHIN each stage. ``order_index`` is
    the global execution position. Each row also carries its ``stage``. Rows are otherwise identical in shape to ``build_schedule``'s."""
    rows: list[dict[str, Any]] = []
    idx = 0
    first = stage_first_arms(stages, seed)
    for st in stages:
        ids = list(st["tasks"])
        for rep in range(1, repetitions + 1):
            for task in block_order(ids, seed):
                arms = [first[int(st["stage"])][task], "B" if first[int(st["stage"])][task] == "A" else "A"]
                for arm in arms:
                    idx += 1
                    rows.append({"order_index": idx, "task_id": task, "arm": arm, "rep": rep, "pair_id": f"{experiment_id}:{task}:r{rep}", "slot_id": f"{experiment_id}/{task}/{arm}/r{rep}",
                                 "arm_position": 1 if arm == arms[0] else 2, "stage": int(st["stage"])})
    return rows


def is_exploratory(doc: Mapping[str, Any]) -> bool:
    return doc.get("kind") == KIND_EXPLORATORY


def apply_replacement(definition: Mapping[str, Any], failed: str, already_used: list[str] | None = None, *, stage: Any = None, stage_tasks: list[str] | None = None,
                      stage_min_repos: int | None = None) -> dict[str, Any]:
    """The pre-registered replacement of a task that failed its controls: the FIRST unused task in the replacement order whose repository still has room
    (``max_per_repo``) and that keeps the overall repository minimum. It inherits the failed task's block position and arm order. Raises when no replacement is allowed (the batch stops).

    Stage-aware (exploratory definitions): when ``stage_tasks`` (the stage's CURRENT tasks, including ``failed``) and ``stage_min_repos`` are given, the candidate the global rules select is then
    checked against its own stage's registered repository minimum. A candidate that would violate it raises ``StageMinimumViolation``: the selection order is NOT altered (no later entry is tried) and
    nothing is silently accepted. Without those arguments (feasibility pilots have no stages) the behaviour is exactly as before."""
    pre = definition["preregistration"]
    tasks = [t["instance_id"] for t in pre["tasks"]]
    if failed not in tasks:
        raise ExperimentError(f"{failed} is not one of the experiment's tasks")
    repos = {t["instance_id"]: t["repo"] for t in pre["tasks"]}
    used = set(already_used or [])
    skipped: list[dict[str, str]] = []  # why each earlier registry entry was passed over: part of the audit record, derived only from the registry and the current task list
    for index, cand in enumerate(pre["replacements"]):
        cid = cand["instance_id"]
        if cid in used:
            skipped.append({"instance_id": cid, "reason": "already_used"})
            continue
        if cid in tasks:
            skipped.append({"instance_id": cid, "reason": "already_a_selected_task"})
            continue
        remaining = [repos[t] for t in tasks if t != failed] + [cand["repo"]]
        if remaining.count(cand["repo"]) > pre["selection"]["max_per_repo"]:
            skipped.append({"instance_id": cid, "reason": "max_per_repo"})
            continue
        if len(set(remaining)) < pre["selection"]["min_repos"]:
            skipped.append({"instance_id": cid, "reason": "min_repos"})
            continue
        stage_check = None
        if stage_tasks is not None or stage_min_repos is not None:
            if stage_tasks is None or not (type(stage_min_repos) is int and stage_min_repos >= 1) or failed not in stage_tasks or any(t not in repos for t in stage_tasks):
                raise ExperimentError(f"{failed} is not consistently part of the stage given for the replacement check")
            stage_repos = sorted({repos[t] for t in stage_tasks if t != failed} | {cand["repo"]})
            stage_check = {"stage": stage, "min_repos": stage_min_repos, "repos_after": stage_repos}
            if len(stage_repos) < stage_min_repos:
                raise StageMinimumViolation(
                    f"replacing {failed} by {cid} ({cand['repo']}), the first registry entry the pre-registered rules select, would leave stage {stage} with {len(stage_repos)} repository(ies) "
                    f"{stage_repos}, below its registered minimum of {stage_min_repos}; no later entry is tried and nothing is recorded: a human decision is required",
                    failed=failed, candidate=cid, repo=cand["repo"], stage=stage, stage_min_repos=stage_min_repos, stage_repos=stage_repos, registry_index=index)
        new_tasks = [cid if t == failed else t for t in tasks]
        return {"replaced": failed, "replacement": cid, "repo": cand["repo"], "tasks": new_tasks, "registry_index": index, "skipped": skipped, "stage_check": stage_check}
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


def _validate_stages(doc: Mapping[str, Any], tasks: list[Mapping[str, Any]], seed: Any, eid: Any, min_repos: Any) -> list[str]:
    """Stages of an exploratory definition: explicit task lists that partition the selected tasks, first arms equal to the per-stage seeded derivation (so every stage is balanced),
    a repository minimum per stage, the conditional-task rules, and a schedule equal to the staged derivation."""
    p: list[str] = []
    design = doc.get("design") or {}
    stages = design.get("stages")
    ids = [t.get("instance_id") for t in tasks]
    repo_of = {t.get("instance_id"): t.get("repo") for t in tasks}
    if not (isinstance(stages, list) and stages and all(isinstance(s, dict) for s in stages)):
        return ["design.stages must be a non-empty list of stage objects"]
    nums = [s.get("stage") for s in stages]
    if nums != list(range(1, len(stages) + 1)):
        p.append("stages must be numbered 1..n in execution order")
    flat = [t for s in stages for t in (s.get("tasks") or [])]
    if flat != ids or len(set(flat)) != len(flat):
        p.append("the stages' tasks must be exactly the selected tasks, each once, in order")
        return p
    for s in stages:
        if s.get("activation") not in ("required", "conditional") or (s.get("stage") == 1 and s.get("activation") != "required"):
            p.append(f"stage {s.get('stage')}: activation must be 'required' (stage 1 always) or 'conditional'")
        if s.get("activation") == "conditional" and not str(s.get("activation_rule", "")).strip():
            p.append(f"stage {s.get('stage')}: a conditional stage needs a written activation_rule that does not depend on observed A/B outcomes")
        sr = {repo_of[t] for t in s["tasks"]}
        if not (type(s.get("min_repos")) is int and len(sr) >= s["min_repos"]):
            p.append(f"stage {s.get('stage')}: at least {s.get('min_repos')} repositories are required")
        if isinstance(seed, str):
            want = first_arm_assignment(list(s["tasks"]), seed)
            if s.get("first_arm") != want:
                p.append(f"stage {s.get('stage')}: first_arm is not what the seed derives for this stage")
            a, b = sum(v == "A" for v in want.values()), sum(v == "B" for v in want.values())
            if len(want) % 2 == 0 and a != b:
                p.append(f"stage {s.get('stage')}: an even-sized stage must be balanced")
        for c in s.get("conditional_tasks") or []:
            dr = c.get("decision_record") if isinstance(c, dict) else None
            if not (isinstance(dr, dict) and dr.get("required") is True and dr.get("status") == "pending" and dr.get("uses_ab_outcomes") is False
                    and tuple(dr.get("fields") or ()) == DECISION_RECORD_FIELDS and "before any agent run" in str(dr.get("required_before", ""))):
                p.append(f"stage {s.get('stage')}: a conditional task needs a pending decision_record (required before any agent run, fields {list(DECISION_RECORD_FIELDS)}, uses_ab_outcomes false)")
            if not (isinstance(c, dict) and c.get("task") in s["tasks"] and str(c.get("rule", "")).strip() and c.get("on_failure") == "replace_from_ordered_list"
                    and c.get("depends_on_ab_outcomes") is False and c.get("decided_before_any_agent_run") is True):
                p.append(f"stage {s.get('stage')}: a conditional task needs a rule, on_failure 'replace_from_ordered_list', depends_on_ab_outcomes false and decided_before_any_agent_run true")
    total_repos = len({repo_of[t] for t in ids})
    if isinstance(min_repos, int) and total_repos < min_repos:
        p.append(f"at least {min_repos} repositories overall")
    if isinstance(seed, str) and isinstance(eid, str) and design.get("schedule") != build_staged_schedule(eid, stages, seed, 1):
        p.append("the schedule is not what the seed derives per stage (balanced first arms within each stage, seeded block order, stage 2 after stage 1)")
    return p


def validate_definition(doc: Any, *, root: Path = REPO_ROOT) -> list[str]:
    p: list[str] = []
    if not isinstance(doc, dict):
        return ["the definition is not a JSON object"]
    exploratory = doc.get("kind") == KIND_EXPLORATORY
    if doc.get("schema") != SCHEMA or doc.get("kind") not in (KIND, KIND_EXPLORATORY):
        p.append("unsupported schema or kind")
    if doc.get("claim_scope") != (EXPLORATORY_CLAIM_SCOPE if exploratory else CLAIM_SCOPE):
        p.append("claim_scope must be the pre-registered " + ("exploratory-study" if exploratory else "feasibility-pilot") + " statement")
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
    if exploratory:
        n_tasks, max_per_repo, min_repos = sel.get("n_tasks"), sel.get("max_per_repo"), sel.get("min_repos")
        if not all(type(v) is int and v >= 1 for v in (n_tasks, max_per_repo, min_repos)):
            p.append("selection.n_tasks, max_per_repo and min_repos must be positive integers (an exploratory definition states its own size)")
            n_tasks = max_per_repo = min_repos = None
    else:
        n_tasks, max_per_repo, min_repos = N_TASKS, MAX_PER_REPO, MIN_REPOS
    if n_tasks is not None:
        if len(ids) != n_tasks or len(set(ids)) != n_tasks or sel.get("n_tasks") != n_tasks:
            p.append(f"the study has exactly {n_tasks} distinct tasks" if exploratory else f"the pilot has exactly {N_TASKS} distinct tasks")
        repos = [t.get("repo") for t in tasks]
        if any(repos.count(r) > max_per_repo for r in set(repos)) or len(set(repos)) < min_repos:
            p.append(f"at most {max_per_repo} tasks per repository and at least {min_repos} repositories")
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
    if exploratory:
        rules = {r.get("id"): r for r in sel.get("eligibility_rules") or [] if isinstance(r, dict)}
        if "repository_not_excluded" not in rules or (rules.get("image_size_and_seed_fit") or {}).get("status") != "pending_preparation_gate":
            p.append("selection.eligibility_rules must state repository_not_excluded and an image_size_and_seed_fit rule that is a pending PREPARATION gate (no size claim is made by the definition)")
        ev = sel.get("evidence") or {}
        inc = [e.get("instance_id") for e in ev.get("tasks") or [] if e.get("decision") == "included" and e.get("evidence")]
        if sorted(inc) != sorted(i for i in ids if i):
            p.append("selection.evidence.tasks must record the evidence for every included task")
        if any(not e.get("evidence") for e in (ev.get("tasks") or []) + (ev.get("replacements") or [])):
            p.append("every inclusion or exclusion in selection.evidence needs its evidence")
        if any(e.get("decision") == "included" and not eligible_repo(e.get("repo")) for e in (ev.get("tasks") or []) + (ev.get("replacements") or [])) or \
                any(not eligible_repo(r.get("repo")) for r in pre.get("replacements") or []):
            p.append("the replacement pool and the selected tasks must pass the same eligibility rules")
        excluded = set(sel.get("excluded_repos") or [])
        if not set(EXCLUDED_REPOS_E1) <= excluded:
            p.append("an exploratory definition must exclude the matplotlib and scikit-learn repositories")
        if {t.get("repo") for t in tasks} & set(EXCLUDED_REPOS_E1) or {r.get("repo") for r in pre.get("replacements") or []} & set(EXCLUDED_REPOS_E1):
            p.append("matplotlib and scikit-learn tasks may be neither selected nor replacements")
    design = doc.get("design") or {}
    if design.get("repetitions") != 1:
        p.append("the pilot runs one repetition per arm per task")
    if exploratory:
        p += _validate_stages(doc, tasks, seed, eid, min_repos)
    elif isinstance(seed, str) and len(ids) == N_TASKS and eid:
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
    if exploratory:
        try:
            if Decimal(str(lim.get("overshoot_allowance_usd"))) < 0:
                raise ValueError
        except Exception:
            p.append("limits.overshoot_allowance_usd (the definition-held in-flight overshoot allowance per run) must be a non-negative decimal string")
        if "PROVISIONAL" not in str(lim.get("overshoot_allowance_status", "")) or "not a guaranteed maximum" not in str(lim.get("overshoot_allowance_status", "")):
            p.append("limits.overshoot_allowance_status must state that the allowance is PROVISIONAL and not a guaranteed maximum")
        if lim.get("pair_atomic_reservation") is not True:
            p.append("an exploratory definition requires pair-atomic budget reservation (limits.pair_atomic_reservation = true)")
    for k in ("budget_usd_per_run", "max_total_computed_usd"):
        if lim.get(k) is not None:
            try:
                if Decimal(lim[k]) <= 0:
                    raise ValueError
            except Exception:
                p.append(f"{k} must be a positive decimal string or null")
    if exploratory and lim.get("budget_usd_per_run") is not None and lim.get("max_total_computed_usd") is not None:
        try:
            need = guaranteed_reservation_usd(doc)
            if need is not None and Decimal(lim["max_total_computed_usd"]) < need:
                p.append(f"max_total_computed_usd {lim['max_total_computed_usd']} is below the guaranteed Stage 1 reservation {format(need, 'f')} "
                         "(required-stage pairs x 2 arms x (budget_usd_per_run + overshoot_allowance_usd)); a definition that cannot fund its required stage under its own allowance is invalid")
        except Exception:
            p.append("the Stage 1 reservation cannot be computed from the limits")
    if doc.get("status") == "locked" and (lim.get("budget_usd_per_run") is None or lim.get("max_total_computed_usd") is None):
        p.append("a locked definition needs both budgets")
    if "financial_notice" not in doc or "not a guaranteed maximum" not in doc["financial_notice"]:
        p.append("the financial notice (the budget is soft, not a guaranteed maximum liability) is required")
    conds = {c.get("id") for c in doc.get("stop_conditions") or []}
    metrics = doc.get("metrics") or {}
    if exploratory:
        for need in ("handoff_integrity_failure", "financial_exposure_uncertain", *EXPLORATORY_STOP_IDS):
            if need not in conds:
                p.append(f"missing stop condition {need}")
        from .transcript import PARSER_VERSION, TEST_COMMAND_SPEC
        if metrics.get("test_command_spec") != TEST_COMMAND_SPEC or metrics.get("transcript_parser_version") != PARSER_VERSION:
            p.append("metrics must carry the transcript parser's test_command_spec and its parser_version")
        if not all(k in (doc.get("derived_from") or {}) for k in ("experiment_id", "definition_sha256", "reason", "changes", "unchanged")):
            p.append("derived_from must state the definition this one was derived from, why, what changed and what did not")
        if not all(isinstance(doc.get(k), (dict, list)) and doc.get(k) for k in ("study_limitations", "reporting_rules")):
            p.append("an exploratory definition must document its study_limitations and reporting_rules")
    elif "supersedes" in doc:  # a definition from m3-pilot-v2 on: the M3.7 batch policy and the structured test-command spec are part of it
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

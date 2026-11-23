"""Build the M3 pilot definition from the pinned local dataset snapshot: screening, seeded selection, derived schedule, fixed text blocks, self-hash.

Offline and read-only: it reads ``config/*`` and the verified local parquet, and writes nothing (the caller writes the file). Re-running with the same
seed and dataset revision gives a byte-identical document.
"""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .experiment_def import (B_MAX_TEST_FIX_CYCLES, CLAIM_SCOPE, EXCLUDED_FROM_SCORING, KIND, MAX_PER_REPO, MIN_REPOS, N_TASKS, SCHEMA, build_schedule,
                             definition_hash)
from .transcript import TEST_COMMAND_SPEC
from .task_screening import DEFAULT_MAX_PASS_TO_PASS, covariates, image_name, select_tasks

DEFAULT_SEED = "aiep-m3-pilot-v1-preregistration"
DEFAULT_ID = "m3-pilot-v2"
V1_ID = "m3-pilot-v1"
V1_DEFINITION_SHA256 = "f2ae65e5b2a2d8d721071093c9b411409bc19323fe1563efe0112e24dbd22f02"
SUPERSEDES = {
    "experiment_id": V1_ID, "definition_sha256": V1_DEFINITION_SHA256, "file": "config/experiments/m3-pilot-v1.json (preserved unchanged, never run)",
    "reason": ("The M3.7 operational-readiness audit found two defects in v1 that a run would have exposed too late: (1) the batch could not tell a deterministic setup or handoff "
               "integrity failure from agent behaviour and would have paid for all 16 runs; (2) the test-command patterns of v1 misclassified commands (missed "
               "/opt/miniconda3/envs/testbed/bin/pytest, ./tests/runtests.py and python setup.py test; counted grep/echo/pip commands that merely mention pytest), which biases the "
               "secondary 'test usage' and 'compliance' metrics. Both live inside the hashed definition, so it is superseded by a new id before any run instead of being edited."),
    "changes": [
        "stop_conditions: adds handoff_integrity_failure (halt at once on a deterministic setup/integrity rejection) and financial_exposure_uncertain (halt at once when a request was "
        "charged at the max_tokens upper bound); accounting_incomplete now also covers a metering failure; consecutive_invalid_runs (n=2) is restated as the policy for TRANSIENT "
        "infrastructure failures only",
        "metrics.test_command_patterns (regular expressions) is replaced by metrics.test_command_spec (structured shell-command parsing, implemented in aiep.transcript)",
        "metrics.definitions.test_run is reworded to match",
        "experiment_id is m3-pilot-v2, so every slot_id and pair_id carries the new id",
        "this supersedes block"],
    "unchanged": ["the 8 tasks and their order", "the 12 ordered replacements", "the pre-registration seed", "the arm assignment, block order and slot order (order_index, arm, arm_position)",
                  "prompt set v2 and its hashes", "model, Claude binary, dataset revision", "limits (budgets still null), pair gap, disk gates", "analysis plan", "financial notice"]}


def load_records(root: Path) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    from .manifest import load_dataset_config, read_verified_split, verify_dataset_snapshot
    cfg = load_dataset_config(root)
    raw = read_verified_split(verify_dataset_snapshot(root / ".cache" / "hf", cfg), cfg.get("split", "test"))
    return pq.read_table(io.BytesIO(raw)).to_pylist()


def task_entry(record: Mapping[str, Any]) -> dict[str, Any]:
    return {"instance_id": record["instance_id"], "repo": record["repo"], "base_commit": record["base_commit"], "image": image_name(record["instance_id"]),
            "pin_status": "unpulled", "image_id": None, "covariates": covariates(record)}


def build_definition(records: Sequence[Mapping[str, Any]], root: Path, *, seed: str = DEFAULT_SEED, experiment_id: str = DEFAULT_ID) -> dict[str, Any]:
    from .experiment import load_prompts
    root = Path(root)
    exp = json.loads((root / "config" / "experiment.json").read_text())
    cli = json.loads((root / "config" / "claude_cli.json").read_text())
    ds = json.loads((root / "config" / "dataset.json").read_text())
    prompts = load_prompts(root / "config" / "workflow_prompts_v2.json")
    sel = select_tasks(records, seed=seed, n=N_TASKS, max_per_repo=MAX_PER_REPO, min_repos=MIN_REPOS, exclude=EXCLUDED_FROM_SCORING)
    tasks = [task_entry(r) for r in sel["chosen"]]
    ids = [t["instance_id"] for t in tasks]
    doc: dict[str, Any] = {
        "schema": SCHEMA, "kind": KIND, "experiment_id": experiment_id, "status": "draft", "claim_scope": CLAIM_SCOPE, "supersedes": SUPERSEDES,
        "research_question": "Does prescribing an iterative test-and-fix workflow improve Claude Code's software issue-resolution performance, and at what additional cost?",
        "arms": {"A": "Basic workflow: investigate, fix, stop. Claude may run tests on its own.",
                 "B": f"Test-and-fix workflow: run relevant existing tests, analyse failures, revise, repeat, at most {B_MAX_TEST_FIX_CYCLES} test-fix cycles."},
        "prompt_set": {"path": "config/workflow_prompts_v2.json", "sha256": prompts.hashes,
                       "change_note": "v2 changes ONLY Config B: it adds an explicit maximum of 5 test-fix cycles. The template and Config A are byte-identical to the original "
                                      "set (config/workflow_prompts.json), whose B prompt (f7ad0072...) is unchanged and retired from this experiment."},
        "model": exp["model"], "claude_cli": {"version": cli["version"], "platform": cli["platform"], "sha256": cli["sha256"]},
        "dataset": {"repo_id": ds["repo_id"], "split": ds["split"], "revision": ds["revision"]},
        "preregistration": {
            "seed": seed,
            "seed_note": "chosen by the implementer before any run, with no outcome information; every derived quantity is a SHA-256 of seed|purpose|key",
            "selection": {"n_tasks": N_TASKS, "max_per_repo": MAX_PER_REPO, "min_repos": MIN_REPOS, "exclude": list(EXCLUDED_FROM_SCORING),
                          "screen": {"max_pass_to_pass": DEFAULT_MAX_PASS_TO_PASS, "min_fail_to_pass": 1,
                                     "pass_to_pass_ids": "every id must match its repository's id format (task_screening.is_well_formed); unverifiable ids exclude the task"},
                          "algorithm": "screen -> rank pool by sha256(seed|select|instance_id) -> take in order unless the repository already has max_per_repo tasks",
                          "pool_size": sel["pool_size"], "pool_sha256": sel["pool_sha256"], "dataset_records": len(records)},
            "tasks": tasks,
            "replacements": [{"instance_id": r["instance_id"], "repo": r["repo"], "image": image_name(r["instance_id"]), "covariates": covariates(r)} for r in sel["replacements"]],
            "replacement_rules": [
                "a task is replaced ONLY if it fails its gold or no-op control (or its image cannot be pinned) BEFORE any agent run on it; a task is never replaced because of an agent outcome",
                "the replacement is the first task in the replacement order that is unused and keeps at most 2 tasks per repository and at least 4 repositories",
                "the replacement takes the failed task's block position AND its arm order (so the 4/4 balance of first arms is kept); it is recorded in the batch state, never by editing this file",
                "if no replacement is allowed the batch stops for review (control_replacement_exhausted)"],
            "control_requirements": {
                "gold": "harness verdict resolved; every FAIL_TO_PASS passes; every PASS_TO_PASS is maintained; patch applied; 0 errors, 0 infrastructure failures",
                "noop": "harness verdict unresolved; 0 of the FAIL_TO_PASS tests pass; patch applied; 0 errors, 0 infrastructure failures",
                "noop_patch": "adds one new text file (AIEP_NOOP_CONTROL.txt): applies to any repository and changes no behaviour"}},
        "design": {"repetitions": 1, "arms": ["A", "B"],
                   "order": "balanced first-arm assignment (rank tasks by sha256(seed|first-arm|id); even ranks A first, odd ranks B first) and seeded block order (sha256(seed|block|id)); "
                            "both arms of a task run back to back inside one block, separated by pair_gap_s",
                   "pair_gap_s": 600, "pair_gap_note": "longer than the 5-minute prompt-cache lifetime, so the second arm of a pair does not start inside the first arm's cache window; "
                                                        "cache-read tokens are recorded and analysed anyway (an uncached-equivalent cost is reported)",
                   "schedule": build_schedule(experiment_id, ids, seed, 1)},
        "limits": {"wall_clock_s": 1800, "budget_usd_per_run": None, "max_total_computed_usd": None, "gateway_max_concurrent": 1, "client_budget_margin": "1.10",
                   "disk": {"start_gate_gib": 16, "hard_stop_gib": 12}, "b_max_test_fix_cycles": B_MAX_TEST_FIX_CYCLES,
                   "same_for_both_arms": "one limits block: nothing here is keyed by arm",
                   "calibration_note": "budgets are fixed after an approved real-API calibration and the definition is then re-hashed and locked; until then nothing may spend"},
        "financial_notice": "The gateway budget is a SOFT limit, not a guaranteed maximum liability. Usage is known only after a response is metered; an interrupted request is charged "
                            "at an upper bound (input as reported plus max_tokens output, about $0.64 of output alone at max_tokens 64000) that can exceed the budget by itself; a request "
                            "killed before it is metered may be billed and never recorded. The pilot allows one concurrent request per agent and stops on incomplete accounting.",
        "stop_conditions": [
            {"id": "accounting_incomplete", "action": "halt", "rule": "any run whose gateway ledger has a started request with no finishing line, an unreadable ledger, unparseable lines or a metering failure: halt the batch for review (usage may have been billed that is not recorded)"},
            {"id": "financial_exposure_uncertain", "action": "halt", "rule": "a run whose usage is not exact because a request was cut off and charged at the max_tokens upper bound: halt at once (the cost is an upper-bound estimate and the soft budget may have been exceeded)"},
            {"id": "handoff_integrity_failure", "action": "halt", "rule": "a run whose handoff is rejected for a deterministic setup or integrity reason (seed, base commit, image, prompts, binary, flags, identity, artifacts, any unclassified check): halt at once, because it would repeat on every run. Agent-behaviour rejections (timeout, non-zero exit, no or non-applying patch, truncated output) do not halt"},
            {"id": "disk_hard_stop", "action": "halt", "rule": "free disk below 12 GiB at any check"},
            {"id": "disk_start_gate", "action": "halt", "rule": "a block does not start below the 16 GiB start gate (the gate is never lowered for the experiment)"},
            {"id": "grading_invalid", "action": "halt", "rule": "a grading run that is not valid (harness error, infrastructure failure, hard stop, incomplete report) halts the batch for diagnosis; it is never read as a model failure"},
            {"id": "task_not_prepared", "action": "halt", "rule": "a block cannot start until its image, seed and identity are pinned and the controls ran (each needs its own approval)"},
            {"id": "cumulative_cost_cap", "action": "halt", "rule": "cumulative gateway-computed cost plus the estimated exposure of the next run would exceed max_total_computed_usd"},
            {"id": "hidden_data_leak", "action": "halt", "rule": "the handoff scan finds hidden test ids or patch lines in an agent's input"},
            {"id": "control_replacement_exhausted", "action": "halt", "rule": "a task failed its controls and no pre-registered replacement is allowed"},
            {"id": "consecutive_invalid_runs", "action": "halt", "rule": "the policy for TRANSIENT infrastructure failures: 2 consecutive runs rejected for infrastructure reasons (proxy died, gateway died or unreachable, lease lost, lifecycle failure, attachment failure) halt the batch; any other run resets the count", "n": 2},
            {"id": "orchestrator_error", "action": "halt", "rule": "any unexpected exception in the batch: halt; resume only after review"}],
        "metrics": {
            "primary": "issue resolved (harness verdict: all FAIL_TO_PASS pass and all PASS_TO_PASS maintained), per task and arm",
            "secondary": ["wall seconds", "gateway-computed cost (and uncached-equivalent cost)", "tokens by field", "requests and request outcomes", "turns", "test runs", "test-fix cycles", "compliance"],
            "test_command_spec": TEST_COMMAND_SPEC,
            "definitions": {
                "test_run": "a Bash tool call whose command, parsed as shell, RUNS a test runner according to test_command_spec (a runner word that is only an argument of another program does not count)",
                "test_fix_cycle": "a FAILED test run followed, before the next test run, by at least one Edit or Write tool call",
                "test_usage": "test_runs >= 1 (measured in BOTH arms; in A it is spontaneous)",
                "compliance_B": f"test_runs >= 1 AND (no failed test run OR at least one test-fix cycle) AND test_fix_cycles <= {B_MAX_TEST_FIX_CYCLES}",
                "cap_exceeded": f"test_fix_cycles > {B_MAX_TEST_FIX_CYCLES}"}},
        "analysis": {
            "sets": {"as_scheduled": "every task whose two scheduled runs both HAVE a result row; a run that is invalid, censored, rejected by the handoff, or ungradable counts as UNRESOLVED",
                     "valid_runs": "pairs where BOTH runs are valid, passed the handoff (ready or ready_flagged) and were graded without harness errors"},
            "paired_report": "2x2 table: both resolved, neither, A only, B only (concordant and discordant pairs), per task",
            "difference": "(B only - A only) / pairs: the paired difference in resolution rate; exact two-sided McNemar p-value on discordant pairs; seeded paired bootstrap interval - all DESCRIPTIVE for a pilot",
            "repetitions": "with more than one repetition the first repetition feeds the paired tests; per-task resolution fractions are reported",
            "cost": "per arm: mean, median, total, cost per resolved task; paired differences; cache-read and uncached-equivalent cost as sensitivity; runs with inexact accounting flagged, never silently pooled",
            "invalid_handling": "counts of invalid, censored, interrupted, budget-limited and unexecuted slots are always reported next to the result",
            "power_note": "a paired binary comparison needs roughly 190 tasks to detect a 10-point difference (25% discordant pairs) and roughly 85 for 15 points: an 8-task pilot cannot support an effectiveness claim"}}
    doc["definition_sha256"] = definition_hash(doc)
    return doc


# ------------------------------------------------------------------ E1: the budget-constrained exploratory definition, derived from m3-pilot-v2 (read-only input)
E1_ID = "e1-exploratory-1"
E1_STAGES = (
    {"stage": 1, "tasks": ("sympy__sympy-18621", "django__django-13933", "django__django-15851", "sympy__sympy-22714"), "activation": "required", "min_repos": 2},
    {"stage": 2, "tasks": ("sphinx-doc__sphinx-10325", "psf__requests-863"), "activation": "conditional", "min_repos": 2},
)
E1_OVERSHOOT_ALLOWANCE = "0.30"
E1_REQUESTS_CONDITION = {
    "task": "psf__requests-863", "condition_id": "isolated_environment_compatible",
    "rule": ("psf__requests-863 stays in stage 2 only if its test requirements are compatible with the isolated agent environment (no network). This is decided by an inspection of the task's tests and "
             "test setup, recorded in the decision record BEFORE any agent run on the task (the batch halts with compatibility_decision_missing otherwise); it never looks at an A or B outcome."),
    "on_failure": "replace_from_ordered_list", "depends_on_ab_outcomes": False, "decided_before_any_agent_run": True,
    "decision_record": {
        "required": True, "status": "pending", "uses_ab_outcomes": False,
        "required_before": "before any agent run on this task (enforced by the batch)",
        "fields": ["compatible", "evidence", "operator_decision", "decided_by", "decided_at"],
        "meaning": {"compatible": "true or false: whether the task's tests run correctly in the isolated, no-network environment", "evidence": "what was inspected (test files, setup, control results); never an A or B outcome",
                    "operator_decision": "include when compatible, replace when not", "decided_by": "the operator", "decided_at": "UTC timestamp"},
        "storage": "the batch state, decisions[task] (immutable once written; recorded with m3_batch.record_compatibility_decision); this file is never edited",
        "if_incompatible": "the pre-registered replacement rule applies: the replacement takes this task's block position and arm order"},
    "replacement": "the first task of preregistration.replacements that is unused, passes the same eligibility rules and keeps at most 2 tasks per repository and at least 4 repositories; it takes the excluded "
                   "task's block position and arm order (B first), so the stage stays balanced"}


def build_exploratory_definition(v2: Mapping[str, Any], *, experiment_id: str = E1_ID) -> dict[str, Any]:
    """The E1 definition (DRAFT, budgets unset) from the m3-pilot-v2 document: same seed, prompts, model, binary and limits shape; a two-stage subset of v2's selected tasks (matplotlib and
    scikit-learn excluded); staged, per-stage-balanced schedule; definition-held overshoot allowance; explicit exploratory claim scope. Offline: nothing is read except ``v2``."""
    from .experiment_def import EXCLUDED_REPOS_E1, EXPLORATORY_CLAIM_SCOPE, KIND_EXPLORATORY, build_staged_schedule, eligible_repo, stage_first_arms
    from .transcript import PARSER_VERSION, TEST_COMMAND_SPEC
    v2 = copy.deepcopy(v2)  # the built document never shares a mutable object with the caller's source definition (nor does the caller's copy change)
    pre = v2["preregistration"]
    by_id = {t["instance_id"]: t for t in pre["tasks"]}
    stage_defs = [dict(s, tasks=list(s["tasks"])) for s in E1_STAGES]
    chosen = [t for s in stage_defs for t in s["tasks"]]
    missing = [t for t in chosen if t not in by_id]
    if missing:
        raise ValueError(f"not selected tasks of the source definition: {missing}")
    first = stage_first_arms(stage_defs, pre["seed"])
    for s in stage_defs:
        s["first_arm"] = first[s["stage"]]
        if s["stage"] == 1:
            s["activation_rule"] = "always: stage 1 is the study"
        else:
            s["activation_rule"] = ("stage 2 is activated only by an explicit operator decision, never automatically, and only if (a) the pair-atomic budget reservation funds its pairs from the remaining "
                                    "experiment cap measured in LEDGER COST, (b) its images, seeds and controls are prepared, and (c) its conditional-task checks pass. It never depends on how A "
                                    "or B performed.")
            s["conditional_tasks"] = [copy.deepcopy(E1_REQUESTS_CONDITION)]  # deep: the nested decision_record must never alias the module constant
    # ONE explicit eligibility rule for the tasks and the replacement pool alike, applied BEFORE selection; the evidence of every inclusion/exclusion is recorded
    eligible = [t for t in pre["tasks"] if eligible_repo(t["repo"])]
    if [t["instance_id"] for t in eligible] != sorted(chosen, key=lambda i: [x["instance_id"] for x in pre["tasks"]].index(i)):
        raise ValueError("E1_STAGES are not exactly the source tasks that pass the eligibility rules")
    tasks = [dict(by_id[t]) for t in chosen]
    src = f"m3-pilot-v2 (definition sha256 {v2['definition_sha256']})"

    def item(x: Mapping[str, Any], pool: str) -> dict[str, Any]:
        ok = eligible_repo(x["repo"])
        return {"instance_id": x["instance_id"], "repo": x["repo"], "decision": "included" if ok else "excluded", "rules_applied": ["repository_not_excluded"], "pending_gates": ["image_size_and_seed_fit"],
                "evidence": (f"repository {x['repo']} is not in excluded_repos; listed in {src} preregistration.{pool}" if ok
                             else f"repository {x['repo']} is in excluded_repos; listed in {src} preregistration.{pool}")}
    evidence = {"source": src, "tasks": [item(x, "tasks") for x in pre["tasks"]], "replacements": [item(x, "replacements") for x in pre["replacements"]],
                "stage_assignment": ("tasks were split into stages by operator declaration before any agent run (E1_STAGES); the split uses no outcome and no model run, and each stage's first arms are derived "
                                     "from the seed")}
    replacements = [r for r in pre["replacements"] if eligible_repo(r["repo"])]
    schedule = build_staged_schedule(experiment_id, stage_defs, pre["seed"], 1)
    doc: dict[str, Any] = {
        "schema": SCHEMA, "kind": KIND_EXPLORATORY, "experiment_id": experiment_id, "status": "draft", "claim_scope": EXPLORATORY_CLAIM_SCOPE,
        "derived_from": {
            "experiment_id": v2["experiment_id"], "definition_sha256": v2["definition_sha256"],
            "reason": ("The total Anthropic budget is $5 of prepaid credit that cannot be increased, so the 8-task, 16-run pilot is replaced by a smaller, staged, budget-constrained exploratory study "
                       "over a subset of that pilot's pre-registered tasks (the matplotlib and scikit-learn repositories are excluded). m3-pilot-v1 and m3-pilot-v2 are preserved unchanged and are not run."),
            "changes": ["a subset of v2's selected tasks in two stages (matplotlib and scikit-learn excluded)", "staged, per-stage-balanced schedule", "limits gain a definition-held overshoot allowance, a protected reserve and "
                        "pair-atomic budget reservation", "exploratory claim scope with documented limitations and reporting rules", "two new stop conditions", "metrics pinned to transcript parser version "
                        f"{PARSER_VERSION}", "kind exploratory_ab"],
            "unchanged": ["seed", "prompt set v2 and its hashes", "model, Claude binary, dataset revision", "arm definitions and the 5-cycle cap in B", "pair gap and wall clock", "calibration/scored separation"]},
        "research_question": v2["research_question"], "arms": v2["arms"], "prompt_set": v2["prompt_set"], "model": v2["model"], "claude_cli": v2["claude_cli"], "dataset": v2["dataset"],
        "preregistration": {
            "seed": pre["seed"], "seed_note": pre["seed_note"],
            "selection": {"n_tasks": len(chosen), "max_per_repo": 2, "min_repos": 4, "exclude": list(EXCLUDED_FROM_SCORING), "excluded_repos": list(EXCLUDED_REPOS_E1),
                          "rule": ("The selected tasks are exactly those of m3-pilot-v2's pre-registered selection that pass the eligibility rules below; the replacement pool is the m3-pilot-v2 replacement list "
                                   "filtered by the SAME rules. They are split into two stages (stage_assignment). The choice uses no outcome and no model run."),
                          "eligibility_rules": [
                              {"id": "repository_not_excluded", "applies_to": "selected tasks and replacements", "status": "applied", "rule": "the repository is not in excluded_repos"},
                              {"id": "image_size_and_seed_fit", "applies_to": "selected tasks and replacements", "status": "pending_preparation_gate",
                               "rule": ("checked when a task is PREPARED, never earlier and never from outcomes: the measured image size must fit the disk start gate and the seed archive the archive limit; a task that "
                                        "fails is replaced under the pre-registered replacement rule. This definition makes no size claim about any task or replacement.")}],
                          "evidence": evidence,
                          "screen": pre["selection"]["screen"], "pool_size": pre["selection"]["pool_size"], "pool_sha256": pre["selection"]["pool_sha256"]},
            "tasks": tasks, "replacements": replacements,
            "replacement_rules": [
                "a task is replaced ONLY if it fails its gold or no-op control, its image cannot be pinned or fails the preparation gate, or (for a task with a conditional rule) its recorded compatibility decision is "
                "'incompatible', always BEFORE any agent run on it; never because of an agent outcome",
                "the replacement is the first task in the replacement order that is unused, passes the same eligibility rules and keeps at most 2 tasks per repository and at least 4 repositories overall; matplotlib and "
                "scikit-learn are never used",
                "the replacement takes the replaced task's block position AND its arm order (so each stage stays balanced); it is recorded in the batch state, never by editing this file",
                "if no replacement is allowed the batch stops for review (control_replacement_exhausted)"],
            "control_requirements": pre["control_requirements"]},
        "design": {"repetitions": 1, "arms": ["A", "B"], "stages": stage_defs,
                   "order": ("stages run in order; within a stage the blocks are in seeded order and the first arm is balanced (rank by sha256(seed|first-arm|id); even ranks A first); both arms of a task run back to "
                             "back inside one block, separated by pair_gap_s"),
                   "pair_gap_s": 600, "pair_gap_note": v2["design"]["pair_gap_note"], "schedule": schedule},
        "limits": {
            "wall_clock_s": 1800, "budget_usd_per_run": None, "max_total_computed_usd": None, "gateway_max_concurrent": 1, "client_budget_margin": "1.10",
            "disk": {"start_gate_gib": 16, "hard_stop_gib": 12}, "b_max_test_fix_cycles": B_MAX_TEST_FIX_CYCLES, "same_for_both_arms": "one limits block: nothing here is keyed by arm",
            "pair_atomic_reservation": True, "overshoot_allowance_usd": E1_OVERSHOOT_ALLOWANCE,
            "overshoot_allowance_status": ("PROVISIONAL planning allowance per run for a request already in flight when the gateway budget is reached. It is supported by the two SymPy calibrations "
                                           "(largest single request about $0.022) but not proven for other repositories, and it is not a guaranteed maximum. Each run's actual overshoot is recorded, "
                                           "and a run that exceeds the allowance halts the study."),
            "protected_reserve_usd": "0.50",
            "budget_planning_note": ("budget_usd_per_run and max_total_computed_usd are deliberately UNSET: they require the final authorization and a fresh billing check in the Console (the earlier balance must not be "
                                     "assumed). The cap is the experiment's computed ledger cost for scored runs only; it is never raised silently."),
            "calibration_spend_observed_usd": "0.0906", "calibration_spend_note": "two calibration runs, from the gateway ledgers (computed, not the Console); excluded from the scored cap"},
        "financial_notice": (v2["financial_notice"] + " E1 reserves, before each pair starts, the cost of BOTH arms (budget plus the provisional overshoot allowance per run) from the remaining cap; a pair that "
                             "cannot be funded is never started, and the study stops early with an explicit status. The workspace spend limit and prepaid balance, not this definition, are the only hard limits."),
        "stop_conditions": [*v2["stop_conditions"],
                            {"id": "pair_budget_unfunded", "action": "stop_early", "rule": "the next pair cannot be funded from the remaining cap (ledger cost so far plus the reservation for both arms would exceed max_total_computed_usd): "
                                                                                          "the study ends early and cleanly; remaining pairs are reported as not run"},
                            {"id": "compatibility_decision_missing", "action": "halt", "rule": "a conditional task is about to start and its compatibility decision record is missing: nothing is prepared or run until the "
                                                                                             "operator records the decision (before any agent run); an incompatible decision replaces the task under the pre-registered rule"},
                            {"id": "overshoot_allowance_exceeded", "action": "halt", "rule": "a run's ledger cost exceeded its gateway budget by more than overshoot_allowance_usd: the planning assumption is false, so the study halts for review before any further pair"}],
        "metrics": {"primary": v2["metrics"]["primary"], "secondary": v2["metrics"]["secondary"], "test_command_spec": copy.deepcopy(TEST_COMMAND_SPEC), "transcript_parser_version": PARSER_VERSION,
                    "definitions": {
                        "test_attempt": "a Bash tool call whose command, parsed as shell with here-documents set aside, RUNS a test runner according to test_command_spec (test_runs)",
                        "test_run_states": "not_started (the runner never started, e.g. a missing module), completed_failed, completed_passed, started_incomplete, unknown; a missing runner is never a failed suite",
                        "edit": "a confirmed Edit/Write tool call, or a heuristic Bash edit-like command (reported separately; it shows a file-writing command ran, not that a file changed)",
                        "test_fix_cycle": "a runner started and completed with failures, then an edit, then a later runner started (conservative); unclear sequences are listed as ambiguous and not counted",
                        "test_usage": "test_runs >= 1 (attempted) and test_usage_started (a runner actually started), in BOTH arms; in A it is spontaneous",
                        "compliance_B": f"a runner started AND (no failed run OR at least one test-fix cycle) AND test_fix_cycles <= {B_MAX_TEST_FIX_CYCLES}",
                        "cap_exceeded": f"test_fix_cycles > {B_MAX_TEST_FIX_CYCLES}"}},
        "analysis": {"kind": "descriptive only",
                     "sets": {"as_scheduled": "every task whose two scheduled runs both HAVE a result row; a run that is invalid, censored, rejected, or ungradable counts as UNRESOLVED",
                              "valid_runs": "pairs where BOTH runs are valid, passed the handoff and were graded without harness errors"},
                     "paired_report": "per task and arm: resolved or not, FAIL_TO_PASS and PASS_TO_PASS, cost, requests, test attempts, started and completed runs, failures, edits, confirmed and Bash-edit-only cycles; "
                                      "the 2x2 table of both resolved / neither / A only / B only is shown as COUNTS",
                     "no_inference": "no p-values, confidence intervals or claims of statistical significance are reported",
                     "cost": "gateway-measured cost and Claude-reported cost are reported as separate measurements, never merged; runs with inexact accounting are flagged",
                     "invalid_handling": "incomplete, invalid, censored, budget-limited and unpaired runs and any task not run are always reported next to the results"},
        "study_limitations": [
            "single repetition per arm per task: run-to-run variation of the model is not measured",
            "heterogeneous tasks and a very small sample: no result generalises beyond these tasks",
            "the tasks are a convenience subset of m3-pilot-v2's tasks (repositories excluded by rule; image size and seed fit are checked only when a task is prepared), chosen to fit a $5 budget; they may be easier or harder than typical SWE-bench tasks",
            "possible ceiling effects: a task that A already solves in a few requests leaves B no room to help",
            "Config A may run tests spontaneously (measured as test_usage), which narrows the contrast",
            "prompt-cache and order effects: the second arm of a pair starts after a 600 s gap, longer than the measured 5-minute cache lifetime, and the arm order is balanced within each stage",
            "transcript metrics come from parser version 2, which is heuristic for Bash edits and per-call output attribution (see the transcript module's limitations)",
            "the study may stop early because of the budget; completed pairs are then fewer than planned"],
        "reporting_rules": [
            "report this as an exploratory feasibility study, never as a test of effectiveness; show counts and per-task results, not significance",
            "the transcripts of the first two scored pairs (every run) are inspected manually before the results are relied on, and any parser misclassification is reported",
            "report incomplete, invalid, censored, budget-limited and unpaired runs and every task or stage not run, with the reason",
            "report the early stop and its reason when the budget ended the study; the stopping rule depends on ledger cost, never on outcomes",
            "report gateway-measured cost and Claude-reported cost separately",
            "disclose the two calibration runs (excluded) and that no prompt changed after them"]}
    doc["definition_sha256"] = definition_hash(doc)
    return doc

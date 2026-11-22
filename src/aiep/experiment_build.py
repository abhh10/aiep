"""Build the M3 pilot definition from the pinned local dataset snapshot: screening, seeded selection, derived schedule, fixed text blocks, self-hash.

Offline and read-only: it reads ``config/*`` and the verified local parquet, and writes nothing (the caller writes the file). Re-running with the same
seed and dataset revision gives a byte-identical document.
"""

from __future__ import annotations

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

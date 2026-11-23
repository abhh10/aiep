"""M3 batch orchestration: per-task blocks, per-task image lifecycle, disk gates, controls, handoff validation, grading, results, explicit stop conditions.

The orchestrator knows ONLY the pre-registered definition and a ``Steps`` object that performs the side effects (Docker, the API, the evaluator, the disk). That
separation is what makes the whole flow testable without Docker or spend, and what keeps the batch from improvising: every decision here is a rule in the definition.

Flow per block (one task, both arms back to back; blocks in the seeded order; a block is skipped if the state says it is done):

    disk gate  ->  prepare the task (pull / pin image / extract seed / verify identity; each its own approval)  ->  gold control  ->  no-op control
        (a control failure replaces the task by the next pre-registered replacement BEFORE any agent run, or halts)
    ->  first arm: cost-cap check, run, handoff validation, grading (only if the handoff passed), transcript metrics, results row
    ->  pair gap  ->  second arm (same steps)  ->  remove the task's image (exact ID)  ->  disk check

Stop conditions (all HALT the batch, which can then only continue after a human acknowledges the halt id): incomplete accounting, disk below the start gate or hard
stop, the cumulative cost cap, a hidden-data leak, an exhausted replacement list, two consecutive infrastructure-invalid runs (the registered policy for transient failures), a deterministic handoff setup/integrity rejection or uncertain financial exposure (each halts at once), an invalid grading (diagnosed, never
read as a model failure), and any unexpected exception. The batch never spends unless the definition is LOCKED and the caller's approved spend equals its total cap.

Budgets are soft: the cap check adds the ESTIMATED exposure of the next run (budget plus the interrupted-request output bound) to the cost so far; it is an estimate,
not a guarantee, and the report says so.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Protocol

from .experiment_def import (ExperimentError, apply_replacement, build_schedule, build_staged_schedule, definition_hash, guaranteed_reservation_usd, is_exploratory, require_locked)
from .rejection_policy import ACCOUNTING, HIDDEN_LEAK, INTEGRITY, TRANSIENT, classify_handoff
from .grading import HARD_STOP_GIB, START_GATE_GIB, control_problems
from .results import ResultsWriter

NOT_ACKNOWLEDGEABLE = frozenset({"slot_interrupted", "interrupted", "definition_changed", "definition_invalid", "identity_mismatch", "accounting_incomplete"})  # accounting_incomplete: the cumulative cost may be understated
REQUIRED_STOP_IDS = ("handoff_integrity_failure", "financial_exposure_uncertain")  # registered by definitions from m3-pilot-v2 on
INFRA_STOP_REASONS = frozenset({"proxy_died", "lease_lost", "gateway_unreachable", "gateway_died"})


@dataclass(frozen=True)
class Approvals:
    spend_usd: str | None = None  # must equal limits.max_total_computed_usd to allow any run that calls the API
    pulls: bool = False  # image pull, pinning, seed extraction, identity verification
    controls: bool = False  # run the evaluator on gold / no-op controls
    remove_images: bool = False  # remove each task's image (exact ID) after its block
    acknowledge_halt: str | None = None  # the halt id of a previous halt, acknowledged by a human
    stages: tuple[int, ...] = (1,)  # exploratory definitions: the stages the operator activates (later stages never start unless listed); ignored by feasibility pilots


@dataclass
class ArmResult:
    run_dir: Path | None
    error: str | None = None


class Steps(Protocol):
    def free_gib(self) -> float: ...
    def task_prepared(self, task_id: str) -> bool: ...
    def prepare_task(self, task_id: str) -> dict[str, Any]: ...
    def grade_control(self, task_id: str, kind: str) -> dict[str, Any]: ...
    def run_arm(self, task_id: str, arm: str, identity: dict[str, Any], budget_usd: str) -> ArmResult: ...
    def validate_handoff(self, run_dir: Path, expected_slots: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]: ...
    def grade_run(self, task_id: str, run_dir: Path, slot_id: str) -> dict[str, Any]: ...
    def transcript(self, run_dir: Path, cycle_cap: int) -> dict[str, Any]: ...
    def build_row(self, run_dir: Path, identity: dict[str, Any], handoff: dict[str, Any], grading: dict[str, Any] | None, transcript: dict[str, Any] | None,
                  repo: str) -> dict[str, Any]: ...
    def exposure_estimate_usd(self, budget_usd: str, max_concurrent: int) -> Decimal: ...
    def remove_image(self, task_id: str) -> None: ...
    def image_in_use(self, task_id: str) -> bool: ...
    def sleep(self, seconds: float) -> None: ...


class EarlyStop(RuntimeError):
    """A clean, planned end of an exploratory study: the next pair cannot be funded. Not a halt (nothing is wrong, nothing needs acknowledging); the remaining pairs are reported as not run."""

    def __init__(self, stop_id: str, detail: str):
        super().__init__(f"{stop_id}: {detail}")
        self.stop_id, self.detail = stop_id, detail


def pair_reservation_usd(definition: Mapping[str, Any], arms: int) -> Decimal:
    """What a pair (``arms`` runs still to start) reserves from the experiment cap: per run, the gateway budget PLUS the definition-held, PROVISIONAL overshoot allowance for a request that is already
    in flight when the soft budget is reached. A planning figure, never a guaranteed maximum."""
    lim = definition["limits"]
    return arms * (Decimal(lim["budget_usd_per_run"]) + Decimal(lim["overshoot_allowance_usd"]))


def validate_stage_selection(definition: Mapping[str, Any], stages: Any) -> tuple[int, ...]:
    """The activated stages as a sorted unique tuple. Exploratory definitions: a PREFIX of their stages (1, or 1 and 2, ...): a stage can never run while an earlier one is skipped. Feasibility pilots
    have no optional stages and accept only (1,). Raises BatchHalt('stage_selection_invalid') before any state is touched."""
    try:
        if isinstance(stages, (str, bytes)) or any(isinstance(x, bool) or int(x) != x for x in stages):
            raise ValueError
        sel = tuple(sorted({int(x) for x in stages}))
    except (TypeError, ValueError):
        raise BatchHalt("stage_selection_invalid", f"stages must be integers, got {stages!r}") from None
    allowed = [int(x["stage"]) for x in definition["design"]["stages"]] if is_exploratory(definition) else [1]
    if not sel or list(sel) != allowed[:len(sel)]:
        raise BatchHalt("stage_selection_invalid", f"stages {list(sel)} are not a prefix of this definition's stages {allowed}: a later stage cannot be activated without every earlier one"
                        + ("" if is_exploratory(definition) else " (a feasibility pilot has no optional stages)"))
    return sel


def _conditional_tasks(definition: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    if not is_exploratory(definition):
        return {}
    return {c["task"]: c for s in definition["design"]["stages"] for c in s.get("conditional_tasks") or []}


def record_compatibility_decision(definition: Mapping[str, Any], state_path: Path, task: str, *, compatible: bool, evidence: str, decided_by: str, decided_at: str | None = None) -> dict[str, Any]:
    """Record the operator's compatibility decision for a conditional task in the batch state. Only for a conditional task, only BEFORE any run of it, immutable once written, never from A/B outcomes
    (the record says so). An incompatible decision makes the batch replace the task under the pre-registered rule before anything is prepared or run."""
    cond = _conditional_tasks(definition).get(task)
    if cond is None:
        raise ExperimentError(f"{task} is not a conditional task of {definition['experiment_id']}")
    if not isinstance(compatible, bool) or not str(evidence).strip() or not str(decided_by).strip():
        raise ExperimentError("a decision needs compatible (true/false), the evidence inspected and who decided")
    store = StateStore(state_path, definition)
    st = store.state
    st.setdefault("decisions", {})
    if task in st["decisions"]:
        raise ExperimentError(f"the decision for {task} is already recorded and is immutable")
    if any(v for v in (st["blocks"].get(task) or {}).get("slots", {}).values()):
        raise ExperimentError(f"{task} already has a run: the decision must be recorded before any agent run")
    rec = {"task": task, "condition_id": cond["condition_id"], "compatible": compatible, "operator_decision": "include" if compatible else "replace", "evidence": str(evidence),
           "decided_by": str(decided_by), "decided_at": decided_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "uses_ab_outcomes": False}
    st["decisions"][task] = rec
    store.event("compatibility_decision", **{k: v for k, v in rec.items() if k != "evidence"})
    store.save()
    return rec


class BatchHalt(RuntimeError):
    def __init__(self, stop_id: str, detail: str):
        super().__init__(f"{stop_id}: {detail}")
        self.stop_id, self.detail = stop_id, detail


class StateStore:
    """The batch's durable state: which blocks and slots are done, replacements, cumulative cost, and any halt. Atomic writes; a halted state needs an acknowledgement."""

    def __init__(self, path: Path, definition: Mapping[str, Any]):
        self.path = Path(path)
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            if self.state.get("definition_sha256") != definition["definition_sha256"]:
                raise BatchHalt("definition_changed", "the state belongs to a different definition (hash differs): start a new experiment id instead of editing")
        else:
            self.state = {"schema": 1, "experiment_id": definition["experiment_id"], "definition_sha256": definition["definition_sha256"], "status": "idle", "halt": None,
                          "replacements": [], "blocks": {}, "cumulative_cost_usd": "0", "consecutive_infra_invalid": 0, "events": [], "not_run": [], "reservations": [], "decisions": {},
                          "stage_activations": []}
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.path)

    def event(self, what: str, **kw: Any) -> None:
        self.state["events"].append({"t": round(time.time(), 1), "event": what, **kw})
        self.save()


def blocks_of(definition: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The scheduled blocks in execution order: [{task_id, slots:[first, second]}]."""
    out: dict[str, dict[str, Any]] = {}
    for r in definition["design"]["schedule"]:
        out.setdefault(r["task_id"], {"task_id": r["task_id"], "slots": []})["slots"].append(dict(r))
    return sorted(out.values(), key=lambda b: b["slots"][0]["order_index"])


def slot_identity(definition: Mapping[str, Any], slot: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    """The experiment identity of a slot, with the task id substituted when a replacement took the block."""
    eid = definition["experiment_id"]
    return {"experiment_id": eid, "task_id": task_id, "arm": slot["arm"], "repetition": slot["rep"], "pair_id": f"{eid}:{task_id}:r{slot['rep']}",
            "order_index": slot["order_index"], "slot_id": f"{eid}/{task_id}/{slot['arm']}/r{slot['rep']}", "arm_position": slot["arm_position"]}


def plan(definition: Mapping[str, Any], stages: Any = (1,)) -> list[str]:
    """What an execution would do, in order, and what each step needs. Touches nothing."""
    lim = definition["limits"]
    kind = "exploratory study" if is_exploratory(definition) else "feasibility pilot"
    active = validate_stage_selection(definition, stages)
    lines = [f"experiment {definition['experiment_id']} status={definition['status']} ({kind}, active stages {list(active)} of {[x['stage'] for x in definition['design']['stages']] if is_exploratory(definition) else [1]}, {len(blocks_of(definition))} blocks, {len(definition['design']['schedule'])} scheduled runs)",
             f"limits: wall {lim['wall_clock_s']} s per run, budget per run {lim['budget_usd_per_run']}, total cap {lim['max_total_computed_usd']}, "
             f"gateway max concurrent {lim['gateway_max_concurrent']}, start gate {START_GATE_GIB:.0f} GiB, hard stop {HARD_STOP_GIB:.0f} GiB"]
    for b in blocks_of(definition):
        s1, s2 = b["slots"]
        lines.append(f"block {b['task_id']}: [approve pulls] pull, pin image, extract seed, verify identity -> [approve controls] gold control, no-op control (replace on failure) "
                     f"-> {s1['arm']} (order {s1['order_index']}) -> wait {definition['design']['pair_gap_s']} s -> {s2['arm']} (order {s2['order_index']}) "
                     "-> handoff validation + grading per run -> results row -> [approve] remove image")
    lines.append("stop conditions: " + ", ".join(c["id"] for c in definition["stop_conditions"]))
    return lines


def run_pilot(definition: Mapping[str, Any], steps: Steps, *, state_path: Path, results_path: Path, approvals: Approvals, execute: bool = False,
              tasks_meta: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Run (or, with ``execute=False``, only validate and plan) the pilot. Returns the final state. Never raises for a stop condition: it records the halt."""
    if not execute:
        return {"planned": plan(definition, approvals.stages), "status": "planned"}
    _check_registered(definition)
    exploratory = is_exploratory(definition)
    stages = validate_stage_selection(definition, approvals.stages)  # before any state exists or changes: stage 2 alone, or a skipped stage, never starts
    pre = definition["preregistration"]
    repos = {t["instance_id"]: t["repo"] for t in pre["tasks"]} | {r["instance_id"]: r["repo"] for r in pre["replacements"]}
    repos.update(tasks_meta or {})
    lim = definition["limits"]
    store = StateStore(state_path, definition)
    st = store.state
    for k, v in (("not_run", []), ("reservations", []), ("decisions", {}), ("stage_activations", [])):
        st.setdefault(k, v)  # states written before these existed stay readable
    writer = ResultsWriter(results_path)
    if st["status"] == "halted":
        if (st["halt"] or {}).get("id") in NOT_ACKNOWLEDGEABLE:
            raise BatchHalt("halted", f"{st['halt']}: this halt cannot be acknowledged (an unrecorded run may have spent money, or the cost record may be incomplete); resolve it by hand and start a new experiment id if needed")
        if approvals.acknowledge_halt != (st["halt"] or {}).get("id"):
            raise BatchHalt("halted", f"the batch is halted ({st['halt']}); acknowledge it with --ack-halt {(st['halt'] or {}).get('id')} after review")
        store.event("halt_acknowledged", halt=st["halt"])
        st["halt"], st["status"] = None, "idle"
    try:
        require_locked(definition)
        if approvals.spend_usd != lim["max_total_computed_usd"]:
            raise ExperimentError("--approve-spend must equal the definition's max_total_computed_usd exactly")
    except ExperimentError as e:
        raise BatchHalt("not_authorised", str(e)) from e
    st["status"] = "running"
    st.pop("early_stop", None)  # a previous early stop is history (in the events), not the current state
    st["stage_activations"].append({"t": round(time.time(), 1), "stages": list(stages)})
    store.event("batch_start", stages=list(stages))
    store.save()
    try:
        for block in blocks_of(definition):
            if exploratory and block["slots"][0].get("stage", 1) not in stages:
                continue  # a later stage never starts on its own: it needs the operator's --stages
            _run_block(definition, steps, store, writer, approvals, repos, block)
        _summarize(definition, st, stages)
        st["status"] = "complete" if not st["not_run"] else "complete_with_unrun_stages"
        store.event("complete", not_run=[n["task_id"] for n in st["not_run"]])
    except EarlyStop as e:
        st["status"], st["early_stop"] = "stopped_early", {"id": e.stop_id, "detail": e.detail}
        store.event("early_stop", id=e.stop_id, detail=e.detail)
        _summarize(definition, st, stages)
    except BatchHalt as h:
        st["status"], st["halt"] = "halted", {"id": h.stop_id, "detail": h.detail}
        store.event("halt", id=h.stop_id, detail=h.detail)
        _summarize(definition, st, stages)
    except Exception as e:  # any unexpected error halts: never swallowed, never continued past
        st["status"], st["halt"] = "halted", {"id": "orchestrator_error", "detail": f"{type(e).__name__}: {e}"}
        store.event("halt", id="orchestrator_error", detail=f"{type(e).__name__}: {e}")
        _summarize(definition, st, stages)
    except BaseException as e:  # KeyboardInterrupt / SystemExit: record the halt (the in-flight slot has no result row and is re-run only after review), then re-raise
        st["status"], st["halt"] = "halted", {"id": "interrupted", "detail": type(e).__name__}
        store.event("halt", id="interrupted", detail=type(e).__name__)
        _summarize(definition, st, stages)
        store.save()
        raise
    store.save()
    return st


def _summarize(definition: Mapping[str, Any], st: dict[str, Any], stages: tuple[int, ...]) -> None:
    """Rebuild, from the state as it is NOW, which tasks are completed, which are not run (and why), which pairs are incomplete and which stages stopped. Rebuilt on every exit path, so nothing stale
    survives a later activation or resume."""
    exploratory = is_exploratory(definition)
    completed: list[str] = []
    unrun: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    for block in blocks_of(definition):
        orig, stage = block["task_id"], block["slots"][0].get("stage")
        b = st["blocks"].get(orig)
        if b and b["status"] == "done":
            completed.append(orig)
            continue
        slots = (b or {}).get("slots", {})
        done, started = sum(v == "done" for v in slots.values()), sum(v == "started" for v in slots.values())
        if done or started:
            incomplete.append({"task_id": orig, "stage": stage, "runs_with_rows": done, "runs_started_without_row": started})
        if exploratory and stage not in stages:
            reason = "stage_not_activated"
        elif st["status"] == "stopped_early":
            reason = "pair_budget_unfunded"
        elif st["status"] == "halted":
            reason = f"halted:{(st.get('halt') or {}).get('id')}"
        else:
            reason = "not_started"
        unrun.append({"task_id": orig, "stage": stage, "reason": reason})
    st["not_run"] = unrun
    st["summary"] = {"completed_tasks": completed, "unrun_tasks": [u["task_id"] for u in unrun], "incomplete_pairs": incomplete, "activated_stages": list(stages),
                     "stopped_stages": sorted({u["stage"] for u in unrun if u["reason"] != "stage_not_activated" and u["stage"] is not None}),
                     "unactivated_stages": sorted({u["stage"] for u in unrun if u["reason"] == "stage_not_activated" and u["stage"] is not None})}


def _check_registered(definition: Mapping[str, Any]) -> None:
    """The batch executes the REGISTERED schedule: the definition must still hash to its own recorded hash and its schedule must equal the one derived from the
    seed and tasks. Anything else halts before a single step runs (a driver that reconstructed or edited the schedule would fail here)."""
    try:
        if definition_hash(definition) != definition.get("definition_sha256"):
            raise ExperimentError("the definition no longer matches its recorded hash")
        pre = definition["preregistration"]
        have = {c.get("id") for c in definition.get("stop_conditions") or []}
        if not set(REQUIRED_STOP_IDS) <= have:
            raise ExperimentError(f"the definition does not register the stop conditions {sorted(set(REQUIRED_STOP_IDS) - have)}: it predates the M3.7 batch policy and cannot be executed")
        if is_exploratory(definition):
            derived = build_staged_schedule(definition["experiment_id"], definition["design"]["stages"], pre["seed"], definition["design"]["repetitions"])
            if not definition["limits"].get("pair_atomic_reservation") or definition["limits"].get("overshoot_allowance_usd") in (None, ""):
                raise ExperimentError("an exploratory definition must hold pair_atomic_reservation and an overshoot allowance")
            need, cap = guaranteed_reservation_usd(definition), definition["limits"].get("max_total_computed_usd")
            if need is not None and cap not in (None, "") and Decimal(cap) < need:
                raise ExperimentError(f"the cap {cap} is below the guaranteed Stage 1 reservation {format(need, 'f')} (required pairs x 2 x (budget + overshoot allowance))")
        else:
            derived = build_schedule(definition["experiment_id"], [t["instance_id"] for t in pre["tasks"]], pre["seed"], definition["design"]["repetitions"])
        if derived != definition["design"]["schedule"]:
            raise ExperimentError("the schedule is not the one derived from the registered seed and tasks")
    except (ExperimentError, KeyError, TypeError) as e:
        raise BatchHalt("definition_invalid", str(e)) from e


def _gate(steps: Steps, *, starting: bool) -> None:
    free = steps.free_gib()
    if free < HARD_STOP_GIB:
        raise BatchHalt("disk_hard_stop", f"free disk {free:.2f} GiB is below the {HARD_STOP_GIB:.0f} GiB hard stop")
    if starting and free < START_GATE_GIB:
        raise BatchHalt("disk_start_gate", f"free disk {free:.2f} GiB is below the {START_GATE_GIB:.0f} GiB start gate: a block does not start")


def _run_block(definition: Mapping[str, Any], steps: Steps, store: StateStore, writer: ResultsWriter, ap: Approvals, repos: Mapping[str, str], block: Mapping[str, Any]) -> None:
    st = store.state
    original = block["task_id"]
    bstate = st["blocks"].setdefault(original, {"status": "pending", "task_id": original, "controls": {}, "slots": {}, "pulled": {}, "image_removed": False})
    if bstate["status"] == "done":
        if ap.remove_images and not bstate["image_removed"]:
            _remove_image(steps, store, bstate, bstate["task_id"])  # recovery: a previous removal failed or was interrupted
        return
    task = bstate["task_id"]
    exploratory = is_exploratory(definition)
    if exploratory:  # pair-atomic: fund BOTH arms (those not yet done) from the remaining cap before anything is prepared or started; an unfunded pair is never begun
        lim0 = definition["limits"]
        pending = [x for x in block["slots"] if bstate["slots"].get(slot_identity(definition, x, task)["slot_id"]) != "done"]
        need = pair_reservation_usd(definition, len(pending))
        remaining = Decimal(lim0["max_total_computed_usd"]) - Decimal(st["cumulative_cost_usd"])
        funded = need <= remaining
        snap = {"arms_reserved": len(pending), "reservation_usd": format(need, "f"), "remaining_cap_usd": format(remaining, "f"), "funded": funded}
        rec = next((r for r in st["reservations"] if r["task"] == original), None)
        if rec is None:  # the AUTHORITATIVE record: exactly one per pair, written the first time the pair is considered
            rec = {"task": original, "stage": block["slots"][0].get("stage"), **snap, "budget_usd_per_run": lim0["budget_usd_per_run"], "overshoot_allowance_usd": lim0["overshoot_allowance_usd"],
                   "allowance_status": "PROVISIONAL, not a guaranteed maximum", "continuations": []}
            st["reservations"].append(rec)
            store.event("pair_reservation", **{k: v for k, v in rec.items() if k != "continuations"})
        else:  # a resume (after an acknowledged halt or a re-invocation): a LINKED continuation of that record, never a second reservation for the pair
            cont = {"t": round(time.time(), 1), "of": original, **snap, "arms_done_before": 2 - len(pending)}
            rec["continuations"].append(cont)
            store.event("pair_reservation_continuation", **cont)
        if not funded:
            store.save()
            raise EarlyStop("pair_budget_unfunded", f"{original}: reserving {format(need, 'f')} for {len(pending)} run(s) exceeds the remaining cap {format(remaining, 'f')} (ledger cost so far "
                                                    f"{st['cumulative_cost_usd']}); the pair is not started and the study ends early")
        store.save()
    _gate(steps, starting=True)
    while True:  # task preparation and controls; a control failure replaces the task before any agent run
        failed = _compat_failures(definition, st, task)  # a conditional task needs its recorded compatibility decision BEFORE anything is prepared or run
        if not failed:
            if not steps.task_prepared(task):
                if not ap.pulls:
                    raise BatchHalt("task_not_prepared", f"{task} is not pinned (image, seed, identity) and pulls/pins were not approved")
                store.event("prepare_task", task=task)
                info = steps.prepare_task(task) or {}
                bstate["pulled"][task] = bool(info.get("pulled"))  # only an image THIS batch pulled is ever removed by it
                store.save()
            for kind in ("gold", "noop"):
                done = bstate["controls"].get(task, {}).get(kind)
                if done is None:
                    if not ap.controls:
                        raise BatchHalt("controls_not_approved", f"the {kind} control of {task} must run before any agent run and was not approved")
                    _gate(steps, starting=False)
                    summary = steps.grade_control(task, kind)
                    if not summary.get("valid"):  # an evaluator/infrastructure failure says nothing about the task: never a reason to replace it
                        store.event("control_invalid", task=task, kind=kind)
                        raise BatchHalt("grading_invalid", f"the {kind} control of {task} could not be graded validly (harness or infrastructure failure): diagnose it; the task is not replaced for this")
                    done = {"problems": control_problems(kind, summary), "summary": {k: summary.get(k) for k in ("run_id", "resolved", "f2p_passed", "f2p_total", "p2p_maintained", "p2p_total", "valid")}}
                    bstate["controls"].setdefault(task, {})[kind] = done
                    store.event("control", task=task, kind=kind, problems=done["problems"])
                failed += [f"{kind}: {p}" for p in done["problems"]]
        if not failed:
            break
        try:
            rep = apply_replacement({"preregistration": definition["preregistration"] | {"tasks": [{"instance_id": t, "repo": repos[t]} for t in _final_tasks(definition, st)]}},
                                    task, already_used=[r["replaced"] for r in st["replacements"]] + [r["replacement"] for r in st["replacements"]])
        except ExperimentError as e:
            raise BatchHalt("control_replacement_exhausted", f"{task} failed its controls ({'; '.join(failed)}) and {e}") from e
        st["replacements"].append({"replaced": task, "replacement": rep["replacement"], "repo": rep["repo"], "reasons": failed, "block": original})
        bstate["task_id"], task = rep["replacement"], rep["replacement"]
        store.event("replacement", **st["replacements"][-1])
        _gate(steps, starting=True)
    lim = definition["limits"]
    for i, slot in enumerate(block["slots"]):
        ident = slot_identity(definition, slot, task)
        if bstate["slots"].get(ident["slot_id"]) == "done":
            continue
        if i > 0:
            steps.sleep(definition["design"]["pair_gap_s"])
        if bstate["slots"].get(ident["slot_id"]) == "started":
            raise BatchHalt("slot_interrupted", f"{ident['slot_id']} was started and has no result row: its cost is unknown, so it is never silently repeated")
        _gate(steps, starting=False)
        exposure = steps.exposure_estimate_usd(lim["budget_usd_per_run"], lim["gateway_max_concurrent"])
        if not exploratory and Decimal(st["cumulative_cost_usd"]) + exposure > Decimal(lim["max_total_computed_usd"]):  # exploratory: the pair was funded up front and is not re-judged between its arms
            raise BatchHalt("cumulative_cost_cap", f"cost so far {st['cumulative_cost_usd']} plus the estimated exposure of the next run {exposure} would exceed the cap "
                                                    f"{lim['max_total_computed_usd']} (an estimate, not a guarantee)")
        bstate["slots"][ident["slot_id"]] = "started"  # durable BEFORE the run: a crash or interrupt leaves a marker, never a silent repeat
        store.event("run_start", slot=ident["slot_id"])
        res = steps.run_arm(task, slot["arm"], ident, lim["budget_usd_per_run"])
        if res.run_dir is None:
            raise BatchHalt("orchestrator_error", f"{ident['slot_id']}: no run directory was produced ({res.error})")
        expected = {s["slot_id"]: s for s in (slot_identity(definition, x, task) for x in block["slots"])}
        handoff = steps.validate_handoff(res.run_dir, expected)
        rejections = " | ".join(handoff.get("rejections") or [])
        grading = None
        if handoff.get("status") in ("ready", "ready_flagged"):
            _gate(steps, starting=False)
            grading = steps.grade_run(task, res.run_dir, ident["slot_id"])
        tmetrics = steps.transcript(res.run_dir, lim["b_max_test_fix_cycles"])
        row = steps.build_row(res.run_dir, ident, handoff, grading, tmetrics, repos[task])
        for k in ("slot_id", "task_id", "arm", "repetition", "pair_id", "order_index", "experiment_id"):
            if k in row and row[k] != ident[k]:
                raise BatchHalt("identity_mismatch", f"{ident['slot_id']}: the result row's {k} is {row[k]!r}, the schedule says {ident[k]!r}")
        writer.append(row)
        bstate["slots"][ident["slot_id"]] = "done"
        st["cumulative_cost_usd"] = format(Decimal(st["cumulative_cost_usd"]) + Decimal(row["cost_usd_computed"]), "f")
        store.event("run_done", slot=ident["slot_id"], handoff=handoff.get("status"), resolved=row.get("resolved"), cost=row["cost_usd_computed"])
        overshoot = None
        if exploratory:  # ledger cost against the gateway budget: the GATEWAY budget is the soft limit, the LEDGER is the truth; the allowance is the planning assumption being tested
            overshoot = Decimal(row["cost_usd_computed"]) - Decimal(lim["budget_usd_per_run"])
            bstate.setdefault("overshoot_usd", {})[ident["slot_id"]] = format(max(overshoot, Decimal(0)), "f")
            store.event("overshoot_check", slot=ident["slot_id"], ledger_cost=row["cost_usd_computed"], gateway_budget=lim["budget_usd_per_run"], overshoot=format(max(overshoot, Decimal(0)), "f"))
            if overshoot > Decimal(lim["overshoot_allowance_usd"]):
                store.event("overshoot_exceeded_allowance", slot=ident["slot_id"], overshoot=format(overshoot, "f"), allowance=lim["overshoot_allowance_usd"])  # visible even if a higher-priority halt is raised
        cls = classify_handoff(handoff)
        infra = (row.get("lifecycle_status") not in (None, "completed")) or (row.get("stop_reason") in INFRA_STOP_REASONS) or cls.has(TRANSIENT)
        st["consecutive_infra_invalid"] = st["consecutive_infra_invalid"] + 1 if infra else 0
        store.save()
        # HALT PRIORITY (highest first): accounting / financial exposure, hidden-data leak, invalid grading, integrity, and only then the overshoot beyond the allowance and the infrastructure streak. A lower-priority
        # halt must never mask a higher one, because acknowledging the visible halt would then let the hidden problem through.
        if "ledger is COMPLETE" in rejections or cls.has(ACCOUNTING) or (row.get("accounting_complete") is False):
            raise BatchHalt("accounting_incomplete", f"{ident['slot_id']}: the gateway ledger is incomplete or the metering failed: usage may have been billed that is not recorded")
        if cls.financial_uncertain:
            raise BatchHalt("financial_exposure_uncertain", f"{ident['slot_id']}: a request was cut off and charged at the max_tokens upper bound, so the cost of this run is an upper-bound "
                                                            "estimate and the budget (a soft limit) may have been exceeded: review before continuing")
        if "agent's INPUT" in rejections or cls.has(HIDDEN_LEAK):
            raise BatchHalt("hidden_data_leak", f"{ident['slot_id']}: hidden task data was found in the agent's input")
        if grading is not None and not grading.get("valid"):
            raise BatchHalt("grading_invalid", f"{ident['slot_id']}: the grading itself was not valid (a harness or infrastructure failure: diagnose, do not read it as a model failure)")
        if cls.has(INTEGRITY):
            why = "; ".join(f"{n[:90]} [{w}]" for c, n, w in cls.items if c == INTEGRITY)
            raise BatchHalt("handoff_integrity_failure", f"{ident['slot_id']}: the handoff was rejected for a deterministic setup or integrity reason that would repeat on every run: {why}")
        if overshoot is not None and overshoot > Decimal(lim["overshoot_allowance_usd"]):
            raise BatchHalt("overshoot_allowance_exceeded", f"{ident['slot_id']}: ledger cost {row['cost_usd_computed']} exceeded the gateway budget {lim['budget_usd_per_run']} by {format(overshoot, 'f')}, more than "
                                                           f"the provisional allowance {lim['overshoot_allowance_usd']}: the planning assumption is false; review before any further pair")
        if st["consecutive_infra_invalid"] >= 2:
            raise BatchHalt("consecutive_invalid_runs", "two consecutive runs were rejected for infrastructure reasons")
    bstate["status"] = "done"
    store.save()
    if ap.remove_images:
        _remove_image(steps, store, bstate, task)
    _gate(steps, starting=False)


def _compat_failures(definition: Mapping[str, Any], st: Mapping[str, Any], task: str) -> list[str]:
    """[] when the task is not conditional or its recorded decision says compatible; a reason when the decision says incompatible (the pre-registered replacement then applies);
    halts when the decision has not been recorded (nothing is prepared, pulled or run until it is)."""
    cond = _conditional_tasks(definition).get(task)
    if cond is None:
        return []
    dec = (st.get("decisions") or {}).get(task)
    if dec is None:
        raise BatchHalt("compatibility_decision_missing", f"{task} is conditional ({cond['condition_id']}): record the operator's compatibility decision (m3_batch.record_compatibility_decision) before any agent run, "
                                                          "then acknowledge this halt")
    return [] if dec["compatible"] else [f"compatibility: recorded as incompatible with the isolated environment (operator {dec['decided_by']}, {dec['decided_at']}; evidence is in the state)"]


def _remove_image(steps: Steps, store: StateStore, bstate: dict[str, Any], task: str) -> None:
    """Remove a task's image only when it is finished with and this batch pulled it. Both slots must have a result row; nothing may be using the image."""
    slots = [k for k in bstate["slots"] if f"/{task}/" in k]
    if len(slots) != 2 or any(bstate["slots"][k] != "done" for k in slots):
        raise BatchHalt("image_still_required", f"{task}: not every scheduled run has a result row, so its image is still required")
    if not bstate["pulled"].get(task):
        store.event("image_kept", task=task, reason="not pulled by this batch")
        bstate["image_removed"] = True
        store.save()
        return
    if steps.image_in_use(task):
        raise BatchHalt("image_still_required", f"{task}: a container still uses the image")
    steps.remove_image(task)
    bstate["image_removed"] = True
    store.event("image_removed", task=task)


def _final_tasks(definition: Mapping[str, Any], st: Mapping[str, Any]) -> list[str]:
    """The current task list: the selected tasks with every recorded replacement applied."""
    tasks = [t["instance_id"] for t in definition["preregistration"]["tasks"]]
    for r in st["replacements"]:
        tasks = [r["replacement"] if t == r["replaced"] else t for t in tasks]
    return tasks

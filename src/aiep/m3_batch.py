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

from .experiment_def import ExperimentError, apply_replacement, build_schedule, definition_hash, require_locked
from .rejection_policy import ACCOUNTING, HIDDEN_LEAK, INTEGRITY, TRANSIENT, classify_handoff
from .grading import HARD_STOP_GIB, START_GATE_GIB, control_problems
from .results import ResultsWriter

NOT_ACKNOWLEDGEABLE = frozenset({"slot_interrupted", "interrupted", "definition_changed", "definition_invalid", "identity_mismatch"})
REQUIRED_STOP_IDS = ("handoff_integrity_failure", "financial_exposure_uncertain")  # registered by definitions from m3-pilot-v2 on
INFRA_STOP_REASONS = frozenset({"proxy_died", "lease_lost", "gateway_unreachable", "gateway_died"})


@dataclass(frozen=True)
class Approvals:
    spend_usd: str | None = None  # must equal limits.max_total_computed_usd to allow any run that calls the API
    pulls: bool = False  # image pull, pinning, seed extraction, identity verification
    controls: bool = False  # run the evaluator on gold / no-op controls
    remove_images: bool = False  # remove each task's image (exact ID) after its block
    acknowledge_halt: str | None = None  # the halt id of a previous halt, acknowledged by a human


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
                          "replacements": [], "blocks": {}, "cumulative_cost_usd": "0", "consecutive_infra_invalid": 0, "events": []}
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


def plan(definition: Mapping[str, Any]) -> list[str]:
    """What an execution would do, in order, and what each step needs. Touches nothing."""
    lim = definition["limits"]
    lines = [f"experiment {definition['experiment_id']} status={definition['status']} (feasibility pilot, {len(blocks_of(definition))} blocks, 16 scored runs)",
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
        return {"planned": plan(definition), "status": "planned"}
    _check_registered(definition)
    pre = definition["preregistration"]
    repos = {t["instance_id"]: t["repo"] for t in pre["tasks"]} | {r["instance_id"]: r["repo"] for r in pre["replacements"]}
    repos.update(tasks_meta or {})
    lim = definition["limits"]
    store = StateStore(state_path, definition)
    st = store.state
    writer = ResultsWriter(results_path)
    if st["status"] == "halted":
        if (st["halt"] or {}).get("id") in NOT_ACKNOWLEDGEABLE:
            raise BatchHalt("halted", f"{st['halt']}: this halt cannot be acknowledged (an unrecorded run may have spent money); resolve it by hand and start a new experiment id if needed")
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
    store.save()
    try:
        for block in blocks_of(definition):
            _run_block(definition, steps, store, writer, approvals, repos, block)
        st["status"] = "complete"
        store.event("complete")
    except BatchHalt as h:
        st["status"], st["halt"] = "halted", {"id": h.stop_id, "detail": h.detail}
        store.event("halt", id=h.stop_id, detail=h.detail)
    except Exception as e:  # any unexpected error halts: never swallowed, never continued past
        st["status"], st["halt"] = "halted", {"id": "orchestrator_error", "detail": f"{type(e).__name__}: {e}"}
        store.event("halt", id="orchestrator_error", detail=f"{type(e).__name__}: {e}")
    except BaseException as e:  # KeyboardInterrupt / SystemExit: record the halt (the in-flight slot has no result row and is re-run only after review), then re-raise
        st["status"], st["halt"] = "halted", {"id": "interrupted", "detail": type(e).__name__}
        store.event("halt", id="interrupted", detail=type(e).__name__)
        raise
    return st


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
    _gate(steps, starting=True)
    while True:  # task preparation and controls; a control failure replaces the task before any agent run
        if not steps.task_prepared(task):
            if not ap.pulls:
                raise BatchHalt("task_not_prepared", f"{task} is not pinned (image, seed, identity) and pulls/pins were not approved")
            store.event("prepare_task", task=task)
            info = steps.prepare_task(task) or {}
            bstate["pulled"][task] = bool(info.get("pulled"))  # only an image THIS batch pulled is ever removed by it
            store.save()
        failed: list[str] = []
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
        if Decimal(st["cumulative_cost_usd"]) + exposure > Decimal(lim["max_total_computed_usd"]):
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
        cls = classify_handoff(handoff)
        infra = (row.get("lifecycle_status") not in (None, "completed")) or (row.get("stop_reason") in INFRA_STOP_REASONS) or cls.has(TRANSIENT)
        st["consecutive_infra_invalid"] = st["consecutive_infra_invalid"] + 1 if infra else 0
        store.save()
        if "ledger is COMPLETE" in rejections or cls.has(ACCOUNTING) or (row.get("accounting_complete") is False):
            raise BatchHalt("accounting_incomplete", f"{ident['slot_id']}: the gateway ledger is incomplete or the metering failed: usage may have been billed that is not recorded")
        if "agent's INPUT" in rejections or cls.has(HIDDEN_LEAK):
            raise BatchHalt("hidden_data_leak", f"{ident['slot_id']}: hidden task data was found in the agent's input")
        if grading is not None and not grading.get("valid"):
            raise BatchHalt("grading_invalid", f"{ident['slot_id']}: the grading itself was not valid (a harness or infrastructure failure: diagnose, do not read it as a model failure)")
        if cls.financial_uncertain:
            raise BatchHalt("financial_exposure_uncertain", f"{ident['slot_id']}: a request was cut off and charged at the max_tokens upper bound, so the cost of this run is an upper-bound "
                                                            "estimate and the budget (a soft limit) may have been exceeded: review before continuing")
        if cls.has(INTEGRITY):
            why = "; ".join(f"{n[:90]} [{w}]" for c, n, w in cls.items if c == INTEGRITY)
            raise BatchHalt("handoff_integrity_failure", f"{ident['slot_id']}: the handoff was rejected for a deterministic setup or integrity reason that would repeat on every run: {why}")
        if st["consecutive_infra_invalid"] >= 2:
            raise BatchHalt("consecutive_invalid_runs", "two consecutive runs were rejected for infrastructure reasons")
    bstate["status"] = "done"
    store.save()
    if ap.remove_images:
        _remove_image(steps, store, bstate, task)
    _gate(steps, starting=False)


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

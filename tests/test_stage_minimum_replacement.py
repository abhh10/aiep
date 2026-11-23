"""A replacement must also keep its OWN STAGE's registered repository minimum (design.stages[i].min_repos). The pre-registered selection order is unchanged and never skips: when the entry the global rules
select would break the stage minimum, ``apply_replacement`` raises ``StageMinimumViolation``, the batch records nothing and halts ``replacement_violates_stage_minimum`` (not acknowledgeable): a human decides.

Everything is mocked or in-memory: no Docker, no network, no task is prepared or replaced for real.
"""
import copy
import json
from collections import deque

import pytest

from aiep import m3_batch as mb
from aiep.experiment_def import ExperimentError, StageMinimumViolation, apply_replacement
from test_e1_exploratory import D13933, D15851, E1_FILE, REQ, SPHINX, SY22714, SYMPY, plain_writer  # noqa: F401 (plain_writer is an autouse fixture)
from test_e1_exploratory import go
from test_m3_integration import World, make_def
from test_preparation_replacement import GATE_FAILURES, PFake, approvals, d20, run, state

D16408, PYLINT, PYTEST, SPHINX_7686 = "django__django-16408", "pylint-dev__pylint-6506", "pytest-dev__pytest-7168", "sphinx-doc__sphinx-7686"
E1 = json.loads(E1_FILE.read_text())
PRE = E1["preregistration"]
REPO = {t["instance_id"]: t["repo"] for t in PRE["tasks"]} | {r["instance_id"]: r["repo"] for r in PRE["replacements"]}
ORIG = [t["instance_id"] for t in PRE["tasks"]]
STAGES = {s["stage"]: s for s in E1["design"]["stages"]}
POS = {n: [ORIG.index(t) for t in s["tasks"]] for n, s in STAGES.items()}  # block positions of each stage in the task list
GATE = GATE_FAILURES["seed_file_over_limit"]


def defn(tasks):
    return {"preregistration": PRE | {"tasks": [{"instance_id": t, "repo": REPO[t]} for t in tasks]}}


def stage_args(tasks, stage):
    return {"stage": stage, "stage_tasks": [tasks[i] for i in POS[stage]], "stage_min_repos": STAGES[stage]["min_repos"]}


def play(failures):
    """Apply the real apply_replacement (no stage context, i.e. the OLD behaviour) to a list of (block position) failures; returns the resulting tasks and used ids exactly as the batch passes them."""
    tasks, used = list(ORIG), set()
    for i in failures:
        r = apply_replacement(defn(tasks), tasks[i], already_used=sorted(used))  # the batch passes already_used = every replaced and replacement id so far
        used |= {tasks[i], r["replacement"]}
        tasks[i] = r["replacement"]
    return tasks, used


# the audit's shortest violating sequence: block 1 (django-13933) fails three times in a row, then block 5 (requests-863) fails
AUDIT = [1, 1, 1]
AUDIT_REQ = 5


# ---------------------------------------------------------------- the stage-aware selection (unit)
def test_a_candidate_that_keeps_the_stage_minimum_is_selected_exactly_as_before_and_reports_its_stage_check():
    tasks, used = list(ORIG), set()
    plain = apply_replacement(defn(tasks), REQ, already_used=sorted(used))
    aware = apply_replacement(defn(tasks), REQ, already_used=sorted(used), **stage_args(tasks, 2))
    assert aware["replacement"] == plain["replacement"] == PYLINT and aware["registry_index"] == plain["registry_index"] and aware["skipped"] == plain["skipped"]
    assert aware["stage_check"] == {"stage": 2, "min_repos": 2, "repos_after": ["pylint-dev/pylint", "sphinx-doc/sphinx"]} and plain["stage_check"] is None


def test_the_audits_four_failure_sequence_raises_instead_of_selecting_a_stage_2_with_one_repository():
    tasks, used = play(AUDIT)
    assert tasks[1] == PYTEST and {D13933, D16408, PYLINT, PYTEST} <= used
    old = apply_replacement(defn(tasks), REQ, already_used=sorted(used))  # without stage context the global rules happily select it
    assert old["replacement"] == SPHINX_7686
    with pytest.raises(StageMinimumViolation) as e:
        apply_replacement(defn(tasks), REQ, already_used=sorted(used), **stage_args(tasks, 2))
    v = e.value
    assert isinstance(v, ExperimentError)  # the batch halts on it before its generic "exhausted" handler
    assert (v.failed, v.candidate, v.repo, v.stage, v.stage_min_repos, v.stage_repos, v.registry_index) == (REQ, SPHINX_7686, "sphinx-doc/sphinx", 2, 2, ["sphinx-doc/sphinx"], old["registry_index"])
    assert "no later entry is tried" in str(v) and "human decision" in str(v)


def test_it_never_skips_to_a_later_registry_entry():
    tasks, used = play(AUDIT)
    # In E1's own registry nothing after sphinx-7686 would pass the global cap, so prove the point on a registry where a later entry WOULD be acceptable:
    doc = copy.deepcopy(defn(tasks))
    registry = {r["instance_id"]: r for r in PRE["replacements"]}
    doc["preregistration"]["replacements"] = [registry[SPHINX_7686], registry[PYLINT]]  # sphinx first (violates the stage minimum), then a perfectly good fallback
    usable = sorted(used - {PYLINT})  # pylint is available again in this constructed registry
    assert apply_replacement(doc, REQ, already_used=usable)["replacement"] == SPHINX_7686  # the global rules choose the first entry
    with pytest.raises(StageMinimumViolation):
        apply_replacement(doc, REQ, already_used=usable, **stage_args(tasks, 2))  # the acceptable later entry is NOT taken: raise, never skip


def test_a_stage_that_stays_diverse_is_not_affected_and_stage_1_cannot_violate():
    # Stage 1: four tasks under the global cap of two per repository always span at least two repositories
    tasks, used = list(ORIG), set()
    for i in (1, 2, 0, 3):
        r = apply_replacement(defn(tasks), tasks[i], already_used=sorted(used), **stage_args(tasks, 1))
        assert len(r["stage_check"]["repos_after"]) >= STAGES[1]["min_repos"]
        used |= {tasks[i], r["replacement"]}
        tasks[i] = r["replacement"]


def test_inconsistent_stage_arguments_are_a_plain_experiment_error_not_a_stage_violation():
    tasks = list(ORIG)
    for kw in ({"stage_tasks": [SPHINX]},                                       # the failed task is not in the stage
               {"stage_tasks": [SPHINX, REQ], "stage_min_repos": 0},            # a minimum must be a positive integer
               {"stage_min_repos": 2},                                          # a minimum without the stage's tasks
               {"stage_tasks": [SPHINX, "unknown__unknown-1"], "stage_min_repos": 2}):
        with pytest.raises(ExperimentError) as e:
            apply_replacement(defn(tasks), REQ, **({"stage": 2} | kw))
        assert not isinstance(e.value, StageMinimumViolation)


def test_a_minimum_of_one_never_blocks_and_a_missing_stage_context_means_the_old_behaviour():
    tasks, used = play(AUDIT)
    ok = apply_replacement(defn(tasks), REQ, already_used=sorted(used), stage=2, stage_tasks=[tasks[i] for i in POS[2]], stage_min_repos=1)
    assert ok["replacement"] == SPHINX_7686 and ok["stage_check"]["repos_after"] == ["sphinx-doc/sphinx"]
    assert apply_replacement(defn(tasks), REQ, already_used=sorted(used))["stage_check"] is None


# ---------------------------------------------------------------- the stage context the batch builds
def test_the_batch_builds_the_stage_context_from_the_current_tasks_and_ignores_feasibility_pilots():
    d = d20()
    blocks = {b["task_id"]: b for b in mb.blocks_of(d)}
    assert mb._stage_context(d, {"replacements": []}, blocks[REQ]) == {"stage": 2, "stage_min_repos": 2, "stage_tasks": [SPHINX, REQ]}
    st = {"replacements": [{"replaced": D13933, "replacement": D16408}, {"replaced": REQ, "replacement": PYLINT}]}
    assert mb._stage_context(d, st, blocks[D13933]) == {"stage": 1, "stage_min_repos": 2, "stage_tasks": [SYMPY, D16408, D15851, SY22714]}
    assert mb._stage_context(d, st, blocks[SPHINX]) == {"stage": 2, "stage_min_repos": 2, "stage_tasks": [SPHINX, PYLINT]}
    v2 = make_def(8)
    assert mb._stage_context(v2, {"replacements": []}, mb.blocks_of(v2)[0]) == {}  # a feasibility pilot has no stages: nothing changes for it


# ---------------------------------------------------------------- the batch: the exact four-failure sequence
def audit_sequence_fake(extra_fail=()):
    chain = {D13933, D16408, PYLINT, *extra_fail}  # three consecutive Stage 1 gate failures (each replacement fails in turn)
    return PFake(fail_when=lambda t: GATE if t in chain else None, cost="0.1")


def run_audit(tmp_path, *, req_trigger):
    d = d20()
    if req_trigger == "compatibility":
        mb.record_compatibility_decision(d, tmp_path / "s.json", REQ, compatible=False, evidence="the tests need a network", decided_by="operator")
        f = audit_sequence_fake()
        return d, f, go(tmp_path, f, d, stages=(1, 2), decide=False)
    f = audit_sequence_fake(extra_fail=(REQ,))
    return d, f, go(tmp_path, f, d, stages=(1, 2))


@pytest.mark.parametrize("req_trigger", ["compatibility", "preparation_gate"])
def test_the_fourth_replacement_halts_without_being_recorded_and_the_history_stays_valid(tmp_path, req_trigger):
    d, f, st = run_audit(tmp_path, req_trigger=req_trigger)
    assert st["status"] == "halted" and st["halt"]["id"] == "replacement_violates_stage_minimum"
    recs = st["replacements"]
    assert [(r["replaced"], r["replacement"]) for r in recs] == [(D13933, D16408), (D16408, PYLINT), (PYLINT, PYTEST)]  # exactly the three Stage 1 replacements; the fourth is NOT recorded
    mb.verify_replacement_history(d, st)  # the history written so far is intact and verifiable
    assert all(r["stage"] == 1 and r["stage_check"]["stage"] == 1 and r["stage_check"]["min_repos"] == 2 and len(r["stage_check"]["repos_after"]) >= 2 for r in recs)
    blocked = [e for e in st["events"] if e["event"] == "replacement_blocked_by_stage_minimum"]
    assert len(blocked) == 1 and blocked[0]["candidate"] == SPHINX_7686 and blocked[0]["stage"] == 2 and blocked[0]["stage_min_repos"] == 2
    assert blocked[0]["stage_repos_after"] == ["sphinx-doc/sphinx"] and blocked[0]["replaced"] == REQ and blocked[0]["trigger"] == req_trigger
    assert st["halt"]["detail"].count(SPHINX_7686) >= 1 and "no later entry is tried" in st["halt"]["detail"]
    assert st["blocks"][REQ]["task_id"] == REQ and st["blocks"][REQ]["slots"] == {}  # the block still holds its registered task; nothing of it started
    assert not any(i["task_id"] in (REQ, SPHINX_7686) for i in f.idents)  # neither the original nor the unacceptable candidate ever ran
    assert not any(e["event"] == "replacement" and e.get("replacement") == SPHINX_7686 for e in st["events"])


def test_the_halt_cannot_be_acknowledged_or_bypassed(tmp_path):
    d, f, st = run_audit(tmp_path, req_trigger="compatibility")
    assert "replacement_violates_stage_minimum" in mb.NOT_ACKNOWLEDGEABLE
    before = (tmp_path / "s.json").read_text()
    for ack in (None, "replacement_violates_stage_minimum", "orchestrator_error"):
        with pytest.raises(mb.BatchHalt, match="cannot be acknowledged"):
            run(tmp_path, PFake(cost="0.1"), d, stages=(1, 2), acknowledge_halt=ack)
    assert (tmp_path / "s.json").read_text() == before  # refusing to continue changes nothing


def test_the_replacement_keeps_every_registered_property_up_to_the_blocked_one(tmp_path):
    d, f, st = run_audit(tmp_path, req_trigger="compatibility")
    sched = [s for s in d["design"]["schedule"] if s["task_id"] == D13933]
    ours = [i for i in f.idents if i["task_id"] == PYTEST]  # the third Stage 1 replacement took the block and ran it
    assert [i["arm"] for i in ours] == [s["arm"] for s in sched] == ["B", "A"]
    assert {i["pair_id"] for i in ours} == {sched[0]["pair_id"]} and [i["order_index"] for i in ours] == [s["order_index"] for s in sched] and {i["repetition"] for i in ours} == {1}


def test_stage_1_replacements_never_halt_for_the_stage_minimum_even_down_the_whole_registry(tmp_path):
    d = d20()
    registry = {r["instance_id"] for r in PRE["replacements"]}
    f = PFake(fail_when=lambda t: GATE if (t == D13933 or t in registry) else None, cost="0.1")
    st = go(tmp_path, f, d)
    assert st["halt"]["id"] == "control_replacement_exhausted"  # exhaustion, never a stage-minimum halt, in Stage 1
    assert not any(e["event"] == "replacement_blocked_by_stage_minimum" for e in st["events"])
    assert all(len(r["stage_check"]["repos_after"]) >= 2 for r in st["replacements"])


def test_a_feasibility_pilot_replacement_has_no_stage_check_and_behaves_as_before(tmp_path):
    world = World(tmp_path)
    d = make_def(8)
    failed = d["design"]["schedule"][0]["task_id"]

    def extract(c, root, task, *, free_bytes):
        from aiep import task_pins as tp
        from aiep.seed import SeedGateError
        if task == failed:
            raise SeedGateError("'x' is larger than the per-file limit", "seed_file_over_limit")
        tp.add_seed_pin(root, task, directory="d", sha256="c" * 64, head="d" * 40, entries=3, image_id=world.img)

    from aiep.m3_steps import RealSteps
    from test_m3_integration import FULL
    st = mb.run_pilot(d, RealSteps(world.deps(d, extract_seed=extract)), state_path=tmp_path / "s.json", results_path=tmp_path / "r.jsonl", approvals=FULL(d["limits"]["max_total_computed_usd"]), execute=True)
    assert st["status"] == "complete" and st["replacements"][0]["stage_check"] is None and st["replacements"][0]["stage"] is None


# ---------------------------------------------------------------- property: every reachable replacement state of E1
def explore(stage_aware):
    """Every state reachable by ANY sequence of gate failures over E1's real tasks and registry. With ``stage_aware`` a move the stage minimum forbids is a dead end (the batch halts there)."""
    start = (tuple(ORIG), frozenset())
    seen, q = {start}, deque([start])
    moves = {"ok": 0, "violation": 0, "exhausted": 0}
    stage1_violations = stage2_violations = 0
    equivalent = True
    while q:
        tasks, used = q.popleft()
        for i in range(6):
            stage = 1 if i in POS[1] else 2
            plain = None
            try:
                plain = apply_replacement(defn(tasks), tasks[i], already_used=sorted(used))
            except ExperimentError:
                pass
            try:
                got = apply_replacement(defn(tasks), tasks[i], already_used=sorted(used), **(stage_args(tasks, stage) if stage_aware else {}))
            except StageMinimumViolation as v:
                moves["violation"] += 1
                stage1_violations += stage == 1
                stage2_violations += stage == 2
                equivalent &= plain is not None and plain["replacement"] == v.candidate  # it raises exactly where the OLD selection would have taken the violating candidate
                continue
            except ExperimentError:
                moves["exhausted"] += 1
                equivalent &= plain is None
                continue
            moves["ok"] += 1
            equivalent &= plain is not None and {k: v for k, v in got.items() if k != "stage_check"} == {k: v for k, v in plain.items() if k != "stage_check"}  # the selection itself is untouched
            nxt = list(tasks)
            nxt[i] = got["replacement"]
            key = (tuple(nxt), frozenset(used | {tasks[i], got["replacement"]}))
            if key not in seen:
                seen.add(key)
                q.append(key)
    return seen, moves, stage1_violations, stage2_violations, equivalent


def stage_distinct(tasks, stage):
    return len({REPO[tasks[i]] for i in POS[stage]})


def test_over_every_reachable_state_the_stage_minimum_holds_and_the_selection_is_otherwise_unchanged():
    old_states, _, _, _, _ = explore(stage_aware=False)
    old_bad = [s for s in old_states if stage_distinct(s[0], 2) < STAGES[2]["min_repos"]]
    assert len(old_states) == 11538 and len(old_bad) == 784  # what the audit found without the check: Stage 2 can end with one repository
    assert not [s for s in old_states if stage_distinct(s[0], 1) < STAGES[1]["min_repos"]]  # ...and Stage 1 never can: four tasks, at most two per repository
    states, moves, s1, s2, equivalent = explore(stage_aware=True)
    assert not [s for s in states if stage_distinct(s[0], 1) < STAGES[1]["min_repos"] or stage_distinct(s[0], 2) < STAGES[2]["min_repos"]]  # no reachable state breaks either stage minimum
    for tasks, _ in states:  # and the global rules still hold everywhere
        repos = [REPO[t] for t in tasks]
        assert max(repos.count(r) for r in set(repos)) <= PRE["selection"]["max_per_repo"] and len(set(repos)) >= PRE["selection"]["min_repos"]
    assert s1 == 0 and s2 > 0 and moves["violation"] == s2  # Stage 1 never raises; Stage 2's minimum IS enforced
    assert equivalent  # wherever the old selection was acceptable it is returned unchanged; wherever it was not, the new code raises for exactly that candidate
    assert len(states) < len(old_states) and moves["ok"] > 0

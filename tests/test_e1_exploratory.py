"""E1: the staged, budget-constrained exploratory definition, its validation, the pair-atomic budget reservation in the batch runner, and the analysis labels. Offline: no Docker, API or evaluator."""
import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from aiep import analysis
from aiep import m3_batch as mb
from aiep.experiment_build import E1_ID, E1_STAGES, build_exploratory_definition
from aiep.experiment_def import (EXPLORATORY_CLAIM_SCOPE, ExperimentError, build_staged_schedule, definition_hash, first_arm_assignment, guaranteed_reservation_usd, require_locked, stage_first_arms,
                                 validate_definition)
from aiep.transcript import PARSER_VERSION

ROOT = Path(__file__).resolve().parents[1]
V2 = json.loads((ROOT / "config/experiments/m3-pilot-v2.json").read_text())
E1_FILE = ROOT / "config/experiments/e1-exploratory-1.json"
SYMPY, D13933, D15851, SY22714, SPHINX, REQ = ("sympy__sympy-18621", "django__django-13933", "django__django-15851", "sympy__sympy-22714", "sphinx-doc__sphinx-10325", "psf__requests-863")


def fresh():
    return build_exploratory_definition(V2)


def rehash(d):
    d["definition_sha256"] = definition_hash(d)
    return d


def locked(budget="1.0", cap="10", allowance="0.30"):
    d = fresh()
    d["status"] = "locked"
    d["limits"]["budget_usd_per_run"], d["limits"]["max_total_computed_usd"], d["limits"]["overshoot_allowance_usd"] = budget, cap, allowance
    return rehash(d)


def problems(d):
    return validate_definition(d, root=ROOT)


APPROVED_BUDGET, APPROVED_CAP = "0.18", "3.84"  # the operator-approved E1 budgets; the BUILDER's template keeps them null (nothing is authorized by the template)


def committed_e1_expected():
    """What the committed E1 file must be: the builder's draft template with ONLY the approved budgets applied, the file's own budget_planning_note (its content is asserted separately) and the
    definition hash recomputed with the canonical implementation. No hash is hard-coded here, so the test cannot go stale when the note or budgets are revised through the proper rehash."""
    d = fresh()
    d["limits"]["budget_usd_per_run"], d["limits"]["max_total_computed_usd"] = APPROVED_BUDGET, APPROVED_CAP
    d["limits"]["budget_planning_note"] = json.loads(E1_FILE.read_text())["limits"]["budget_planning_note"]
    return rehash(d)


# ---------------------------------------------------------------- the definition
def test_the_committed_e1_file_is_the_builders_draft_plus_the_approved_budgets_and_validates():
    on_disk = json.loads(E1_FILE.read_text())
    assert on_disk == committed_e1_expected() and problems(on_disk) == []
    assert on_disk["definition_sha256"] == definition_hash(on_disk)
    assert on_disk["status"] == "draft" and on_disk["kind"] == "exploratory_ab" and on_disk["experiment_id"] == E1_ID != V2["experiment_id"]  # budgets set, NOT locked
    assert on_disk["limits"]["budget_usd_per_run"] == APPROVED_BUDGET and on_disk["limits"]["max_total_computed_usd"] == APPROVED_CAP
    assert fresh()["limits"]["budget_usd_per_run"] is None and fresh()["limits"]["max_total_computed_usd"] is None  # the builder's template stays null
    with pytest.raises(ExperimentError):
        require_locked(on_disk)  # approved budgets authorize nothing: a draft cannot spend


def test_the_budget_planning_note_states_the_approved_figures_and_that_nothing_is_authorized():
    d = json.loads(E1_FILE.read_text())
    note, lim = d["limits"]["budget_planning_note"], d["limits"]
    for must in ("0.18", "3.84", "PROVISIONAL 0.30", "4 pairs x 2 runs x (0.18 + 0.30) = 3.84", "no reservation slack", "do NOT authorize execution", "remains a draft", "not locked", "never raised silently"):
        assert must in note, must
    assert "UNSET" not in note
    assert Decimal(APPROVED_CAP) == guaranteed_reservation_usd(d) == 4 * 2 * (Decimal(APPROVED_BUDGET) + Decimal(lim["overshoot_allowance_usd"]))  # exactly funded: zero slack


def test_e1_states_itself_as_exploratory_not_powered_and_documents_its_limits():
    d = fresh()
    assert d["claim_scope"] == EXPLORATORY_CLAIM_SCOPE and "NOT a statistically powered" in d["claim_scope"]
    text = " ".join(d["study_limitations"]).lower()
    for needle in ("single repetition", "heterogeneous", "ceiling", "spontaneous", "cache", "600 s", "parser", "stop early"):
        assert needle in text or needle in " ".join(d["reporting_rules"]).lower(), needle
    rules = " ".join(d["reporting_rules"]).lower()
    for needle in ("manually", "incomplete, invalid", "unpaired", "early stop"):
        assert needle in rules, needle
    assert d["analysis"]["no_inference"].startswith("no p-values")


def test_the_stage_1_and_stage_2_tasks_and_first_arms_are_the_registered_ones():
    d = fresh()
    s1, s2 = d["design"]["stages"]
    assert s1["tasks"] == [SYMPY, D13933, D15851, SY22714] and s2["tasks"] == [SPHINX, REQ]
    assert s1["first_arm"] == {SYMPY: "A", D13933: "B", D15851: "B", SY22714: "A"} and s2["first_arm"] == {SPHINX: "A", REQ: "B"}
    assert (s1["activation"], s2["activation"]) == ("required", "conditional") and s1["min_repos"] == 2
    first = {}
    for r in d["design"]["schedule"]:
        if r["arm_position"] == 1:
            first[r["task_id"]] = r["arm"]
    assert list(first.values()).count("A") == 3 == list(first.values()).count("B")  # three A-first and three B-first overall
    assert len({t["repo"] for t in d["preregistration"]["tasks"]}) == 4 and len({t["repo"] for t in d["preregistration"]["tasks"][:4]}) >= 2


def test_stage_1_alone_is_balanced_two_and_two_and_stages_run_in_order():
    d = fresh()
    sched = d["design"]["schedule"]
    assert [r["stage"] for r in sched] == [1] * 8 + [2] * 4 and [r["order_index"] for r in sched] == list(range(1, 13))
    f = {r["task_id"]: r["arm"] for r in sched if r["arm_position"] == 1 and r["stage"] == 1}
    assert sorted(f.values()) == ["A", "A", "B", "B"]
    for t in {r["task_id"] for r in sched}:  # both arms of a task are adjacent, first arm first
        rows = [r for r in sched if r["task_id"] == t]
        assert [r["arm_position"] for r in rows] == [1, 2] and rows[1]["order_index"] == rows[0]["order_index"] + 1 and {r["arm"] for r in rows} == {"A", "B"}


@pytest.mark.parametrize("sizes", [(4,), (2, 2), (4, 2), (3, 3), (2, 2, 2), (6,)])
def test_the_staged_schedule_is_generic_and_every_even_stage_is_balanced(sizes):
    ids = [f"repo{i}__x-{i}" for i in range(sum(sizes))]
    stages, k = [], 0
    for n, size in enumerate(sizes, 1):
        stages.append({"stage": n, "tasks": ids[k:k + size]})
        k += size
    sched = build_staged_schedule("e", stages, "seedseedseed", 1)
    assert len(sched) == 2 * sum(sizes) and sched == build_staged_schedule("e", stages, "seedseedseed", 1)  # deterministic
    first = stage_first_arms(stages, "seedseedseed")
    for st in stages:
        arms = list(first[st["stage"]].values())
        if len(arms) % 2 == 0:
            assert arms.count("A") == arms.count("B")
        assert first[st["stage"]] == first_arm_assignment(st["tasks"], "seedseedseed")
    assert [r["stage"] for r in sched] == sorted(r["stage"] for r in sched)  # a later stage never precedes an earlier one


def test_nothing_hard_codes_eight_tasks_or_four_repositories_for_exploratory_definitions():
    d = fresh()
    assert d["preregistration"]["selection"]["n_tasks"] == 6 and d["preregistration"]["selection"]["min_repos"] == 4 and len(d["design"]["schedule"]) == 12
    d["preregistration"]["selection"]["n_tasks"] = 8
    assert any("exactly 8 distinct tasks" in p for p in problems(rehash(d)))


# ---------------------------------------------------------------- validation
def test_repository_count_validation_overall_and_per_stage():
    d = fresh()
    d["preregistration"]["selection"]["min_repos"] = 5
    assert any("at least 5 repositories" in p for p in problems(rehash(d)))
    d = fresh()
    d["design"]["stages"][0]["min_repos"] = 3
    assert any("stage 1: at least 3 repositories" in p for p in problems(rehash(d)))
    d = fresh()
    d["preregistration"]["selection"]["max_per_repo"] = 1
    assert any("at most 1 tasks per repository" in p for p in problems(rehash(d)))


def test_matplotlib_and_scikit_learn_remain_excluded():
    d = fresh()
    assert {"matplotlib/matplotlib", "scikit-learn/scikit-learn"} <= set(d["preregistration"]["selection"]["excluded_repos"])
    assert not any(t["repo"] in ("matplotlib/matplotlib", "scikit-learn/scikit-learn") for t in d["preregistration"]["tasks"] + d["preregistration"]["replacements"])
    bad = fresh()
    bad["preregistration"]["selection"]["excluded_repos"] = []
    assert any("must exclude the matplotlib and scikit-learn" in p for p in problems(rehash(bad)))
    bad = fresh()
    bad["preregistration"]["replacements"].append({"instance_id": "matplotlib__matplotlib-1", "repo": "matplotlib/matplotlib", "image": "x"})
    assert any("neither selected nor replacements" in p for p in problems(rehash(bad)))


def test_stage_structure_is_validated():
    d = fresh()
    d["design"]["stages"][1]["tasks"] = [REQ, SPHINX]
    assert any("exactly the selected tasks" in p for p in problems(rehash(d)))
    d = fresh()
    d["design"]["stages"][0]["first_arm"][SYMPY] = "B"
    assert any("first_arm is not what the seed derives" in p for p in problems(rehash(d)))
    d = fresh()
    d["design"]["schedule"][0], d["design"]["schedule"][2] = d["design"]["schedule"][2], d["design"]["schedule"][0]
    assert any("schedule is not what the seed derives" in p for p in problems(rehash(d)))
    d = fresh()
    d["design"]["stages"][0]["activation"] = "conditional"
    assert any("stage 1: activation" in p for p in problems(rehash(d)))
    d = fresh()
    d["design"]["stages"][1]["activation_rule"] = ""
    assert any("written activation_rule" in p for p in problems(rehash(d)))


def test_the_conditional_task_rule_is_preregistered_and_cannot_depend_on_outcomes():
    c = fresh()["design"]["stages"][1]["conditional_tasks"][0]
    assert c["task"] == REQ and c["depends_on_ab_outcomes"] is False and c["decided_before_any_agent_run"] is True and c["on_failure"] == "replace_from_ordered_list"
    assert "never looks at an A or B outcome" in c["rule"]
    for key, val in (("depends_on_ab_outcomes", True), ("decided_before_any_agent_run", False), ("on_failure", "drop"), ("rule", "")):
        d = fresh()
        d["design"]["stages"][1]["conditional_tasks"][0][key] = val
        assert any("a conditional task needs a rule" in p for p in problems(rehash(d))), key
    assert any("never because of an agent outcome" in r for r in fresh()["preregistration"]["replacement_rules"])


def test_the_overshoot_allowance_is_held_in_the_definition_and_labelled_provisional():
    lim = fresh()["limits"]
    assert lim["overshoot_allowance_usd"] == "0.30" and "PROVISIONAL" in lim["overshoot_allowance_status"] and "not a guaranteed maximum" in lim["overshoot_allowance_status"]
    assert lim["pair_atomic_reservation"] is True and lim["protected_reserve_usd"] == "0.50"
    for mutate, msg in ((lambda l: l.pop("overshoot_allowance_usd"), "overshoot_allowance_usd"), (lambda l: l.update(overshoot_allowance_usd="-1"), "overshoot_allowance_usd"),
                        (lambda l: l.update(overshoot_allowance_status="fine"), "PROVISIONAL"), (lambda l: l.update(pair_atomic_reservation=False), "pair-atomic")):
        d = fresh()
        mutate(d["limits"])
        assert any(msg in p for p in problems(rehash(d))), msg


def test_stage_1_guaranteed_reservation_is_required_not_just_one_pair():
    """Stage 1 = 4 pairs x 2 arms x (gateway budget + overshoot allowance). With budget 1.0 and allowance 0.30 that is 10.40."""
    from aiep.experiment_def import guaranteed_reservation_usd
    assert guaranteed_reservation_usd(locked(budget="1.0", allowance="0.30", cap="10.4")) == Decimal("10.40")
    assert guaranteed_reservation_usd(locked(budget="0.5", allowance="0.30", cap="10")) == Decimal("6.40")
    assert guaranteed_reservation_usd(fresh()) is None  # budgets unset: nothing to guarantee yet
    assert not any("guaranteed Stage 1" in p for p in problems(locked(budget="1.0", cap="10.4")))
    for cap in ("10.39", "2.6", "5.2"):  # one pair (2.6) or two pairs (5.2) is NOT enough
        assert any("below the guaranteed Stage 1 reservation 10.40" in p for p in problems(locked(budget="1.0", cap=cap))), cap
    assert any("below the guaranteed Stage 1 reservation 6.40" in p for p in problems(locked(budget="0.5", cap="6.39")))
    d = fresh()  # a DRAFT with budgets still null stays valid (nothing to compare)
    assert d["limits"]["budget_usd_per_run"] is None and problems(d) == []


def test_the_guarantee_is_a_planning_guarantee_under_the_declared_allowance_and_says_so():
    lim = fresh()["limits"]
    assert "PROVISIONAL" in lim["overshoot_allowance_status"] and "not a guaranteed maximum" in lim["overshoot_allowance_status"]
    assert "never raised silently" in lim["budget_planning_note"]


def test_a_draft_cannot_spend_and_a_locked_one_needs_both_budgets():
    d = fresh()
    d["status"] = "locked"
    assert any("a locked definition needs both budgets" in p for p in problems(rehash(d)))


def test_the_definition_pins_the_transcript_parser_version_and_carries_derivation():
    d = fresh()
    assert d["metrics"]["transcript_parser_version"] == PARSER_VERSION == 2
    d["metrics"]["transcript_parser_version"] = 1
    assert any("parser_version" in p for p in problems(rehash(d)))
    f = fresh()
    assert f["derived_from"]["definition_sha256"] == V2["definition_sha256"] and f["derived_from"]["experiment_id"] == "m3-pilot-v2"


# ---------------------------------------------------------------- the existing definitions are untouched
def test_v1_and_v2_are_unchanged_and_still_validate():
    for name, h in (("m3-pilot-v1", "f2ae65e5"), ("m3-pilot-v2", "590536ec")):
        d = json.loads((ROOT / f"config/experiments/{name}.json").read_text())
        assert d["definition_sha256"].startswith(h) and problems(d) == [] and d["kind"] == "feasibility_pilot" and d["status"] == "draft"
    assert V2["claim_scope"] != EXPLORATORY_CLAIM_SCOPE
    assert analysis.label_for(V2).startswith("FEASIBILITY PILOT: 8 tasks")


# ---------------------------------------------------------------- batch: pair-atomic reservation
class Fake:
    def __init__(self, *, cost="0.5", free=30.0, bad_controls=(), costs=None):
        self.cost, self.free, self.bad_controls, self.costs = cost, free, set(bad_controls), costs or {}
        self.log, self.prepared = [], set()

    def free_gib(self): return self.free
    def task_prepared(self, t): return t in self.prepared
    def prepare_task(self, t): self.prepared.add(t); self.log.append(("prepare", t)); return {"pulled": True}
    def image_in_use(self, t): return False
    def grade_control(self, t, kind):
        if (t, kind) in self.bad_controls:
            return {"valid": True, "resolved": kind == "noop", "patch_applied": True, "f2p_passed": 1, "f2p_total": 2, "p2p_maintained": 1, "p2p_total": 1}
        ok = kind == "gold"
        return {"valid": True, "resolved": ok, "patch_applied": True, "f2p_passed": 2 if ok else 0, "f2p_total": 2, "p2p_maintained": 1, "p2p_total": 1}
    def run_arm(self, t, arm, ident, budget):
        self.log.append(("run", ident["slot_id"]))
        return mb.ArmResult(Path("/x") / ident["slot_id"])
    def validate_handoff(self, rd, exp): return {"status": "ready", "rejections": []}
    def grade_run(self, t, rd, slot): return {"valid": True, "resolved": True}
    def transcript(self, rd, cap): return {}
    def build_row(self, rd, ident, handoff, grading, tm, repo):
        return {"slot_id": ident["slot_id"], "task_id": ident["task_id"], "cost_usd_computed": self.costs.get(ident["slot_id"], self.cost), "resolved": True,
                "accounting_complete": True, "lifecycle_status": "completed", "stop_reason": None}
    def exposure_estimate_usd(self, b, c): return Decimal("1.5")  # the old per-run check would use this; exploratory definitions must not
    def verify_task_identity(self, task, repo): return {"instance_id": task, "repo": repo, "base_commit": "c" * 40}  # a replacement's dataset identity (the real steps read the local snapshot)
    def preparation_state(self, task): return {"image_pinned": task in self.prepared, "seed_pinned": task in self.prepared, "identity_verified": task in self.prepared, "runnable": task in self.prepared, "missing": []}
    def remove_image(self, t): pass
    def sleep(self, s): pass

    def runs(self): return [x[1] for x in self.log if x[0] == "run"]


class W(mb.ResultsWriter):
    def append(self, row):
        with open(self.path, "a") as f:
            f.write(json.dumps(row) + "\n")


@pytest.fixture(autouse=True)
def plain_writer(monkeypatch):
    monkeypatch.setattr(mb, "ResultsWriter", W)


def go(tmp_path, fake, d, stages=(1,), decide=True, name="s.json"):
    """Run the batch. When stage 2 is active the operator's compatibility decision for requests-863 is recorded first (the batch halts without it), unless ``decide`` is False."""
    cap = d["limits"]["max_total_computed_usd"]
    sp = tmp_path / name
    if decide and 2 in stages:
        try:
            mb.record_compatibility_decision(d, sp, REQ, compatible=True, evidence="test: tests inspected, no network needed", decided_by="test operator")
        except Exception:
            pass  # already recorded
    return mb.run_pilot(d, fake, state_path=sp, results_path=tmp_path / "r.jsonl", approvals=mb.Approvals(spend_usd=cap, pulls=True, controls=True, stages=stages), execute=True)


def short(slot):  # "e1-exploratory-1/<task>/<arm>/r1" -> "<task>/<arm>"
    return "/".join(slot.split("/")[1:3])


def test_the_reservation_is_budget_plus_allowance_for_both_arms():
    assert mb.pair_reservation_usd(locked(budget="1.0", allowance="0.30"), 2) == Decimal("2.60") and mb.pair_reservation_usd(locked(budget="1.0", allowance="0.30"), 1) == Decimal("1.30")


def test_stage_1_runs_four_pairs_both_arms_each_and_stage_2_does_not_start_unless_activated(tmp_path):
    f = Fake(cost="0.1")
    st = go(tmp_path, f, locked(cap="20"))
    assert len(f.runs()) == 8 and st["status"] == "complete_with_unrun_stages"
    assert {n["task_id"] for n in st["not_run"]} == {SPHINX, REQ} and all(n["reason"] == "stage_not_activated" for n in st["not_run"])
    assert [short(s) for s in f.runs()][:2] == [f"{SYMPY}/A", f"{SYMPY}/B"] and not any(SPHINX in p or REQ in p for p in (x for x in f.runs()))
    assert all(k[1] != SPHINX and k[1] != REQ for k in f.log if k[0] == "prepare")


def test_activating_stage_2_runs_all_six_pairs_in_order(tmp_path):
    f = Fake(cost="0.1")
    st = go(tmp_path, f, locked(cap="20"), stages=(1, 2))
    assert len(f.runs()) == 12 and st["status"] == "complete" and st["not_run"] == []
    assert [short(s) for s in f.runs()[-4:]] == [f"{SPHINX}/A", f"{SPHINX}/B", f"{REQ}/B", f"{REQ}/A"]  # stage 2 after stage 1; requests is B first


def test_an_unfundable_pair_is_never_started_and_the_study_ends_early_and_cleanly(tmp_path):
    """Stage 1 is guaranteed (cap 10.40 = exactly its reservation) and each run costs the full allowance edge 1.30; stage 2's first pair then has nothing left to reserve."""
    f = Fake(cost="1.3")
    st = go(tmp_path, f, locked(budget="1.0", cap="10.4"), stages=(1, 2))
    assert st["status"] == "stopped_early" and st["early_stop"]["id"] == "pair_budget_unfunded" and st["halt"] is None
    assert len(f.runs()) == 8 and Decimal(st["cumulative_cost_usd"]) == Decimal("10.4")  # stage 1 complete, never half a pair
    assert {n["task_id"]: n["reason"] for n in st["not_run"]} == {SPHINX: "pair_budget_unfunded", REQ: "pair_budget_unfunded"}
    assert st["summary"]["completed_tasks"] == [SYMPY, D13933, D15851, SY22714] and st["summary"]["stopped_stages"] == [2]
    last = st["reservations"][-1]
    assert last["funded"] is False and last["reservation_usd"] == "2.60" and last["remaining_cap_usd"] == "0.0" and "PROVISIONAL" in last["allowance_status"]
    assert not any(k[0] == "prepare" and k[1] in (SPHINX, REQ) for k in f.log)  # nothing was even prepared for the unfunded pair


def test_a_cap_below_the_stage_1_reservation_starts_nothing(tmp_path):
    f = Fake()
    with pytest.raises(mb.BatchHalt, match="definition_invalid.*below the guaranteed Stage 1 reservation"):
        go(tmp_path, f, locked(budget="1.0", cap="2.0"))
    assert f.log == [] and not (tmp_path / "s.json").exists()


def test_the_second_arm_is_never_withheld_because_the_first_cost_more_than_expected(tmp_path):
    d = locked(budget="1.0", cap="10.4")
    first = f"{E1_ID}/{D15851}/B/r1"
    f = Fake(cost="1.3", costs={first: "1.3"})  # every run spends the whole allowance edge; the OLD per-run check (cost so far + 1.5 > cap) would have stopped inside the last pair
    st = go(tmp_path, f, d)
    assert len(f.runs()) == 8 and st["status"] == "complete_with_unrun_stages" and st["halt"] is None and st["summary"]["incomplete_pairs"] == []
    assert st["blocks"][SY22714]["status"] == "done" and st["blocks"][D15851]["overshoot_usd"][first] == "0.3"


def test_overshoot_beyond_the_allowance_halts_before_any_further_pair(tmp_path):
    first = f"{E1_ID}/{SYMPY}/A/r1"
    f = Fake(cost="0.1", costs={first: "1.4"})  # 0.4 over the gateway budget, allowance 0.30
    st = go(tmp_path, f, locked(budget="1.0", cap="20"))
    assert st["status"] == "halted" and st["halt"]["id"] == "overshoot_allowance_exceeded" and len(f.runs()) == 1
    assert (tmp_path / "r.jsonl").read_text().count("\n") == 1  # the over-budget run's row was written first: the ledger cost is recorded, not hidden
    assert st["blocks"][SYMPY]["overshoot_usd"][first] == "0.4"
    with pytest.raises(mb.BatchHalt):  # and it cannot simply be resumed
        go(tmp_path, f, locked(budget="1.0", cap="20"))


def test_the_gateway_budget_and_the_ledger_cost_stay_distinct(tmp_path):
    f = Fake(cost="0.05")
    st = go(tmp_path, f, locked(budget="1.0", cap="20"))
    assert st["blocks"][SYMPY]["overshoot_usd"] and all(v == "0" for v in st["blocks"][SYMPY]["overshoot_usd"].values())  # under budget: no overshoot
    assert Decimal(st["cumulative_cost_usd"]) == Decimal("0.05") * 8  # the cap is charged LEDGER cost, never the gateway budget or the reservation
    assert all(r["budget_usd_per_run"] == "1.0" and r["overshoot_allowance_usd"] == "0.30" for r in st["reservations"])


def test_reservations_are_audited_per_pair(tmp_path):
    f = Fake(cost="0.1")
    st = go(tmp_path, f, locked(cap="20"))
    assert [r["task"] for r in st["reservations"]] == [SYMPY, D13933, D15851, SY22714] and all(r["funded"] and r["arms_reserved"] == 2 for r in st["reservations"])


def test_a_control_failure_replaces_a_task_with_an_allowed_replacement_and_keeps_its_arm_order(tmp_path):
    d = locked(cap="20")
    f = Fake(cost="0.1", bad_controls=[(D13933, "gold")])
    st = go(tmp_path, f, d)
    rep = st["replacements"][0]
    assert rep["replaced"] == D13933 and rep["repo"] not in ("matplotlib/matplotlib", "scikit-learn/scikit-learn")
    runs = [short(s) for s in f.runs()]
    assert [r.split("/")[1] for r in runs[2:4]] == ["B", "A"]  # the replacement keeps the replaced block's B-first order


def test_exploratory_definitions_are_checked_against_the_staged_schedule_before_any_step(tmp_path):
    d = locked(cap="20")
    d["design"]["schedule"][0]["arm"] = "B"
    f = Fake()
    with pytest.raises(mb.BatchHalt, match="definition_invalid"):  # raised before any state exists or any step runs
        go(tmp_path, f, rehash(d))
    assert f.log == []


def test_a_draft_exploratory_definition_cannot_spend(tmp_path):
    d = fresh()
    with pytest.raises(mb.BatchHalt):
        mb.run_pilot(d, Fake(), state_path=tmp_path / "s.json", results_path=tmp_path / "r.jsonl", approvals=mb.Approvals(spend_usd="1"), execute=True)
    assert mb.run_pilot(d, Fake(), state_path=tmp_path / "s2.json", results_path=tmp_path / "r2.jsonl", approvals=mb.Approvals(), execute=False)["status"] == "planned"


def test_the_plan_describes_an_exploratory_study(tmp_path):
    assert "exploratory study" in mb.plan(fresh())[0] and "12 scheduled runs" in mb.plan(fresh())[0]


# ---------------------------------------------------------------- analysis
def row(task, arm, *, resolved=True, cost="0.05", claude="0.05", **extra):
    base = {"task_id": task, "arm": arm, "repetition": 1, "repo": task.split("__")[0], "resolved": resolved, "graded": True, "valid_measurement": True, "handoff_status": "ready",
            "cost_usd_computed": cost, "uncached_equivalent_usd": cost, "claude_reported_cost_usd": claude, "cost_exact": True, "requests": 5, "wall_seconds": 30.0, "turns": 5,
            "tokens": {}, "test_usage": False, "compliance": False, "test_runs": 0, "test_fix_cycles": 0, "edits_total": 1, "stop_reason": "end_turn", "transcript_parser_version": 2,
            "test_runs_started": 0, "test_runs_not_started": 0, "test_runs_failed": 0, "bash_edit_like_calls": 0, "test_fix_cycles_confirmed_edit": 0, "ambiguous_sequences": 0, "transcript_complete": True}
    return base | extra


def rows():
    return [row(SYMPY, "A"), row(SYMPY, "B", cost="0.09", test_runs=2, test_runs_started=1, test_runs_not_started=1, test_runs_failed=1, test_fix_cycles=1, test_fix_cycles_confirmed_edit=0,
                                  bash_edit_like_calls=1, ambiguous_sequences=1),
            row(D13933, "A", resolved=False), row(D13933, "B"), row(D15851, "B")]  # the last is an unpaired run; sy22714 is not run at all


def test_analysis_reads_label_and_task_count_from_the_definition_and_reports_no_inference():
    rep = analysis.analyze(rows(), fresh())
    assert rep["label"] == EXPLORATORY_CLAIM_SCOPE and rep["exploratory"] is True and rep["tasks_scheduled"] == 6
    o = rep["as_scheduled"]["outcomes"]
    assert o["mcnemar_exact_two_sided_p"] is None and o["bootstrap_95_interval"] is None and "no p-value" in o["interpretation"]
    md = analysis.render_markdown(rep)
    assert "McNemar" not in md and "bootstrap" not in md and "NOT a statistically powered" in md
    assert (o["pairs"], len(o["cells"]["both_resolved"]), len(o["cells"]["B_only"])) == (2, 1, 1)


def test_analysis_shows_stages_unpaired_runs_and_missing_observations():
    rep = analysis.analyze(rows(), fresh())
    s1, s2 = rep["stages"]
    assert s1["paired"] == sorted([SYMPY, D13933]) and set(s1["not_run"]) == {D15851, SY22714} and s2["not_run"] == sorted([SPHINX, REQ])
    assert rep["unpaired_runs"] == [{"task_id": D15851, "arm": "B"}] and {n["task_id"] for n in rep["not_run_tasks"]} == {D15851, SY22714, SPHINX, REQ}
    assert rep["limitations"] and rep["reporting_rules"]
    assert "unpaired runs" in analysis.render_markdown(rep)


def test_analysis_reports_per_task_transcript_detail_costs_and_parser_version_separately():
    rep = analysis.analyze(rows(), fresh())
    b = next(t for t in rep["task_table"] if t["task_id"] == SYMPY)["B_detail"]
    assert (b["test_attempts"], b["test_runs_started"], b["test_runs_not_started"], b["test_runs_failed"], b["edits_confirmed"], b["bash_edit_like_calls"]) == (2, 1, 1, 1, 1, 1)
    assert (b["test_fix_cycles"], b["cycles_confirmed_edit"], b["cycles_bash_edit_only"], b["ambiguous_sequences"], b["transcript_parser_version"]) == (1, 0, 1, 1, 2)
    assert b["gateway_cost_usd"] == "0.09" and b["claude_reported_cost_usd"] == "0.05"
    arm = rep["as_scheduled"]["per_arm"]["B"]
    assert arm["test_fix_cycles_bash_edit_only_total"] == 1 and arm["ambiguous_sequences_total"] == 1 and arm["test_runs_started_total"] == 1 and arm["rows_without_parser_v2_fields"] == 0
    assert rep["transcript_parser_versions"] == {"2": 5}
    assert rep["as_scheduled"]["gateway_vs_claude_reported"]["rows_with_both"] == 4 and "claude_reported_cost_usd_total" in arm  # two separate measurements


def test_rows_without_version_2_fields_are_disclosed_not_guessed():
    old = row(SYMPY, "A") | {"transcript_parser_version": None}
    old.pop("test_runs_started")
    rep = analysis.analyze([old, row(SYMPY, "B")], fresh())
    assert rep["as_scheduled"]["per_arm"]["A"]["rows_without_parser_v2_fields"] == 1 and rep["transcript_parser_versions"] == {"None": 1, "2": 1}


def test_a_feasibility_pilot_still_gets_its_p_value_and_label():
    pilot_rows = [row(t["instance_id"], arm) for t in V2["preregistration"]["tasks"] for arm in "AB"]
    rep = analysis.analyze(pilot_rows, V2)
    assert rep["exploratory"] is False and rep["label"].startswith("FEASIBILITY PILOT: 8 tasks") and rep["as_scheduled"]["outcomes"]["mcnemar_exact_two_sided_p"] is not None
    assert "stages" not in rep and "McNemar" in analysis.render_markdown(rep)


def test_calibration_rows_are_excluded_from_e1_analysis():
    with pytest.raises(ValueError, match="calibration"):
        analysis.analyze([row(SYMPY, "A") | {"calibration": {"id": "m3-calibration-2"}}], fresh())
    with pytest.raises(ValueError, match="calibration"):
        analysis.analyze([row(SYMPY, "A") | {"experiment_id": "m3-calibration-1"}], fresh())


def test_result_rows_from_the_real_parser_carry_the_version_the_definition_pins():
    from aiep.transcript import parse_transcript
    m = parse_transcript(json.dumps({"type": "result", "subtype": "success", "num_turns": 1, "total_cost_usd": 0.01}) + "\n", cycle_cap=5)
    assert m["parser_version"] == fresh()["metrics"]["transcript_parser_version"]


# ================================================================ review fixes: halt priority, bookkeeping, stage prefix, reservation audit, selection evidence, driver
LEDGER_REJ = "the gateway ledger is COMPLETE: every request it started forwarding has a finishing line"
LEAK_REJ = "the agent's INPUT contains no hidden task data"
EXACT_REJ = "the usage figures are EXACT (no request was cut off and charged at the max_tokens upper bound)"
SEED_REJ = "the pinned seed's HEAD equals the recorded seed head, and its parent is the dataset base_commit (x)"


class Scripted(Fake):
    """The FIRST run of the batch is rejected with the given handoff rejections and/or graded invalid, and costs the given amount; every other run is clean and cheap."""

    def __init__(self, *, rejections=(), flags=(), valid_grade=True, first_cost="1.4", **kw):
        super().__init__(cost="0.1", **kw)
        self.rejections, self.flags, self.valid_grade, self.first_cost, self.n = list(rejections), list(flags), valid_grade, first_cost, 0

    def run_arm(self, t, arm, ident, budget):
        self.n += 1
        if self.n == 1:
            self.costs[ident["slot_id"]] = self.first_cost
        return super().run_arm(t, arm, ident, budget)

    def validate_handoff(self, rd, exp):
        if self.n == 1 and self.rejections:
            return {"status": "rejected", "rejections": list(self.rejections)}
        if self.n == 1 and self.flags:
            return {"status": "ready_flagged", "rejections": [], "flags": list(self.flags)}
        return {"status": "ready", "rejections": []}

    def grade_run(self, t, rd, slot): return {"valid": self.valid_grade if self.n == 1 else True, "resolved": True}


def state_events(st, name):
    return [e for e in st["events"] if e["event"] == name]


@pytest.mark.parametrize("rejections,flags,valid_grade,halt", [
    ([LEDGER_REJ], [], True, "accounting_incomplete"),
    ([], [EXACT_REJ], True, "financial_exposure_uncertain"),
    ([LEAK_REJ], [], True, "hidden_data_leak"),
    ([], [], False, "grading_invalid"),
    ([SEED_REJ], [], True, "handoff_integrity_failure"),
])
def test_an_overshoot_never_masks_a_higher_priority_halt(tmp_path, rejections, flags, valid_grade, halt):
    d = locked(budget="1.0", cap="20")
    f = Scripted(rejections=rejections, flags=flags, valid_grade=valid_grade, first_cost="1.4")  # 0.4 over the budget: beyond the 0.30 allowance as well
    st = go(tmp_path, f, d)
    assert st["status"] == "halted" and st["halt"]["id"] == halt, st["halt"]
    assert state_events(st, "overshoot_exceeded_allowance") and st["blocks"][SYMPY]["overshoot_usd"]  # the overshoot is recorded and visible in the audit trail anyway
    assert len(f.runs()) == 1 and (tmp_path / "r.jsonl").read_text().count("\n") == 1
    with pytest.raises(mb.BatchHalt, match="halted"):  # acknowledging the LOWER-priority overshoot cannot release the real halt
        mb.run_pilot(d, f, state_path=tmp_path / "s.json", results_path=tmp_path / "r.jsonl", approvals=mb.Approvals(spend_usd="20", pulls=True, controls=True,
                                                                                                                     acknowledge_halt="overshoot_allowance_exceeded"), execute=True)
    assert json.loads((tmp_path / "s.json").read_text())["halt"]["id"] == halt and len(f.runs()) == 1


def test_overshoot_alone_still_halts_with_its_own_id(tmp_path):
    st = go(tmp_path, Scripted(first_cost="1.4"), locked(budget="1.0", cap="20"))
    assert st["halt"]["id"] == "overshoot_allowance_exceeded"


def test_accounting_incomplete_cannot_be_acknowledged_or_resumed(tmp_path):
    d = locked(budget="1.0", cap="20")
    f = Scripted(rejections=[LEDGER_REJ], first_cost="0.1")
    assert go(tmp_path, f, d)["halt"]["id"] == "accounting_incomplete"
    assert "accounting_incomplete" in mb.NOT_ACKNOWLEDGEABLE
    with pytest.raises(mb.BatchHalt, match="cannot be acknowledged"):
        mb.run_pilot(d, f, state_path=tmp_path / "s.json", results_path=tmp_path / "r.jsonl", approvals=mb.Approvals(spend_usd="20", pulls=True, controls=True,
                                                                                                                     acknowledge_halt="accounting_incomplete"), execute=True)
    with pytest.raises(mb.BatchHalt, match="halted"):  # and without an acknowledgement it just stays halted
        go(tmp_path, f, d)
    assert len(f.runs()) == 1


def test_a_halt_between_arms_is_reported_as_an_incomplete_pair(tmp_path):
    st = go(tmp_path, Scripted(first_cost="1.4"), locked(budget="1.0", cap="20"))
    assert st["summary"]["incomplete_pairs"] == [{"task_id": SYMPY, "stage": 1, "runs_with_rows": 1, "runs_started_without_row": 0}]
    assert st["summary"]["completed_tasks"] == [] and st["summary"]["stopped_stages"] == [1] and {n["reason"] for n in st["not_run"]} == {"halted:overshoot_allowance_exceeded"} | {"stage_not_activated"}
    assert [n["task_id"] for n in st["not_run"] if n["reason"] == "stage_not_activated"] == [SPHINX, REQ]


# ---------------------------------------------------------------- staged bookkeeping
def test_stage_1_only_then_stage_2_activation_leaves_no_stale_not_run_entries(tmp_path):
    """The reproduced defect: after stage 1 alone the two stage-2 tasks were listed as not run forever."""
    d, f = locked(cap="20"), Fake(cost="0.1")
    st = go(tmp_path, f, d, stages=(1,))
    assert st["status"] == "complete_with_unrun_stages" and [n["task_id"] for n in st["not_run"]] == [SPHINX, REQ] and st["summary"]["unactivated_stages"] == [2]
    assert st["stage_activations"][-1]["stages"] == [1]
    st = go(tmp_path, f, d, stages=(1, 2))
    assert st["status"] == "complete" and st["not_run"] == [] and len(f.runs()) == 12
    assert st["summary"]["completed_tasks"] == [SYMPY, D13933, D15851, SY22714, SPHINX, REQ] and st["summary"]["unrun_tasks"] == [] and st["summary"]["unactivated_stages"] == []
    assert [a["stages"] for a in st["stage_activations"]] == [[1], [1, 2]]  # every activation is audited, in order


def test_an_early_stop_is_not_carried_into_a_later_complete_run(tmp_path):
    d, f = locked(budget="1.0", cap="10.4"), Fake(cost="1.3")
    assert go(tmp_path, f, d, stages=(1, 2))["status"] == "stopped_early"
    again = go(tmp_path, f, d, stages=(1,))  # the operator re-invokes with stage 1 only: nothing stale about the early stop
    assert again["status"] == "complete_with_unrun_stages" and "early_stop" not in again and len(f.runs()) == 8


def test_summary_is_rebuilt_on_every_exit_and_reports_stopped_stages(tmp_path):
    st = go(tmp_path, Fake(cost="0.1"), locked(cap="20"), stages=(1,))
    assert st["summary"]["activated_stages"] == [1] and st["summary"]["stopped_stages"] == []


# ---------------------------------------------------------------- stage prefix rule, inside run_pilot
@pytest.mark.parametrize("stages", [(2,), (1, 3), (3,), (0,), (), (2, 3), (1, 2, 3), ("1",), (True,), (1.5,)])
def test_invalid_stage_selections_are_refused_before_any_state_or_step(tmp_path, stages):
    f = Fake()
    with pytest.raises(mb.BatchHalt, match="stage_selection_invalid"):
        mb.run_pilot(locked(cap="20"), f, state_path=tmp_path / "s.json", results_path=tmp_path / "r.jsonl", approvals=mb.Approvals(spend_usd="20", stages=stages), execute=True)
    assert f.log == [] and not (tmp_path / "s.json").exists()


@pytest.mark.parametrize("stages,expected", [((1,), (1,)), ((1, 2), (1, 2)), ((2, 1, 1), (1, 2)), ([1, 2], (1, 2))])
def test_valid_stage_selections_are_normalised_to_a_sorted_unique_tuple(stages, expected):
    assert mb.validate_stage_selection(locked(cap="20"), stages) == expected


def test_stage_2_alone_cannot_run_even_when_stage_1_is_done_or_not(tmp_path):
    f = Fake(cost="0.1")
    go(tmp_path, f, locked(cap="20"), stages=(1,))
    with pytest.raises(mb.BatchHalt, match="stage_selection_invalid"):
        go(tmp_path, f, locked(cap="20"), stages=(2,))


def test_feasibility_pilots_cannot_activate_optional_stages(tmp_path):
    assert mb.validate_stage_selection(V2, (1,)) == (1,)
    for stages in ((2,), (1, 2), ()):
        with pytest.raises(mb.BatchHalt, match="no optional stages"):
            mb.validate_stage_selection(V2, stages)
    with pytest.raises(mb.BatchHalt, match="stage_selection_invalid"):
        mb.run_pilot(V2, Fake(), state_path=tmp_path / "s.json", results_path=tmp_path / "r.jsonl", approvals=mb.Approvals(spend_usd="1", stages=(1, 2)), execute=True)


def test_stage_2_never_activates_from_outcomes(tmp_path):
    """Every task resolved (the best possible A/B outcomes) and nothing in the batch starts stage 2: only the operator's stages argument does."""
    f = Fake(cost="0.05")
    st = go(tmp_path, f, locked(cap="20"))
    assert len(f.runs()) == 8 and st["stage_activations"][-1]["stages"] == [1] and mb.Approvals().stages == (1,)


# ---------------------------------------------------------------- reservation audit: one authoritative record per pair
def test_repeated_halt_and_resume_keeps_exactly_one_reservation_record_per_pair(tmp_path):
    d = locked(budget="1.0", cap="20")
    first = f"{E1_ID}/{SYMPY}/A/r1"
    f = Fake(cost="0.1", costs={first: "1.4"})
    st = go(tmp_path, f, d)
    assert st["halt"]["id"] == "overshoot_allowance_exceeded"
    for _ in range(2):  # acknowledge, resume: the second arm of the SAME pair runs, then the batch continues
        st = mb.run_pilot(d, f, state_path=tmp_path / "s.json", results_path=tmp_path / "r.jsonl", approvals=mb.Approvals(
            spend_usd="20", pulls=True, controls=True, acknowledge_halt=(st["halt"] or {}).get("id")) if st["status"] == "halted" else mb.Approvals(spend_usd="20", pulls=True, controls=True), execute=True)
    assert st["status"] == "complete_with_unrun_stages"
    recs = st["reservations"]
    assert [r["task"] for r in recs] == [SYMPY, D13933, D15851, SY22714]  # exactly one record per pair
    r = recs[0]
    assert r["arms_reserved"] == 2 and r["reservation_usd"] == "2.60" and r["funded"] is True  # the authoritative record is the pair's first, 2-arm reservation, never rewritten
    assert len(r["continuations"]) >= 1 and r["continuations"][0]["of"] == SYMPY and r["continuations"][0]["arms_reserved"] == 1 and r["continuations"][0]["reservation_usd"] == "1.30"
    assert r["continuations"][0]["arms_done_before"] == 1
    assert [len(x["continuations"]) for x in recs[1:]] == [0, 0, 0] or all(len(x["continuations"]) <= 1 for x in recs[1:])
    assert len(state_events(st, "pair_reservation")) == 4 and len(state_events(st, "pair_reservation_continuation")) == sum(len(x["continuations"]) for x in recs)


def test_reservations_are_not_charged_and_not_discarded(tmp_path):
    f = Fake(cost="0.2")
    st = go(tmp_path, f, locked(budget="1.0", cap="20"))
    assert Decimal(st["cumulative_cost_usd"]) == Decimal("1.6")  # only ledger cost is charged to the cap, never the reservation
    assert all(r["reservation_usd"] == "2.60" and r["funded"] for r in st["reservations"]) and len(st["reservations"]) == 4  # and every reservation stays in the audit trail


def test_re_invoking_an_early_stopped_study_links_to_the_same_unfunded_record(tmp_path):
    d, f = locked(budget="1.0", cap="10.4"), Fake(cost="1.3")
    go(tmp_path, f, d, stages=(1, 2))
    st = go(tmp_path, f, d, stages=(1, 2))
    last = [r for r in st["reservations"] if r["task"] == SPHINX]
    assert len(last) == 1 and last[0]["funded"] is False and len(last[0]["continuations"]) == 1 and last[0]["continuations"][0]["funded"] is False
    assert len(f.runs()) == 8  # nothing was started by the re-invocation


# ---------------------------------------------------------------- requests-863: the compatibility decision record
def test_without_a_recorded_decision_the_conditional_task_halts_before_anything_is_prepared(tmp_path):
    d, f = locked(cap="20"), Fake(cost="0.1")
    st = go(tmp_path, f, d, stages=(1, 2), decide=False)
    assert st["status"] == "halted" and st["halt"]["id"] == "compatibility_decision_missing" and REQ in st["halt"]["detail"]
    assert not any(k[0] == "prepare" and k[1] == REQ for k in f.log) and not any(REQ in s for s in f.runs()) and len(f.runs()) == 10  # stage 1 and sphinx ran; requests did not start
    assert st["summary"]["unrun_tasks"] == [REQ]


def test_a_compatible_decision_lets_the_task_run_and_is_recorded_immutably(tmp_path):
    d, f = locked(cap="20"), Fake(cost="0.1")
    rec = mb.record_compatibility_decision(d, tmp_path / "s.json", REQ, compatible=True, evidence="tests need no network; control plan unchanged", decided_by="operator", decided_at="2026-11-23T10:00:00Z")
    assert rec["operator_decision"] == "include" and rec["uses_ab_outcomes"] is False and rec["decided_at"] == "2026-11-23T10:00:00Z"
    with pytest.raises(Exception, match="immutable"):
        mb.record_compatibility_decision(d, tmp_path / "s.json", REQ, compatible=False, evidence="changed my mind", decided_by="operator")
    st = go(tmp_path, f, d, stages=(1, 2), decide=False)
    assert st["status"] == "complete" and st["decisions"][REQ]["compatible"] is True and st["replacements"] == []


def test_an_incompatible_decision_applies_the_preregistered_replacement_before_any_run(tmp_path):
    d, f = locked(cap="20"), Fake(cost="0.1")
    mb.record_compatibility_decision(d, tmp_path / "s.json", REQ, compatible=False, evidence="the tests fetch a URL; the environment has no network", decided_by="operator")
    st = go(tmp_path, f, d, stages=(1, 2), decide=False)
    (rep,) = st["replacements"]
    assert rep["replaced"] == REQ and rep["replacement"] != REQ and rep["repo"] not in ("matplotlib/matplotlib", "scikit-learn/scikit-learn") and rep["reasons"][0].startswith("compatibility:")
    assert not any(REQ in s for s in f.runs()) and not any(k[0] == "prepare" and k[1] == REQ for k in f.log)  # the incompatible task was never prepared or run
    last = [short(s) for s in f.runs()[-2:]]
    assert [x.split("/")[1] for x in last] == ["B", "A"]  # the replacement keeps requests-863's B-first order
    assert rep["replacement"] in {r["instance_id"] for r in d["preregistration"]["replacements"]}


def test_a_decision_needs_evidence_a_conditional_task_and_no_prior_run(tmp_path):
    d = locked(cap="20")
    for kw in ({"evidence": "  "}, {"decided_by": ""}):
        with pytest.raises(Exception, match="needs compatible"):
            mb.record_compatibility_decision(d, tmp_path / "s.json", REQ, **{"compatible": True, "evidence": "e", "decided_by": "o", **kw})
    with pytest.raises(Exception, match="needs compatible"):
        mb.record_compatibility_decision(d, tmp_path / "s.json", REQ, compatible="yes", evidence="e", decided_by="o")
    with pytest.raises(Exception, match="not a conditional task"):
        mb.record_compatibility_decision(d, tmp_path / "s.json", SYMPY, compatible=True, evidence="e", decided_by="o")
    store = mb.StateStore(tmp_path / "s2.json", d)
    store.state["blocks"][REQ] = {"status": "pending", "task_id": REQ, "controls": {}, "slots": {f"{E1_ID}/{REQ}/B/r1": "done"}, "pulled": {}, "image_removed": False}
    store.save()
    with pytest.raises(Exception, match="before any agent run"):
        mb.record_compatibility_decision(d, tmp_path / "s2.json", REQ, compatible=True, evidence="e", decided_by="o")


def test_the_definition_holds_the_decision_record_schema_and_rejects_tampering():
    c = fresh()["design"]["stages"][1]["conditional_tasks"][0]
    dr = c["decision_record"]
    assert dr["required"] is True and dr["status"] == "pending" and dr["uses_ab_outcomes"] is False and dr["fields"] == ["compatible", "evidence", "operator_decision", "decided_by", "decided_at"]
    assert "before any agent run" in dr["required_before"] and "never edited" in dr["storage"] and "replacement rule" in dr["if_incompatible"]
    for mutate in (lambda x: x.pop("decision_record"), lambda x: x["decision_record"].update(uses_ab_outcomes=True), lambda x: x["decision_record"].update(status="decided"),
                   lambda x: x["decision_record"].update(fields=["compatible"])):
        d = fresh()
        mutate(d["design"]["stages"][1]["conditional_tasks"][0])
        assert any("pending decision_record" in p for p in problems(rehash(d)))
    assert "compatibility_decision_missing" in {c["id"] for c in fresh()["stop_conditions"]}


# ---------------------------------------------------------------- selection documentation and evidence
def test_the_definition_makes_no_unsupported_size_claims():
    text = json.dumps(fresh())
    import re
    assert not re.search(r"(?<![\d.])2 GiB", text)  # (the inherited 12 GiB disk hard stop is not a size claim)
    for needle in ("under 2", "compressed", "small images", "small-image", "registry metadata", "small tasks"):
        assert needle not in text, needle
    sel = fresh()["preregistration"]["selection"]
    gate = next(r for r in sel["eligibility_rules"] if r["id"] == "image_size_and_seed_fit")
    assert gate["status"] == "pending_preparation_gate" and "PREPARED" in gate["rule"] and "no size claim" in gate["rule"]
    assert next(r for r in sel["eligibility_rules"] if r["id"] == "repository_not_excluded")["status"] == "applied"


def test_every_inclusion_and_exclusion_records_its_evidence():
    d = fresh()
    ev = d["preregistration"]["selection"]["evidence"]
    assert ev["source"].startswith("m3-pilot-v2") and V2["definition_sha256"] in ev["source"]
    inc = {e["instance_id"] for e in ev["tasks"] if e["decision"] == "included"}
    exc = {e["instance_id"]: e for e in ev["tasks"] if e["decision"] == "excluded"}
    assert inc == {t["instance_id"] for t in d["preregistration"]["tasks"]} and len(exc) == 2
    assert {e["repo"] for e in exc.values()} == {"matplotlib/matplotlib", "scikit-learn/scikit-learn"} and all("excluded_repos" in e["evidence"] for e in exc.values())
    assert all(e["evidence"] and e["rules_applied"] == ["repository_not_excluded"] and e["pending_gates"] == ["image_size_and_seed_fit"] for e in ev["tasks"] + ev["replacements"])
    assert "no outcome" in ev["stage_assignment"]


def test_the_replacement_pool_passes_the_same_eligibility_rules_as_the_tasks():
    from aiep.experiment_def import eligible_repo
    d = fresh()
    assert all(eligible_repo(r["repo"]) for r in d["preregistration"]["replacements"]) and all(eligible_repo(t["repo"]) for t in d["preregistration"]["tasks"])
    pool = {e["instance_id"] for e in d["preregistration"]["selection"]["evidence"]["replacements"] if e["decision"] == "included"}
    assert pool == {r["instance_id"] for r in d["preregistration"]["replacements"]}
    d["preregistration"]["replacements"].append({"instance_id": "matplotlib__matplotlib-9", "repo": "matplotlib/matplotlib", "image": "x"})
    assert any("same eligibility rules" in p for p in problems(rehash(d)))


def test_selection_documentation_is_validated():
    for mutate, msg in ((lambda s: s["eligibility_rules"].pop(), "PREPARATION gate"), (lambda s: s["eligibility_rules"][1].update(status="applied"), "PREPARATION gate"),
                        (lambda s: s["evidence"]["tasks"].pop(0), "evidence for every included task"), (lambda s: s["evidence"]["tasks"][0].update(evidence=""), "needs its evidence")):
        d = fresh()
        mutate(d["preregistration"]["selection"])
        assert any(msg in p for p in problems(rehash(d))), msg


def test_the_builder_refuses_stages_that_are_not_exactly_the_eligible_tasks(monkeypatch):
    import aiep.experiment_build as eb
    monkeypatch.setattr(eb, "E1_STAGES", ({"stage": 1, "tasks": (SYMPY, D13933), "activation": "required", "min_repos": 2},))
    with pytest.raises(ValueError, match="not exactly the source tasks that pass the eligibility rules"):
        eb.build_exploratory_definition(V2)


def test_regenerated_e1_is_still_a_draft_with_the_approved_budgets_and_v1_v2_are_unchanged():
    on_disk = json.loads(E1_FILE.read_text())
    assert on_disk == committed_e1_expected() and on_disk["status"] == "draft" and on_disk["limits"]["budget_usd_per_run"] == APPROVED_BUDGET and on_disk["limits"]["max_total_computed_usd"] == APPROVED_CAP
    assert problems(on_disk) == [] and on_disk["metrics"]["transcript_parser_version"] == PARSER_VERSION
    assert V2["definition_sha256"].startswith("590536ec") and "small" not in on_disk["claim_scope"]


# ---------------------------------------------------------------- the driver
def test_driver_stages_flag_default_plan_and_refusals(capsys, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import run_m3_pilot as drv
    base = ["--definition", str(E1_FILE)]
    assert drv.main(base, env={}) == 0
    out = capsys.readouterr().out
    assert "active stages [1] of [1, 2]" in out and "exploratory study" in out and "blocker: the definition is not locked" in out
    assert drv.main(base + ["--stages", "1", "2"], env={}) == 0 and "active stages [1, 2] of [1, 2]" in capsys.readouterr().out
    assert drv.main(base + ["--stages", "2", "1", "1"], env={}) == 0 and "active stages [1, 2]" in capsys.readouterr().out  # normalised: sorted, unique
    assert drv.main(base + ["--stages", "2"], env={}) == 2 and "stage_selection_invalid" in capsys.readouterr().err
    assert drv.main(base + ["--stages", "1", "3"], env={}) == 2
    assert drv.main(base + ["--execute", "--stages", "2", "--approve-spend", "1"], env={"AIEP_UPSTREAM_API_KEY": "k" * 40}) == 2  # refused before anything starts
    assert drv.main([], env={}) == 0 and "active stages [1] of [1]" in capsys.readouterr().out  # a feasibility pilot keeps its behaviour
    assert drv.main(["--stages", "1", "2"], env={}) == 2  # and has no optional stage


# ---------------------------------------------------------------- the builder shares no mutable object with its sources
def _containers(obj, acc=None):
    """The identity of every mutable container (dict, list, set) reachable from obj, with duplicates preserved so internal aliasing is visible."""
    acc = [] if acc is None else acc
    if isinstance(obj, (dict, list, set)):
        acc.append(id(obj))
        for v in (obj.values() if isinstance(obj, dict) else obj):
            _containers(v, acc)
    return acc


def _build_from_fresh_v2():
    src = copy.deepcopy(V2)
    return src, copy.deepcopy(src), build_exploratory_definition(src)


@pytest.mark.parametrize("path", [("arms",), ("preregistration", "replacements"), ("preregistration", "control_requirements"), ("stop_conditions",), ("metrics", "secondary"), ("claude_cli",), ("dataset",), ("prompt_set",),
                                  ("preregistration", "tasks"), ("preregistration", "selection", "screen")])
def test_mutating_a_built_structure_never_changes_the_v2_source(path):
    src, pristine, e1 = _build_from_fresh_v2()
    node = e1
    for k in path:
        node = node[k]
    assert isinstance(node, (dict, list))
    if isinstance(node, list):
        node.append("MUTATED")
        if node[:-1] and isinstance(node[0], dict):
            node[0]["MUTATED"] = True
    else:
        node["MUTATED"] = True
    assert src == pristine


def test_mutating_the_built_test_command_spec_never_changes_the_parsers_constant():
    from aiep.transcript import TEST_COMMAND_SPEC
    before = copy.deepcopy(TEST_COMMAND_SPEC)
    e1 = fresh()
    assert e1["metrics"]["test_command_spec"] == TEST_COMMAND_SPEC and e1["metrics"]["test_command_spec"] is not TEST_COMMAND_SPEC
    e1["metrics"]["test_command_spec"]["MUTATED"] = True
    for v in list(e1["metrics"]["test_command_spec"].values()):
        if isinstance(v, (dict, list)):
            v.append("MUTATED") if isinstance(v, list) else v.update(MUTATED=True)
    assert TEST_COMMAND_SPEC == before


def test_the_built_document_shares_no_container_with_the_source_the_constants_or_itself():
    from aiep import experiment_build as eb
    from aiep.transcript import TEST_COMMAND_SPEC
    src = copy.deepcopy(V2)
    e1 = build_exploratory_definition(src)
    mine = _containers(e1)
    assert len(mine) == len(set(mine)), "a mutable object appears twice inside the built document"
    foreign = set(_containers(src)) | set(_containers(TEST_COMMAND_SPEC)) | set(_containers(eb.E1_REQUESTS_CONDITION)) | set(_containers(list(eb.E1_STAGES)))
    assert not (set(mine) & foreign)


def test_the_builder_does_not_change_its_input_and_repeated_builds_are_independent():
    src = copy.deepcopy(V2)
    first, second = build_exploratory_definition(src), build_exploratory_definition(src)
    assert src == V2 and first == second
    assert not (set(_containers(first)) & set(_containers(second)))
    first["stop_conditions"].append({"id": "x"})
    first["design"]["stages"][1]["conditional_tasks"][0]["decision_record"]["status"] = "recorded"
    assert second == fresh() and fresh()["design"]["stages"][1]["conditional_tasks"][0]["decision_record"]["status"] == "pending"


def test_after_the_aliasing_fix_e1_is_unchanged_and_still_valid():
    e1 = json.loads(E1_FILE.read_text())
    assert e1 == committed_e1_expected() and e1["definition_sha256"] == definition_hash(e1)
    assert problems(e1) == [] and e1["claim_scope"] == EXPLORATORY_CLAIM_SCOPE and e1["status"] == "draft"
    assert e1["limits"]["budget_usd_per_run"] == APPROVED_BUDGET and e1["limits"]["max_total_computed_usd"] == APPROVED_CAP
    assert fresh()["limits"]["budget_usd_per_run"] is None and fresh()["limits"]["max_total_computed_usd"] is None  # the aliasing fix left the builder's null-budget template alone
    assert e1["metrics"]["transcript_parser_version"] == PARSER_VERSION and [s["tasks"] for s in e1["design"]["stages"]] == [list(s["tasks"]) for s in E1_STAGES]

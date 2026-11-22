import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from aiep import grading as g
from aiep import m3_batch as mb
from aiep import task_pins as tp
from aiep.experiment_def import definition_hash
from aiep.gateway import parse_max_concurrent
from aiep.ledger import request_status_summary

ROOT = Path(__file__).resolve().parents[1]
IMG = {"image": "n", "image_id": "sha256:" + "a" * 64}


# ---------- grading
def test_gates_equal_m1_eval_script():
    sh = (ROOT / "scripts" / "m1_eval.sh").read_text()
    assert int(re.search(r"MIN_START=(\d+)", sh).group(1)) == g.START_GATE_GIB
    assert float(re.search(r"f<([\d.]+)\)", sh).group(1)) == g.HARD_STOP_GIB


def test_noop_prediction_valid(tmp_path):
    p = g.write_noop_predictions(tmp_path / "p.jsonl", "a__b-1")
    assert json.loads(p.read_text())["instance_id"] == "a__b-1"


def inst(resolved, f=(2, 0), p=(3, 0), applied=True):
    return {"resolved": resolved, "patch_successfully_applied": applied, "tests_status": {"FAIL_TO_PASS": {"success": ["x"] * f[0], "failure": ["y"] * f[1]},
                                                                                       "PASS_TO_PASS": {"success": ["x"] * p[0], "failure": ["y"] * p[1]}}}


H = {"completed_instances": 1, "submitted_instances": 1, "error_instances": 0}


def test_summary_and_controls():
    ok = g.summarize("r", "i", H, inst(True), 0, False)
    assert ok["valid"] and ok["resolved"] is True and not g.control_problems("gold", ok)
    noop = g.summarize("r", "i", H, inst(False, f=(0, 2)), 0, False)
    assert not g.control_problems("noop", noop)
    assert g.control_problems("noop", g.summarize("r", "i", H, inst(False, f=(1, 1)), 0, False))
    assert g.control_problems("gold", g.summarize("r", "i", H, inst(False, f=(1, 1)), 0, False))
    bad = g.summarize("r", "i", H | {"error_instances": 1}, inst(True), 0, False)
    assert not bad["valid"] and bad["resolved"] is None and g.control_problems("gold", bad)
    assert not g.summarize("r", "i", H, inst(True), 0, True)["valid"]


def test_grade_refuses_without_starting(tmp_path):
    calls = []
    kw = dict(kind="gold", root=tmp_path, expected_image=IMG, image_id_of=lambda n: IMG["image_id"], run_process=lambda *a: calls.append(a) or 0,
              kill_containers=lambda r: None)
    with pytest.raises(g.GradingRefused):
        g.grade("i", "gold", "r1", free_gib=lambda: 15.9, **kw)
    with pytest.raises(g.GradingRefused):
        g.grade("i", "gold", "r1", free_gib=lambda: 20, **(kw | {"image_id_of": lambda n: "sha256:" + "b" * 64}))
    with pytest.raises(g.GradingRefused):
        g.grade("i", "relative.jsonl", "r1", free_gib=lambda: 20, **kw)
    assert calls == []
    (tmp_path / "runs" / "m1" / "dup").mkdir(parents=True)
    with pytest.raises(g.GradingRefused):
        g.grade("i", "gold", "dup", free_gib=lambda: 20, **kw)


def test_grade_runs_and_hard_stops(tmp_path):
    killed = []

    def runner(cmd, cwd, env):
        assert env["HF_HUB_OFFLINE"] == "1"
        return 0
    out = g.grade("i", "gold", "ok1", kind="gold", root=tmp_path, expected_image=IMG, free_gib=lambda: 20, image_id_of=lambda n: IMG["image_id"], run_process=runner,
                  kill_containers=killed.append, sleep=lambda s: None)
    assert not out.summary["valid"] and not killed  # no report produced: never read as resolved
    seq = iter([20, 11.5])
    import time
    out = g.grade("i", "gold", "stop1", kind="gold", root=tmp_path, expected_image=IMG, free_gib=lambda: next(seq, 11.5), image_id_of=lambda n: IMG["image_id"],
                  run_process=lambda *a: time.sleep(0.3) or 0, kill_containers=killed.append, sleep=lambda s: None)
    assert out.hard_stopped and killed == ["stop1"] and not out.summary["valid"]


# ---------- gateway concurrency + ledger status
def test_max_concurrent_parse():
    assert parse_max_concurrent(None) == 2 and parse_max_concurrent("1") == 1
    for bad in ("0", "5", "01", "x", "1.0", "-1"):
        with pytest.raises(ValueError):
            parse_max_concurrent(bad)


def test_request_status_summary():
    lines = [{"rid": "1", "outcome": "forwarding"}, {"rid": "1", "outcome": "ok", "status": 200}, {"rid": "2", "outcome": "ok", "status": 429},
             {"rid": "3", "outcome": "refused", "status": 429, "reason": "gateway busy"}, {"rid": "4", "outcome": "upstream_read_error", "status": 200, "usage_incomplete_upper_bound": True},
             {"rid": "5", "outcome": "ok", "status": 529}, {"rid": "6", "outcome": "ok", "status": 500}]
    s = request_status_summary("\n".join(json.dumps(x) for x in lines) + "\nbroken")
    c = s["categories"]
    assert s["finished_requests"] == 6 and c["success"] == 1 and c["rate_limited_429"] == 2 and c["interrupted"] == 1 and c["overloaded_529"] == 1 and c["server_5xx"] == 1
    assert s["gateway_busy_429"] == 1 and s["usage_incomplete_upper_bound"] == 1 and s["non_success"] == 5 and sum(c.values()) == 6
    assert request_status_summary(None)["non_success"] is None


# ---------- pins
def test_pins_never_overwrite(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "evaluator_images.json").write_text(json.dumps({"images": {}}))
    (tmp_path / "config" / "seeds.json").write_text(json.dumps({"seeds": {}}))
    iid, sid = "psf__requests-863", "sha256:" + "a" * 64
    tp.add_evaluator_image(tmp_path, iid, sid, source="t")
    tp.add_evaluator_image(tmp_path, iid, sid, source="t")
    with pytest.raises(tp.PinError):
        tp.add_evaluator_image(tmp_path, iid, "sha256:" + "b" * 64, source="t")
    with pytest.raises(tp.PinError):
        tp.add_evaluator_image(tmp_path, iid, "bad", source="t")
    with pytest.raises(tp.PinError):
        tp.add_seed_pin(tmp_path, iid, directory="d", sha256="c" * 64, head="d" * 40, entries=3, image_id="sha256:" + "e" * 64)
    tp.add_seed_pin(tmp_path, iid, directory="d", sha256="c" * 64, head="d" * 40, entries=3, image_id=sid)
    with pytest.raises(tp.PinError):
        tp.add_seed_pin(tmp_path, iid, directory="d", sha256="f" * 64, head="d" * 40, entries=3, image_id=sid)
    st = tp.pin_status(tmp_path, iid)
    assert st["image_pinned"] and st["seed_pinned"] and not st["runnable"] and "agent_identity" in st["missing"]


def test_repo_unpinned_tasks_not_runnable():
    doc = json.loads((ROOT / "config/experiments/m3-pilot-v2.json").read_text())
    for t in doc["preregistration"]["tasks"]:
        assert not tp.pin_status(ROOT, t["instance_id"])["runnable"]


# ---------- batch
def make_def(budget="1.0", cap="10", status="locked"):
    doc = json.loads((ROOT / "config/experiments/m3-pilot-v2.json").read_text())
    doc["status"] = status
    doc["limits"]["budget_usd_per_run"], doc["limits"]["max_total_computed_usd"] = budget, cap
    doc["definition_sha256"] = definition_hash(doc)
    return doc


class Fake:
    def __init__(self, *, free=30.0, bad_controls=(), cost="0.1", rejected=(), accounting=True, valid_grade=True):
        self.free, self.bad_controls, self.cost, self.rejected, self.accounting, self.valid_grade = free, set(bad_controls), cost, set(rejected), accounting, valid_grade
        self.log, self.prepared, self.sleeps = [], set(), []

    def free_gib(self): return self.free
    def task_prepared(self, t): return t in self.prepared
    def prepare_task(self, t): self.prepared.add(t); self.log.append(("prepare", t)); return {"pulled": True}
    def image_in_use(self, t): return False
    def grade_control(self, t, kind):
        self.log.append(("control", t, kind))
        if (t, kind) in self.bad_controls:
            return {"valid": True, "resolved": kind == "noop", "patch_applied": True, "f2p_passed": 1, "f2p_total": 2, "p2p_maintained": 1, "p2p_total": 1}
        if kind == "gold":
            return {"valid": True, "resolved": True, "patch_applied": True, "f2p_passed": 2, "f2p_total": 2, "p2p_maintained": 1, "p2p_total": 1}
        return {"valid": True, "resolved": False, "patch_applied": True, "f2p_passed": 0, "f2p_total": 2, "p2p_maintained": 1, "p2p_total": 1}
    def run_arm(self, t, arm, ident, budget):
        self.log.append(("run", ident["slot_id"]))
        return mb.ArmResult(Path("/x") / ident["slot_id"])
    def validate_handoff(self, rd, exp):
        bad = str(rd).removeprefix("/x/") in self.rejected
        return {"status": "rejected" if bad else "ready", "rejections": (["ledger is COMPLETE"] if not self.accounting else ["x"]) if bad else []}
    def grade_run(self, t, rd, slot): self.log.append(("grade", slot)); return {"valid": self.valid_grade, "resolved": True}
    def transcript(self, rd, cap): return {}
    def build_row(self, rd, ident, handoff, grading, tm, repo):
        return {"slot_id": ident["slot_id"], "task_id": ident["task_id"], "cost_usd_computed": self.cost, "resolved": (grading or {}).get("resolved"),
                "accounting_complete": self.accounting or handoff["status"] != "rejected", "lifecycle_status": "completed", "stop_reason": None}
    def exposure_estimate_usd(self, b, c): return Decimal("1.5")
    def remove_image(self, t): self.log.append(("remove", t))
    def sleep(self, s): self.sleeps.append(s)


class W(mb.ResultsWriter):  # the orchestrator's writer validates real rows; the fake rows are minimal, so only record them
    def append(self, row):
        with open(self.path, "a") as f:
            f.write(json.dumps(row) + "\n")


@pytest.fixture(autouse=True)
def plain_writer(monkeypatch):
    monkeypatch.setattr(mb, "ResultsWriter", W)


AP = mb.Approvals


def FULL(cap="10"):
    return AP(spend_usd=cap, pulls=True, controls=True, remove_images=True)


def run(tmp_path, fake, ap, d=None, execute=True, name="s.json"):
    return mb.run_pilot(d or make_def(), fake, state_path=tmp_path / name, results_path=tmp_path / "r.jsonl", approvals=ap, execute=execute)


def test_plan_touches_nothing(tmp_path):
    f = Fake()
    out = run(tmp_path, f, AP(), make_def(status="draft"), execute=False)
    assert out["status"] == "planned" and f.log == [] and not (tmp_path / "s.json").exists()


def test_refuses_without_lock_or_exact_spend(tmp_path):
    with pytest.raises(mb.BatchHalt):
        run(tmp_path, Fake(), FULL(), make_def(status="draft"))
    with pytest.raises(mb.BatchHalt):
        run(tmp_path, Fake(), FULL("9"))


def test_full_flow_ordering_and_gap(tmp_path):
    d = make_def()
    f = Fake()
    st = run(tmp_path, f, FULL(), d)
    assert st["status"] == "complete", st["halt"]
    assert [x[1] for x in f.log if x[0] == "run"] == [r["slot_id"] for r in d["design"]["schedule"]]
    assert f.sleeps == [600] * 8
    for t in {r["task_id"] for r in d["design"]["schedule"]}:
        kinds = [x[0] for x in f.log if (x[0] in ("prepare", "control", "remove") and x[1] == t) or (x[0] in ("run", "grade") and f"/{t}/" in x[1])]
        assert kinds[:3] == ["prepare", "control", "control"] and kinds[-1] == "remove" and kinds.count("run") == 2
    assert len((tmp_path / "r.jsonl").read_text().splitlines()) == 16


def test_cost_cap_halts_and_resume_needs_ack(tmp_path):
    d = make_def(cap="1.6")
    f = Fake()
    st = run(tmp_path, f, FULL("1.6"), d)
    assert st["status"] == "halted" and st["halt"]["id"] == "cumulative_cost_cap"
    with pytest.raises(mb.BatchHalt):
        run(tmp_path, Fake(), FULL("1.6"), d)
    st2 = run(tmp_path, Fake(), AP("1.6", True, True, True, acknowledge_halt="cumulative_cost_cap"), d)
    assert st2["halt"]["id"] == "cumulative_cost_cap"  # still over the cap: halts again, never overspends silently


def test_state_bound_to_definition(tmp_path):
    run(tmp_path, Fake(), FULL(), make_def())
    other = make_def(budget="2.0")
    with pytest.raises(mb.BatchHalt):
        run(tmp_path, Fake(), FULL(), other)


def test_disk_gates(tmp_path):
    assert run(tmp_path, Fake(free=15.9), FULL())["halt"]["id"] == "disk_start_gate"
    assert run(tmp_path / "b", Fake(free=11.9), FULL())["halt"]["id"] == "disk_hard_stop"
    f = Fake(free=20)
    orig = f.run_arm
    def drop(*a):
        f.free = 11.9
        return orig(*a)
    f.run_arm = drop
    assert run(tmp_path / "c", f, FULL())["halt"]["id"] == "disk_hard_stop"


def test_control_failure_replaces_before_any_run(tmp_path):
    d = make_def()
    first = d["design"]["schedule"][0]["task_id"]
    f = Fake(bad_controls={(first, "gold")})
    st = run(tmp_path, f, FULL(), d)
    assert st["status"] == "complete", st["halt"]
    assert st["replacements"][0]["replaced"] == first
    assert not any(x[0] == "run" and f"/{first}/" in x[1] for x in f.log)
    new = st["replacements"][0]["replacement"]
    runs = [x[1] for x in f.log if x[0] == "run"]
    assert f"/{new}/" in runs[0] and f"/{new}/" in runs[1]


def test_replacements_exhausted_halts(tmp_path):
    d = make_def()
    pre = d["preregistration"]
    bad = {(t["instance_id"], "gold") for t in pre["tasks"] + pre["replacements"]}
    st = run(tmp_path, Fake(bad_controls=bad), FULL(), d)
    assert st["halt"]["id"] == "control_replacement_exhausted"


def test_controls_and_pulls_need_approval(tmp_path):
    assert run(tmp_path, Fake(), AP(spend_usd="10", pulls=False, controls=True))["halt"]["id"] == "task_not_prepared"
    assert run(tmp_path / "b", Fake(), AP(spend_usd="10", pulls=True, controls=False))["halt"]["id"] == "controls_not_approved"


def test_accounting_incomplete_halts(tmp_path):
    d = make_def()
    st = run(tmp_path, Fake(rejected=[d["design"]["schedule"][0]["slot_id"]], accounting=False), FULL(), d)
    assert st["halt"]["id"] == "accounting_incomplete"


def test_invalid_grading_halts(tmp_path):
    st = run(tmp_path, Fake(valid_grade=False), FULL())
    assert st["halt"]["id"] == "grading_invalid"


def test_rejected_run_is_not_graded(tmp_path):
    d = make_def()
    first = d["design"]["schedule"][0]["slot_id"]
    f = Fake(rejected=[first])
    run(tmp_path, f, FULL(), d)
    assert ("grade", first) not in f.log


def test_unexpected_exception_halts(tmp_path):
    f = Fake()
    f.prepare_task = lambda t: (_ for _ in ()).throw(RuntimeError("boom"))
    assert run(tmp_path, f, FULL())["halt"]["id"] == "orchestrator_error"

"""End-to-end M3 orchestration through the REAL batch, RealSteps, task pins, results builder, transcript parser and analysis, with fake Docker/agent/evaluator/handoff.

Nothing here touches Docker, the network, an API or an evaluator: every side effect is an in-memory fake that writes realistic run directories.
"""

import json
import shutil
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiep import analysis, m3_batch as mb, task_pins as tp
from aiep.experiment_def import build_schedule, definition_hash
from aiep.grading import GradingOutcome, control_problems, summarize
from aiep.m3_steps import Deps, RealSteps, StepError, read_predictions
from aiep.pricing import load_pricing
from aiep.results import ResultsWriter, validate_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
IMG = "sha256:" + "a" * 64
PRICING = load_pricing(ROOT / "config" / "model_pricing.json", json.loads((ROOT / "config" / "experiment.json").read_text())["model"])
PATCH = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-a\n+b\n"


def make_def(n_tasks=8, budget="1.0", cap="10", prompts_path=None):
    doc = json.loads((ROOT / "config/experiments/m3-pilot-v2.json").read_text())
    pre = doc["preregistration"]
    pre["tasks"] = pre["tasks"][:n_tasks]
    doc["design"]["schedule"] = build_schedule(doc["experiment_id"], [t["instance_id"] for t in pre["tasks"]], pre["seed"], 1)
    doc["status"], doc["limits"]["budget_usd_per_run"], doc["limits"]["max_total_computed_usd"] = "locked", budget, cap
    doc["definition_sha256"] = definition_hash(doc)
    return doc


def tu(i, name, **inp):
    return json.dumps({"type": "assistant", "message": {"id": f"m{i}", "content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}})


def tr(i, out):
    return json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": out}]}})


class World:
    """Fake Docker + agent + evaluator + handoff. ``behavior[(task, arm)]`` selects what a run does; ``resolved[(task, arm)]`` what grading says."""

    def __init__(self, tmp_path):
        self.root = tmp_path / "repo"
        (self.root / "config").mkdir(parents=True)
        (self.root / "config" / "evaluator_images.json").write_text(json.dumps({"images": {}}))
        (self.root / "config" / "seeds.json").write_text(json.dumps({"seeds": {}}))
        legacy = json.loads((ROOT / "config" / "agent_identity.json").read_text())
        self.img = legacy["verified_image_id"]
        shutil.copy(ROOT / "config" / "agent_identity.json", self.root / "config" / "agent_identity.json")
        self.images, self.running, self.log = set(), 0, []
        self.behavior, self.resolved, self.bad_controls, self.pull_fails = {}, {}, set(), set()
        self.free, self.run_counter, self.grade_calls, self.prepare_calls = 30.0, 0, [], []
        self.remove_fails = 0
        self.cost = "0.2"

    # --- docker-ish
    def client(self):
        return SimpleNamespace(close=lambda: None)

    @staticmethod
    def record_of(task):
        """A fake verified digest record: only the fields the preparation path reads."""
        return {"repository": "swebench/sweb.eval.x86_64." + task.replace("__", "_1776_"), "index_digest": "sha256:" + "a" * 64}

    @staticmethod
    def task_of(record):
        return record["repository"].split("x86_64.")[1].replace("_1776_", "__")

    def pull(self, c, record):
        task = self.task_of(record)
        if task in self.pull_fails:
            raise RuntimeError("pull failed")
        self.images.add(task)
        self.log.append(("pull", task))

    def remove(self, c, iid):
        if self.remove_fails:
            self.remove_fails -= 1
            raise RuntimeError("remove failed")
        self.images.clear()
        self.log.append(("remove",))

    # --- m2 prepare/execute
    def prepare(self, root, **kw):
        self.run_counter += 1
        rid = f"m2-{kw['instance_id'].split('__')[-1]}-{kw['label']}-{self.run_counter:03d}"
        self.prepare_calls.append((rid, kw))
        return SimpleNamespace(run_id=rid, kw=kw, seed_sha="s" * 64)

    def execute(self, client, p, *, base, root):
        kw, beh = p.kw, self.behavior.get((p.kw["instance_id"], p.kw["label"]), "ok")
        self.log.append(("run", kw["experiment"]["slot_id"]))
        d = Path(base) / p.run_id
        d.mkdir(parents=True)
        ok = beh in ("ok", "no_patch")
        acc_complete = beh != "incomplete_accounting"
        agent = {"status": "exited" if ok or beh == "incomplete_accounting" else "timeout", "valid": ok or beh == "incomplete_accounting",
                 "invalid_reason": None if ok else "wall clock exceeded", "stop_reason": None if ok else "wall_clock", "claude_exit_code": 0 if ok else None,
                 "patch": {"status": "ok", "sha256": "p" * 64, "size": len(PATCH)}}
        run = {"run_id": p.run_id, "experiment": kw["experiment"], "prompt_set_file": kw["prompts_file"], "model": "m", "image_id": self.img, "seed": {"sha256": p.seed_sha},
               "claude_cli": {"sha256": "c" * 64}, "prompt": {"label": kw["label"]}, "limits": {"budget_usd": kw["budget_usd"], "gateway_max_concurrent": kw["max_concurrent"]},
               "orchestrator_wall_s": 12.5, "lifecycle": {"status": "completed", "agent": agent},
               "accounting": {"complete": acc_complete, "exact": acc_complete},
               "usage_measured": {"tokens": {"input_tokens": 1000, "output_tokens": 100, "cache_read_tokens": 500}, "computed_usd_upper_bound_basis": self.cost, "exact": acc_complete,
                                  "requests": 3, "request_status": {"finished_requests": 3, "non_success": 0, "categories": {"success": 3}}}}
        (d / "run.json").write_text(json.dumps(run))
        lines = [tu(1, "Edit", file_path="f.py")] if kw["label"] == "A" else [tu(1, "Bash", command="pytest t.py"), tr(1, "1 failed"), tu(2, "Edit", file_path="f.py"),
                                                                           tu(3, "Bash", command="pytest t.py"), tr(3, "2 passed in 1s")]
        (d / "claude_stdout.jsonl").write_text("\n".join(lines + [json.dumps({"type": "result", "subtype": "success", "num_turns": 4})]) + "\n")
        if beh != "no_patch" and agent["valid"]:
            (d / "predictions.jsonl").write_text(json.dumps({"instance_id": kw["instance_id"], "model_name_or_path": "aiep", "model_patch": PATCH}) + "\n")
        if beh == "raise":
            raise RuntimeError("lifecycle blew up after persisting")
        return SimpleNamespace(directory=d, problems=[])

    # --- handoff / grading
    def handoff(self, run_dir, expected):
        run = json.loads((Path(run_dir) / "run.json").read_text())
        assert run["experiment"]["slot_id"] in expected and expected[run["experiment"]["slot_id"]]["order_index"] == run["experiment"]["order_index"]
        rej = []
        if not run["lifecycle"]["agent"]["valid"]:
            rej.append("Claude Code exited on its own with status 0 (not killed, timed out, or container died)")  # a wall-clock timeout: agent behaviour, not setup
        if not run["accounting"]["complete"]:
            rej.append("the gateway ledger is COMPLETE: required")
        return {"status": "rejected" if rej else "ready", "rejections": rej, "flags": [], "report_sha256": "h" * 64}

    def grade(self, instance_id, preds, run_id, *, kind, expected_image):
        self.grade_calls.append((instance_id, preds, run_id, kind))
        if kind in ("gold", "noop"):
            bad = (instance_id, kind) in self.bad_controls
            inst = ({"resolved": kind == "noop", "tests_status": {"FAIL_TO_PASS": {"success": ["x"], "failure": ["y"]}, "PASS_TO_PASS": {"success": ["z"], "failure": []}}} if bad else
                    {"resolved": kind == "gold", "tests_status": {"FAIL_TO_PASS": {"success": ["x", "y"] if kind == "gold" else [], "failure": [] if kind == "gold" else ["x", "y"]},
                                                                  "PASS_TO_PASS": {"success": ["z"], "failure": []}}})
        else:
            arm = run_id.split("-")[-2] if False else ("A" if "-A-" in run_id else "B")
            res = self.resolved.get((instance_id, arm), True)
            inst = {"resolved": res, "tests_status": {"FAIL_TO_PASS": {"success": ["x"] if res else [], "failure": [] if res else ["x"]}, "PASS_TO_PASS": {"success": ["z"], "failure": []}}}
        inst["patch_successfully_applied"] = True
        summ = summarize(run_id, instance_id, {"completed_instances": 1, "submitted_instances": 1, "error_instances": 0}, inst,
                         1 if (self.eval_fails if kind == "scored" else self.control_infra_fails) else 0, False)
        return GradingOutcome(run_id, instance_id, kind, 0, False, 20, 20, summary=summ)

    eval_fails = False
    control_infra_fails = False

    def deps(self, definition, **over):
        w = self
        pre = definition["preregistration"]
        repo_of = {t["instance_id"]: t["repo"] for t in pre["tasks"]} | {t["instance_id"]: t["repo"] for t in pre["replacements"]}

        def pin_image(c, root, task, *, free_bytes):
            tp.add_evaluator_image(root, task, w.img, source="fake")
            return w.img

        def extract(c, root, task, *, free_bytes):
            tp.add_seed_pin(root, task, directory="d", sha256="c" * 64, head="d" * 40, entries=3, image_id=w.img)

        d = dict(root=self.root, definition=definition, client_factory=self.client, free_gib=lambda: self.free, prepare=self.prepare, execute=self.execute, handoff=self.handoff,
                 grade=self.grade, write_noop=lambda p, t: p, pricing=PRICING, pin_image=pin_image, pull_image=self.pull, extract_seed=extract,
                 digest_record=self.record_of, verify_registry=lambda rec: self.log.append(("verify_registry", self.task_of(rec))),
                 verify_identity=lambda *a, **k: None, image_present=lambda c, rec: any(t == self.task_of(rec) for t in self.images),
                 image_id_of=lambda c, n: self.img, containers_using=lambda c, i: self.running, remove_image_by_id=self.remove,
                 dataset_record=lambda t: {"instance_id": t, "repo": repo_of[t], "base_commit": "c" * 40},
                 real_key="k" * 40, claude_binary=Path("/x"), sleep=lambda s: self.log.append(("sleep", s)))
        d.update(over)
        return Deps(**d)


FULL = lambda cap="10": mb.Approvals(spend_usd=cap, pulls=True, controls=True, remove_images=True)


def go(tmp_path, world, definition, ap=None, name="state.json"):
    steps = RealSteps(world.deps(definition))
    st = mb.run_pilot(definition, steps, state_path=tmp_path / name, results_path=tmp_path / "results.jsonl", approvals=ap or FULL(definition["limits"]["max_total_computed_usd"]), execute=True)
    return st, steps


def rows(tmp_path):
    return ResultsWriter(tmp_path / "results.jsonl").rows()


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# ---------------------------------------------------------------- the happy paths
def test_one_complete_pair(tmp_path, world):
    d = make_def(1)
    st, _ = go(tmp_path, world, d)
    assert st["status"] == "complete", st["halt"]
    r = rows(tmp_path)
    assert [x["slot_id"] for x in r] == [s["slot_id"] for s in d["design"]["schedule"]]
    for x in r:
        validate_row(x)
        assert x["valid_measurement"] and x["graded"] and x["resolved"] is True and x["handoff_status"] == "ready" and x["cost_exact"]
        assert x["prompt_set_file"] == "config/workflow_prompts_v2.json" and x["definition_sha256"] == d["definition_sha256"]
    a, b = (next(x for x in r if x["arm"] == k) for k in "AB")
    assert a["test_runs"] == 0 and b["test_runs"] == 2 and b["test_fix_cycles"] == 1 and b["compliance"] and not a["compliance"]
    assert a["pair_id"] == b["pair_id"] and {a["order_index"], b["order_index"]} == {1, 2}
    assert ("sleep", 600) in world.log and world.log.index(("sleep", 600)) < world.log.index(("run", d["design"]["schedule"][1]["slot_id"]))
    assert ("remove",) in world.log and not world.images
    t = d["design"]["schedule"][0]["task_id"]
    assert world.log.index(("verify_registry", t)) < world.log.index(("pull", t))  # the digest record is re-verified against the registry before the pull


def test_identity_preserved_through_every_stage(tmp_path, world):
    d = make_def(2)
    st, _ = go(tmp_path, world, d)
    assert st["status"] == "complete"
    for (rid, kw), slot in zip(world.prepare_calls, d["design"]["schedule"]):
        exp = kw["experiment"]
        assert exp == {"experiment_id": d["experiment_id"], "task_id": slot["task_id"], "arm": slot["arm"], "repetition": slot["rep"], "pair_id": slot["pair_id"],
                       "order_index": slot["order_index"], "slot_id": slot["slot_id"]}
        assert kw["label"] == slot["arm"] and kw["max_concurrent"] == 1 and kw["prompts_file"] == "config/workflow_prompts_v2.json"
    for x, slot in zip(rows(tmp_path), d["design"]["schedule"]):
        assert (x["slot_id"], x["task_id"], x["arm"], x["repetition"], x["pair_id"], x["order_index"]) == (slot["slot_id"], slot["task_id"], slot["arm"], slot["rep"], slot["pair_id"], slot["order_index"])


def test_eight_tasks_registered_order_and_balance_then_analysis(tmp_path, world):
    d = make_def(8)
    world.resolved = {(t["instance_id"], "B"): (i % 2 == 0) for i, t in enumerate(d["preregistration"]["tasks"])}
    st, _ = go(tmp_path, world, d)
    assert st["status"] == "complete"
    assert [kw["experiment"]["slot_id"] for _, kw in world.prepare_calls] == [s["slot_id"] for s in d["design"]["schedule"]]
    firsts = [kw["label"] for _, kw in world.prepare_calls][::2]
    assert firsts.count("A") == firsts.count("B") == 4
    assert len({rid for rid, _ in world.prepare_calls}) == 16  # a fresh prepare (fresh containers, new run id) for every arm
    rep = analysis.analyze(rows(tmp_path), d)
    assert rep["as_scheduled"]["outcomes"]["pairs"] == 8 == rep["valid_runs"]["outcomes"]["pairs"] and not rep["not_run_tasks"]
    o = rep["as_scheduled"]["outcomes"]
    assert len(o["cells"]["both_resolved"]) == 4 and len(o["cells"]["A_only"]) == 4 and o["concordant"] == 4 and o["discordant"] == 4
    assert "FEASIBILITY PILOT" in rep["label"]


# ---------------------------------------------------------------- evaluator input
def test_evaluator_gets_only_the_patch_file(tmp_path, world):
    d = make_def(1)
    go(tmp_path, world, d)
    scored = [c for c in world.grade_calls if c[3] == "scored"]
    assert len(scored) == 2
    for inst, preds, rid, kind in scored:
        assert Path(preds).name == "predictions.jsonl" and Path(preds).is_absolute()
        assert set(json.loads(Path(preds).read_text())) == {"instance_id", "model_name_or_path", "model_patch"}


def test_read_predictions_refuses_extra_fields_or_other_task(tmp_path):
    p = tmp_path / "p.jsonl"
    base = {"instance_id": "t", "model_name_or_path": "m", "model_patch": PATCH}
    for bad in (base | {"extra": 1}, base | {"instance_id": "other"}, base | {"model_patch": " "}):
        p.write_text(json.dumps(bad) + "\n")
        with pytest.raises(StepError):
            read_predictions(p, "t")
    p.write_text(json.dumps(base) + "\n" + json.dumps(base) + "\n")
    with pytest.raises(StepError):
        read_predictions(p, "t")


def test_no_patch_is_not_graded_and_not_a_success(tmp_path, world):
    d = make_def(1)
    first = d["design"]["schedule"][0]
    world.behavior[(first["task_id"], first["arm"])] = "no_patch"
    st, _ = go(tmp_path, world, d)
    assert st["status"] == "complete"
    x = rows(tmp_path)[0]
    assert x["graded"] is False and x["resolved"] is None and x["valid_measurement"] is False
    assert len([c for c in world.grade_calls if c[3] == "scored"]) == 1
    rep = analysis.analyze(rows(tmp_path), d)
    assert rep["as_scheduled"]["outcomes"]["pairs"] == 1 and rep["valid_runs"]["outcomes"]["pairs"] == 0  # counted unresolved, excluded from valid runs


# ---------------------------------------------------------------- failures
def test_task_preparation_failure_halts_before_anything_runs(tmp_path, world):
    d = make_def(2)
    world.pull_fails.add(d["design"]["schedule"][0]["task_id"])
    st, _ = go(tmp_path, world, d)
    assert st["halt"]["id"] == "orchestrator_error" and not any(x[0] == "run" for x in world.log) and not world.grade_calls and not rows(tmp_path)


def test_control_infrastructure_failure_halts_and_does_not_replace(tmp_path, world):
    d = make_def(8)
    world.control_infra_fails = True
    st, _ = go(tmp_path, world, d)
    assert st["halt"]["id"] == "grading_invalid" and not st["replacements"] and not any(x[0] == "run" for x in world.log)


def test_gold_noop_control_failure_replaces_then_runs_replacement(tmp_path, world):
    d = make_def(8)
    first = d["design"]["schedule"][0]["task_id"]
    world.bad_controls.add((first, "noop"))
    st, _ = go(tmp_path, world, d)
    assert st["status"] == "complete", st["halt"]
    new = st["replacements"][0]["replacement"]
    assert st["replacements"][0]["replaced"] == first and new not in [t["instance_id"] for t in d["preregistration"]["tasks"]]
    assert [kw["instance_id"] for _, kw in world.prepare_calls][:2] == [new, new]
    assert all(x["task_id"] != first for x in rows(tmp_path))
    r = rows(tmp_path)[0]
    assert r["slot_id"] == f"{d['experiment_id']}/{new}/{d['design']['schedule'][0]['arm']}/r1" and r["order_index"] == 1
    assert r["pair_id"] == d["design"]["schedule"][0]["pair_id"] == f"{d['experiment_id']}:{first}:r1"  # the pair keeps its REGISTERED id; task_id and slot_id name the replacement


def test_invalid_handoff_never_graded_never_success(tmp_path, world):
    d = make_def(1)
    s0 = d["design"]["schedule"][0]
    world.behavior[(s0["task_id"], s0["arm"])] = "timeout"
    st, _ = go(tmp_path, world, d)
    assert st["status"] == "complete"
    x = rows(tmp_path)[0]
    assert x["handoff_status"] == "rejected" and not x["graded"] and x["resolved"] is None and not x["valid_measurement"] and x["valid"] is False
    assert not any(c[2].startswith("m3eval-") and s0["arm"] in c[2].split("-")[2] for c in world.grade_calls if False)
    assert len([c for c in world.grade_calls if c[3] == "scored"]) == 1


def test_agent_exception_after_persist_is_rejected_not_lost(tmp_path, world):
    d = make_def(1)
    s0 = d["design"]["schedule"][0]
    world.behavior[(s0["task_id"], s0["arm"])] = "raise"
    st, _ = go(tmp_path, world, d)
    assert st["status"] == "complete"
    x = rows(tmp_path)[0]
    assert x["slot_id"] == s0["slot_id"] and x["resolved"] in (None, True)  # persisted run dir was used; a valid-looking run still had to pass the handoff


def test_interrupt_halts_unackable_and_never_repeats_slot(tmp_path, world):
    d = make_def(1)
    s0 = d["design"]["schedule"][0]
    orig = world.execute

    def boom(*a, **k):
        raise KeyboardInterrupt()
    world.execute = boom
    with pytest.raises(KeyboardInterrupt):
        go(tmp_path, world, d)
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["status"] == "halted" and state["halt"]["id"] == "interrupted" and state["blocks"][s0["task_id"]]["slots"][s0["slot_id"]] == "started"
    world.execute = orig
    with pytest.raises(mb.BatchHalt):  # cannot be acknowledged: the started run may have spent money that nothing recorded
        go(tmp_path, world, d, ap=mb.Approvals("10", True, True, True, acknowledge_halt="interrupted"))
    assert not rows(tmp_path)


def test_evaluator_failure_halts_as_invalid_grading(tmp_path, world):
    d = make_def(1)
    world.eval_fails = True
    st, _ = go(tmp_path, world, d)
    assert st["halt"]["id"] == "grading_invalid"
    x = rows(tmp_path)[0]
    assert x["graded"] is False and x["resolved"] is None  # an evaluator failure is never read as "unresolved by the model"


def test_incomplete_accounting_halts(tmp_path, world):
    d = make_def(1)
    s0 = d["design"]["schedule"][0]
    world.behavior[(s0["task_id"], s0["arm"])] = "incomplete_accounting"
    st, _ = go(tmp_path, world, d)
    assert st["halt"]["id"] == "accounting_incomplete" and len(rows(tmp_path)) == 1 and rows(tmp_path)[0]["accounting_complete"] is False
    assert not any(x[0] == "run" and x[1] == d["design"]["schedule"][1]["slot_id"] for x in world.log)


def test_disk_gate_violations(tmp_path, world):
    d = make_def(1)
    world.free = 15.9
    st, _ = go(tmp_path, world, d)
    assert st["halt"]["id"] == "disk_start_gate" and not world.log
    w2 = World(tmp_path / "w2")
    steps_free = iter([30, 30, 30, 30, 30, 11.0])
    w2.free = 30
    orig = w2.execute

    def drop(*a, **k):
        w2.free = 11.0
        return orig(*a, **k)
    w2.execute = drop
    (tmp_path / "w2b").mkdir()
    st, _ = go(tmp_path / "w2b", w2, d)
    assert st["halt"]["id"] == "disk_hard_stop"


def test_cumulative_budget_stop(tmp_path, world):
    d = make_def(8, cap="2.0")
    st, _ = go(tmp_path, world, d)
    assert st["halt"]["id"] == "cumulative_cost_cap" and len(rows(tmp_path)) == 2 and Decimal(st["cumulative_cost_usd"]) == Decimal("0.4")
    assert "estimate" in st["halt"]["detail"]


# ---------------------------------------------------------------- image lifecycle
def test_image_removed_only_after_both_runs_and_only_if_pulled(tmp_path, world):
    d = make_def(2)
    st, _ = go(tmp_path, world, d)
    seq = [x for x in world.log if x[0] in ("pull", "run", "remove")]
    kinds = [x[0] for x in seq]
    assert kinds == ["pull", "run", "run", "remove", "pull", "run", "run", "remove"]


def test_image_not_removed_while_container_uses_it_then_recovers(tmp_path, world):
    d = make_def(1)
    world.running = 1
    st, _ = go(tmp_path, world, d)
    assert st["halt"]["id"] == "image_still_required" and world.images and len(rows(tmp_path)) == 2
    world.running = 0
    st2, _ = go(tmp_path, world, d, ap=mb.Approvals("10", True, True, True, acknowledge_halt="image_still_required"))
    assert st2["status"] == "complete" and not world.images
    assert len([x for x in world.log if x[0] == "run"]) == 2  # recovery removed the image; nothing was re-run


def test_image_removal_failure_recovers_without_rerun(tmp_path, world):
    d = make_def(1)
    world.remove_fails = 1
    st, _ = go(tmp_path, world, d)
    assert st["halt"]["id"] == "orchestrator_error" and world.images
    st2, _ = go(tmp_path, world, d, ap=mb.Approvals("10", True, True, True, acknowledge_halt="orchestrator_error"))
    assert st2["status"] == "complete" and not world.images and len(rows(tmp_path)) == 2 and len([x for x in world.log if x[0] == "run"]) == 2


def test_preexisting_image_is_kept(tmp_path, world):
    d = make_def(1)
    t = d["design"]["schedule"][0]["task_id"]
    world.images.add(t)
    st, _ = go(tmp_path, world, d)
    assert st["status"] == "complete" and t in world.images and ("pull", t) not in world.log


# ---------------------------------------------------------------- registered schedule
def test_edited_or_reconstructed_schedule_is_refused(tmp_path, world):
    d = make_def(2)
    d["design"]["schedule"][0], d["design"]["schedule"][1] = d["design"]["schedule"][1], d["design"]["schedule"][0]
    d["definition_sha256"] = definition_hash(d)  # even re-hashed, it is not the schedule the seed derives
    with pytest.raises(mb.BatchHalt) as e:
        go(tmp_path, world, d)
    assert e.value.stop_id == "definition_invalid" and not world.log


def test_state_cannot_be_reused_for_another_definition(tmp_path, world):
    go(tmp_path, world, make_def(1))
    with pytest.raises(mb.BatchHalt):
        go(tmp_path, World(tmp_path / "x"), make_def(1, budget="2.0"))


# ---------------------------------------------------------------- driver script
def test_driver_plan_and_refusals(tmp_path, capsys):
    import run_m3_pilot as drv
    assert drv.main([], env={}) == 0
    out = capsys.readouterr().out
    assert "blocker: the definition is not locked" in out and "feasibility pilot" in out
    assert drv.main(["--execute"], env={}) == 2  # draft definition, no approvals, no key
    assert drv.main(["--execute", "--approve-spend", "10", "--approve-pulls", "--approve-controls"], env={"AIEP_UPSTREAM_API_KEY": "k" * 40}) == 2


def test_driver_executes_with_injected_steps(tmp_path, world):
    import run_m3_pilot as drv
    d = make_def(1)
    defpath = tmp_path / "def.json"
    defpath.write_text(json.dumps(d))
    root = tmp_path / "droot"
    shutil.copytree(ROOT / "config", root / "config")
    (root / "config" / "experiments").mkdir(exist_ok=True)
    fake_bin = tmp_path / "claude"
    fake_bin.write_bytes(b"x")
    # the definition must validate against the repo configs, which a 1-task copy does not: use the loader seam
    import aiep.experiment_def as ed
    orig = drv.load_experiment
    drv.load_experiment = lambda p, root: d
    try:
        rc = drv.main(["--execute", "--approve-spend", "10", "--approve-pulls", "--approve-controls", "--claude-binary", str(fake_bin)], root=world.root, env={"AIEP_UPSTREAM_API_KEY": "k" * 40},
                      steps_factory=lambda r, definition, a, key: RealSteps(world.deps(definition)))
    finally:
        drv.load_experiment = orig
    assert rc == 0 and len((world.root / "runs" / "m3" / d["experiment_id"] / "results.jsonl").read_text().splitlines()) == 2

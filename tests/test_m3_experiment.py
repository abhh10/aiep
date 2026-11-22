import copy
import json
from pathlib import Path

import pytest

from aiep import experiment_def as ed
from aiep.experiment import load_prompts
from aiep.task_screening import image_name, is_well_formed, screen_record, select_tasks

ROOT = Path(__file__).resolve().parents[1]


def rec(i, repo="a/b", p2p=("t.py::x",), f2p=("t.py::y",)):
    return {"instance_id": f"{repo.split('/')[0]}__{repo.split('/')[1]}-{i}", "repo": repo, "base_commit": "0" * 40, "FAIL_TO_PASS": json.dumps(list(f2p)),
            "PASS_TO_PASS": json.dumps(list(p2p))}


def test_id_formats():
    assert is_well_formed("pytest-dev/pytest", "testing/a.py::t")
    assert not is_well_formed("pytest-dev/pytest", "[ 50%]")
    assert is_well_formed("django/django", "test_x (app.tests.T)")
    assert not is_well_formed("django/django", "garbage")
    assert is_well_formed("sympy/sympy", "test_abc")


def test_screen_excludes_malformed_and_excluded():
    assert screen_record(rec(1)) == []
    assert screen_record(rec(1, p2p=("[ 50%]",)))
    assert screen_record(rec(1), exclude=("a__b-1",))


def test_image_name():
    assert image_name("psf__requests-863") == "swebench/sweb.eval.x86_64.psf_1776_requests-863:latest"


def test_selection_deterministic_and_capped():
    recs = [rec(i, f"o{i % 6}/r{i % 6}") for i in range(60)]
    a = select_tasks(recs, seed="s", n=8, max_per_repo=2, min_repos=4)
    b = select_tasks(recs, seed="s", n=8, max_per_repo=2, min_repos=4)
    assert [r["instance_id"] for r in a["chosen"]] == [r["instance_id"] for r in b["chosen"]]
    repos = [r["repo"] for r in a["chosen"]]
    assert len(a["chosen"]) == 8 and max(repos.count(x) for x in set(repos)) <= 2 and len(set(repos)) >= 4
    assert [r["instance_id"] for r in select_tasks(recs, seed="other", n=8, max_per_repo=2, min_repos=4)["chosen"]] != [r["instance_id"] for r in a["chosen"]]


@pytest.fixture(scope="module")
def doc():
    return ed.load_experiment(root=ROOT)


def test_committed_definition_valid_and_draft(doc):
    assert doc["status"] == "draft" and doc["kind"] == "feasibility_pilot"
    assert "pytest-dev__pytest-5227" not in [t["instance_id"] for t in doc["preregistration"]["tasks"]]
    assert len(doc["preregistration"]["tasks"]) == 8
    with pytest.raises(ed.ExperimentError):
        ed.require_locked(doc)


def test_schedule_balanced_and_paired(doc):
    sched = doc["design"]["schedule"]
    assert len(sched) == 16
    firsts = [r["arm"] for r in sched if r["arm_position"] == 1]
    assert firsts.count("A") == firsts.count("B") == 4
    assert sched == ed.build_schedule(doc["experiment_id"], [t["instance_id"] for t in doc["preregistration"]["tasks"]], doc["preregistration"]["seed"], 1)


def test_tamper_detected(doc, tmp_path):
    bad = copy.deepcopy(doc)
    bad["limits"]["wall_clock_s"] = 1
    assert any("sha256" in p or "hash" in p for p in ed.validate_definition(bad, root=ROOT))
    bad = copy.deepcopy(doc)
    bad["limits"]["gateway_max_concurrent"] = 2
    bad["definition_sha256"] = ed.definition_hash(bad)
    assert ed.validate_definition(bad, root=ROOT)


def test_prompts_v2_changes_only_b(doc):
    v1, v2 = load_prompts(ROOT / "config/workflow_prompts.json"), load_prompts(ROOT / "config/workflow_prompts_v2.json")
    assert v1.hashes["template"] == v2.hashes["template"] and v1.hashes["A"] == v2.hashes["A"] and v1.hashes["B"] != v2.hashes["B"]
    assert "at most 5 test-fix cycles" in v2.workflows["B"]
    assert doc["prompt_set"]["sha256"] == v2.hashes


def test_replacement_rules(doc):
    failed = doc["preregistration"]["tasks"][0]["instance_id"]
    r = ed.apply_replacement(doc, failed)
    assert r["replacement"] not in [t["instance_id"] for t in doc["preregistration"]["tasks"]]
    repos = [t["repo"] for t in doc["preregistration"]["tasks"] if t["instance_id"] != failed] + [r["repo"]]
    assert max(repos.count(x) for x in set(repos)) <= 2
    used = [c["instance_id"] for c in doc["preregistration"]["replacements"]]
    with pytest.raises(ed.ExperimentError):
        ed.apply_replacement(doc, failed, already_used=used)
    with pytest.raises(ed.ExperimentError):
        ed.apply_replacement(doc, "nope__nope-1")


def test_financial_notice_not_a_guarantee(doc):
    assert "not a guaranteed maximum" in doc["financial_notice"]
    assert {c["id"] for c in doc["stop_conditions"]} >= {"accounting_incomplete", "disk_hard_stop", "cumulative_cost_cap"}

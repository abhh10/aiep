"""Evaluator readiness assessment (read-only): the blockers it reports are computed from the live files, not hardcoded."""
import json
import shutil
from pathlib import Path

import pytest

from aiep.evaluator_readiness import assess

REAL = Path(__file__).resolve().parents[1]
IMG = "sha256:c91459f86ed261ab81871b086d7ca2d9fe16abd2ddf374bf172edd5d991fd28b"


@pytest.fixture
def root(tmp_path):
    shutil.copytree(REAL / "config", tmp_path / "config")
    (tmp_path / "scripts").mkdir()
    shutil.copy(REAL / "scripts" / "m1_eval.sh", tmp_path / "scripts" / "m1_eval.sh")
    (tmp_path / "src" / "aiep").mkdir(parents=True)
    for n in ("evaluator.py", "container_policy.py"):
        shutil.copy(REAL / "src" / "aiep" / n, tmp_path / "src" / "aiep" / n)
    return tmp_path


def test_the_stock_m1_evaluator_is_authoritative_and_its_disk_gate_is_read_from_the_driver(root):
    r = assess(root, env={}, free_gib=15.07)
    assert "stock_eval" in r["authoritative_evaluator"] and r["stock_m1"]["authoritative"] and not r["hardened"]["authoritative"]
    assert r["driver"]["min_start_gib"] == 16 and r["driver"]["hard_stop_gib"] == 12
    assert r["stock_m1"]["disk"] == {"free_gib": 15.07, "min_start_gib": 16, "hard_stop_gib": 12, "shortfall_gib": 0.93}
    assert r["stock_m1"]["executable"] is False and any("refuses to start below 16 GiB" in b for b in r["stock_m1"]["blockers"])


def test_with_enough_disk_and_the_right_image_the_stock_evaluator_has_no_blockers_but_still_needs_the_network(root):
    r = assess(root, env={}, free_gib=17.0, image_id_of=lambda name: IMG)
    assert r["stock_m1"]["executable"] is True and r["stock_m1"]["blockers"] == [] and r["stock_m1"]["needs_network"] is True
    assert any("NETWORK REQUIRED" in n and "setuptools-scm" in n for n in r["stock_m1"]["notes"])


@pytest.mark.parametrize("got", [None, "sha256:" + "0" * 64])
def test_a_missing_or_different_evaluator_image_blocks_because_the_harness_would_pull_by_mutable_tag(root, got):
    r = assess(root, env={}, free_gib=20.0, image_id_of=lambda name: got)
    assert r["stock_m1"]["executable"] is False and any("silently PULL" in b for b in r["stock_m1"]["blockers"])


def test_the_hardened_evaluator_is_blocked_for_each_documented_reason_and_none_is_waived(root):
    r = assess(root, env={}, free_gib=20.0)
    b = " | ".join(r["hardened"]["blockers"])
    assert r["hardened"]["executable"] is False and r["hardened"]["needs_network"] is False
    for needle in ("wheelhouse.json is not populated", "AIEP_ATTEST_KEY_FILE is not set", "no tool exists that runs the egress probe", "nothing injects the wheelhouse",
                   "network_mode=none"):
        assert needle in b, needle


def test_a_populated_wheelhouse_alone_does_not_unblock_the_hardened_evaluator(root):
    wh = root / "config" / "wheelhouse.json"
    wh.write_text(json.dumps({"schema": 1, "status": "populated", "wheels": [{"name": "x"}]}))
    r = assess(root, env={}, free_gib=20.0)
    b = " | ".join(r["hardened"]["blockers"])
    assert "not populated" not in b and r["hardened"]["executable"] is False and "AIEP_ATTEST_KEY_FILE" in b and "no tool exists" in b


def test_an_attestation_key_that_is_group_readable_is_a_blocker(root, tmp_path):
    key = tmp_path / "k"
    key.write_bytes(b"k" * 40)
    key.chmod(0o644)
    assert any("accessible by group or others" in b for b in assess(root, env={"AIEP_ATTEST_KEY_FILE": str(key)}, free_gib=20.0)["hardened"]["blockers"])
    assert any("not readable" in b for b in assess(root, env={"AIEP_ATTEST_KEY_FILE": str(tmp_path / "missing")}, free_gib=20.0)["hardened"]["blockers"])


def test_the_real_repository_state_is_reported_as_it_is():
    r = assess(REAL, env={}, free_gib=15.07)
    assert r["hardened"]["executable"] is False and r["driver"]["instance"] == "pytest-dev__pytest-5227" and r["note"].startswith("read-only")
    assert r["images"]["pytest-dev__pytest-5227"] == IMG


def test_the_assessment_runs_nothing_and_imports_no_docker_or_network_module():
    import ast
    tree = ast.parse((REAL / "src" / "aiep" / "evaluator_readiness.py").read_text())
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not mods & {"docker", "subprocess", "urllib", "requests", "socket", "http"}

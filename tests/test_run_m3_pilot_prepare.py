"""run_m3_pilot.py --prepare-only: pull/pin/seed/identity for named tasks. No agent, no API, no key, no evaluator, no image removal; pulls need --approve-pulls."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiep import m3_steps
from aiep.m3_steps import StepError, prepare_pinned_task

REAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REAL / "scripts"))
spec = importlib.util.spec_from_file_location("run_m3_pilot", REAL / "scripts" / "run_m3_pilot.py")
drv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drv)

DEF = json.loads((REAL / "config/experiments/m3-pilot-v2.json").read_text())
T1, T2 = DEF["preregistration"]["tasks"][0]["instance_id"], DEF["preregistration"]["tasks"][1]["instance_id"]
REPL = DEF["preregistration"]["replacements"][0]["instance_id"]


class Calls:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, root, task_id, *, approved_pulls):
        self.calls.append((task_id, approved_pulls))
        if task_id == self.fail:
            raise StepError("boom")
        return {"pulled": True, "status": {"runnable": True}}


def go(args, prepare=None, capsys=None, **kw):
    return drv.main(args, root=REAL, env={}, prepare_fn=prepare or Calls(), **kw)


def test_without_approval_nothing_is_called_and_the_status_is_printed(capsys):
    c = Calls()
    assert go(["--prepare-only", "--task", T1], c) == 0
    out = capsys.readouterr().out
    assert c.calls == [] and T1 in out and "--approve-pulls" in out


def test_with_approval_each_named_task_is_prepared_in_order_and_nothing_else_happens(capsys):
    c = Calls()
    assert go(["--prepare-only", "--task", T1, "--task", T2, "--approve-pulls"], c) == 0
    assert c.calls == [(T1, True), (T2, True)]
    assert "pulled" in capsys.readouterr().out


def test_a_task_must_be_named_and_must_belong_to_the_definition(capsys):
    c = Calls()
    assert go(["--prepare-only", "--approve-pulls"], c) == 2
    assert go(["--prepare-only", "--task", "nope__nope-1", "--approve-pulls"], c) == 2
    assert go(["--prepare-only", "--task", "pytest-dev__pytest-5227", "--approve-pulls"], c) == 2  # the plumbing task is not part of the pilot
    assert c.calls == []
    assert go(["--prepare-only", "--task", REPL, "--approve-pulls"], c) == 0  # a pre-registered replacement may be prepared


def test_it_cannot_be_combined_with_execution_or_spend_flags(capsys):
    c = Calls()
    assert go(["--prepare-only", "--execute", "--task", T1, "--approve-pulls"], c) == 2
    assert go(["--prepare-only", "--task", T1, "--approve-pulls", "--approve-spend", "1"], c) == 2
    assert go(["--prepare-only", "--task", T1, "--approve-pulls", "--approve-controls"], c) == 2
    assert go(["--prepare-only", "--task", T1, "--approve-pulls", "--approve-image-removal"], c) == 2
    assert c.calls == []


def test_a_failure_stops_at_that_task_and_exits_1(capsys):
    c = Calls(fail=T1)
    assert go(["--prepare-only", "--task", T1, "--task", T2, "--approve-pulls"], c) == 1
    assert c.calls == [(T1, True)] and "boom" in capsys.readouterr().err


def test_no_key_binary_or_definition_lock_is_needed(capsys):
    assert DEF["status"] == "draft" and go(["--prepare-only", "--task", T1, "--approve-pulls"]) == 0


# ---- the shared preparation function (digest-pinned; see tests/test_image_digests.py for the full set)
REC = {"repository": "swebench/sweb.eval.x86_64.demo_1776_demo-1", "index_digest": "sha256:" + "a" * 64}


def deps(present=False, free=30.0):
    log = []
    d = dict(root=REAL, client_factory=lambda: SimpleNamespace(close=lambda: log.append("close")), free_gib=lambda: free, digest_record=lambda t: REC,
             verify_registry=lambda rec: log.append("verify_registry"), image_present=lambda c, rec: present, pull_image=lambda c, rec: log.append(("pull", rec["index_digest"])),
             pin_image=lambda *a, **k: log.append("pin"), extract_seed=lambda *a, **k: log.append("seed"), verify_identity=lambda *a, **k: log.append("identity"))
    return d, log


def test_pull_refused_below_the_start_gate_before_any_docker_call(monkeypatch):
    d, log = deps(present=False, free=15.0)
    monkeypatch.setattr(m3_steps, "pin_status", lambda root, t: {"runnable": False, "seed_pinned": False, "identity_verified": False, "missing": ["x"]})
    monkeypatch.setattr(m3_steps, "evaluator_image_pin", lambda root, t: None)
    with pytest.raises(StepError, match="16 GiB"):
        prepare_pinned_task(**d, task_id=T1, allow_pull=True)
    assert not any(isinstance(x, tuple) for x in log) and "verify_registry" not in log


def test_no_pull_without_permission(monkeypatch):
    d, log = deps(present=False, free=30.0)
    with pytest.raises(StepError, match="not approved"):
        prepare_pinned_task(**d, task_id=T1, allow_pull=False)
    assert not any(isinstance(x, tuple) for x in log) and "verify_registry" not in log


def test_a_local_image_is_never_pulled(monkeypatch):
    d, log = deps(present=True, free=30.0)
    seq = iter([{"runnable": False, "seed_pinned": False, "identity_verified": False, "missing": []}] * 2 + [{"runnable": True, "seed_pinned": True, "identity_verified": True, "missing": []}])  # seed check, identity check, final status
    monkeypatch.setattr(m3_steps, "pin_status", lambda root, t: next(seq))
    monkeypatch.setattr(m3_steps, "evaluator_image_pin", lambda root, t: None)
    r = prepare_pinned_task(**d, task_id=T1, allow_pull=True)
    assert r["pulled"] is False and log == ["pin", "seed", "identity", "close"]

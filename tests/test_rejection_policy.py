"""Defect 1: handoff rejections are classified, fail-closed, and the batch halts accordingly. The classifier is anchored to the stable check names in aiep.handoff."""
import ast
from pathlib import Path

import pytest

from aiep import rejection_policy as rp

ROOT = Path(__file__).resolve().parents[1]


def handoff_check_calls():
    """(name, severity) of every literal rep.check(...) in aiep.handoff, read from the source so a new or reworded check cannot slip past the classifier."""
    tree = ast.parse((ROOT / "src" / "aiep" / "handoff.py").read_text())
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "check" and isinstance(n.func.value, ast.Name) and n.func.value.id == "rep":
            first = n.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                sev = next((k.value.value for k in n.keywords if k.arg == "severity"), "reject")
                out.append((first.value, sev))
    return out


def test_every_rejecting_handoff_check_is_explicitly_classified():
    rejecting = [name for name, sev in handoff_check_calls() if sev == "reject"]
    assert len(rejecting) > 30
    unclassified = [n for n in rejecting if rp.classify_check(n, {})[1] == "unclassified"]
    assert unclassified == [], "classify every rejecting handoff check explicitly (the default is fail-closed, but it must be a decision): " + repr(unclassified)


def test_every_rule_matches_a_real_check():
    names = [n for n, _ in handoff_check_calls()]
    for key in rp.RULES:
        assert any(key in n for n in names), f"rule {key!r} matches no handoff check (reworded?)"


def test_categories():
    c = lambda phrase, detail=None: rp.classify_check(next(n for n, _ in handoff_check_calls() if phrase in n), detail or {})[0]
    assert c("the gateway ledger is COMPLETE") == rp.ACCOUNTING
    assert c("the seed the agent started from is the pinned seed") == rp.INTEGRITY
    assert c("the Claude binary recorded is the pinned one") == rp.INTEGRITY
    assert c("the recorded prompt hash equals") == rp.INTEGRITY
    assert c("the agent ran in the SAME image") == rp.INTEGRITY
    assert c("the pinned seed's HEAD equals") == rp.INTEGRITY
    assert c("Claude Code exited on its own") == rp.AGENT
    assert c("the exact model_patch applies cleanly") == rp.AGENT
    assert c("the patch passes the production patch validator") == rp.AGENT
    assert c("the lifecycle completed") == rp.TRANSIENT
    assert c("the agent's post-start network attachment check passed") == rp.TRANSIENT
    assert c("the orchestrator was not interrupted") == rp.TRANSIENT
    assert c("the agent's INPUT") == rp.HIDDEN_LEAK


def test_unknown_rejection_fails_closed():
    assert rp.classify_check("something new nobody classified", {}) == ("integrity", "unclassified")


def test_missing_artifacts_is_agent_only_when_just_the_patch_outputs_are_missing():
    name = next(n for n, _ in handoff_check_calls() if n.startswith("every required artifact is present"))
    assert rp.classify_check(name, {"missing": ["predictions.jsonl"]})[0] == rp.AGENT
    assert rp.classify_check(name, {"missing": ["predictions.jsonl", "patch.diff"]})[0] == rp.AGENT
    assert rp.classify_check(name, {"missing": ["run.json"]})[0] == rp.INTEGRITY
    assert rp.classify_check(name, {"missing": ["predictions.jsonl", "gateway_ledger.jsonl"]})[0] == rp.INTEGRITY
    assert rp.classify_check(name, {})[0] == rp.INTEGRITY


def test_classify_handoff_uses_checks_when_present_and_strings_otherwise():
    checks = [{"check": "the gateway ledger is COMPLETE: every request ...", "ok": False, "severity": "reject", "detail": {}},
              {"check": "the lifecycle completed (gateway and proxy came up; not a startup failure)", "ok": False, "severity": "reject", "detail": "failed"},
              {"check": "fine", "ok": True, "severity": "reject", "detail": ""}]
    r = rp.classify_handoff({"status": "rejected", "rejections": [c["check"] for c in checks[:2]], "checks": checks, "flags": []})
    assert r.categories == {rp.ACCOUNTING, rp.TRANSIENT}
    only_strings = rp.classify_handoff({"status": "rejected", "rejections": ["the Claude binary recorded is the pinned one (sha256, size, version, platform)"], "flags": []})
    assert only_strings.categories == {rp.INTEGRITY}


def test_ready_and_flagged_handoffs_have_no_rejection_category():
    assert rp.classify_handoff({"status": "ready", "rejections": [], "flags": []}).categories == set()


def test_exactness_flag_means_uncertain_financial_exposure():
    flag = next(n for n, sev in handoff_check_calls() if n.startswith("the usage figures are EXACT"))
    r = rp.classify_handoff({"status": "ready_flagged", "rejections": [], "flags": [flag]})
    assert r.financial_uncertain and r.categories == set()
    assert not rp.classify_handoff({"status": "ready_flagged", "rejections": [], "flags": ["the run was not stopped by the gateway's budget"]}).financial_uncertain


def test_rejected_with_no_reason_is_integrity():
    assert rp.classify_handoff({"status": "rejected", "rejections": [], "flags": []}).categories == {rp.INTEGRITY}
    assert rp.classify_handoff({"status": "weird"}).categories == {rp.INTEGRITY}


VALID = "the agent run is VALID (not invalidated by a stop reason, a failed attachment / identity / injection check, or a lost lease)"


@pytest.mark.parametrize("detail,cat", [
    ({"invalid_reason": "the run was stopped because its host-side lease could not be refreshed: lease_lost", "stop_reason": "lease_lost"}, rp.TRANSIENT),
    ({"invalid_reason": "gateway stopped the run: proxy_died", "stop_reason": "proxy_died"}, rp.TRANSIENT),
    ({"invalid_reason": "gateway stopped the run: gateway_died", "stop_reason": "gateway_died"}, rp.TRANSIENT),
    ({"invalid_reason": "gateway stopped the run: gateway_unreachable", "stop_reason": "gateway_unreachable"}, rp.TRANSIENT),
    ({"invalid_reason": "agent attachment check failed: x", "stop_reason": None}, rp.TRANSIENT),
    ({"invalid_reason": "agent identity check failed: uid", "stop_reason": None}, rp.INTEGRITY),
    ({"invalid_reason": "/x does not match its manifest: y", "stop_reason": None}, rp.INTEGRITY),
    ({"invalid_reason": "gateway stopped the run: model_violation", "stop_reason": "model_violation"}, rp.INTEGRITY),
    ({"invalid_reason": "gateway stopped the run: tool_violation", "stop_reason": "tool_violation"}, rp.INTEGRITY),
    ({"invalid_reason": "gateway stopped the run: metering_failure", "stop_reason": "metering_failure"}, rp.ACCOUNTING),
    ({"invalid_reason": "claude output truncated at the 33554432-byte cap per stream: the run is censored and is not graded", "stop_reason": None}, rp.AGENT),
    ({"invalid_reason": "not finished", "stop_reason": None}, rp.INTEGRITY),
    ({}, rp.INTEGRITY),
    ("garbage", rp.INTEGRITY),
])
def test_valid_check_is_classified_by_its_cause(detail, cat):
    assert rp.classify_check(VALID, detail)[0] == cat

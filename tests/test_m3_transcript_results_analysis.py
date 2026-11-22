import json
from decimal import Decimal

import pytest

from aiep import analysis as an
from aiep import results as rs
from aiep.transcript import is_test_command, parse_transcript


def tu(i, name, **inp):
    return json.dumps({"type": "assistant", "message": {"id": f"m{i}", "content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}})


def tr(i, out, err=False):
    return json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": out, "is_error": err}]}})


RESULT = json.dumps({"type": "result", "subtype": "success", "num_turns": 7, "total_cost_usd": 1.0})


def tx(*lines):
    return "\n".join(lines) + "\n"


def test_command_detection():
    assert is_test_command("cd /testbed && python -m pytest tests/x.py -q")
    assert is_test_command("python tests/runtests.py x")
    assert not is_test_command("pytest --version")
    assert not is_test_command("pytest --collect-only")
    assert not is_test_command("ls tests")


def test_cycles_and_compliance():
    raw = tx(tu(1, "Bash", command="pytest t.py"), tr(1, "1 failed, 2 passed"), tu(2, "Edit", file_path="a"), tu(3, "Bash", command="pytest t.py"), tr(3, "3 passed in 1s"), RESULT)
    m = parse_transcript(raw, cycle_cap=5)
    assert m["test_runs"] == 2 and m["test_runs_failed"] == 1 and m["test_fix_cycles"] == 1 and m["compliance"] and m["ended_with_passing_tests"] and m["complete"]


def test_no_cycle_without_edit_and_cap():
    m = parse_transcript(tx(tu(1, "Bash", command="pytest"), tr(1, "1 failed"), tu(2, "Bash", command="pytest"), tr(2, "1 failed"), RESULT), cycle_cap=5)
    assert m["test_fix_cycles"] == 0 and not m["compliance"]
    lines = []
    for i in range(1, 15, 2):
        lines += [tu(i, "Bash", command="pytest"), tr(i, "1 failed"), tu(i + 1, "Edit", file_path="a")]
    assert parse_transcript(tx(*lines, RESULT), cycle_cap=5)["cap_exceeded"]


def test_a_arm_no_tests_and_incomplete():
    m = parse_transcript(tx(tu(1, "Edit", file_path="a"), RESULT), cycle_cap=5)
    assert not m["test_usage"] and m["edits_total"] == 1
    assert not parse_transcript(tx(tu(1, "Edit", file_path="a"), "{broken"), cycle_cap=5)["complete"]


def test_mcnemar_and_bootstrap():
    assert an.mcnemar_exact_two_sided(0, 0) == 1.0
    assert an.mcnemar_exact_two_sided(5, 0) == pytest.approx(0.0625)
    assert an.mcnemar_exact_two_sided(3, 3) == 1.0
    assert an.bootstrap_interval([], "s") is None
    assert an.bootstrap_interval([1, 0, -1, 1], "s") == an.bootstrap_interval([1, 0, -1, 1], "s")


class P:
    input, output = Decimal("2"), Decimal("10")


def row(task, arm, resolved, valid=True, cost="1.0", handoff="ready"):
    return {"task_id": task, "arm": arm, "repetition": 1, "resolved": resolved if valid else None, "graded": valid, "valid_measurement": valid, "cost_usd_computed": cost,
            "uncached_equivalent_usd": cost, "cost_exact": True, "handoff_status": handoff, "repo": "r", "tokens": {}, "stop_reason": None}


DEF = {"experiment_id": "e", "definition_sha256": "x", "preregistration": {"seed": "s", "tasks": [{"instance_id": t} for t in "abcd"]}}


def test_analysis_sets_and_discordance():
    rows = [row("a", "A", True), row("a", "B", True), row("b", "A", False), row("b", "B", True), row("c", "A", True), row("c", "B", None, valid=False, handoff="rejected"),
            row("c", "A", True) | {"slot": 1}]
    rows = rows[:6]
    rep = an.analyze(rows, DEF)
    s, v = rep["as_scheduled"]["outcomes"], rep["valid_runs"]["outcomes"]
    assert s["pairs"] == 3 and v["pairs"] == 2
    assert s["cells"]["A_only"] == ["c"]  # the invalid B run counts as unresolved
    assert v["cells"]["B_only"] == ["b"] and v["cells"]["both_resolved"] == ["a"]
    assert rep["not_run_tasks"][0]["task_id"] == "d" and "FEASIBILITY PILOT" in rep["label"]
    assert rep["exclusions_from_valid_runs"]["slots"] == 1


def test_uncached_equivalent():
    assert rs.uncached_equivalent_usd({"input_tokens": 1_000_000, "cache_read_tokens": 1_000_000, "output_tokens": 1_000_000}, P) == "14"


def good_row():
    return {"schema": 1, "experiment_id": "e", "slot_id": "e/t/A/r1", "task_id": "t", "repo": "r", "arm": "A", "pair_id": "e:t:r1", "run_id": "x", "handoff_status": "ready",
            "cost_usd_computed": "0.5", "uncached_equivalent_usd": "0.5", "repetition": 1, "order_index": 1, "arm_position": 1, "requests": 1, "non_success_requests": 0,
            "turns": 1, "test_runs": 0, "test_runs_failed": 0, "test_fix_cycles": 0, "edits_total": 0, "tool_calls_total": 0, "valid": True, "graded": True,
            "valid_measurement": True, "cost_exact": True, "accounting_complete": True, "test_usage": False, "compliance": False, "cap_exceeded": False, "resolved": True,
            "invalid_reason": None, "stop_reason": None, "handoff_rejections": [], "handoff_flags": [], "tokens": {}, "request_categories": {}}


def test_writer_append_only(tmp_path):
    w = rs.ResultsWriter(tmp_path / "r.jsonl")
    w.append(good_row())
    with pytest.raises(rs.ResultsError):
        w.append(good_row())
    assert len(rs.ResultsWriter(tmp_path / "r.jsonl").rows()) == 1


def test_row_validation():
    r = good_row() | {"graded": False}
    with pytest.raises(rs.ResultsError):
        rs.validate_row(r)  # resolved without graded
    with pytest.raises(rs.ResultsError):
        rs.validate_row(good_row() | {"slot_id": "wrong"})
    with pytest.raises(rs.ResultsError):
        rs.validate_row(good_row() | {"handoff_status": "rejected"})

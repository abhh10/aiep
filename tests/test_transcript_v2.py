"""Transcript metrics version 2: complete shell parsing (here-documents), test-run states, confirmed versus heuristic edits, and the conservative test-fix cycle.
Includes the structure observed in calibration-2. Pure functions: no Docker, network or API."""
import json
from pathlib import Path

import pytest

from aiep import results
from aiep.transcript import (PARSER_VERSION, TEST_COMMAND_SPEC, bash_edit_like, classify_test_run, is_test_command, parse_transcript)

REAL = Path(__file__).resolve().parents[1]
CAL2 = REAL / "runs" / "calibration" / "m2-sympy__sympy-18621-B-20261122T173619Z" / "claude_stdout.jsonl"


# ---------------------------------------------------------------- transcript builders
def tu(i, name, **inp):
    return json.dumps({"type": "assistant", "message": {"id": f"m{i}", "content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}})


def tr(i, content, **extra):
    return json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": content, **extra}]}})


RESULT = json.dumps({"type": "result", "subtype": "success", "num_turns": 3, "total_cost_usd": 0.01})


def parse(*lines, cap=5):
    return parse_transcript("\n".join([*lines, RESULT]) + "\n", cycle_cap=cap)


def bash(i, command, output="", **extra):
    return [tu(i, "Bash", command=command), tr(i, output, **extra)]


PYTEST_FAIL = "============ 1 failed, 2 passed in 0.31s ============"
PYTEST_PASS = "============ 3 passed in 0.30s ============"
PYTEST_MISSING = "/opt/miniconda3/envs/testbed/bin/python: No module named pytest"


# ---------------------------------------------------------------- the structure observed in calibration-2
CAL2_EDIT_AND_PYTEST = """python - <<'E'
p='sympy/matrices/expressions/blockmatrix.py'
s=open(p).read()
s=s.replace(\"\"\"        return old_expression(data)\"\"\",\"\"\"        return new_expression(data, flag=True)\"\"\")  # synthetic text: only the heredoc and the file write matter to the parser
open(p,'w').write(s)
E
python -c "
from sympy import *
M=Matrix([[1,2],[3,4]])
print(Matrix(BlockDiagMatrix(M)));print(Matrix(BlockDiagMatrix(M,M)))"
python -m pytest -q sympy/matrices/expressions/tests/test_blockmatrix.py 2>&1 | tail -3"""
CAL2_SYMPY_RUNNER = """python -m sympy.testing.runtests 2>&1 | head -0; python -c "
import sympy;sympy.test('sympy/matrices/expressions/tests/test_blockmatrix.py')" 2>&1 | tail -5"""
CAL2_OUT_C = "Matrix([[1, 2], [3, 4]])\nMatrix([[1, 2, 0, 0], [3, 4, 0, 0], [0, 0, 1, 2], [0, 0, 3, 4]])\n" + PYTEST_MISSING
CAL2_OUT_D = ("hash randomization: on (PYTHONHASHSEED=781307485)\n\nsympy/matrices/expressions/tests/test_blockmatrix.py[16] ................   [OK]\n\n"
              "================== tests finished: 16 passed, in 0.52 seconds ==================")


def cal2():
    return parse(tu(1, "Grep", pattern="def blocks"), tr(1, "145: def blocks"), tu(2, "Read", file_path="/testbed/sympy/matrices/expressions/blockmatrix.py"), tr(2, "272 ..."),
                 *bash(3, 'python -c "\nfrom sympy import *\nM=Matrix([[1,2],[3,4]])\nprint(BlockDiagMatrix(M).blocks)"', "Matrix(...)", is_error=False),
                 *bash(4, CAL2_EDIT_AND_PYTEST, CAL2_OUT_C, is_error=False), *bash(5, CAL2_SYMPY_RUNNER, CAL2_OUT_D, is_error=False))


def test_calibration_2_structure_the_test_after_the_heredoc_is_seen_and_the_missing_pytest_is_not_a_failed_suite():
    m = cal2()
    assert m["parser_version"] == PARSER_VERSION == 2
    assert (m["test_runs"], m["test_runs_not_started"], m["test_runs_started"], m["test_runs_completed"], m["test_runs_passed"], m["test_runs_failed"]) == (2, 1, 1, 1, 1, 0)
    assert m["test_fix_cycles"] == 0 and m["compliance"] is True and m["test_usage"] is True and m["test_usage_started"] is True and m["final_test_run_state"] == "completed_passed"
    assert m["edits_total"] == 0 and m["bash_edit_like_calls"] == 1  # the python heredoc wrote a file
    kinds = {d["call_index"]: d for d in m["analysis_details"]}
    assert kinds[3]["state"] == "not_started" and "did not start" in kinds[3]["evidence"]
    edit = next(d for d in m["analysis_details"] if d["kind"] == "bash_edit_like")
    assert edit["classification"] == ["python_write"] and edit["source"] == "heuristic" and "blockmatrix.py" in " ".join(edit["targets"])


def test_the_old_behaviour_missed_both_the_test_and_the_edit():
    """What version 1 did with the same two commands (documented, not run): the text after '<<' was ignored, so the pytest attempt was not seen, and the heredoc edit was not recognised."""
    assert is_test_command(CAL2_EDIT_AND_PYTEST) is True  # version 2 sees the command after the heredoc
    head_only = CAL2_EDIT_AND_PYTEST.partition("<<")[0]
    assert is_test_command(head_only) is False  # version 1's view


@pytest.mark.skipif(not CAL2.exists(), reason="the (git-ignored) calibration-2 transcript is not on this machine")
def test_the_real_calibration_2_transcript_gives_the_corrected_metrics_without_touching_it():
    before = CAL2.read_bytes()
    m = parse_transcript(before, cycle_cap=5)
    assert CAL2.read_bytes() == before
    assert (m["test_runs"], m["test_runs_not_started"], m["test_runs_started"], m["test_runs_passed"], m["test_runs_failed"]) == (2, 1, 1, 1, 0)
    assert m["bash_edit_like_calls"] == 1 and m["edits_total"] == 0 and m["test_fix_cycles"] == 0 and m["compliance"] is True
    assert parse_transcript(before, cycle_cap=5) == m  # deterministic


# ---------------------------------------------------------------- here-documents
HEREDOC_CASES = [
    ("python - <<'E'\nprint(1)\nE\npytest x", True),
    ('python - <<"E"\nprint(1)\nE\npytest x', True),
    ("python - <<E\nprint(1)\nE\npytest x", True),
    ("python - <<-E\n\tprint(1)\n\tE\npytest x", True),
    ("cat <<'EOF' > /tmp/x\nhello\nEOF\npython -m pytest -q tests", True),
    ("python - <<E\nprint(1)\nE\nls\npytest a\necho done", True),
    ("python - <<E\nprint(1)\nE\nls && echo ok; pytest a", True),
    ("cat <<A <<B\none\nA\ntwo\nB\npytest x", True),
    ("python - <<E | tail -3\nprint(1)\nE\npytest x", True),
    ("python - <<E\nprint(1)\nE | tail -3\npytest x", False),  # 'E | tail -3' is not a terminator, so the body runs on to the end (unterminated here-document)
    # misleading text inside a body is data, never a command
    ("cat <<EOF\npytest tests\nsed -i s/a/b/ f.py\nrm -rf /\nEOF", False),
    ("cat <<'EOF'\npython -m pytest x\nEOF\nls", False),
    ("python - <<E\nprint('pytest')\nE", False),
    ("echo hi <<E\nbin/test sympy\nE", False),
    # not here-documents
    ("cat <<< 'pytest x'", False),
    ('echo "a << b"; pytest x', True),
    ("echo $((1 << 3)); pytest x", True),
    ("echo $((1 << 3))", False),
    # a heredoc marker inside a quoted multi-line string is not a heredoc
    ("python -c \"\nx = 1 << E\nprint(x)\n\"\npytest y", True),
    # a stdin program that runs a test
    ("python - <<'E'\nimport pytest\npytest.main(['x'])\nE", True),
    ("python <<E\nimport unittest\nunittest.main()\nE", True),
    # unterminated heredoc: everything to the end is the body
    ("python - <<E\nprint(1)\npytest x", False),
    # terminator with trailing carriage return
    ("python - <<E\r\nprint(1)\r\nE\r\npytest x", True),
]


@pytest.mark.parametrize("cmd,expected", HEREDOC_CASES)
def test_heredoc_handling(cmd, expected):
    assert is_test_command(cmd) is expected, cmd


def test_heredoc_bodies_are_never_edit_commands_but_a_python_body_that_writes_is():
    assert bash_edit_like("cat <<EOF\nsed -i s/a/b/ f.py\ncat > x.py\nEOF") == []
    (e,) = bash_edit_like("python - <<E\nopen('a.py','w').write('x')\nE")
    assert e["kind"] == "python_write" and e["source"] == "heuristic" and e["targets"] == ["a.py"]


def test_the_registered_test_command_spec_is_unchanged():
    assert TEST_COMMAND_SPEC["version"] == 2 and "here-documents are set aside" in TEST_COMMAND_SPEC["method"]
    defin = json.loads((REAL / "config" / "experiments" / "m3-pilot-v2.json").read_text())
    assert defin["metrics"]["test_command_spec"] == TEST_COMMAND_SPEC  # the hashed v2 definition still matches the code


# ---------------------------------------------------------------- test-run states
@pytest.mark.parametrize("out,state", [
    (PYTEST_MISSING, "not_started"), ("bash: pytest: command not found", "not_started"), ("ModuleNotFoundError: No module named 'pytest'", "not_started"),
    ("python: can't open file '/testbed/runtests.py'", "not_started"),
    (PYTEST_FAIL, "completed_failed"), ("FAILED tests/test_a.py::test_x - assert 1 == 2\n1 failed in 0.1s", "completed_failed"), ("Ran 3 tests in 0.1s\n\nFAILED (failures=1)", "completed_failed"),
    ("hash randomization: on\ntest_a.py[2] .F  [FAIL]\n= tests finished: 1 passed, 1 failed, in 0.2 seconds =", "completed_failed"),
    ("Ran 2 tests in 0.1s\n\nFAILED (errors=1)", "completed_failed"),
    (PYTEST_PASS, "completed_passed"), ("Ran 3 tests in 0.1s\n\nOK", "completed_passed"), (CAL2_OUT_D, "completed_passed"),
    ("============ test session starts ============\ncollected 5 items\n\ntests/test_a.py ..", "started_incomplete"),
    ("hash randomization: on\ntest_a.py[2] ..", "started_incomplete"),
    ("", "unknown"), ("Exit code 1", "unknown"), ("some unrelated output", "unknown"),
])
def test_test_run_classification(out, state):
    assert classify_test_run(out)[0] == state, out


def test_a_started_suite_whose_tests_import_a_missing_module_is_a_failed_suite_not_a_missing_runner():
    out = "============ test session starts ============\ncollected 1 item\n\ntests/test_a.py F\nE   ModuleNotFoundError: No module named 'pytest_foo'\n============ 1 failed in 0.1s ============"
    assert classify_test_run(out)[0] == "completed_failed"


def test_a_missing_pytest_is_never_a_failed_run_and_never_starts_a_cycle():
    m = parse(*bash(1, "python -m pytest -q tests", PYTEST_MISSING), tu(2, "Edit", file_path="f.py"), tr(2, "ok"), *bash(3, "python -m pytest -q tests", PYTEST_MISSING))
    assert (m["test_runs"], m["test_runs_failed"], m["test_runs_not_started"], m["test_runs_started"], m["test_fix_cycles"]) == (2, 0, 2, 0, 0)
    assert m["compliance"] is False and m["test_usage"] is True and m["test_usage_started"] is False  # attempts do not count as a test run that happened


def test_a_failed_execution_is_a_failed_run():
    m = parse(*bash(1, "pytest x", PYTEST_FAIL))
    assert m["test_runs_failed"] == 1 and m["test_runs_completed"] == 1 and m["final_test_run_state"] == "completed_failed" and m["ended_with_passing_tests"] is False


# ---------------------------------------------------------------- confirmed versus heuristic edits
@pytest.mark.parametrize("cmd,kind", [
    ("python - <<E\nopen('sympy/a.py','w').write('x')\nE", "python_write"),
    ("python - <<E\nfrom pathlib import Path\nPath('a.py').write_text('x')\nE", "python_write"),
    ("python -c \"open('a.py', 'w').write('x')\"", "python_write"),
    ("python3 - <<'E'\nwith open('f.rst','a') as f:\n    f.write('x')\nE", "python_write"),
    ("sed -i 's/a/b/' sympy/a.py", "sed_i"), ("sed -i.bak -e 's/a/b/' a.py b.py", "sed_i"), ("sed --in-place 's/a/b/' a.py", "sed_i"), ("sed -ni 's/a/b/p' a.py", "sed_i"),
    ("perl -pi -e 's/a/b/' a.py", "perl_i"), ("perl -i.bak -pe 's/a/b/' a.py", "perl_i"),
    ("cat > sympy/new.py <<'E'\nx = 1\nE", "cat_redirect"), ("cat >> notes.md", "cat_redirect"),
    ("echo 'x = 1' > sympy/a.py", "redirect_write"), ("printf 'x' >> a.cfg", "redirect_write"), ("python gen.py > out.py", "redirect_write"),
    ("echo hi | tee sympy/a.py", "tee"), ("tee -a a.py < b.py", "tee"),
    ("git apply fix.patch", "vcs_or_patch"), ("patch -p1 < fix.diff", "vcs_or_patch"), ("git restore sympy/a.py", "vcs_or_patch"),
])
def test_bash_edit_like_commands_are_recognised_and_labelled_heuristic(cmd, kind):
    found = bash_edit_like(cmd)
    assert found and found[0]["kind"] == kind and all(f["source"] == "heuristic" for f in found), cmd


@pytest.mark.parametrize("cmd", [
    "cat sympy/a.py", "grep -rn 'x' .", "sed -n '1,20p' a.py", "sed 's/a/b/' a.py", "sed -e 's/a/b/' a.py", "head -5 a.py | tee", "python -c \"print(open('a.py').read())\"",
    "python - <<E\nprint(open('a.py').read())\nE", "python script.py", "pytest tests 2>&1 | tail -3", "pytest tests > /tmp/out.txt", "ls > /dev/null 2>&1", "python x.py 2>/dev/null",
    "tee /dev/null", "tee /tmp/x", "echo hi > /tmp/note", "python - <<E\nopen('/tmp/x.py','w').write('x')\nE", "python gen.py > run.log", "git diff", "git status", "git log -3",
    "perl -ne 'print' a.py", "cat a.py b.py", "find . -name '*.py' | xargs grep x", "echo hi", "sed -i", "ls -la; cd sympy; pwd",
])
def test_reads_searches_tests_and_scratch_writes_are_not_edit_like(cmd):
    assert bash_edit_like(cmd) == [], cmd


def test_edit_tool_calls_are_confirmed_unless_they_errored():
    m = parse(tu(1, "Edit", file_path="a.py"), tr(1, "updated"), tu(2, "Edit", file_path="a.py"), tr(2, "String to replace not found", is_error=True), tu(3, "Write", file_path="b.py"), tr(3, "created"))
    assert m["edits_total"] == 3 and m["edit_tool_calls_errored"] == 1 and m["bash_edit_like_calls"] == 0


def test_the_audit_trail_says_how_each_edit_was_classified():
    m = parse(*bash(1, "sed -i 's/a/b/' sympy/a.py; python - <<E\nopen('x.py','w').write('')\nE", ""))
    (d,) = [x for x in m["analysis_details"] if x["kind"] == "bash_edit_like"]
    assert d["classification"] == ["sed_i", "python_write"] and d["source"] == "heuristic" and "sympy/a.py" in d["targets"] and d["command_excerpt"].startswith("sed -i")


# ---------------------------------------------------------------- the conservative test-fix cycle
def test_failure_edit_rerun_is_one_cycle_with_a_confirmed_edit():
    m = parse(*bash(1, "pytest x", PYTEST_FAIL), tu(2, "Edit", file_path="a.py"), tr(2, "ok"), *bash(3, "pytest x", PYTEST_PASS))
    assert (m["test_fix_cycles"], m["test_fix_cycles_confirmed_edit"], m["test_fix_cycles_bash_edit_only"], m["compliance"], m["unfinished_cycles"]) == (1, 1, 0, True, 0)


def test_a_bash_only_edit_makes_a_cycle_that_is_reported_as_bash_edit_only():
    m = parse(*bash(1, "pytest x", PYTEST_FAIL), *bash(2, "sed -i 's/a/b/' sympy/a.py"), *bash(3, "pytest x", PYTEST_PASS))
    assert (m["test_fix_cycles"], m["test_fix_cycles_confirmed_edit"], m["test_fix_cycles_bash_edit_only"]) == (1, 0, 1)


def test_an_edit_and_the_rerun_in_the_same_bash_call_close_the_cycle_in_order():
    m = parse(*bash(1, "pytest x", PYTEST_FAIL), *bash(2, "sed -i 's/a/b/' sympy/a.py && pytest x", PYTEST_PASS))
    assert m["test_fix_cycles"] == 1 and m["test_fix_cycles_bash_edit_only"] == 1
    m = parse(*bash(1, "pytest x", PYTEST_FAIL), *bash(2, "pytest x; sed -i 's/a/b/' sympy/a.py", PYTEST_FAIL))  # the edit comes AFTER this run
    assert m["test_fix_cycles"] == 0 and m["unfinished_cycles"] == 1


def test_multiple_cycles_in_order():
    m = parse(*bash(1, "pytest x", PYTEST_FAIL), tu(2, "Edit", file_path="a.py"), tr(2, "ok"), *bash(3, "pytest x", PYTEST_FAIL), tu(4, "Write", file_path="a.py"), tr(4, "ok"),
              *bash(5, "pytest x", PYTEST_PASS))
    assert m["test_fix_cycles"] == 2 and m["test_runs_failed"] == 2 and m["test_runs_passed"] == 1 and m["compliance"] is True and m["cap_exceeded"] is False


def test_the_cap_is_applied_to_the_cycle_count():
    lines = []
    for i in range(7):
        lines += [*bash(10 * i + 1, "pytest x", PYTEST_FAIL), tu(10 * i + 2, "Edit", file_path="a.py"), tr(10 * i + 2, "ok")]
    lines += bash(99, "pytest x", PYTEST_PASS)
    m = parse(*lines, cap=5)
    assert m["test_fix_cycles"] == 7 and m["cap_exceeded"] is True and m["compliance"] is False


def test_a_failed_run_before_an_edit_that_never_gets_a_rerun_is_not_a_cycle_but_is_reported():
    m = parse(*bash(1, "pytest x", PYTEST_FAIL), tu(2, "Edit", file_path="a.py"), tr(2, "ok"))
    assert m["test_fix_cycles"] == 0 and m["unfinished_cycles"] == 1 and m["compliance"] is False
    assert [a["kind"] for a in m["ambiguous_sequences"]] == ["failure_and_edit_without_a_later_run"]


def test_a_rerun_with_no_edit_is_ambiguous_not_a_cycle():
    m = parse(*bash(1, "pytest x", PYTEST_FAIL), *bash(2, "pytest x", PYTEST_PASS))
    assert m["test_fix_cycles"] == 0 and [a["kind"] for a in m["ambiguous_sequences"]] == ["rerun_without_an_edit"] and m["compliance"] is False


def test_a_missing_runner_after_a_failure_and_edit_cannot_close_the_cycle():
    m = parse(*bash(1, "pytest x", PYTEST_FAIL), tu(2, "Edit", file_path="a.py"), tr(2, "ok"), *bash(3, "python -m pytest x", PYTEST_MISSING))
    assert m["test_fix_cycles"] == 0 and m["unfinished_cycles"] == 1 and m["test_runs_not_started"] == 1


def test_a_missing_runner_between_the_failure_and_the_edit_does_not_reset_the_open_failure():
    m = parse(*bash(1, "pytest x", PYTEST_FAIL), *bash(2, "python -m pytest x", PYTEST_MISSING), tu(3, "Edit", file_path="a.py"), tr(3, "ok"), *bash(4, "pytest x", PYTEST_PASS))
    assert m["test_fix_cycles"] == 1


def test_a_later_run_with_no_summary_closes_the_cycle_but_is_flagged_ambiguous():
    m = parse(*bash(1, "pytest x", PYTEST_FAIL), tu(2, "Edit", file_path="a.py"), tr(2, "ok"), *bash(3, "pytest x", "============ test session starts ============\ncollected 5 items\n"))
    assert m["test_fix_cycles"] == 1 and [a["kind"] for a in m["ambiguous_sequences"]] == ["cycle_closed_by_a_run_with_no_summary"]


def test_a_later_run_with_no_evidence_leaves_the_cycle_open_and_ambiguous():
    m = parse(*bash(1, "pytest x", PYTEST_FAIL), tu(2, "Edit", file_path="a.py"), tr(2, "ok"), *bash(3, "pytest x", "", is_error=True))
    assert m["test_fix_cycles"] == 0 and "run_with_no_evidence_after_a_failure" in [a["kind"] for a in m["ambiguous_sequences"]]


def test_several_test_commands_in_one_call_are_one_attempt_and_flagged():
    m = parse(*bash(1, "pytest a; pytest b", PYTEST_FAIL))
    assert m["test_runs"] == 1 and [a["kind"] for a in m["ambiguous_sequences"]] == ["several_test_commands_in_one_call"]


def test_edits_before_the_first_failure_never_count_toward_a_cycle():
    m = parse(tu(1, "Edit", file_path="a.py"), tr(1, "ok"), *bash(2, "pytest x", PYTEST_FAIL), *bash(3, "pytest x", PYTEST_PASS))
    assert m["test_fix_cycles"] == 0 and m["edits_total"] == 1


def test_config_a_style_run_with_no_tests_is_not_test_usage():
    m = parse(tu(1, "Edit", file_path="a.py"), tr(1, "ok"))
    assert m["test_runs"] == 0 and m["test_usage"] is False and m["test_usage_started"] is False and m["compliance"] is False and m["final_test_state"] is None


# ---------------------------------------------------------------- schema compatibility, determinism and separation
def test_every_version_1_field_is_still_present_and_the_output_is_deterministic_json():
    m = parse(*bash(1, "pytest x", PYTEST_FAIL), tu(2, "Edit", file_path="a.py"), tr(2, "ok"), *bash(3, "pytest x", PYTEST_PASS))
    for k in ("schema", "complete", "unparseable_lines", "result_present", "result", "assistant_messages", "turns_reported", "tool_calls_total", "tool_calls_by_name", "bash_edit_like_calls",
              "edits_total", "test_runs", "test_runs_failed", "test_runs_passed", "test_runs_unknown", "first_test_run_call_index", "edits_after_first_test_run", "test_fix_cycles",
              "cycle_cap", "cap_exceeded", "test_usage", "compliance", "final_test_state", "ended_with_passing_tests"):
        assert k in m, k
    assert m["schema"] == 1 and json.loads(json.dumps(m)) == m
    assert parse(*bash(1, "pytest x", PYTEST_FAIL), tu(2, "Edit", file_path="a.py"), tr(2, "ok"), *bash(3, "pytest x", PYTEST_PASS)) == m


def test_the_unparseable_and_incomplete_transcript_rules_are_unchanged():
    m = parse_transcript("not json\n" + tu(1, "Edit", file_path="a.py") + "\n", cycle_cap=5)
    assert m["complete"] is False and m["unparseable_lines"] == 1 and m["result_present"] is False


def test_calibration_rows_are_still_refused_whatever_the_transcript_says(tmp_path):
    m = cal2()
    run = {"run_id": "r", "calibration": {"id": "m3-calibration-2"}, "usage_measured": {}, "accounting": {}, "lifecycle": {}}
    (tmp_path / "run.json").write_text(json.dumps(run))
    ident = {"experiment_id": "e", "task_id": "t", "arm": "B", "repetition": 1, "pair_id": "p", "order_index": 1, "slot_id": "e/t/B/r1", "arm_position": 1}
    with pytest.raises(results.ResultsError, match="calibration"):
        results.build_row(tmp_path, ident, {"status": "ready"}, None, m, definition_sha256="h" * 64, pricing=None, repo="r/r")


def test_result_rows_carry_the_version_2_fields_as_optional_extras(tmp_path):
    from aiep.pricing import load_pricing
    pricing = load_pricing(REAL / "config" / "model_pricing.json", json.loads((REAL / "config" / "experiment.json").read_text())["model"])
    run = {"run_id": "r", "model": "m", "experiment": None, "usage_measured": {"computed_usd_upper_bound_basis": "0.05", "tokens": {}, "exact": True, "request_status": {}}, "accounting": {"complete": True, "exact": True},
           "lifecycle": {"status": "completed", "agent": {"valid": True, "patch": {}}}}
    (tmp_path / "run.json").write_text(json.dumps(run))
    ident = {"experiment_id": "e", "task_id": "t", "arm": "B", "repetition": 1, "pair_id": "p", "order_index": 1, "slot_id": "e/t/B/r1", "arm_position": 1}
    row = results.build_row(tmp_path, ident, {"status": "ready"}, None, cal2(), definition_sha256="h" * 64, pricing=pricing, repo="r/r")
    assert row["transcript_parser_version"] == 2 and row["test_runs_started"] == 1 and row["test_runs_not_started"] == 1 and row["bash_edit_like_calls"] == 1
    assert row["test_runs"] == 2 and row["test_runs_failed"] == 0 and row["test_fix_cycles"] == 0 and row["compliance"] is True
    results.validate_row(row)
    old_row = {k: v for k, v in row.items() if k not in ("transcript_parser_version", "test_runs_started", "test_runs_not_started", "bash_edit_like_calls", "test_fix_cycles_confirmed_edit", "ambiguous_sequences")}
    results.validate_row(old_row)  # rows written before version 2 stay valid

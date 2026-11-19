import json
from pathlib import Path

import pytest

from aiep import task_input
from aiep.task_input import ALLOWED_FIELDS, TaskInputError, export_task_input

REAL = Path(__file__).resolve().parents[1]
SHA = "2051e30b9b596e944524ccb787ed20f9f5be93e3"

HIDDEN = {
    "patch": "CANARY-GOLD-PATCH diff --git a/x b/x",
    "test_patch": "CANARY-TEST-PATCH def test_hidden(): pass",
    "FAIL_TO_PASS": ["CANARY-F2P::test_a"],
    "PASS_TO_PASS": ["CANARY-P2P::test_b"],
    "eval_script": "CANARY-EVAL-SCRIPT #!/bin/bash",
    "hints_text": "CANARY-HINTS the fix is in foo.py",
    "image": "CANARY-IMAGE swebench/sweb.eval...",
    "log_parser": "CANARY-PARSER",
    "environment_setup_commit": "CANARY-ENVCOMMIT",
    "version": "CANARY-VERSION",
    "created_at": "CANARY-DATE",
    "difficulty": "CANARY-DIFF",
    "eval_type": "CANARY-TYPE",
    "some_new_future_column": "CANARY-FUTURE",
}


def record(**over):
    r = {"instance_id": "pytest-dev__pytest-5227", "repo": "pytest-dev/pytest", "base_commit": SHA,
         "problem_statement": "Improve default logging format", **HIDDEN}
    r.update(over)
    return r


def test_schema_is_exactly_four_fields():
    assert ALLOWED_FIELDS == ("instance_id", "repo", "base_commit", "problem_statement")
    assert set(json.loads(export_task_input(record()).to_json())) == set(ALLOWED_FIELDS)


def test_no_hidden_canary_or_future_column_leaks():
    out = export_task_input(record()).to_json()
    assert "CANARY" not in out
    for v in HIDDEN.values():
        assert str(v) not in out


def test_hidden_text_inside_allowed_field_name_is_not_added_by_export():
    # export copies the four values verbatim and adds nothing else
    out = json.loads(export_task_input(record(problem_statement="hello")).to_json())
    assert out == {"instance_id": "pytest-dev__pytest-5227", "repo": "pytest-dev/pytest",
                   "base_commit": SHA, "problem_statement": "hello"}


@pytest.mark.parametrize(
    "over",
    [
        {"instance_id": ""},
        {"instance_id": "no-double-underscore-1"},
        {"instance_id": "a__b-1; rm -rf /"},
        {"repo": "not a repo"},
        {"repo": "../etc/passwd"},
        {"base_commit": "main"},
        {"base_commit": SHA.upper()},
        {"problem_statement": "   "},
        {"problem_statement": None},
        {"problem_statement": "x" * 200_001},
        {"base_commit": 1},
    ],
)
def test_invalid_fields_rejected(over):
    with pytest.raises(TaskInputError):
        export_task_input(record(**over))


def test_missing_field_rejected():
    r = record()
    del r["problem_statement"]
    with pytest.raises(TaskInputError):
        export_task_input(r)


def test_dataclass_is_frozen():
    t = export_task_input(record())
    with pytest.raises(Exception):
        t.problem_statement = "changed"


def test_real_record_exports_without_any_hidden_text():
    """Against the real pinned local snapshot, if present: no hidden field's text appears in the export.

    ``load_record`` reads the verified snapshot directly (no ``datasets``/``huggingface_hub``), so this needs no network
    switches and no fakes; tests/conftest.py fails the test if anything tries the network."""
    hf = REAL / ".cache" / "hf"
    if not hf.exists():
        pytest.skip("no local HF cache")
    rec = task_input.load_record("pytest-dev__pytest-5227", hf, REAL)
    out = export_task_input(rec).to_json()
    for name in ("patch", "test_patch", "eval_script", "hints_text"):
        text = rec.get(name)
        if isinstance(text, str) and len(text) > 20:
            assert text not in out, name

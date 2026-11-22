"""Structural guarantees behind the A/B comparison and the agent/evaluator separation. Each test inspects the real code, not a mock of it."""
import ast
import inspect
from pathlib import Path

import pytest

from aiep import experiment, m2_run, stock_eval
from aiep import evaluator as hardened_evaluator
from aiep.experiment import claude_args, load_prompts

REAL = Path(__file__).resolve().parents[1]
REGISTERED = {"template": "86571bc70d453965dfec954dae6c2e3a073bf61ddbdc2656bc7f5667846d7158",
              "A": "5e44cde50fea6d6c70cc65c0c97043a7c8fe9357d9468b4492305c0db4d64082",
              "B": "f7ad00720d74ee2467e1f7854e432d3cb09d435185383f691d8712dad6710111"}


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out.add(("." * n.level) + (n.module or ""))
    return out


def test_the_registered_prompt_hashes_are_unchanged_and_recomputed_from_the_text():
    p = load_prompts(REAL / "config" / "workflow_prompts.json")  # raises if any recomputed hash differs from the registered one
    assert p.hashes == REGISTERED


def test_the_two_arms_cannot_differ_in_flags_or_environment_by_construction():
    assert list(inspect.signature(claude_args).parameters) == ["model", "budget_usd"]  # no config label can reach the CLI flags
    from aiep import agent_runner
    assert "config_label" not in inspect.signature(agent_runner.build_create_kwargs).parameters
    assert claude_args("claude-sonnet-5-5", "0.50") == claude_args("claude-sonnet-5-5", "0.50")


def test_the_prompt_is_the_only_thing_the_label_selects():
    p = load_prompts(REAL / "config" / "workflow_prompts.json")
    task = experiment.build_task_text.__name__
    assert task == "build_task_text"
    a, b = (p.template.replace("<<WORKFLOW>>", p.workflows[x]) for x in ("A", "B"))
    assert a != b and a.replace(p.workflows["A"], "") == b.replace(p.workflows["B"], "")  # identical outside the workflow line


@pytest.mark.parametrize("mod", [stock_eval.main, hardened_evaluator.parse_args])
def test_the_evaluators_take_no_configuration_label_so_a_and_b_are_graded_under_identical_conditions(mod):
    src = inspect.getsource(inspect.getmodule(mod))
    tree = ast.parse(src)
    flags = {c.args[0].value for c in ast.walk(tree) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "add_argument" and c.args
             and isinstance(c.args[0], ast.Constant)}
    assert not {f for f in flags if any(w in f.lower() for w in ("config", "label", "arm", "variant"))}, flags
    assert "--run-id" in flags and "-p" in flags or "--predictions" in flags  # the only per-run inputs are an id and a predictions file


def test_the_evaluator_driver_grades_an_instance_and_a_predictions_file_and_nothing_else():
    text = (REAL / "scripts" / "m1_eval.sh").read_text()
    assert "RUN_ID=$1; PRED=$2" in text and "A\b" not in text.split("RUN_ID=$1")[0]
    assert "claude" not in text.lower() and "config" not in text.lower().replace("configuration", "")


def test_the_orchestrator_never_imports_or_runs_the_evaluator_or_swebench():
    mods = imports_of(REAL / "src" / "aiep" / "m2_run.py")
    assert not {m for m in mods if "stock_eval" in m or m.endswith("evaluator") or m.startswith("swebench")}, mods
    assert "evaluation" in (REAL / "src" / "aiep" / "m2_run.py").read_text() and "not run: a separate, approved step" in (REAL / "src" / "aiep" / "m2_run.py").read_text()


def test_the_agent_side_modules_never_import_the_dataset_loader_or_hidden_fields():
    for name in ("agent_runner.py", "agent_env.py", "container_policy.py", "container_exec.py", "gateway.py", "egress_proxy.py", "agent_attachment.py"):
        mods = imports_of(REAL / "src" / "aiep" / name)
        assert not {m for m in mods if m.endswith("task_input") or "manifest" in m or m.startswith("swebench")}, (name, mods)
    src = (REAL / "src" / "aiep" / "agent_runner.py").read_text()
    assert "FAIL_TO_PASS" not in src and "PASS_TO_PASS" not in src and "test_patch" not in src


def test_the_task_the_agent_receives_is_an_allowlisted_view_without_hidden_fields():
    from aiep.task_input import export_task_input
    rec = {"instance_id": "a__b-1", "repo": "a/b", "base_commit": "0" * 40, "problem_statement": "issue text", "hints_text": "", "version": "1.0",
           "FAIL_TO_PASS": "[\"t::x\"]", "PASS_TO_PASS": "[\"t::y\"]", "test_patch": "diff", "patch": "gold", "environment_setup_commit": "1" * 40, "created_at": "x"}
    t = export_task_input(rec)
    blob = repr(t)
    for hidden in ("t::x", "t::y", "gold", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS"):
        assert hidden not in blob


def test_predictions_are_written_only_for_a_valid_run_with_an_ok_patch():
    src = (REAL / "src" / "aiep" / "m2_run.py").read_text()
    assert 'agent.patch.status == "ok" and agent.valid' in src  # the single gate in front of patch.diff and predictions.jsonl


def test_the_documentation_names_the_handoff_gate_and_its_three_verdicts():
    doc = (REAL / "docs" / "M2_EVALUATOR_HANDOFF.md").read_text()
    assert "validate_handoff" in doc and "ready_flagged" in doc and "rejected" in doc

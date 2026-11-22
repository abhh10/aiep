"""run_m2_agent --prompts-file: calibration-2 can select the registered v2 prompt set (Config B with the five-cycle cap) explicitly; the default stays v1; arbitrary files are refused.
Fakes only: no Docker, network or API."""
import hashlib
import json
from pathlib import Path

import pytest

from aiep import experiment, handoff, m2_run
from test_agent_runner import repo, seed  # noqa: F401 (fixtures)
from test_calibration import Stop, spy  # noqa: F401 (fixture)
from test_m2_run import GIB, KEY, Client, cli, drive, fake_record, full, good_nets, lifecycle, prep, root  # noqa: F401 (fixtures)

REAL = Path(__file__).resolve().parents[1]
V1, V2 = "config/workflow_prompts.json", "config/workflow_prompts_v2.json"
FIVE = "at most 5 test-fix cycles"
HASH_B_V1, HASH_B_V2, HASH_A = "f7ad00720d74ee2467e1f7854e432d3cb09d435185383f691d8712dad6710111", "cd6fbb95384237c2dcc399d61456ca92d746242384792f81c57217596aeb2d27", \
    "5e44cde50fea6d6c70cc65c0c97043a7c8fe9357d9468b4492305c0db4d64082"


def argv_b(root, *extra):
    return ["--config", "B", "--budget-usd", "0.75", "--wall-clock-s", "1200", "--claude-binary", str(root / "claude"), "--execute", "--approve-spend", "0.75",
            "--max-concurrent", "1", "--calibration", "m3-calibration-2", *extra]


def test_the_registered_sets_are_the_two_prompt_files_and_one_definition_serves_everyone():
    assert experiment.REGISTERED_PROMPT_SETS == (V1, V2) and handoff.ALLOWED_PROMPT_SETS == experiment.REGISTERED_PROMPT_SETS


def test_the_default_is_the_v1_file_and_an_explicit_v2_reaches_prepare(root, spy):
    drive(root, argv_b(root))
    assert spy["prompts_file"] == V1
    spy.clear()
    drive(root, argv_b(root, "--prompts-file", V2))
    assert spy["prompts_file"] == V2 and spy["calibration"] == "m3-calibration-2" and spy["max_concurrent"] == 1


@pytest.mark.parametrize("bad", ["config/other.json", "config/workflow_prompts_v3.json", "/etc/passwd", "../workflow_prompts.json", "config/../config/workflow_prompts_v2.json",
                                 " config/workflow_prompts_v2.json", "config/workflow_prompts_v2.json ", "", "workflow_prompts_v2.json", str(REAL / V2)])
def test_unregistered_prompt_files_are_refused_before_prepare_docker_or_the_key(root, spy, bad, capsys):
    rc, built, _ = drive(root, argv_b(root, "--prompts-file", bad))
    assert rc == 2 and built == [] and spy == {} and "prompts-file" in capsys.readouterr().err


def test_prepare_itself_refuses_an_unregistered_prompt_file(root):
    with pytest.raises(m2_run.PrepareError, match="registered"):
        prep(root, prompts_file="config/experiment.json")
    with pytest.raises(m2_run.PrepareError, match="registered"):
        prep(root, prompts_file="../outside.json")
    assert prep(root, prompts_file=V2).prompts_file == V2 and prep(root).prompts_file == V1


def run_script(root, monkeypatch, argv):
    holder = {}
    real_prepare = m2_run.prepare
    monkeypatch.setattr(cli.m2_run, "prepare", lambda r, **kw: holder.setdefault("p", real_prepare(r, **kw)))
    rc, built, client = drive(root, argv, run_fn=lambda c, g, a: lifecycle(holder["p"]))
    return rc, holder["p"], json.loads((root / "runs" / "calibration" / holder["p"].run_id / "run.json").read_text()) if (root / "runs" / "calibration").exists() else None


def test_config_b_with_v2_contains_the_five_cycle_instruction_and_the_run_records_its_hashes(root, monkeypatch):
    rc, p, run = run_script(root, monkeypatch, argv_b(root, "--prompts-file", V2))
    assert rc == 0 and FIVE in p.task_text
    assert FIVE in (root / "runs" / "calibration" / p.run_id / "prompt.txt").read_text()
    assert run["prompt_set_file"] == V2 and run["prompt_set_file_sha256"] == hashlib.sha256((root / V2).read_bytes()).hexdigest()
    assert run["prompt"]["workflow_sha256"] == HASH_B_V2 and run["prompt"]["label"] == "B" and run["calibration"]["id"] == "m3-calibration-2"
    assert run["config"] == "B" and run["experiment"] is None and run["limits"]["gateway_max_concurrent"] == 1


def test_config_b_with_the_default_v1_has_no_cycle_cap_which_is_exactly_why_the_option_is_needed(root, monkeypatch):
    rc, p, run = run_script(root, monkeypatch, argv_b(root))
    assert rc == 0 and FIVE not in p.task_text and run["prompt_set_file"] == V1 and run["prompt"]["workflow_sha256"] == HASH_B_V1
    assert run["prompt_set_file_sha256"] == hashlib.sha256((root / V1).read_bytes()).hexdigest()


def test_config_a_and_the_v1_behaviour_are_unchanged(root, monkeypatch):
    # Config A text is byte-identical in v1 and v2: the prompt differs only for B (compared BEFORE run_script patches m2_run.prepare)
    assert prep(root, "A", prompts_file=V1).task_text == prep(root, "A", prompts_file=V2).task_text
    assert prep(root, "B", prompts_file=V1).task_text != prep(root, "B", prompts_file=V2).task_text
    a_args = ["--config", "A", "--budget-usd", "2.00", "--claude-binary", str(root / "claude"), "--execute", "--approve-spend", "2.00", "--calibration", "m3-calibration-1"]
    rc1, p1, run1 = run_script(root, monkeypatch, a_args)
    assert rc1 == 0 and run1["prompt_set_file"] == V1 and run1["prompt"]["workflow_sha256"] == HASH_A and run1["limits"]["gateway_max_concurrent"] == 1
    assert run1["prompt_set_file_sha256"] == hashlib.sha256((root / V1).read_bytes()).hexdigest()


def test_the_plan_uses_the_same_selection_as_execution(root, capsys):
    drive(root, ["--config", "B", "--budget-usd", "0.75", "--prompts-file", V2, "--calibration", "m3-calibration-2"], env={})
    out = capsys.readouterr().out
    assert HASH_B_V2 in out and HASH_B_V1 not in out and V2 in out
    drive(root, ["--config", "B", "--budget-usd", "0.75"], env={})
    out = capsys.readouterr().out
    assert HASH_B_V1 in out and HASH_B_V2 not in out and V1 in out


def test_the_blockers_check_the_selected_file_not_a_hardcoded_one(root, capsys):
    (root / V2).write_text((root / V2).read_text().replace("at most 5 test-fix cycles", "at most 7 test-fix cycles"))  # tampered: its recorded hashes no longer match
    rc, built, _ = drive(root, argv_b(root, "--prompts-file", V2))
    err = capsys.readouterr().err
    assert rc == 2 and built == [] and "prompts:" in err and "SHA-256" in err
    # the default v1 file is untouched and still passes the same check
    rc, built, _ = drive(root, argv_b(root))
    assert "prompts:" not in capsys.readouterr().err

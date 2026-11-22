"""Calibration preparation: explicit concurrency 1 and an explicit calibration identity that can never become a scored A/B observation.
Fakes only: no Docker, no network, no API."""
import json
from pathlib import Path

import pytest

from aiep import analysis, handoff, m2_run, rejection_policy as rp, results
from aiep.experiment import client_budget_usd
from aiep.handoff import validate_handoff
from aiep.results import ResultsError, ResultsWriter
from test_agent_runner import repo, seed  # noqa: F401 (fixtures)
from test_m2_run import GIB, KEY, STAMP, Client, cli, drive, fake_record, full, good_nets, lifecycle, prep, root  # noqa: F401 (fixtures)

REAL = Path(__file__).resolve().parents[1]
CAL = "m3-calibration-1"


class Stop(Exception):
    pass


@pytest.fixture
def spy(monkeypatch):
    """Replace m2_run.prepare inside the script with a recorder that stops the run before any Docker call."""
    seen = {}

    def fake_prepare(root, **kw):
        seen.update(kw)
        raise Stop

    monkeypatch.setattr(cli.m2_run, "prepare", fake_prepare)
    return seen


def go(root, extra, **kw):
    argv = [a for a in full(root)] + extra
    return drive(root, argv, **kw)


# ---------------------------------------------------------------- concurrency
def test_the_script_defaults_to_concurrency_1_never_2(root, spy):
    rc, built, _ = go(root, [])
    assert rc == 1 and spy["max_concurrent"] == 1 and type(spy["max_concurrent"]) is int


@pytest.mark.parametrize("value", ["1", "2", "3", "4"])
def test_an_explicit_value_reaches_prepare(root, spy, value):
    go(root, ["--max-concurrent", value])
    assert spy["max_concurrent"] == int(value)


@pytest.mark.parametrize("bad", ["0", "5", "-1", "1.5", "abc", "", " 1", "01", "١"])
def test_invalid_concurrency_is_refused_before_prepare_docker_or_the_key_is_used(root, spy, bad, capsys):
    rc, built, _ = go(root, ["--max-concurrent", bad])
    assert rc == 2 and built == [] and spy == {} and "max-concurrent" in capsys.readouterr().err


def test_invalid_concurrency_is_refused_even_in_plan_mode(root, capsys):
    rc, built, _ = drive(root, ["--config", "A", "--budget-usd", "2.00", "--max-concurrent", "9"], env={})
    assert rc == 2 and built == []


def test_prepare_keeps_its_own_default_of_2_for_other_callers_and_passes_an_explicit_value_to_the_gateway(root):
    assert prep(root).max_concurrent == 2
    for value in (1, 2, 3, 4):
        p = prep(root, max_concurrent=value)
        assert p.max_concurrent == value and p.gspec.max_concurrent == value


def test_the_effective_concurrency_is_recorded_in_run_json(root):
    p = prep(root, max_concurrent=1)
    ex = m2_run.execute(good_nets(), p, base=root / "runs" / "m2", root=root, run_fn=lambda c, g, a: lifecycle(p))
    lim = json.loads((ex.directory / "run.json").read_text())["limits"]
    assert lim["gateway_max_concurrent"] == 1 and lim["budget_is_soft"] is True


@pytest.mark.parametrize("bad", [0, 5, -1, True, 1.5, "1", None])
def test_prepare_itself_rejects_a_bad_concurrency(root, bad):
    with pytest.raises(m2_run.PrepareError, match="max_concurrent"):
        prep(root, max_concurrent=bad)


# ---------------------------------------------------------------- calibration identity
@pytest.mark.parametrize("bad", ["", "calibration", "m3-calibration-", "m3-calibration-0", "m3-calibration-01", "m3-pilot-v2", "M3-CALIBRATION-1", "m3-calibration-1/x",
                                 "m3-calibration-1 ", "m3-calibration-99999", "../m3-calibration-1"])
def test_a_bad_calibration_id_is_refused_before_prepare(root, spy, bad, capsys):
    rc, built, _ = go(root, ["--calibration", bad])
    assert rc == 2 and built == [] and spy == {} and "calibration" in capsys.readouterr().err


def test_the_calibration_id_reaches_prepare_and_ordinary_runs_pass_none(root, spy):
    go(root, ["--calibration", CAL])
    assert spy["calibration"] == CAL
    spy.clear()
    go(root, [])
    assert spy["calibration"] is None


def test_prepare_refuses_a_calibration_with_an_experiment_identity(root):
    ident = {"experiment_id": "m3-pilot-v2", "task_id": "pytest-dev__pytest-5227", "arm": "A", "repetition": 1, "pair_id": "p", "order_index": 1,
             "slot_id": "m3-pilot-v2/pytest-dev__pytest-5227/A/r1"}
    with pytest.raises(m2_run.PrepareError, match="no experiment identity"):
        prep(root, calibration=CAL, experiment=ident)
    with pytest.raises(m2_run.PrepareError, match="calibration"):
        prep(root, calibration="m3-pilot-v2")


def calibration_run(root, **kw):
    p = prep(root, calibration=CAL, budget_usd="2.00", **kw)
    ex = m2_run.execute(good_nets(), p, base=root / "runs" / "calibration", root=root, run_fn=lambda c, g, a: lifecycle(p))
    return p, ex, json.loads((ex.directory / "run.json").read_text())


def test_run_json_records_the_calibration_and_keeps_its_identity_separate_from_m3_v2(root):
    p, ex, run = calibration_run(root)
    assert run["calibration"]["id"] == CAL and run["calibration"]["purpose"] == "calibration" and run["calibration"]["scored"] is False
    assert run["calibration"]["not_an_ab_observation"] is True
    assert run["experiment"] is None  # no slot, no pair, no arm position in any experiment definition
    assert "m3-pilot" not in json.dumps(run)
    assert ex.directory.parent.name == "calibration" or ex.directory == root / "runs" / "calibration" / p.run_id


def test_ordinary_m2_runs_are_unchanged_and_have_no_calibration_key(root):
    p = prep(root)
    ex = m2_run.execute(good_nets(), p, base=root / "runs" / "m2", root=root, run_fn=lambda c, g, a: lifecycle(p))
    run = json.loads((ex.directory / "run.json").read_text())
    assert "calibration" not in run and run["experiment"] is None and run["limits"]["gateway_max_concurrent"] == 2


def test_the_script_writes_calibration_runs_under_runs_calibration_not_runs_m2(root, monkeypatch):
    holder = {}
    real_prepare = m2_run.prepare

    def capture(r, **kw):
        holder["p"] = real_prepare(r, **kw)
        return holder["p"]
    monkeypatch.setattr(cli.m2_run, "prepare", capture)
    rc, built, client = drive(root, full(root) + ["--calibration", CAL, "--max-concurrent", "1"], run_fn=lambda c, g, a: lifecycle(holder["p"]))
    run_dir = root / "runs" / "calibration" / holder["p"].run_id
    assert rc == 0 and (run_dir / "run.json").exists() and not (root / "runs" / "m2").exists()
    run = json.loads((run_dir / "run.json").read_text())
    assert run["calibration"]["id"] == CAL and run["limits"]["gateway_max_concurrent"] == 1


def test_an_ordinary_script_run_still_writes_under_runs_m2(root, monkeypatch):
    holder = {}
    real_prepare = m2_run.prepare
    monkeypatch.setattr(cli.m2_run, "prepare", lambda r, **kw: holder.setdefault("p", real_prepare(r, **kw)))
    rc, built, client = drive(root, full(root), run_fn=lambda c, g, a: lifecycle(holder["p"]))
    assert rc == 0 and (root / "runs" / "m2" / holder["p"].run_id / "run.json").exists() and not (root / "runs" / "calibration").exists()
    assert holder["p"].max_concurrent == 1  # the script's default is 1 for every run it launches


# ---------------------------------------------------------------- calibration can never become a scored observation
def test_build_row_refuses_a_calibration_run_even_with_a_full_scheduled_identity(root):
    p, ex, run = calibration_run(root)
    disguised = {"experiment_id": "m3-pilot-v2", "task_id": p.instance_id, "arm": "A", "repetition": 1, "pair_id": "m3-pilot-v2:x:r1", "order_index": 1,
                 "slot_id": f"m3-pilot-v2/{p.instance_id}/A/r1", "arm_position": 1}
    run["experiment"] = {k: disguised[k] for k in ("experiment_id", "task_id", "arm", "repetition", "pair_id", "order_index", "slot_id")}
    (ex.directory / "run.json").chmod(0o644)
    (ex.directory / "run.json").write_text(json.dumps(run))  # arm-like metadata injected by hand
    with pytest.raises(ResultsError, match="calibration"):
        results.build_row(ex.directory, disguised, {"status": "ready"}, None, None, definition_sha256="h" * 64, pricing=None, repo="r/r")


def valid_row():
    from test_m3_transcript_results_analysis import good_row  # a valid scored row
    return good_row()


def test_validate_row_and_the_writer_refuse_calibration_rows(tmp_path):
    row = valid_row()
    results.validate_row(row)
    for bad in (dict(row, calibration={"id": CAL}), dict(row, experiment_id=CAL, slot_id=f"{CAL}/{row['task_id']}/{row['arm']}/r{row['repetition']}")):
        with pytest.raises(ResultsError, match="calibration"):
            results.validate_row(bad)
        with pytest.raises(ResultsError, match="calibration"):
            ResultsWriter(tmp_path / "r.jsonl").append(bad)
    assert not (tmp_path / "r.jsonl").exists() or (tmp_path / "r.jsonl").read_text() == ""


def test_analysis_refuses_a_results_file_that_contains_a_calibration_row():
    from test_m3_transcript_results_analysis import DEF, row
    with pytest.raises(ValueError, match="calibration"):
        analysis.analyze([row("a", "A", True), dict(row("a", "B", True), calibration={"id": CAL})], DEF)
    with pytest.raises(ValueError, match="calibration"):
        analysis.analyze([dict(row("a", "A", True), experiment_id=CAL)], DEF)


def test_the_handoff_flags_a_calibration_run_for_evaluator_use_and_rejects_it_for_scheduled_slots(root):
    p, ex, run = calibration_run(root)
    rep = validate_handoff(ex.directory, root=root)
    assert any("calibration run" in f for f in rep["flags"])
    rep2 = validate_handoff(ex.directory, root=root, expected_slots={f"m3-pilot-v2/{p.instance_id}/A/r1": {"order_index": 1}})
    assert rep2["status"] == "rejected" and any("is not a calibration run" in r for r in rep2["rejections"])
    assert rp.classify_handoff(rep2).has(rp.INTEGRITY)


def test_no_batch_or_analysis_code_reads_the_calibration_directory():
    for rel in ("src/aiep/m3_batch.py", "src/aiep/m3_steps.py", "scripts/m3_analyze.py", "scripts/run_m3_pilot.py"):
        assert "runs/calibration" not in (REAL / rel).read_text() and '"calibration"' not in (REAL / rel).read_text(), rel


# ---------------------------------------------------------------- financial accounting is unchanged and honestly described
def test_the_calibration_configuration_values(root):
    assert client_budget_usd("2.00") == "2.2"
    p, ex, run = calibration_run(root, wall_clock_s=1200)
    lim = run["limits"]
    assert lim["budget_usd"] == "2.00" and lim["claude_client_budget_usd"] == "2.2" and lim["wall_clock_s"] == 1200 and lim["agent_max_output_tokens"] == 64000
    assert lim["budget_is_soft"] is True and lim["gateway_budget_usd"] == "2.00"
    assert "--max-budget-usd" in run["claude_args"] and run["claude_args"][run["claude_args"].index("--max-budget-usd") + 1] == "2.2"


def test_gateway_and_client_accounting_stay_separate_in_run_json(root):
    p, ex, run = calibration_run(root)
    assert "usage_measured" in run and "usage_cli_estimate" in run and "budget_attribution" in run and "accounting" in run
    assert "NOT measured" in run["usage_cli_estimate"]["source"] and run["usage_cli_estimate"]["total_cost_usd"] == 0.42


def test_the_plan_and_execute_output_state_that_the_limits_are_soft(root, capsys):
    drive(root, ["--config", "A", "--budget-usd", "2.00", "--calibration", CAL, "--max-concurrent", "1"], env={})
    out = capsys.readouterr().out
    assert "SOFT" in out and "not a hard" in out and "calibration" in out and CAL in out and "never a scored" in out.replace("\n", " ")


def test_the_gateway_budget_code_is_untouched():
    text = (REAL / "src" / "aiep" / "gateway.py").read_text()
    assert "if spent >= cfg.budget_usd:" in text and "usage.output_tokens = max(usage.output_tokens, max_tokens)" in text and "gw.stop(\"budget_exhausted\")" in text


# ---------------------------------------------------------------- a scheduled slot's identity round-trips through the REAL prepare into run.json
def test_a_scheduled_experiment_identity_is_recorded_in_run_json_not_overwritten_by_the_model_config(root):
    ident = {"experiment_id": "m3-pilot-v2", "task_id": "pytest-dev__pytest-5227", "arm": "A", "repetition": 1, "pair_id": "m3-pilot-v2:pytest-dev__pytest-5227:r1",
             "order_index": 1, "slot_id": "m3-pilot-v2/pytest-dev__pytest-5227/A/r1"}
    p = prep(root, experiment=ident)
    ex = m2_run.execute(good_nets(), p, base=root / "runs" / "m2", root=root, run_fn=lambda c, g, a: lifecycle(p))
    assert json.loads((ex.directory / "run.json").read_text())["experiment"] == ident

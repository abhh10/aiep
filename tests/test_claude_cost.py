"""The scored result row carries BOTH the gateway-measured cost and Claude's own reported cost, as separate fields that never overwrite or substitute for each other,
and the analysis reports them separately. No Docker, network or API."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from aiep import analysis, results
from aiep.pricing import load_pricing
from aiep.results import ResultsError, ResultsWriter

REAL = Path(__file__).resolve().parents[1]
PRICING = load_pricing(REAL / "config" / "model_pricing.json", json.loads((REAL / "config" / "experiment.json").read_text())["model"])
IDENT = {"experiment_id": "e", "task_id": "t", "arm": "A", "repetition": 1, "pair_id": "e:t:r1", "order_index": 1, "slot_id": "e/t/A/r1", "arm_position": 1}
GATEWAY_SRC = "gateway ledger (measured token usage, computed cost)"
CLIENT_SRC = "Claude Code client-side estimate (NOT measured)"


def run_json(tmp_path, *, gateway="0.0402250", estimate="default", **extra):
    um = {"source": GATEWAY_SRC, "tokens": {"input_tokens": 10, "output_tokens": 1131}, "exact": True, "requests": 5, "request_status": {"finished_requests": 5, "non_success": 0, "categories": {}}}
    if gateway is not None:
        um["computed_usd_upper_bound_basis"] = gateway
    run = {"run_id": "r", "model": "m", "experiment": IDENT, "usage_measured": um, "accounting": {"complete": True, "exact": True},
           "lifecycle": {"status": "completed", "agent": {"valid": True, "patch": {}}}}
    if estimate == "default":
        run["usage_cli_estimate"] = {"source": CLIENT_SRC, "subtype": "success", "total_cost_usd": 0.5}
    elif estimate != "absent":
        run["usage_cli_estimate"] = estimate
    run.update(extra)
    d = tmp_path / "run"
    d.mkdir(exist_ok=True)
    (d / "run.json").write_text(json.dumps(run))
    return d


def row_of(tmp_path, **kw):
    return results.build_row(run_json(tmp_path, **kw), IDENT, {"status": "ready"}, None, None, definition_sha256="h" * 64, pricing=PRICING, repo="r/r")


def test_both_costs_present_and_different_stay_separate(tmp_path):
    r = row_of(tmp_path)
    assert r["cost_usd_computed"] == "0.0402250" and r["claude_reported_cost_usd"] == "0.5"
    assert r["cost_usd_computed"] != r["claude_reported_cost_usd"]
    assert r["gateway_cost_source"] == GATEWAY_SRC and r["claude_reported_cost_source"] == CLIENT_SRC
    results.validate_row(r)


def test_the_two_figures_for_the_real_calibration_run_agree_and_keep_their_own_precision(tmp_path):
    r = row_of(tmp_path, gateway="0.0402250", estimate={"source": CLIENT_SRC, "total_cost_usd": 0.040225})
    assert r["cost_usd_computed"] == "0.0402250" and r["claude_reported_cost_usd"] == "0.040225"


@pytest.mark.parametrize("kind,estimate", [("absent", "absent"), ("null", None), ("empty", {}), ("no_cost", {"source": CLIENT_SRC, "subtype": "success"}),
                                           ("string", {"source": CLIENT_SRC, "total_cost_usd": "1.5"}), ("bool", {"source": CLIENT_SRC, "total_cost_usd": True}),
                                           ("negative", {"source": CLIENT_SRC, "total_cost_usd": -0.1}), ("nan", {"source": CLIENT_SRC, "total_cost_usd": float("nan")}),
                                           ("inf", {"source": CLIENT_SRC, "total_cost_usd": float("inf")}), ("list", {"source": CLIENT_SRC, "total_cost_usd": [1]}),
                                           ("not_a_dict", "0.5"), ("not_a_dict_list", [0.5])])
def test_a_missing_null_or_invalid_client_estimate_is_stored_as_null_never_the_gateway_cost(tmp_path, kind, estimate):
    r = row_of(tmp_path, estimate=estimate)
    assert r["claude_reported_cost_usd"] is None, kind
    assert r["cost_usd_computed"] == "0.0402250"  # untouched
    assert r["claude_reported_cost_usd"] != r["cost_usd_computed"]
    results.validate_row(r)


def test_the_source_is_recorded_only_when_the_estimate_supplies_one(tmp_path):
    assert row_of(tmp_path, estimate={"total_cost_usd": 0.1})["claude_reported_cost_source"] is None
    assert row_of(tmp_path, estimate="absent")["claude_reported_cost_source"] is None


def test_gateway_cost_missing_while_the_client_estimate_exists_does_not_copy_either_way(tmp_path):
    r = row_of(tmp_path, gateway=None)
    assert r["cost_usd_computed"] == "0"  # existing behaviour for an unreadable gateway cost, unchanged
    assert r["claude_reported_cost_usd"] == "0.5"  # the client figure is NOT used to fill the gateway field, and the gateway "0" does not touch it
    r2 = row_of(tmp_path, gateway="not a number")
    assert r2["cost_usd_computed"] == "0" and r2["claude_reported_cost_usd"] == "0.5"


def test_all_existing_fields_keep_their_values(tmp_path):
    r = row_of(tmp_path)
    for k in results.REQUIRED:
        assert k in r, k
    assert r["cost_exact"] is True and r["accounting_complete"] is True and r["requests"] == 5 and r["tokens"]["output_tokens"] == 1131
    assert r["uncached_equivalent_usd"] == results.uncached_equivalent_usd({"input_tokens": 10, "output_tokens": 1131}, PRICING)


# ---------------------------------------------------------------- schema compatibility
def good_row():
    from test_m3_transcript_results_analysis import good_row as g
    return g()


def test_rows_written_before_the_new_fields_still_validate_and_append(tmp_path):
    old = good_row()
    assert "claude_reported_cost_usd" not in old
    results.validate_row(old)
    ResultsWriter(tmp_path / "r.jsonl").append(old)
    assert ResultsWriter(tmp_path / "r.jsonl").rows() == [old]


@pytest.mark.parametrize("bad", ["abc", 1.5, True, "-1", "NaN", "Infinity", "1e999999", {}, []])
def test_a_malformed_claude_reported_cost_in_a_row_is_rejected(bad):
    with pytest.raises(ResultsError, match="claude_reported_cost_usd"):
        results.validate_row(dict(good_row(), claude_reported_cost_usd=bad))


@pytest.mark.parametrize("ok", [None, "0", "0.040225", "12.5"])
def test_valid_claude_reported_cost_values_are_accepted(ok):
    results.validate_row(dict(good_row(), claude_reported_cost_usd=ok))


def test_the_schema_version_and_required_fields_are_unchanged():
    assert results.SCHEMA == 1 and "claude_reported_cost_usd" not in results.REQUIRED  # optional: older rows stay valid


# ---------------------------------------------------------------- analysis exposes both without conflating them
def arm_row(task, arm, gateway, client, **kw):
    r = dict(good_row(), task_id=task, arm=arm, slot_id=f"e/{task}/{arm}/r1", pair_id=f"e:{task}:r1", cost_usd_computed=gateway, uncached_equivalent_usd=gateway,
             claude_reported_cost_usd=client)
    r.update(kw)
    return r


DEF = {"experiment_id": "e", "definition_sha256": "x", "preregistration": {"seed": "s", "tasks": [{"instance_id": t} for t in ("a", "b", "c")]}}


def test_analysis_reports_gateway_and_claude_costs_in_separate_fields():
    rows = [arm_row("a", "A", "0.10", "0.11"), arm_row("a", "B", "0.30", "0.33"), arm_row("b", "A", "0.20", None), arm_row("b", "B", "0.40", "0.44"), arm_row("c", "A", "1", "1.1"), arm_row("c", "B", "2", None)]
    rep = analysis.analyze(rows, DEF)
    a, b = rep["valid_runs"]["per_arm"]["A"], rep["valid_runs"]["per_arm"]["B"]
    assert a["cost_usd_total"] == "1.30" and b["cost_usd_total"] == "2.70"  # gateway figures: exactly as before
    assert a["claude_reported_cost_usd_total"] == "1.21" and b["claude_reported_cost_usd_total"] == "0.77"  # only rows that have one
    assert a["claude_reported_cost_runs"] == 2 and a["claude_reported_cost_missing_runs"] == 1 and b["claude_reported_cost_runs"] == 2
    assert rep["valid_runs"]["cost_ratio_B_over_A"] == "2.0769"  # gateway ratio unchanged
    paired = rep["valid_runs"]["paired_claude_reported_cost_difference_B_minus_A_usd"]
    assert paired["pairs_with_both"] == 1 and paired["mean"] == "0.22" and "NOT measured" in paired["note"]
    gap = rep["valid_runs"]["gateway_vs_claude_reported"]
    assert gap["rows_with_both"] == 4 and "never substituted" in gap["note"]


def test_analysis_with_no_claude_cost_at_all_reports_none_not_zero():
    rows = [arm_row("a", "A", "0.10", None), arm_row("a", "B", "0.30", None)]
    rep = analysis.analyze(rows, DEF)
    assert rep["valid_runs"]["per_arm"]["A"]["claude_reported_cost_usd_total"] is None and rep["valid_runs"]["per_arm"]["A"]["claude_reported_cost_runs"] == 0
    assert rep["valid_runs"]["paired_claude_reported_cost_difference_B_minus_A_usd"]["mean"] is None


def test_the_analysis_of_old_rows_without_the_field_is_unchanged_apart_from_the_new_keys():
    from test_m3_transcript_results_analysis import row
    rep = analysis.analyze([row("a", "A", True), row("a", "B", True)], dict(DEF))
    assert rep["as_scheduled"]["per_arm"]["A"]["cost_usd_total"] == "1.0" and rep["as_scheduled"]["per_arm"]["A"]["claude_reported_cost_runs"] == 0


def test_the_markdown_labels_the_two_costs_separately():
    rows = [arm_row("a", "A", "0.10", "0.11"), arm_row("a", "B", "0.30", "0.33")]
    md = analysis.render_markdown(analysis.analyze(rows, DEF))
    assert "gateway-measured cost total" in md and "Claude-reported cost total (client estimate, not measured)" in md

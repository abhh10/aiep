"""Defect 1 through the REAL batch: which handoff outcomes halt, which count toward the registered consecutive policy, which are just invalid runs."""
import json
from pathlib import Path

import pytest

from aiep import m3_batch as mb
from aiep.experiment_def import build_schedule, definition_hash
from aiep.results import ResultsWriter
ROOT = Path(__file__).resolve().parents[1]
from test_m3_integration import FULL, World, go, make_def as make_def_v1

SEED = "the pinned seed's HEAD equals the recorded seed head, and its parent is the dataset base_commit (x)"
BINARY = "the Claude binary recorded is the pinned one (sha256, size, version, platform)"
EXITED = "Claude Code exited on its own with status 0 (not killed, timed out, or container died)"
PATCH_APPLIES = "the exact model_patch applies cleanly (git apply --check) to a fresh copy of the pinned seed"
LIFECYCLE = "the lifecycle completed (gateway and proxy came up; not a startup failure)"
ATTACH = "the agent's post-start network attachment check passed"
LEDGER = "the gateway ledger is COMPLETE: every request it started forwarding has a finishing line"
EXACT = "the usage figures are EXACT (no request was cut off and charged at the max_tokens upper bound)"


def make_def(n_tasks=2):
    return make_def_v1(n_tasks)  # the current (v2) definition registers the new stop conditions


class Scripted(World):
    """A World whose handoff verdict per run (1-based call number) is scripted: a list of (rejections, flags) or None for a clean handoff."""

    def __init__(self, tmp_path, script):
        super().__init__(tmp_path)
        self.script, self.calls, self.seen_states = script, 0, []

    def handoff(self, run_dir, expected):
        self.calls += 1
        item = self.script[min(self.calls, len(self.script)) - 1]
        rej, flags = item if item else ([], [])
        st = self.statefile
        self.seen_states.append(json.loads(st.read_text()).get("consecutive_infra_invalid") if st and st.exists() else None)
        return {"status": "rejected" if rej else ("ready_flagged" if flags else "ready"), "rejections": rej, "flags": flags, "report_sha256": "h" * 64}

    statefile = None


def run(tmp_path, script, n_tasks=2, definition=None):
    w = Scripted(tmp_path, script)
    w.statefile = tmp_path / "state.json"
    d = definition or make_def(n_tasks)
    st, _ = go(tmp_path, w, d)
    return st, w, ResultsWriter(tmp_path / "results.jsonl").rows()


def test_setup_rejection_on_the_first_run_halts_immediately_and_pays_for_one_run(tmp_path):
    st, w, rows = run(tmp_path, [([SEED], [])])
    assert st["status"] == "halted" and st["halt"]["id"] == "handoff_integrity_failure"
    assert len(rows) == 1 and rows[0]["valid_measurement"] is False  # the run that revealed it is recorded, never graded
    assert sum(1 for x in w.log if x[0] == "run") == 1 and not [g for g in w.grade_calls if g[3] == "scored"]
    assert "pinned seed" in st["halt"]["detail"] or SEED[:20] in st["halt"]["detail"]


@pytest.mark.parametrize("reason", [BINARY, "a check nobody has classified"])
def test_other_integrity_and_unclassified_rejections_halt_immediately(tmp_path, reason):
    st, w, rows = run(tmp_path, [([reason], [])])
    assert st["halt"]["id"] == "handoff_integrity_failure" and len(rows) == 1


def test_integrity_mixed_with_agent_rejection_still_halts(tmp_path):
    st, _, rows = run(tmp_path, [([EXITED, SEED], [])])
    assert st["halt"]["id"] == "handoff_integrity_failure"


def test_agent_behaviour_rejections_do_not_halt_the_batch(tmp_path):
    st, w, rows = run(tmp_path, [([EXITED], []), ([PATCH_APPLIES], []), ([EXITED], []), ([EXITED, PATCH_APPLIES], [])])
    assert st["status"] == "complete" and len(rows) == 4 and all(r["valid_measurement"] is False for r in rows)
    assert st["consecutive_infra_invalid"] == 0


def test_two_consecutive_transient_failures_halt_under_the_registered_policy(tmp_path):
    st, w, rows = run(tmp_path, [([LIFECYCLE], []), ([ATTACH], [])])
    assert st["halt"]["id"] == "consecutive_invalid_runs" and len(rows) == 2


def test_one_transient_failure_then_a_good_run_resets_the_counter(tmp_path):
    st, w, rows = run(tmp_path, [([ATTACH], []), None, ([ATTACH], []), None])
    assert st["status"] == "complete" and len(rows) == 4 and st["consecutive_infra_invalid"] == 0


def test_agent_failure_between_two_transients_resets_the_counter(tmp_path):
    st, w, rows = run(tmp_path, [([ATTACH], []), ([EXITED], []), ([ATTACH], []), None])
    assert st["status"] == "complete"


def test_counter_is_durable_between_runs(tmp_path):
    st, w, rows = run(tmp_path, [([ATTACH], []), None, None, None])
    assert w.seen_states[:2] == [0, 1]  # the state file already said 1 when the second run's handoff was evaluated


def test_incomplete_accounting_rejection_halts_immediately(tmp_path):
    st, _, rows = run(tmp_path, [([LEDGER], [])])
    assert st["halt"]["id"] == "accounting_incomplete" and len(rows) == 1


def test_uncertain_financial_exposure_halts_immediately_even_for_an_accepted_run(tmp_path):
    st, _, rows = run(tmp_path, [([], [EXACT])])
    assert st["halt"]["id"] == "financial_exposure_uncertain" and len(rows) == 1
    assert "upper bound" in st["halt"]["detail"] and "soft" in st["halt"]["detail"]


def test_an_unrelated_flag_does_not_halt(tmp_path):
    st, _, rows = run(tmp_path, [([], ["the run was not stopped by the gateway's budget"])] * 4)
    assert st["status"] == "complete" and len(rows) == 4


def test_definition_without_the_new_stop_conditions_is_refused_before_anything_runs(tmp_path):
    d = json.loads((ROOT / "config/experiments/m3-pilot-v1.json").read_text())  # the superseded v1: its stop conditions do not register the new halts
    d["status"], d["limits"]["budget_usd_per_run"], d["limits"]["max_total_computed_usd"] = "locked", "1.0", "10"
    d["definition_sha256"] = definition_hash(d)
    with pytest.raises(mb.BatchHalt, match="definition_invalid"):
        mb.run_pilot(d, object(), state_path=tmp_path / "s.json", results_path=tmp_path / "r.jsonl", approvals=FULL(d["limits"]["max_total_computed_usd"]), execute=True)
    assert not (tmp_path / "s.json").exists()

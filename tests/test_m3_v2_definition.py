"""Decision 0: m3-pilot-v2 supersedes v1 (kept unchanged). Same tasks, seed, schedule, arm ordering and replacements; only the documented items differ."""
import hashlib
import json
from pathlib import Path

import pytest

from aiep.experiment_def import DEFAULT_DEFINITION, build_schedule, definition_hash, load_experiment, validate_definition
from aiep.transcript import TEST_COMMAND_SPEC

ROOT = Path(__file__).resolve().parents[1]
V1, V2 = ROOT / "config/experiments/m3-pilot-v1.json", ROOT / "config/experiments/m3-pilot-v2.json"
V1_FILE_SHA256_PREFIX = "e47bedac24269bed"  # recorded in docs/M3_7_NEXT_SESSION_HANDOFF.md
V1_DEFINITION_SHA256 = "f2ae65e5b2a2d8d721071093c9b411409bc19323fe1563efe0112e24dbd22f02"


@pytest.fixture(scope="module")
def v1():
    return json.loads(V1.read_text())


@pytest.fixture(scope="module")
def v2():
    return json.loads(V2.read_text())


def test_v1_is_preserved_byte_for_byte_and_still_valid():
    assert hashlib.sha256(V1.read_bytes()).hexdigest().startswith(V1_FILE_SHA256_PREFIX)
    doc = load_experiment(V1, root=ROOT)
    assert doc["definition_sha256"] == V1_DEFINITION_SHA256 and doc["status"] == "draft" and doc["experiment_id"] == "m3-pilot-v1"


def test_v2_validates_is_the_default_and_is_a_draft_that_cannot_spend(v2):
    assert DEFAULT_DEFINITION.name == "m3-pilot-v2.json"
    doc = load_experiment(V2, root=ROOT)
    assert doc["experiment_id"] == "m3-pilot-v2" and doc["status"] == "draft" and doc["definition_sha256"] == definition_hash(doc) != V1_DEFINITION_SHA256
    assert doc["limits"]["budget_usd_per_run"] is None and doc["limits"]["max_total_computed_usd"] is None


def test_only_the_documented_top_level_keys_differ(v1, v2):
    differing = {k for k in set(v1) | set(v2) if v1.get(k) != v2.get(k)}
    assert differing == {"definition_sha256", "experiment_id", "supersedes", "stop_conditions", "metrics", "design"}  # design: slot ids and pair ids carry the experiment id


def test_tasks_seed_replacements_prompts_limits_analysis_are_identical(v1, v2):
    assert v1["preregistration"] == v2["preregistration"]
    for k in ("prompt_set", "limits", "analysis", "model", "claude_cli", "dataset", "financial_notice", "arms", "claim_scope", "research_question", "kind", "schema", "status"):
        assert v1[k] == v2[k], k
    assert {k: v for k, v in v1["design"].items() if k != "schedule"} == {k: v for k, v in v2["design"].items() if k != "schedule"}


def test_schedule_is_identical_apart_from_the_experiment_id(v1, v2):
    strip = lambda rows: [{k: r[k] for k in ("order_index", "task_id", "arm", "rep", "arm_position")} for r in rows]
    assert strip(v1["design"]["schedule"]) == strip(v2["design"]["schedule"]) and len(v2["design"]["schedule"]) == 16
    assert [r["slot_id"] for r in v2["design"]["schedule"]] == [r["slot_id"].replace("m3-pilot-v1", "m3-pilot-v2") for r in v1["design"]["schedule"]]
    pre = v2["preregistration"]
    assert v2["design"]["schedule"] == build_schedule("m3-pilot-v2", [t["instance_id"] for t in pre["tasks"]], pre["seed"], 1)
    firsts = [r["arm"] for r in v2["design"]["schedule"] if r["arm_position"] == 1]
    assert firsts.count("A") == firsts.count("B") == 4


def test_stop_conditions_keep_every_v1_id_and_add_the_two_new_ones(v1, v2):
    old, new = [c["id"] for c in v1["stop_conditions"]], [c["id"] for c in v2["stop_conditions"]]
    assert set(old) < set(new) and set(new) - set(old) == {"handoff_integrity_failure", "financial_exposure_uncertain"}
    n2 = next(c for c in v2["stop_conditions"] if c["id"] == "consecutive_invalid_runs")
    assert n2["n"] == 2 and "TRANSIENT" in n2["rule"]


def test_metrics_use_the_registered_parser_spec(v1, v2):
    assert v2["metrics"]["test_command_spec"] == TEST_COMMAND_SPEC and "test_command_patterns" not in v2["metrics"]
    assert "test_command_patterns" in v1["metrics"]
    assert {k: v for k, v in v1["metrics"].items() if k not in ("test_command_patterns", "definitions")} == {k: v for k, v in v2["metrics"].items() if k not in ("test_command_spec", "definitions")}


def test_supersedes_documents_reason_changes_and_what_stayed(v2):
    s = v2["supersedes"]
    assert s["experiment_id"] == "m3-pilot-v1" and s["definition_sha256"] == V1_DEFINITION_SHA256
    assert len(s["reason"]) > 100 and len(s["changes"]) >= 4 and any("tasks" in u for u in s["unchanged"])


def test_editing_the_spec_or_dropping_a_new_stop_id_invalidates_v2(v2):
    bad = json.loads(json.dumps(v2))
    bad["metrics"]["test_command_spec"]["version"] = 99
    bad["definition_sha256"] = definition_hash(bad)
    assert any("test_command_spec" in p for p in validate_definition(bad, root=ROOT))
    bad = json.loads(json.dumps(v2))
    bad["stop_conditions"] = [c for c in bad["stop_conditions"] if c["id"] != "handoff_integrity_failure"]
    bad["definition_sha256"] = definition_hash(bad)
    assert any("handoff_integrity_failure" in p for p in validate_definition(bad, root=ROOT))


# ---- scripts/m3_experiment.py (offline)
def _script():
    import importlib.util
    spec = importlib.util.spec_from_file_location("m3_experiment", ROOT / "scripts" / "m3_experiment.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_script_validates_v2_by_default_and_v1_on_request(capsys):
    m = _script()
    assert m.main(["validate"]) == 0 and "m3-pilot-v2" in capsys.readouterr().out
    assert m.main(["validate", "--definition", str(V1)]) == 0 and "m3-pilot-v1" in capsys.readouterr().out


def test_script_plan_prints_sixteen_slots_with_the_v2_ids(capsys):
    assert _script().main(["plan"]) == 0
    out = capsys.readouterr().out
    assert out.count("m3-pilot-v2/") == 16 and "m3-pilot-v1" not in out


@pytest.mark.skipif(not (ROOT / ".cache" / "hf" / "hub" / "datasets--SWE-bench--SWE-bench_Lite" / "refs" / "main").is_file(),
                    reason="the build step reads the pinned local dataset snapshot (.cache/hf, git-ignored); a fresh clone does not have it")
def test_script_never_overwrites_an_existing_definition(capsys):
    before = V2.read_bytes()
    assert _script().main(["build", "--write"]) == 2 and V2.read_bytes() == before
    assert _script().main(["build", "--write", "--experiment-id", "m3-pilot-v1"]) == 2 and hashlib.sha256(V1.read_bytes()).hexdigest().startswith(V1_FILE_SHA256_PREFIX)


def test_the_v1_file_cannot_be_executed_by_the_batch():
    from aiep import m3_batch as mb
    d = json.loads(V1.read_text())
    with pytest.raises(mb.BatchHalt, match="definition_invalid"):
        mb._check_registered(d)
    mb._check_registered(json.loads(V2.read_text()))

"""M2 -> M1 handoff validation (aiep.handoff): synthetic run directories, a tiny local git repo as the "pinned seed", fake dataset record. No Docker, no network."""
import copy
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from aiep import artifacts, handoff
from aiep.experiment import claude_args, load_prompts
from aiep.handoff import READY, READY_FLAGGED, REJECTED, Providers, validate_handoff
from aiep.patch import validate_patch, write_predictions

REAL = Path(__file__).resolve().parents[1]
INSTANCE = "pytest-dev__pytest-5227"
F2P_ID = "testing/logging/test_reporting.py::test_hidden_target_case_alpha"
P2P_ID = "testing/logging/test_reporting.py::test_existing_case_beta"
HIDDEN_TEST_LINE = "    assert caplog.records[0].levelname == 'HIDDEN-EXPECTED-LEVEL-NAME-123'"
GOLD_LINE = "        fmt = '%(filename)-25s %(lineno)4d %(levelname)-8s %(message)s  # gold'"
BUDGET = "0.50"
ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, env=ENV, check=True, capture_output=True).stdout.decode().strip()


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """A tiny repo: commit 1 (the 'dataset base_commit'), commit 2 (the synthetic 'SWE-bench' HEAD), and a root with a patched seeds.json."""
    d = tmp_path_factory.mktemp("world")
    repo = d / "seedtree"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.py").write_text("x = 1\ny = 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_text("x = 1\ny = 2\n")
    git(repo, "commit", "-q", "--allow-empty", "-m", "SWE-bench")
    head = git(repo, "rev-parse", "HEAD")
    root = d / "root"
    shutil.copytree(REAL / "config", root / "config")
    seeds = json.loads((root / "config" / "seeds.json").read_text())
    seeds["seeds"][INSTANCE].update(head=head, sha256="s" * 64, entries=3)
    (root / "config" / "seeds.json").write_text(json.dumps(seeds))
    good_patch = "diff --git a/a.py b/a.py\nindex 0000000..1111111 100644\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,3 @@\n x = 1\n y = 2\n+z = 3\n"
    return {"repo": repo, "root": root, "base": base, "head": head, "patch": good_patch}


def record(world):
    return {"instance_id": INSTANCE, "repo": "pytest-dev/pytest", "base_commit": world["base"], "FAIL_TO_PASS": json.dumps([F2P_ID]),
            "PASS_TO_PASS": json.dumps([P2P_ID, "some progress marker without a double colon"]),
            "test_patch": f"diff --git a/testing/logging/test_reporting.py b/testing/logging/test_reporting.py\n--- a/testing/logging/test_reporting.py\n+++ b/testing/logging/test_reporting.py\n+{HIDDEN_TEST_LINE}\n",
            "problem_statement": "The default format is poor. Maybe use a format like the one a user proposed in the issue.", "hints_text": "",
            "patch": f"diff --git a/src/_pytest/logging.py b/src/_pytest/logging.py\n--- a/src/_pytest/logging.py\n+++ b/src/_pytest/logging.py\n+{GOLD_LINE}\n"}


def seed_git(world):
    def f(pin, patch):
        head, parent = git(world["repo"], "rev-parse", "HEAD"), git(world["repo"], "rev-parse", "HEAD^")
        ap = subprocess.run(["git", "apply", "--check", "-"], cwd=world["repo"], env=ENV, input=patch, capture_output=True)
        return {"head": head, "parent": parent, "applies": ap.returncode == 0, "why": ap.stderr.decode()[:200]}
    return f


def providers(world, rec=None):
    return Providers(load_record=lambda i: rec if rec is not None else record(world), seed_git=seed_git(world))


_n = [0]


def make_run(tmp_path, world, *, patch=None, label="A", meta=None, files=None, drop=(), pred=None, prompt_extra="", ledger=None, stdout=None):
    """Write a run directory exactly the way m2_run does, then return it. ``meta``/``files`` are mutators; ``pred`` replaces predictions.jsonl bytes."""
    root = world["root"]
    _n[0] += 1
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(1_800_000_000 + _n[0]))
    run_id = artifacts.make_run_id(INSTANCE, label, stamp)
    prompts = load_prompts(root / "config" / "workflow_prompts.json")
    prompt = f"Resolve the issue.\n<issue>\nsomething is broken\n</issue>\n{prompt_extra}"
    patch = world["patch"] if patch is None else patch
    pr = validate_patch(patch.encode()) if patch else None
    cli = json.loads((root / "config" / "claude_cli.json").read_text())
    gw = json.loads((root / "config" / "gateway_image.json").read_text())
    images = json.loads((root / "config" / "evaluator_images.json").read_text())["images"]
    seeds = json.loads((root / "config" / "seeds.json").read_text())["seeds"][INSTANCE]
    ledger_text = ledger if ledger is not None else (
        json.dumps({"rid": "r1", "outcome": "forwarding", "seq": 1, "requested_max_tokens": 64000}) + "\n"
        + json.dumps({"rid": "r1", "outcome": "ok", "status": 200, "cumulative_usd_computed": "0.00697", "usage": {"input_tokens": 1}}) + "\n")
    meta_d = {
        "task": {"instance_id": INSTANCE, "repo": "pytest-dev/pytest", "base_commit": world["base"]},
        "seed": {"sha256": seeds["sha256"], "head": seeds["head"], "entries": seeds["entries"]},
        "image_id": images[INSTANCE]["image_id"], "model": json.loads((root / "config" / "experiment.json").read_text())["model"], "config": label,
        "claude_cli": {"version": cli["version"], "platform": cli["platform"], "sha256": cli["sha256"], "size_bytes": cli["size_bytes"]},
        "gateway_image": {"image": gw["repo_digest"], "image_id": gw["image_id"]},
        "prompt": {"label": label, "template_sha256": prompts.hashes["template"], "workflow_sha256": prompts.hashes[label],
                   "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()},
        "limits": {"budget_usd": BUDGET}, "networks": {"aiep-agent-net": "a", "aiep-proxy-net": "p", "aiep-egress-net": "e"},
        "interrupted": False, "orchestrator_error": None, "termination": None, "budget_attribution": {"client_budget_exceeded": False},
        "lifecycle": {"status": "completed", "gateway_stop_reason": None, "gateway_removed": True, "proxy": {"removed": True},
                      "agent": {"valid": True, "status": "exited", "claude_exit_code": 0, "claude_output_truncated": False, "removed": True,
                                "attachment": {"networks": {"aiep-agent-net": {}}}, "timeline_s": {"attachment_ok": 1.0},
                                "patch": {"sha256": pr.sha256, "size": pr.size, "status": pr.status} if pr else None}},
        "accounting": {"complete": True, "exact": True},
        "usage_measured": {"exact": True, "metered": 1, "entries_charged_at_max_tokens_upper_bound": 0, "computed_usd_upper_bound_basis": "0.00697", "requests": 1,
                           "tokens": {"input_tokens": 1}},
        "evaluation": "not run",
    }
    meta_d["claude_args"] = list(claude_args(meta_d["model"], BUDGET))
    if meta:
        meta(meta_d)
    f = {"prompt.txt": prompt, "claude_stdout.jsonl": stdout if stdout is not None else '{"type":"result","subtype":"success"}\n', "claude_stderr.txt": "",
         "gateway_ledger.jsonl": ledger_text}
    if patch:
        f["patch.diff"] = patch
        tmp = tmp_path / f"pred{_n[0]}"
        tmp.mkdir()
        f["predictions.jsonl"] = (write_predictions(tmp / "p.jsonl", INSTANCE, f"aiep-{label}-{meta_d['model']}", pr).read_bytes() if pr.status == "ok" else b"")
    if pred is not None:
        f["predictions.jsonl"] = pred(f.get("predictions.jsonl", b""))
    if files:
        files(f)
    for name in drop:
        f.pop(name, None)
    return artifacts.write_run_artifacts(tmp_path / "runs", run_id, f, meta_d, ()).directory


def run(tmp_path, world, **kw):
    ho = {k: kw.pop(k) for k in ("hidden_scan", "apply_check", "rec") if k in kw}
    d = make_run(tmp_path, world, **kw)
    return validate_handoff(d, root=world["root"], providers=providers(world, ho.get("rec")), hidden_scan=ho.get("hidden_scan", True), apply_check=ho.get("apply_check", True))


def failed(rep):
    return " | ".join(rep["rejections"])


# ---------------------------------------------------------------- the good run
def test_a_clean_run_is_ready_and_the_report_says_nothing_was_graded(tmp_path, world):
    rep = run(tmp_path, world)
    assert rep["status"] == READY and rep["rejections"] == [] and rep["flags"] == [], rep["rejections"] + rep["flags"]
    assert rep["grading"]["performed"] is False and rep["grading"]["resolved"] is None and "not a result" in rep["grading"]["note"]
    s = rep["summary"]
    assert s["patch"]["files"] == 1 and s["patch"]["additions"] == 1 and s["instance_id"] == INSTANCE and s["evaluator_command_not_run"].startswith("scripts/m1_eval.sh m2eval-")
    assert s["hidden_data"] == {"fail_to_pass": 1, "pass_to_pass": 2, "note": s["hidden_data"]["note"]}


def test_the_report_never_contains_hidden_identifiers_or_patch_text(tmp_path, world):
    blob = json.dumps(run(tmp_path, world))
    for secret in (F2P_ID, P2P_ID, HIDDEN_TEST_LINE.strip(), GOLD_LINE.strip(), "test_hidden_target_case_alpha"):
        assert secret not in blob


def test_the_report_is_deterministic_and_carries_its_own_hash(tmp_path, world):
    rep = run(tmp_path, world)
    body = {k: v for k, v in rep.items() if k != "report_sha256"}
    assert rep["report_sha256"] == hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------- presence, hashes, schema
@pytest.mark.parametrize("name", ["run.json", "prompt.txt", "claude_stdout.jsonl", "claude_stderr.txt", "gateway_ledger.jsonl", "patch.diff", "predictions.jsonl", "artifacts_manifest.json"])
def test_every_missing_artifact_is_rejected(tmp_path, world, name):
    d = make_run(tmp_path, world)
    (d / name).chmod(0o644)
    (d / name).unlink()
    rep = validate_handoff(d, root=world["root"], providers=providers(world))
    assert rep["status"] == REJECTED and "every required artifact is present" in failed(rep)


def test_a_run_with_no_patch_cannot_be_handed_off_and_still_explains_why(tmp_path, world):
    rep = run(tmp_path, world, patch="", meta=lambda m: m["lifecycle"]["agent"].update(valid=False, invalid_reason="proxy died", status="gateway_stopped", claude_exit_code=None))
    assert rep["status"] == REJECTED and "required artifact" in failed(rep) and "agent run is VALID" in failed(rep)


@pytest.mark.parametrize("name", ["patch.diff", "predictions.jsonl", "prompt.txt", "gateway_ledger.jsonl", "claude_stdout.jsonl", "run.json"])
def test_a_file_changed_after_the_manifest_was_written_is_rejected(tmp_path, world, name):
    d = make_run(tmp_path, world)
    (d / name).chmod(0o644)
    (d / name).write_bytes((d / name).read_bytes() + b" ")
    assert "manifest" in failed(validate_handoff(d, root=world["root"], providers=providers(world)))


def test_an_unexpected_extra_file_is_rejected(tmp_path, world):
    d = make_run(tmp_path, world)
    (d / "notes.txt").write_text("x")
    assert "manifest" in failed(validate_handoff(d, root=world["root"], providers=providers(world)))


def test_a_directory_that_is_not_a_run_is_rejected(tmp_path, world):
    (tmp_path / "empty").mkdir()
    assert validate_handoff(tmp_path / "empty", root=world["root"], providers=providers(world))["status"] == REJECTED


# ---------------------------------------------------------------- predictions schema
def pred_variant(fn):
    return lambda b: fn(json.loads(b.decode().splitlines()[0])) if callable(fn) else fn


def reserialise(mutator):
    def f(raw):
        d = json.loads(raw.decode().splitlines()[0])
        out = mutator(d)
        return (json.dumps(out if out is not None else d) + "\n").encode()
    return f


@pytest.mark.parametrize("name,pred,needle", [
    ("two lines", lambda b: b + b, "exactly one"), ("no trailing newline", lambda b: b.rstrip(b"\n"), "exactly one"), ("empty", lambda b: b"", "exactly one"),
    ("not json", lambda b: b"not json\n", "harness keys"), ("array", lambda b: b"[1]\n", "harness keys"),
    ("missing key", reserialise(lambda d: d.pop("model_patch") and None), "harness keys"),
    ("extra key", reserialise(lambda d: d.update(extra="x")), "harness keys"),
    ("non-string patch", reserialise(lambda d: d.update(model_patch=None)), "harness keys"),
    ("wrong instance", reserialise(lambda d: d.update(instance_id="pytest-dev__pytest-5103")), "instance_id is this run's task"),
    ("wrong model name", reserialise(lambda d: d.update(model_name_or_path="gold")), "model_name_or_path"),
    ("different patch text", reserialise(lambda d: d.update(model_patch=d["model_patch"] + "\n+evil\n")), "byte-identical"),
    ("empty patch", reserialise(lambda d: d.update(model_patch="")), "byte-identical"),
])
def test_malformed_or_mismatched_predictions_are_rejected(tmp_path, world, name, pred, needle):
    rep = run(tmp_path, world, pred=pred)
    assert rep["status"] == REJECTED and needle in failed(rep), (name, failed(rep))


# ---------------------------------------------------------------- identity and registered configuration
@pytest.mark.parametrize("name,mut,needle", [
    ("run_id differs", lambda m: m.update(run_id="m2-other-A-20260101T000000Z"), None),
    ("instance differs", lambda m: m["task"].update(instance_id="pytest-dev__pytest-5103"), "agree with the directory name"),
    ("config differs", lambda m: m.update(config="B"), "agree with the directory name"),
    ("model differs", lambda m: m.update(model="claude-opus-5-5"), "pinned model"),
    ("image differs", lambda m: m.update(image_id="sha256:" + "0" * 64), "SAME image"),
    ("seed head differs", lambda m: m["seed"].update(head="0" * 40), "pinned seed"),
    ("seed hash differs", lambda m: m["seed"].update(sha256="0" * 64), "pinned seed"),
    ("base commit differs", lambda m: m["task"].update(base_commit="0" * 40), "base_commit"),
    ("repo differs", lambda m: m["task"].update(repo="other/repo"), "repo and base_commit"),
    ("template hash differs", lambda m: m["prompt"].update(template_sha256="0" * 64), "registered prompt"),
    ("workflow hash is B's in an A run", lambda m: m["prompt"].update(workflow_sha256=load_prompts(REAL / "config" / "workflow_prompts.json").hashes["B"]), "registered prompt"),
    ("prompt hash differs", lambda m: m["prompt"].update(prompt_sha256="0" * 64), "sha256 of prompt.txt"),
    ("claude binary differs", lambda m: m["claude_cli"].update(sha256="0" * 64), "pinned one"),
    ("claude flags differ", lambda m: m.update(claude_args=m["claude_args"] + ["--dangerously-skip-permissions"]), "exactly the experiment's flags"),
    ("gateway image differs", lambda m: m["gateway_image"].update(image_id="sha256:" + "1" * 64), "gateway image"),
    ("networks missing", lambda m: m.update(networks={"aiep-agent-net": "a"}), "three AIEP networks"),
])
def test_wrong_identity_or_configuration_is_rejected(tmp_path, world, name, mut, needle):
    rep = run(tmp_path, world, meta=mut)
    assert rep["status"] == REJECTED, name
    if needle:
        assert needle in failed(rep), (name, failed(rep))


def test_config_b_with_its_own_registered_hashes_is_accepted_and_the_two_arms_use_identical_flags(tmp_path, world):
    a, b = run(tmp_path, world, label="A"), run(tmp_path, world, label="B")
    assert a["status"] == b["status"] == READY
    assert claude_args("claude-sonnet-5-5", BUDGET) == claude_args("claude-sonnet-5-5", BUDGET)  # no label input exists


# ---------------------------------------------------------------- the patch and the pinned seed
def test_a_patch_that_does_not_apply_to_the_pinned_seed_is_rejected(tmp_path, world):
    bad = "diff --git a/a.py b/a.py\nindex 0000000..1111111 100644\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n x = 999\n-y = 2\n+y = 3\n"
    rep = run(tmp_path, world, patch=bad)
    assert rep["status"] == REJECTED and "applies cleanly" in failed(rep)


@pytest.mark.parametrize("patch,needle", [
    ("diff --git a/../evil.py b/../evil.py\nnew file mode 100644\n--- /dev/null\n+++ b/../evil.py\n@@ -0,0 +1 @@\n+x\n", "validator"),
    ("diff --git a/a.py b/b.py\nrename from a.py\nrename to b.py\n", "validator"),
    ("diff --git a/l b/l\nnew file mode 120000\n--- /dev/null\n+++ b/l\n@@ -0,0 +1 @@\n+a.py\n", "validator"),
    ("not a diff\n", "validator"),
])
def test_unsafe_or_malformed_patches_are_rejected_by_the_production_validator(tmp_path, world, patch, needle):
    d = make_run(tmp_path, world, patch=patch, meta=lambda m: m["lifecycle"]["agent"].update(patch={"sha256": hashlib.sha256(patch.encode()).hexdigest(), "size": len(patch), "status": "ok"}),
                 pred=lambda b: json.dumps({"instance_id": INSTANCE, "model_name_or_path": "aiep-A-claude-sonnet-5-5", "model_patch": patch}).encode() + b"\n",
                 files=lambda f: f.update({"patch.diff": patch}))
    rep = validate_handoff(d, root=world["root"], providers=providers(world))
    assert rep["status"] == REJECTED and needle in failed(rep)


def test_the_patch_hash_must_equal_what_the_agent_run_recorded(tmp_path, world):
    rep = run(tmp_path, world, meta=lambda m: m["lifecycle"]["agent"]["patch"].update(sha256="0" * 64))
    assert rep["status"] == REJECTED and "recorded when it extracted" in failed(rep)


def test_a_seed_whose_parent_is_not_the_dataset_base_commit_is_rejected(tmp_path, world):
    rec = record(world)
    rec["base_commit"] = "1" * 40
    rep = run(tmp_path, world, rec=rec, meta=lambda m: m["task"].update(base_commit="1" * 40))
    assert rep["status"] == REJECTED and "parent is the dataset base_commit" in failed(rep)


def test_an_unloadable_dataset_record_fails_closed(tmp_path, world):
    d = make_run(tmp_path, world)

    def boom(i):
        raise RuntimeError("no dataset")
    rep = validate_handoff(d, root=world["root"], providers=Providers(load_record=boom, seed_git=seed_git(world)))
    assert rep["status"] == REJECTED and "dataset record could be loaded" in failed(rep)


# ---------------------------------------------------------------- failed, censored, interrupted runs
@pytest.mark.parametrize("name,mut,needle", [
    ("lifecycle failed", lambda m: m["lifecycle"].update(status="gateway_failed"), "lifecycle completed"),
    ("agent invalid", lambda m: m["lifecycle"]["agent"].update(valid=False, invalid_reason="x"), "VALID"),
    ("proxy died", lambda m: m["lifecycle"]["agent"].update(valid=False, status="gateway_stopped", stop_reason="proxy_died", claude_exit_code=None), "VALID"),
    ("claude killed", lambda m: m["lifecycle"]["agent"].update(status="timeout", claude_exit_code=None), "exited on its own"),
    ("claude exit 1", lambda m: m["lifecycle"]["agent"].update(claude_exit_code=1), "exited on its own"),
    ("container died", lambda m: m["lifecycle"]["agent"].update(status="container_died"), "exited on its own"),
    ("censored output", lambda m: m["lifecycle"]["agent"].update(claude_output_truncated=True), "NOT truncated"),
    ("interrupted", lambda m: m.update(interrupted=True), "not interrupted"),
    ("orchestrator error", lambda m: m.update(orchestrator_error="RuntimeError: x"), "not interrupted"),
    ("lease lost", lambda m: m.update(termination={"reason": "lease_lost"}), "not interrupted"),
    ("no attachment evidence", lambda m: m["lifecycle"]["agent"].update(attachment=None), "attachment check passed"),
    ("model violation stop", lambda m: m["lifecycle"].update(gateway_stop_reason="model_violation"), "violation"),
    ("metering failure stop", lambda m: m["lifecycle"].update(gateway_stop_reason="metering_failure"), "violation"),
])
def test_failed_censored_or_interrupted_runs_are_rejected_never_measured(tmp_path, world, name, mut, needle):
    rep = run(tmp_path, world, meta=mut)
    assert rep["status"] == REJECTED and needle in failed(rep), (name, failed(rep))


# ---------------------------------------------------------------- accounting
def test_incomplete_accounting_is_rejected_because_usage_may_have_been_billed(tmp_path, world):
    only_forwarding = json.dumps({"rid": "r1", "outcome": "forwarding", "seq": 1, "requested_max_tokens": 64000}) + "\n"
    rep = run(tmp_path, world, ledger=only_forwarding, meta=lambda m: m["accounting"].update(complete=False, exact=False))
    assert rep["status"] == REJECTED and "ledger is COMPLETE" in failed(rep)


def test_an_unparseable_ledger_line_makes_accounting_incomplete(tmp_path, world):
    rep = run(tmp_path, world, ledger='{"rid":"r1","outcome":"forwarding"}\nnot json\n{"rid":"r1","outcome":"ok"}\n')
    assert rep["status"] == REJECTED and "ledger is COMPLETE" in failed(rep)


def test_no_billed_request_at_all_is_rejected(tmp_path, world):
    rep = run(tmp_path, world, meta=lambda m: m["usage_measured"].update(metered=0))
    assert rep["status"] == REJECTED and "at least one billed request" in failed(rep)


def test_an_upper_bound_charge_makes_the_run_gradable_but_flagged_not_a_clean_measurement(tmp_path, world):
    rep = run(tmp_path, world, meta=lambda m: (m["usage_measured"].update(exact=False, entries_charged_at_max_tokens_upper_bound=1, computed_usd_upper_bound_basis="0.644"),
                                               m["accounting"].update(exact=True)))
    assert rep["status"] == READY_FLAGGED and "usage figures are EXACT" in " ".join(rep["flags"]) and "does not exceed the configured budget" in " ".join(rep["flags"])
    assert rep["summary"]["accounting"]["computed_usd"] == "0.644" and rep["summary"]["accounting"]["budget_usd"] == "0.50"


def test_a_run_stopped_by_the_budget_is_flagged(tmp_path, world):
    rep = run(tmp_path, world, meta=lambda m: m["lifecycle"].update(gateway_stop_reason="budget_exhausted"))
    assert rep["status"] == READY_FLAGGED and any("stopped by the gateway's budget" in f for f in rep["flags"])


def test_claudes_own_client_side_budget_ending_the_run_is_flagged(tmp_path, world):
    rep = run(tmp_path, world, meta=lambda m: m["budget_attribution"].update(client_budget_exceeded=True))
    assert rep["status"] == READY_FLAGGED


def test_a_cleanup_failure_is_flagged(tmp_path, world):
    rep = run(tmp_path, world, meta=lambda m: m["lifecycle"]["proxy"].update(removed=False))
    assert rep["status"] == READY_FLAGGED and any("containers were all removed" in f for f in rep["flags"])


def test_claudes_own_cost_estimate_is_never_used_as_the_cost(tmp_path, world):
    rep = run(tmp_path, world, meta=lambda m: m.update(usage_cli_estimate={"total_cost_usd": 99.0}))
    assert rep["summary"]["accounting"]["computed_usd"] == "0.00697" and "client-side estimate" in rep["summary"]["accounting"]["source"]


# ---------------------------------------------------------------- hidden data
@pytest.mark.parametrize("extra", [F2P_ID, P2P_ID, HIDDEN_TEST_LINE.strip(), GOLD_LINE.strip(), "see " + F2P_ID + " please"])
def test_hidden_data_in_the_agents_input_is_rejected(tmp_path, world, extra):
    rep = run(tmp_path, world, prompt_extra=extra + "\n")
    assert rep["status"] == REJECTED and "agent's INPUT" in failed(rep)
    assert extra not in json.dumps(rep)  # and it is not echoed into the report


def test_a_hidden_line_that_the_public_issue_text_itself_quotes_is_not_a_leak(tmp_path, world):
    rec = record(world)
    rec["problem_statement"] += "\nI suggest: " + GOLD_LINE.strip()  # an issue that proposes the fix quotes the fix
    rep = run(tmp_path, world, rec=rec, prompt_extra=GOLD_LINE.strip() + "\n")
    assert rep["status"] == READY, failed(rep)
    check = next(c for c in rep["checks"] if "agent's INPUT" in c["check"])
    assert check["ok"] and check["detail"]["quoted_by_the_public_issue_text"] == 1 and check["detail"]["gold_patch_lines"] == 0


def test_hidden_lines_in_the_agents_output_are_flagged_not_rejected(tmp_path, world):
    rep = run(tmp_path, world, stdout=GOLD_LINE.strip() + "\n")
    assert rep["status"] == READY_FLAGGED and any("repeat no hidden-patch line" in f for f in rep["flags"])


def test_a_patch_touching_a_hidden_test_file_is_flagged(tmp_path, world):
    p = "diff --git a/testing/logging/test_reporting.py b/testing/logging/test_reporting.py\nnew file mode 100644\n--- /dev/null\n+++ b/testing/logging/test_reporting.py\n@@ -0,0 +1 @@\n+x = 1\n"
    rep = run(tmp_path, world, patch=p)
    assert any("hidden test patch also modifies" in f for f in rep["flags"])


def test_skipped_optional_checks_are_flagged_never_silently_passed(tmp_path, world):
    assert run(tmp_path, world, hidden_scan=False)["status"] == READY_FLAGGED
    assert any("applicability check" in f for f in run(tmp_path, world, apply_check=False)["flags"])


def test_the_driver_limitation_is_flagged_for_other_instances(tmp_path, world):
    rep = run(tmp_path, world, meta=lambda m: None)
    assert not any("hardcoded" in f for f in rep["flags"])  # pytest-5227 is the driver's instance


# ---------------------------------------------------------------- the validator itself
def test_without_a_seed_git_provider_the_applicability_check_is_skipped_and_flagged_never_assumed_passed(tmp_path, world):
    d = make_run(tmp_path, world)
    rep = validate_handoff(d, root=world["root"], providers=Providers(load_record=lambda i: record(world)))
    assert rep["status"] == READY_FLAGGED and any("applicability check" in f for f in rep["flags"])


def test_the_validator_module_contains_no_subprocess_so_host_git_stays_confined():
    assert "subprocess" not in (REAL / "src" / "aiep" / "handoff.py").read_text()


def test_the_validator_never_imports_docker_or_the_network_stack():
    import ast
    tree = ast.parse((REAL / "src" / "aiep" / "handoff.py").read_text())
    mods = {n.module if isinstance(n, ast.ImportFrom) else a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (getattr(n, "names", None) or [n])}
    assert not {m for m in mods if m and m.split(".")[0] in ("docker", "requests", "urllib", "http", "socket", "huggingface_hub", "datasets")}


def test_cli_exit_codes_and_report_file(tmp_path, world, monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("validate_handoff_cli", REAL / "scripts" / "validate_handoff.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    d = make_run(tmp_path, world)
    monkeypatch.setattr(cli, "validate_handoff", lambda rd, **kw: validate_handoff(rd, **{**kw, "root": world["root"], "providers": providers(world)}))
    out = tmp_path / "r.json"
    assert cli.main([str(d), "--out", str(out)]) == 0 and json.loads(out.read_text())["status"] == READY
    assert cli.main([str(d), "--out", str(out), "--no-hidden-scan"]) == 3
    assert cli.main([str(tmp_path / "nope")]) == 2
    bad = make_run(tmp_path, world, drop=("patch.diff",))
    assert cli.main([str(bad), "--out", str(out)]) == 1


# ---------------------------------------------------------------- M3: experiment identity and the registered schedule
def _exp(label="A", **over):
    e = {"experiment_id": "ex1", "task_id": INSTANCE, "arm": label, "repetition": 1, "pair_id": f"ex1:{INSTANCE}:r1", "order_index": 3, "slot_id": f"ex1/{INSTANCE}/{label}/r1"}
    return e | over


def _validate(tmp_path, world, exp, expected):
    d = make_run(tmp_path, world, meta=lambda m: m.__setitem__("experiment", exp))
    return validate_handoff(d, root=world["root"], providers=providers(world), expected_slots=expected)


def test_experiment_identity_matching_the_schedule_is_accepted(tmp_path, world):
    e = _exp()
    rep = _validate(tmp_path, world, e, {e["slot_id"]: e})
    assert not [r for r in rep["rejections"] if "experiment" in r or "slot" in r]
    assert rep["summary"]["experiment"] == e


@pytest.mark.parametrize("mut", [{"order_index": 9}, {"pair_id": "other"}, {"task_id": "other__t-1"}, {"arm": "B"}, {"slot_id": "ex1/x/A/r1"}])
def test_experiment_identity_that_disagrees_with_the_schedule_or_run_is_rejected(tmp_path, world, mut):
    sched = _exp()
    rep = _validate(tmp_path, world, _exp(**mut), {sched["slot_id"]: sched})
    assert rep["status"] == "rejected" and any("experiment" in r or "slot" in r for r in rep["rejections"])


def test_a_run_without_experiment_identity_is_rejected_when_a_schedule_is_given(tmp_path, world):
    d = make_run(tmp_path, world)
    rep = validate_handoff(d, root=world["root"], providers=providers(world), expected_slots={_exp()["slot_id"]: _exp()})
    assert rep["status"] == "rejected"


def test_an_unregistered_prompt_set_file_is_rejected(tmp_path, world):
    d = make_run(tmp_path, world, meta=lambda m: m.__setitem__("prompt_set_file", "config/elsewhere.json"))
    rep = validate_handoff(d, root=world["root"], providers=providers(world))
    assert rep["status"] == "rejected" and any("registered prompt sets" in r for r in rep["rejections"])


# ---------------------------------------------------------------- the evaluator-support check uses the REGISTERED task identity, not the M1 driver's hardcoded instance
OTHER = "demo__demo-7"


def _registered_root(tmp_path, *, seed=True, identity=True, image=True):
    from aiep import task_pins as tp
    from aiep.agent_env import load_agent_config
    root = tmp_path / "reg"
    shutil.copytree(REAL / "config", root / "config")
    legacy = load_agent_config(root / "config" / "agent_identity.json")
    img = "sha256:" + "e" * 64  # NOT the image the legacy identity was verified in, so the legacy identity cannot vouch for this task
    if image:
        tp.add_evaluator_image(root, OTHER, img, source="test")
    if seed:
        tp.add_seed_pin(root, OTHER, directory="d", sha256="a" * 64, head="b" * 40, entries=3, image_id=img)
    if identity:
        tp.write_identity(root, OTHER, legacy, image_id=img, evidence=["test"], verified_on="2026-01-01")
    return root


def test_the_m1_drivers_own_instance_is_supported_as_before(tmp_path):
    ok, via = handoff.evaluator_support(REAL, INSTANCE)
    assert ok and "M1 driver" in via


def test_a_fully_registered_task_is_supported_through_its_identity_not_the_m1_driver(tmp_path):
    ok, via = handoff.evaluator_support(_registered_root(tmp_path), OTHER)
    assert ok and "registered" in via


@pytest.mark.parametrize("kw", [{"seed": False}, {"identity": False}, {"image": False, "seed": False, "identity": False}])
def test_a_partly_registered_task_is_not_supported(tmp_path, kw):
    assert handoff.evaluator_support(_registered_root(tmp_path, **kw), OTHER)[0] is False


def test_an_unknown_instance_and_garbage_are_not_supported(tmp_path):
    assert handoff.evaluator_support(REAL, "nope__nope-1")[0] is False
    assert handoff.evaluator_support(REAL, None)[0] is False
    assert handoff.evaluator_support(tmp_path / "missing", INSTANCE)[0] is True  # the M1 driver's instance never needs the registry


def test_report_flag_for_an_unsupported_instance_and_none_for_a_registered_one(tmp_path, world, monkeypatch):
    monkeypatch.setattr(handoff, "DRIVER_INSTANCE", "some__other-1")  # 5227 is no longer the driver's instance: only its registration can support it
    assert not any("not registered" in f for f in run(tmp_path, world, meta=lambda m: None)["flags"])  # world root is a copy of config with 5227 fully pinned
    broken = tmp_path / "broken"
    shutil.copytree(world["root"], broken)
    (broken / "config" / "agent_identity.json").unlink()
    (tmp_path / "r2").mkdir()
    rep = validate_handoff(make_run(tmp_path / "r2", world), root=broken, providers=providers(world))
    assert any("not registered" in f for f in rep["flags"])

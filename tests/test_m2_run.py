"""Orchestration with fakes: no Docker, no network, no Anthropic. The lifecycle itself is stubbed (it has its own tests)."""
import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from aiep import artifacts, experiment, m2_run
from aiep.agent_runner import AgentResult
from aiep.experiment import ClaudeCliUnpinned
from aiep.gateway_runner import GatewayImageUnpinned
from aiep.container_policy import PolicyViolation
from aiep.lifecycle import LifecycleResult
from aiep.patch import PatchResult
from aiep.seed import save_seed
from aiep import networks as N
from test_agent_runner import repo, seed  # noqa: F401 (fixtures)
from test_networks import Client as NetClient

REAL = Path(__file__).resolve().parents[1]
KEY = "sk-ant-api03-REAL-KEY-CANARY-abcdefghijklmnopqrstu"
BINARY = b"#!fake linux claude binary\n" * 500
IMAGE = json.loads((REAL / "config" / "evaluator_images.json").read_text())["images"]["pytest-dev__pytest-5227"]["image_id"]
HIDDEN = {"FAIL_TO_PASS": '["HIDDEN-F2P-CANARY"]', "PASS_TO_PASS": '["HIDDEN-P2P-CANARY"]',
          "test_patch": "HIDDEN-TEST-PATCH-CANARY", "patch": "HIDDEN-GOLD-PATCH-CANARY", "hints_text": "HIDDEN-HINT-CANARY"}
RECORD = {"instance_id": "pytest-dev__pytest-5227", "repo": "pytest-dev/pytest", "base_commit": "d" * 40,
          "problem_statement": "Improve logging format {name}\n", **HIDDEN}
STAMP = "20261119T231100Z"


@pytest.fixture(autouse=True)
def fake_record(monkeypatch):
    monkeypatch.setattr(m2_run, "load_record", lambda *a, **k: dict(RECORD))


@pytest.fixture
def root(tmp_path, seed):
    r = tmp_path / "root"
    shutil.copytree(REAL / "config", r / "config")
    from aiep.proxy_lifecycle import PROXY_FILES
    for rel in (*m2_run.build_gateway_component.__globals__["GATEWAY_FILES"], *PROXY_FILES):
        if rel.startswith("src/"):
            (r / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REAL / rel, r / rel)
    seed_dir = r / ".cache" / "seeds" / "pytest-dev__pytest-5227"
    save_seed(seed_dir, seed)
    (r / "config" / "seeds.json").write_text(json.dumps({"schema": 1, "seeds": {"pytest-dev__pytest-5227": {
        "directory": ".cache/seeds/pytest-dev__pytest-5227", "sha256": seed.manifest.archive_sha256, "head": seed.manifest.head,
        "entries": len(seed.manifest.entries), "image_id": IMAGE}}}))
    (r / "config" / "claude_cli.json").write_text(json.dumps({
        "schema": 1, "status": "pinned", "version": "2.1.328", "platform": "linux-x64",
        "sha256": hashlib.sha256(BINARY).hexdigest(), "size_bytes": len(BINARY), "destination": "/opt/aiep/claude",
        "max_binary_bytes": 400 * 1024**2}))
    (r / "config" / "gateway_image.json").write_text(json.dumps({
        "schema": 2, "status": "verified", "repo_digest": "python@sha256:" + "3" * 64, "image_id": "sha256:" + "9" * 64,
        "entrypoint": ["/bin/sleep"], "command": ["2147483647"], "python": "/usr/local/bin/python3", "uid": 10001, "gid": 10001,
        "declared_volumes": []}))
    (r / "claude").write_bytes(BINARY)
    return r


def prep(root, label="A", **over):
    kw = dict(instance_id="pytest-dev__pytest-5227", label=label, budget_usd="1.00", wall_clock_s=900, claude_binary=root / "claude",
              real_key=KEY, stamp=STAMP)
    kw.update(over)
    return m2_run.prepare(root, **kw)


# ------------------------------------------------------------------ prepare
def test_prepare_builds_policy_checked_specs_with_everything_pinned(root, seed):
    p = prep(root)
    assert p.run_id == f"m2-pytest-dev__pytest-5227-A-{STAMP}" and p.image_id == IMAGE and p.model == "claude-sonnet-5-5"
    assert p.aspec.gateway_url == p.gspec.url and p.aspec.run_token == p.gspec.run_token and p.aspec.agent_network == "aiep-agent-net"
    assert p.aspec.claude_args == experiment.claude_args("claude-sonnet-5-5", "1.00") and p.gspec.budget_usd == "1.00"
    assert p.aspec.components.manifest.entries and p.aspec.components_size == "416m"
    assert KEY in p.aspec.forbidden_secrets and KEY not in repr(p) and KEY not in repr(p.gspec) and KEY not in repr(p.aspec)
    assert p.seed.manifest.archive_sha256 == seed.manifest.archive_sha256


def test_a_and_b_runs_differ_only_in_the_workflow_instruction(root):
    a, b = prep(root, "A"), prep(root, "B")
    sa, sb = a.aspec, b.aspec
    same = ["image", "pinned_model", "python", "identity", "claude_args", "image_id", "wall_clock_s", "testbed_size", "tmp_size",
            "home_size", "components_size", "shm_size", "ipc_mode", "max_patch_bytes", "agent_network", "forbidden_secrets"]
    for f in same:
        assert getattr(sa, f) == getattr(sb, f), f
    assert sa.seed.manifest.archive_sha256 == sb.seed.manifest.archive_sha256
    assert sa.components.manifest.archive_sha256 == sb.components.manifest.archive_sha256
    assert (a.budget_usd, a.model, a.image_id) == (b.budget_usd, b.model, b.image_id)
    assert a.task == b.task and sa.config_label == "A" and sb.config_label == "B"
    w = a.prompts.workflows
    assert a.task_text.replace(w["A"], "@@") == b.task_text.replace(w["B"], "@@")
    # what is left is per-run identity (names, token), never a setting
    assert {sa.run_id != sb.run_id, sa.run_token != sb.run_token} == {True}


def test_hidden_dataset_fields_never_enter_the_prompt_or_the_specs(root):
    p = prep(root)
    blob = p.task_text + repr(p.aspec.claude_args) + p.task.to_json()
    for canary in HIDDEN.values():
        assert canary not in blob
    assert "Improve logging format {name}" in p.task_text and p.task.base_commit == "d" * 40 and "d" * 40 not in p.task_text


@pytest.mark.parametrize("problem,exc", [
    ("unpinned_cli", ClaudeCliUnpinned), ("unpinned_gateway", GatewayImageUnpinned), ("binary_hash", m2_run.PrepareError),
    ("binary_size", m2_run.PrepareError), ("seed_hash", Exception), ("seed_head", m2_run.PrepareError),
    ("seed_image", m2_run.PrepareError), ("symlink_binary", m2_run.PrepareError), ("missing_binary", m2_run.PrepareError),
])
def test_prepare_refuses_every_unverified_input_before_docker(root, problem, exc):
    kw = {}
    if problem == "unpinned_cli":
        d = json.loads((REAL / "config" / "claude_cli.json").read_text())
        d.update(status="unpinned", sha256=None, size_bytes=None)
        (root / "config" / "claude_cli.json").write_text(json.dumps(d))
    elif problem == "unpinned_gateway":
        d = json.loads((REAL / "config" / "gateway_image.json").read_text())
        d.update(status="unpinned", repo_digest=None, image_id=None)
        (root / "config" / "gateway_image.json").write_text(json.dumps(d))
    elif problem == "binary_hash":
        (root / "claude").write_bytes(b"X" * len(BINARY))
    elif problem == "binary_size":
        (root / "claude").write_bytes(BINARY + b"extra")
    elif problem in ("seed_hash", "seed_head", "seed_image"):
        f = root / "config" / "seeds.json"
        d = json.loads(f.read_text())
        pin = d["seeds"]["pytest-dev__pytest-5227"]
        pin[{"seed_hash": "sha256", "seed_head": "head", "seed_image": "image_id"}[problem]] = "0" * 40 if problem == "seed_head" else "sha256:" + "0" * 64 if problem == "seed_image" else "0" * 64
        f.write_text(json.dumps(d))
    elif problem == "symlink_binary":
        (root / "link").symlink_to(root / "claude")
        kw["claude_binary"] = root / "link"
    elif problem == "missing_binary":
        kw["claude_binary"] = root / "nope"
    with pytest.raises(exc):
        prep(root, **kw)


@pytest.mark.parametrize("over", [{"label": "C"}, {"real_key": ""}, {"real_key": "short"}, {"wall_clock_s": 5}, {"wall_clock_s": 99999},
                                  {"wall_clock_s": 3600.5}, {"wall_clock_s": 7200},
                                  {"wall_clock_s": True}, {"budget_usd": "abc"}, {"budget_usd": "0"}])
def test_prepare_refuses_bad_parameters(root, over):
    with pytest.raises(Exception):
        prep(root, **over)


# ------------------------------------------------------------------ execute: artifacts
def lifecycle(p, *, patch="diff --git a/a.py b/a.py\n", valid=True, status="completed", with_agent=True):
    agent = AgentResult(
        status="exited", valid=valid, invalid_reason=None if valid else "gateway stopped the run: model_violation", stop_reason=None,
        claude_exit_code=0, duration_s=12.5,
        claude_stdout=(f'{{"type":"system"}}\n{{"type":"assistant","text":"token {p.aspec.run_token} key {KEY}"}}\n'
                       '{"type":"result","subtype":"success","is_error":false,"num_turns":3,"duration_ms":1000,"total_cost_usd":0.42}\n').encode(),
        claude_stderr=b"warn\n", patch=PatchResult("ok", patch, [], hashlib.sha256(patch.encode()).hexdigest(), len(patch)) if patch else None,
        seed_head=p.seed.manifest.head, seed_sha256=p.seed.manifest.archive_sha256, container_id="abc", removed=True,
        config_label=p.label, account="nonroot:1000:1000")
    ledger = (json.dumps({"seq": 1, "usage": {"input_tokens": 100, "output_tokens": 20}, "cumulative_usd_computed": "0.01"}) + "\n"
              + json.dumps({"seq": 2, "usage": {"input_tokens": 50, "output_tokens": 5}, "cumulative_usd_computed": "0.02"}) + "\n").encode()
    return LifecycleResult(status=status, agent=agent if with_agent else None, ledger=ledger, gateway_stop_reason=None,
                           gateway_removed=True, gateway_state="removed")


def run(p, root, res=None, exc=None):
    def fn(client, gspec, aspec):
        if exc is not None:
            raise exc
        return res if res is not None else lifecycle(p)
    return m2_run.execute(good_nets(), p, base=root / "runs" / "m2", root=root, run_fn=fn)


def test_successful_run_persists_everything_and_hands_off_to_evaluation(root):
    p = prep(root)
    ex = run(p, root)
    d = ex.directory
    assert d == root / "runs" / "m2" / p.run_id and ex.problems == [] and artifacts.verify_run_artifacts(d) == []
    assert {x.name for x in d.iterdir()} == {"prompt.txt", "claude_stdout.jsonl", "claude_stderr.txt", "gateway_ledger.jsonl",
                                             "patch.diff", "predictions.jsonl", "run.json", "artifacts_manifest.json"}
    assert (d / "prompt.txt").read_text() == p.task_text and (d / "patch.diff").read_text() == "diff --git a/a.py b/a.py\n"
    pred = json.loads((d / "predictions.jsonl").read_text())
    assert pred == {"instance_id": "pytest-dev__pytest-5227", "model_name_or_path": "aiep-A-claude-sonnet-5-5",
                    "model_patch": "diff --git a/a.py b/a.py\n"}
    assert ex.eval_command == f"scripts/m1_eval.sh m2eval-{p.run_id} {d / 'predictions.jsonl'}"
    run_json = json.loads((d / "run.json").read_text())
    assert run_json["task"] == {"instance_id": "pytest-dev__pytest-5227", "repo": "pytest-dev/pytest", "base_commit": "d" * 40}
    assert run_json["seed"]["sha256"] == p.seed.manifest.archive_sha256 and run_json["model"] == "claude-sonnet-5-5"
    assert run_json["config"] == "A" and run_json["prompt"]["prompt_sha256"] == experiment.sha256_text(p.task_text)
    assert run_json["prompt"]["workflow_sha256"] == p.prompts.hashes["A"] and run_json["claude_cli"]["sha256"] == p.cli.sha256
    assert run_json["claude_args"] == list(p.aspec.claude_args) and run_json["limits"]["budget_usd"] == "1.00"
    assert run_json["usage_measured"]["tokens"] == {"input_tokens": 150, "output_tokens": 25}
    assert run_json["usage_measured"]["metered"] == 2 and run_json["usage_measured"]["computed_usd_upper_bound_basis"] == "0.02"
    assert "NOT measured" in run_json["usage_cli_estimate"]["source"] and run_json["usage_cli_estimate"]["total_cost_usd"] == 0.42
    assert "not run" in run_json["evaluation"] and run_json["lifecycle"]["agent"]["claude_exit_code"] == 0


def test_secrets_are_scrubbed_from_every_artifact(root):
    p = prep(root)
    ex = run(p, root)
    for f in ex.directory.iterdir():
        blob = f.read_bytes()
        assert KEY.encode() not in blob and p.aspec.run_token.encode() not in blob, f.name
    run_json = json.loads((ex.directory / "run.json").read_text())
    assert run_json["redactions"]["claude_stdout.jsonl"] == 2
    assert not any(c.encode() in (ex.directory / "prompt.txt").read_bytes() for c in HIDDEN.values())


def test_an_invalid_run_is_kept_without_a_patch_or_predictions(root):
    p = prep(root)
    ex = run(p, root, lifecycle(p, valid=False))
    names = {x.name for x in ex.directory.iterdir()}
    assert "patch.diff" not in names and "predictions.jsonl" not in names and ex.eval_command is None and ex.problems == []
    run_json = json.loads((ex.directory / "run.json").read_text())
    assert run_json["lifecycle"]["agent"]["valid"] is False and "model_violation" in run_json["lifecycle"]["agent"]["invalid_reason"]


def test_a_valid_run_with_no_patch_has_no_predictions(root):
    p = prep(root)
    ex = run(p, root, lifecycle(p, patch=None))
    assert ex.eval_command is None and not (ex.directory / "predictions.jsonl").exists()


def test_a_gateway_failure_still_leaves_a_record(root):
    p = prep(root)
    res = LifecycleResult(status="gateway_failed", gateway_error="GatewayStartError: did not become ready", gateway_state="created",
                          gateway_removed=True)
    ex = run(p, root, res)
    assert {x.name for x in ex.directory.iterdir()} == {"prompt.txt", "run.json", "artifacts_manifest.json"}
    assert json.loads((ex.directory / "run.json").read_text())["lifecycle"]["gateway_error"].startswith("GatewayStartError")


def test_an_exception_in_the_lifecycle_is_recorded_and_propagates(root):
    p = prep(root)
    with pytest.raises(RuntimeError, match="boom"):
        run(p, root, exc=RuntimeError("boom"))
    d = root / "runs" / "m2" / p.run_id
    assert artifacts.verify_run_artifacts(d) == []
    run_json = json.loads((d / "run.json").read_text())
    assert run_json["orchestrator_error"] == "RuntimeError: boom" and run_json["interrupted"] is False and run_json["lifecycle"] is None


def test_a_keyboard_interrupt_still_persists_and_propagates(root):
    p = prep(root)
    with pytest.raises(KeyboardInterrupt):
        run(p, root, exc=KeyboardInterrupt())
    run_json = json.loads((root / "runs" / "m2" / p.run_id / "run.json").read_text())
    assert run_json["interrupted"] is True and "KeyboardInterrupt" in run_json["orchestrator_error"]


def test_a_failure_to_write_artifacts_never_hides_the_original_exception(root, monkeypatch, capsys):
    p = prep(root)
    monkeypatch.setattr(artifacts, "_write_new", lambda *a: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    with pytest.raises(RuntimeError, match="boom"):
        run(p, root, exc=RuntimeError("boom"))
    assert "artifacts could not be written" in capsys.readouterr().err


def test_a_failure_to_write_artifacts_after_a_good_run_is_raised(root):
    p = prep(root)
    (root / "runs" / "m2" / p.run_id).mkdir(parents=True)  # the run directory already exists
    with pytest.raises(artifacts.ArtifactError, match="already exists"):
        run(p, root)


def test_the_orchestrator_never_runs_the_evaluator_or_touches_docker_itself():
    src = Path(m2_run.__file__).read_text()
    for banned in ("stock_eval", "aiep.evaluator", "from . import evaluator", "docker.from_env", "import docker", "run_evaluation"):
        assert banned not in src
    assert "m1_eval.sh" in src  # it only prints the handoff command


def test_ledger_and_estimate_summaries_tolerate_garbage():
    assert m2_run.summarize_ledger(None) is None
    s = m2_run.summarize_ledger(b'not json\n{"usage": {"input_tokens": 3, "x": true}}\n{"seq":2}\n')
    assert s["requests"] == 2 and s["metered"] == 1 and s["tokens"] == {"input_tokens": 3}
    assert m2_run.cli_estimate(b"nothing here\n") is None
    assert m2_run.cli_estimate(b'{"type":"result","total_cost_usd":1.5,"extra":"x"}\n')["total_cost_usd"] == 1.5


# ------------------------------------------------------------------ the script
spec_ = importlib.util.spec_from_file_location("run_m2_agent", REAL / "scripts" / "run_m2_agent.py")
cli = importlib.util.module_from_spec(spec_)
spec_.loader.exec_module(cli)
GIB = 1024**3


def good_nets():
    """A fake Docker client whose two AIEP networks exist and match policy exactly (what a real preflight must see)."""
    c = NetClient()
    N.ensure_networks(c)
    return c


class Client:
    def __init__(self, close_error=None):
        self.closed, self.close_error = 0, close_error
        self.networks = good_nets().networks

    def close(self):
        self.closed += 1
        if self.close_error:
            raise self.close_error


def drive(root, argv, *, free=16 * GIB, env=None, run_fn=None, client=None):
    client = client or Client()
    built = []

    def factory():
        built.append(1)
        return client

    rc = cli.main(argv, client_factory=factory, free_bytes=lambda q: free, root=root, run_fn=run_fn,
                  env={"AIEP_UPSTREAM_API_KEY": KEY} if env is None else env)
    return rc, built, client


FULL = ["--config", "A", "--budget-usd", "1.00", "--claude-binary", "{bin}", "--execute", "--approve-spend", "1.00"]


def full(root, **over):
    return [a.replace("{bin}", str(root / "claude")) for a in FULL]


def test_plan_is_the_default_and_needs_no_docker_key_or_pins(capsys):
    rc, built, client = drive(REAL, ["--config", "B", "--budget-usd", "2.50"], env={})
    out = capsys.readouterr().out
    assert rc == 0 and built == [] and "PLAN" in out and "nothing is started" in out
    assert "Claude CLI" not in out  # pinned at Gate 1b: no longer an open blocker
    assert "gateway image" not in out  # pinned, with its identity recorded: no longer an open blocker
    assert "AIEP_UPSTREAM_API_KEY is not set" in out and "no --claude-binary file given" in out and "not approved to spend" in out
    assert experiment.load_prompts().hashes["B"] in out and "--max-budget-usd 2.75" in out and "budget $2.50" in out  # client 2.50 x 1.10; the gateway keeps 2.50


@pytest.mark.parametrize("argv", [["--config", "A", "--budget-usd", "1.00", "--execute"],
                                  ["--config", "A", "--budget-usd", "1.00", "--execute", "--approve-spend", "2.00"],
                                  ["--config", "A", "--budget-usd", "1.00", "--execute", "--approve-spend", "1.0"]])
def test_a_paid_run_needs_explicit_matching_approval(root, argv, capsys):
    rc, built, client = drive(root, argv)
    assert rc == 2 and built == [] and "REFUSED" in capsys.readouterr().err and not (root / "runs").exists()


def test_approval_without_execute_is_still_only_a_plan(root, capsys):
    rc, built, _ = drive(root, ["--config", "A", "--budget-usd", "1.00", "--approve-spend", "1.00"])
    assert rc == 0 and built == [] and "PLAN" in capsys.readouterr().out and not (root / "runs").exists()


def test_open_blockers_refuse_before_any_docker_call(capsys):
    # both pins are real and loadable now; what is still open on the real repository is the key (and, in a plan, the binary file)
    rc, built, _ = drive(REAL, ["--config", "A", "--budget-usd", "1.00", "--claude-binary", str(REAL / "config" / "seeds.json"),
                                "--execute", "--approve-spend", "1.00"], env={})
    err = capsys.readouterr().err
    assert rc == 2 and built == [] and "AIEP_UPSTREAM_API_KEY is not set" in err
    assert "gateway image" not in err and "Claude CLI" not in err


def test_the_disk_gate_applies_before_any_docker_call(root, capsys):
    rc, built, client = drive(root, full(root), free=16 * GIB - 1)
    assert rc == 2 and built == [] and client.closed == 0 and "REFUSED" in capsys.readouterr().err


def test_malformed_budget_is_refused_up_front(root):
    rc, built, _ = drive(root, ["--config", "A", "--budget-usd", "1e9", "--execute", "--approve-spend", "1e9"])
    assert rc == 2 and built == []


def test_a_local_precheck_failure_never_constructs_the_docker_client(root, capsys):
    (root / "claude").write_bytes(b"tampered")
    rc, built, _ = drive(root, full(root))
    assert rc == 1 and built == [] and "ERROR before Docker" in capsys.readouterr().err


def test_a_full_run_persists_closes_the_client_and_never_prints_the_key(root, capsys):
    holder = {}

    def fn(client, gspec, aspec):
        p = holder.setdefault("p", prep(root))
        return lifecycle(p)

    rc, built, client = drive(root, full(root), run_fn=fn)
    cap = capsys.readouterr()
    assert rc == 0 and built == [1] and client.closed == 1
    assert KEY not in cap.out + cap.err and "evaluation NOT run" in cap.out and "m1_eval.sh m2eval-" in cap.out
    runs = list((root / "runs" / "m2").iterdir())
    assert len(runs) == 1 and artifacts.verify_run_artifacts(runs[0]) == []


def test_a_failing_run_exits_nonzero_but_still_closes_the_client_and_keeps_artifacts(root, capsys):
    def fn(client, gspec, aspec):
        raise RuntimeError("daemon went away")

    rc, built, client = drive(root, full(root), run_fn=fn)
    assert rc == 1 and client.closed == 1 and "daemon went away" in capsys.readouterr().err
    (run_dir,) = list((root / "runs" / "m2").iterdir())
    assert artifacts.verify_run_artifacts(run_dir) == []


def test_a_gateway_failed_lifecycle_exits_nonzero(root):
    def fn(client, gspec, aspec):
        return LifecycleResult(status="gateway_failed", gateway_error="GatewayStartError: x", gateway_removed=True)

    rc, _, client = drive(root, full(root), run_fn=fn)
    assert rc == 1 and client.closed == 1


def test_a_client_close_failure_fails_the_run_and_is_reported(root, capsys):
    p_holder = {}
    rc, _, client = drive(root, full(root), run_fn=lambda c, g, a: lifecycle(p_holder.setdefault("p", prep(root))),
                          client=Client(close_error=OSError("socket")))
    assert rc == 1 and "closing the Docker client failed" in capsys.readouterr().err


# ------------------------------------------------------------------ the run lease
from aiep.lease import LeaseError, read_lease_status  # noqa: E402
from aiep.limits import DEFAULT_WALL_CLOCK_S, MAX_WALL_CLOCK_S, MIN_WALL_CLOCK_S  # noqa: E402


def test_the_wall_clock_ceiling_is_the_central_one(root):
    assert prep(root, wall_clock_s=MAX_WALL_CLOCK_S).wall_clock_s == MAX_WALL_CLOCK_S
    assert prep(root, wall_clock_s=MIN_WALL_CLOCK_S).wall_clock_s == MIN_WALL_CLOCK_S
    for bad in (MAX_WALL_CLOCK_S + 1, MIN_WALL_CLOCK_S - 1):
        with pytest.raises(m2_run.PrepareError):
            prep(root, wall_clock_s=bad)
    assert cli.DEFAULT_WALL_CLOCK_S == DEFAULT_WALL_CLOCK_S == 1800.0


def test_the_lease_is_held_with_a_fresh_heartbeat_while_the_lifecycle_runs_and_released_after(root):
    p = prep(root)
    seen = {}

    def fn(client, gspec, aspec):
        st = read_lease_status(root / ".cache" / "leases", p.run_id, now=__import__("time").time())
        seen["status"], seen["exists"] = st.status, (root / ".cache" / "leases" / f"{p.run_id}.lease").exists()
        return lifecycle(p)

    ex = m2_run.execute(good_nets(), p, base=root / "runs" / "m2", root=root, run_fn=fn)
    assert seen == {"status": "fresh", "exists": True}  # taken BEFORE the lifecycle (so before any container)
    assert not (root / ".cache" / "leases" / f"{p.run_id}.lease").exists()  # released at the end
    run_json = json.loads((ex.directory / "run.json").read_text())
    assert run_json["lease"]["errors"] == [] and isinstance(run_json["lease"]["heartbeats"], int)


def test_the_heartbeat_actually_runs_during_a_long_lifecycle(root):
    p = prep(root)

    def fn(client, gspec, aspec):
        import time
        deadline = time.monotonic() + 5
        path = root / ".cache" / "leases" / f"{p.run_id}.lease"
        first = json.loads(path.read_text())["heartbeat_at"]
        while json.loads(path.read_text())["heartbeat_at"] == first and time.monotonic() < deadline:
            time.sleep(0.01)
        assert json.loads(path.read_text())["heartbeat_at"] > first
        return lifecycle(p)

    ex = m2_run.execute(good_nets(), p, base=root / "runs" / "m2", root=root, run_fn=fn, lease_interval_s=0.02)
    assert json.loads((ex.directory / "run.json").read_text())["lease"]["heartbeats"] >= 1


@pytest.mark.parametrize("exc", [RuntimeError("boom"), KeyboardInterrupt()])
def test_the_lease_is_released_when_the_lifecycle_raises_or_is_interrupted(root, exc):
    p = prep(root)
    with pytest.raises(type(exc)):
        run(p, root, exc=exc)
    assert not (root / ".cache" / "leases" / f"{p.run_id}.lease").exists()


def test_the_lease_is_released_even_when_artifact_writing_fails(root):
    p = prep(root)
    (root / "runs" / "m2" / p.run_id).mkdir(parents=True)
    with pytest.raises(artifacts.ArtifactError):
        run(p, root)
    assert not (root / ".cache" / "leases" / f"{p.run_id}.lease").exists()


def test_if_the_lease_cannot_be_taken_nothing_runs_and_nothing_is_written(root):
    p = prep(root)
    called = []
    (root / ".cache" / "leases").mkdir(parents=True)
    (root / ".cache" / "leases" / f"{p.run_id}.lease").write_text("{}")  # another orchestrator (or a stale one) holds this run id
    with pytest.raises(LeaseError):
        m2_run.execute(good_nets(), p, base=root / "runs" / "m2", root=root, run_fn=lambda *a: called.append(1))
    assert called == [] and not (root / "runs" / "m2" / p.run_id).exists()
    assert (root / ".cache" / "leases" / f"{p.run_id}.lease").read_text() == "{}"  # someone else's lease is never removed by us


def test_an_unwritable_lease_directory_stops_the_run_before_any_container(root):
    p = prep(root)
    (root / ".cache").mkdir(parents=True, exist_ok=True)
    (root / ".cache" / "leases").write_text("a file where the lease directory should be")
    called = []
    with pytest.raises(LeaseError):
        m2_run.execute(good_nets(), p, base=root / "runs" / "m2", root=root, run_fn=lambda *a: called.append(1))
    assert called == []


# ------------------------------------------------------------------ audit sprint: network preflight, accounting, truncation, budgets
def _called_run(p, root, client):
    called = []
    try:
        m2_run.execute(client, p, base=root / "runs" / "m2", root=root, run_fn=lambda *a: called.append(1) or lifecycle(p))
    finally:
        pass
    return called


def test_networks_are_verified_through_docker_before_anything_is_created(root):
    p = prep(root)
    events = []
    client = good_nets()
    real_get = client.networks.get
    client.networks.get = lambda name: (events.append(("inspect", name)), real_get(name))[1]
    m2_run.execute(client, p, base=root / "runs" / "m2", root=root,
                   run_fn=lambda *a: events.append(("lifecycle", None)) or lifecycle(p))
    assert events[:3] == [("inspect", "aiep-agent-net"), ("inspect", "aiep-proxy-net"), ("inspect", "aiep-egress-net")] and events[-1] == ("lifecycle", None)
    run_json = json.loads((root / "runs" / "m2" / p.run_id / "run.json").read_text())
    assert set(run_json["networks"]) == {"aiep-agent-net", "aiep-proxy-net", "aiep-egress-net"}  # ids recorded
    assert set(run_json["network_subnets"]) == {"aiep-agent-net", "aiep-proxy-net"} and run_json["proxy"]["endpoint"] == f"aiep.proxy.{p.run_id}:3128"


def _mutate(client, name, **changes):
    attrs = client.networks.store[name]
    for k, v in changes.items():
        if k.startswith("Options."):
            if v is None:
                attrs["Options"].pop(k.split(".", 1)[1], None)
            else:
                attrs["Options"][k.split(".", 1)[1]] = v
        else:
            attrs[k] = v


@pytest.mark.parametrize("name,changes", [
    ("aiep-agent-net", {"Internal": False}),                                              # the agent could reach the internet
    ("aiep-agent-net", {"Options.com.docker.network.bridge.inhibit_ipv4": None}),         # VM services reachable through the bridge address
    ("aiep-agent-net", {"Options.com.docker.network.bridge.inhibit_ipv4": "false"}),
    ("aiep-agent-net", {"Driver": "macvlan"}),
    ("aiep-agent-net", {"Attachable": True}),
    ("aiep-agent-net", {"EnableIPv6": True}),
    ("aiep-agent-net", {"Labels": {}}),                                                   # same name, not created by AIEP
    ("aiep-agent-net", {"Containers": {"abc": {"Name": "somebody-else"}}}),               # another container already on the agent network
    ("aiep-agent-net", {"IPAM": {"Config": [{"Subnet": "172.30.1.0/24", "Gateway": "172.30.1.1"}]}}),
    ("aiep-proxy-net", {"Internal": False}),                                              # the gateway would have a route out
    ("aiep-proxy-net", {"Options.com.docker.network.bridge.inhibit_ipv4": None}),
    ("aiep-proxy-net", {"Containers": {"abc": {"Name": "somebody-else"}}}),
    ("aiep-proxy-net", {"Labels": {}}),
    ("aiep-egress-net", {"Internal": True}),
    ("aiep-egress-net", {"Options.com.docker.network.bridge.enable_icc": None}),
    ("aiep-egress-net", {"Labels": {"aiep.managed": "true", "aiep.role": "agent-network"}}),
])
def test_an_off_policy_network_stops_the_run_before_the_lease_or_any_container(root, name, changes):
    p = prep(root)
    client = good_nets()
    _mutate(client, name, **changes)
    called = []
    with pytest.raises(N.NetworkError, match="does not match policy"):
        m2_run.execute(client, p, base=root / "runs" / "m2", root=root, run_fn=lambda *a: called.append(1))
    assert called == []  # the lifecycle (which creates the containers) never ran
    assert not (root / ".cache" / "leases").exists() or list((root / ".cache" / "leases").iterdir()) == []
    assert not (root / "runs").exists()


def test_a_missing_network_is_an_error_and_is_never_created_by_the_run(root):
    p = prep(root)
    client = good_nets()
    del client.networks.store["aiep-agent-net"]
    calls_before = len(client.networks.calls)
    with pytest.raises(N.NetworkError, match="cannot inspect"):
        m2_run.execute(client, p, base=root / "runs" / "m2", root=root, run_fn=lambda *a: pytest.fail("must not run"))
    assert "aiep-agent-net" not in client.networks.store and len(client.networks.calls) == calls_before  # nothing was created


@pytest.mark.parametrize("agent,proxy,egress", [("bridge", "aiep-proxy-net", "aiep-egress-net"), ("aiep-agent-net", "bridge", "aiep-egress-net"),
                                                ("aiep-agent-net", "aiep-proxy-net", "bridge"), ("none", "aiep-proxy-net", "aiep-egress-net"),
                                                ("host", "host", "host"), ("aiep-egress-net", "aiep-proxy-net", "aiep-agent-net"),
                                                ("aiep-agent-net", "aiep-egress-net", "aiep-proxy-net"), ("custom", "custom2", "custom3")])
def test_network_overrides_that_violate_policy_are_refused_at_prepare_and_at_execute(root, agent, proxy, egress):
    with pytest.raises(m2_run.PrepareError, match="no override"):
        prep(root, agent_network=agent, proxy_network=proxy, egress_network=egress)
    import dataclasses
    p = prep(root)  # a Prepared tampered with after prepare() is caught by the preflight too (or is refused even earlier, by the policy)
    try:
        p.aspec = dataclasses.replace(p.aspec, agent_network=agent)
        p.gspec = dataclasses.replace(p.gspec, ctx=dataclasses.replace(p.gspec.ctx, proxy_network=proxy))
        p.pspec = dataclasses.replace(p.pspec, ctx=dataclasses.replace(p.pspec.ctx, proxy_network=proxy, egress_network=egress))
    except PolicyViolation:
        return
    with pytest.raises((N.NetworkError, m2_run.PrepareError)):
        m2_run.execute(good_nets(), p, base=root / "runs" / "m2", root=root, run_fn=lambda *a: pytest.fail("must not run"))


def test_there_is_one_definition_of_the_network_names():
    assert not hasattr(m2_run, "DEFAULT_AGENT_NETWORK") and not hasattr(m2_run, "DEFAULT_EGRESS_NETWORK")
    assert m2_run.AGENT_NETWORK is N.AGENT_NETWORK and m2_run.EGRESS_NETWORK is N.EGRESS_NETWORK and m2_run.PROXY_NETWORK is N.PROXY_NETWORK


def _incomplete(p):
    r = lifecycle(p)
    r.ledger = (json.dumps({"seq": 1, "outcome": "forwarding", "rid": "r1", "requested_max_tokens": 64000, "stream": True}) + "\n").encode()
    r.accounting = {"complete": False, "exact": False, "readable": True, "unresolved": [{"rid": "r1", "requested_max_tokens": 64000}],
                    "drain": {"timed_out": True}, "note": "INCOMPLETE"}
    return r


def test_incomplete_accounting_is_recorded_in_run_json_and_reported_as_a_problem_never_as_exact(root):
    p = prep(root)
    ex = run(p, root, res=_incomplete(p))
    run_json = json.loads((ex.directory / "run.json").read_text())
    assert run_json["accounting"]["complete"] is False and run_json["lifecycle"]["accounting"]["unresolved"][0]["rid"] == "r1"
    assert run_json["usage_measured"]["exact"] is False and run_json["usage_measured"]["unresolved_requests"] == 1
    assert run_json["usage_measured"]["requests"] == 0  # a 'forwarding' line is the start of a request, not a finished one
    assert any("INCOMPLETE" in x for x in ex.problems)


def test_a_complete_ledger_is_exact(root):
    p = prep(root)
    ex = run(p, root)
    assert json.loads((ex.directory / "run.json").read_text())["usage_measured"]["exact"] is True and ex.problems == []


def test_entries_charged_at_the_upper_bound_are_not_exact():
    ledger = (json.dumps({"rid": "r1", "outcome": "forwarding"}) + "\n"
              + json.dumps({"rid": "r1", "outcome": "client_disconnected", "usage": {"output_tokens": 64000},
                            "usage_incomplete_upper_bound": True, "cumulative_usd_computed": "0.64"}) + "\n").encode()
    s = m2_run.summarize_ledger(ledger)
    assert s["exact"] is False and s["entries_charged_at_max_tokens_upper_bound"] == 1 and s["unresolved_requests"] == 0 and s["requests"] == 1


@pytest.mark.parametrize("gateway_reason,subtype,want", [
    ("budget_exhausted", None, "gateway"), (None, "error_max_budget_usd", "client"),
    ("budget_exhausted", "error_max_budget_usd", "both"), (None, "success", None), (None, None, None),
    ("model_violation", "success", None)])
def test_the_gateway_budget_and_the_clients_own_budget_are_separate_termination_reasons(gateway_reason, subtype, want):
    est = None if subtype is None else {"subtype": subtype}
    a = m2_run.budget_attribution(gateway_reason, est)
    assert a["terminated_by"] == want
    assert a["gateway_budget_exhausted"] is (gateway_reason == "budget_exhausted")
    assert a["client_budget_exceeded"] is (subtype == "error_max_budget_usd")


def test_the_run_record_attributes_each_budget_stop_to_the_right_side(root):
    p = prep(root)
    r = lifecycle(p)
    r.gateway_stop_reason = "budget_exhausted"
    assert json.loads((run(p, root, res=r).directory / "run.json").read_text())["budget_attribution"]["terminated_by"] == "gateway"
    p2 = prep(root, stamp="20260101T000002Z")
    r2 = lifecycle(p2)
    r2.agent.claude_stdout = b'{"type":"result","subtype":"error_max_budget_usd","is_error":true,"total_cost_usd":1.2}\n'
    rj = json.loads((run(p2, root, res=r2).directory / "run.json").read_text())
    assert rj["budget_attribution"] == {"gateway_budget_exhausted": False, "client_budget_exceeded": True, "terminated_by": "client"}
    assert rj["limits"]["gateway_budget_usd"] == "1.00" and rj["limits"]["claude_client_budget_usd"] == "1.1"  # gateway authoritative, client above


def test_the_run_record_carries_the_pinned_claude_runtime_and_what_stays_uncontrolled(root):
    from aiep.limits import AGENT_RUNTIME_PINS
    p = prep(root)
    rj = json.loads((run(p, root).directory / "run.json").read_text())
    assert rj["claude_runtime"]["pinned_env"] == AGENT_RUNTIME_PINS and rj["claude_runtime"]["uncontrolled"]
    assert rj["limits"]["gateway_drain_timeout_s"] > 0


def test_a_truncated_claude_output_never_reaches_predictions_or_normal_grading(root):
    p = prep(root)
    r = lifecycle(p, valid=False)  # run_agent marks a censored run invalid; this checks everything downstream of that
    r.agent.claude_output_truncated = True
    r.agent.invalid_reason = "claude output truncated at the 33554432-byte cap per stream: the run is censored and is not graded"
    ex = run(p, root, res=r)
    assert ex.predictions is None and ex.eval_command is None
    assert {x.name for x in ex.directory.iterdir()}.isdisjoint({"predictions.jsonl", "patch.diff"})
    rj = json.loads((ex.directory / "run.json").read_text())
    assert rj["lifecycle"]["agent"]["claude_output_truncated"] is True and rj["lifecycle"]["agent"]["valid"] is False
    assert "censored" in rj["lifecycle"]["agent"]["invalid_reason"]
    assert any("truncated" in x for x in ex.problems)


def test_even_if_a_truncated_result_were_wrongly_marked_valid_the_handoff_reports_it(root):
    p = prep(root)
    r = lifecycle(p)  # deliberately inconsistent: valid=True but truncated
    r.agent.claude_output_truncated = True
    ex = run(p, root, res=r)
    assert any("truncated" in x for x in ex.problems)  # the CLI exits non-zero on any problem, so this cannot pass as a clean run


def test_the_agent_spec_carries_the_verified_network_identity_into_the_lifecycle(root):
    p = prep(root)
    assert p.aspec.network_id is None and p.aspec.network_cidr is None  # unknown until Docker has been asked
    seen = {}
    m2_run.execute(good_nets(), p, base=root / "runs" / "m2", root=root,
                   run_fn=lambda c, g, a: seen.update(aspec=a, gspec=g) or lifecycle(p))
    a = seen["aspec"]
    assert a.network_id == "id-aiep-agent-net" and a.network_cidr == "172.30.1.0/24"
    assert seen["gspec"].ctx.agent_cidr == a.network_cidr

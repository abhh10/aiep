import io
import json
import os
import subprocess
import tarfile
import threading
from pathlib import Path

import pytest

from aiep import agent_runner as AR
from aiep import container_exec as CE
from aiep.limits import AGENT_RUNTIME_PINS
from aiep.agent_env import experiment_env
from aiep.agent_runner import (STATUS_CONTAINER_DIED, STATUS_EXITED, STATUS_GATEWAY_STOPPED, STATUS_INVALID,
                               STATUS_TIMEOUT, AgentRunSpec, build_create_kwargs, run_agent)
from aiep.container_policy import (AgentIdentity, AGENT_KEEPALIVE_COMMAND, AGENT_MEM_BYTES, AGENT_NANO_CPUS, AGENT_PIDS,
                                   PolicyViolation)
from aiep.patch import HARDENED_GIT
from aiep.seed import VERIFY_SCRIPT, build_component_archive, parse_seed_archive
from fake_docker import ExecResult, FakeAgent, FakeContainerFS, FakeExecApi

MODEL = "claude-sonnet-5-5"
REAL_KEY = "sk-ant-REAL-KEY-CANARY"
TASK = "ISSUE-TEXT-CANARY: the logging format is wrong when the module name is long. Please fix it."
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True).stdout.strip()


# ---------------- a real repository that looks like the instance image's /testbed ----------------
@pytest.fixture
def repo(tmp_path):
    """Two commits: the dataset's base_commit, then the image's synthetic 'SWE-bench' commit on top."""
    d = tmp_path / "image_testbed"
    d.mkdir()
    git(d, "init", "-q", "-b", "main")
    (d / "a.py").write_text("print('a')\n")
    (d / "keep.txt").write_text("keep\n")
    (d / "tool.sh").write_text("#!/bin/sh\n")
    os.chmod(d / "tool.sh", 0o755)
    git(d, "add", "-A")
    git(d, "commit", "-qm", "upstream base")
    base = git(d, "rev-parse", "HEAD")
    (d / "a.py").write_text("print('a')\n# synthetic image change\n")
    git(d, "commit", "-qam", "SWE-bench")
    head = git(d, "rev-parse", "HEAD")
    assert base != head
    return d, base, head


def archive_of(directory: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        tf.add(directory, arcname="testbed")
    return buf.getvalue()


@pytest.fixture
def seed(repo):
    d, base, head = repo
    s = parse_seed_archive(archive_of(d))
    assert s.head == head
    return s


# Test-specific fixture identity, used with fakes only. It does not come from config/agent_identity.json. (The values happen to
# equal the account the conformance probe verified in the real image: nonroot, uid 1000, gid 1000, /home/nonroot. They no
# longer stand in for an unverified account; they are just fixtures for a fake container.)
IDENT = AgentIdentity(name="nonroot", uid=1000, gid=1000, home="/home/nonroot")
COMPONENTS = build_component_archive({"claude": (b"#!fake claude binary\n", 0o755)})


def spec_for(seed, **over):
    d = dict(run_id="r1", image="instance:1", gateway_url="http://gateway:8080", agent_network="aiep-agent-net",
             pinned_model=MODEL, run_token="per-run-token", seed=seed, components=COMPONENTS, task_text=TASK,
             claude_args=("-p", "--output-format", "stream-json"), wall_clock_s=5.0, python="/opt/miniconda3/bin/python",
             identity=IDENT, config_label="A",
             forbidden_secrets=(REAL_KEY,))
    d.update(over)
    return AgentRunSpec(**d)


# ---------------- fakes for the Docker client ----------------
_SHM_UNITS = {"k": 1024, "m": 1024**2, "g": 1024**3}


def shm_bytes(text):  # independent of the policy code under test: "64m" -> 67108864
    return int(text[:-1]) * _SHM_UNITS[text[-1].lower()] if text[-1].isalpha() else int(text)


def good_inspect(spec):
    kw = build_create_kwargs(spec)
    return {
        "Image": spec.image_id or "sha256:" + "a" * 64,
        "HostConfig": {"Privileged": False, "CapAdd": None, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"],
                       "NetworkMode": spec.agent_network, "Memory": AGENT_MEM_BYTES, "PidsLimit": AGENT_PIDS,
                       "NanoCpus": AGENT_NANO_CPUS, "ReadonlyRootfs": True, "Binds": None, "Tmpfs": kw["tmpfs"],
                       "IpcMode": spec.ipc_mode, "ShmSize": shm_bytes(spec.shm_size)},
        "Mounts": [], "Config": {"User": spec.user, "Env": ["ANTHROPIC_AUTH_TOKEN=tok", *[f"{k}={v}" for k, v in AGENT_RUNTIME_PINS.items()]]},
        "State": {"Running": True, "Paused": False, "Restarting": False, "Dead": False, "Status": "running"},
        "NetworkSettings": {"Networks": {spec.agent_network: {"NetworkID": AGENT_NET_ID, "IPAddress": "172.30.1.5", "GlobalIPv6Address": "", "Gateway": ""}},
                            "Ports": {}},
    }


AGENT_NET_ID = "net-agent-id"
AGENT_ROUTES = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
                "eth0\t00011EAC\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n")  # 172.30.1.0/24 on eth0, no default route


class FakeContainer:
    id = "abc123"

    def __init__(self, start_error=None, remove_error=None):
        self.status = "created"
        self.calls, self.start_error, self.remove_error = [], start_error, remove_error
        self.attrs = {"State": {"OOMKilled": False}}

    def start(self):
        self.calls.append("start")
        if self.start_error:
            raise self.start_error
        self.status = "running"

    def reload(self):
        pass

    def kill(self):
        self.calls.append("kill")
        self.status = "exited"

    def remove(self, force=False, v=False):
        self.calls.append(f"remove(force={force},v={v})")
        if self.remove_error:
            raise self.remove_error

    def die(self, oom=False):
        self.status = "exited"
        self.attrs = {"State": {"OOMKilled": oom}}


class FakeClient:
    def __init__(self, spec, api, container=None):
        self.container = container or FakeContainer()
        self.created = []
        outer = self
        self.api = api
        api.inspect = good_inspect(spec)

        class Containers:
            def create(self, image, command=None, **kw):
                outer.created.append({"image": image, "command": command, **kw})
                return outer.container

            def run(self, *a, **k):
                raise AssertionError("containers.run must never be used")

        self.containers = Containers()


def rig(tmp_path, seed, *, agent=None, handler_wrap=None, owner=(1000, 1000), **spec_over):
    spec = spec_for(seed, **spec_over)
    fs = FakeContainerFS(tmp_path / "fs", agent=agent, owner=owner)
    handler = fs.handler if handler_wrap is None else handler_wrap(fs)
    api = FakeExecApi(handler)
    client = FakeClient(spec, api)
    return spec, fs, api, client


def go(spec, client, stopped=lambda: None, **kw):
    return run_agent(client, spec, stopped, poll_interval=0.02, **kw)


def edit_agent(**kw):
    def act(task, wd):
        (wd / "a.py").write_text((wd / "a.py").read_text() + "# agent fix\n")
        (wd / "new_file.py").write_text("x = 1\n")
        return b'{"type":"result","subtype":"success"}\n'
    return FakeAgent(act=act, **kw)


# ---------------- creation ----------------
def test_create_kwargs_follow_policy_keepalive_and_carry_no_real_key_or_task(seed):
    kw = build_create_kwargs(spec_for(seed))
    env = kw["environment"]
    assert kw["command"] == AGENT_KEEPALIVE_COMMAND and kw["read_only"] is True and kw["cap_drop"] == ["ALL"]
    assert "ANTHROPIC_API_KEY" not in env and env["ANTHROPIC_AUTH_TOKEN"] == "per-run-token"
    assert {v for k, v in env.items() if "MODEL" in k} == {MODEL}
    assert set(kw["tmpfs"]) == {"/testbed", "/opt/aiep", "/tmp", "/home/nonroot"}
    assert "exec" in kw["tmpfs"]["/testbed"] and "exec" in kw["tmpfs"]["/opt/aiep"]
    assert "uid=1000,gid=1000" in kw["tmpfs"]["/testbed"] and "nosuid" in kw["tmpfs"]["/tmp"]
    blob = json.dumps(kw)
    assert REAL_KEY not in blob and "ISSUE-TEXT-CANARY" not in blob
    for forbidden in ("volumes", "mounts", "ports", "privileged", "network_mode", "devices", "cap_add", "dns"):
        assert forbidden not in kw


@pytest.mark.parametrize("over", [{"tmp_size": "2g"}, {"testbed_size": "900m"}, {"home_size": "2g"}])
def test_unsafe_specs_are_refused_before_docker(seed, over):
    with pytest.raises(PolicyViolation):
        build_create_kwargs(spec_for(seed, **over))


def test_spec_repr_hides_the_task_token_and_archives(seed):
    r = repr(spec_for(seed))
    assert "ISSUE-TEXT-CANARY" not in r and "per-run-token" not in r and "tar=" not in r
    assert REAL_KEY not in r  # forbidden_secrets holds the real API key: it must not appear in a log line or traceback


@pytest.mark.parametrize("over", [{"task_text": ""}, {"task_text": "  \n "}, {"task_text": "x" * (AR.MAX_TASK_BYTES + 1)}])
def test_invalid_task_refused_before_any_docker_call(tmp_path, seed, over):
    spec, fs, api, client = rig(tmp_path, seed, **over)
    with pytest.raises(ValueError):
        go(spec, client)
    assert client.created == [] and api.calls == []


def test_seed_without_head_or_with_a_tampered_tar_is_refused_before_docker(tmp_path, seed):
    spec, fs, api, client = rig(tmp_path, seed)
    good_head = seed.manifest.head
    seed.manifest.head = None
    with pytest.raises(ValueError, match="HEAD"):
        go(spec, client)
    seed.manifest.head = good_head
    original = seed.tar
    seed.tar = original + b"\0" * 512
    with pytest.raises(Exception, match="SHA-256"):
        go(spec, client)
    seed.tar = original
    assert client.created == []


# ---------------- the happy path ----------------
def test_full_lifecycle_extracts_the_agents_changes_against_the_seed_head(tmp_path, seed, repo):
    _, base_commit, head = repo
    agent = edit_agent()
    spec, fs, api, client = rig(tmp_path, seed, agent=agent)
    res = go(spec, client)

    assert (res.status, res.valid, res.claude_exit_code, res.stop_reason) == (STATUS_EXITED, True, 0, None)
    assert res.patch.status == "ok" and res.patch_error is None
    assert "a/a.py" in res.patch.patch and "+# agent fix" in res.patch.patch and "b/new_file.py" in res.patch.patch
    # the synthetic image commit is NOT part of the agent's patch (it would be if we diffed against base_commit)
    # (it would appear as an ADDED line; as unchanged hunk context it is just the file's existing text)
    assert "+# synthetic image change" not in res.patch.patch and "-# synthetic image change" not in res.patch.patch
    assert res.seed_head == head and res.seed_sha256 == seed.manifest.archive_sha256
    assert res.components_sha256 == COMPONENTS.manifest.archive_sha256
    assert res.removed and res.survivors_after_quiesce == [] and res.cleanup_error is None
    assert client.container.calls == ["start", "kill", "remove(force=True,v=True)"]
    assert set(res.timeline) >= {"running", "injected", "claude_started", "claude_ended", "quiesced", "extracted"}
    assert b"success" in res.claude_stdout


def test_git_runs_in_the_container_and_diffs_against_the_seeds_head_never_the_dataset_base(tmp_path, seed, repo):
    _, base_commit, head = repo
    spec, fs, api, client = rig(tmp_path, seed, agent=edit_agent())
    go(spec, client)
    git_calls = [c["cmd"] for c in api.calls if c["cmd"][0] == "/usr/bin/git"]
    assert [c[len(HARDENED_GIT) + 1] for c in git_calls] == ["cat-file", "add", "diff"]
    diff = next(c for c in git_calls if "diff" in c)
    assert head in diff and base_commit not in diff
    assert all(base_commit not in json.dumps(c["cmd"]) for c in api.calls)
    assert all(c[1:1 + len(HARDENED_GIT)] == HARDENED_GIT for c in git_calls)
    assert all(c["environment"] == {**experiment_env(IDENT), **CE._GIT_VARS} for c in api.calls if c["cmd"][0] == "/usr/bin/git")
    assert all(c["user"] == "1000:1000" and c["privileged"] is False for c in api.calls)


def test_spec_has_no_dataset_base_commit_field():
    import dataclasses

    assert "base_commit" not in {f.name for f in dataclasses.fields(AgentRunSpec)}


def test_the_task_travels_on_stdin_only(tmp_path, seed):
    agent = edit_agent()
    spec, fs, api, client = rig(tmp_path, seed, agent=agent)
    res = go(spec, client)
    assert agent.seen_stdin == [TASK.encode()]
    assert agent.seen_argv == [["/opt/aiep/claude", "-p", "--output-format", "stream-json"]]
    # nowhere else: not in create kwargs, exec argv, exec environment, inspect, or the result
    blob = json.dumps(client.created) + json.dumps(api.calls) + json.dumps(api.inspect) + repr(res)
    assert "ISSUE-TEXT-CANARY" not in blob
    with_task = [i for i, s in enumerate(api.stdins) if TASK.encode() in s]
    claude_calls = [i for i, c in enumerate(api.calls) if c["cmd"][0] == "/opt/aiep/claude"]
    assert with_task == claude_calls and len(with_task) == 1


def test_injection_order_and_destinations(tmp_path, seed):
    spec, fs, api, client = rig(tmp_path, seed, agent=edit_agent())
    go(spec, client)
    names = [c["cmd"][0] for c in api.calls]
    assert names[:6] == ["/bin/cat", "/bin/sh", "/bin/tar", "/opt/miniconda3/bin/python", "/bin/tar", "/opt/miniconda3/bin/python"]
    assert api.calls[2]["cmd"][-1] == "/testbed" and api.calls[4]["cmd"][-1] == "/opt/aiep"
    assert names.index("/opt/aiep/claude") == 6  # the agent starts only after the identity check and both injections verified
    assert api.stdins[2] == seed.tar and api.stdins[4] == COMPONENTS.tar


# ---------------- seed verification failures ----------------
def test_tampered_extraction_makes_the_run_invalid_and_never_starts_the_agent(tmp_path, seed):
    def wrap(fs):
        def handler(argv, rec, stdin):
            res = fs.handler(argv, rec, stdin)
            if argv[0] == "/bin/tar" and argv[-1] == "/testbed":
                (fs.path("/testbed") / "keep.txt").write_text("TAMPERED\n")
            return res
        return handler

    agent = edit_agent()
    spec, fs, api, client = rig(tmp_path, seed, agent=agent, handler_wrap=wrap)
    res = go(spec, client)
    assert res.status == STATUS_INVALID and res.valid is False and "/testbed does not match its manifest" in res.invalid_reason
    assert "keep.txt" in res.invalid_reason and agent.seen_stdin == [] and res.patch is None and res.removed


def test_tampered_components_also_invalidate_the_run(tmp_path, seed):
    def wrap(fs):
        def handler(argv, rec, stdin):
            res = fs.handler(argv, rec, stdin)
            if argv[0] == "/bin/tar" and argv[-1] == "/opt/aiep":
                (fs.path("/opt/aiep") / "claude").write_bytes(b"#!evil\n")
            return res
        return handler

    agent = edit_agent()
    spec, fs, api, client = rig(tmp_path, seed, agent=agent, handler_wrap=wrap)
    res = go(spec, client)
    assert res.valid is False and "/opt/aiep" in res.invalid_reason and agent.seen_stdin == []


# ---------------- limits and stop conditions ----------------
def test_wall_clock_timeout_quiesces_then_still_extracts_the_partial_work(tmp_path, seed):
    hang = threading.Event()
    agent = FakeAgent(before=lambda task, wd: (wd / "partial.py").write_text("p = 1\n"), hang=hang)
    spec, fs, api, client = rig(tmp_path, seed, agent=agent, wall_clock_s=0.3)
    res = go(spec, client)
    hang.set()
    assert res.status == STATUS_TIMEOUT and res.valid is True
    assert ["/bin/sh", "-c", CE.QUIESCE_SCRIPT] in [c["cmd"] for c in api.calls]
    assert res.patch.status == "ok" and "b/partial.py" in res.patch.patch and res.removed
    kinds = [c["cmd"][0] for c in api.calls]
    assert kinds.index("/bin/sh") < kinds.index("/usr/bin/git")  # quiesce strictly before extraction


def test_budget_exhaustion_is_a_valid_outcome_with_a_patch(tmp_path, seed):
    hang = threading.Event()
    agent = FakeAgent(before=lambda task, wd: (wd / "b.py").write_text("b = 1\n"), hang=hang)
    state = {"n": 0}

    def stopped():
        state["n"] += 1
        return "budget_exhausted" if state["n"] > 3 else None

    spec, fs, api, client = rig(tmp_path, seed, agent=agent)
    res = go(spec, client, stopped=stopped)
    hang.set()
    assert (res.status, res.stop_reason, res.valid) == (STATUS_GATEWAY_STOPPED, "budget_exhausted", True)
    assert res.patch.status == "ok" and "b/b.py" in res.patch.patch


@pytest.mark.parametrize("reason", ["model_violation", "tool_violation", "metering_failure"])
def test_one_model_violations_invalidate_the_run_and_nothing_is_extracted(tmp_path, seed, reason):
    hang = threading.Event()
    agent = FakeAgent(before=lambda task, wd: (wd / "x.py").write_text("x\n"), hang=hang)
    spec, fs, api, client = rig(tmp_path, seed, agent=agent)
    res = go(spec, client, stopped=lambda: reason)
    hang.set()
    assert (res.status, res.valid, res.stop_reason) == (STATUS_GATEWAY_STOPPED, False, reason)
    assert reason in res.invalid_reason and res.patch is None and res.removed
    assert not any(c["cmd"][0] == "/usr/bin/git" for c in api.calls)  # no extraction at all
    assert not any(c["cmd"][:2] == ["/bin/sh", "-c"] and c["cmd"][2] == CE.QUIESCE_SCRIPT for c in api.calls)


def test_container_death_is_recorded_and_the_patch_is_reported_lost(tmp_path, seed):
    hang = threading.Event()
    agent = FakeAgent(hang=hang)
    spec, fs, api, client = rig(tmp_path, seed, agent=agent)
    threading.Timer(0.15, lambda: client.container.die(oom=True)).start()
    res = go(spec, client)
    hang.set()
    assert res.status == STATUS_CONTAINER_DIED and res.oom_killed is True and res.valid is True
    assert res.patch is None and "tmpfs workspace is gone" in res.patch_error and res.removed


def test_survivors_after_quiesce_are_retried_once_and_reported(tmp_path, seed):
    calls = {"list": 0}

    def wrap(fs):
        def handler(argv, rec, stdin):
            if argv[:2] == ["/bin/sh", "-c"] and argv[2] == CE.LIST_PIDS_SCRIPT:
                calls["list"] += 1
                return ExecResult(stdout=b"1\n42\n")
            return fs.handler(argv, rec, stdin)
        return handler

    spec, fs, api, client = rig(tmp_path, seed, agent=edit_agent(), handler_wrap=wrap)
    res = go(spec, client)
    assert calls["list"] == 2 and res.survivors_after_quiesce == ["42"]
    assert res.patch.status == "ok"  # extraction still happens; the run is flagged, not discarded


# ---------------- extraction outcomes are recorded, not raised ----------------
def test_empty_patch_is_reported_as_empty(tmp_path, seed):
    spec, fs, api, client = rig(tmp_path, seed, agent=FakeAgent())
    res = go(spec, client)
    assert res.valid and res.patch.status == "empty" and res.patch_error is None


def test_binary_or_unsafe_changes_are_rejected_not_passed_on(tmp_path, seed):
    agent = FakeAgent(act=lambda t, wd: ((wd / "blob.bin").write_bytes(bytes(range(256)) * 8), b"")[1])
    spec, fs, api, client = rig(tmp_path, seed, agent=agent)
    res = go(spec, client)
    assert res.patch.status == "rejected" and any("binary" in r for r in res.patch.reasons) and res.patch.patch is None


def test_generated_bytecode_and_caches_never_enter_the_patch(tmp_path, seed):
    def act(t, wd):
        (wd / "__pycache__").mkdir()
        (wd / "__pycache__" / "a.cpython-39.pyc").write_bytes(b"\x00\x01binary")
        (wd / ".pytest_cache").mkdir()
        (wd / ".pytest_cache" / "x").write_text("c")
        (wd / "real.py").write_text("ok = 1\n")
        return b""

    spec, fs, api, client = rig(tmp_path, seed, agent=FakeAgent(act=act))
    res = go(spec, client)
    assert res.patch.status == "ok" and "b/real.py" in res.patch.patch
    assert "__pycache__" not in res.patch.patch and ".pytest_cache" not in res.patch.patch


def test_a_failing_git_is_recorded_as_patch_error(tmp_path, seed):
    def wrap(fs):
        def handler(argv, rec, stdin):
            if argv[0] == "/usr/bin/git" and "diff" in argv:
                return ExecResult(stderr=b"fatal: boom", exit_code=128)
            return fs.handler(argv, rec, stdin)
        return handler

    spec, fs, api, client = rig(tmp_path, seed, agent=edit_agent(), handler_wrap=wrap)
    res = go(spec, client)
    assert res.patch is None and "git in the agent container failed" in res.patch_error and res.valid


def test_oversized_diff_output_is_cut_off_and_recorded(tmp_path, seed):
    agent = FakeAgent(act=lambda t, wd: ((wd / "big.txt").write_text("line\n" * 400_000), b"")[1])
    spec, fs, api, client = rig(tmp_path, seed, agent=agent, max_patch_bytes=2000)
    res = go(spec, client)
    # the diff is larger than the cap: either truncated output or a rejected patch, never a silent success
    assert res.patch is None or res.patch.status == "rejected"


def test_missing_seed_commit_means_no_patch(tmp_path, seed):
    def wrap(fs):
        def handler(argv, rec, stdin):
            if argv[0] == "/usr/bin/git" and "cat-file" in argv:
                return ExecResult(stderr=b"fatal: Not a valid object name", exit_code=128)
            return fs.handler(argv, rec, stdin)
        return handler

    spec, fs, api, client = rig(tmp_path, seed, agent=edit_agent(), handler_wrap=wrap)
    res = go(spec, client)
    assert res.patch is None and "HEAD commit is missing" in res.patch_error
    assert not any("diff" in c["cmd"] for c in api.calls)


# ---------------- policy and cleanup on every failure path ----------------
@pytest.mark.parametrize("claude_args", [("-p", REAL_KEY), ("-p", "A" * 600), ("-p", TASK)])
def test_claude_arguments_cannot_carry_secrets_or_the_task_and_the_container_is_still_removed(tmp_path, seed, claude_args):
    spec, fs, api, client = rig(tmp_path, seed, agent=edit_agent(), claude_args=claude_args)
    with pytest.raises(PolicyViolation):
        go(spec, client)
    assert client.container.calls[-1] == "remove(force=True,v=True)"
    assert not any(c["cmd"][0] == "/opt/aiep/claude" for c in api.calls)


def test_inspect_mismatch_blocks_start_and_removes(tmp_path, seed):
    spec, fs, api, client = rig(tmp_path, seed)
    api.inspect["HostConfig"]["NetworkMode"] = "bridge"
    with pytest.raises(PolicyViolation):
        go(spec, client)
    assert "start" not in client.container.calls and client.container.calls[-1] == "remove(force=True,v=True)"
    assert api.calls == []


def test_a_real_key_canary_in_inspect_blocks_start(tmp_path, seed):
    spec, fs, api, client = rig(tmp_path, seed)
    api.inspect["Config"]["Env"].append(f"X={REAL_KEY}")
    with pytest.raises(PolicyViolation):
        go(spec, client)
    assert "start" not in client.container.calls


def test_start_failure_removes_the_container(tmp_path, seed):
    spec = spec_for(seed)
    api = FakeExecApi(FakeContainerFS(tmp_path / "fs").handler)
    client = FakeClient(spec, api, FakeContainer(start_error=RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        go(spec, client)
    assert client.container.calls[-1] == "remove(force=True,v=True)"


def test_unexpected_exec_failure_still_tears_down(tmp_path, seed):
    def boom(*a, **k):
        raise RuntimeError("daemon went away")

    spec, fs, api, client = rig(tmp_path, seed)
    real_create = api.exec_create
    api.exec_create = lambda container, cmd=None, **k: real_create(container, cmd, **k) if cmd[0] == "/bin/cat" else boom()  # the route-table read works
    with pytest.raises(RuntimeError, match="daemon went away"):
        go(spec, client)
    assert client.container.calls[-1] == "remove(force=True,v=True)"


def test_remove_failure_is_reported_not_swallowed(tmp_path, seed):
    spec = spec_for(seed)
    fs = FakeContainerFS(tmp_path / "fs", agent=edit_agent())
    client = FakeClient(spec, FakeExecApi(fs.handler), FakeContainer(remove_error=RuntimeError("daemon busy")))
    res = go(spec, client)
    assert res.removed is False and "daemon busy" in res.cleanup_error


def test_container_that_never_runs_is_an_error_and_removed(tmp_path, seed):
    spec = spec_for(seed)
    c = FakeContainer()
    c.start = lambda: c.calls.append("start")  # stays "created"
    client = FakeClient(spec, FakeExecApi(FakeContainerFS(tmp_path / "fs").handler), c)
    with pytest.raises(RuntimeError, match="did not reach the running state"):
        run_agent(client, spec, lambda: None, poll_interval=0.01, start_timeout_s=0.1)
    assert c.calls[-1] == "remove(force=True,v=True)"


def test_runner_source_has_no_put_archive_run_subprocess_or_env_task_delivery():
    import ast

    text = Path(AR.__file__).read_text()
    doc = ast.get_docstring(ast.parse(text)) or ""
    code = text.replace(doc, "")  # the docstring explains why put_archive is NOT used; only code is checked
    for needle in ("put_archive", "containers.run", "subprocess", "get_archive", "network_mode", "privileged",
                   "docker.sock", "volumes"):
        assert needle not in code


def test_run_record_is_json_safe_and_holds_hashes_but_no_task_output_or_patch_body(tmp_path, seed):
    spec, fs, api, client = rig(tmp_path, seed, agent=edit_agent())
    res = go(spec, client)
    rec = AR.record_of(res)
    text = json.dumps(rec)
    assert "ISSUE-TEXT-CANARY" not in text and "agent fix" not in text and "success" not in text
    assert rec["seed"] == {"head": seed.head, "archive_sha256": seed.manifest.archive_sha256}
    assert rec["patch"]["status"] == "ok" and len(rec["patch"]["sha256"]) == 64
    assert rec["components_sha256"] == COMPONENTS.manifest.archive_sha256 and rec["removed"] is True


def test_production_extraction_uses_the_container_runner_never_the_host_runner():
    import ast

    text = Path(AR.__file__).read_text()
    code = text.replace(ast.get_docstring(ast.parse(text)) or "", "")
    assert "container_git_runner" in code and "trusted_local_git_runner" not in code
    src = Path(AR.__file__).parent
    assert sorted(p.name for p in src.glob("*.py") if "trusted_local_git_runner" in p.read_text()) == ["patch.py"]


# ---------------- explicit interpreter ----------------
@pytest.mark.parametrize("bad", ["", "python3", "/usr/bin/../bin/python3", "/tmp/python", "/opt/aiep/python", "/testbed/py"])
def test_an_unsafe_or_missing_interpreter_is_refused_before_any_docker_call(tmp_path, seed, bad):
    spec, fs, api, client = rig(tmp_path, seed, python=bad)
    with pytest.raises(PolicyViolation):
        go(spec, client)
    assert client.created == [] and api.calls == []


def test_agent_spec_has_no_default_interpreter():
    import dataclasses
    f = {f.name: f for f in dataclasses.fields(AgentRunSpec)}["python"]
    assert f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING


def test_verification_uses_exactly_the_configured_interpreter(tmp_path, seed):
    spec, fs, api, client = rig(tmp_path, seed, python="/opt/other/bin/python", agent=edit_agent())
    fs.python = "/opt/other/bin/python"
    go(spec, client)
    pythons = {c["cmd"][0] for c in api.calls if "-I" in c["cmd"]}
    assert pythons == {"/opt/other/bin/python"}
    assert not any(c["cmd"][0] == "/usr/bin/python3" for c in api.calls)


def test_a_wrong_interpreter_fails_the_run_without_falling_back_or_starting_the_agent(tmp_path, seed):
    agent = edit_agent()
    spec, fs, api, client = rig(tmp_path, seed, python="/usr/bin/python3", agent=agent)  # the image has only miniconda
    with pytest.raises(AR.ExecError, match="exit code 127"):
        go(spec, client)
    names = [c["cmd"][0] for c in api.calls]
    assert names == ["/bin/cat", "/bin/sh", "/bin/tar", "/usr/bin/python3"]  # one attempt, no retry with another interpreter
    assert agent.seen_stdin == [] and client.container.calls[-1] == "remove(force=True,v=True)"


# ---------------- identity probe inside the container ----------------
@pytest.mark.parametrize("account, needle", [
    (None, "no passwd entry"),
    (("nonroot", 1001, 1000, "/home/nonroot"), "uid is"),
    (("nonroot", 1000, 1001, "/home/nonroot"), "gid is"),
    (("someone", 1000, 1000, "/home/nonroot"), "account name"),
    (("nonroot", 1000, 1000, "/home/agent"), "passwd home"),
])
def test_a_missing_or_different_account_makes_the_run_invalid_before_injection(tmp_path, seed, account, needle):
    agent = edit_agent()
    spec = spec_for(seed)
    fs = FakeContainerFS(tmp_path / "fs", agent=agent, account=account)
    api = FakeExecApi(fs.handler)
    client = FakeClient(spec, api)
    res = go(spec, client)
    assert res.status == STATUS_INVALID and not res.valid and needle in res.invalid_reason
    assert [c["cmd"][0] for c in api.calls] == ["/bin/cat", "/bin/sh"]  # nothing injected, nothing run
    assert agent.seen_stdin == [] and res.patch is None and res.removed


def test_the_probe_runs_as_the_identity_with_the_experiment_environment(tmp_path, seed):
    spec, fs, api, client = rig(tmp_path, seed, agent=edit_agent())
    go(spec, client)
    assert api.calls[0]["cmd"] == ["/bin/cat", "/proc/net/route"] and api.calls[0]["user"] == "1000:1000"  # the attachment check reads the route table first
    probe = api.calls[1]
    assert probe["cmd"][:2] == ["/bin/sh", "-c"] and probe["user"] == "1000:1000"
    assert probe["environment"] == experiment_env(IDENT)


# ---------------- Config A and Config B cannot receive different environments ----------------
def _run(tmp_path, seed, label, task, claude_args):
    spec, fs, api, client = rig(tmp_path / label, seed, agent=edit_agent(), config_label=label,
                                task_text=task, claude_args=claude_args)
    res = go(spec, client)
    return spec, api, client, res


def test_config_a_and_b_receive_identical_environments_for_every_exec_and_for_create(tmp_path, seed):
    a_spec, a_api, a_client, a_res = _run(tmp_path, seed, "A", "ISSUE-TEXT-CANARY: fix it.\n", ("-p", "--output-format", "stream-json"))
    b_spec, b_api, b_client, b_res = _run(tmp_path, seed, "B", "ISSUE-TEXT-CANARY: fix it, then run the tests and iterate.\n",
                                          ("-p", "--output-format", "stream-json", "--append-system-prompt", "iterate on test failures"))
    assert a_res.valid and b_res.valid and (a_res.config_label, b_res.config_label) == ("A", "B")
    # container creation: everything but the run-independent name is identical
    ka, kb = a_client.created[0], b_client.created[0]
    assert ka == kb
    # every exec: same binary order, same user, workdir and environment; only Claude's argv and stdin may differ
    shape = lambda api: [(c["cmd"][0], c["user"], c["workdir"], c["environment"]) for c in api.calls]
    assert shape(a_api) == shape(b_api)
    claude_envs = {json.dumps(c["environment"], sort_keys=True) for api in (a_api, b_api) for c in api.calls
                   if c["cmd"][0] == "/opt/aiep/claude"}
    assert claude_envs == {json.dumps(experiment_env(IDENT), sort_keys=True)}
    # the experiment path is really in effect for Claude: the task's own conda environment comes first
    assert experiment_env(IDENT)["PATH"].startswith("/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/condabin:/opt/miniconda3/bin:")


def test_the_configuration_label_is_validated_and_never_reaches_any_docker_request(tmp_path, seed):
    spec, fs, api, client = rig(tmp_path, seed, config_label="C")
    with pytest.raises(ValueError, match="config_label"):
        go(spec, client)
    assert client.created == [] and api.calls == []
    spec, fs, api, client = rig(tmp_path / "b", seed, agent=edit_agent(), config_label="B")
    go(spec, client)
    blob = json.dumps(client.created) + json.dumps(api.calls)
    assert '"aiep.config' not in blob and "config_label" not in blob


def test_agent_spec_requires_identity_and_configuration_without_defaults():
    import dataclasses
    fields = {f.name: f for f in dataclasses.fields(AgentRunSpec)}
    for name in ("identity", "config_label", "python"):
        assert fields[name].default is dataclasses.MISSING
    assert "user" not in fields and "uid" not in fields  # derived from the identity only


# ---------------- /dev/shm and IPC mode: explicit, private, pinned ----------------
PREVIOUS_CREATE_KEYS = {"cap_drop", "command", "detach", "environment", "image", "labels", "mem_limit", "memswap_limit", "name",
                        "nano_cpus", "network", "pids_limit", "read_only", "security_opt", "tmpfs", "user", "working_dir"}
PREVIOUS_TMPFS = {  # exactly what the runner produced before /dev/shm was made explicit (identity 1000:1000, default sizes)
    "/testbed": "rw,exec,nosuid,nodev,size=192m,uid=1000,gid=1000,mode=0755",
    "/opt/aiep": "rw,exec,nosuid,nodev,size=416m,uid=1000,gid=1000,mode=0755",
    "/tmp": "rw,nosuid,nodev,size=128m,uid=1000,gid=1000,mode=0700",
    "/home/nonroot": "rw,nosuid,nodev,size=128m,uid=1000,gid=1000,mode=0700",
}


def test_default_spec_pins_private_ipc_and_64_mib_and_changes_nothing_else_in_the_create_kwargs(seed):
    spec = spec_for(seed)
    assert (spec.shm_size, spec.ipc_mode) == ("64m", "private")
    kw = build_create_kwargs(spec)
    assert kw["shm_size"] == "64m" and kw["ipc_mode"] == "private"
    assert set(kw) - {"shm_size", "ipc_mode"} == PREVIOUS_CREATE_KEYS  # the two new keys are the only additions
    assert kw["tmpfs"] == PREVIOUS_TMPFS  # the four tmpfs mounts, /tmp still 0700
    assert kw["read_only"] is True and kw["cap_drop"] == ["ALL"] and kw["security_opt"] == ["no-new-privileges"]
    assert "cap_add" not in kw and "privileged" not in kw and "volumes" not in kw


def test_an_explicit_valid_configuration_runs_the_whole_fake_lifecycle(tmp_path, seed):
    spec, fs, api, client = rig(tmp_path, seed, agent=edit_agent(), shm_size="32m")
    assert spec.shm_size == "32m" and api.inspect["HostConfig"]["ShmSize"] == 32 * 1024**2
    res = go(spec, client)
    assert res.valid and res.status == STATUS_EXITED and res.removed
    created = client.created[0]
    assert created["shm_size"] == "32m" and created["ipc_mode"] == "private" and created["tmpfs"]["/tmp"].endswith("mode=0700")


class _NoDocker:
    """A client that fails loudly if the runner so much as touches it."""

    def __getattr__(self, name):
        raise AssertionError(f"Docker was used ({name}) although the spec is invalid")


@pytest.mark.parametrize("over", [
    {"shm_size": "0"}, {"shm_size": ""}, {"shm_size": "64mb"}, {"shm_size": "-1m"}, {"shm_size": "1.5g"}, {"shm_size": "64m\n"},
    {"shm_size": "513m"}, {"shm_size": "1g"}, {"shm_size": None}, {"shm_size": 67108864}, {"shm_size": True},
    {"ipc_mode": "host"}, {"ipc_mode": "shareable"}, {"ipc_mode": "container:abc"}, {"ipc_mode": "none"}, {"ipc_mode": ""},
    {"ipc_mode": None}, {"ipc_mode": "PRIVATE"},
])
def test_invalid_shm_or_ipc_specs_are_refused_before_any_docker_call(seed, over):
    spec = spec_for(seed, **over)
    with pytest.raises(PolicyViolation):
        build_create_kwargs(spec)
    with pytest.raises(PolicyViolation):
        run_agent(_NoDocker(), spec, lambda: None)  # run_agent refuses before it creates anything


@pytest.mark.parametrize("field, value", [
    ("IpcMode", "host"), ("IpcMode", "shareable"), ("IpcMode", "container:abc"), ("IpcMode", "none"), ("IpcMode", ""),
    ("IpcMode", None), ("ShmSize", 0), ("ShmSize", 64 * 1024**2 + 1), ("ShmSize", 128 * 1024**2), ("ShmSize", None),
    ("ShmSize", "67108864"), ("ShmSize", True),
])
def test_wrong_inspect_shm_or_ipc_blocks_start_and_the_container_is_removed(tmp_path, seed, field, value):
    spec, fs, api, client = rig(tmp_path, seed)
    api.inspect["HostConfig"][field] = value
    with pytest.raises(PolicyViolation):
        go(spec, client)
    assert "start" not in client.container.calls and client.container.calls[-1] == "remove(force=True,v=True)"
    assert api.calls == []  # nothing ran in the container


@pytest.mark.parametrize("field", ["IpcMode", "ShmSize"])
def test_missing_inspect_shm_or_ipc_blocks_start_and_the_container_is_removed(tmp_path, seed, field):
    spec, fs, api, client = rig(tmp_path, seed)
    del api.inspect["HostConfig"][field]
    with pytest.raises(PolicyViolation):
        go(spec, client)
    assert "start" not in client.container.calls and client.container.calls[-1] == "remove(force=True,v=True)"
    assert api.calls == []


def test_the_configured_size_not_a_constant_is_what_inspect_is_compared_with(tmp_path, seed):
    spec, fs, api, client = rig(tmp_path, seed, shm_size="32m")
    api.inspect["HostConfig"]["ShmSize"] = 64 * 1024**2  # Docker's default instead of the configured 32 MiB
    with pytest.raises(PolicyViolation, match="ShmSize"):
        go(spec, client)
    assert "start" not in client.container.calls


# ---------------- ownership of the injected trees is verified (regression: run_agent used to skip the owner check) ----------------
def test_the_identity_the_tests_use_is_1000_1000():
    assert (IDENT.uid, IDENT.gid) == (1000, 1000)


@pytest.mark.parametrize("owner", [(0, 0), (1000, 1001), (1001, 1000), None])  # None: the listing reports the real file owner
def test_wrong_owner_of_the_seed_invalidates_the_run_before_the_agent_exists(tmp_path, seed, owner):
    agent = edit_agent()
    spec, fs, api, client = rig(tmp_path, seed, agent=agent, owner=owner)
    res = go(spec, client)
    assert res.valid is False and res.status == STATUS_INVALID and "owner differs" in res.invalid_reason
    assert "/testbed" in res.invalid_reason and agent.seen_stdin == [] and res.patch is None and res.removed


def _rewrite_listing(rewrite, dest):
    def wrap(fs):
        def handler(argv, rec, stdin):
            res = fs.handler(argv, rec, stdin)
            if VERIFY_SCRIPT in argv and argv[-1] == dest:
                res.stdout = json.dumps(rewrite(json.loads(res.stdout))).encode()
            return res
        return handler
    return wrap


def test_wrong_owner_of_the_components_also_invalidates_the_run(tmp_path, seed):
    agent = edit_agent()
    wrap = _rewrite_listing(lambda rows: [[*r[:6], 0, 0] for r in rows], "/opt/aiep")
    spec, fs, api, client = rig(tmp_path, seed, agent=agent, handler_wrap=wrap)
    res = go(spec, client)
    assert res.valid is False and "/opt/aiep" in res.invalid_reason and "owner differs" in res.invalid_reason
    assert agent.seen_stdin == []


def test_a_listing_that_reports_no_owners_cannot_skip_the_check(tmp_path, seed):
    agent = edit_agent()
    wrap = _rewrite_listing(lambda rows: [r[:6] for r in rows], "/testbed")
    spec, fs, api, client = rig(tmp_path, seed, agent=agent, handler_wrap=wrap)
    res = go(spec, client)
    assert res.valid is False and "owner not reported" in res.invalid_reason and agent.seen_stdin == []


def test_the_expected_owner_comes_from_the_verified_identity_not_a_constant(tmp_path, seed):
    other = AgentIdentity(name="nonroot", uid=1234, gid=1234, home="/home/nonroot")
    spec = spec_for(seed, identity=other)
    fs = FakeContainerFS(tmp_path / "fs", agent=edit_agent(), account=("nonroot", 1234, 1234, "/home/nonroot"), owner=(1000, 1000))
    client = FakeClient(spec, FakeExecApi(fs.handler))
    res = go(spec, client)
    assert res.valid is False and "owner differs" in res.invalid_reason and "1234:1234" in res.invalid_reason


def test_correct_ownership_passes_and_the_agent_runs(tmp_path, seed):
    agent = edit_agent()
    spec, fs, api, client = rig(tmp_path, seed, agent=agent)
    res = go(spec, client)
    assert res.valid is True and res.status == STATUS_EXITED and len(agent.seen_stdin) == 1


# ---------------- component sizing, recomputed against the policy constants ----------------
def test_component_sizing_arithmetic_fits_the_policy_caps():
    from aiep.container_exec import MAX_STDIN_BYTES
    from aiep.container_policy import (AGENT_MEM_BYTES, TMPFS_MAX_EACH_BYTES, TMPFS_MAX_TOTAL_BYTES, _parse_size,
                                       validate_shm_size)
    MiB = 1024**2
    sizes = {"testbed": "192m", "components": AR.COMPONENTS_TMPFS_SIZE, "tmp": "128m", "home": "128m"}
    parsed = {k: _parse_size(v) for k, v in sizes.items()}
    shm = validate_shm_size("64m")
    assert AR.MAX_COMPONENT_FILE_BYTES == 400 * MiB and parsed["components"] == 416 * MiB
    assert parsed["components"] >= AR.MAX_COMPONENT_FILE_BYTES + 16 * MiB  # ceiling plus 16 MiB slack
    assert parsed["components"] <= TMPFS_MAX_EACH_BYTES
    assert sum(parsed.values()) + shm == 928 * MiB <= TMPFS_MAX_TOTAL_BYTES == 1024 * MiB
    assert AGENT_MEM_BYTES - (sum(parsed.values()) + shm) >= 1024 * MiB  # a completely full tmpfs still leaves >= 1 GiB for processes
    assert AR.MAX_COMPONENT_FILE_BYTES + 1024 * 1024 < MAX_STDIN_BYTES  # the injection tar fits the exec stdin cap with room for tar headers


def test_the_default_spec_uses_the_computed_components_size_and_passes_policy(seed):
    spec = spec_for(seed)
    assert spec.components_size == AR.COMPONENTS_TMPFS_SIZE == "416m"
    kw = build_create_kwargs(spec)  # PolicyViolation if the aggregate or any single mount were over the cap
    assert "size=416m" in kw["tmpfs"]["/opt/aiep"]


def test_the_claude_pin_ceiling_equals_the_runners_component_ceiling():
    cfg = json.loads((Path(__file__).resolve().parents[1] / "config" / "claude_cli.json").read_text())
    assert cfg["max_binary_bytes"] == AR.MAX_COMPONENT_FILE_BYTES


def test_a_larger_components_tmpfs_is_still_capped_by_the_policy(seed):
    for size in ("513m", "600m"):
        with pytest.raises(PolicyViolation):
            build_create_kwargs(spec_for(seed, components_size=size))
    with pytest.raises(PolicyViolation):  # 192 + 512 + 128 + 128 + 64 = 1024 is allowed; one more MiB is not
        build_create_kwargs(spec_for(seed, components_size="512m", tmp_size="129m"))


# ---------------- the central wall-clock ceiling (the reaper's safety margin depends on it) ----------------
@pytest.mark.parametrize("secs", [0, -1, 3600.5, 7200, 1e9, True])
def test_a_wall_clock_outside_the_central_limits_is_refused_before_any_docker_call(tmp_path, seed, secs):
    spec, fs, api, client = rig(tmp_path, seed, wall_clock_s=secs)
    with pytest.raises(ValueError, match="wall_clock_s"):
        go(spec, client)
    assert client.created == [] and api.calls == []


def test_the_default_wall_clock_is_the_central_default(seed):
    from aiep.limits import DEFAULT_WALL_CLOCK_S
    assert AgentRunSpec.__dataclass_fields__["wall_clock_s"].default == DEFAULT_WALL_CLOCK_S == 1800.0


# ---------------- Claude Code's max_tokens must fit the gateway cap (measured: it asks for 128000 by default) ----------------
def test_the_agent_is_pinned_to_the_gateway_max_tokens_cap_identically_for_both_configs(seed):
    from aiep import gateway as GW
    from aiep.limits import AGENT_MAX_OUTPUT_TOKENS
    a, b = build_create_kwargs(spec_for(seed, config_label="A")), build_create_kwargs(spec_for(seed, config_label="B"))
    assert a["environment"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == str(AGENT_MAX_OUTPUT_TOKENS) == b["environment"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"]
    assert AGENT_MAX_OUTPUT_TOKENS <= GW.MAX_TOKENS_CAP  # every request Claude can make passes the gateway's cap
    assert a["environment"] == b["environment"]


@pytest.mark.parametrize("value", ["128000", "64001", "0", "-1", "abc", "", "64000 ", "1e5"])
def test_any_other_output_token_value_is_refused_by_the_policy(seed, value):
    from aiep.agent_runner import agent_context
    from aiep.container_policy import _check_agent_env
    spec = spec_for(seed)
    env = dict(build_create_kwargs(spec)["environment"])
    _check_agent_env(env, agent_context(spec))  # the real environment passes
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = value
    with pytest.raises(PolicyViolation, match="MAX_OUTPUT_TOKENS"):
        _check_agent_env(env, agent_context(spec))


# ---------------- audit sprint: truncated Claude output is a censored run, never a graded one ----------------
def _big_output_agent(n):
    def act(task, wd):
        (wd / "a.py").write_text((wd / "a.py").read_text() + "# agent fix\n")  # a perfectly good patch exists
        return b"x" * n
    return FakeAgent(act=act)


def test_output_over_the_cap_makes_the_run_invalid_with_no_patch_extraction(tmp_path, seed, monkeypatch):
    monkeypatch.setattr(AR, "MAX_CLAUDE_OUTPUT", 2000)
    spec, fs, api, client = rig(tmp_path, seed, agent=_big_output_agent(5000))
    res = go(spec, client)
    assert res.status == "exited" and res.claude_exit_code is not None  # the process itself ended normally...
    assert res.claude_output_truncated is True
    assert res.valid is False and "censored" in res.invalid_reason and "truncated" in res.invalid_reason  # ...but the run is censored
    assert res.patch is None and res.patch_error is None  # extraction never ran: nothing can reach grading
    assert "quiesced" not in res.timeline and "extracted" not in res.timeline
    assert res.removed is True  # the container is still removed
    rec = AR.record_of(res)
    assert rec["claude_output_truncated"] is True and rec["valid"] is False and "censored" in rec["invalid_reason"]  # kept in run.json
    assert len(res.claude_stdout) <= 2000 + 65536  # what was read is kept as evidence (bounded)


def test_output_at_or_under_the_cap_is_graded_normally(tmp_path, seed, monkeypatch):
    monkeypatch.setattr(AR, "MAX_CLAUDE_OUTPUT", 2000)
    spec, fs, api, client = rig(tmp_path, seed, agent=_big_output_agent(1000))
    res = go(spec, client)
    assert res.claude_output_truncated is False and res.valid is True and res.invalid_reason is None
    assert res.patch is not None and res.patch.status == "ok"


def test_truncation_is_invalid_even_when_the_gateway_stopped_the_run_for_a_valid_reason(tmp_path, seed, monkeypatch):
    # budget_exhausted is a VALID stop; combined with censored output the run still must not be graded
    monkeypatch.setattr(AR, "MAX_CLAUDE_OUTPUT", 2000)
    spec, fs, api, client = rig(tmp_path, seed, agent=_big_output_agent(5000))
    res = go(spec, client, stopped=lambda: "budget_exhausted")
    assert res.status in ("gateway_stopped", "exited")
    if res.claude_output_truncated:
        assert res.valid is False and res.patch is None


def test_a_truncated_run_cannot_reach_prediction_writing(tmp_path, seed, monkeypatch):
    # m2_run writes predictions only when agent.valid and patch.status == "ok": both are false for a truncated run
    monkeypatch.setattr(AR, "MAX_CLAUDE_OUTPUT", 2000)
    spec, fs, api, client = rig(tmp_path, seed, agent=_big_output_agent(5000))
    res = go(spec, client)
    assert not (res.valid and res.patch is not None and res.patch.status == "ok")


# ---------------- audit sprint: runtime pins (output cap, retries, timeouts) ----------------
def test_the_claude_runtime_pins_are_identical_for_a_and_b_and_cover_retries_and_timeouts(seed):
    from aiep.limits import AGENT_RUNTIME_PINS
    a, b = build_create_kwargs(spec_for(seed, config_label="A")), build_create_kwargs(spec_for(seed, config_label="B"))
    for k, v in AGENT_RUNTIME_PINS.items():
        assert a["environment"][k] == b["environment"][k] == v
    assert set(AGENT_RUNTIME_PINS) == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS", "CLAUDE_CODE_MAX_RETRIES", "API_TIMEOUT_MS",
                                       "BASH_DEFAULT_TIMEOUT_MS", "BASH_MAX_TIMEOUT_MS"}
    assert a["environment"] == b["environment"]


@pytest.mark.parametrize("key,bad", [("CLAUDE_CODE_MAX_RETRIES", "0"), ("CLAUDE_CODE_MAX_RETRIES", "99"), ("API_TIMEOUT_MS", "1"),
                                     ("BASH_DEFAULT_TIMEOUT_MS", "999999"), ("BASH_MAX_TIMEOUT_MS", "1")])
def test_a_different_runtime_pin_value_is_refused(seed, key, bad):
    from aiep.agent_runner import agent_context
    from aiep.container_policy import _check_agent_env
    spec = spec_for(seed)
    env = dict(build_create_kwargs(spec)["environment"])
    env[key] = bad
    with pytest.raises(PolicyViolation, match=key):
        _check_agent_env(env, agent_context(spec))


@pytest.mark.parametrize("key", ["CLAUDE_CODE_MAX_OUTPUT_TOKENS", "CLAUDE_CODE_MAX_RETRIES", "API_TIMEOUT_MS", "BASH_DEFAULT_TIMEOUT_MS",
                                 "BASH_MAX_TIMEOUT_MS"])
def test_an_omitted_runtime_pin_is_refused_it_cannot_fall_back_to_the_binarys_default(seed, key):
    from aiep.agent_runner import agent_context
    from aiep.container_policy import _check_agent_env
    spec = spec_for(seed)
    env = dict(build_create_kwargs(spec)["environment"])
    del env[key]
    with pytest.raises(PolicyViolation, match=key):
        _check_agent_env(env, agent_context(spec))


def test_verify_inspect_checks_the_created_containers_own_environment_for_every_pin(seed):
    from aiep.agent_runner import agent_context
    from aiep.container_policy import verify_inspect
    spec = spec_for(seed)
    good = good_inspect(spec)
    verify_inspect("agent", good, agent_context(spec))
    for key in AGENT_RUNTIME_PINS:
        broken = json.loads(json.dumps(good))
        broken["Config"]["Env"] = [e for e in broken["Config"]["Env"] if not e.startswith(key + "=")]
        with pytest.raises(PolicyViolation, match=key):
            verify_inspect("agent", broken, agent_context(spec))
        changed = json.loads(json.dumps(good))
        changed["Config"]["Env"] = [e if not e.startswith(key + "=") else f"{key}=1" for e in changed["Config"]["Env"]]
        with pytest.raises(PolicyViolation, match=key):
            verify_inspect("agent", changed, agent_context(spec))


def test_the_output_token_pin_is_validated_against_the_gateways_configured_cap(seed):
    import dataclasses
    from aiep import gateway as GW
    from aiep.agent_runner import agent_context
    from aiep.container_policy import verify_inspect
    from aiep.limits import AGENT_MAX_OUTPUT_TOKENS, GATEWAY_MAX_TOKENS_CAP
    assert GATEWAY_MAX_TOKENS_CAP == GW.MAX_TOKENS_CAP  # the orchestrator's copy of the cap is the gateway's own constant
    spec = spec_for(seed)
    ctx = agent_context(spec)
    assert ctx.gateway_max_tokens_cap == GW.MAX_TOKENS_CAP >= AGENT_MAX_OUTPUT_TOKENS
    with pytest.raises(PolicyViolation, match="exceeds the gateway's max_tokens cap"):
        dataclasses.replace(ctx, gateway_max_tokens_cap=AGENT_MAX_OUTPUT_TOKENS - 1)  # a lower gateway cap would refuse every request
    verify_inspect("agent", good_inspect(spec), ctx)

import json

import pytest

from aiep.agent_runner import (STATUS_EXITED, STATUS_GATEWAY_STOPPED, STATUS_TIMEOUT, AgentRunSpec,
                               build_create_kwargs, run_agent)
from aiep.container_policy import PolicyViolation

MODEL = "claude-sonnet-5-5"
REAL_KEY = "sk-ant-REAL-KEY-CANARY"
GB2 = 2 * 1024**3


def good_inspect(**host_over):
    host = {"Privileged": False, "CapAdd": None, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"],
            "NetworkMode": "aiep-agent-net", "Memory": GB2, "PidsLimit": 512, "NanoCpus": 2_000_000_000,
            "ReadonlyRootfs": True, "Binds": None}
    host.update(host_over)
    return {"HostConfig": host, "Mounts": [], "Config": {"User": "agent", "Env": ["ANTHROPIC_AUTH_TOKEN=tok"]}}


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeContainer:
    id = "abc123"

    def __init__(self, clock, runs_for=None, exit_code=0, logs=b"log", remove_error=None, start_error=None):
        self.clock, self.runs_for, self.exit_code, self._logs = clock, runs_for, exit_code, logs
        self.remove_error, self.start_error = remove_error, start_error
        self.started_at = None
        self.killed = self.removed = False
        self.status = "created"
        self.attrs = {"State": {}}
        self.calls = []

    def start(self):
        self.calls.append("start")
        if self.start_error:
            raise self.start_error
        self.started_at = self.clock.t
        self.status = "running"

    def reload(self):
        if self.killed:
            self.status, self.attrs = "exited", {"State": {"ExitCode": 137}}
        elif self.started_at is not None and self.runs_for is not None and self.clock.t - self.started_at >= self.runs_for:
            self.status, self.attrs = "exited", {"State": {"ExitCode": self.exit_code}}

    def kill(self):
        self.calls.append("kill")
        self.killed = True

    def logs(self, **kw):
        return self._logs

    def remove(self, force=False, v=False):
        self.calls.append("remove")
        if self.remove_error:
            raise self.remove_error
        self.removed = True


class FakeClient:
    def __init__(self, container, inspect=None):
        self.container = container
        self.created = []
        outer = self

        class Containers:
            def create(self, image, command=None, **kw):
                outer.created.append({"image": image, "command": command, **kw})
                return outer.container

            def run(self, *a, **k):
                raise AssertionError("containers.run must never be used")

        class Api:
            def inspect_container(self, _id):
                return inspect or good_inspect()

        self.containers, self.api = Containers(), Api()


def spec(**over):
    d = dict(run_id="r1", image="agent:1", command=["claude", "-p", "do the thing"],
             gateway_url="http://gateway:8080", agent_network="aiep-agent-net", pinned_model=MODEL,
             run_token="per-run-token", wall_clock_s=100.0, forbidden_secrets=(REAL_KEY,))
    d.update(over)
    return AgentRunSpec(**d)


def run(container, *, stopped=lambda: None, inspect=None, **spec_over):
    clock = FakeClock()
    container.clock = clock
    client = FakeClient(container, inspect)
    res = run_agent(client, spec(**spec_over), stopped, clock=clock, sleep=clock.sleep, poll_interval=1.0)
    return res, client


# ---------------- kwargs ----------------
def test_kwargs_follow_policy_and_carry_no_real_key():
    kw = build_create_kwargs(spec())
    env = kw["environment"]
    assert "ANTHROPIC_API_KEY" not in env and env["ANTHROPIC_BASE_URL"] == "http://gateway:8080"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "per-run-token"
    models = {v for k, v in env.items() if "MODEL" in k}
    assert models == {MODEL}  # one model, including background/subagent overrides
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1" and env["DISABLE_UPDATES"] == "1"
    assert kw["cap_drop"] == ["ALL"] and kw["read_only"] is True and kw["network"] == "aiep-agent-net"
    for forbidden in ("volumes", "mounts", "ports", "privileged", "network_mode", "devices", "cap_add", "dns"):
        assert forbidden not in kw
    assert REAL_KEY not in json.dumps(kw)


def test_real_key_in_any_field_is_refused_before_docker():
    with pytest.raises(PolicyViolation):
        build_create_kwargs(spec(command=["sh", "-c", f"echo {REAL_KEY}"]))
    with pytest.raises(PolicyViolation):
        build_create_kwargs(spec(run_token=REAL_KEY))


@pytest.mark.parametrize("user", ["root", "0", "0:0", ""])
def test_agent_must_not_run_as_root(user):
    with pytest.raises(PolicyViolation):
        build_create_kwargs(spec(user=user))


def test_spec_has_no_free_form_environment_or_mount_fields():
    import dataclasses

    names = {f.name for f in dataclasses.fields(AgentRunSpec)}
    assert not names & {"environment", "env", "volumes", "mounts", "ports", "network_mode", "privileged"}


@pytest.mark.parametrize("cmd", ["claude -p x", [], ["ok", 3], None])
def test_command_must_be_a_list_of_strings(cmd):
    with pytest.raises(ValueError):
        build_create_kwargs(spec(command=cmd))


# ---------------- supervision ----------------
def test_normal_exit_is_reported_and_container_removed():
    c = FakeContainer(None, runs_for=5, exit_code=0, logs=b"hello")
    res, client = run(c)
    assert (res.status, res.exit_code, res.removed, res.stop_reason) == (STATUS_EXITED, 0, True, None)
    assert res.logs == b"hello" and 5 <= res.duration_s < 7
    assert c.calls == ["start", "remove"] and not c.killed
    assert len(client.created) == 1 and client.created[0]["image"] == "agent:1"


def test_nonzero_exit_code_is_reported_not_hidden():
    res, _ = run(FakeContainer(None, runs_for=1, exit_code=3))
    assert res.status == STATUS_EXITED and res.exit_code == 3


def test_wall_clock_timeout_kills_and_removes_externally():
    c = FakeContainer(None, runs_for=None)  # never exits by itself
    res, _ = run(c, wall_clock_s=30.0)
    assert res.status == STATUS_TIMEOUT and c.killed and c.removed and res.removed
    assert 30 <= res.duration_s < 32


def test_gateway_stop_ends_the_run_with_its_reason():
    state = {"n": 0}

    def stopped():
        state["n"] += 1
        return "model_violation" if state["n"] > 3 else None

    c = FakeContainer(None, runs_for=None)
    res, _ = run(c, stopped=stopped)
    assert res.status == STATUS_GATEWAY_STOPPED and res.stop_reason == "model_violation"
    assert c.killed and c.removed


def test_exit_wins_when_both_exited_and_stopped_are_true():
    c = FakeContainer(None, runs_for=0, exit_code=0)
    res, _ = run(c, stopped=lambda: "budget_exhausted")
    assert res.status == STATUS_EXITED and res.exit_code == 0


def test_policy_failure_on_inspect_removes_without_starting():
    c = FakeContainer(None, runs_for=1)
    with pytest.raises(PolicyViolation):
        run(c, inspect=good_inspect(NetworkMode="bridge"))
    assert "start" not in c.calls and c.removed


def test_inspect_showing_a_real_key_canary_blocks_start():
    bad = good_inspect()
    bad["Config"]["Env"].append(f"X={REAL_KEY}")
    c = FakeContainer(None, runs_for=1)
    with pytest.raises(PolicyViolation):
        run(c, inspect=bad)
    assert "start" not in c.calls and c.removed


def test_start_failure_removes_container_and_reraises():
    c = FakeContainer(None, runs_for=1, start_error=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        run(c)
    assert c.removed


def test_supervision_exception_still_tears_down():
    c = FakeContainer(None, runs_for=None)

    def stopped():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run(c, stopped=stopped)
    assert c.removed


def test_remove_failure_is_reported_not_swallowed():
    c = FakeContainer(None, runs_for=1, remove_error=RuntimeError("daemon busy"))
    res, _ = run(c)
    assert res.removed is False and "daemon busy" in res.cleanup_error


def test_logs_are_capped_to_the_tail():
    c = FakeContainer(None, runs_for=1, logs=b"a" * 100 + b"END")
    clock = FakeClock()
    c.clock = clock
    res = run_agent(FakeClient(c), spec(), lambda: None, clock=clock, sleep=clock.sleep, max_log_bytes=10)
    assert res.logs.endswith(b"END") and len(res.logs) == 10


def test_runner_source_never_uses_containers_run_or_host_access():
    from pathlib import Path
    import aiep.agent_runner as mod

    src = Path(mod.__file__).read_text()
    for needle in ("containers.run", "network_mode", "privileged", "volumes", "docker.sock", "subprocess"):
        assert needle not in src


def test_runner_enforces_the_expected_agent_image_id():
    good = "sha256:" + "a" * 64
    ok = good_inspect()
    ok["Image"] = good
    res, _ = run(FakeContainer(None, runs_for=1), inspect=ok, image_id=good)
    assert res.status == STATUS_EXITED
    other = good_inspect()
    other["Image"] = "sha256:" + "b" * 64
    c = FakeContainer(None, runs_for=1)
    with pytest.raises(PolicyViolation, match="expected image"):
        run(c, inspect=other, image_id=good)
    assert "start" not in c.calls and c.removed


@pytest.mark.parametrize("tmpfs", [{"/etc": "rw,nosuid,nodev,size=1m"}, {"/tmp": "size=64m"}, {"/tmp": "rw,nosuid,nodev,exec,size=1m"}])
def test_runner_refuses_unsafe_tmpfs_before_docker(tmpfs):
    with pytest.raises(PolicyViolation):
        build_create_kwargs(spec(tmpfs=tmpfs))

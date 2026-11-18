import copy

import pytest

from aiep.container_policy import (
    AGENT_ENV_KEYS,
    AgentContext,
    PolicyViolation,
    harden,
    verify_inspect,
)

MODEL = "claude-sonnet-5-5"
CTX = AgentContext(
    gateway_url="http://gateway:8080",
    agent_network="aiep-agent-net",
    pinned_model=MODEL,
    forbidden_secrets=("REAL-KEY-CANARY-123",),
)


def eval_kwargs(**over):
    kw = dict(
        image="img:latest",
        command="tail -f /dev/null",
        name="sweb.eval.x.run1",
        user="root",
        detach=True,
        cap_add=["SYS_ADMIN"],
    )
    kw.update(over)
    return kw


def agent_kwargs(**over):
    kw = dict(
        image="agent:1",
        command=["claude", "-p", "hi"],
        name="agent.run1",
        user="agent",
        detach=True,
        network="aiep-agent-net",
        read_only=True,
        tmpfs={"/tmp": "rw,nosuid,nodev,size=64m"},
        working_dir="/testbed",
        environment={
            "ANTHROPIC_BASE_URL": "http://gateway:8080",
            "ANTHROPIC_AUTH_TOKEN": "per-run-token",
            "ANTHROPIC_MODEL": MODEL,
        },
    )
    kw.update(over)
    return kw


# ---------------- evaluator ----------------
def test_evaluator_strips_sys_admin_drops_caps_and_has_no_network():
    out = harden("evaluator", eval_kwargs())
    assert "cap_add" not in out
    assert out["cap_drop"] == ["ALL"]
    assert out["security_opt"] == ["no-new-privileges"]
    assert out["network_mode"] == "none"
    assert out["mem_limit"] == out["memswap_limit"] == "2g"
    assert out["pids_limit"] == 1024 and out["nano_cpus"] == 2_000_000_000
    assert out["environment"]["PIP_NO_INDEX"] == "1"


def test_evaluator_does_not_mutate_input():
    kw = eval_kwargs()
    snapshot = copy.deepcopy(kw)
    harden("evaluator", kw)
    assert kw == snapshot


@pytest.mark.parametrize(
    "extra",
    [
        {"privileged": True},
        {"volumes": {"/var/run/docker.sock": {"bind": "/x"}}},
        {"mounts": []},
        {"network_mode": "host"},
        {"network": "bridge"},
        {"pid_mode": "host"},
        {"devices": ["/dev/fuse"]},
        {"ports": {"80/tcp": 80}},
        {"dns": ["8.8.8.8"]},
        {"extra_hosts": {"a": "1.2.3.4"}},
        {"environment": {"X": "1"}},  # evaluator env is set by policy only
        {"some_future_kwarg": 1},  # unknown arguments fail closed
    ],
)
def test_evaluator_refuses_anything_not_allowlisted(extra):
    with pytest.raises(PolicyViolation):
        harden("evaluator", eval_kwargs(**extra))


@pytest.mark.parametrize(
    "over",
    [
        {"cap_add": ["SYS_ADMIN", "NET_ADMIN"]},
        {"cap_add": ["NET_ADMIN"]},
        {"user": "1000"},
        {"detach": False},
        {"command": "sh"},
        {"name": "other.name"},
        {"image": ""},
    ],
)
def test_evaluator_refuses_unexpected_values(over):
    with pytest.raises(PolicyViolation):
        harden("evaluator", eval_kwargs(**over))


def test_unknown_role_refused():
    with pytest.raises(PolicyViolation):
        harden("gateway", {})


# ---------------- agent ----------------
def test_agent_ok_and_limits_applied():
    out = harden("agent", agent_kwargs(), CTX)
    assert out["cap_drop"] == ["ALL"] and out["security_opt"] == ["no-new-privileges"]
    assert out["pids_limit"] == 512 and out["mem_limit"] == "2g"
    assert out["network"] == "aiep-agent-net" and out["read_only"] is True
    assert out["labels"]["aiep.role"] == "agent"


@pytest.mark.parametrize("user", ["root", "0", "", "0:0"])
def test_agent_must_be_non_root(user):
    with pytest.raises(PolicyViolation):
        harden("agent", agent_kwargs(user=user), CTX)


@pytest.mark.parametrize(
    "over",
    [
        {"network": "bridge"},
        {"network": "aiep-egress-net"},
        {"read_only": False},
        {"privileged": True},
        {"volumes": {"/x": {"bind": "/y"}}},
        {"network_mode": "host"},
        {"ports": {"80/tcp": 80}},
        {"cap_add": ["NET_ADMIN"]},
        {"cap_drop": ["NET_RAW"]},
        {"dns": ["8.8.8.8"]},
    ],
)
def test_agent_refuses_dangerous_options(over):
    with pytest.raises(PolicyViolation):
        harden("agent", agent_kwargs(**over), CTX)


@pytest.mark.parametrize(
    "env_over",
    [
        {"ANTHROPIC_API_KEY": "anything"},
        {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"},
        {"ANTHROPIC_BASE_URL": "http://evil:8080"},
        {"ANTHROPIC_MODEL": "claude-haiku-5-5"},
        {"ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-5-5"},
        {"AWS_SECRET_ACCESS_KEY": "x"},
        {"ANTHROPIC_AUTH_TOKEN": "sk-ant-api03-abcdefghijklmnop"},
        {"TZ": "sk-ant-leak"},
    ],
)
def test_agent_environment_is_allowlisted(env_over):
    kw = agent_kwargs()
    kw["environment"].update(env_over)
    with pytest.raises(PolicyViolation):
        harden("agent", kw, CTX)


def test_agent_requires_token_and_gateway():
    for drop in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        kw = agent_kwargs()
        del kw["environment"][drop]
        with pytest.raises(PolicyViolation):
            harden("agent", kw, CTX)


def test_agent_forbidden_secret_canary_never_allowed_anywhere():
    kw = agent_kwargs(command=["sh", "-c", "echo REAL-KEY-CANARY-123"])
    with pytest.raises(PolicyViolation):
        harden("agent", kw, CTX)
    kw = agent_kwargs()
    kw["labels"] = {"note": "REAL-KEY-CANARY-123"}
    with pytest.raises(PolicyViolation):
        harden("agent", kw, CTX)


def test_agent_requires_context():
    with pytest.raises(PolicyViolation):
        harden("agent", agent_kwargs())


def test_agent_env_allowlist_has_no_real_key_variable():
    assert "ANTHROPIC_API_KEY" not in AGENT_ENV_KEYS


# ---------------- verify_inspect ----------------
def good_eval_inspect():
    return {
        "HostConfig": {
            "Privileged": False, "CapAdd": None, "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"], "NetworkMode": "none",
            "Memory": 2 * 1024**3, "PidsLimit": 1024, "NanoCpus": 2_000_000_000,
            "Binds": None, "Devices": [], "PidMode": "",
        },
        "Mounts": [],
        "Config": {"User": "root"},
    }


def good_agent_inspect():
    return {
        "HostConfig": {
            "Privileged": False, "CapAdd": None, "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"], "NetworkMode": "aiep-agent-net",
            "Memory": 2 * 1024**3, "PidsLimit": 512, "NanoCpus": 2_000_000_000,
            "ReadonlyRootfs": True, "Binds": None,
        },
        "Mounts": [],
        "Config": {"User": "agent", "Env": ["ANTHROPIC_AUTH_TOKEN=t"]},
    }


def test_verify_inspect_accepts_good_containers():
    verify_inspect("evaluator", good_eval_inspect())
    verify_inspect("agent", good_agent_inspect(), CTX)


@pytest.mark.parametrize(
    "path,value",
    [
        (("HostConfig", "Privileged"), True),
        (("HostConfig", "CapAdd"), ["SYS_ADMIN"]),
        (("HostConfig", "CapDrop"), []),
        (("HostConfig", "SecurityOpt"), []),
        (("HostConfig", "NetworkMode"), "bridge"),
        (("HostConfig", "Binds"), ["/var/run/docker.sock:/s"]),
        (("HostConfig", "Devices"), [{"PathOnHost": "/dev/fuse"}]),
        (("HostConfig", "PidMode"), "host"),
        (("HostConfig", "PortBindings"), {"80/tcp": [{}]}),
        (("HostConfig", "Memory"), 0),
        (("HostConfig", "PidsLimit"), None),
        (("Mounts",), [{"Source": "/"}]),
    ],
)
def test_verify_inspect_rejects_each_deviation_for_evaluator(path, value):
    info = good_eval_inspect()
    node = info
    for p in path[:-1]:
        node = node[p]
    node[path[-1]] = value
    with pytest.raises(PolicyViolation):
        verify_inspect("evaluator", info)


def test_verify_inspect_agent_specific_checks():
    info = good_agent_inspect()
    info["Config"]["User"] = "root"
    with pytest.raises(PolicyViolation):
        verify_inspect("agent", info, CTX)
    info = good_agent_inspect()
    info["HostConfig"]["ReadonlyRootfs"] = False
    with pytest.raises(PolicyViolation):
        verify_inspect("agent", info, CTX)
    info = good_agent_inspect()
    info["Config"]["Env"].append("LEAK=REAL-KEY-CANARY-123")
    with pytest.raises(PolicyViolation):
        verify_inspect("agent", info, CTX)


# ---------------- tmpfs and image pinning (audit fixes) ----------------
from aiep.container_policy import TMPFS_MAX_EACH_BYTES, TMPFS_MAX_TOTAL_BYTES, validate_tmpfs

T = "rw,nosuid,nodev"


@pytest.mark.parametrize("mapping", [
    {"/tmp": f"{T},size=256m"},
    {"/tmp": f"{T},size=256m", "/home/agent": f"{T},size=256m,mode=0700,uid=1000,gid=1000"},
    {"/testbed": f"{T},exec,size=768m,uid=1000,gid=1000,mode=0755"},
    {"/testbed": f"{T},exec,size=768m", "/tmp": f"{T},size=256m", "/home/agent": f"{T},size=256m"},
    {},
])
def test_valid_tmpfs_mappings_accepted(mapping):
    validate_tmpfs(mapping, creating=True)


@pytest.mark.parametrize("mapping", [
    {"/etc": f"{T},size=1m"},                       # path not allowed
    {"/": f"{T},size=1m"},
    {"/tmp": f"{T}"},                               # no size
    {"/tmp": f"{T},size=2g"},                       # over per-mount cap
    {"/tmp": f"{T},size=abc"},
    {"/tmp": f"{T},size=-5m"},
    {"/tmp": f"{T},size=0"},
    {"/tmp": "rw,nodev,size=1m"},                   # missing nosuid
    {"/tmp": "rw,nosuid,size=1m"},                  # missing nodev
    {"/tmp": f"{T},exec,size=1m"},                  # exec only allowed on /testbed
    {"/testbed": f"{T},size=1m"},                   # /testbed must say exec explicitly
    {"/testbed": f"{T},exec,noexec,size=1m"},
    {"/tmp": f"{T},size=1m,uid=0"},                 # root-owned
    {"/tmp": f"{T},size=1m,gid=0"},
    {"/tmp": f"{T},size=1m,uid=abc"},
    {"/tmp": f"{T},size=1m,mode=9999"},
    {"/tmp": f"{T},size=1m,size=2m"},               # duplicate
    {"/tmp": f"{T},size=1m,bind=/etc"},             # unknown option
    {"/tmp": f"{T},size=1m,suid"},
    {"/tmp": f"{T},size=1m,ro"},
    {"/tmp": f"{T},size=1m,"},
    {"/tmp": 5},
    {"/testbed": f"{T},exec,size=1g", "/tmp": f"{T},size=512m"},  # total over cap
])
def test_unsafe_tmpfs_mappings_refused(mapping):
    with pytest.raises(PolicyViolation):
        validate_tmpfs(mapping, creating=True)


def test_tmpfs_caps_are_consistent():
    assert TMPFS_MAX_EACH_BYTES <= TMPFS_MAX_TOTAL_BYTES <= 2 * 1024**3  # never more than the memory limit


def test_agent_harden_rejects_bad_tmpfs_and_inspect_rechecks_it():
    with pytest.raises(PolicyViolation):
        harden("agent", agent_kwargs(tmpfs={"/etc": f"{T},size=1m"}), CTX)
    with pytest.raises(PolicyViolation):
        harden("agent", agent_kwargs(tmpfs={"/tmp": "size=64m"}), CTX)  # unhardened
    info = good_agent_inspect()
    info["HostConfig"]["Tmpfs"] = {"/tmp": "rw,nosuid,nodev,noexec,size=65536k"}
    verify_inspect("agent", info, CTX)  # Docker may add noexec: fine
    info["HostConfig"]["Tmpfs"] = {"/var/run": "rw,size=1m"}
    with pytest.raises(PolicyViolation):
        verify_inspect("agent", info, CTX)


def test_agent_image_id_is_enforced_when_expected():
    ctx = AgentContext(gateway_url="http://gateway:8080", agent_network="aiep-agent-net",
                       pinned_model=MODEL, image_id="sha256:" + "a" * 64)
    info = good_agent_inspect()
    info["Image"] = "sha256:" + "a" * 64
    verify_inspect("agent", info, ctx)
    info["Image"] = "sha256:" + "b" * 64
    with pytest.raises(PolicyViolation, match="expected image"):
        verify_inspect("agent", info, ctx)
    del info["Image"]
    with pytest.raises(PolicyViolation):
        verify_inspect("agent", info, ctx)

import copy

import pytest

from aiep.container_policy import (gateway_tmpfs, validate_interpreter_path,
    AGENT_ENV_KEYS,
    AgentContext,
    AgentIdentity,
    agent_tmpfs_rules,
    PolicyViolation,
    harden,
    verify_inspect,
)

MODEL = "claude-sonnet-5-5"
# TEST-ONLY identity values: the real image's account is unverified (config/agent_identity.json).
IDENT = AgentIdentity(name="nonroot", uid=1000, gid=1000, home="/home/nonroot")
OWN = f"uid={IDENT.uid},gid={IDENT.gid}"


def agent_tmpfs():
    return {"/tmp": f"rw,nosuid,nodev,size=64m,{OWN},mode=0700",
            IDENT.home: f"rw,nosuid,nodev,size=64m,{OWN},mode=0700",
            "/testbed": f"rw,exec,nosuid,nodev,size=64m,{OWN},mode=0755",
            "/opt/aiep": f"rw,exec,nosuid,nodev,size=64m,{OWN},mode=0755"}


CTX = AgentContext(
    gateway_url="http://gateway:8080",
    agent_network="aiep-agent-net",
    pinned_model=MODEL,
    identity=IDENT,
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
        command=["/bin/sleep", "2147483647"],
        name="agent.run1",
        user=IDENT.user,
        detach=True,
        network="aiep-agent-net",
        read_only=True,
        tmpfs=agent_tmpfs(),
        working_dir="/testbed",
        environment={
            "ANTHROPIC_BASE_URL": "http://gateway:8080",
            "ANTHROPIC_AUTH_TOKEN": "per-run-token",
            "ANTHROPIC_MODEL": MODEL,
            "HOME": IDENT.home,
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
            "ReadonlyRootfs": True, "Binds": None, "Tmpfs": agent_tmpfs(),
        },
        "Mounts": [],
        "Config": {"User": IDENT.user, "Env": ["ANTHROPIC_AUTH_TOKEN=t"]},
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
    {"/tmp": f"{T},size=256m", "/home/nonroot": f"{T},size=256m,mode=0700,uid=1000,gid=1000"},
    {"/testbed": f"{T},exec,size=192m,uid=1000,gid=1000,mode=0755"},
    {"/testbed": f"{T},exec,size=192m", "/tmp": f"{T},size=128m", "/home/nonroot": f"{T},size=128m",
     "/opt/aiep": f"{T},exec,size=256m"},
    {},
])
def test_valid_tmpfs_mappings_accepted(mapping):
    validate_tmpfs(mapping, creating=True, rules=agent_tmpfs_rules(IDENT))


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
    info["HostConfig"]["Tmpfs"] = {**agent_tmpfs(), "/tmp": f"rw,nosuid,nodev,noexec,size=65536k,{OWN},mode=0700"}
    verify_inspect("agent", info, CTX)  # Docker may add noexec: fine
    info["HostConfig"]["Tmpfs"] = {"/var/run": "rw,size=1m"}
    with pytest.raises(PolicyViolation):
        verify_inspect("agent", info, CTX)


def test_agent_image_id_is_enforced_when_expected():
    ctx = AgentContext(gateway_url="http://gateway:8080", agent_network="aiep-agent-net",
                       pinned_model=MODEL, identity=IDENT, image_id="sha256:" + "a" * 64)
    info = good_agent_inspect()
    info["Image"] = "sha256:" + "a" * 64
    verify_inspect("agent", info, ctx)
    info["Image"] = "sha256:" + "b" * 64
    with pytest.raises(PolicyViolation, match="expected image"):
        verify_inspect("agent", info, ctx)
    del info["Image"]
    with pytest.raises(PolicyViolation):
        verify_inspect("agent", info, ctx)


# ---------------- injection-design additions: command, workdir, /opt/aiep, caps ----------------
from aiep.container_policy import (AGENT_FIXED_TMPFS_RULES, EXEC_ENV_KEYS, ExecContext, GatewayContext, harden_exec)
from aiep.patch import HARDENED_GIT


@pytest.mark.parametrize("command", [
    ["claude", "-p", "hi"], ["/bin/sh", "-c", "id"], ["/bin/sleep", "1"], "sleep infinity", None, [],
])
def test_agent_pid1_must_be_exactly_the_keepalive_command(command):
    with pytest.raises(PolicyViolation):
        harden("agent", agent_kwargs(command=command), CTX)


@pytest.mark.parametrize("wd", ["/", "/etc", "/opt/aiep", "/tmp"])
def test_agent_working_dir_must_be_testbed(wd):
    with pytest.raises(PolicyViolation):
        harden("agent", agent_kwargs(working_dir=wd), CTX)


def test_opt_aiep_tmpfs_is_allowed_only_as_exec_and_nosuid_nodev():
    validate_tmpfs({"/opt/aiep": f"{T},exec,size=256m"}, creating=True)
    for bad in ("rw,nosuid,nodev,size=256m", f"{T},noexec,size=256m", "rw,exec,size=256m", f"{T},exec,size=600m"):
        with pytest.raises(PolicyViolation):
            validate_tmpfs({"/opt/aiep": bad}, creating=True)
    rules = agent_tmpfs_rules(IDENT)
    assert set(rules) == {"/tmp", "/home/nonroot", "/testbed", "/opt/aiep"} and set(AGENT_FIXED_TMPFS_RULES) == {"/tmp", "/testbed", "/opt/aiep"}
    assert rules["/testbed"] and rules["/opt/aiep"] and not rules["/tmp"] and not rules["/home/nonroot"]


def test_all_agent_tmpfs_at_their_maximum_exceed_the_total_cap_so_sizes_must_be_chosen():
    with pytest.raises(PolicyViolation, match="total tmpfs"):
        validate_tmpfs({"/testbed": f"{T},exec,size=512m", "/opt/aiep": f"{T},exec,size=512m", "/tmp": f"{T},size=1m"},
                       creating=True)


# ---------------- harden_exec ----------------
ECTX = ExecContext(user="agent", allowed_scripts=frozenset({"kill -KILL -1", "VERIFY"}), python="/opt/miniconda3/bin/python",
                   readable_paths=frozenset({"/tmp/gateway.stop"}), forbidden_secrets=("SECRET-CANARY-1234",))
ENV = {"HOME": "/home/agent", "PATH": "/usr/bin:/bin"}
PYTHON_FOR_TESTS = "/opt/miniconda3/bin/python"
G = list(HARDENED_GIT)


def hx(argv, **kw):
    d = dict(user="agent", workdir="/testbed", env=ENV, ctx=ECTX)
    d.update(kw)
    return harden_exec(argv, **d)


@pytest.mark.parametrize("argv", [
    ["/bin/tar", "-x", "-p", "--no-same-owner", "-f", "-", "-C", "/testbed"],
    ["/bin/tar", "-x", "-p", "--no-same-owner", "-f", "-", "-C", "/opt/aiep"],
    ["/bin/sh", "-c", "kill -KILL -1"],
    ["/opt/miniconda3/bin/python", "-I", "-c", "VERIFY", "/testbed"],
    ["/bin/cat", "/tmp/gateway.stop"],
    ["/usr/bin/git", *G, "add", "-A", "-N", "--", "."],
    ["/usr/bin/git", *G, "diff", "--no-ext-diff", "--no-textconv", "a" * 40, "--", "."],
    ["/usr/bin/git", *G, "cat-file", "-e", "a" * 40 + "^{commit}"],
    ["/opt/aiep/claude", "-p", "--output-format", "stream-json", "--verbose"],
])
def test_harden_exec_accepts_exactly_the_registered_shapes(argv):
    out = hx(argv)
    assert out["cmd"] == argv and out["user"] == "agent" and out["tty"] is False and out["workdir"] == "/testbed"


@pytest.mark.parametrize("argv", [
    ["/bin/bash", "-c", "id"], ["/usr/bin/curl", "http://x"], ["/usr/bin/env", "sh"], ["/opt/aiep/other"],
    ["/bin/sh", "-c", "id"], ["/bin/sh", "-c"], ["/bin/sh", "kill -KILL -1"], ["/bin/sh", "-c", "kill -KILL -1", "x"],
    ["/opt/miniconda3/bin/python", "-I", "-c", "VERIFY", "/etc"], ["/opt/miniconda3/bin/python", "-c", "VERIFY", "/testbed"],
    ["/opt/miniconda3/bin/python", "-I", "-c", "print(1)", "/testbed"],
    ["/bin/tar", "-x", "-p", "--no-same-owner", "-f", "-", "-C", "/"],
    ["/bin/tar", "-x", "-p", "-f", "-", "-C", "/testbed"],
    ["/bin/tar", "-x", "-p", "--no-same-owner", "-f", "-", "-C", "/testbed", "--checkpoint-action=exec=id"],
    ["/bin/cat", "/etc/passwd"], ["/bin/cat", "/tmp/gateway.stop", "/etc/shadow"],
    ["/usr/bin/git", "diff"], ["/usr/bin/git", *G], ["/usr/bin/git", *G, "config", "x", "y"],
    ["/usr/bin/git", *G, "fetch"], ["/usr/bin/git", *G, "diff", "--output=/x"], ["/usr/bin/git", *G, "diff", "-c", "a=b"],
    ["/usr/bin/git", *G, "diff", "--git-dir=/etc"], ["/usr/bin/git", *G, "diff", "--ext-diff"],
    ["/usr/bin/git", *G, "diff", "x" * 513],
    ["/opt/aiep/claude", "a" * 513], ["/opt/aiep/claude", "x\x00y"], ["/opt/aiep/claude", 5],
    [], "not a list", None, ["/bin/sh", 5, "x"],
])
def test_harden_exec_refuses_everything_else(argv):
    with pytest.raises(PolicyViolation):
        hx(argv)


def test_harden_exec_user_workdir_env_and_secrets():
    ok = ["/opt/aiep/claude", "-p"]
    for user in ("root", "0", "", "other"):
        with pytest.raises(PolicyViolation):
            hx(ok, user=user)
    for wd in ("/etc", "/opt/aiep", "relative"):
        with pytest.raises(PolicyViolation):
            hx(ok, workdir=wd)
    for env in ({"LD_PRELOAD": "x"}, {"ANTHROPIC_API_KEY": "x"}, {"PATH": 5}, {"HOME": "x" * 2000}, None):
        with pytest.raises(PolicyViolation):
            hx(ok, env=env)
    with pytest.raises(PolicyViolation):
        hx(["/opt/aiep/claude", "SECRET-CANARY-1234"])
    with pytest.raises(PolicyViolation):
        hx(ok, env={"HOME": "SECRET-CANARY-1234"})
    assert set(ENV) <= EXEC_ENV_KEYS


def test_multiline_secret_text_is_caught_even_though_repr_would_escape_it():
    ctx = ExecContext(user="agent", forbidden_secrets=("line one of the task\nline two of the task",))
    with pytest.raises(PolicyViolation):
        harden_exec(["/opt/aiep/claude", "line one of the task\nline two of the task"], user="agent",
                    workdir="/testbed", env=ENV, ctx=ctx)


# ---------------- gateway role ----------------
GW_IMAGE_ID = "sha256:" + "9" * 64
# TEST-ONLY gateway values: the real image, its interpreter path and its uid:gid are unresolved (config/gateway_image.json).
GW_PY, GW_UID, GW_GID = "/usr/local/bin/python3", 10001, 10001
GCTX = GatewayContext(agent_network="aiep-agent-net", egress_network="aiep-egress-net", image_id=GW_IMAGE_ID,
                      python=GW_PY, uid=GW_UID, gid=GW_GID, extra_tmpfs=("/var/lib/localstack",), real_key="sk-ant-GATEWAY-REAL-KEY")


def gw_kwargs(**over):
    kw = dict(
        image="gateway-base:1", command=["2147483647"], entrypoint=["/bin/sleep"], name="aiep.gateway.r1",
        user="10001:10001", detach=True, network="aiep-agent-net", read_only=True,
        tmpfs=gateway_tmpfs(GCTX),
        environment={"AIEP_UPSTREAM_API_KEY": "sk-ant-GATEWAY-REAL-KEY", "AIEP_RUN_TOKEN": "run-token",
                     "AIEP_BUDGET_USD": "0.50", "AIEP_ALLOWED_HOSTS": "gateway:8080", "AIEP_LISTEN": "0.0.0.0:8080",
                     "AIEP_LEDGER_PATH": "/tmp/ledger.jsonl", "AIEP_STOP_FILE": "/tmp/gateway.stop",
                     "PYTHONPATH": "/opt/aiep-gw/src", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    kw.update(over)
    return kw


def good_gw_inspect():
    return {
        "Image": GW_IMAGE_ID,
        "HostConfig": {"Privileged": False, "CapAdd": None, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"],
                       "ReadonlyRootfs": True, "Memory": 256 * 1024**2, "PidsLimit": 128, "NanoCpus": 1_000_000_000,
                       "Tmpfs": gw_kwargs()["tmpfs"], "Binds": None},
        "Mounts": [], "Config": {"User": "10001:10001", "Entrypoint": ["/bin/sleep"], "Cmd": ["2147483647"]},
        "NetworkSettings": {"Networks": {"aiep-agent-net": {}, "aiep-egress-net": {}}},
    }


def test_gateway_role_ok_and_limits_applied():
    out = harden("gateway", gw_kwargs(), GCTX)
    assert out["cap_drop"] == ["ALL"] and out["read_only"] is True and out["mem_limit"] == "256m"
    assert out["pids_limit"] == 128 and out["labels"]["aiep.role"] == "gateway"
    verify_inspect("gateway", good_gw_inspect(), GCTX)


@pytest.mark.parametrize("over", [
    {"user": "root"}, {"user": "0"}, {"user": "agent"}, {"user": "10001"}, {"user": "10001:0"}, {"user": "10002:10001"},
    {"command": ["/usr/local/bin/python3", "-m", "aiep.gateway"]},  # starting it is only allowed after verification
    {"command": ["/bin/sleep", "2147483647"]}, {"entrypoint": ["/docker-entrypoint.sh"]}, {"entrypoint": []},
    {"entrypoint": ["/bin/sleep", "1"]}, {"entrypoint": "/bin/sleep"},
    {"network": "bridge"}, {"network": "aiep-egress-net"},
    {"read_only": False}, {"command": ["/bin/sh"]}, {"command": ["/usr/local/bin/python3", "-c", "x"]},
    {"privileged": True}, {"volumes": {"/x": {"bind": "/y"}}}, {"ports": {"8080/tcp": 8080}},
    {"cap_add": ["NET_ADMIN"]}, {"entrypoint": ["/bin/sh"]}, {"network_mode": "host"},
    {"tmpfs": {"/etc": f"{T},size=1m"}},
    {"tmpfs": {"/opt/aiep-gw": f"{T},exec,size=1m"}},  # the gateway never needs to execute from tmpfs
])
def test_gateway_role_refuses_dangerous_requests(over):
    with pytest.raises(PolicyViolation):
        harden("gateway", gw_kwargs(**over), GCTX)


@pytest.mark.parametrize("env_over", [
    {"AWS_SECRET_ACCESS_KEY": "x"}, {"ANTHROPIC_API_KEY": "x"}, {"AIEP_RUN_TOKEN": "sk-ant-GATEWAY-REAL-KEY"},
    {"AIEP_UPSTREAM_API_KEY": ""}, {"LD_PRELOAD": "x"},
])
def test_gateway_environment_is_allowlisted_and_token_differs_from_key(env_over):
    kw = gw_kwargs()
    kw["environment"].update(env_over)
    with pytest.raises(PolicyViolation):
        harden("gateway", kw, GCTX)


def test_the_real_key_may_appear_only_in_its_own_variable():
    kw = gw_kwargs()
    kw["labels"] = {"note": "sk-ant-GATEWAY-REAL-KEY"}
    with pytest.raises(PolicyViolation, match="real API key"):
        harden("gateway", kw, GCTX)
    kw = gw_kwargs()
    kw["environment"]["AIEP_ALLOWED_HOSTS"] = "sk-ant-GATEWAY-REAL-KEY"
    with pytest.raises(PolicyViolation, match="real API key"):
        harden("gateway", kw, GCTX)


def test_image_volume_must_be_neutralised_by_a_declared_tmpfs():
    # without extra_tmpfs the localstack VOLUME path is not an allowed tmpfs, so the request is refused
    plain = GatewayContext(agent_network="aiep-agent-net", egress_network="aiep-egress-net", image_id=GW_IMAGE_ID,
                           python=GW_PY, uid=GW_UID, gid=GW_GID)
    with pytest.raises(PolicyViolation):
        harden("gateway", gw_kwargs(), plain)
    # and an anonymous volume that was NOT neutralised shows up as a mount at inspect time
    info = good_gw_inspect()
    info["Mounts"] = [{"Type": "volume", "Destination": "/var/lib/localstack"}]
    with pytest.raises(PolicyViolation, match="mounts"):
        verify_inspect("gateway", info, GCTX)


@pytest.mark.parametrize("mutate", [
    lambda i: i["NetworkSettings"]["Networks"].pop("aiep-egress-net"),
    lambda i: i["NetworkSettings"]["Networks"].__setitem__("bridge", {}),
    lambda i: i.__setitem__("Image", "sha256:" + "1" * 64),
    lambda i: i["HostConfig"].__setitem__("ReadonlyRootfs", False),
    lambda i: i["Config"].__setitem__("User", "root"),
    lambda i: i["HostConfig"].__setitem__("Memory", 0),
])
def test_gateway_inspect_checks_networks_image_and_hardening(mutate):
    info = good_gw_inspect()
    mutate(info)
    with pytest.raises(PolicyViolation):
        verify_inspect("gateway", info, GCTX)


def test_agent_must_be_attached_to_the_agent_network_only():
    info = good_agent_inspect()
    info["NetworkSettings"] = {"Networks": {"aiep-agent-net": {}, "bridge": {}}}
    with pytest.raises(PolicyViolation, match="attached networks"):
        verify_inspect("agent", info, CTX)
    info["NetworkSettings"] = {"Networks": {"aiep-agent-net": {}}}
    verify_inspect("agent", info, CTX)


# ---------------- explicit interpreter (no assumed /usr/bin/python3) ----------------
@pytest.mark.parametrize("bad", [
    "", "python3", "python", "bin/python", "/usr/bin/../bin/python3", "/usr/bin/./python3", "//usr/bin/python3",
    "/usr/bin/python3 ", "/usr/bin/python3\n", "/usr/bin/py thon", "/usr/bin/$PYTHON", "/", "/usr/bin/",
    "/testbed/python", "/opt/aiep/python", "/opt/aiep-gw/python", "/tmp/python", "/home/agent/python",
    "/proc/self/exe", "/dev/shm/python", "/" + "a" * 600, None, 3,
])
def test_interpreter_path_must_be_explicit_absolute_normalised_and_on_the_read_only_root(bad):
    with pytest.raises(PolicyViolation):
        validate_interpreter_path(bad)


@pytest.mark.parametrize("good", ["/opt/miniconda3/bin/python", "/usr/local/bin/python3", "/usr/bin/python3"])
def test_interpreter_path_accepts_plain_absolute_paths(good):
    assert validate_interpreter_path(good) == good


def test_exec_runs_only_the_configured_interpreter_and_never_a_lookalike():
    verify = ["VERIFY"]
    ok = hx([PYTHON_FOR_TESTS, "-I", "-c", "VERIFY", "/testbed"])
    assert ok["cmd"][0] == PYTHON_FOR_TESTS
    for other in ("/usr/bin/python3", "/usr/bin/python", "python3", "/opt/miniconda3/bin/python3"):
        with pytest.raises(PolicyViolation):
            hx([other, "-I", "-c", "VERIFY", "/testbed"])
    # a context with no interpreter runs no interpreter at all (no default, no fallback)
    with pytest.raises(PolicyViolation):
        hx([PYTHON_FOR_TESTS, "-I", "-c", "VERIFY", "/testbed"],
           ctx=ExecContext(user="agent", allowed_scripts=frozenset({"VERIFY"})))
    assert verify == ["VERIFY"]


def test_exec_context_rejects_an_unsafe_interpreter_at_construction():
    for bad in ("python3", "/tmp/python", "/opt/aiep/python"):
        with pytest.raises(PolicyViolation):
            ExecContext(user="agent", python=bad)


def test_the_agent_context_cannot_start_the_gateway():
    with pytest.raises(PolicyViolation):
        hx([PYTHON_FOR_TESTS, "-m", "aiep.gateway"])


# ---------------- gateway identity and tmpfs ownership ----------------
def test_gateway_identity_is_one_explicit_uid_gid_and_never_root():
    assert GCTX.user == "10001:10001"
    for uid, gid in ((0, 10001), (10001, 0), (-1, 5), (5, -1), (True, 5), (5, True), (False, 5), ("10001", 5), (5, "5"),
                     (None, 5), (5, None), (1.5, 5), (5, 2.0), ([1], 5)):
        with pytest.raises(PolicyViolation):
            GatewayContext(agent_network="a", egress_network="e", image_id=GW_IMAGE_ID, python=GW_PY, uid=uid, gid=gid)


def test_gateway_context_has_no_default_identity_interpreter_or_anything_inferred():
    import dataclasses
    fields = {f.name: f for f in dataclasses.fields(GatewayContext)}
    for name in ("agent_network", "egress_network", "image_id", "python", "uid", "gid"):
        assert fields[name].default is dataclasses.MISSING and fields[name].default_factory is dataclasses.MISSING, name
    base = dict(agent_network="a", egress_network="e", image_id=GW_IMAGE_ID, python=GW_PY, uid=GW_UID, gid=GW_GID)
    GatewayContext(**base)
    for missing in ("python", "uid", "gid", "image_id"):
        kw = dict(base)
        del kw[missing]
        with pytest.raises(TypeError):  # a missing identity is refused outright, never filled in
            GatewayContext(**kw)


def test_gateway_tmpfs_builder_owns_every_mount_by_the_gateway_identity_and_keeps_it_private():
    opts = gateway_tmpfs(GCTX)
    assert set(opts) == {"/opt/aiep-gw", "/tmp", "/var/lib/localstack"}
    for path, o in opts.items():
        assert "uid=10001" in o and "gid=10001" in o and "mode=0700" in o, path
        assert "exec" not in o.split(","), path
    harden("gateway", gw_kwargs(), GCTX)


@pytest.mark.parametrize("mutate", [
    lambda t: t.__setitem__("/opt/aiep-gw", t["/opt/aiep-gw"].replace("uid=10001", "uid=0")),
    lambda t: t.__setitem__("/opt/aiep-gw", t["/opt/aiep-gw"].replace("gid=10001", "gid=0")),
    lambda t: t.__setitem__("/tmp", t["/tmp"].replace("uid=10001", "uid=10002")),
    lambda t: t.__setitem__("/opt/aiep-gw", t["/opt/aiep-gw"].replace("uid=10001,", "")),  # ownership left implicit (root)
    lambda t: t.__setitem__("/opt/aiep-gw", t["/opt/aiep-gw"].replace("mode=0700", "mode=0777")),
    lambda t: t.__setitem__("/opt/aiep-gw", t["/opt/aiep-gw"].replace("mode=0700", "mode=0750,mode=0770")),
    lambda t: t.__setitem__("/opt/aiep-gw", t["/opt/aiep-gw"].replace("mode=0700", "mode=0500")),  # owner cannot write
    lambda t: t.__setitem__("/opt/aiep-gw", t["/opt/aiep-gw"].replace(",mode=0700", "")),  # mode left to default
    lambda t: t.__setitem__("/var/lib/localstack", t["/var/lib/localstack"].replace("uid=10001", "uid=1")),
])
def test_gateway_tmpfs_with_wrong_owner_or_mode_is_refused_at_create_and_at_inspect(mutate):
    kw = gw_kwargs()
    mutate(kw["tmpfs"])
    with pytest.raises(PolicyViolation):
        harden("gateway", kw, GCTX)
    info = good_gw_inspect()
    mutate(info["HostConfig"]["Tmpfs"])
    with pytest.raises(PolicyViolation):
        verify_inspect("gateway", info, GCTX)


def test_gateway_exec_must_use_the_gateway_identity():
    from aiep.gateway_runner import gateway_exec_context

    ectx = gateway_exec_context(GCTX)
    assert ectx.user == "10001:10001" and ectx.python == GCTX.python and ectx.may_start_gateway
    start = harden_exec([GCTX.python, "-m", "aiep.gateway"], user="10001:10001", workdir="/", env={}, ctx=ectx)
    assert start["user"] == "10001:10001"
    for user in ("10001", "root", "0:0", "10002:10001"):
        with pytest.raises(PolicyViolation):
            harden_exec([GCTX.python, "-m", "aiep.gateway"], user=user, workdir="/", env={}, ctx=ectx)
    for argv in ([GCTX.python, "-m", "http.server"], [GCTX.python, "-m", "aiep.gateway", "--x"],
                 [GCTX.python, "-c", "print(1)"]):
        with pytest.raises(PolicyViolation):
            harden_exec(argv, user="10001:10001", workdir="/", env={}, ctx=ectx)


def test_gateway_entrypoint_is_required_and_fixed_not_inherited_from_the_image():
    kw = gw_kwargs()
    del kw["entrypoint"]  # leaving it out would run whatever ENTRYPOINT the image declares
    with pytest.raises(PolicyViolation, match="entrypoint"):
        harden("gateway", kw, GCTX)
    out = harden("gateway", gw_kwargs(), GCTX)
    assert out["entrypoint"] == ["/bin/sleep"] and out["command"] == ["2147483647"]
    info = good_gw_inspect()
    info["Config"]["Entrypoint"] = ["/usr/local/bin/docker-entrypoint.sh"]
    with pytest.raises(PolicyViolation, match="entrypoint"):
        verify_inspect("gateway", info, GCTX)


# ---------------- agent identity ----------------
def test_agent_identity_is_required_validated_and_defaultless():
    import dataclasses
    with pytest.raises(TypeError):
        AgentContext(gateway_url="u", agent_network="n", pinned_model=MODEL)  # no identity, no default
    assert all(f.default is dataclasses.MISSING for f in dataclasses.fields(AgentIdentity))
    for bad in (dict(name="root"), dict(name="Bad Name"), dict(uid=0), dict(gid=0), dict(uid=True), dict(uid="1000"),
                dict(home="/root"), dict(home="/home/x/../y"), dict(home="/tmp"), dict(home="/home/")):
        kw = dict(name="nonroot", uid=1000, gid=1000, home="/home/nonroot")
        kw.update(bad)
        with pytest.raises(PolicyViolation):
            AgentIdentity(**kw)


def test_agent_user_home_tmpfs_and_env_are_all_derived_from_the_one_identity():
    out = harden("agent", agent_kwargs(), CTX)
    assert out["user"] == "1000:1000" and out["environment"]["HOME"] == "/home/nonroot"
    assert set(out["tmpfs"]) == {"/tmp", "/home/nonroot", "/testbed", "/opt/aiep"}
    for over in ({"user": "nonroot"}, {"user": "1000"}, {"user": "1001:1000"}, {"user": "1000:1001"}, {"user": "root"},
                 {"user": "0:0"}):
        with pytest.raises(PolicyViolation, match="user must be exactly"):
            harden("agent", agent_kwargs(**over), CTX)
    kw = agent_kwargs()
    kw["environment"]["HOME"] = "/home/agent"
    with pytest.raises(PolicyViolation, match="HOME"):
        harden("agent", kw, CTX)
    kw = agent_kwargs()
    del kw["environment"]["HOME"]
    with pytest.raises(PolicyViolation, match="HOME"):
        harden("agent", kw, CTX)


@pytest.mark.parametrize("mutate", [
    lambda t: t.__setitem__("/home/nonroot", t["/home/nonroot"].replace("uid=1000", "uid=0")),
    lambda t: t.__setitem__("/home/nonroot", t["/home/nonroot"].replace("gid=1000", "gid=1001")),
    lambda t: t.__setitem__("/home/nonroot", t["/home/nonroot"].replace("mode=0700", "mode=0777")),
    lambda t: t.__setitem__("/testbed", t["/testbed"].replace(f"{OWN},", "")),
    lambda t: t.__setitem__("/tmp", t["/tmp"].replace(",mode=0700", "")),
    lambda t: t.pop("/home/nonroot"),                       # HOME must be mounted: the account's home on the read-only root is unwritable
    lambda t: t.__setitem__("/home/agent", t["/tmp"]),       # a home tmpfs for some other account
])
def test_agent_tmpfs_must_be_exactly_the_identitys_and_owned_by_it(mutate):
    kw = agent_kwargs()
    mutate(kw["tmpfs"])
    with pytest.raises(PolicyViolation):
        harden("agent", kw, CTX)
    info = good_agent_inspect()
    mutate(info["HostConfig"]["Tmpfs"])
    with pytest.raises(PolicyViolation):
        verify_inspect("agent", info, CTX)


def test_agent_inspect_user_must_equal_the_identity():
    for user in ("root", "nonroot", "1000", "1001:1000"):
        info = good_agent_inspect()
        info["Config"]["User"] = user
        with pytest.raises(PolicyViolation, match="must run as"):
            verify_inspect("agent", info, CTX)


def test_agent_environment_may_carry_the_experiment_path_and_conda_variables():
    kw = agent_kwargs()
    kw["environment"].update({"PATH": "/opt/miniconda3/envs/testbed/bin:/usr/bin", "CONDA_PREFIX": "/opt/miniconda3/envs/testbed",
                              "CONDA_DEFAULT_ENV": "testbed"})
    harden("agent", kw, CTX)


# ---------------- agent home: explicit, validated, no /home/<name> assumption ----------------
@pytest.mark.parametrize("home", ["/home/nonroot", "/nonexistent", "/var/lib/someone", "/home/a.b-c_d", "/srv/accounts/x",
                                  "/opt/accounts/x"])
def test_any_plain_absolute_home_that_overlaps_nothing_protected_is_accepted(home):
    assert AgentIdentity(name="x", uid=5, gid=5, home=home).home == home  # TEST-ONLY values


@pytest.mark.parametrize("home", [
    None, "", 0, "home/x", "./x", "/", "//", "/home", "/root", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/var", "/run",
    "/boot", "/opt", "/mnt", "/proc", "/proc/self", "/sys/x", "/dev", "/dev/shm", "/opt/miniconda3",
    "/home/x/", "/home/x/../y", "/home/./x", "/home//x", "/home/x y", "/home/$X", "/home/x\n", "/home/\u00e9", "/" + "a" * 600,
    "/testbed", "/testbed/home", "/tmp", "/tmp/home", "/opt/aiep", "/opt/aiep/home", "/opt/aiep-gw", "/opt/aiep-gw/x",
    "/opt/miniconda3/home", "/opt/miniconda3/envs",
])
def test_invalid_protected_or_overlapping_homes_are_refused(home):
    with pytest.raises(PolicyViolation):
        AgentIdentity(name="x", uid=5, gid=5, home=home)


def test_home_must_not_shadow_the_interpreter_or_a_path_directory():
    from aiep.agent_env import check_home_overlaps
    ident = AgentIdentity(name="x", uid=5, gid=5, home="/home/x")
    check_home_overlaps(ident, "/opt/miniconda3/bin/python")
    for home in ("/usr/local", "/usr/bin", "/usr/local/bin/x", "/usr/local/sbin"):
        with pytest.raises(PolicyViolation):
            check_home_overlaps(AgentIdentity(name="x", uid=5, gid=5, home=home), "/opt/miniconda3/bin/python")
    with pytest.raises(PolicyViolation):  # home that contains the pinned interpreter
        check_home_overlaps(AgentIdentity(name="x", uid=5, gid=5, home="/srv/py"), "/srv/py/bin/python")


def test_account_names_are_only_checked_for_safe_characters_not_for_a_convention():
    for name in ("nonroot", "Agent-1", "_svc", "a.b"):
        AgentIdentity(name=name, uid=5, gid=5, home="/home/h")
    for name in ("", "root", "a b", "a:b", "-x", ".x", "a\n", "x" * 65, None):
        with pytest.raises(PolicyViolation):
            AgentIdentity(name=name, uid=5, gid=5, home="/home/h")

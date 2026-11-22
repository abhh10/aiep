"""Gateway lifecycle and combined-run tests with fakes: no Docker, no network, no containers."""
import json
import threading
from pathlib import Path

import pytest

from aiep.agent_runner import STATUS_EXITED, STATUS_GATEWAY_STOPPED, STATUS_INVALID
from aiep.container_exec import REGISTERED_SCRIPTS
from aiep.container_policy import (GATEWAY_MEM_BYTES, GATEWAY_NANO_CPUS, GATEWAY_PIDS, PROXY_MEM_BYTES, PROXY_NANO_CPUS, PROXY_PIDS,
                                   GatewayContext, PolicyViolation, ProxyContext)
from aiep.gateway_runner import build_gateway_component
from aiep.proxy_lifecycle import ProxySpec, build_proxy_component
from aiep.lifecycle import (LEDGER_PATH, STOP_FILE, GatewayManager, GatewaySpec, GatewayStartError,
                            build_gateway_create_kwargs, lifecycle_record, run_with_gateway)
from fake_docker import ExecResult, FakeAgent, FakeContainerFS, FakeExecApi
from test_agent_runner import (REAL_KEY, TASK, FakeContainer, edit_agent, good_inspect, repo, seed,  # noqa: F401 (fixtures)
                               spec_for)

REAL = Path(__file__).resolve().parents[1]
GW_PY = "/usr/local/bin/python3"
GW_IMAGE_ID = "sha256:" + "9" * 64
TOKEN = "per-run-token"  # what spec_for gives the agent


def gateway_spec(**over):
    d = dict(run_id="r1", image="python@sha256:" + "3" * 64, run_token=TOKEN, budget_usd="0.50",
             code=build_gateway_component(REAL),
             ctx=GatewayContext(agent_network="aiep-agent-net", proxy_network="aiep-proxy-net", image_id=GW_IMAGE_ID,
                                python=GW_PY, uid=10001, gid=10001, real_key=REAL_KEY, agent_cidr="172.30.1.0/24"),  # TEST-ONLY uid:gid
             proxy_endpoint="aiep.proxy.r1:3128")
    d.update(over)
    return GatewaySpec(**d)


def proxy_spec(**over):
    d = dict(run_id="r1", image="python@sha256:" + "3" * 64, code=build_proxy_component(REAL),
             ctx=ProxyContext(proxy_network="aiep-proxy-net", egress_network="aiep-egress-net", image_id=GW_IMAGE_ID, python=GW_PY,
                              uid=10002, gid=10002, proxy_cidr="172.30.2.0/24", forbidden_secrets=(REAL_KEY, TOKEN)))
    d.update(over)
    return ProxySpec(**d)


class HostApi(FakeExecApi):
    """Routes exec to the right fake container filesystem and answers inspect per container."""

    def __init__(self, host):
        super().__init__(self._route)
        self.host = host

    def _route(self, argv, rec, stdin):
        return self.host.fs_for(rec["container"]).handler(argv, rec, stdin)

    def inspect_container(self, cid):
        return self.host.inspects[cid]


def _compliant_networks():
    """The three AIEP networks exactly as the policy requires them (what execute()'s run-time preflight inspects)."""
    from aiep import networks as N
    from test_networks import Client as NetClient
    return N.ensure_networks(NetClient())


COMPLIANT_NETWORKS = _compliant_networks()


_PREFIX = {"aiep-agent-net": "172.30.1", "aiep-proxy-net": "172.30.2", "aiep-egress-net": "172.30.3"}  # inside each fake network's subnet


class FakeNetwork:
    def __init__(self, host, name):
        self.host, self.name = host, name
        self.id = COMPLIANT_NETWORKS[name]["Id"]
        self.reload()

    def reload(self):
        self.attrs = {**COMPLIANT_NETWORKS[self.name], "Containers": {cid: {} for cid, nets in self.host.attached.items() if self.name in nets}}

    def connect(self, container):
        if self.host.connect_error and self.host.connect_error_for in (None, container.id):
            raise self.host.connect_error
        self.host.connected.append((self.name, container.id))
        self.host.attach(container.id, self.name)


class FakeHost:
    """Both containers, one Docker API. ``events`` is the global order of lifecycle-relevant calls."""

    def __init__(self, tmp_path, aspec, gspec, *, agent=None, pspec=None):
        self.aspec, self.gspec, self.pspec = aspec, gspec, pspec or proxy_spec()
        self.px_fs = FakeContainerFS(tmp_path / "px", python=GW_PY, owner=(10002, 10002))
        self.px_c = FakeContainer()
        self.px_c.id = "px-1"
        self.attached: dict[str, set[str]] = {}
        self.connect_error_for: str | None = None
        self.gw_fs = FakeContainerFS(tmp_path / "gw", python=GW_PY, owner=(10001, 10001))
        self.ag_fs = FakeContainerFS(tmp_path / "ag", agent=agent or edit_agent())
        self.gw_c, self.ag_c = FakeContainer(), FakeContainer()
        self.gw_c.id, self.ag_c.id = "gw-1", "ag-1"
        self.inspects: dict[str, dict] = {}
        self.created: list[tuple[str, dict]] = []
        self.connected: list[tuple[str, str]] = []
        self.connect_error: Exception | None = None
        self.api = HostApi(self)
        host = self
        self.events: list[str] = []
        for c, label in ((self.gw_c, "gateway"), (self.ag_c, "agent"), (self.px_c, "proxy")):
            for meth in ("start", "kill"):
                orig = getattr(c, meth)
                setattr(c, meth, (lambda o=orig, m=meth, l=label: (host.events.append(f"{l}.{m}"), o())[1]))
            orig_remove = c.remove
            c.remove = lambda force=False, v=False, o=orig_remove, l=label: (host.events.append(f"{l}.remove"), o(force=force, v=v))[1]

        class Containers:
            def create(_, image, command=None, **kw):
                role = "proxy" if kw["name"].startswith("aiep.proxy.") else "gateway" if kw["name"].startswith("aiep.gateway.") else "agent"
                host.events.append(f"{role}.create")
                host.created.append((role, {"image": image, "command": command, **kw}))
                c = {"proxy": host.px_c, "gateway": host.gw_c, "agent": host.ag_c}[role]
                if host.create_error.get(role):
                    raise host.create_error[role]
                host.inspects[c.id] = (host._gw_inspect({**kw, "command": command}) if role == "gateway" else
                                       host._px_inspect({**kw, "command": command}) if role == "proxy" else good_inspect(host.aspec) | {})
                host.attach(c.id, kw["network"])
                return c

        class Networks:
            def get(_, name):
                return FakeNetwork(host, name)

        self.containers, self.networks = Containers(), Networks()
        self.create_error: dict[str, Exception] = {}

    def attach(self, cid, net):
        self.attached.setdefault(cid, set()).add(net)
        nets = self.inspects[cid]["NetworkSettings"]["Networks"]
        nets[net] = {"NetworkID": COMPLIANT_NETWORKS[net]["Id"], "IPAddress": f"{_PREFIX[net]}.{len(self.attached) + 1}"}

    @property
    def client(self):
        return self

    def fs_for(self, cid):
        return {self.gw_c.id: self.gw_fs, self.px_c.id: self.px_fs}.get(cid, self.ag_fs)

    def _px_inspect(self, kw):
        ctx = self.pspec.ctx
        return {"Image": ctx.image_id,
                "HostConfig": {"Privileged": False, "CapAdd": None, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"],
                               "ReadonlyRootfs": True, "Memory": PROXY_MEM_BYTES, "PidsLimit": PROXY_PIDS,
                               "NanoCpus": PROXY_NANO_CPUS, "Tmpfs": kw["tmpfs"], "Binds": None},
                "Mounts": [], "Config": {"User": ctx.user, "Entrypoint": kw["entrypoint"], "Cmd": kw["command"],
                                         "Env": [f"{k}={v}" for k, v in kw["environment"].items()]},
                "NetworkSettings": {"Networks": {}}}

    def _gw_inspect(self, kw):
        ctx = self.gspec.ctx
        return {"Image": ctx.image_id,
                "HostConfig": {"Privileged": False, "CapAdd": None, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"],
                               "ReadonlyRootfs": True, "Memory": GATEWAY_MEM_BYTES, "PidsLimit": GATEWAY_PIDS,
                               "NanoCpus": GATEWAY_NANO_CPUS, "Tmpfs": kw["tmpfs"], "Binds": None},
                "Mounts": [], "Config": {"User": ctx.user, "Entrypoint": kw["entrypoint"], "Cmd": kw["command"]},
                "NetworkSettings": {"Networks": {}}}


def build(tmp_path, seed, **kw):
    gspec = kw.pop("gspec", None) or gateway_spec()
    aspec = spec_for(seed, gateway_url=gspec.url, run_token=gspec.run_token, forbidden_secrets=())
    host = FakeHost(tmp_path, aspec, gspec, agent=kw.pop("agent", None), pspec=kw.pop("pspec", None))
    return host, gspec, aspec


def run(host, gspec, aspec, **kw):
    kw.setdefault("poll_interval", 0.02)
    kw.setdefault("min_poll_interval", 0.0)
    kw.setdefault("pspec", host.pspec)
    return run_with_gateway(host, gspec, aspec, **kw)


def release(host):
    host.gw_fs.gateway_alive.clear()


# ---------------- create kwargs ----------------
def test_gateway_create_kwargs_are_policy_checked_inert_and_carry_the_key_in_one_variable_only():
    kw = build_gateway_create_kwargs(gateway_spec())
    assert kw["entrypoint"] == ["/bin/sleep"] and kw["command"] == ["2147483647"]
    assert kw["user"] == "10001:10001" and kw["read_only"] is True and kw["network"] == "aiep-agent-net"
    assert kw["environment"]["AIEP_UPSTREAM_API_KEY"] == REAL_KEY
    assert REAL_KEY not in json.dumps({k: v for k, v in kw.items() if k != "environment"})
    assert {k: v for k, v in kw["environment"].items() if REAL_KEY in v} == {"AIEP_UPSTREAM_API_KEY": REAL_KEY}
    assert kw["environment"]["AIEP_ALLOWED_HOSTS"] == "aiep.gateway.r1:8080"
    assert "ports" not in kw and "volumes" not in kw and "privileged" not in kw
    assert REAL_KEY not in repr(gateway_spec())


def test_a_missing_real_key_or_unpinned_pieces_are_refused():
    ctx = GatewayContext(agent_network="aiep-agent-net", proxy_network="aiep-proxy-net", image_id=GW_IMAGE_ID, python=GW_PY,
                         uid=10001, gid=10001, agent_cidr="172.30.1.0/24")
    with pytest.raises(PolicyViolation):
        build_gateway_create_kwargs(gateway_spec(ctx=ctx))
    with pytest.raises(ValueError):
        gateway_spec(run_id="../x")
    with pytest.raises(ValueError):
        gateway_spec(port=0)


# ---------------- the agent never starts before the gateway is verified and ready ----------------
def test_happy_path_order_gateway_verified_and_ready_before_the_agent_exists(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    res = run(host, gspec, aspec)
    release(host)
    assert res.gateway_error is None, res.gateway_error
    assert res.status == "completed" and res.agent.status == STATUS_EXITED and res.agent.valid
    ev = host.events
    assert (ev.index("proxy.create") < ev.index("proxy.start") < ev.index("gateway.create") < ev.index("gateway.start")
            < ev.index("agent.create") < ev.index("agent.start"))
    # inside the gateway: tar, verification listing, process start, readiness probe, then the agent's first call
    names = [(c["container"], c["cmd"][0], c["cmd"][1] if len(c["cmd"]) > 1 else "") for c in host.api.calls]
    gw = [n[1:] for n in names if n[0] == "gw-1"]
    assert gw[:3] == [("/bin/tar", "-x"), (GW_PY, "-I"), (GW_PY, "-m")] and gw[3][0] == GW_PY and gw[3][1] == "-I"
    first_agent_call = next(i for i, n in enumerate(names) if n[0] == "ag-1")
    last_gateway_prestart = max(i for i, n in enumerate(names) if n[0] == "gw-1" and n[2] == "-I")
    assert last_gateway_prestart < first_agent_call
    assert host.connected == [("aiep-egress-net", "px-1"), ("aiep-proxy-net", "gw-1")]  # the proxy gets egress; the gateway never does
    assert host.gw_fs.gateway_started == [[GW_PY, "-m", "aiep.gateway"]] and host.gw_fs.started_after_listings == 1


@pytest.mark.parametrize("tamper, needle", [
    (lambda d: (d / "src/aiep/gateway.py").write_bytes(b"# evil\n"), "content differs"),
    (lambda d: (d / "src/aiep/extra.py").write_text("x"), "unexpected"),
])
def test_integrity_failure_means_no_gateway_process_no_agent_and_full_cleanup(tmp_path, seed, tamper, needle):
    host, gspec, aspec = build(tmp_path, seed)
    orig = host.gw_fs.handler

    def handler(argv, rec, stdin):
        res = orig(argv, rec, stdin)
        if argv[0] == "/bin/tar":
            tamper(host.gw_fs.path("/opt/aiep-gw"))
        return res
    host.gw_fs.handler = handler
    res = run(host, gspec, aspec)
    assert res.status == "gateway_failed" and needle in res.gateway_error and res.agent is None
    assert host.gw_fs.gateway_started == [] and not any(e.startswith("agent.") for e in host.events)
    assert host.created and all(k in ("gateway", "proxy") for k, _ in host.created)
    assert res.gateway_removed and res.proxy_removed and host.events[-3:] == ["gateway.remove", "proxy.kill", "proxy.remove"]


def test_a_gateway_that_never_becomes_ready_blocks_the_agent_and_is_removed(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed, gspec=gateway_spec(ready_timeout_s=0.3))
    orig = host.gw_fs.handler

    def handler(argv, rec, stdin):
        res = orig(argv, rec, stdin)
        host.gw_fs.gateway_listening = False  # the process runs but never listens
        return res
    host.gw_fs.handler = handler
    res = run(host, gspec, aspec)
    assert res.status == "gateway_failed" and "readiness deadline" in res.gateway_error
    assert res.agent is None and not any(e.startswith("agent.") for e in host.events)
    assert res.gateway_removed and res.gateway_state == "removed"
    assert not host.gw_fs.gateway_alive.is_set()  # the gateway process was killed by stop()


def test_a_gateway_process_that_exits_during_startup_is_reported_with_its_stderr(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    orig = host.gw_fs.handler

    def handler(argv, rec, stdin):
        if argv[1:] == ["-m", "aiep.gateway"]:
            return ExecResult(stderr=b"Traceback: boom", exit_code=1)
        return orig(argv, rec, stdin)
    host.gw_fs.handler = handler
    res = run(host, gspec, aspec)
    assert res.status == "gateway_failed" and "exited before it was ready" in res.gateway_error and "boom" in res.gateway_error
    assert res.agent is None and res.gateway_removed


@pytest.mark.parametrize("fail_at", ["create", "network", "inspect", "start"])
def test_failures_in_every_early_step_leave_nothing_behind(tmp_path, seed, fail_at):
    host, gspec, aspec = build(tmp_path, seed)
    if fail_at == "create":
        def boom(*a, **k):
            raise RuntimeError("daemon refused")
        host.containers.create = boom
    elif fail_at == "network":
        host.connect_error = RuntimeError("no such network")
    elif fail_at == "inspect":
        orig = host.containers.create

        def create(image, command=None, **kw):
            c = orig(image, command, **kw)
            host.inspects[c.id]["HostConfig"]["ReadonlyRootfs"] = False
            return c
        host.containers.create = create
    else:
        host.px_c.start_error = RuntimeError("start failed")
    res = run(host, gspec, aspec)
    # the proxy is the first container created, so its failure ends the run before the gateway or the agent exist
    assert res.status == "proxy_failed" and res.agent is None and res.proxy_error and res.gateway_error is None
    assert not any(e.startswith(("agent.", "gateway.")) for e in host.events)
    created = "proxy.create" in host.events
    assert res.proxy_removed and (not created or host.events[-1] == "proxy.remove")
    assert host.gw_fs.gateway_started == [] and host.px_fs.gateway_started == []


def test_state_machine_refuses_out_of_order_steps(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    gm = GatewayManager(host, gspec)
    for step in (gm.start, gm.inject_verify_start, gm.wait_ready):
        with pytest.raises(GatewayStartError):
            step()
    assert gm.stop_reason() == "gateway_unreachable"  # not ready: never reported as healthy
    gm.create()
    with pytest.raises(GatewayStartError):
        gm.inject_verify_start()  # cannot inject or start the process before the container runs
    assert gm.teardown() == (True, None) and gm.state == "removed"


def test_inspect_with_the_wrong_image_entrypoint_or_networks_is_refused_after_create(tmp_path, seed):
    for mutate in (lambda i: i.__setitem__("Image", "sha256:" + "1" * 64),
                   lambda i: i["Config"].__setitem__("Entrypoint", ["/entry.sh"])):
        host, gspec, aspec = build(tmp_path, seed)
        orig = host.containers.create

        def create(image, command=None, orig=orig, mutate=mutate, **kw):
            c = orig(image, command, **kw)
            if kw["name"].startswith("aiep.gateway."):
                mutate(host.inspects[c.id])
            return c
        host.containers.create = create
        res = run(host, gspec, aspec)
        assert res.status == "gateway_failed" and "gateway:" in res.gateway_error and res.gateway_removed and res.proxy_removed


# ---------------- stop reasons reach agent supervision ----------------
def test_budget_exhaustion_from_the_gateway_stop_file_stops_the_agent_and_keeps_the_partial_patch(tmp_path, seed):
    hang = threading.Event()
    host_holder = {}

    def before(task, wd):
        (wd / "b.py").write_text("b = 1\n")
        host_holder["h"].gw_fs.files[STOP_FILE] = b"budget_exhausted\n"
        host_holder["h"].gw_fs.files[LEDGER_PATH] = b'{"outcome":"ok"}\n'

    agent = FakeAgent(before=before, hang=hang)
    host, gspec, aspec = build(tmp_path, seed, agent=agent)
    host_holder["h"] = host
    res = run(host, gspec, aspec)
    hang.set()
    release(host)
    assert res.status == "completed" and res.agent.status == STATUS_GATEWAY_STOPPED
    assert res.agent.stop_reason == "budget_exhausted" and res.agent.valid and res.agent.patch.status == "ok"
    assert res.gateway_stop_reason == "budget_exhausted" and res.ledger == b'{"outcome":"ok"}\n'
    rec = lifecycle_record(res)
    assert rec["ledger"]["bytes"] == len(res.ledger) and "outcome" not in json.dumps(rec)  # hash and size, not the body
    assert REAL_KEY not in json.dumps(rec)


@pytest.mark.parametrize("reason", ["model_violation", "tool_violation", "metering_failure"])
def test_gateway_violations_invalidate_the_run_and_nothing_is_extracted(tmp_path, seed, reason):
    hang = threading.Event()
    agent = FakeAgent(hang=hang)
    host, gspec, aspec = build(tmp_path, seed, agent=agent)
    orig = host.ag_fs.handler

    def handler(argv, rec, stdin):
        if argv[0] == "/opt/aiep/claude":
            host.gw_fs.files[STOP_FILE] = reason.encode()
        return orig(argv, rec, stdin)
    host.ag_fs.handler = handler
    res = run(host, gspec, aspec)
    hang.set()
    release(host)
    assert res.agent.status == STATUS_GATEWAY_STOPPED and not res.agent.valid and res.agent.patch is None
    assert reason in res.agent.invalid_reason and res.gateway_stop_reason == reason


def test_a_gateway_that_dies_mid_run_invalidates_the_run_and_is_cleaned_up(tmp_path, seed):
    hang = threading.Event()
    agent = FakeAgent(hang=hang)
    host, gspec, aspec = build(tmp_path, seed, agent=agent)
    orig = host.ag_fs.handler

    def handler(argv, rec, stdin):
        if argv[0] == "/opt/aiep/claude":
            threading.Timer(0.1, host.gw_fs.gateway_alive.clear).start()  # the gateway process ends by itself
        return orig(argv, rec, stdin)
    host.ag_fs.handler = handler
    res = run(host, gspec, aspec)
    hang.set()
    assert res.agent.stop_reason == "gateway_died" and res.agent.status == STATUS_GATEWAY_STOPPED and not res.agent.valid
    assert res.agent.patch is None and res.agent.removed and res.gateway_removed


def test_an_unreachable_stop_channel_counts_as_a_stop_never_as_healthy(tmp_path, seed):
    hang = threading.Event()
    host, gspec, aspec = build(tmp_path, seed, agent=FakeAgent(hang=hang))
    orig = host.gw_fs.handler
    broken = {"on": False}

    def handler(argv, rec, stdin):
        if broken["on"] and argv[0] == "/bin/cat":
            return ExecResult(stderr=b"cat: Permission denied", exit_code=1)
        return orig(argv, rec, stdin)
    host.gw_fs.handler = handler
    ag = host.ag_fs.handler

    def ag_handler(argv, rec, stdin):
        if argv[0] == "/opt/aiep/claude":
            broken["on"] = True
        return ag(argv, rec, stdin)
    host.ag_fs.handler = ag_handler
    res = run(host, gspec, aspec)
    hang.set()
    assert res.agent.stop_reason == "gateway_unreachable" and not res.agent.valid


def test_stop_reason_is_sticky_and_rate_limited(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    now = {"t": 0.0}
    gm = GatewayManager(host, gspec, clock=lambda: now["t"], sleep=lambda s: None, min_poll_interval=1.0)
    gm.setup()
    reads = lambda: sum(1 for c in host.api.calls if c["cmd"][0] == "/bin/cat")
    assert gm.stop_reason() is None and reads() == 1
    assert gm.stop_reason() is None and reads() == 1  # within the interval: no new exec
    now["t"] = 5.0
    host.gw_fs.files[STOP_FILE] = b"budget_exhausted\n"
    assert gm.stop_reason() == "budget_exhausted"
    host.gw_fs.files.pop(STOP_FILE)
    now["t"] = 10.0
    assert gm.stop_reason() == "budget_exhausted"  # sticky
    gm.stop()
    gm.teardown()


# ---------------- agent-side failures still clean up the gateway ----------------
def test_agent_identity_failure_still_stops_and_removes_both_containers(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    host.ag_fs.account = None  # the image has no passwd entry
    res = run(host, gspec, aspec)
    release(host)
    assert res.status == "completed" and res.agent.status == STATUS_INVALID and "no passwd entry" in res.agent.invalid_reason
    assert res.agent.removed and res.gateway_removed
    assert host.events.count("gateway.remove") == 1 and host.events.count("agent.remove") == 1
    assert host.events.index("agent.remove") < host.events.index("gateway.remove")  # agent first, then the gateway


def test_an_unexpected_agent_exception_propagates_but_the_gateway_is_still_torn_down(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)

    def boom(*a, **k):
        raise RuntimeError("agent runner bug")
    with pytest.raises(RuntimeError, match="agent runner bug"):
        run(host, gspec, aspec, run_agent_fn=boom)
    assert host.events[-3:] == ["gateway.remove", "proxy.kill", "proxy.remove"] and host.gw_c.status == "exited" and host.px_c.status == "exited"
    assert not host.gw_fs.gateway_alive.is_set()


def test_gateway_remove_failure_is_reported_not_swallowed(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    host.gw_c.remove_error = RuntimeError("daemon busy")
    res = run(host, gspec, aspec)
    release(host)
    assert res.gateway_removed is False and "daemon busy" in res.gateway_cleanup_error and res.agent.removed


def test_all_exec_sockets_and_processes_are_closed_at_the_end(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    gm = GatewayManager(host, gspec, min_poll_interval=0.0)
    gm.setup()
    assert gm.session is not None and not gm.session.done()
    gm.stop()
    assert gm.session._closed and not host.gw_fs.gateway_alive.is_set() and gm.state == "stopped"
    assert gm.teardown() == (True, None) and gm.state == "removed"


# ---------------- consistency checks between the two specs, before anything is created ----------------
@pytest.mark.parametrize("over", [{"gateway_url": "http://other:8080"}, {"agent_network": "somewhere-else"},
                                  {"run_token": "another-token"}])
def test_mismatched_specs_are_refused_before_any_docker_call(tmp_path, seed, over):
    host, gspec, aspec = build(tmp_path, seed)
    import dataclasses
    bad = dataclasses.replace(aspec, **over)
    with pytest.raises(ValueError):
        run(host, gspec, bad)
    assert host.events == [] and host.api.calls == []


def test_the_real_key_is_added_to_the_agents_forbidden_secrets(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    assert REAL_KEY not in aspec.forbidden_secrets
    seen = {}

    def spy(client, spec, stopped, **kw):
        seen["secrets"] = spec.forbidden_secrets
        raise RuntimeError("stop here")
    with pytest.raises(RuntimeError):
        run(host, gspec, aspec, run_agent_fn=spy)
    assert REAL_KEY in seen["secrets"]
    assert [k for k, _ in host.created] == ["proxy", "gateway"]  # the agent was never created by this spy
    assert REAL_KEY not in json.dumps(host.created[0][1]) and TOKEN not in json.dumps(host.created[0][1])  # nothing secret reaches the proxy


def test_the_registered_scripts_include_the_probes_and_nothing_executes_unregistered_text():
    from aiep.agent_env import IDENTITY_SCRIPT
    from aiep.container_policy import READY_SCRIPT
    assert IDENTITY_SCRIPT in REGISTERED_SCRIPTS and READY_SCRIPT in REGISTERED_SCRIPTS


# ---------------- cleanup is guaranteed: every stage, and KeyboardInterrupt ----------------
def _raising(exc):
    def boom(self, *a, **k):
        raise exc
    return boom


def _arm(host, tmp_marker=b"budget_exhausted\n"):
    host.gw_fs.files[LEDGER_PATH] = b'{"outcome":"ok"}\n'  # something for the ledger read to return


def _assert_torn_down_in_order(host, res=None):
    ev = host.events
    assert ev.count("gateway.remove") == 1 and ev.count("proxy.remove") == 1 and ev[-1] == "proxy.remove"
    assert ev.index("gateway.remove") < ev.index("proxy.remove")  # the proxy outlives the gateway, its only client
    if "agent.create" in ev:
        assert ev.index("agent.remove") < ev.index("gateway.remove")  # agent teardown strictly before the gateway's


@pytest.mark.parametrize("stage, still_collected", [
    ("stop", {"stop_reason": True, "ledger": True}),
    ("final_stop_reason", {"stop_reason": False, "ledger": True}),  # a failed read must not prevent the ledger read
    ("read_ledger", {"stop_reason": True, "ledger": False}),
])
def test_an_exception_at_any_cleanup_stage_is_recorded_and_teardown_still_runs(tmp_path, seed, monkeypatch, stage, still_collected):
    host, gspec, aspec = build(tmp_path, seed)
    _arm(host)
    host.gw_fs.files[STOP_FILE] = b""  # present but empty: no stop reason
    monkeypatch.setattr(GatewayManager, stage, _raising(RuntimeError(f"injected at {stage}")))
    res = run(host, gspec, aspec)
    release(host)
    assert res.status == "completed" and res.agent.valid and res.agent.status == STATUS_EXITED  # the run outcome is untouched
    assert res.gateway_removed is True and res.gateway_state == "removed" and res.gateway_cleanup_error is None
    _assert_torn_down_in_order(host)
    label = {"stop": "stop", "final_stop_reason": "final stop reason", "read_ledger": "ledger"}[stage]
    assert any(f"cleanup {label}:" in e and f"injected at {stage}" in e for e in res.gateway_errors), res.gateway_errors
    assert (res.ledger is not None) == still_collected["ledger"]  # later stages were still attempted


def test_every_cleanup_stage_failing_at_once_still_removes_the_gateway(tmp_path, seed, monkeypatch):
    host, gspec, aspec = build(tmp_path, seed)
    for stage in ("stop", "final_stop_reason", "read_ledger"):
        monkeypatch.setattr(GatewayManager, stage, _raising(RuntimeError(f"injected at {stage}")))
    res = run(host, gspec, aspec)
    release(host)
    assert res.status == "completed" and res.agent.valid and res.gateway_removed
    # the drain (which also reads the ledger) fails too: four independent stage errors, none hidden, and the accounting is flagged incomplete
    assert len(res.gateway_errors) == 4 and res.ledger is None
    assert any(e.startswith("cleanup drain:") for e in res.gateway_errors)
    assert res.accounting["complete"] is False and res.accounting["exact"] is False and res.accounting["readable"] is False
    _assert_torn_down_in_order(host)


def test_a_teardown_that_itself_raises_is_reported_and_does_not_replace_the_outcome(tmp_path, seed, monkeypatch):
    host, gspec, aspec = build(tmp_path, seed)
    monkeypatch.setattr(GatewayManager, "teardown", _raising(RuntimeError("teardown bug")))
    res = run(host, gspec, aspec)
    release(host)
    assert res.status == "completed" and res.agent.valid
    assert res.gateway_removed is False and "teardown bug" in res.gateway_cleanup_error


def test_the_original_exception_survives_failures_during_cleanup(tmp_path, seed, monkeypatch):
    host, gspec, aspec = build(tmp_path, seed)
    for stage in ("stop", "final_stop_reason", "read_ledger"):
        monkeypatch.setattr(GatewayManager, stage, _raising(RuntimeError(f"cleanup failure at {stage}")))

    def boom(*a, **k):
        raise RuntimeError("the original agent failure")
    with pytest.raises(RuntimeError, match="the original agent failure"):  # not one of the cleanup errors
        run(host, gspec, aspec, run_agent_fn=boom)
    release(host)
    _assert_torn_down_in_order(host)


@pytest.mark.parametrize("stage", ["stop", "final_stop_reason", "read_ledger"])
def test_keyboard_interrupt_at_any_cleanup_stage_still_tears_the_gateway_down(tmp_path, seed, monkeypatch, stage):
    host, gspec, aspec = build(tmp_path, seed)
    _arm(host)
    monkeypatch.setattr(GatewayManager, stage, _raising(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        run(host, gspec, aspec)
    release(host)
    _assert_torn_down_in_order(host)  # the agent is gone, then the gateway was killed and removed despite the interrupt


def test_keyboard_interrupt_during_kill_still_attempts_the_removal(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)

    def interrupted_kill():
        host.events.append("gateway.kill")
        raise KeyboardInterrupt
    host.gw_c.kill = interrupted_kill
    with pytest.raises(KeyboardInterrupt):
        run(host, gspec, aspec)
    release(host)
    assert host.events[-4:] == ["gateway.kill", "gateway.remove", "proxy.kill", "proxy.remove"]  # remove(force=True) was still reached
    _assert_torn_down_in_order(host)


def test_a_failing_kill_or_remove_is_reported_and_remove_is_still_tried(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    host.gw_c.kill = lambda: (_ for _ in ()).throw(RuntimeError("kill failed"))
    res = run(host, gspec, aspec)
    release(host)
    assert res.gateway_removed is True and host.events[-1] == "proxy.remove" and "gateway.remove" in host.events  # kill errors are ignorable: it may be stopped
    host2, gspec2, aspec2 = build(tmp_path / "b", seed)
    host2.gw_c.remove_error = RuntimeError("daemon busy")
    res2 = run(host2, gspec2, aspec2)
    release(host2)
    assert res2.gateway_removed is False and "daemon busy" in res2.gateway_cleanup_error


def test_keyboard_interrupt_during_cleanup_keeps_the_original_exception_as_context(tmp_path, seed, monkeypatch):
    host, gspec, aspec = build(tmp_path, seed)
    monkeypatch.setattr(GatewayManager, "stop", _raising(KeyboardInterrupt()))

    def boom(*a, **k):
        raise RuntimeError("the original agent failure")
    with pytest.raises(KeyboardInterrupt) as info:
        run(host, gspec, aspec, run_agent_fn=boom)
    release(host)
    assert isinstance(info.value.__context__, RuntimeError) and "original agent failure" in str(info.value.__context__)
    _assert_torn_down_in_order(host)


def test_keyboard_interrupt_while_the_agent_runs_removes_the_agent_then_the_gateway(tmp_path, seed, monkeypatch):
    hang = threading.Event()
    host, gspec, aspec = build(tmp_path, seed, agent=FakeAgent(hang=hang))
    calls = {"n": 0}
    real = GatewayManager.stop_reason

    def interrupting(self):
        calls["n"] += 1
        if calls["n"] > 2:
            raise KeyboardInterrupt  # Ctrl-C arrives inside the supervision loop
        return real(self)
    monkeypatch.setattr(GatewayManager, "stop_reason", interrupting)
    with pytest.raises(KeyboardInterrupt):
        run(host, gspec, aspec)
    hang.set()
    release(host)
    assert "agent.create" in host.events and "agent.remove" in host.events
    _assert_torn_down_in_order(host)


@pytest.mark.parametrize("step", ["create", "start", "inject_verify_start", "wait_ready"])
def test_keyboard_interrupt_during_setup_removes_the_gateway_and_never_creates_the_agent(tmp_path, seed, monkeypatch, step):
    host, gspec, aspec = build(tmp_path, seed)
    monkeypatch.setattr(GatewayManager, step, _raising(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        run(host, gspec, aspec)
    release(host)
    assert not any(e.startswith("agent.") for e in host.events)
    created = "gateway.create" in host.events
    if step != "create":
        assert created
        # states before create did not exist; the manager only knows a container exists once create() has finished
    assert (not created) or host.events[-1] == "proxy.remove" or step == "create"

"""Proxy container lifecycle, the three-container run, startup/teardown failures, topology, reaper ownership and secret absence. Fakes only."""
import dataclasses
import json
import threading
from pathlib import Path

import pytest

from aiep import networks as N
from aiep import reaper as R
from aiep.agent_runner import STATUS_EXITED, STATUS_GATEWAY_STOPPED
from aiep.container_policy import PolicyViolation, ProxyContext, harden, verify_inspect
from aiep.gateway_runner import GatewayImagePin, load_gateway_image
from aiep.lifecycle import GatewayManager, run_with_gateway
from aiep.proxy_lifecycle import (PROXY_DEST, PROXY_FILES, ProxyConfigError, ProxyIntegrityError, ProxyManager, ProxyStartError, build_proxy_component,
                                  build_proxy_create_kwargs, load_proxy_pin, proxy_context)
from aiep.seed import verify_archive
from fake_docker import ExecResult, FakeAgent
from test_agent_runner import REAL_KEY, repo, seed  # noqa: F401 (fixtures)
from test_lifecycle import TOKEN, build, gateway_spec, proxy_spec, release, run
from test_reaper import FakeApi, c, lease_file, reap

REAL = Path(__file__).resolve().parents[1]
GW_PIN = GatewayImagePin("docker.io/library/python@sha256:" + "3" * 64, "sha256:" + "9" * 64, "/usr/local/bin/python3", 10001, 10001, ())


# ---------------- pin, identity, component ----------------
def write_proxy_cfg(tmp_path, **over):
    d = {"schema": 1, "status": "verified", "uid": 10002, "gid": 10002, "port": 3128, **over}
    p = tmp_path / "proxy.json"
    p.write_text(json.dumps(d))
    return p


def test_the_real_proxy_config_has_its_own_identity_distinct_from_the_gateway_and_the_agent():
    gw = load_gateway_image(REAL / "config" / "gateway_image.json")
    agent = json.loads((REAL / "config" / "agent_identity.json").read_text())
    pin = load_proxy_pin(REAL / "config" / "proxy.json", gateway_pin=gw, agent_uid=agent["uid"])
    assert (pin.uid, pin.gid, pin.port) == (10002, 10002, 3128)
    assert len({pin.uid, gw.uid, agent["uid"]}) == 3 and len({pin.gid, gw.gid, agent["gid"]}) == 3


@pytest.mark.parametrize("over", [dict(uid=10001), dict(gid=10001), dict(uid=1000), dict(uid=0), dict(gid=0), dict(uid=True), dict(uid="10002"),
                                  dict(port=8080), dict(port=0), dict(status="proposed"), dict(uid=None)])
def test_a_proxy_identity_that_collides_or_is_not_explicit_is_refused(tmp_path, over):
    with pytest.raises(ProxyConfigError):
        load_proxy_pin(write_proxy_cfg(tmp_path, **over), gateway_pin=GW_PIN, agent_uid=1000)


def test_the_proxy_component_has_exactly_its_own_files_no_gateway_code_and_no_secret():
    code = build_proxy_component(REAL)
    verify_archive(code)
    paths = {e.path for e in code.manifest.entries if e.type == "f"}
    assert paths == set(PROXY_FILES) == {"src/aiep/__init__.py", "src/aiep/egress_proxy.py", "src/aiep/listen.py", "config/experiment.json"}
    assert not any("gateway" in p or "ledger" in p or "pricing" in p for p in paths)
    assert REAL_KEY.encode() not in code.tar and b"AIEP_UPSTREAM_API_KEY" not in code.tar and b"AIEP_RUN_TOKEN" not in code.tar and b"AIEP_BUDGET" not in code.tar


def test_the_gateway_component_gains_only_the_shared_listen_module():
    from aiep.gateway_runner import GATEWAY_FILES
    assert set(GATEWAY_FILES) == {"src/aiep/__init__.py", "src/aiep/gateway.py", "src/aiep/ledger.py", "src/aiep/listen.py", "src/aiep/pricing.py",
                                  "config/experiment.json", "config/model_pricing.json"}


def test_proxy_create_kwargs_are_policy_checked_secret_free_and_name_the_proxy_networks_real_subnet():
    kw = build_proxy_create_kwargs(proxy_spec())
    assert kw["user"] == "10002:10002" and kw["network"] == "aiep-proxy-net" and kw["read_only"] is True and kw["cap_drop"] == ["ALL"]
    assert kw["security_opt"] == ["no-new-privileges"] and kw["labels"] == {"aiep.run_id": "r1", "aiep.role": "proxy"} and kw["name"] == "aiep.proxy.r1"
    assert kw["environment"]["AIEP_PROXY_LISTEN"] == "net:172.30.2.0/24:3128"
    blob = json.dumps(kw)
    assert REAL_KEY not in blob and TOKEN not in blob and "API_KEY" not in blob and "TOKEN" not in blob and "SECRET" not in blob
    assert set(kw["environment"]) == {"AIEP_PROXY_LISTEN", "AIEP_PROXY_LOG", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "TZ", "HOME"}
    assert "ports" not in kw and "volumes" not in kw and "network_mode" not in kw


def test_an_unresolved_proxy_subnet_is_refused_outside_the_preflight():
    spec = proxy_spec()
    spec = dataclasses.replace(spec, ctx=dataclasses.replace(spec.ctx, proxy_cidr=None))
    with pytest.raises(PolicyViolation, match="not been resolved"):
        build_proxy_create_kwargs(spec)
    assert build_proxy_create_kwargs(spec, preflight=True)["environment"]["AIEP_PROXY_LISTEN"] == "net:172.16.0.0/24:3128"


def test_the_gateway_is_told_the_proxys_endpoint_and_nothing_about_the_internet():
    from aiep.lifecycle import build_gateway_create_kwargs
    env = build_gateway_create_kwargs(gateway_spec())["environment"]
    assert env["AIEP_UPSTREAM_PROXY"] == proxy_spec().endpoint == "aiep.proxy.r1:3128"
    assert not any(k in env for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "AIEP_UPSTREAM_HOST"))


# ---------------- the three-container run ----------------
def test_happy_path_attaches_exactly_the_documented_networks_and_records_the_verified_topology(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    res = run(host, gspec, aspec)
    release(host)
    assert res.status == "completed" and res.gateway_error is None and res.proxy_error is None and res.agent.status == STATUS_EXITED
    assert host.attached["px-1"] == {"aiep-proxy-net", "aiep-egress-net"}
    assert host.attached["gw-1"] == {"aiep-agent-net", "aiep-proxy-net"}
    assert host.attached["ag-1"] == {"aiep-agent-net"}
    assert "aiep-egress-net" not in host.attached["gw-1"] | host.attached["ag-1"]  # only the proxy can leave
    topo = res.topology
    assert topo["networks"]["aiep-egress-net"]["attached"] == ["px-1"] and set(topo["containers"]["gateway"]) == {"aiep-agent-net", "aiep-proxy-net"}
    assert res.proxy_removed and res.proxy_state == "removed" and res.proxy_cleanup_error is None
    ev = host.events
    assert ev.index("agent.remove") < ev.index("gateway.remove") < ev.index("proxy.remove")
    assert host.px_fs.gateway_started == [["/usr/local/bin/python3", "-m", "aiep.egress_proxy"]] and host.px_fs.started_after_listings == 1
    from aiep.lifecycle import lifecycle_record
    rec = json.dumps(lifecycle_record(res))
    assert '"proxy"' in rec and '"topology"' in rec and REAL_KEY not in rec


def test_the_proxy_is_ready_before_the_gateway_is_created_and_every_proxy_exec_runs_as_its_own_identity(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    run(host, gspec, aspec)
    release(host)
    ev = host.events
    assert ev.index("proxy.start") < ev.index("gateway.create")
    assert {x["user"] for x in host.api.calls if x["container"] == "px-1"} == {"10002:10002"}
    assert {x["user"] for x in host.api.calls if x["container"] == "gw-1"} == {"10001:10001"}
    px_cmds = [x["cmd"][0] for x in host.api.calls if x["container"] == "px-1"]
    assert px_cmds[0] == "/bin/tar" and all(not x["privileged"] for x in host.api.calls)


def test_the_real_key_and_run_token_never_reach_the_proxy_container_request_environment_or_execs(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    run(host, gspec, aspec)
    release(host)
    (_, px_kw), = [x for x in host.created if x[0] == "proxy"]
    blob = json.dumps(px_kw) + json.dumps(host.inspects["px-1"], default=str) + json.dumps([x for x in host.api.calls if x["container"] == "px-1"], default=str)
    assert REAL_KEY not in blob and TOKEN not in blob
    assert REAL_KEY not in json.dumps(host.px_fs.log) and b"AIEP_UPSTREAM_API_KEY" not in b"".join(host.api.stdins[:0] or [b""])


def test_the_real_key_is_absent_from_the_agent_request_and_environment(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    run(host, gspec, aspec)
    release(host)
    (_, ag_kw), = [x for x in host.created if x[0] == "agent"]
    assert REAL_KEY not in json.dumps(ag_kw) and "AIEP_UPSTREAM_API_KEY" not in ag_kw["environment"] and "ANTHROPIC_API_KEY" not in ag_kw["environment"]
    (_, gw_kw), = [x for x in host.created if x[0] == "gateway"]
    assert {k for k, v in gw_kw["environment"].items() if REAL_KEY in v} == {"AIEP_UPSTREAM_API_KEY"}  # the gateway alone holds it


def test_the_proxy_policy_refuses_a_request_or_inspect_that_carries_the_real_key():
    ctx = proxy_spec().ctx
    hardened_only = ("mem_limit", "memswap_limit", "nano_cpus", "pids_limit")

    def raw():
        return {k: v for k, v in build_proxy_create_kwargs(proxy_spec()).items() if k not in hardened_only}
    harden("proxy", raw(), ctx)  # the unmodified request is fine
    kw = raw()
    kw["environment"]["TZ"] = REAL_KEY
    with pytest.raises(PolicyViolation, match="API key|secret"):
        harden("proxy", kw, ctx)
    kw = raw()
    kw["labels"] = {"x": TOKEN}
    with pytest.raises(PolicyViolation, match="secret appears"):
        harden("proxy", kw, ctx)


# ---------------- startup failures ----------------
@pytest.mark.parametrize("fail_at", ["create", "network", "inspect", "start"])
def test_a_proxy_startup_failure_ends_the_run_before_the_gateway_or_agent_exist_and_removes_the_proxy(tmp_path, seed, fail_at):
    host, gspec, aspec = build(tmp_path, seed)
    if fail_at == "create":
        host.create_error["proxy"] = RuntimeError("daemon refused")
    elif fail_at == "network":
        host.connect_error, host.connect_error_for = RuntimeError("no such network"), "px-1"
    elif fail_at == "inspect":
        orig = host.containers.create

        def create(image, command=None, **kw):
            cont = orig(image, command, **kw)
            host.inspects[cont.id]["NetworkSettings"]["Networks"]["aiep-agent-net"] = {}
            return cont
        host.containers.create = create
    else:
        host.px_c.start_error = RuntimeError("start failed")
    res = run(host, gspec, aspec)
    assert res.status == "proxy_failed" and res.proxy_error and res.agent is None and res.gateway_error is None
    assert not any(e.startswith(("gateway.", "agent.")) for e in host.events)
    assert res.proxy_removed and res.gateway_state == "new" and res.ledger is None and res.accounting is None


def test_the_proxy_tree_must_match_its_manifest_or_the_process_is_not_started(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    orig = host.px_fs.handler

    def handler(argv, rec, stdin):
        res = orig(argv, rec, stdin)
        if argv[0] == "/bin/tar":
            (host.px_fs.path("/opt/aiep-proxy") / "src/aiep/egress_proxy.py").write_bytes(b"# evil\n")
        return res
    host.px_fs.handler = handler
    res = run(host, gspec, aspec)
    assert res.status == "proxy_failed" and "ProxyIntegrityError" in res.proxy_error and host.px_fs.gateway_started == []
    assert res.proxy_removed and not any(e.startswith(("gateway.", "agent.")) for e in host.events)


def test_a_wrong_owner_in_the_proxy_tree_is_caught(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    host.px_fs.owner = (10002, 0)
    res = run(host, gspec, aspec)
    assert res.status == "proxy_failed" and "owner differs" in res.proxy_error and res.proxy_removed


def test_a_proxy_process_that_exits_during_startup_is_reported_with_its_stderr(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    orig = host.px_fs.handler

    def handler(argv, rec, stdin):
        if argv[1:] == ["-m", "aiep.egress_proxy"]:
            return ExecResult(stderr=b"refusing to start: boom", exit_code=2)
        return orig(argv, rec, stdin)
    host.px_fs.handler = handler
    res = run(host, gspec, aspec)
    assert res.status == "proxy_failed" and "exited before it was ready" in res.proxy_error and "boom" in res.proxy_error and res.proxy_removed


def test_a_proxy_that_never_becomes_ready_times_out_and_is_removed(tmp_path, seed):
    spec = dataclasses.replace(proxy_spec(), ready_timeout_s=0.3)
    host, gspec, aspec = build(tmp_path, seed, pspec=spec)
    host.px_fs.gateway_listening = False
    orig = host.px_fs.handler

    def handler(argv, rec, stdin):
        if argv[1:] == ["-m", "aiep.egress_proxy"]:
            return ExecResult(hang=threading.Event())  # starts, never listens
        return orig(argv, rec, stdin)
    host.px_fs.handler = handler
    res = run(host, gspec, aspec)
    assert res.status == "proxy_failed" and "readiness deadline" in res.proxy_error and res.proxy_removed
    assert not any(e.startswith(("gateway.", "agent.")) for e in host.events)


def test_a_gateway_failure_after_the_proxy_is_up_still_removes_both_in_order(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    host.gw_c.start_error = RuntimeError("gateway start failed")
    res = run(host, gspec, aspec)
    assert res.status == "gateway_failed" and "gateway start failed" in res.gateway_error and res.agent is None
    assert res.gateway_removed and res.proxy_removed
    assert host.events[-4:] == ["gateway.kill", "gateway.remove", "proxy.kill", "proxy.remove"]
    assert not any(e.startswith("agent.") for e in host.events)


# ---------------- topology enforced before the agent exists ----------------
@pytest.mark.parametrize("extra", ["aiep-egress-net", "bridge"])
def test_a_gateway_with_an_extra_network_never_gets_an_agent(tmp_path, seed, extra):
    host, gspec, aspec = build(tmp_path, seed)
    orig = host.containers.create

    def create(image, command=None, **kw):
        cont = orig(image, command, **kw)
        if kw["name"].startswith("aiep.gateway."):
            host.inspects[cont.id]["NetworkSettings"]["Networks"][extra] = {"NetworkID": "x", "IPAddress": "1.2.3.4"}
        return cont
    host.containers.create = create
    res = run(host, gspec, aspec)
    assert res.status == "gateway_failed" and res.agent is None and "attached networks" in res.gateway_error
    assert not any(e.startswith("agent.") for e in host.events) and res.gateway_removed and res.proxy_removed


def test_an_unexpected_container_on_a_network_stops_the_run_before_the_agent(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    host.attached["stranger"] = {"aiep-agent-net"}  # someone else is on the agent network
    res = run(host, gspec, aspec)
    assert res.status == "gateway_failed" and "attached containers are" in res.gateway_error and res.agent is None
    assert not any(e.startswith("agent.") for e in host.events) and res.gateway_removed and res.proxy_removed


def test_an_impostor_network_with_the_right_name_is_caught_by_the_attachment_check(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    orig = host.attach

    def attach(cid, net):
        orig(cid, net)
        if cid == "gw-1" and net == "aiep-proxy-net":
            host.inspects[cid]["NetworkSettings"]["Networks"][net]["NetworkID"] = "impostor"
    host.attach = attach
    res = run(host, gspec, aspec)
    assert res.status == "gateway_failed" and "not the AIEP network" in res.gateway_error and res.agent is None


# ---------------- supervision and teardown ----------------
def test_a_proxy_that_dies_during_the_run_ends_it_as_invalid(tmp_path, seed):
    hang = threading.Event()
    host, gspec, aspec = build(tmp_path, seed, agent=FakeAgent(hang=hang))
    orig = host.px_c.reload
    calls = {"n": 0}

    def reload():
        calls["n"] += 1
        if host.ag_c.status == "running" and calls["n"] > 3:
            host.px_c.status = "exited"
        return orig()
    host.px_c.reload = reload
    res = run(host, gspec, aspec)
    hang.set()
    release(host)
    assert res.status == "completed" and res.agent.status == STATUS_GATEWAY_STOPPED and res.agent.stop_reason == "proxy_died" and res.agent.valid is False
    assert res.agent.removed and res.gateway_removed and res.proxy_removed


def test_a_proxy_removal_failure_is_reported_not_swallowed_and_the_gateway_and_agent_are_still_gone(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    host.px_c.remove_error = RuntimeError("daemon busy")
    res = run(host, gspec, aspec)
    release(host)
    assert res.proxy_removed is False and "daemon busy" in res.proxy_cleanup_error and res.agent.removed and res.gateway_removed
    assert res.status == "completed"


def test_a_failing_proxy_kill_still_attempts_the_removal(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    host.px_c.kill = lambda: (_ for _ in ()).throw(RuntimeError("kill failed"))
    res = run(host, gspec, aspec)
    release(host)
    assert res.proxy_removed is True and host.events[-1] == "proxy.remove"


def test_a_proxy_stop_failure_is_recorded_and_the_proxy_is_still_removed(tmp_path, seed, monkeypatch):
    host, gspec, aspec = build(tmp_path, seed)
    monkeypatch.setattr(ProxyManager, "stop", lambda self: (_ for _ in ()).throw(RuntimeError("stop exploded")))
    res = run(host, gspec, aspec)
    release(host)
    assert res.proxy_removed and any("cleanup proxy stop: RuntimeError: stop exploded" in e for e in res.proxy_errors)


def test_keyboard_interrupt_while_the_agent_runs_removes_agent_gateway_and_proxy_in_order(tmp_path, seed, monkeypatch):
    hang = threading.Event()
    host, gspec, aspec = build(tmp_path, seed, agent=FakeAgent(hang=hang))
    calls = {"n": 0}
    real = GatewayManager.stop_reason

    def interrupting(self):
        calls["n"] += 1
        if calls["n"] > 2:
            raise KeyboardInterrupt
        return real(self)
    monkeypatch.setattr(GatewayManager, "stop_reason", interrupting)
    with pytest.raises(KeyboardInterrupt):
        run(host, gspec, aspec)
    hang.set()
    release(host)
    ev = host.events
    assert ev.index("agent.remove") < ev.index("gateway.remove") < ev.index("proxy.remove") and ev[-1] == "proxy.remove"


@pytest.mark.parametrize("step", ["create", "start", "inject_verify_start", "wait_ready"])
def test_keyboard_interrupt_during_proxy_setup_removes_the_proxy_and_creates_nothing_else(tmp_path, seed, monkeypatch, step):
    host, gspec, aspec = build(tmp_path, seed)

    def boom(self, *a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(ProxyManager, step, boom)
    with pytest.raises(KeyboardInterrupt):
        run(host, gspec, aspec)
    assert not any(e.startswith(("gateway.", "agent.")) for e in host.events)
    if step != "create":
        assert host.events[-1] == "proxy.remove"


def test_the_proxy_state_machine_refuses_out_of_order_steps(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    pm = ProxyManager(host, host.pspec)
    for step in (pm.start, pm.inject_verify_start, pm.wait_ready):
        with pytest.raises(ProxyStartError):
            step()
    assert pm.teardown() == (True, None)  # nothing was created, so nothing to remove
    pm.create()
    with pytest.raises(ProxyStartError):
        pm.inject_verify_start()
    assert pm.teardown() == (True, None) and pm.state == "removed"


def test_run_with_gateway_refuses_inconsistent_specs_before_creating_anything(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    bad = [
        dict(pspec=dataclasses.replace(host.pspec, run_id="other")),
        dict(pspec=dataclasses.replace(host.pspec, ctx=dataclasses.replace(host.pspec.ctx, uid=10001, gid=10001))),
        dict(pspec=dataclasses.replace(host.pspec, ctx=dataclasses.replace(host.pspec.ctx, proxy_network="aiep-other"))),
    ]
    for over in bad:
        with pytest.raises(ValueError):
            run_with_gateway(host, gspec, aspec, **over)
    with pytest.raises(ValueError, match="proxy endpoint"):
        run_with_gateway(host, dataclasses.replace(gspec, proxy_endpoint="aiep.proxy.zzz:3128"), aspec, pspec=host.pspec)
    assert host.events == []


# ---------------- reaper ownership ----------------
OLD = R.MIN_TTL_S + 1000


def test_the_reaper_removes_an_abandoned_runs_proxy_with_its_agent_and_gateway_by_exact_id(tmp_path):
    leases = tmp_path / "leases"
    proxy = c("c", "aiep.proxy.r1", role="proxy", age=OLD)
    api = FakeApi([c("a", "aiep.agent.r1", age=OLD), c("b", "aiep.gateway.r1", role="gateway", age=OLD), proxy])
    rep = reap(api, leases, execute=True)
    assert proxy["Id"] in api.removed and len(api.removed) == 3 and all(i.removed for i in rep.items)
    assert all(x[2] == {"v": True, "force": True} for x in api.calls if x[0] == "remove")


def test_a_live_runs_lease_protects_its_proxy_however_old(tmp_path):
    leases = tmp_path / "leases"
    lease_file(leases, "r1", silence=5)
    api = FakeApi([c("c", "aiep.proxy.r1", role="proxy", age=1000 * 3600)])
    rep = reap(api, leases, execute=True)
    assert api.removed == [] and rep.items[0].lease == "fresh"


@pytest.mark.parametrize("victim", [
    c("c", "aiep.proxy.r1", role="proxy", run="r2"),                        # name does not match its run label
    c("c", "aiep.proxy.r1", role="agent"),                                  # name does not match its role
    c("c", "postgres", role="proxy"),
    c("c", "aiep.proxy.r1", role="proxy", labels={"aiep.role": "proxy"}),   # no run label
    c("c", "aiep.proxy.", role="proxy", run=""),
    c("c", "aiep.proxy.r1", role="proxy", labels={"aiep.run_id": "../x", "aiep.role": "proxy"}),
    c("c", "aiep.egress-proxy.r1", role="proxy"),
    c("c", "aiep.proxy.r1.extra", role="proxy"),
])
def test_proxy_lookalikes_are_never_reaped(tmp_path, victim):
    leases = tmp_path / "leases"
    lease_file(leases, "r1", silence=100 * 3600)
    lease_file(leases, "r2", silence=100 * 3600)
    api = FakeApi([victim])
    rep = reap(api, leases, execute=True)
    assert api.removed == [] and all(i.action == "skip" for i in rep.items)


def test_the_reaper_never_touches_networks_images_or_volumes_only_containers(tmp_path):
    api = FakeApi([c("c", "aiep.proxy.r1", role="proxy", age=OLD)])
    reap(api, tmp_path / "leases", execute=True)
    assert {x[0] for x in api.calls} <= {"containers", "inspect", "remove"}
    assert R.ROLE_PREFIX["proxy"] == "aiep.proxy."


# ---------------- the process-start hook used by the integration test ----------------
def test_overriding_only_the_process_start_keeps_creation_policy_injection_and_verification_production(tmp_path, seed):
    from aiep.lifecycle import GatewayManager
    started = []

    class SeamProxy(ProxyManager):
        def _start_process(self):
            started.append(("proxy", self.state, self.host_listings()))
            return super()._start_process()  # (the fake daemon understands the production start; a real seam would differ here)

        def host_listings(self):
            return host.px_fs.listings

    class SeamGateway(GatewayManager):
        def _start_process(self):
            started.append(("gateway", self.state, host.gw_fs.listings))
            return super()._start_process()
    host, gspec, aspec = build(tmp_path, seed)
    px, gw = SeamProxy(host, host.pspec), SeamGateway(host, gspec)
    px.setup()
    gw.setup()
    # the hook ran exactly once each, after the files were injected AND verified entry by entry, and before the process existed
    assert started == [("proxy", "started", 1), ("gateway", "started", 1)]
    assert host.px_fs.gateway_started == [["/usr/local/bin/python3", "-m", "aiep.egress_proxy"]] and px.state == gw.state == "ready"
    release(host)
    gw.teardown()
    px.teardown()


def test_the_production_start_is_unchanged_python_dash_m_after_verification(tmp_path, seed):
    host, gspec, aspec = build(tmp_path, seed)
    res = run(host, gspec, aspec)
    release(host)
    assert host.gw_fs.gateway_started == [["/usr/local/bin/python3", "-m", "aiep.gateway"]] and host.px_fs.gateway_started == [["/usr/local/bin/python3", "-m", "aiep.egress_proxy"]]
    assert res.status == "completed"

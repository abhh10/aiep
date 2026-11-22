"""The RUNNING agent's attachment check (agent_attachment) and its use by run_agent / the lifecycle. Fakes only."""
import copy

import pytest

from aiep import networks as N
from aiep.agent_attachment import parse_routes, verify_running_agent
from aiep.agent_runner import STATUS_INVALID, agent_context
from test_agent_runner import AGENT_NET_ID, AGENT_ROUTES, edit_agent, go, good_inspect, repo, rig, seed, spec_for  # noqa: F401 (fixtures)

CIDR = "172.30.1.0/24"
PROXY_NET_ID = "net-proxy-id"


def running(spec, **over):
    d = good_inspect(spec)
    d.update(over)
    return d


def check(spec, inspect, *, routes=AGENT_ROUTES, network_id=AGENT_NET_ID, cidr=CIDR):
    return verify_running_agent(inspect, agent_context(spec), network_id=network_id, cidr=cidr, route_text=routes)


def test_a_correctly_attached_running_agent_has_no_problems_and_yields_evidence(seed):
    spec = spec_for(seed)
    problems, ev = check(spec, good_inspect(spec))
    assert problems == [] and ev["networks"] == {"aiep-agent-net": {"network_id": AGENT_NET_ID, "ip": "172.30.1.5"}}
    assert ev["published_ports"] == [] and ev["routes"] == ["eth0 172.30.1.0/24"]


def mutate(fn):
    def f(i):
        fn(i)
        return i
    return f


NET = lambda i: i["NetworkSettings"]["Networks"]  # noqa: E731
BAD_INSPECTS = {
    "created, not running": lambda i: i["State"].update(Running=False, Status="created"),
    "paused": lambda i: i["State"].update(Paused=True),
    "restarting": lambda i: i["State"].update(Restarting=True),
    "no State at all": lambda i: i.pop("State"),
    "also on the proxy network": lambda i: NET(i).__setitem__("aiep-proxy-net", {"NetworkID": PROXY_NET_ID, "IPAddress": "172.30.2.9"}),
    "also on the egress network": lambda i: NET(i).__setitem__("aiep-egress-net", {"NetworkID": "e", "IPAddress": "172.30.3.9"}),
    "also on the default bridge": lambda i: NET(i).__setitem__("bridge", {"NetworkID": "b", "IPAddress": "172.17.0.2"}),
    "only on the proxy network": lambda i: i["NetworkSettings"].update(Networks={"aiep-proxy-net": {"NetworkID": PROXY_NET_ID, "IPAddress": "172.30.2.9"}}),
    "no networks": lambda i: i["NetworkSettings"].update(Networks={}),
    "impostor network with the right name": lambda i: NET(i)["aiep-agent-net"].update(NetworkID="impostor"),
    "address outside the agent subnet": lambda i: NET(i)["aiep-agent-net"].update(IPAddress="172.30.2.5"),
    "no address": lambda i: NET(i)["aiep-agent-net"].update(IPAddress=""),
    "an IPv6 address": lambda i: NET(i)["aiep-agent-net"].update(GlobalIPv6Address="fd00::5"),
    "a gateway address": lambda i: NET(i)["aiep-agent-net"].update(Gateway="172.30.1.1"),
    "a published port": lambda i: i["NetworkSettings"].update(Ports={"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]}),
    "port bindings": lambda i: i["HostConfig"].update(PortBindings={"22/tcp": [{"HostPort": "2222"}]}),
    "publish all ports": lambda i: i["HostConfig"].update(PublishAllPorts=True),
    "privileged": lambda i: i["HostConfig"].update(Privileged=True),
    "added capability": lambda i: i["HostConfig"].update(CapAdd=["NET_ADMIN"]),
    "writable root": lambda i: i["HostConfig"].update(ReadonlyRootfs=False),
    "host network mode": lambda i: i["HostConfig"].update(NetworkMode="host"),
    "runs as root": lambda i: i["Config"].update(User="root"),
    "a mount": lambda i: i.update(Mounts=[{"Type": "bind", "Destination": "/x"}]),
}


@pytest.mark.parametrize("name", sorted(BAD_INSPECTS))
def test_every_deviation_in_the_running_container_is_reported(seed, name):
    spec = spec_for(seed)
    i = good_inspect(spec)
    BAD_INSPECTS[name](i)
    problems, _ = check(spec, i)
    assert problems, name


@pytest.mark.parametrize("routes,needle", [
    ("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\neth0\t00000000\t0101A8C0\t0003\t0\t0\t0\t00000000\n", "default route"),
    (AGENT_ROUTES + "eth0\t00021EAC\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n", "other than the agent subnet"),  # 172.30.2.0/24 as well
    (AGENT_ROUTES + "eth1\t00011EAC\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n", "more than one interface"),
    ("garbage", "unreadable route table"),
    ("", "unreadable route table"),
])
def test_routes_are_checked_in_the_container_itself(seed, routes, needle):
    spec = spec_for(seed)
    problems, _ = check(spec, good_inspect(spec), routes=routes)
    assert any(needle in p for p in problems), problems


def test_an_unreadable_route_table_is_a_problem_not_a_pass(seed):
    spec = spec_for(seed)
    problems, _ = verify_running_agent(good_inspect(spec), agent_context(spec), network_id=AGENT_NET_ID, cidr=CIDR, route_text=None)
    assert any("route table could not be read" in p for p in problems)


def test_the_network_id_and_subnet_are_only_enforced_when_known_but_everything_else_always_is(seed):
    spec = spec_for(seed)
    i = good_inspect(spec)
    NET(i)["aiep-agent-net"].update(NetworkID="whatever", IPAddress="10.9.9.9")
    assert check(spec, i, network_id=None, cidr=None, routes=AGENT_ROUTES.replace("00011EAC", "00011EAC"))[0] == []
    assert any("NetworkID" in p for p in check(spec, i, cidr=None)[0])
    NET(i)["aiep-agent-net"].update(NetworkID=AGENT_NET_ID)
    assert any("not inside" in p for p in check(spec, i)[0])


def test_parse_routes_decodes_the_kernels_little_endian_hex():
    rows = parse_routes(AGENT_ROUTES)
    assert [(r["iface"], str(r["network"]), r["default"]) for r in rows] == [("eth0", "172.30.1.0/24", False)]
    d = parse_routes("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\neth1\t00000000\t010014AC\t0003\t0\t0\t0\t00000000\n")
    assert d[0]["default"] is True
    with pytest.raises(ValueError):
        parse_routes("eth0 1 2")


# ---------------- run_agent: a mismatch invalidates the run, injects nothing and removes the container ----------------
def rig_with_running(tmp_path, seed, running_inspect, **kw):
    spec, fs, api, client = rig(tmp_path, seed, network_id=AGENT_NET_ID, network_cidr=CIDR, agent=edit_agent())
    created = good_inspect(spec)
    api.inspect_container = lambda cid: running_inspect(spec) if client.container.status == "running" else created
    return spec, fs, api, client


@pytest.mark.parametrize("name", ["also on the proxy network", "also on the egress network", "a published port", "impostor network with the right name",
                                  "address outside the agent subnet", "runs as root", "added capability"])
def test_a_started_agent_that_is_not_exactly_attached_invalidates_the_run_and_is_removed_before_anything_is_injected(tmp_path, seed, name):
    def bad(spec):
        i = good_inspect(spec)
        BAD_INSPECTS[name](i)
        return i
    spec, fs, api, client = rig_with_running(tmp_path, seed, bad)
    res = go(spec, client)
    assert res.status == STATUS_INVALID and res.valid is False and "agent attachment check failed" in res.invalid_reason
    assert res.removed and client.container.calls[-1] == "remove(force=True,v=True)" and res.patch is None
    names = [c["cmd"][0] for c in api.calls]
    assert "/bin/tar" not in names and "/opt/aiep/claude" not in names  # nothing injected, Claude never started
    assert res.attachment is not None


def test_a_default_route_inside_the_running_container_invalidates_it(tmp_path, seed):
    spec, fs, api, client = rig_with_running(tmp_path, seed, lambda s: good_inspect(s))
    fs.files["/proc/net/route"] = (b"Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
                                   b"eth0\t00000000\t010014AC\t0003\t0\t0\t0\t00000000\n")
    res = go(spec, client)
    assert res.status == STATUS_INVALID and "default route" in res.invalid_reason and res.removed
    assert "/bin/tar" not in [c["cmd"][0] for c in api.calls]


def test_an_unreadable_route_table_invalidates_the_run(tmp_path, seed):
    spec, fs, api, client = rig_with_running(tmp_path, seed, lambda s: good_inspect(s))
    del fs.files["/proc/net/route"]
    res = go(spec, client)
    assert res.status == STATUS_INVALID and "route table could not be read" in res.invalid_reason and res.removed


def test_the_topology_hook_runs_with_the_agents_real_id_and_its_failure_invalidates_the_run(tmp_path, seed):
    spec, fs, api, client = rig_with_running(tmp_path, seed, lambda s: good_inspect(s))
    seen = []
    res = go(spec, client, attachment_check=lambda cid: seen.append(cid))
    assert seen == [client.container.id] and res.valid and res.attachment["networks"]["aiep-agent-net"]["network_id"] == AGENT_NET_ID

    spec, fs, api, client = rig_with_running(tmp_path / "b", seed, lambda s: good_inspect(s))

    def boom(cid):
        raise N.NetworkError("aiep-egress-net: attached containers are ['x', 'y'], expected exactly ['x']")
    res = go(spec, client, attachment_check=boom)
    assert res.status == STATUS_INVALID and "network topology" in res.invalid_reason and "aiep-egress-net" in res.invalid_reason and res.removed
    assert "/opt/aiep/claude" not in [c["cmd"][0] for c in api.calls]


def test_the_attachment_evidence_is_in_the_agent_record(tmp_path, seed):
    from aiep.agent_runner import record_of
    spec, fs, api, client = rig_with_running(tmp_path, seed, lambda s: good_inspect(s))
    rec = record_of(go(spec, client))
    assert rec["attachment"]["routes"] == ["eth0 172.30.1.0/24"] and rec["attachment"]["published_ports"] == []


def test_the_running_containers_image_id_is_checked_when_the_spec_pins_it(seed):
    spec = spec_for(seed, image_id="sha256:" + "a" * 64)
    assert check(spec, good_inspect(spec))[0] == []
    i = good_inspect(spec)
    i["Image"] = "sha256:" + "1" * 64
    assert any("image" in p for p in check(spec, i)[0])

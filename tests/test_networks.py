import pytest

from aiep import networks as N


SUBNETS = {N.AGENT_NETWORK: "172.30.1.0/24", N.PROXY_NETWORK: "172.30.2.0/24", N.EGRESS_NETWORK: "172.30.3.0/24"}


class NotFound(Exception):
    pass


class FakeNet:
    def __init__(self, host, attrs):
        self.host, self.attrs = host, attrs
        self.id = attrs["Id"]
        self.removed = False

    def reload(self):
        self.attrs = self.host.store[self.attrs["Name"]]

    def remove(self):
        self.host.calls.append(("remove", self.attrs["Name"]))
        del self.host.store[self.attrs["Name"]]


class FakeNetworks:
    def __init__(self):
        self.store, self.calls = {}, []

    def get(self, name):
        if name not in self.store:
            raise NotFound(name)
        return FakeNet(self, self.store[name])

    def create(self, **kw):
        self.calls.append(("create", kw))
        name = kw["name"]
        attrs = {"Name": name, "Id": "id-" + name, "Driver": kw["driver"], "Scope": "local", "Internal": kw["internal"],
                 "Attachable": kw["attachable"], "Ingress": False, "EnableIPv6": kw["enable_ipv6"], "Labels": kw["labels"],
                 "Options": kw.get("options", {}), "Containers": {},
                 "IPAM": {"Config": [{"Subnet": SUBNETS[name]}]} if kw["internal"] else {"Config": [{"Subnet": SUBNETS[name], "Gateway": "172.30.3.1"}]}}
        self.store[name] = attrs
        return FakeNet(self, attrs)


class Client:
    def __init__(self):
        self.networks = FakeNetworks()


def test_the_agent_network_is_internal_and_the_egress_network_is_not():
    a, e = N.network_kwargs(N.AGENT_NETWORK), N.network_kwargs(N.EGRESS_NETWORK)
    assert a["internal"] is True and e["internal"] is False
    assert a["driver"] == e["driver"] == "bridge" and a["attachable"] is False and e["attachable"] is False
    assert a["enable_ipv6"] is False and e["enable_ipv6"] is False
    assert a["labels"]["aiep.managed"] == "true" and a["labels"]["aiep.role"] == "agent-network" and e["labels"]["aiep.role"] == "egress-network"
    assert e["options"] == {"com.docker.network.bridge.enable_icc": "false"}
    assert a["options"] == {"com.docker.network.bridge.inhibit_ipv4": "true"}  # no VM-side address on the internal bridge
    assert set(a) == {"name", "driver", "internal", "attachable", "enable_ipv6", "labels", "options", "check_duplicate"}  # nothing else is ever passed


def test_ensure_creates_all_three_verifies_them_and_is_idempotent():
    c = Client()
    out = N.ensure_networks(c)
    assert set(out) == {N.AGENT_NETWORK, N.PROXY_NETWORK, N.EGRESS_NETWORK} and [x[0] for x in c.networks.calls] == ["create"] * 3
    N.ensure_networks(c)  # second call adopts the verified networks and creates nothing
    assert [x[0] for x in c.networks.calls] == ["create"] * 3


@pytest.mark.parametrize("field,value", [("Internal", False), ("Driver", "overlay"), ("Attachable", True), ("EnableIPv6", True),
                                         ("Labels", {}), ("Scope", "swarm"), ("Ingress", True),
                                         ("Options", {"com.docker.network.bridge.host_binding_ipv4": "0.0.0.0"})])
def test_an_existing_network_that_differs_is_an_error_and_is_never_modified(field, value):
    c = Client()
    c.networks.store[N.AGENT_NETWORK] = {"Name": N.AGENT_NETWORK, "Id": "x", "Driver": "bridge", "Scope": "local", "Internal": True,
                                        "Attachable": False, "Ingress": False, "EnableIPv6": False,
                                        "Options": {"com.docker.network.bridge.inhibit_ipv4": "true"},
                                        "Labels": {"aiep.managed": "true", "aiep.role": "agent-network"}, "Containers": {}}
    c.networks.store[N.AGENT_NETWORK][field] = value
    with pytest.raises(N.NetworkError, match="does not match policy"):
        N.ensure_networks(c)
    assert c.networks.calls == []


def test_the_agent_network_must_have_no_vm_side_address():
    base = N.network_kwargs(N.AGENT_NETWORK)
    ok = {"Name": N.AGENT_NETWORK, "Driver": "bridge", "Scope": "local", "Internal": True, "Labels": base["labels"], "Options": base["options"],
          "IPAM": {"Config": [{"Subnet": "172.21.0.0/16"}]}}
    assert N.verify_network(ok, N.AGENT_NETWORK) == []
    assert any("inhibit_ipv4" in x for x in N.verify_network({**ok, "Options": {}}, N.AGENT_NETWORK))  # the previously created network shape
    assert any("gateway address" in x for x in N.verify_network({**ok, "IPAM": {"Config": [{"Subnet": "172.19.0.0/16", "Gateway": "172.19.0.1"}]}},
                                                                  N.AGENT_NETWORK))


def test_the_egress_network_must_have_icc_off_and_masquerade_on():
    base = N.network_kwargs(N.EGRESS_NETWORK)
    ok = {"Name": N.EGRESS_NETWORK, "Driver": "bridge", "Scope": "local", "Internal": False, "Labels": base["labels"], "Options": base["options"]}
    assert N.verify_network(ok, N.EGRESS_NETWORK) == []
    assert N.verify_network({**ok, "Options": {}}, N.EGRESS_NETWORK)
    assert N.verify_network({**ok, "Options": {**base["options"], "com.docker.network.bridge.enable_ip_masquerade": "false"}}, N.EGRESS_NETWORK)


def test_only_aiep_networks_can_be_created_or_removed():
    with pytest.raises(N.NetworkError):
        N.network_kwargs("bridge")
    with pytest.raises(N.NetworkError):
        N.remove_networks(Client(), ("bridge",))
    assert N.verify_network({"Name": "bridge"}, "bridge")


def test_remove_only_removes_empty_labelled_aiep_networks():
    c = Client()
    N.ensure_networks(c)
    c.networks.store[N.AGENT_NETWORK]["Containers"] = {"abc": {}}
    with pytest.raises(N.NetworkError, match="still attached"):
        N.remove_networks(c)
    assert not [x for x in c.networks.calls if x[0] == "remove"]
    c.networks.store[N.AGENT_NETWORK]["Containers"] = {}
    assert N.remove_networks(c) == ["id-" + N.EGRESS_NETWORK, "id-" + N.PROXY_NETWORK, "id-" + N.AGENT_NETWORK]
    assert N.remove_networks(c) == []  # already gone: nothing to do


def test_a_same_named_network_without_aiep_labels_is_never_removed():
    c = Client()
    N.ensure_networks(c)
    c.networks.store[N.EGRESS_NETWORK]["Labels"] = {}
    with pytest.raises(N.NetworkError, match="AIEP labels"):
        N.remove_networks(c, (N.EGRESS_NETWORK,))
    assert N.EGRESS_NETWORK in c.networks.store


# ---------------- the proxy network and the run-time pre-flight / topology checks ----------------
def test_the_proxy_network_is_internal_with_no_vm_side_address_and_icc_on():
    p = N.network_kwargs(N.PROXY_NETWORK)
    assert p["internal"] is True and p["options"] == {"com.docker.network.bridge.inhibit_ipv4": "true"} and p["labels"]["aiep.role"] == "proxy-network"
    ok = {"Name": N.PROXY_NETWORK, "Driver": "bridge", "Scope": "local", "Internal": True, "Labels": p["labels"], "Options": p["options"],
          "IPAM": {"Config": [{"Subnet": "172.21.0.0/16"}]}}
    assert N.verify_network(ok, N.PROXY_NETWORK) == []
    assert any("Internal" in x for x in N.verify_network({**ok, "Internal": False}, N.PROXY_NETWORK))
    assert any("inhibit_ipv4" in x for x in N.verify_network({**ok, "Options": {}}, N.PROXY_NETWORK))
    assert any("disabled" in x for x in N.verify_network({**ok, "Options": {**p["options"], "com.docker.network.bridge.enable_icc": "false"}}, N.PROXY_NETWORK))
    assert any("AIEP labels" in x for x in N.verify_network({**ok, "Labels": {**p["labels"], "aiep.role": "agent-network"}}, N.PROXY_NETWORK))


def test_each_network_has_exactly_the_documented_role_attachments():
    assert N.ROLE_NETWORKS == {"agent": {N.AGENT_NETWORK}, "gateway": {N.AGENT_NETWORK, N.PROXY_NETWORK}, "proxy": {N.PROXY_NETWORK, N.EGRESS_NETWORK}}
    assert N.EGRESS_NETWORK not in N.ROLE_NETWORKS["gateway"] and N.EGRESS_NETWORK not in N.ROLE_NETWORKS["agent"]
    assert N.PROXY_NETWORK not in N.ROLE_NETWORKS["agent"]


def _ready_client():
    c = Client()
    N.ensure_networks(c)
    return c


def test_verify_run_networks_returns_ids_and_the_real_subnets_and_creates_nothing():
    c = _ready_client()
    n_calls = len(c.networks.calls)
    out = N.verify_run_networks(c, N.AGENT_NETWORK, N.PROXY_NETWORK, N.EGRESS_NETWORK)
    assert out.ids == {n: "id-" + n for n in N.ALL_NETWORKS}
    assert out.subnets == {N.AGENT_NETWORK: SUBNETS[N.AGENT_NETWORK], N.PROXY_NETWORK: SUBNETS[N.PROXY_NETWORK]}
    assert len(c.networks.calls) == n_calls


@pytest.mark.parametrize("names", [("bridge", N.PROXY_NETWORK, N.EGRESS_NETWORK), (N.AGENT_NETWORK, "bridge", N.EGRESS_NETWORK),
                                   (N.AGENT_NETWORK, N.PROXY_NETWORK, "host"), (N.PROXY_NETWORK, N.AGENT_NETWORK, N.EGRESS_NETWORK),
                                   (N.AGENT_NETWORK, N.EGRESS_NETWORK, N.PROXY_NETWORK)])
def test_verify_run_networks_refuses_other_names_and_swapped_roles(names):
    with pytest.raises(N.NetworkError, match="must use the networks"):
        N.verify_run_networks(_ready_client(), *names)


@pytest.mark.parametrize("name,patch,needle", [
    (N.PROXY_NETWORK, {"Internal": False}, "Internal"),
    (N.AGENT_NETWORK, {"Internal": False}, "Internal"),
    (N.EGRESS_NETWORK, {"Internal": True}, "Internal"),
    (N.EGRESS_NETWORK, {"Options": {}}, "inter-container"),
    (N.PROXY_NETWORK, {"Containers": {"stray": {}}}, "already attached"),
    (N.PROXY_NETWORK, {"IPAM": {"Config": []}}, "exactly one subnet"),
    (N.PROXY_NETWORK, {"IPAM": {"Config": [{"Subnet": "8.8.8.0/24"}]}}, "unusable subnet"),
    (N.PROXY_NETWORK, {"IPAM": {"Config": [{"Subnet": SUBNETS[N.AGENT_NETWORK]}]}}, "overlap"),
])
def test_verify_run_networks_inspects_actual_properties_not_names(name, patch, needle):
    c = _ready_client()
    c.networks.store[name].update(patch)
    with pytest.raises(N.NetworkError, match=needle):
        N.verify_run_networks(c, N.AGENT_NETWORK, N.PROXY_NETWORK, N.EGRESS_NETWORK)


class TopologyClient(Client):
    """Networks plus containers whose attachments can be set; ``api.inspect_container`` answers per container."""

    def __init__(self):
        super().__init__()
        N.ensure_networks(self)
        self.containers = {}
        client = self

        class Api:
            def inspect_container(_, cid):
                return client.containers[cid]
        self.api = Api()

    def attach(self, role_id, networks, *, network_id=None):
        nets = {}
        for n in networks:
            attrs = self.networks.store[n]
            attrs["Containers"][role_id] = {}
            nets[n] = {"NetworkID": network_id or attrs["Id"], "IPAddress": "172.30.9.2"}
        self.containers[role_id] = {"NetworkSettings": {"Networks": nets}}


def _topology(**over):
    c = TopologyClient()
    c.attach("px", [N.PROXY_NETWORK, N.EGRESS_NETWORK])
    c.attach("gw", [N.AGENT_NETWORK, N.PROXY_NETWORK])
    for k, v in over.items():
        v(c)
    return c


def test_verify_topology_accepts_the_exact_attachments_and_returns_evidence():
    c = _topology()
    ev = N.verify_topology(c, {"proxy": "px", "gateway": "gw"})
    assert ev["networks"][N.EGRESS_NETWORK]["attached"] == ["px"] and ev["networks"][N.AGENT_NETWORK]["attached"] == ["gw"]
    assert set(ev["containers"]["gateway"]) == {N.AGENT_NETWORK, N.PROXY_NETWORK}
    c.attach("ag", [N.AGENT_NETWORK])
    N.verify_topology(c, {"proxy": "px", "gateway": "gw", "agent": "ag"})  # with the agent on the agent network only


def test_the_gateway_on_the_egress_network_is_refused():
    c = _topology()
    c.attach("gw", [N.AGENT_NETWORK, N.PROXY_NETWORK, N.EGRESS_NETWORK])
    with pytest.raises(N.NetworkError) as e:
        N.verify_topology(c, {"proxy": "px", "gateway": "gw"})
    assert "gateway: attached to" in str(e.value) and "aiep-egress-net: attached containers" in str(e.value)


def test_the_agent_on_the_proxy_or_egress_network_is_refused():
    for extra in (N.PROXY_NETWORK, N.EGRESS_NETWORK):
        c = _topology()
        c.attach("ag", [N.AGENT_NETWORK, extra])
        with pytest.raises(N.NetworkError, match="agent: attached to"):
            N.verify_topology(c, {"proxy": "px", "gateway": "gw", "agent": "ag"})


def test_the_proxy_off_the_egress_network_or_a_gateway_off_the_proxy_network_is_refused():
    c = _topology()
    c.attach("px", [N.PROXY_NETWORK])
    with pytest.raises(N.NetworkError, match="proxy: attached to"):
        N.verify_topology(c, {"proxy": "px", "gateway": "gw"})
    c = _topology()
    c.networks.store[N.PROXY_NETWORK]["Containers"].pop("gw")
    c.attach("gw", [N.AGENT_NETWORK])
    with pytest.raises(N.NetworkError, match="gateway: attached to"):
        N.verify_topology(c, {"proxy": "px", "gateway": "gw"})


def test_a_stray_container_on_any_network_is_refused():
    for net in N.ALL_NETWORKS:
        c = _topology()
        c.networks.store[net]["Containers"]["stranger"] = {}
        with pytest.raises(N.NetworkError, match="attached containers are"):
            N.verify_topology(c, {"proxy": "px", "gateway": "gw"})


def test_an_attachment_to_an_impostor_network_with_the_same_name_is_refused():
    c = _topology()
    c.attach("gw", [N.AGENT_NETWORK, N.PROXY_NETWORK], network_id="impostor")
    with pytest.raises(N.NetworkError, match="not the AIEP network"):
        N.verify_topology(c, {"proxy": "px", "gateway": "gw"})


def test_topology_requires_every_network_to_still_match_policy_and_an_ip_per_attachment():
    c = _topology()
    c.networks.store[N.EGRESS_NETWORK]["Options"] = {}
    with pytest.raises(N.NetworkError, match="inter-container"):
        N.verify_topology(c, {"proxy": "px", "gateway": "gw"})
    c = _topology()
    c.containers["gw"]["NetworkSettings"]["Networks"][N.AGENT_NETWORK]["IPAddress"] = ""
    with pytest.raises(N.NetworkError, match="no IPv4 address"):
        N.verify_topology(c, {"proxy": "px", "gateway": "gw"})


def test_topology_rejects_unknown_roles_and_unreadable_containers():
    c = _topology()
    with pytest.raises(N.NetworkError, match="unknown container role"):
        N.verify_topology(c, {"evaluator": "x"})
    with pytest.raises(N.NetworkError, match="cannot inspect the gateway"):
        N.verify_topology(c, {"proxy": "px", "gateway": "missing"})


def test_the_egress_subnet_must_not_overlap_an_internal_one():
    c = _ready_client()
    c.networks.store[N.EGRESS_NETWORK]["IPAM"] = {"Config": [{"Subnet": "172.30.2.0/25"}]}  # inside the proxy network's 172.30.2.0/24
    with pytest.raises(N.NetworkError, match="overlap"):
        N.verify_run_networks(c, N.AGENT_NETWORK, N.PROXY_NETWORK, N.EGRESS_NETWORK)

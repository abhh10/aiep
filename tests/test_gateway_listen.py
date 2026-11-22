"""AIEP_LISTEN: ``net:<cidr>:<port>`` binds exactly the interface inside the intended Docker network, plus loopback; everything else is refused."""
import pytest

from aiep import gateway as G
from aiep import listen as L
from aiep.lifecycle import build_gateway_create_kwargs

# /proc/net/route of a gateway on two INTERNAL networks (no default route at all): eth0 = agent network, eth1 = proxy network
INTERNAL_ONLY = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
eth0\t0001 1EAC\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0
""".replace("0001 1EAC", "00011EAC")
# a proxy: eth0 = proxy network (no default route), eth1 = egress network (carries the default route)
PROXY_ROUTE = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
eth1\t00000000\t010014AC\t0003\t0\t0\t0\t00000000\t0\t0\t0
eth0\t00021EAC\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0
"""
GW_ADDRS = {"eth0": "172.30.1.3", "eth1": "172.30.2.3", "lo": "127.0.0.1"}
PX_ADDRS = {"eth0": "172.30.2.2", "eth1": "172.20.0.2", "lo": "127.0.0.1"}
NAMES = ["eth0", "eth1", "lo"]


def parse(value, addrs=GW_ADDRS, route=INTERNAL_ONLY, names=NAMES):
    return L.parse_listen(value, route_text=route, interfaces=names, addr_of=addrs.get)


def test_the_gateway_binds_only_its_agent_network_interface_and_loopback():
    hosts, port = parse("net:172.30.1.0/24:8080")
    assert hosts == ["172.30.1.3", "127.0.0.1"] and port == 8080
    assert "172.30.2.3" not in hosts and "0.0.0.0" not in hosts  # the proxy-network interface is never bound


def test_the_proxy_binds_only_its_proxy_network_interface_never_the_egress_one():
    hosts, port = parse("net:172.30.2.0/24:3128", PX_ADDRS, PROXY_ROUTE)
    assert hosts == ["172.30.2.2", "127.0.0.1"] and port == 3128 and "172.20.0.2" not in hosts


def test_naming_the_egress_network_is_refused_because_that_interface_carries_the_default_route():
    with pytest.raises(ValueError, match="default route"):
        parse("net:172.20.0.0/16:3128", PX_ADDRS, PROXY_ROUTE)


def test_no_interface_in_the_network_fails_closed_instead_of_falling_back_to_a_wildcard():
    with pytest.raises(ValueError, match="no interface"):
        parse("net:172.20.5.0/24:8080")
    with pytest.raises(ValueError, match="no interface"):
        L.parse_listen("net:172.30.1.0/24:8080", route_text=INTERNAL_ONLY, interfaces=["eth0"], addr_of=lambda n: None)


def test_two_interfaces_inside_the_named_network_are_refused():
    with pytest.raises(ValueError, match="exactly one"):
        parse("net:172.30.0.0/16:8080")  # both eth0 (172.30.1.3) and eth1 (172.30.2.3) are inside 172.30.0.0/16


@pytest.mark.parametrize("value", [
    "0.0.0.0:8080", "::0:8080", ":8080", "*:8080", "[::]:8080", "127.0.0.1:8080", "172.30.1.3:8080", "internal:8080", "internal", "8080", "",
    "localhost:8080", "host.example:8080", "net:172.30.1.0/24", "net:172.30.1.0/24:0", "net:172.30.1.0/24:65536", "net:172.30.1.0/24:80x",
    "net:172.30.1.0/24:-1", "net:172.30.1.0/24:", "net:0.0.0.0/0:8080", "net:127.0.0.0/8:8080", "net:127.0.0.1/32:8080",
    "net:169.254.0.0/16:8080", "net:169.254.169.254/32:8080", "net:224.0.0.0/4:8080", "net:8.8.8.0/24:8080", "net:100.64.0.0/10:8080",
    "net:172.30.1.1/24:8080", "net:172.30.1.0/31:8080", "net:172.30.1.0/32:8080", "net:172.0.0.0/7:8080", "net:172.30.1.0/024:8080",
    "net:172.30.1.0/255.255.255.0:8080", "net:::1/128:8080", "net:fd00::/8:8080", "net:172.30.1.0/24:8080:9", " net:172.30.1.0/24:8080",
    "net:172.30.1.0/24:08080x", "net:１７２.30.1.0/24:8080",
])
def test_wildcards_loopback_bare_addresses_and_malformed_values_are_refused(value):
    with pytest.raises(ValueError):
        parse(value)


def test_default_route_interfaces_are_found_from_a_real_table():
    assert L.default_route_interfaces(PROXY_ROUTE) == {"eth1"}
    assert L.default_route_interfaces(INTERNAL_ONLY) == set()
    assert L.default_route_interfaces("") == set()


def test_the_gateway_module_re_exports_the_shared_listen_rules_it_no_longer_duplicates():
    assert G.parse_listen is L.parse_listen and G.default_route_interfaces is L.default_route_interfaces
    assert not hasattr(G, "internal_listen_addresses") and not hasattr(G, "LISTEN_INTERNAL")


def test_main_refuses_an_invalid_listen_value_before_starting(monkeypatch, capsys):
    for k, v in {"AIEP_UPSTREAM_API_KEY": "k" * 30, "AIEP_RUN_TOKEN": "t", "AIEP_BUDGET_USD": "1", "AIEP_ALLOWED_HOSTS": "h:1",
                 "AIEP_LISTEN": "0.0.0.0:8080", "AIEP_LEDGER_PATH": "/nonexistent/ledger", "AIEP_UPSTREAM_PROXY": "aiep.proxy.r:3128"}.items():
        monkeypatch.setenv(k, v)
    assert G.main([]) == 2 and "invalid AIEP_LISTEN" in capsys.readouterr().err


def test_main_refuses_to_start_without_a_proxy_there_is_no_direct_fallback(monkeypatch, capsys, tmp_path):
    for k, v in {"AIEP_UPSTREAM_API_KEY": "k" * 30, "AIEP_RUN_TOKEN": "t", "AIEP_BUDGET_USD": "1", "AIEP_ALLOWED_HOSTS": "h:1",
                 "AIEP_LISTEN": "net:172.30.1.0/24:8080", "AIEP_LEDGER_PATH": str(tmp_path / "l")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AIEP_UPSTREAM_PROXY", raising=False)
    assert G.main([]) == 2 and "AIEP_UPSTREAM_PROXY" in capsys.readouterr().err
    monkeypatch.setenv("AIEP_UPSTREAM_PROXY", "no-port")
    monkeypatch.setattr(G, "parse_listen", lambda v: (["127.0.0.1"], 9))
    assert G.main([]) == 2 and "invalid AIEP_UPSTREAM_PROXY" in capsys.readouterr().err


def test_the_orchestrator_asks_for_the_agent_subnet_never_a_wildcard():
    from test_lifecycle import gateway_spec
    env = build_gateway_create_kwargs(gateway_spec())["environment"]
    assert env["AIEP_LISTEN"] == "net:172.30.1.0/24:8080" and env["AIEP_UPSTREAM_PROXY"] == "aiep.proxy.r1:3128"


def test_an_unresolved_subnet_is_never_defaulted_outside_the_preflight():
    import dataclasses
    from aiep.container_policy import PolicyViolation
    from test_lifecycle import gateway_spec
    spec = gateway_spec()
    spec = dataclasses.replace(spec, ctx=dataclasses.replace(spec.ctx, agent_cidr=None))
    with pytest.raises(PolicyViolation, match="not been resolved"):
        build_gateway_create_kwargs(spec)
    assert build_gateway_create_kwargs(spec, preflight=True)["environment"]["AIEP_LISTEN"] == "net:172.16.0.0/24:8080"


def test_the_upstream_override_is_a_python_argument_only_never_an_environment_setting():
    import inspect
    sig = inspect.signature(G.main)
    assert sig.parameters["upstream"].kind is inspect.Parameter.KEYWORD_ONLY and sig.parameters["upstream"].default is None
    src = inspect.getsource(G.main)
    assert "UPSTREAM_HOST" not in src and "env.get(\"AIEP_UPSTREAM" not in src  # no environment variable can name another upstream HOST
    assert {k for k in ("AIEP_UPSTREAM_API_KEY", "AIEP_UPSTREAM_PROXY") if k in src} == {"AIEP_UPSTREAM_API_KEY", "AIEP_UPSTREAM_PROXY"}


def test_python_dash_m_gateway_uses_the_experiment_upstream_host_through_the_proxy(monkeypatch, tmp_path):
    seen = {}

    class Stop(Exception):
        pass

    def spy(self, cfg):
        seen["upstream"] = cfg.upstream
        raise Stop

    monkeypatch.setattr(G.Gateway, "__init__", spy)
    monkeypatch.setattr(G, "parse_listen", lambda v: (["127.0.0.1"], 9))
    for k, v in {"AIEP_UPSTREAM_API_KEY": "k" * 30, "AIEP_RUN_TOKEN": "t", "AIEP_BUDGET_USD": "1", "AIEP_ALLOWED_HOSTS": "h:1",
                 "AIEP_LISTEN": "net:172.30.1.0/24:9", "AIEP_LEDGER_PATH": str(tmp_path / "ledger.jsonl"),
                 "AIEP_UPSTREAM_PROXY": "aiep.proxy.r1:3128"}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(Stop):
        G.main([])
    up = seen["upstream"]
    assert up.host == "api.anthropic.com" and up.ca_file is None and up.allow_private_addresses is False and up.resolver is None and up.port == 443
    assert (up.proxy_host, up.proxy_port) == ("aiep.proxy.r1", 3128) and up.require_proxy is True

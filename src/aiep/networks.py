"""The three AIEP Docker networks: creation, verification against ``docker inspect``, attachment checks, and exact-name removal.

Topology (see ``lifecycle.py``)::

    agent --- [aiep-agent-net: internal] --- gateway --- [aiep-proxy-net: internal] --- proxy --- [aiep-egress-net: bridge] --- internet

* ``aiep-agent-net``  INTERNAL. Members: the agent and the gateway. No default route, no masquerade, and (``inhibit_ipv4``) no address for the
  bridge on the Docker VM side, so neither member can reach the internet, the LAN, the VM or the macOS host.
* ``aiep-proxy-net``  INTERNAL, same properties. Members: the gateway and the proxy. The gateway therefore has NO network that leads anywhere
  except to the proxy.
* ``aiep-egress-net`` an ordinary bridge with outbound connectivity and inter-container communication OFF. Member: the proxy only.

The gateway (which holds the real key) is on the two internal networks only. The proxy (which holds no secret) is the only container with a
route out, and it is a restricted CONNECT proxy (``aiep.egress_proxy``). The agent is on the agent network only and never on the proxy or
egress network. Internal networks are a necessary condition, not a proof: the isolation is measured by ``scripts/m2_runtime_validation.py``.

Policy: networks are created once with fixed names and labels, VERIFIED field by field against ``inspect``, and never modified. An existing
network of the same name that differs in any checked field is an error, never adopted or "fixed". ``verify_topology`` checks the ACTUAL
attachments (container IDs in the network's ``Containers``, and each container's ``NetworkID`` per network), not just names. Removal only
touches a network whose name AND labels are AIEP's and which has no containers attached.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .listen import validate_cidr

AGENT_NETWORK = "aiep-agent-net"
PROXY_NETWORK = "aiep-proxy-net"
EGRESS_NETWORK = "aiep-egress-net"
ALL_NETWORKS = (AGENT_NETWORK, PROXY_NETWORK, EGRESS_NETWORK)
INTERNAL_NETWORKS = frozenset({AGENT_NETWORK, PROXY_NETWORK})
LABEL_MANAGED = "aiep.managed"
LABEL_ROLE = "aiep.role"
ROLES = {AGENT_NETWORK: "agent-network", PROXY_NETWORK: "proxy-network", EGRESS_NETWORK: "egress-network"}
# Which networks each container role may be attached to, exactly (no more, no fewer).
ROLE_NETWORKS = {
    "agent": frozenset({AGENT_NETWORK}),
    "gateway": frozenset({AGENT_NETWORK, PROXY_NETWORK}),
    "proxy": frozenset({PROXY_NETWORK, EGRESS_NETWORK}),
}
_INHIBIT = "com.docker.network.bridge.inhibit_ipv4"
_ICC = "com.docker.network.bridge.enable_icc"


class NetworkError(RuntimeError):
    """A network is missing, differs from policy, or cannot be removed safely."""


def network_kwargs(name: str) -> dict[str, Any]:
    """The ``networks.create`` arguments for one AIEP network. Nothing else is ever passed."""
    if name not in ROLES:
        raise NetworkError(f"{name!r} is not an AIEP network")
    labels = {LABEL_MANAGED: "true", LABEL_ROLE: ROLES[name], "aiep.schema": "1"}
    if name in INTERNAL_NETWORKS:
        # inhibit_ipv4: the bridge gets NO address on the VM side. Measured: without it the agent could connect to TCP ports of
        # the Docker Desktop VM (111 and another) through the internal bridge's own gateway address, because "internal" only removes
        # the default route and masquerade, not the VM's presence on the segment. The proxy network gets the same treatment so the
        # gateway cannot reach the VM through it either.
        return dict(name=name, driver="bridge", internal=True, attachable=False, enable_ipv6=False, labels=labels,
                    options={_INHIBIT: "true"}, check_duplicate=True)
    # egress: outbound allowed; container-to-container traffic on it is off (the proxy is its only member), so a stray
    # container that joins it cannot reach the proxy's egress-side address
    return dict(name=name, driver="bridge", internal=False, attachable=False, enable_ipv6=False, labels=labels,
                options={_ICC: "false"}, check_duplicate=True)


def verify_network(inspect: Mapping[str, Any], name: str) -> list[str]:
    """Problems found in an inspected network (empty means it matches policy exactly)."""
    if name not in ROLES:
        return [f"{name!r} is not an AIEP network"]
    want_internal = name in INTERNAL_NETWORKS
    p: list[str] = []
    if inspect.get("Name") != name:
        p.append(f"name is {inspect.get('Name')!r}")
    if inspect.get("Driver") != "bridge":
        p.append(f"driver is {inspect.get('Driver')!r}, not bridge")
    if inspect.get("Scope") != "local":
        p.append(f"scope is {inspect.get('Scope')!r}, not local")
    if bool(inspect.get("Internal")) is not want_internal:
        p.append(f"Internal is {inspect.get('Internal')!r}, must be {want_internal}")
    if inspect.get("Attachable"):
        p.append("network is attachable (any container could join it)")
    if inspect.get("Ingress"):
        p.append("network is an ingress network")
    if inspect.get("EnableIPv6"):
        p.append("IPv6 is enabled")
    labels = inspect.get("Labels") or {}
    if labels.get(LABEL_MANAGED) != "true" or labels.get(LABEL_ROLE) != ROLES[name]:
        p.append("AIEP labels are missing or wrong")
    opts = inspect.get("Options") or {}
    if want_internal and opts.get(_INHIBIT) != "true":
        p.append(f"the {name} bridge has an address on the Docker VM (inhibit_ipv4 is not set), so VM services are reachable")
    if want_internal:
        for cfg in (inspect.get("IPAM") or {}).get("Config") or []:
            if cfg.get("Gateway"):
                p.append(f"{name} has a gateway address {cfg['Gateway']}")
    if name == EGRESS_NETWORK and opts.get(_ICC) != "false":
        p.append("inter-container communication is not disabled on the egress network")
    if want_internal and opts.get(_ICC) == "false":
        p.append(f"inter-container communication is disabled on {name}, so its two members could not talk")
    if opts.get("com.docker.network.bridge.enable_ip_masquerade") == "false" and not want_internal:
        p.append("the egress network does not masquerade, so the proxy could not reach the upstream")
    if opts.get("com.docker.network.bridge.host_binding_ipv4") not in (None, ""):
        p.append("host binding options are set")
    return p


def network_subnet(inspect: Mapping[str, Any]) -> str:
    """The single IPv4 subnet of an inspected network. Raises NetworkError if there is not exactly one valid private one."""
    subnets = [c.get("Subnet") for c in (inspect.get("IPAM") or {}).get("Config") or [] if c.get("Subnet")]
    if len(subnets) != 1:
        raise NetworkError(f"network {inspect.get('Name')!r} must have exactly one subnet, has {subnets}")
    try:
        return str(validate_cidr(subnets[0]))
    except ValueError as e:
        raise NetworkError(f"network {inspect.get('Name')!r} has an unusable subnet {subnets[0]!r}: {e}") from e


def ensure_networks(client: Any) -> dict[str, dict[str, Any]]:
    """Create the missing AIEP networks and verify all of them. Returns ``name -> inspect``. Never modifies an existing network."""
    out: dict[str, dict[str, Any]] = {}
    for name in ALL_NETWORKS:
        try:
            net = client.networks.get(name)
        except Exception as e:
            if type(e).__name__ != "NotFound":
                raise NetworkError(f"cannot look up network {name}: {type(e).__name__}: {e}") from e
            net = client.networks.create(**network_kwargs(name))
        net.reload()
        problems = verify_network(net.attrs, name)
        if problems:
            raise NetworkError(f"network {name} ({net.id}) does not match policy: {'; '.join(problems)}")
        out[name] = net.attrs
    return out


def _require_disjoint(subnets: Mapping[str, str]) -> None:
    import ipaddress
    nets = {n: ipaddress.ip_network(c) for n, c in subnets.items()}
    names = sorted(nets)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if nets[a].overlaps(nets[b]):
                raise NetworkError(f"the subnets of {a} ({nets[a]}) and {b} ({nets[b]}) overlap")


@dataclass(frozen=True)
class RunNetworks:
    """What the run-time pre-flight found: each network's ID and the subnets of the two internal ones (read from Docker, never assumed)."""

    ids: dict[str, str]
    subnets: dict[str, str] = field(default_factory=dict)


def verify_run_networks(client: Any, agent_network: str, proxy_network: str, egress_network: str) -> RunNetworks:
    """The pre-flight of every run: all three networks must be the AIEP networks AND, as they exist right now, match policy exactly.

    Names alone prove nothing (a same-named non-internal network, or ``bridge``, would pass a name check), so each network is inspected
    through the Docker API and run through ``verify_network``. Nothing is created or modified here: a missing or off-policy network is an
    error, and no container exists yet when this raises. A network that already has containers attached is refused as well, because anything
    else attached to an AIEP network could be reached from it. The internal networks' subnets must be single, private and disjoint.
    """
    if (agent_network, proxy_network, egress_network) != (AGENT_NETWORK, PROXY_NETWORK, EGRESS_NETWORK):
        raise NetworkError(f"a run must use the networks {AGENT_NETWORK!r}, {PROXY_NETWORK!r} and {EGRESS_NETWORK!r}, "
                           f"not {agent_network!r}, {proxy_network!r} and {egress_network!r}")
    ids: dict[str, str] = {}
    subnets: dict[str, str] = {}
    egress_attrs: dict[str, Any] = {}
    for name in ALL_NETWORKS:
        try:
            net = client.networks.get(name)
            net.reload()
            attrs = net.attrs
        except Exception as e:
            raise NetworkError(f"cannot inspect network {name}: {type(e).__name__}: {e}") from e
        problems = verify_network(attrs, name)
        if attrs.get("Containers"):
            problems.append(f"containers are already attached: {sorted(attrs['Containers'])}")
        if name in INTERNAL_NETWORKS:
            try:
                subnets[name] = network_subnet(attrs)
            except NetworkError as e:
                problems.append(str(e))
        if problems:
            raise NetworkError(f"network {name} does not match policy: {'; '.join(problems)}")
        ids[name] = str(net.id)
        if name == EGRESS_NETWORK:
            egress_attrs = attrs
    # the egress network's subnet (any address space) must not overlap the internal ones either, or routes inside the proxy would be ambiguous
    egress_cfg = [c.get("Subnet") for c in (egress_attrs.get("IPAM") or {}).get("Config") or [] if c.get("Subnet")]
    _require_disjoint({**subnets, **({EGRESS_NETWORK: egress_cfg[0]} if len(egress_cfg) == 1 else {})})
    return RunNetworks(ids, subnets)


def verify_topology(client: Any, containers: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """Check the ACTUAL attachments of the run's live containers against ``ROLE_NETWORKS``. ``containers`` maps role -> container ID.

    For every AIEP network: it still matches policy, and the container IDs Docker lists as attached to it are EXACTLY those of the given
    containers whose role allows it (nothing else is attached, nothing is missing). For every container: the networks it reports are
    exactly its role's, and each one's ``NetworkID`` is the ID of the real AIEP network of that name (a same-named impostor fails) and has an
    IPv4 address. Raises ``NetworkError`` listing every problem; returns the evidence (per-network inspect summaries and per-container
    attachments) for the run record.
    """
    unknown = sorted(set(containers) - set(ROLE_NETWORKS))
    if unknown:
        raise NetworkError(f"unknown container role(s): {unknown}")
    problems: list[str] = []
    net_ids: dict[str, str] = {}
    evidence: dict[str, Any] = {"networks": {}, "containers": {}}
    for name in ALL_NETWORKS:
        try:
            net = client.networks.get(name)
            net.reload()
            attrs = net.attrs
        except Exception as e:
            raise NetworkError(f"cannot inspect network {name}: {type(e).__name__}: {e}") from e
        net_ids[name] = str(attrs.get("Id", net.id))
        problems += [f"{name}: {x}" for x in verify_network(attrs, name)]
        want = {cid for role, cid in containers.items() if name in ROLE_NETWORKS[role]}
        have = set((attrs.get("Containers") or {}).keys())
        if have != want:
            problems.append(f"{name}: attached containers are {sorted(have)}, expected exactly {sorted(want)}")
        evidence["networks"][name] = {"id": net_ids[name], "internal": bool(attrs.get("Internal")), "attached": sorted(have)}
    for role, cid in containers.items():
        try:
            info = client.api.inspect_container(cid)
        except Exception as e:
            raise NetworkError(f"cannot inspect the {role} container: {type(e).__name__}: {e}") from e
        nets = (info.get("NetworkSettings") or {}).get("Networks") or {}
        if set(nets) != set(ROLE_NETWORKS[role]):
            problems.append(f"{role}: attached to {sorted(nets)}, must be exactly {sorted(ROLE_NETWORKS[role])}")
        for name, entry in nets.items():
            if name in net_ids and str(entry.get("NetworkID")) != net_ids[name]:
                problems.append(f"{role}: its {name} attachment is not the AIEP network (NetworkID differs)")
            if not entry.get("IPAddress"):
                problems.append(f"{role}: no IPv4 address on {name}")
        evidence["containers"][role] = {n: {"network_id": e.get("NetworkID"), "ip": e.get("IPAddress")} for n, e in nets.items()}
    if problems:
        raise NetworkError("; ".join(problems))
    return evidence


def remove_networks(client: Any, names: tuple[str, ...] = (EGRESS_NETWORK, PROXY_NETWORK, AGENT_NETWORK)) -> list[str]:
    """Remove the named AIEP networks if (and only if) they are AIEP-labelled and empty. Returns the removed network IDs.

    Two phases: every named network is checked first and nothing is removed if any check fails.
    """
    found = []
    for name in names:
        if name not in ROLES:
            raise NetworkError(f"refusing to remove {name!r}: not an AIEP network")
        try:
            net = client.networks.get(name)
        except Exception as e:
            if type(e).__name__ == "NotFound":
                continue
            raise
        net.reload()
        attrs = net.attrs
        labels = attrs.get("Labels") or {}
        if attrs.get("Name") != name or labels.get(LABEL_MANAGED) != "true" or labels.get(LABEL_ROLE) != ROLES[name]:
            raise NetworkError(f"refusing to remove {name}: it does not carry the AIEP labels")
        if attrs.get("Containers"):
            raise NetworkError(f"refusing to remove {name}: containers are still attached ({sorted(attrs['Containers'])})")
        found.append(net)
    removed: list[str] = []
    for net in found:
        net.remove()
        removed.append(net.id)
    return removed

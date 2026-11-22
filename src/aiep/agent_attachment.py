"""Post-start verification of a RUNNING agent container: what Docker says about it now, not what was asked for at create time.

``verify_running_agent`` takes the ``docker inspect`` of the started container and the text of its ``/proc/net/route`` (read inside it) and returns
the problems it finds, plus evidence for the run record. It requires:

* the container is running (not created, paused, restarting or dead);
* exactly ONE network attachment, the agent network: no proxy network, no egress network, no bridge, nothing else;
* that attachment's ``NetworkID`` is the ID of the AIEP agent network the pre-flight verified, and the container's IPv4 address is inside that
  network's real subnet (no IPv6, no secondary addresses);
* no published ports (``NetworkSettings.Ports``, ``HostConfig.PortBindings``, ``PublishAllPorts``);
* no default route, and every route in the container's own table is the agent network's subnet (nothing else is routable from inside);
* the complete agent policy again, applied to the RUNNING container (user, read-only root, capabilities, security options, limits, tmpfs, shm and IPC,
  environment pins, image ID, no secret).

The caller invalidates the run and tears everything down on any problem. Pure functions: unit-tested without Docker, exercised for real by
``scripts/m2_egress_validation.py``.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Mapping

from .container_policy import AgentContext, PolicyViolation, verify_inspect


def parse_routes(route_text: str) -> list[dict[str, Any]]:
    """Rows of ``/proc/net/route`` as {iface, network (IPv4Network), default (bool)}. Raises ValueError for a malformed table."""
    lines = route_text.strip().splitlines()
    if not lines or not lines[0].startswith("Iface"):
        raise ValueError("not a /proc/net/route table")
    rows = []
    for line in lines[1:]:
        f = line.split()
        if len(f) < 8:
            raise ValueError(f"malformed route line {line!r}")
        dest = ipaddress.IPv4Address(int(f[1], 16).to_bytes(4, "little"))
        mask = ipaddress.IPv4Address(int(f[7], 16).to_bytes(4, "little"))
        net = ipaddress.IPv4Network(f"{dest}/{mask}", strict=False)
        rows.append({"iface": f[0], "network": net, "default": net.prefixlen == 0})
    return rows


def verify_running_agent(inspect: Mapping[str, Any], ctx: AgentContext, *, network_id: str | None, cidr: str | None,
                         route_text: str | None) -> tuple[list[str], dict[str, Any]]:
    problems: list[str] = []
    evidence: dict[str, Any] = {}
    state = inspect.get("State") or {}
    if state.get("Running") is not True or state.get("Paused") or state.get("Restarting") or state.get("Dead"):
        problems.append(f"the container is not simply running (State: Running={state.get('Running')!r}, Status={state.get('Status')!r})")
    try:  # the whole agent policy, on the running container
        verify_inspect("agent", inspect, ctx)
    except PolicyViolation as e:
        problems.append(f"policy: {e}")

    ns = inspect.get("NetworkSettings") or {}
    nets = ns.get("Networks") or {}
    if set(nets) != {ctx.agent_network}:
        problems.append(f"attached to {sorted(nets)}, must be exactly [{ctx.agent_network!r}] (never the proxy or egress network)")
    entry = nets.get(ctx.agent_network) or {}
    evidence["networks"] = {n: {"network_id": e.get("NetworkID"), "ip": e.get("IPAddress")} for n, e in nets.items()}
    if network_id is not None and entry.get("NetworkID") != network_id:
        problems.append(f"its {ctx.agent_network} attachment has NetworkID {entry.get('NetworkID')!r}, not the verified network {network_id!r}")
    try:
        ip = ipaddress.IPv4Address(entry.get("IPAddress") or "")
        if cidr is not None and ip not in ipaddress.IPv4Network(cidr):
            problems.append(f"IPv4 address {ip} is not inside the agent network's subnet {cidr}")
    except ValueError:
        problems.append(f"no valid IPv4 address on {ctx.agent_network} ({entry.get('IPAddress')!r})")
    if entry.get("GlobalIPv6Address") or ns.get("GlobalIPv6Address") or entry.get("SecondaryIPAddresses") or ns.get("SecondaryIPAddresses"):
        problems.append("the container has an IPv6 or secondary address")
    if entry.get("Gateway"):
        problems.append(f"the attachment has a gateway address {entry['Gateway']!r} (the agent network must have none)")

    ports = {k: v for k, v in (ns.get("Ports") or {}).items() if v}
    host = inspect.get("HostConfig") or {}
    if ports or host.get("PortBindings") or host.get("PublishAllPorts"):
        problems.append(f"published ports: {sorted(ports) or host.get('PortBindings') or 'PublishAllPorts'}")
    evidence["published_ports"] = sorted(ports)

    if route_text is None:
        problems.append("the container's route table could not be read")
    else:
        try:
            rows = parse_routes(route_text)
        except ValueError as e:
            rows = []
            problems.append(f"unreadable route table: {e}")
        evidence["routes"] = [f"{r['iface']} {r['network']}" for r in rows]
        if any(r["default"] for r in rows):
            problems.append("the container has a default route")
        others = sorted({str(r["network"]) for r in rows if not r["default"]} - ({cidr} if cidr else set()))
        if cidr is not None and others:
            problems.append(f"routes to networks other than the agent subnet {cidr}: {others}")
        if len({r["iface"] for r in rows}) > 1:
            problems.append(f"routes on more than one interface: {sorted({r['iface'] for r in rows})}")
    return problems, evidence

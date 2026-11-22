"""``AIEP_LISTEN`` / ``AIEP_PROXY_LISTEN``: which addresses a container's server may bind. Stdlib only (injected into the gateway and the proxy).

A gateway or proxy container is attached to TWO Docker networks, and only one of them is where its clients live. Binding ``0.0.0.0`` would
also listen on the other one, and loopback alone would be unreachable. The only accepted value is therefore

    net:<ipv4 network in CIDR form>:<port>        e.g. net:172.19.0.0/16:8080

meaning "bind the one interface whose address is inside that network, plus loopback" (loopback is where the orchestrator's readiness probe
connects; it is not reachable from any other container). The CIDR is the real subnet of the intended Docker network, read by the orchestrator
from ``docker network inspect`` and checked again here against the interfaces that actually exist. Everything else is refused: wildcards,
bare or loopback-only addresses, a CIDR that is not a private non-loopback IPv4 network, no matching interface, more than one matching
interface, and an interface that carries the default route (that is the egress side).
"""

from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from typing import Callable

NET_PREFIX = "net:"
LOOPBACK = "127.0.0.1"
MIN_PREFIX, MAX_PREFIX = 8, 30


def default_route_interfaces(route_text: str) -> set[str]:
    """Interfaces that carry the IPv4 default route, from ``/proc/net/route`` text (destination and mask both zero)."""
    out: set[str] = set()
    for line in route_text.strip().splitlines()[1:]:
        f = line.split()
        if len(f) >= 8 and f[1] == "00000000" and f[7] == "00000000":
            out.add(f[0])
    return out


def validate_cidr(text: str) -> ipaddress.IPv4Network:
    """A private, ordinary IPv4 network of sensible size. Raises ValueError otherwise (never normalises)."""
    if not isinstance(text, str) or not text.isascii() or "/" not in text:
        raise ValueError("the listen network must be an IPv4 CIDR such as 172.19.0.0/16")
    net = ipaddress.IPv4Network(text, strict=True)  # host bits set -> ValueError
    if str(net) != text:
        raise ValueError("the listen network must be written in canonical form")
    if not (MIN_PREFIX <= net.prefixlen <= MAX_PREFIX):
        raise ValueError(f"the listen network prefix must be between /{MIN_PREFIX} and /{MAX_PREFIX}")
    if not net.is_private or net.is_loopback or net.is_link_local or net.is_multicast or net.is_unspecified or net.is_reserved:
        raise ValueError("the listen network must be a private, non-loopback, non-link-local network")
    return net


def split_listen(value: str) -> tuple[ipaddress.IPv4Network, int]:
    """Parse ``net:<cidr>:<port>`` into (network, port) WITHOUT looking at any interface. Raises ValueError for anything else."""
    if not isinstance(value, str) or not value.startswith(NET_PREFIX):
        raise ValueError("the listen value must be net:<ipv4 cidr>:<port> (wildcards, bare addresses and loopback are refused)")
    cidr, _, port_text = value[len(NET_PREFIX):].rpartition(":")
    if not port_text.isascii() or not port_text.isdigit() or not (0 < int(port_text) < 65536) or len(port_text) > 5:
        raise ValueError("the listen value must end in a valid port")
    return validate_cidr(cidr), int(port_text)


def _interface_ipv4(name: str) -> str | None:
    import fcntl
    import struct
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            packed = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))  # SIOCGIFADDR
        except OSError:
            return None
    return socket.inet_ntoa(packed[20:24])


def parse_listen(value: str, *, route_text: str | None = None, interfaces: list[str] | None = None,
                 addr_of: Callable[[str], str | None] = _interface_ipv4) -> tuple[list[str], int]:
    """``net:<cidr>:<port>`` -> (addresses to bind, port): the single interface inside the network, then loopback.

    Fails closed: no interface in the network, several of them, or one that carries the default route all raise ``ValueError``.
    """
    network, port = split_listen(value)
    if route_text is None:
        route_text = Path("/proc/net/route").read_text()
    names = interfaces if interfaces is not None else [n for _, n in socket.if_nameindex()]
    egress = default_route_interfaces(route_text)
    inside = []
    for name in names:
        if name == "lo":
            continue
        addr = addr_of(name)
        if addr and ipaddress.IPv4Address(addr) in network:
            inside.append((name, addr))
    if not inside:
        raise ValueError(f"no interface has an address inside {network} (refusing to fall back to a wildcard address)")
    if len(inside) != 1:
        raise ValueError(f"{len(inside)} interfaces have an address inside {network}; exactly one is required")
    name, addr = inside[0]
    if name in egress:
        raise ValueError(f"the interface inside {network} carries the default route: that is an egress interface, not an internal one")
    return [addr, LOOPBACK], port

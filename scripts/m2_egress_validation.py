#!/usr/bin/env python3
"""Real-Docker validation of the restricted egress architecture (approval-gated; AIEP-owned resources only).

    PYTHONPATH=src .venv/bin/python scripts/m2_egress_validation.py --evidence-dir <dir outside the repo> [--host-lan-ip A.B.C.D]

Topology under test: agent -- aiep-agent-net (internal) -- gateway -- aiep-proxy-net (internal) -- proxy -- aiep-egress-net -- internet.

It measures connectivity from inside every container (it never infers it from configuration), uses a fake API key and a local mock upstream, and
makes NO request to any Anthropic endpoint (one allowed CONNECT is opened to api.anthropic.com:443 and closed without sending a byte, to prove that
the permitted route exists; no TLS handshake and no HTTP request is made). It creates only containers labelled ``aiep.sprint=m2-egress`` (plus
the production-path containers named ``aiep.<role>.<run id>``), removes exactly the IDs it created in a ``finally``, and never touches any other
Docker resource. The only network it may create is the AIEP proxy network (and a temporary ``aiep-sprint-egress-mock-net``, removed at the end).
It does not run the reaper in execute mode.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import m2_runtime_validation as V  # noqa: E402  (reuses its Recorder, tracked cleanup, probe/exec/mock helpers; nothing here runs on import)
from aiep import reaper  # noqa: E402
from aiep.container_exec import ExecSession, inject_tar, list_tree  # noqa: E402
from aiep.gateway_runner import build_gateway_component, gateway_context, load_gateway_image  # noqa: E402
from aiep.lifecycle import GatewayManager, GatewaySpec, run_with_gateway  # noqa: E402
from aiep.networks import (AGENT_NETWORK, ALL_NETWORKS, EGRESS_NETWORK, PROXY_NETWORK, NetworkError, ensure_networks, verify_run_networks,  # noqa: E402
                           verify_topology)
from aiep.proxy_lifecycle import (PROXY_DEST, ProxyManager, ProxySpec, build_proxy_component, load_proxy_pin, proxy_context)  # noqa: E402
from aiep.seed import verify_listing  # noqa: E402

SPRINT = {"aiep.sprint": "m2-egress"}
FAKE_KEY = V.FAKE_KEY
TOKEN = V.DUMMY_TOKEN
PY = V.PY
GW_USER, PX_USER, AG_USER = "10001:10001", "10002:10002", "1000:1000"
HOST_GW = "192.168.65.254"
HOST_PORTS = [3306, 5000, 7000, 8828, 22617, 24692]  # the host listeners discovered earlier (3306 mysqld, 5000/7000 ControlCenter, 8828/24692 an IDE ...)
EXTRA_HOST_PORTS = [33060, 50176, 22, 80, 443, 445]
MOCK_NET = "aiep-sprint-egress-mock-net"
MOCK = V.MOCK_HOST  # the name the mock certificate is issued for
MOCK_PORT = 8443

PROBE = r'''
import errno, json, socket, sys, threading, time
TCP = %(tcp)s
DNS = %(dns)s
out = {"tcp": {}, "dns": {}}
sem = threading.Semaphore(48)
def one(h, p):
    with sem:
        try:
            s = socket.create_connection((h, p), %(timeout)s); s.close(); r = "CONNECTED"
        except socket.timeout: r = "TIMEOUT"
        except ConnectionRefusedError: r = "REFUSED"
        except OSError as e: r = "UNREACHABLE:" + errno.errorcode.get(e.errno or 0, str(e.errno))
        out["tcp"]["%%s:%%d" %% (h, p)] = r
def name(n):
    try: out["dns"][n] = sorted({a[4][0] for a in socket.getaddrinfo(n, 443, type=socket.SOCK_STREAM)})
    except Exception as e: out["dns"][n] = "FAIL " + type(e).__name__
ts = [threading.Thread(target=one, args=(h, p)) for h, p in TCP] + [threading.Thread(target=name, args=(n,)) for n in DNS]
[t.start() for t in ts]; [t.join(40) for t in ts]
out["routes"] = [l.split() for l in open("/proc/net/route").read().strip().splitlines()[1:]]
out["default_route"] = any(l[1] == "00000000" and l[7] == "00000000" for l in out["routes"])
def listening():
    res = []
    for f in ("/proc/net/tcp",):
        for l in open(f).read().splitlines()[1:]:
            x = l.split()
            if x[3] == "0A":
                a, p = x[1].split(":"); res.append(socket.inet_ntoa(bytes.fromhex(a)[::-1]) + ":" + str(int(p, 16)))
    return sorted(res)
out["listening"] = listening()
print(json.dumps(out))
'''


def probe_text(tcp: list[tuple[str, int]], dns: list[str] | None = None, timeout: float = 3.0) -> str:
    return PROBE % {"tcp": repr([list(t) for t in tcp]), "dns": repr(dns or []), "timeout": timeout}


def probe(client: Any, c: Any, user: str, tcp: list[tuple[str, int]], dns: list[str] | None = None) -> dict:
    code, out = V.run_py(client, c, probe_text(tcp, dns), user=user)
    try:
        return json.loads(out.strip().splitlines()[-1])
    except Exception:
        raise RuntimeError(f"probe failed (exit {code}): {out[-300:]!r}")


# A client that speaks the CONNECT protocol to the proxy exactly as the gateway would, one request per (raw bytes) case, and reports the reply.
CONNECT_PROBE = r'''
import json, socket
PROXY = %(proxy)r
CASES = %(cases)s
res = {}
for label, raw in CASES:
    try:
        s = socket.create_connection(PROXY, 6); s.settimeout(6)
        s.sendall(raw.encode("latin-1"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = s.recv(4096)
            if not c: break
            buf += c
        head = buf.split(b"\r\n", 1)[0].decode("latin-1")
        body = b""
        if head.split(" ")[1:2] != ["200"]:
            try:
                while True:
                    c = s.recv(4096)
                    if not c: break
                    body += c
            except Exception: pass
        res[label] = [head, body.decode("latin-1").strip()[:80] or buf.partition(b"\r\n\r\n")[2].decode("latin-1")[:80]]
        s.close()   # nothing is ever sent through an opened tunnel
    except Exception as e:
        res[label] = ["ERROR", type(e).__name__ + ": " + str(e)[:80]]
print(json.dumps(res))
'''


def connect_cases(spec: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(label, target if target.startswith(("GET ", "POST ")) else f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n")
            for label, target in spec]


def connect_probe(client: Any, c: Any, user: str, proxy_addr: tuple[str, int], cases: list[tuple[str, str]]) -> dict:
    code, out = V.run_py(client, c, CONNECT_PROBE % {"proxy": proxy_addr, "cases": repr(connect_cases(cases))}, user=user)
    return json.loads(out.strip().splitlines()[-1])


def exec_detached(client: Any, c: Any, user: str, script: str, env: dict[str, str]) -> None:
    # NOT ``-I``: like the production start (`python -m aiep.gateway`), the process needs PYTHONPATH to find the injected, verified code
    ex = client.api.exec_create(c.id, [PY, "-c", script], user=user, stdout=False, stderr=False, privileged=False, environment=env)
    client.api.exec_start(ex["Id"], detach=True)


def sh_out(client: Any, c: Any, user: str, cmd: str) -> str:
    return V.run_sh(client, c, user, cmd)[1]


def ips_of(client: Any, c: Any) -> dict[str, str]:
    c.reload()
    return {n: v.get("IPAddress") for n, v in c.attrs["NetworkSettings"]["Networks"].items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence-dir", required=True)
    ap.add_argument("--host-lan-ip", default="")
    args = ap.parse_args(argv)
    import docker
    client = docker.from_env(timeout=120)
    rec = V.Recorder("egress", Path(args.evidence_dir))
    tr = V.Tracked(client)
    run = "egv" + time.strftime("%m%d%H%M%S")
    matrix: list[dict[str, Any]] = []
    try:
        _validate(client, rec, tr, run, args.host_lan_ip, matrix)
    finally:
        # ---- cleanup: exactly what this run created, by ID, then the temporary network
        try:
            rec.fact("temporary containers removed", len(tr.cleanup()))
        except Exception as e:
            print(f"WARNING: cleanup failed: {e}", file=sys.stderr)
        try:
            client.networks.get(MOCK_NET).remove()
            rec.fact("temporary mock network removed", MOCK_NET)
        except Exception as e:
            if type(e).__name__ != "NotFound":
                print(f"WARNING: could not remove {MOCK_NET}: {type(e).__name__}: {e}", file=sys.stderr)
        rec.facts["connectivity_matrix"] = matrix
        rec.save()
        (Path(args.evidence_dir) / "connectivity_matrix.json").write_text(json.dumps(matrix, indent=1))
        client.close()
    return 1 if any(r["status"] == "FAIL" for r in rec.rows) else 0


def _validate(client: Any, rec: V.Recorder, tr: V.Tracked, run: str, lan_ip: str, matrix: list) -> None:
    api = client.api

    def row(source: str, dest: str, outcome: str, expect: str, note: str = "") -> None:
        matrix.append({"source": source, "destination": dest, "outcome": outcome, "expected": expect, "note": note})

    # ================================================================== 0. networks
    gpin = load_gateway_image(ROOT / "config" / "gateway_image.json")
    api.inspect_image(gpin.image)  # raises if the pinned image is not local: this script never pulls
    created_net = False
    try:
        client.networks.get(PROXY_NETWORK)
    except Exception:
        created_net = True
    nets = ensure_networks(client)
    rec.fact("proxy network created by this run", created_net)
    for n in ALL_NETWORKS:
        a = nets[n]
        rec.fact(f"network {n}", {"id": a["Id"][:12], "internal": a["Internal"], "driver": a["Driver"], "options": a.get("Options"), "labels": a.get("Labels"),
                                  "ipam": a.get("IPAM"), "attachable": a.get("Attachable"), "ipv6": a.get("EnableIPv6"), "containers": sorted(a.get("Containers") or {})})
    rec.check("agent and proxy networks are INTERNAL, the egress network is not", nets[AGENT_NETWORK]["Internal"] and nets[PROXY_NETWORK]["Internal"]
              and not nets[EGRESS_NETWORK]["Internal"])
    rec.check("internal networks have no VM-side bridge address (inhibit_ipv4) and no IPAM gateway", all(
        nets[n]["Options"].get("com.docker.network.bridge.inhibit_ipv4") == "true" and not any(c.get("Gateway") for c in nets[n]["IPAM"]["Config"])
        for n in (AGENT_NETWORK, PROXY_NETWORK)), [nets[n]["IPAM"] for n in (AGENT_NETWORK, PROXY_NETWORK)])
    rec.check("egress network has inter-container communication OFF", nets[EGRESS_NETWORK]["Options"].get("com.docker.network.bridge.enable_icc") == "false")
    found = verify_run_networks(client, AGENT_NETWORK, PROXY_NETWORK, EGRESS_NETWORK)
    rec.check("run-time pre-flight accepts the three networks (empty, policy-exact, disjoint private subnets)", True, found.subnets)
    a_cidr, p_cidr = found.subnets[AGENT_NETWORK], found.subnets[PROXY_NETWORK]

    # ================================================================== 1. production-path proxy and gateway
    agent_cfg = json.loads((ROOT / "config" / "agent_identity.json").read_text())
    ppin = load_proxy_pin(ROOT / "config" / "proxy.json", gateway_pin=gpin, agent_uid=agent_cfg["uid"])
    pctx = proxy_context(gpin, ppin, proxy_network=PROXY_NETWORK, egress_network=EGRESS_NETWORK, proxy_cidr=p_cidr, forbidden_secrets=(FAKE_KEY, TOKEN))
    pspec = ProxySpec(run_id=run, image=gpin.image, ctx=pctx, code=build_proxy_component(ROOT))
    gctx = gateway_context(gpin, agent_network=AGENT_NETWORK, proxy_network=PROXY_NETWORK, real_key=FAKE_KEY, agent_cidr=a_cidr)
    gspec = GatewaySpec(run_id=run, image=gpin.image, ctx=gctx, run_token=TOKEN, budget_usd="0.50", code=build_gateway_component(ROOT),
                        proxy_endpoint=pspec.endpoint)
    pxm, gwm = ProxyManager(client, pspec), GatewayManager(client, gspec, min_poll_interval=0.0)
    try:
        pxm.setup()
        rec.check("PRODUCTION proxy: create (policy-checked), egress attach, inspect, inject+verify, `-m aiep.egress_proxy`, ready", pxm.state == "ready")
        gwm.setup()
        rec.check("PRODUCTION gateway: create, proxy-network attach, inspect, inject+verify, `-m aiep.gateway`, ready", gwm.state == "ready")
        px_c, gw_c = pxm.container, gwm.container
        px_ip, gw_ip = ips_of(client, px_c), ips_of(client, gw_c)
        rec.fact("proxy IPs", px_ip)
        rec.fact("gateway IPs", gw_ip)
        evidence = verify_topology(client, {"proxy": px_c.id, "gateway": gw_c.id})
        rec.fact("verified topology (verify_topology evidence)", evidence)
        rec.check("EXACT attachments: proxy = {proxy-net, egress-net}; gateway = {agent-net, proxy-net}; no stray container on any AIEP network", True,
                  {k: sorted(v) for k, v in {"proxy": px_ip, "gateway": gw_ip}.items()})
        rec.check("the gateway is NOT attached to the egress network", EGRESS_NETWORK not in gw_ip)

        # stand-in agent (same attachment as the real one: agent network only; the real agent policy is unit-tested)
        agent = tr.create(**V.hardened(f"aiep-egress-standin-agent-{run}", AG_USER, AGENT_NETWORK))
        agent.start()
        ag_ip = ips_of(client, agent)
        rec.check("stand-in agent is attached to the agent network ONLY", set(ag_ip) == {AGENT_NETWORK}, ag_ip)
        evidence2 = verify_topology(client, {"proxy": px_c.id, "gateway": gw_c.id, "agent": agent.id})
        rec.check("topology still exact with the agent present (agent-net = {agent, gateway}; egress-net = {proxy})", True,
                  {n: v["attached"] for n, v in evidence2["networks"].items()})
        outsider = tr.create(**V.hardened(f"aiep-egress-outsider-{run}", AG_USER, EGRESS_NETWORK))
        outsider.start()
        out_ip = ips_of(client, outsider)

        # ---------------------------------------------------- 2. direct connectivity from each container
        lan = [(lan_ip, p) for p in HOST_PORTS] if lan_ip else []
        host_targets = [(HOST_GW, p) for p in HOST_PORTS + EXTRA_HOST_PORTS] + [(h, 80) for h in ("192.168.65.1", "192.168.65.2", "192.168.65.7")]
        private = [("10.0.0.1", 80), ("10.255.255.254", 443), ("172.16.0.1", 80), ("172.17.0.1", 80), ("192.168.0.1", 80), ("192.168.1.1", 80),
                   ("192.168.29.1", 80)] + lan
        linklocal = [("169.254.169.254", 80), ("169.254.0.1", 80)]
        import socket as _s
        try:
            api_ips = sorted({a[4][0] for a in _s.getaddrinfo("api.anthropic.com", 443, family=_s.AF_INET, type=_s.SOCK_STREAM)})
        except OSError:
            api_ips = []
        public = [("1.1.1.1", 443), ("1.1.1.1", 53), ("8.8.8.8", 443), ("8.8.8.8", 53), ("93.184.216.34", 80), ("93.184.216.34", 443)] + [(a, 443) for a in api_ips[:2]]
        loop = [("127.0.0.1", 3306), ("127.0.0.1", 5000), ("127.0.0.1", 8080), ("127.0.0.1", 3128)]
        groups = {"host-gateway": host_targets, "private": private, "link-local": linklocal, "public": public, "loopback(own netns)": loop}
        flat = [t for g in groups.values() for t in g]
        dns_names = ["api.anthropic.com", "example.com", "host.docker.internal", "gateway.docker.internal"]
        peer_gw = [(gw_ip[AGENT_NETWORK], 8080), (gw_ip[PROXY_NETWORK], 8080)]
        peer_px = [(px_ip[PROXY_NETWORK], 3128), (px_ip[PROXY_NETWORK], 22), (px_ip[EGRESS_NETWORK], 3128)]

        def run_probe(src: str, c: Any, user: str, extra: list[tuple[str, int]]) -> dict:
            r = probe(client, c, user, flat + extra, dns_names + [f"aiep.proxy.{run}", f"aiep.gateway.{run}", MOCK])
            rec.fact(f"probe from {src}", {k: r[k] for k in ("default_route", "listening", "routes", "dns")})
            return r

        def group_of(t: tuple[str, int]) -> str:
            return next((g for g, ts in groups.items() if t in ts), "peer")

        # ---- the gateway (holds the key)
        g = run_probe("gateway", gw_c, GW_USER, peer_px)
        rec.check("GATEWAY has no default route", g["default_route"] is False, g["routes"])
        for t in flat:
            o = g["tcp"][f"{t[0]}:{t[1]}"]
            exp = "CONNECTED" if t == ("127.0.0.1", 8080) else "not CONNECTED"
            row("gateway (direct)", f"{t[0]}:{t[1]} [{group_of(t)}]", o, exp)
        for grp, ts in groups.items():
            if grp.startswith("loopback"):
                continue
            rec.check(f"GATEWAY cannot connect directly to any {grp} destination ({len(ts)} probed)", all(g["tcp"][f"{t[0]}:{t[1]}"] != "CONNECTED" for t in ts),
                      {f"{t[0]}:{t[1]}": g["tcp"][f"{t[0]}:{t[1]}"] for t in ts[:8]})
        rec.check("GATEWAY: every one of the 6 discovered host ports via 192.168.65.254 is unreachable", all(
            g["tcp"][f"{HOST_GW}:{p}"] != "CONNECTED" for p in HOST_PORTS), {p: g["tcp"][f"{HOST_GW}:{p}"] for p in HOST_PORTS})
        rec.check("GATEWAY resolves no external name (api.anthropic.com, example.com, host.docker.internal)", all(
            str(g["dns"][n]).startswith("FAIL") for n in dns_names[:2]), {n: g["dns"][n] for n in dns_names})
        rec.fact("gateway DNS for host.docker.internal / gateway.docker.internal", {n: g["dns"][n] for n in dns_names[2:]})
        rec.check("GATEWAY control: its own loopback listener works (the probe can connect when it can)", g["tcp"]["127.0.0.1:8080"] == "CONNECTED")
        rec.check("GATEWAY control: it reaches the proxy over the proxy network (proxy:3128), and ONLY there", g["tcp"][f"{px_ip[PROXY_NETWORK]}:3128"] == "CONNECTED"
                  and g["tcp"][f"{px_ip[PROXY_NETWORK]}:22"] == "REFUSED", {k: v for k, v in g["tcp"].items() if k.startswith(px_ip[PROXY_NETWORK])})
        rec.check("GATEWAY cannot reach the proxy's EGRESS-side address (different network)", g["tcp"][f"{px_ip[EGRESS_NETWORK]}:3128"] != "CONNECTED",
                  g["tcp"][f"{px_ip[EGRESS_NETWORK]}:3128"])
        rec.check("GATEWAY resolves the proxy by name (proxy network DNS) and not the mock", isinstance(g["dns"][f"aiep.proxy.{run}"], list) and
                  str(g["dns"][MOCK]).startswith("FAIL"), {n: g["dns"][n] for n in (f"aiep.proxy.{run}", MOCK)})
        gw_listen = g["listening"]
        want = sorted({f"127.0.0.1:8080", f"{gw_ip[AGENT_NETWORK]}:8080"})
        rec.check("GATEWAY listens ONLY on its agent-network address and loopback (not on the proxy-network address, not on 0.0.0.0)", sorted(
            x for x in gw_listen if x.endswith(":8080")) == want, gw_listen)
        for t in peer_px:
            row("gateway (direct)", f"{t[0]}:{t[1]} [proxy peer]", g["tcp"][f"{t[0]}:{t[1]}"], "3128 CONNECTED, others not")

        # ---- the agent (stand-in)
        a_ = run_probe("agent", agent, AG_USER, peer_gw + peer_px)
        rec.check("AGENT has no default route", a_["default_route"] is False, a_["routes"])
        for t in flat:
            row("agent (direct)", f"{t[0]}:{t[1]} [{group_of(t)}]", a_["tcp"][f"{t[0]}:{t[1]}"], "not CONNECTED")
        rec.check("AGENT cannot connect directly to any host/private/link-local/public destination", all(
            a_["tcp"][f"{t[0]}:{t[1]}"] != "CONNECTED" for grp, ts in groups.items() if not grp.startswith("loopback") for t in ts))
        rec.check("AGENT reaches the gateway on its agent-network address (control)", a_["tcp"][f"{gw_ip[AGENT_NETWORK]}:8080"] == "CONNECTED")
        rec.check("AGENT cannot reach the gateway's proxy-network address, the proxy (either address) or resolve the proxy",
                  a_["tcp"][f"{gw_ip[PROXY_NETWORK]}:8080"] != "CONNECTED" and a_["tcp"][f"{px_ip[PROXY_NETWORK]}:3128"] != "CONNECTED"
                  and a_["tcp"][f"{px_ip[EGRESS_NETWORK]}:3128"] != "CONNECTED" and str(a_["dns"][f"aiep.proxy.{run}"]).startswith("FAIL"),
                  {k: a_["tcp"][k] for k in (f"{gw_ip[PROXY_NETWORK]}:8080", f"{px_ip[PROXY_NETWORK]}:3128", f"{px_ip[EGRESS_NETWORK]}:3128")} | {"dns": a_["dns"][f"aiep.proxy.{run}"]})
        for t in peer_gw + peer_px:
            row("agent (direct)", f"{t[0]}:{t[1]} [peer]", a_["tcp"][f"{t[0]}:{t[1]}"], "gateway agent-side CONNECTED, others not")
        rec.check("AGENT resolves no external name", all(str(a_["dns"][n]).startswith("FAIL") for n in dns_names[:2]), {n: a_["dns"][n] for n in dns_names})

        # ---- the proxy itself (direct connections from its network namespace: measures what the NETWORK allows; the application layer is tested next)
        p_ = run_probe("proxy", px_c, PX_USER, [(gw_ip[PROXY_NETWORK], 8080), (gw_ip[AGENT_NETWORK], 8080)] + [(ag_ip[AGENT_NETWORK], 22)])
        rec.check("PROXY has a default route (via the egress network)", p_["default_route"] is True)
        rec.check("PROXY control: it can reach a public address directly (the egress network works)", p_["tcp"]["1.1.1.1:443"] == "CONNECTED", p_["tcp"]["1.1.1.1:443"])
        for t in flat:
            row("proxy (direct, namespace-level)", f"{t[0]}:{t[1]} [{group_of(t)}]", p_["tcp"][f"{t[0]}:{t[1]}"], "(measurement)")
        host_open = {f"{HOST_GW}:{p}": p_["tcp"][f"{HOST_GW}:{p}"] for p in HOST_PORTS + EXTRA_HOST_PORTS}
        rec.fact("PROXY namespace-level reachability of host services via 192.168.65.254", host_open)
        reachable = [k for k, v in host_open.items() if v == "CONNECTED"]
        rec.check("KNOWN LIMITATION (not an isolation claim): the proxy container's own network namespace CAN reach macOS host services through the Docker Desktop "
                  "host gateway; Docker cannot block this from inside the VM. The proxy therefore relies on its application-level destination control "
                  "(tested below) and holds no secret.", not reachable, reachable, status="PASS" if not reachable else "LIMITATION")
        rec.check("PROXY is NOT attached to the agent network and cannot reach the agent", p_["tcp"][f"{ag_ip[AGENT_NETWORK]}:22"] != "CONNECTED", p_["tcp"][f"{ag_ip[AGENT_NETWORK]}:22"])
        rec.check("PROXY cannot reach the gateway on its agent-network address; the gateway does not listen on its proxy-network address",
                  p_["tcp"][f"{gw_ip[AGENT_NETWORK]}:8080"] != "CONNECTED" and p_["tcp"][f"{gw_ip[PROXY_NETWORK]}:8080"] == "REFUSED",
                  {k: p_["tcp"][k] for k in (f"{gw_ip[AGENT_NETWORK]}:8080", f"{gw_ip[PROXY_NETWORK]}:8080")})
        px_listen = p_["listening"]
        rec.check("PROXY listens ONLY on its proxy-network address and loopback (not on the egress address, not on 0.0.0.0)", sorted(
            x for x in px_listen if x.endswith(":3128")) == sorted({"127.0.0.1:3128", f"{px_ip[PROXY_NETWORK]}:3128"}), px_listen)

        # ---- outsider on the egress network (control: egress works; ICC is off)
        o_ = probe(client, outsider, AG_USER, [("1.1.1.1", 443), (px_ip[EGRESS_NETWORK], 3128), (px_ip[PROXY_NETWORK], 3128)])
        rec.check("control: a container on the egress network reaches the internet", o_["tcp"]["1.1.1.1:443"] == "CONNECTED")
        rec.check("another egress-network container cannot reach the proxy (ICC off) or its proxy-network address",
                  o_["tcp"][f"{px_ip[EGRESS_NETWORK]}:3128"] != "CONNECTED" and o_["tcp"][f"{px_ip[PROXY_NETWORK]}:3128"] != "CONNECTED",
                  {k: v for k, v in o_["tcp"].items() if k != "1.1.1.1:443"})
        for t in (("1.1.1.1", 443), (px_ip[EGRESS_NETWORK], 3128), (px_ip[PROXY_NETWORK], 3128)):
            row("outsider (egress net only)", f"{t[0]}:{t[1]}", o_["tcp"][f"{t[0]}:{t[1]}"], "1.1.1.1 CONNECTED, proxy not")

        # ---------------------------------------------------- 3. the proxy's application layer, driven the way the gateway would drive it
        pxa = (px_ip[PROXY_NETWORK], 3128)
        cases: list[tuple[str, str]] = [("PERMITTED api.anthropic.com:443 (CONNECT only; closed unused)", "api.anthropic.com:443")]
        refused_specs = [("example.com:443", "example.com:443"), ("api.anthropic.com:80", "api.anthropic.com:80"), ("api.anthropic.com:22", "api.anthropic.com:22"),
                         ("api.anthropic.com:22617", "api.anthropic.com:22617"), ("api.anthropic.com.:443", "api.anthropic.com.:443"),
                         ("evilapi.anthropic.com:443", "evilapi.anthropic.com:443"), ("host.docker.internal:3306", "host.docker.internal:3306"),
                         (f"{MOCK}:{MOCK_PORT} (the mock is NOT allowed by the production proxy)", f"{MOCK}:{MOCK_PORT}"),
                         ("127.0.0.1:443", "127.0.0.1:443"), ("127.0.0.1:8080", "127.0.0.1:8080"), ("0.0.0.0:443", "0.0.0.0:443"),
                         ("10.0.0.1:443", "10.0.0.1:443"), ("172.17.0.1:80", "172.17.0.1:80"), ("192.168.1.1:80", "192.168.1.1:80"),
                         ("169.254.169.254:80", "169.254.169.254:80"), ("8.8.8.8:443", "8.8.8.8:443"), ("1.1.1.1:443", "1.1.1.1:443"),
                         (f"{api_ips[0] if api_ips else '160.79.104.10'}:443 (the real API's own IP literal)", f"{api_ips[0] if api_ips else '160.79.104.10'}:443"),
                         ("[::1]:443", "[::1]:443"), ("2130706433:443 (decimal 127.0.0.1)", "2130706433:443"), ("0x7f.1:443", "0x7f.1:443"),
                         ("GET http://api.anthropic.com/ (plain forwarding)", "GET http://api.anthropic.com/ HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n"),
                         ("POST / (plain forwarding)", "POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n"),
                         ("bytes behind the CONNECT", "CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\nGET / HTTP/1.1\r\n\r\n")]
        refused_specs += [(f"{HOST_GW}:{p} (host gateway, discovered host port)", f"{HOST_GW}:{p}") for p in HOST_PORTS]
        cases += refused_specs
        res = connect_probe(client, gw_c, GW_USER, pxa, cases)
        rec.fact("CONNECT results as seen by the gateway container", res)
        okkey = cases[0][0]
        rec.check("the PERMITTED CONNECT api.anthropic.com:443 is answered 200 (tunnel to the proxy-resolved, validated public address)", res[okkey][0].split(" ")[1:2] == ["200"], res[okkey])
        row("gateway -> proxy (CONNECT)", "api.anthropic.com:443", res[okkey][0], "200 Connection Established")
        bad = {k: v for k, v in res.items() if k != okkey and (len(v[0].split(" ")) < 2 or v[0].split(" ")[1] not in ("400", "403", "405", "502"))}
        rec.check(f"every one of the {len(refused_specs)} non-allowlisted CONNECT/plain requests is refused (4xx/5xx), none gets a 200", not bad and all(
            res[l][0].split(" ")[1] != "200" for l, _ in refused_specs), {l: res[l] for l, _ in refused_specs[:6]})
        for label, _ in refused_specs:
            row("gateway -> proxy (CONNECT)", label, res[label][0] + " | " + res[label][1], "refused")
        log = sh_out(client, px_c, PX_USER, "cat /tmp/proxy.jsonl")
        entries = [json.loads(l) for l in log.splitlines() if l.startswith("{")]
        tunnels = [e for e in entries if e.get("outcome") == "tunnel"]
        rec.fact("proxy decision log (inside the proxy container)", entries)
        rec.check("the proxy's own decision log shows exactly ONE tunnel (the permitted api.anthropic.com one) and a refusal for at least every other request "
                  "(readiness/TCP probes that sent no request are logged as malformed refusals too)", len(tunnels) == 1 and tunnels[0].get("host") == "api.anthropic.com"
                  and sum(e["outcome"] == "refused" for e in entries) >= len(refused_specs),
                  {"tunnels": len(tunnels), "refused": sum(e["outcome"] == "refused" for e in entries), "pinned": tunnels[0].get("pinned_ip") if tunnels else None})
        rec.check("the pinned IP of the permitted tunnel is a public address the proxy resolved itself", bool(tunnels) and tunnels[0].get("pinned_ip") not in ("", None)
                  and not tunnels[0]["pinned_ip"].startswith(("10.", "127.", "192.168.", "172.", "169.254.")), tunnels[0].get("pinned_ip") if tunnels else None)
        rec.check("no host IP / private / loopback / link-local literal produced a tunnel", not any(
            e.get("outcome") == "tunnel" and e.get("host") != "api.anthropic.com" for e in entries))

        # ---------------------------------------------------- 4. DNS validation with the REAL resolver, through seam instances inside the same proxy container
        def seam(port: int, host: str, tport: int, allow_private: bool = False, grace: float = 30.0) -> None:
            script = (f"import sys\nfrom aiep.egress_proxy import main, ProxyConfig\n"
                      f"sys.exit(main(config=ProxyConfig(host={host!r}, port={tport}, allow_private_addresses={allow_private}, half_close_grace_s={grace})))\n")
            ex = api.exec_create(px_c.id, ["/bin/sh", "-c", f'exec {PY} -c "$SEAM" 2>/tmp/seam{port}.err'], user=PX_USER, stdout=False, stderr=False, privileged=False,
                                 environment={"AIEP_PROXY_LISTEN": f"net:{p_cidr}:{port}", "AIEP_PROXY_LOG": f"/tmp/seam{port}.jsonl", "SEAM": script,
                                              "PYTHONPATH": f"{PROXY_DEST}/src", "HOME": "/tmp"})
            api.exec_start(ex["Id"], detach=True)

        def wait_listening(ports: list[int]) -> None:
            """Poll (from the gateway container) until every seam instance accepts connections; python start-up time varies."""
            t0 = time.monotonic()
            while time.monotonic() - t0 < 30:
                r = probe(client, gw_c, GW_USER, [(px_ip[PROXY_NETWORK], p) for p in ports])
                if all(v == "CONNECTED" for v in r["tcp"].values()):
                    return
                time.sleep(0.5)
            raise RuntimeError(f"seam instances {ports} did not start listening")

        seams = [(3141, "host.docker.internal", 3306), (3142, "localhost", 3306), (3143, "gateway.docker.internal", 5000)]
        for port, host, tport in seams:
            seam(port, host, tport)
        wait_listening([p for p, _, _ in seams])
        for port, host, tport in seams:
            r = connect_probe(client, gw_c, GW_USER, (px_ip[PROXY_NETWORK], port), [(f"{host}:{tport}", f"{host}:{tport}")])[f"{host}:{tport}"]
            lg = [json.loads(l) for l in sh_out(client, px_c, PX_USER, f"cat /tmp/seam{port}.jsonl").splitlines() if l.startswith("{")]
            row("gateway -> proxy (REAL resolver, name allowed in this test config)", f"{host}:{tport}", r[0] + " | " + r[1], "refused: resolves to a non-public address")
            rec.check(f"REAL DNS: an allowed NAME ({host}) that resolves inside the proxy container to a non-public address is refused, never dialled",
                      "403" in r[0] and "resolves_to_non_public_address" in r[1] + "|".join(e.get("reason", "") for e in lg) or ("502" in r[0] and "dns_failure" in r[1]),
                      {"reply": r, "log": lg[-1:] if lg else None, "stderr": sh_out(client, px_c, PX_USER, f"cat /tmp/seam{port}.err")[-300:]}, status=None)

        # bounded DNS inside the real container: a resolver that never answers in time -> 504 dns_timeout, then dns_busy at the cap, nothing dialled
        slow_dns = ("import sys, time\nfrom aiep.egress_proxy import main, ProxyConfig\n"
                    "sys.exit(main(config=ProxyConfig(host='api.anthropic.com', port=443, resolver=lambda h, p: (time.sleep(20), ['160.79.104.10'])[1], "
                    "dns_timeout_s=1.0, dns_max_inflight=1)))\n")
        exec_detached(client, px_c, PX_USER, slow_dns, {"AIEP_PROXY_LISTEN": f"net:{p_cidr}:3146", "AIEP_PROXY_LOG": "/tmp/seam3146.jsonl",
                                                          "PYTHONPATH": f"{PROXY_DEST}/src", "HOME": "/tmp"})
        wait_listening([3146])
        t0 = time.monotonic()
        d1 = connect_probe(client, gw_c, GW_USER, (px_ip[PROXY_NETWORK], 3146), [("dns1", "api.anthropic.com:443")])["dns1"]
        el = time.monotonic() - t0
        d2 = connect_probe(client, gw_c, GW_USER, (px_ip[PROXY_NETWORK], 3146), [("dns2", "api.anthropic.com:443")])["dns2"]
        lg = [e for e in (json.loads(l) for l in sh_out(client, px_c, PX_USER, "cat /tmp/seam3146.jsonl").splitlines() if l.startswith("{"))
              if e.get("reason") != "malformed_request"]  # (the readiness poll's bare connects are logged as malformed requests)
        rec.check("BOUNDED DNS (real container): a resolver that never answers in time gives 504 dns_timeout after ~1 s (not 20 s), the next request gets 503 dns_busy "
                  "(the stuck lookup holds the only resolver slot), nothing is dialled, and both are logged distinctly",
                  "504" in d1[0] and "dns_timeout" in d1[1] and el < 8 and "503" in d2[0] and "dns_busy" in d2[1] and [e.get("reason") for e in lg] == ["dns_timeout", "dns_busy"]
                  and not any("pinned_ip" in e for e in lg), {"d1": d1, "d2": d2, "elapsed_s": round(el, 1), "log": [e.get("reason") for e in lg]})
        row("gateway -> proxy (resolver stalled)", "api.anthropic.com:443", f"{d1[0]} | {d1[1]} ; {d2[0]} | {d2[1]}", "504 dns_timeout then 503 dns_busy")

        # ---------------------------------------------------- 5. mock upstream route (gateway -> proxy -> mock), fake key, no Anthropic contact
        certs = V.make_certs(rec.dir / "mock_certs")
        mock_net = client.networks.create(MOCK_NET, driver="bridge", internal=True, labels={**SPRINT})
        mock = tr.create(**V.hardened(MOCK, "10002:10002", MOCK_NET))
        mock.start()
        V.put_files(client, mock, "10002:10002", "/tmp/mock", {"server.pem": (certs["server.pem"], 0o644), "server.key": (certs["server.key"], 0o600),
                                                                "mock.py": ((ROOT / "scripts" / "m2_mock_upstream.py").read_bytes(), 0o644)})
        ex = api.exec_create(mock.id, ["/bin/sh", "-c", f"exec {PY} /tmp/mock/mock.py /tmp/requests.jsonl 2>/tmp/mock.err"], user="10002:10002", stdout=False,
                             stderr=False, privileged=False)
        api.exec_start(ex["Id"], detach=True)
        mock_net.connect(px_c)  # the PROXY (not the gateway) joins the temporary mock network
        time.sleep(1.5)
        mock_ip = ips_of(client, mock)[MOCK_NET]
        px_ips2 = ips_of(client, px_c)
        rec.fact("proxy attachments while the temporary mock network is joined", px_ips2)
        V.put_files(client, gw_c, GW_USER, "/tmp", {"mock-ca.pem": (certs["ca.pem"], 0o644)})
        mock_proxy_port = 3144
        seam(mock_proxy_port, MOCK, MOCK_PORT, True)  # test seam: the mock lives on a private address, so this instance allows it
        launch = (f"import sys\nfrom aiep.gateway import main, UpstreamConfig\n"
                  f"sys.exit(main(upstream=UpstreamConfig(host={MOCK!r}, port={MOCK_PORT}, ca_file='/tmp/mock-ca.pem', proxy_host='aiep.proxy.{run}', proxy_port={mock_proxy_port})))\n")
        exec_detached(client, gw_c, GW_USER, launch,
                      {"AIEP_LISTEN": f"net:{a_cidr}:8081", "AIEP_LEDGER_PATH": "/tmp/ledger_mock.jsonl", "AIEP_STOP_FILE": "/tmp/stop_mock", "AIEP_BUDGET_USD": "0.50",
                       "AIEP_ALLOWED_HOSTS": f"aiep.gateway.{run}:8081", "AIEP_RUN_TOKEN": TOKEN, "AIEP_UPSTREAM_API_KEY": FAKE_KEY,
                       "AIEP_UPSTREAM_PROXY": f"aiep.proxy.{run}:{mock_proxy_port}", "PYTHONPATH": "/opt/aiep-gw/src", "HOME": "/tmp"})
        time.sleep(2.5)
        g2 = probe(client, gw_c, GW_USER, [(mock_ip, MOCK_PORT), ("127.0.0.1", 8081)], [MOCK])
        rec.check("GATEWAY cannot reach the mock directly (different network); it can only go through the proxy", g2["tcp"][f"{mock_ip}:{MOCK_PORT}"] != "CONNECTED",
                  g2["tcp"][f"{mock_ip}:{MOCK_PORT}"])
        row("gateway (direct)", f"{mock_ip}:{MOCK_PORT} [mock upstream]", g2["tcp"][f"{mock_ip}:{MOCK_PORT}"], "not CONNECTED")
        p2 = probe(client, px_c, PX_USER, [(mock_ip, MOCK_PORT)])
        rec.check("PROXY reaches the mock on its temporary network (control)", p2["tcp"][f"{mock_ip}:{MOCK_PORT}"] == "CONNECTED")
        row("proxy (direct)", f"{mock_ip}:{MOCK_PORT} [mock upstream]", p2["tcp"][f"{mock_ip}:{MOCK_PORT}"], "CONNECTED")
        body = {"model": "claude-sonnet-5-5", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}
        sc = {"ok1": {"port": 8081, "steps": [{"body": body}]}, "ok2": {"port": 8081, "steps": [{"body": body}]}}
        V.put_files(client, agent, AG_USER, "/tmp", {"client.py": (V.CLIENT.encode(), 0o644)})
        client_cmd = f"{PY} -I /tmp/client.py {run} {TOKEN} '{json.dumps(sc)}'"
        c1, o1 = V.run_sh(client, agent, AG_USER, client_cmd)
        results = json.loads(o1.strip().splitlines()[-1])
        rec.fact("agent -> gateway -> proxy -> mock results", results)
        statuses = [r[0].get("status") for r in results.values()]
        rec.check("agent -> gateway -> PROXY -> mock upstream streaming-free requests succeed (200) end to end over verified TLS (name+CA check inside the gateway)",
                  statuses == [200, 200], results)
        mock_log = [json.loads(l) for l in V.read_file(client, mock, "10002:10002", "/tmp/requests.jsonl").splitlines() if l.strip()]
        rec.fact("requests the mock received", [{k: r[k] for k in ("path", "model", "max_tokens", "client_ip")} for r in mock_log])
        real_hash = hashlib.sha256(FAKE_KEY.encode()).hexdigest()
        rec.check("the mock saw the request from the PROXY's mock-network address (not the gateway, which is not on that network)", bool(mock_log) and
                  {r["client_ip"] for r in mock_log} == {px_ips2[MOCK_NET]}, [r["client_ip"] for r in mock_log])
        rec.check("upstream saw the gateway's (fake) key via x-api-key and never the agent's token or an Authorization header", bool(mock_log) and all(
            r["x_api_key_sha256"] == real_hash and not r["has_authorization_header"] for r in mock_log))
        ledger = [json.loads(l) for l in V.read_file(client, gw_c, GW_USER, "/tmp/ledger_mock.jsonl").splitlines() if l.strip()]
        fin = [e for e in ledger if e.get("outcome") != "forwarding"]
        spent = Decimal(str(fin[-1]["cumulative_usd_computed"])) if fin else None
        rec.check("gateway accounting is exact through the proxy: 2 requests x $0.000401 = $0.000802 computed from measured usage", spent == V.MOCK_REQUEST_COST_USD * 2,
                  {"entries": len(fin), "cumulative": str(spent)})
        proxy_log = [json.loads(l) for l in sh_out(client, px_c, PX_USER, f"cat /tmp/seam{mock_proxy_port}.jsonl").splitlines() if l.startswith("{")]
        rec.check("the proxy tunnelled both gateway connections (outcome tunnel, bytes both ways) and its log holds no key, token or prompt", len(
            [e for e in proxy_log if e["outcome"] == "tunnel"]) >= 2 and FAKE_KEY not in json.dumps(proxy_log) and TOKEN not in json.dumps(proxy_log),
            [{k: e.get(k) for k in ("outcome", "bytes_up", "bytes_down", "reason")} for e in proxy_log][:3])
        row("agent -> gateway -> proxy -> mock", f"{MOCK}:{MOCK_PORT}", "200 x2, exact accounting", "success")

        # ---------------------------------------------------- 5b. STREAMING through the production proxy and gateway (local mock only)
        from aiep import ledger as ledger_mod
        sbody = {**body, "stream": True}

        def call(port: int, name: str, headers: dict | None = None, b: dict | None = None) -> dict:
            step = {"body": b or sbody, **({"headers": headers} if headers else {})}
            cmd = f"{PY} -I /tmp/client.py {run} {TOKEN} '{json.dumps({name: {'port': port, 'steps': [step]}})}'"
            return json.loads(V.run_sh(client, agent, AG_USER, cmd)[1].strip().splitlines()[-1])[name][0]

        def fin_entries(path: str, user: str = GW_USER, c: Any = gw_c) -> list[dict]:
            return [e for e in (json.loads(l) for l in V.read_file(client, c, user, path).splitlines() if l.strip()) if e.get("outcome") != "forwarding"]

        r_ok = call(8081, "stream_ok")
        e = fin_entries("/tmp/ledger_mock.jsonl")[-1]
        rec.check("STREAMING through gateway -> proxy -> mock: 200 text/event-stream, all SSE events delivered (message_start ... message_stop)",
                  r_ok.get("status") == 200 and "text/event-stream" in (r_ok.get("ctype") or "") and "message_start" in r_ok["body"] and "message_stop" in r_ok["body"]
                  and r_ok["body"].count("event: content_block_delta") == 2, {k: r_ok.get(k) for k in ("status", "ctype")})
        rec.check("STREAMING: usage extracted from the stream and metered exactly ($0.000401, stream=true, no upper bound), ledger complete",
                  e.get("stream") is True and e.get("cost_usd_computed") == "0.000401" and "usage_incomplete_upper_bound" not in e and
                  ledger_mod.audit(V.read_file(client, gw_c, GW_USER, "/tmp/ledger_mock.jsonl"))["complete"], {k: e.get(k) for k in ("outcome", "usage", "cost_usd_computed", "stream")})
        row("agent -> gateway -> proxy -> mock (stream)", f"{MOCK}:{MOCK_PORT}", "200 SSE, $0.000401", "success")
        r_cut = call(8081, "stream_cut", {"anthropic-mock-mode": "early_close"})
        e = fin_entries("/tmp/ledger_mock.jsonl")[-1]
        rec.check("STREAMING: an upstream that ends the stream mid-response (no message_stop) is charged at the max_tokens upper bound and flagged ($0.001201), never exact",
                  e.get("usage_incomplete_upper_bound") is True and e.get("cost_usd_computed") == "0.001201" and (e.get("usage") or {}).get("output_tokens") == 100,
                  {k: e.get(k) for k in ("outcome", "usage", "cost_usd_computed", "usage_incomplete_upper_bound")})
        row("agent -> gateway -> proxy -> mock (stream cut by upstream)", f"{MOCK}:{MOCK_PORT}", "upper-bound charge $0.001201", "flagged incomplete")

        # the PROXY is killed in the middle of a stream (the mock holds the stream open for 12 s after the first event)
        n_fin = len(fin_entries("/tmp/ledger_mock.jsonl"))
        slow_cmd = f"{PY} -I /tmp/client.py {run} {TOKEN} '{json.dumps({'slow': {'port': 8081, 'steps': [{'body': sbody, 'headers': {'anthropic-mock-mode': 'slow'}}]}})}' > /tmp/out_slow.json 2>&1"
        ex = api.exec_create(agent.id, ["/bin/sh", "-c", slow_cmd], user=AG_USER, stdout=False, stderr=False, privileged=False)
        api.exec_start(ex["Id"], detach=True)
        time.sleep(3.0)
        kill_proxy = sh_out(client, px_c, PX_USER, "for p in /proc/[0-9]*; do n=${p#/proc/}; if tr '\\0' '\\n' < $p/environ 2>/dev/null | grep -q 'seam3144'; then kill -KILL $n; echo killed $n; fi; done")
        time.sleep(4.0)
        e = fin_entries("/tmp/ledger_mock.jsonl")
        # the mock's stream is close-delimited (HTTP/1.0), so the severed tunnel reads as a clean end of body (outcome ok); with a chunked upstream, as
        # the real API uses, it is an upstream_read_error (covered locally). Either way: ended at once (not after the mock's 12 s), flagged, upper-bound.
        rec.check("STREAMING: the proxy killed MID-STREAM ends the request at once, charged at the upper bound and flagged incomplete; the gateway does not reconnect directly",
                  "killed" in kill_proxy and len(e) == n_fin + 1 and e[-1].get("outcome") in ("upstream_read_error", "ok") and e[-1].get("usage_incomplete_upper_bound") is True
                  and e[-1].get("duration_s", 99) < 10,
                  {"killed": kill_proxy.strip(), "entry": {k: e[-1].get(k) for k in ("outcome", "cost_usd_computed", "usage_incomplete_upper_bound", "duration_s")} if e else None})
        row("agent -> gateway -> (proxy killed mid-stream)", f"{MOCK}:{MOCK_PORT}", "upstream_read_error, upper bound", "flagged incomplete, no fallback")

        # the GATEWAY is killed in the middle of a stream: a second gateway instance (own ledger), a second proxy instance
        seam(3145, MOCK, MOCK_PORT, True, grace=2.0)
        launch2 = launch.replace(f"proxy_port={mock_proxy_port}", "proxy_port=3145")
        exec_detached(client, gw_c, GW_USER, launch2,
                      {"AIEP_LISTEN": f"net:{a_cidr}:8082", "AIEP_LEDGER_PATH": "/tmp/ledger_kill.jsonl", "AIEP_STOP_FILE": "/tmp/stop_kill", "AIEP_BUDGET_USD": "0.50",
                       "AIEP_ALLOWED_HOSTS": f"aiep.gateway.{run}:8082", "AIEP_RUN_TOKEN": TOKEN, "AIEP_UPSTREAM_API_KEY": FAKE_KEY,
                       "AIEP_UPSTREAM_PROXY": f"aiep.proxy.{run}:3145", "PYTHONPATH": "/opt/aiep-gw/src", "HOME": "/tmp"})
        time.sleep(3.0)
        slow2 = f"{PY} -I /tmp/client.py {run} {TOKEN} '{json.dumps({'slow2': {'port': 8082, 'steps': [{'body': sbody, 'headers': {'anthropic-mock-mode': 'slow'}}]}})}' > /tmp/out_slow2.json 2>&1"
        ex = api.exec_create(agent.id, ["/bin/sh", "-c", slow2], user=AG_USER, stdout=False, stderr=False, privileged=False)
        api.exec_start(ex["Id"], detach=True)
        time.sleep(3.0)
        mid = V.read_file(client, gw_c, GW_USER, "/tmp/ledger_kill.jsonl")
        kill_gw = sh_out(client, gw_c, GW_USER, "for p in /proc/[0-9]*; do n=${p#/proc/}; if tr '\\0' '\\n' < $p/environ 2>/dev/null | grep -q 'ledger_kill'; then kill -KILL $n; echo killed $n; fi; done")
        time.sleep(2.0)
        after = V.read_file(client, gw_c, GW_USER, "/tmp/ledger_kill.jsonl")
        a_mid, a_after = ledger_mod.audit(mid), ledger_mod.audit(after)
        rec.check("STREAMING: a gateway process KILLED mid-stream leaves a durable `forwarding` line with no finishing line: the audit reports INCOMPLETE accounting (1 unresolved request)",
                  "killed" in kill_gw and a_mid["complete"] is False and len(a_mid["unresolved"]) == 1 and a_after["complete"] is False and len(a_after["unresolved"]) == 1
                  and sum(1 for l in after.splitlines() if l.strip()) == 1,
                  {"killed": kill_gw.strip(), "audit_after": {k: a_after[k] for k in ("forwarded", "finished", "complete")}, "unresolved": a_after["unresolved"]})
        row("agent -> gateway (killed mid-stream) -> proxy -> mock", f"{MOCK}:{MOCK_PORT}", "forwarding line, no finish: audit incomplete", "incomplete, never exact")
        time.sleep(4.0)  # the proxy's half-close grace (2 s in this instance) elapses
        proxy2 = [json.loads(l) for l in sh_out(client, px_c, PX_USER, "cat /tmp/seam3145.jsonl").splitlines() if l.startswith("{")]
        rec.check("STREAMING: the proxy's tunnel for the killed gateway ended by itself after the half-close grace (reason half_close_timeout) and holds no key, token or prompt", any(
            x.get("outcome") == "tunnel" and x.get("reason") == "half_close_timeout" for x in proxy2) and FAKE_KEY not in json.dumps(proxy2) and TOKEN not in json.dumps(proxy2),
            [{k: x.get(k) for k in ("outcome", "reason", "bytes_up", "bytes_down")} for x in proxy2])
        sh_out(client, px_c, PX_USER, "for p in /proc/[0-9]*; do n=${p#/proc/}; if tr '\\0' '\\n' < $p/environ 2>/dev/null | grep -q 'seam3145'; then kill -KILL $n; fi; done")

        # no direct-fallback: take the proxy instance away and send another request
        n_before = len([l for l in V.read_file(client, mock, "10002:10002", "/tmp/requests.jsonl").splitlines() if l.strip()])
        # stop ONLY the seam instance for the mock (identify it by its listening port in its environment)
        kill_out = sh_out(client, px_c, PX_USER, "for p in /proc/[0-9]*; do n=${p#/proc/}; if tr '\\0' '\\n' < $p/environ 2>/dev/null | grep -q 'seam3144'; then kill -KILL $n; echo killed $n; fi; done")
        time.sleep(1.0)
        c2, o2 = V.run_sh(client, agent, AG_USER, f"{PY} -I /tmp/client.py {run} {TOKEN} '{json.dumps({'after_proxy_down': {'port': 8081, 'steps': [{'body': body}]}})}'")
        r2 = json.loads(o2.strip().splitlines()[-1])["after_proxy_down"][0]
        mock_log2 = [json.loads(l) for l in V.read_file(client, mock, "10002:10002", "/tmp/requests.jsonl").splitlines() if l.strip()]
        rec.check("with the proxy instance gone the gateway FAILS CLOSED (502) and does not fall back to a direct connection; the mock saw no new request",
                  r2.get("status") == 502 and len(mock_log2) == n_before, {"reply": r2, "killed": kill_out.strip()})
        row("agent -> gateway -> (proxy down)", f"{MOCK}:{MOCK_PORT}", str(r2.get("status")), "502, no fallback")

        # detach the temporary network, drop the control container that sits on the egress network, and prove the topology is exactly the production one again
        mock_net.disconnect(px_c)
        api.remove_container(outsider.id, force=True, v=True)
        tr.ids = [i for i in tr.ids if i != outsider.id]
        again = verify_topology(client, {"proxy": px_c.id, "gateway": gw_c.id, "agent": agent.id})
        rec.check("after detaching the temporary mock network the EXACT production topology holds again", True, {n: v["attached"] for n, v in again["networks"].items()})

        # no key in the agent or proxy
        for who, c, user in (("agent", agent, AG_USER), ("proxy", px_c, PX_USER)):
            env_txt = sh_out(client, c, user, "tr '\\0' '\\n' < /proc/1/environ; env")
            c.reload()
            rec.check(f"the (fake) real API key and the run token are absent from the {who} container's environment (process env and Docker config)",
                      FAKE_KEY not in env_txt and FAKE_KEY not in json.dumps(c.attrs["Config"]["Env"]) and (who == "agent" or TOKEN not in json.dumps(c.attrs["Config"]["Env"])))
        rec.check("the key is held by the gateway container alone", FAKE_KEY in json.dumps(gw_c.attrs["Config"]["Env"]) if gw_c.attrs else True)
        # ---------------------------------------------------- 5c. the REAL agent container (policy create, real image, real pinned seed), post-start attachment check
        api.remove_container(agent.id, force=True, v=True)  # the stand-in is gone: agent-net must hold only the gateway now
        tr.ids = [i for i in tr.ids if i != agent.id]
        _real_agent_checks(client, rec, run, gw_c, px_c, found, a_cidr)
    finally:
        # ---------------------------------------------------- production-path teardown (exact IDs), in the production order
        t_gw = gwm.teardown() if gwm.container is not None else (True, None)
        t_px = pxm.teardown() if pxm.container is not None else (True, None)
        rec.check("teardown removes the gateway, then the proxy (exact container IDs, anonymous volumes)", t_gw[0] and t_px[0], {"gateway": t_gw, "proxy": t_px})

    # the stand-in agent and the mock are no longer needed; the lifecycle phase requires EMPTY AIEP networks (as every run's pre-flight does)
    rec.fact("stand-in/mock containers removed before the lifecycle phase", len(tr.cleanup()))
    # ================================================================== 6. lifecycle on real Docker
    _lifecycle(client, rec, tr, gpin, ppin, agent_cfg, a_cidr, p_cidr, found, nets)
    # ================================================================== 7. reaper (dry run only)
    _reaper(client, rec, tr, gpin, ppin, agent_cfg, a_cidr, p_cidr)


def _real_agent_checks(client: Any, rec: V.Recorder, run: str, gw_c: Any, px_c: Any, found: Any, a_cidr: str) -> None:
    """Production ``run_agent`` against a REAL agent container, with the post-start attachment check. Claude is replaced by a no-op script (no API call)."""
    from aiep import m2_run
    from aiep.agent_env import load_agent_config
    from aiep.agent_runner import AgentRunSpec, run_agent
    from aiep.seed import build_component_archive, load_seed

    instance = "pytest-dev__pytest-5227"
    image_id = json.loads((ROOT / "config" / "evaluator_images.json").read_text())["images"][instance]["image_id"]
    cfg = load_agent_config(ROOT / "config" / "agent_identity.json", image_id=image_id)
    pin = m2_run.load_seed_pin(ROOT, instance)
    seed = load_seed(ROOT / pin["directory"], expected_sha256=pin["sha256"])
    noop = build_component_archive({"claude": (b"#!/bin/sh\nexit 0\n", 0o755)})  # stands in for the Claude binary: it makes no request of any kind
    base = dict(image=image_id, gateway_url=f"http://aiep.gateway.{run}:8080", agent_network=AGENT_NETWORK, pinned_model="claude-sonnet-5-5", run_token=TOKEN,
                seed=seed, components=noop, python=cfg.verification_python, identity=cfg.identity, config_label="A", task_text="attachment check only",
                claude_args=("-p",), image_id=image_id, wall_clock_s=120.0, forbidden_secrets=(FAKE_KEY,))
    ids = found.ids

    def hook(cid: str) -> None:
        verify_topology(client, {"proxy": px_c.id, "gateway": gw_c.id, "agent": cid})

    class Wrapped:
        """The real container, plus an extra network attached right AFTER it starts (a deviation that only exists in the running container)."""

        def __init__(self, c: Any, extra: str):
            self._c, self._extra = c, extra

        def start(self) -> None:
            self._c.start()
            client.networks.get(self._extra).connect(self._c)

        def __getattr__(self, n: str) -> Any:
            return getattr(self._c, n)

    class WClient:
        def __init__(self, extra: str):
            self.api = client.api
            self.containers = self
            self._extra = extra

        def create(self, *a: Any, **k: Any) -> Any:
            return Wrapped(client.containers.create(*a, **k), self._extra)

    def left() -> list[str]:
        return [c.name for c in client.containers.list(all=True) if c.name.startswith("aiep.agent.")]

    def run_once(label: str, spec_over: dict, extra: str | None = None):
        spec = AgentRunSpec(run_id=run, network_id=ids[AGENT_NETWORK], network_cidr=a_cidr, **{**base, **spec_over})
        t0 = time.monotonic()
        res = run_agent(WClient(extra) if extra else client, spec, lambda: None, attachment_check=hook, start_timeout_s=180.0)
        rec.fact(f"real agent run [{label}]", {"status": res.status, "valid": res.valid, "invalid_reason": (res.invalid_reason or "")[:400], "removed": res.removed,
                                              "seconds": round(time.monotonic() - t0, 1), "attachment": res.attachment, "timeline": list(res.timeline)})
        return res

    ok = run_once("valid", {})
    rec.check("REAL agent, correctly attached: the post-start check passes (exactly the agent network, real network ID, IP inside the real subnet, no ports, no default route) "
              "and the run proceeds to injection and completion", ok.valid and ok.attachment is not None and "attachment_ok" in ok.timeline and "injected" in ok.timeline
              and set(ok.attachment["networks"]) == {AGENT_NETWORK} and ok.attachment["networks"][AGENT_NETWORK]["network_id"] == ids[AGENT_NETWORK]
              and ok.attachment["published_ports"] == [] and len(ok.attachment["routes"]) == 1 and ok.removed, ok.attachment)
    for label, over, extra, needle in (
            ("wrong expected subnet", {}, None, "not inside"), ("wrong expected network id", {}, None, "NetworkID"),
            ("attached to the proxy network after start", {}, PROXY_NETWORK, "aiep-proxy-net"),
            ("attached to the egress network after start", {}, EGRESS_NETWORK, "aiep-egress-net")):
        spec_over = dict(over)
        if label == "wrong expected subnet":
            res = _run_with(client, base, run, ids[AGENT_NETWORK], "172.31.99.0/24", hook)
        elif label == "wrong expected network id":
            res = _run_with(client, base, run, "impostor-network-id", a_cidr, hook)
        else:
            res = run_once(label, spec_over, extra)
        rec.fact(f"real agent run [{label}]", {"status": res.status, "invalid_reason": (res.invalid_reason or "")[:500], "removed": res.removed, "timeline": list(res.timeline)})
        rec.check(f"REAL agent, {label}: the run is INVALID (attachment check), Claude never starts, nothing is injected, and the container is removed",
                  res.valid is False and needle in (res.invalid_reason or "") and "agent attachment check failed" in (res.invalid_reason or "")
                  and "injected" not in res.timeline and "claude_started" not in res.timeline and res.removed and left() == [], (res.invalid_reason or "")[:300])
    topo = verify_topology(client, {"proxy": px_c.id, "gateway": gw_c.id})
    rec.check("after the failed agent runs the proxy and gateway topology is still exact and no agent container is left", True, {n: v["attached"] for n, v in topo["networks"].items()})


def _run_with(client: Any, base: dict, run: str, network_id: str, cidr: str, hook: Any):
    from aiep.agent_runner import AgentRunSpec, run_agent
    return run_agent(client, AgentRunSpec(run_id=run, network_id=network_id, network_cidr=cidr, **base), lambda: None, attachment_check=hook, start_timeout_s=180.0)


def _specs(gpin: Any, ppin: Any, agent_cfg: dict, run: str, a_cidr: str, p_cidr: str) -> tuple[ProxySpec, GatewaySpec]:
    pctx = proxy_context(gpin, ppin, proxy_network=PROXY_NETWORK, egress_network=EGRESS_NETWORK, proxy_cidr=p_cidr, forbidden_secrets=(FAKE_KEY, TOKEN))
    pspec = ProxySpec(run_id=run, image=gpin.image, ctx=pctx, code=build_proxy_component(ROOT), ready_timeout_s=15, start_timeout_s=30)
    gctx = gateway_context(gpin, agent_network=AGENT_NETWORK, proxy_network=PROXY_NETWORK, real_key=FAKE_KEY, agent_cidr=a_cidr)
    gspec = GatewaySpec(run_id=run, image=gpin.image, ctx=gctx, run_token=TOKEN, budget_usd="0.50", code=build_gateway_component(ROOT),
                        proxy_endpoint=pspec.endpoint, ready_timeout_s=15, start_timeout_s=30)
    return pspec, gspec


def _left_over(client: Any, run: str) -> list[str]:
    return sorted(c.name for c in client.containers.list(all=True) if c.name in (f"aiep.proxy.{run}", f"aiep.gateway.{run}", f"aiep.agent.{run}"))


def _lifecycle(client: Any, rec: V.Recorder, tr: V.Tracked, gpin: Any, ppin: Any, agent_cfg: dict, a_cidr: str, p_cidr: str, found: Any, nets: dict) -> None:
    from aiep.agent_runner import AgentRunSpec  # noqa: F401
    from aiep.container_exec import ExecError  # noqa: F401
    egress_cidr = nets[EGRESS_NETWORK]["IPAM"]["Config"][0]["Subnet"]

    class Stub(Exception):
        pass

    def stub_agent(boom: str):
        def fn(client_: Any, aspec: Any, stopped: Any, **kw: Any) -> Any:
            raise Stub(boom)
        return fn

    def full(run: str, *, a=a_cidr, p=p_cidr, agent_fn=None, label: str = ""):
        pspec, gspec = _specs(gpin, ppin, agent_cfg, run, a, p)
        # the agent spec is only consulted for consistency (names, token, network); the stub never creates an agent container
        aspec = dataclasses.make_dataclass("A", [("gateway_url", str), ("agent_network", str), ("run_token", str), ("forbidden_secrets", tuple)], frozen=True)(
            gspec.url, AGENT_NETWORK, TOKEN, ())
        try:
            res = run_with_gateway(client, gspec, aspec, pspec=pspec, run_agent_fn=agent_fn or stub_agent("stub agent ran"), min_poll_interval=0.0)
            return res, None
        except Exception as e:
            return None, e

    # A. a normal supervised run (the agent stub raises after the proxy and gateway are ready and the topology is verified)
    run = "egl" + time.strftime("%H%M%S")
    t0 = time.monotonic()
    res, exc = full(run)
    rec.check("LIFECYCLE: the stub agent's exception propagates AFTER the gateway and proxy were torn down (agent -> gateway -> proxy order, exact IDs)",
              isinstance(exc, Stub) and _left_over(client, run) == [], {"exception": repr(exc), "left": _left_over(client, run), "seconds": round(time.monotonic() - t0, 1),
                                                                        "result": None if res is None else {"status": res.status, "gateway_error": res.gateway_error,
                                                                                                           "proxy_error": res.proxy_error}})
    rec.check("LIFECYCLE: all three AIEP networks are empty and policy-exact afterwards (the next run's pre-flight passes)", verify_run_networks(
        client, AGENT_NETWORK, PROXY_NETWORK, EGRESS_NETWORK).ids is not None)

    # B. a proxy start failure: the proxy is asked to listen on the EGRESS network's subnet -> its listen validation refuses (default-route interface)
    run = "egp" + time.strftime("%H%M%S")
    res, exc = full(run, p=egress_cidr)
    err = getattr(res, "proxy_error", None) if res else str(exc)
    rec.check("LIFECYCLE: a proxy told to bind the EGRESS interface refuses to start (default-route interface), the run ends proxy_failed, the gateway is never created and the proxy is removed",
              res is not None and res.status == "proxy_failed" and "default route" in (err or "") and res.proxy_removed and _left_over(client, run) == [],
              {"status": getattr(res, "status", None), "error": (err or "")[:300], "left": _left_over(client, run)})
    # C. a gateway start failure: wrong agent subnet -> no interface inside it -> the gateway exits; proxy removed as well
    run = "egg" + time.strftime("%H%M%S")
    res, exc = full(run, a="172.31.77.0/24")
    err = getattr(res, "gateway_error", None) if res else str(exc)
    rec.check("LIFECYCLE: a gateway told to bind a subnet it is not attached to exits (no wildcard fallback); gateway AND proxy are removed and no agent is created",
              res is not None and res.status == "gateway_failed" and "no interface" in (err or "") and res.gateway_removed and res.proxy_removed and _left_over(client, run) == [],
              {"status": getattr(res, "status", None), "error": (err or "")[:300], "left": _left_over(client, run)})
    # D. a teardown failure that is REPORTED: the proxy container disappears underneath the orchestrator mid-run
    run = "egt" + time.strftime("%H%M%S")
    holder: dict[str, Any] = {}

    def killer(client_: Any, aspec: Any, stopped: Any, **kw: Any) -> Any:
        c = client.containers.get(f"aiep.proxy.{run}")
        client.api.remove_container(c.id, force=True, v=True)  # out-of-band removal (a daemon-side failure stand-in)
        holder["reason"] = stopped()
        raise Stub("agent ended")
    res, exc = full(run, agent_fn=killer)
    rec.check("LIFECYCLE: a proxy that dies mid-run is seen by supervision as 'proxy_died' (an invalid-run stop reason) and everything is still cleaned up", holder.get("reason") == "proxy_died"
              and _left_over(client, run) == [], {"stop_reason": holder.get("reason"), "left": _left_over(client, run), "exception": repr(exc)})
    # E. real AIEP_PROXY_LISTEN values refused inside a real container (exit code 2 and the message), no listener left behind
    run = "egl2" + time.strftime("%H%M%S")
    pspec, gspec = _specs(gpin, ppin, agent_cfg, run, a_cidr, p_cidr)
    pm = ProxyManager(client, pspec)
    try:
        pm.setup()
        results = {}
        for label, value in (("wildcard", f"0.0.0.0:3128"), ("loopback only", "127.0.0.1:3128"), ("internal:port", "internal:3128"), ("egress subnet", f"net:{egress_cidr}:3128"),
                             ("agent subnet (not attached)", f"net:{a_cidr}:3128"), ("/8 wildcard-ish", "net:0.0.0.0/0:3128")):
            ex = client.api.exec_create(pm.container.id, [PY, "-m", "aiep.egress_proxy"], user=PX_USER, privileged=False,
                                        environment={"AIEP_PROXY_LISTEN": value, "PYTHONPATH": f"{PROXY_DEST}/src", "HOME": "/tmp"}, stdout=True, stderr=True)
            out = client.api.exec_start(ex["Id"])
            results[label] = [client.api.exec_inspect(ex["Id"]).get("ExitCode"), (out or b"").decode()[-150:].strip()]
        rec.fact("AIEP_PROXY_LISTEN values refused inside the real proxy container", results)
        rec.check("REAL container: wildcard, loopback-only, internal:, the egress subnet, an unattached subnet and 0.0.0.0/0 are ALL refused with exit code 2", all(
            v[0] == 2 and "invalid AIEP_PROXY_LISTEN" in v[1] for v in results.values()), results)
        ex = client.api.exec_create(pm.container.id, [PY, "-m", "aiep.egress_proxy"], user=PX_USER, privileged=False, stdout=True, stderr=True,
                                    environment={"AIEP_PROXY_LISTEN": f"net:{p_cidr}:3199", "AIEP_UPSTREAM_API_KEY": FAKE_KEY, "PYTHONPATH": f"{PROXY_DEST}/src", "HOME": "/tmp"})
        out = (client.api.exec_start(ex["Id"]) or b"").decode()
        rec.check("REAL container: the proxy REFUSES TO START when given an API key in its environment", client.api.exec_inspect(ex["Id"]).get("ExitCode") == 2
                  and "no secrets" in out and FAKE_KEY not in out, out[-200:])
    finally:
        pm.teardown()
    rec.check("no AIEP lifecycle container was left behind by any lifecycle scenario", all(_left_over(client, r) == [] for r in (run,)))


def _reaper(client: Any, rec: V.Recorder, tr: V.Tracked, gpin: Any, ppin: Any, agent_cfg: dict, a_cidr: str, p_cidr: str) -> None:
    run = "egr" + time.strftime("%H%M%S")
    pspec, gspec = _specs(gpin, ppin, agent_cfg, run, a_cidr, p_cidr)
    pm = ProxyManager(client, pspec)
    try:
        pm.create()  # created but never started: a real, labelled, AIEP-owned proxy container
        info = client.api.inspect_container(pm.container.id)
        shape = reaper._shape(info["Name"].lstrip("/"), info["Config"]["Labels"], info["Id"])
        rec.check("REAPER ownership: the real proxy container has the exact AIEP name and labels and is recognised as role 'proxy'", shape == ("proxy", "ok"),
                  {"name": info["Name"], "labels": info["Config"]["Labels"], "shape": shape})
        rec.check("the proxy carries ONLY the AIEP role/run labels (no foreign labels)", set(info["Config"]["Labels"]) == {"aiep.run_id", "aiep.role"}, info["Config"]["Labels"])
        # dry run with the real clock: a young container with no lease is not reapable (nothing is removed; execute=False never calls remove)
        rep = reaper.reap(client, lease_dir=ROOT / ".cache" / "leases", execute=False)
        mine = [i for i in rep.items if i.container_id == pm.container.id]
        rec.check("REAPER dry run (real clock): the live proxy is listed, classified as role proxy, NOT planned for removal (younger than the TTL, no lease)",
                  len(mine) == 1 and mine[0].role == "proxy" and mine[0].action == "skip", [(i.role, i.action, i.reason) for i in mine])
        others = [i for i in rep.items if i.container_id != pm.container.id]
        rec.fact("other containers the dry run listed (read-only)", [(i.name, i.role, i.action) for i in others])
        # dry run with a simulated clock, scoped to this phase's own container (execute=False: nothing can be removed)
        scoped = V.ScopedReaperClient(V.ScopedReaperApi(client.api, lambda: {pm.container.id}, "egr"))
        future = reaper.reap(scoped, lease_dir=ROOT / ".cache" / "leases", execute=False, now=lambda: time.time() + 6 * 3600)
        planned = [(i.role, i.action, i.removed) for i in future.items]
        rec.check("REAPER dry run (clock +6h, scoped to this proxy only): the abandoned proxy is PLANNED for removal and NOTHING is removed", planned == [("proxy", "remove", False)]
                  and client.api.inspect_container(pm.container.id)["Id"] == pm.container.id, planned)
    finally:
        removed, err = pm.teardown()
        rec.check("the reaper-phase proxy was removed by exact ID by its own manager", removed and _left_over(client, run) == [], err)


if __name__ == "__main__":
    sys.exit(main())

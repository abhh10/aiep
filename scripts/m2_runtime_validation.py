#!/usr/bin/env python3
"""M2 runtime validation against a real Docker daemon (approval-gated). Measures facts; never assumes them.

    PYTHONPATH=src .venv/bin/python scripts/m2_runtime_validation.py --evidence-dir <dir outside the repo> --phase networks|gateway|agent|integration

Each phase creates only temporary containers labelled ``aiep.sprint=m2-integration``, records every check as PASS / FAIL, writes a JSON
evidence file, and removes exactly the containers it created (by ID) in a ``finally``. It uses fake credentials and local mock servers only:
no real API key and no request to any Anthropic API endpoint (the only upstream contact is a DNS lookup and a bare TCP+TLS handshake to the
upstream host, with no HTTP request, to prove reachability).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiep.gateway_runner import build_gateway_component, gateway_context, load_gateway_image  # noqa: E402
from aiep.lifecycle import LEDGER_PATH, STOP_FILE, GatewayManager, GatewaySpec  # noqa: E402
from aiep.networks import AGENT_NETWORK, EGRESS_NETWORK, ensure_networks  # noqa: E402

FAKE_KEY = "sk-ant-api03-SPRINT-FAKE-KEY-0000000000000000000000"  # fake: never valid anywhere
DUMMY_TOKEN = "sprint-dummy-run-token-0123456789abcdef"

PY_REF = "docker.io/library/python@sha256:c61cffc3bacb94ed026ff8b45d5114b7693dd0420baca0a1ace0e3d0ce9035fd"
PY_ID = "sha256:c61cffc3bacb94ed026ff8b45d5114b7693dd0420baca0a1ace0e3d0ce9035fd"
PY = "/usr/local/bin/python3"
SPRINT_LABEL = {"aiep.sprint": "m2-integration"}
UPSTREAM_HOST = "api.anthropic.com"


class Recorder:
    def __init__(self, phase: str, evidence_dir: Path):
        self.phase, self.dir, self.rows, self.facts = phase, Path(evidence_dir), [], {}
        self.dir.mkdir(parents=True, exist_ok=True)

    def check(self, name: str, ok: bool | None, detail: Any = "", status: str | None = None) -> bool:
        st = status or ("PASS" if ok else "FAIL")
        self.rows.append({"check": name, "status": st, "detail": detail})
        print(f"[{st}] {name}" + (f" :: {detail}" if detail not in ("", None) else ""))
        return st == "PASS"

    def fact(self, key: str, value: Any) -> None:
        self.facts[key] = value
        print(f"      {key}: {json.dumps(value, default=str)[:600]}")

    def save(self) -> None:
        (self.dir / f"{self.phase}.json").write_text(json.dumps({"phase": self.phase, "results": self.rows, "facts": self.facts},
                                                                  indent=2, default=str, sort_keys=True) + "\n")
        passed = sum(r["status"] == "PASS" for r in self.rows)
        print(f"== {self.phase}: {passed}/{len(self.rows)} PASS, " f"{sum(r['status'] == 'FAIL' for r in self.rows)} FAIL ==")


class Tracked:
    """Containers this run created. Cleanup removes exactly these IDs and nothing else."""

    def __init__(self, client: Any):
        self.client, self.ids = client, []

    def create(self, **kw: Any) -> Any:
        image, command = kw.pop("image"), kw.pop("command")
        c = self.client.containers.create(image, command, labels={**SPRINT_LABEL, **kw.pop("labels", {})}, **kw)
        self.ids.append(c.id)
        return c

    def cleanup(self) -> list[str]:
        gone = []
        for cid in reversed(self.ids):
            try:
                self.client.api.remove_container(cid, v=True, force=True)
                gone.append(cid)
            except Exception as e:  # reported by the caller
                print(f"WARNING: could not remove {cid}: {type(e).__name__}: {e}", file=sys.stderr)
        self.ids = [i for i in self.ids if i not in gone]
        return gone


# The mock upstream's fixed usage per message and its cost, specified INDEPENDENTLY of aiep.pricing (so the check below is not circular).
# Prices (config/model_pricing.json, claude-sonnet-5-5), per million tokens: input $2, cache read $0.10, output $10. Arithmetic:
#   100 input tokens      x $2.00 / 1e6 = $0.000200
#    10 cache-read tokens x $0.10 / 1e6 = $0.000001
#    20 output tokens     x $10.0 / 1e6 = $0.000200
#                                 total = $0.000401 per message
MOCK_USAGE = {"input_tokens": 100, "cache_read_input_tokens": 10, "output_tokens": 20}
MOCK_REQUEST_COST_USD = Decimal("0.000401")


class ScopedReaperApi:
    """The only Docker API surface the reaper phase hands to ``reaper.reap``: containers THIS phase created, and nothing else.

    ``reap(execute=True)`` sweeps every AIEP-labelled container on the daemon, and the phase runs it with a simulated clock six hours ahead,
    which would delete a live run's containers. Through this wrapper an out-of-scope container is invisible in listings, and inspecting or
    removing one by ID raises (and is recorded in ``denied``), so the destructive path cannot reach a container that is not ours even if a
    namespace collided. ``allowed_ids`` is a live view of the IDs the phase created; ``namespace`` must also prefix the run label.
    """

    def __init__(self, api: Any, allowed_ids: Callable[[], set[str]], namespace: str):
        self._api, self._ids, self.namespace, self.denied = api, allowed_ids, namespace, []

    def _in_scope(self, entry: dict) -> bool:
        labels = entry.get("Labels") or {}
        return entry.get("Id") in self._ids() and str(labels.get("aiep.run_id", "")).startswith(self.namespace)

    def containers(self, **kw: Any) -> list[dict]:
        return [e for e in self._api.containers(**kw) if self._in_scope(e)]

    def _guard(self, cid: str, what: str) -> None:
        if cid not in self._ids():
            self.denied.append((what, cid))
            raise PermissionError(f"reaper phase scope: refusing to {what} {cid}: this phase did not create it")

    def inspect_container(self, cid: str, *a: Any, **kw: Any) -> Any:
        self._guard(cid, "inspect")
        return self._api.inspect_container(cid, *a, **kw)

    def remove_container(self, cid: str, *a: Any, **kw: Any) -> Any:
        self._guard(cid, "remove")
        return self._api.remove_container(cid, *a, **kw)


class ScopedReaperClient:
    def __init__(self, api: ScopedReaperApi):
        self.api = api


def hardened(name: str, user: str, network: str, *, image: str = PY_REF, mem: str = "128m", extra_env: dict | None = None) -> dict:
    uid, _, gid = user.partition(":")
    return dict(image=image, command=["/bin/sleep", "3600"], name=name, user=user, detach=True, network=network, read_only=True,
                cap_drop=["ALL"], security_opt=["no-new-privileges"], mem_limit=mem, memswap_limit=mem, pids_limit=64,
                environment={"HOME": "/tmp", **(extra_env or {})},
                tmpfs={"/tmp": f"rw,nosuid,nodev,size=16m,uid={uid},gid={gid},mode=0700"})


def run_py(client: Any, container: Any, script: str, *, user: str, timeout: int = 60) -> tuple[int | None, str]:
    """Run ``python -I -c script`` in the container (python image) and return (exit code, stdout+stderr)."""
    ex = client.api.exec_create(container.id, [PY, "-I", "-c", script], user=user, stdout=True, stderr=True, privileged=False)
    out = client.api.exec_start(ex["Id"], demux=False)
    info = client.api.exec_inspect(ex["Id"])
    return info.get("ExitCode"), (out or b"").decode("utf-8", "replace")


def start_detached(client: Any, container: Any, script: str, *, user: str) -> None:
    ex = client.api.exec_create(container.id, [PY, "-I", "-c", script], user=user, stdout=False, stderr=False, privileged=False)
    client.api.exec_start(ex["Id"], detach=True)


PROBE = r'''
import json, socket, ssl, sys
targets = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
'''  # (arguments are baked into the script text by ``probe_script``)


def probe_script(dns: list[str], tcp: list[list], tls: list[str], udp_dns: list[str], interfaces: bool = True) -> str:
    return f'''
import json, socket, ssl
R = {{"dns": {{}}, "tcp": {{}}, "tls": {{}}, "udp_dns": {{}}}}
def dns(name):
    try: return sorted({{a[4][0] for a in socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)}})
    except Exception as e: return "FAIL " + type(e).__name__ + ": " + str(e)[:80]
def tcp(host, port):
    try:
        s = socket.create_connection((host, port), 4); s.close(); return "CONNECTED"
    except Exception as e: return "FAIL " + type(e).__name__ + ": " + str(e)[:80]
def tls(host):
    try:
        s = socket.create_connection((host, 443), 6)
        t = ssl.create_default_context().wrap_socket(s, server_hostname=host)
        r = t.version() + " " + t.cipher()[0] + " subject=" + str(dict(x[0] for x in t.getpeercert()["subject"]).get("commonName"))
        t.close(); return "TLS-OK " + r
    except Exception as e: return "FAIL " + type(e).__name__ + ": " + str(e)[:100]
def udp_dns(server):
    q = bytes.fromhex("beef01000001000000000000076578616d706c6503636f6d0000010001")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3); s.sendto(q, (server, 53)); d = s.recv(512); s.close()
        return "REPLY %d bytes" % len(d)
    except Exception as e: return "FAIL " + type(e).__name__ + ": " + str(e)[:60]
for n in {dns!r}: R["dns"][n] = dns(n)
for h, p in {tcp!r}: R["tcp"][h + ":" + str(p)] = tcp(h, p)
for h in {tls!r}: R["tls"][h] = tls(h)
for s_ in {udp_dns!r}: R["udp_dns"][s_] = udp_dns(s_)
R["resolv"] = open("/etc/resolv.conf").read().strip().splitlines()
R["routes"] = [l.split() for l in open("/proc/net/route").read().strip().splitlines()[1:]]
print(json.dumps(R))
'''


def inspect_nets(client: Any, container: Any) -> dict[str, Any]:
    container.reload()
    return {n: v.get("IPAddress") for n, v in container.attrs["NetworkSettings"]["Networks"].items()}


def phase_networks(client: Any, rec: Recorder) -> None:
    nets = ensure_networks(client)
    for name, attrs in nets.items():
        rec.fact(f"network {name}", {"id": attrs["Id"], "internal": attrs["Internal"], "driver": attrs["Driver"],
                                     "subnet": attrs["IPAM"]["Config"][0]["Subnet"], "options": attrs["Options"], "labels": attrs["Labels"]})
    rec.check("agent network is internal", nets[AGENT_NETWORK]["Internal"] is True)
    rec.check("egress network is not internal and has ICC off", nets[EGRESS_NETWORK]["Internal"] is False
              and nets[EGRESS_NETWORK]["Options"].get("com.docker.network.bridge.enable_icc") == "false")
    t = Tracked(client)
    try:
        g = t.create(**hardened("aiep.nettest.gw", "10001:10001", AGENT_NETWORK))
        a = t.create(**hardened("aiep.nettest.agent", "1000:1000", AGENT_NETWORK))
        o = t.create(**hardened("aiep.nettest.outsider", "1000:1000", EGRESS_NETWORK))
        x = t.create(**hardened("aiep.nettest.bridge", "1000:1000", "bridge"))
        client.networks.get(EGRESS_NETWORK).connect(g)  # exactly what GatewayManager.create does
        for c in (g, a, o, x):
            c.start()
        gw_ips, a_ips, o_ips, x_ips = (inspect_nets(client, c) for c in (g, a, o, x))
        rec.fact("gateway-stand-in networks", gw_ips)
        rec.fact("agent-stand-in networks", a_ips)
        rec.fact("outsider (egress only) networks", o_ips)
        rec.fact("default-bridge container networks", x_ips)
        rec.check("agent stand-in is attached to the agent network only", set(a_ips) == {AGENT_NETWORK})
        rec.check("gateway stand-in is attached to both AIEP networks and nothing else", set(gw_ips) == {AGENT_NETWORK, EGRESS_NETWORK})
        gw_agent_ip, gw_egress_ip = gw_ips[AGENT_NETWORK], gw_ips[EGRESS_NETWORK]
        listener = ("import socket,threading\ns=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',8080));s.listen(8)\n"
                    "while True:\n c,_=s.accept(); c.sendall(b'GW-LISTENER\\n'); c.close()\n")
        for c, user in ((g, "10001:10001"), (x, "1000:1000")):
            start_detached(client, c, listener, user=user)
        time.sleep(1.5)
        # ---- from the AGENT stand-in
        code, out = run_py(client, a, probe_script(
            ["aiep.nettest.gw", UPSTREAM_HOST, "example.com", "host.docker.internal", "google.com"],
            [["aiep.nettest.gw", 8080], [gw_egress_ip, 8080], ["1.1.1.1", 443], ["8.8.8.8", 443], ["1.1.1.1", 53], ["93.184.216.34", 80],
             [x_ips["bridge"], 8080], ["169.254.169.254", 80]],
            [UPSTREAM_HOST], ["1.1.1.1", "8.8.8.8"]), user="1000:1000")
        ap = json.loads(out) if code == 0 else {}
        rec.fact("agent probe", ap)
        rec.check("agent resolves and reaches the gateway by name", ap["tcp"].get("aiep.nettest.gw:8080") == "CONNECTED"
                  and isinstance(ap["dns"]["aiep.nettest.gw"], list) and ap["dns"]["aiep.nettest.gw"] == [gw_agent_ip], ap["dns"].get("aiep.nettest.gw"))
        for tgt in ("1.1.1.1:443", "8.8.8.8:443", "1.1.1.1:53", "93.184.216.34:80", "169.254.169.254:80"):
            rec.check(f"agent cannot reach public/link-local address {tgt}", ap["tcp"][tgt].startswith("FAIL"), ap["tcp"][tgt])
        for srv in ("1.1.1.1", "8.8.8.8"):
            rec.check(f"agent cannot use public DNS server {srv} (UDP)", ap["udp_dns"][srv].startswith("FAIL"), ap["udp_dns"][srv])
        for n in (UPSTREAM_HOST, "example.com", "google.com"):
            rec.check(f"agent cannot resolve public name {n}", str(ap["dns"][n]).startswith("FAIL"), ap["dns"][n])
        rec.check("agent cannot resolve host.docker.internal (no route to host services)", str(ap["dns"]["host.docker.internal"]).startswith("FAIL"),
                  ap["dns"]["host.docker.internal"])
        rec.check("agent cannot reach the gateway's EGRESS-side address", ap["tcp"][f"{gw_egress_ip}:8080"].startswith("FAIL"), ap["tcp"][f"{gw_egress_ip}:8080"])
        rec.check("agent cannot reach a container on the default bridge", ap["tcp"][f"{x_ips['bridge']}:8080"].startswith("FAIL"), ap["tcp"][f"{x_ips['bridge']}:8080"])
        rec.check("agent cannot complete a TLS handshake to the upstream host", ap["tls"][UPSTREAM_HOST].startswith("FAIL"), ap["tls"][UPSTREAM_HOST])
        rec.check("agent network has no default route", all(r[1] != "00000000" for r in ap["routes"]), ap["routes"])
        # ---- from the GATEWAY stand-in
        code, out = run_py(client, g, probe_script([UPSTREAM_HOST, "aiep.nettest.agent", "host.docker.internal"],
                                                   [["aiep.nettest.agent", 8080], [a_ips[AGENT_NETWORK], 22]], [UPSTREAM_HOST], []), user="10001:10001")
        gp = json.loads(out) if code == 0 else {}
        rec.fact("gateway probe", gp)
        rec.check("gateway resolves the upstream host", isinstance(gp["dns"][UPSTREAM_HOST], list) and bool(gp["dns"][UPSTREAM_HOST]), gp["dns"][UPSTREAM_HOST])
        rec.check("gateway completes a TLS handshake (certificate verified, no HTTP request sent) to the upstream host",
                  gp["tls"][UPSTREAM_HOST].startswith("TLS-OK"), gp["tls"][UPSTREAM_HOST])
        rec.check("gateway has a default route (via the egress network)", any(r[1] == "00000000" for r in gp["routes"]), gp["routes"])
        rec.fact("gateway reaches the agent network (by design: it shares it)", gp["tcp"])
        # ---- from the OUTSIDER (egress network only)
        code, out = run_py(client, o, probe_script([UPSTREAM_HOST], [[gw_egress_ip, 8080], [gw_agent_ip, 8080]], [UPSTREAM_HOST], []), user="1000:1000")
        op = json.loads(out) if code == 0 else {}
        rec.fact("outsider probe", op)
        rec.check("control: an egress-network container can reach the internet", op["tls"][UPSTREAM_HOST].startswith("TLS-OK"), op["tls"][UPSTREAM_HOST])
        rec.check("another egress-network container cannot reach the gateway's egress address (ICC off)", op["tcp"][f"{gw_egress_ip}:8080"].startswith("FAIL"),
                  op["tcp"][f"{gw_egress_ip}:8080"])
        rec.check("an egress-network container cannot reach the gateway's agent-network address", op["tcp"][f"{gw_agent_ip}:8080"].startswith("FAIL"),
                  op["tcp"][f"{gw_agent_ip}:8080"])
        # ---- the agent network has no VM-side address: nothing but the AIEP containers may answer anywhere on its subnet
        prefix = a_ips[AGENT_NETWORK].rsplit(".", 1)[0]
        own = {a_ips[AGENT_NETWORK], gw_agent_ip}
        hosts = [f"{prefix}.{i}" for i in range(1, 13) if f"{prefix}.{i}" not in own]
        scan = ("import socket, select, time\nHOSTS = %r\nopen_ports = []\nports = list(range(1, 1025)) + [2375, 2376, 5000, 8080, 58339]\n"
                "for host in HOSTS:\n for i in range(0, len(ports), 300):\n  socks = {}\n  poll = select.poll()\n  for port in ports[i:i + 300]:\n"
                "   s = socket.socket(); s.setblocking(False); s.connect_ex((host, port)); socks[s.fileno()] = (s, port); poll.register(s.fileno(), select.POLLOUT)\n"
                "  t0 = time.time(); pend = set(socks)\n  while pend and time.time() - t0 < 1.0:\n   for fd, ev in poll.poll(200):\n"
                "    s, port = socks[fd]\n    if s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0 and not ev & (select.POLLERR | select.POLLHUP): open_ports.append((host, port))\n"
                "    pend.discard(fd); poll.unregister(fd)\n  for s, _ in socks.values(): s.close()\nprint(open_ports)\n") % (hosts,)
        code, out = run_py(client, a, scan, user="1000:1000", timeout=300)
        rec.check(f"agent finds NO open TCP port on any other address of its subnet ({hosts[0]}-{hosts[-1]}, incl. the former VM ports 111 and 58339)",
                  code == 0 and out.strip() == "[]", out.strip()[:200])
        rec.check("the agent network has no gateway address in IPAM", not any(c_.get("Gateway") for c_ in nets[AGENT_NETWORK]["IPAM"]["Config"]),
                  nets[AGENT_NETWORK]["IPAM"]["Config"])
        # ---- host services seen from the gateway (the one place a firewall would matter)
        code, out = run_py(client, g, probe_script(["host.docker.internal", "gateway.docker.internal"], [], [], []), user="10001:10001")
        hp = json.loads(out) if code == 0 else {}
        rec.fact("gateway: host.docker.internal / gateway.docker.internal resolution", hp.get("dns"))
        import re
        import subprocess
        ls = subprocess.run(["lsof", "-iTCP", "-sTCP:LISTEN", "-nP"], capture_output=True, text=True).stdout
        ports = sorted({int(m.group(1)) for m in re.finditer(r"(?:\*|127\.0\.0\.1|\[::1\]|\[::\]):(\d+) \(LISTEN\)", ls)})[:6]
        rec.fact("macOS TCP listening ports tried from the gateway stand-in", ports)
        if ports:
            code, out = run_py(client, g, probe_script([], [["192.168.65.254", p_] for p_ in ports], [], []), user="10001:10001")
            reach = json.loads(out)["tcp"] if code == 0 else {}
            rec.fact("gateway stand-in -> macOS host services via host.docker.internal address", reach)
            open_ = [k for k, v in reach.items() if v == "CONNECTED"]
            rec.check("gateway stand-in cannot reach macOS host services (a container firewall would be needed otherwise)", not open_,
                      f"REACHABLE: {open_}" if open_ else "none reachable")
            code, out = run_py(client, a, probe_script([], [["192.168.65.254", p_] for p_ in ports], [], []), user="1000:1000")
            reach_a = json.loads(out)["tcp"] if code == 0 else {}
            rec.check("agent stand-in cannot reach macOS host services", not any(v == "CONNECTED" for v in reach_a.values()), reach_a)
    finally:
        removed = t.cleanup()
        rec.fact("temporary containers removed", len(removed))


def table(rec: Recorder, title: str, rows: list[tuple[str, Any, Any]]) -> None:
    """configured vs observed rows -> one PASS/FAIL each."""
    for name, configured, observed in rows:
        rec.check(f"{title}: {name}", configured == observed, f"configured={configured!r} observed={observed!r}")


def policy_rows(insp: dict, *, user: str, image_id: str, mem: int, pids: int, nano: int, networks: set[str], entrypoint: Any, cmd: Any,
                tmpfs: dict[str, str] | None, env_keys: set[str]) -> list[tuple[str, Any, Any]]:
    h, c = insp["HostConfig"], insp["Config"]
    return [("image ID", image_id, insp["Image"]), ("user", user, c.get("User")), ("read-only root", True, h.get("ReadonlyRootfs")),
            ("capabilities dropped", ["ALL"], h.get("CapDrop")), ("no added capabilities", None, h.get("CapAdd")),
            ("no-new-privileges", True, any("no-new-privileges" in x for x in h.get("SecurityOpt") or [])),
            ("not privileged", False, bool(h.get("Privileged"))), ("memory bytes", mem, h.get("Memory")),
            ("memory+swap bytes (no swap)", mem, h.get("MemorySwap")), ("pids limit", pids, h.get("PidsLimit")), ("nano CPUs", nano, h.get("NanoCpus")),
            ("no bind mounts", True, not h.get("Binds")), ("no mounts or volumes", True, not insp.get("Mounts")),
            ("no published ports", True, not h.get("PortBindings")), ("no devices", True, not h.get("Devices")),
            ("no PID namespace sharing", True, not h.get("PidMode")), ("IPC mode not host", True, h.get("IpcMode") != "host"),
            ("attached networks", sorted(networks), sorted(insp["NetworkSettings"]["Networks"])),
            ("entrypoint", entrypoint, c.get("Entrypoint")), ("command", cmd, c.get("Cmd")),
            ("tmpfs mounts", tmpfs, h.get("Tmpfs")), ("environment variable names", sorted(env_keys),
                                                      sorted({e.split("=", 1)[0] for e in c.get("Env") or []} & env_keys))]


STATUS_SCRIPT = r'''
import json, os, socket, ssl, sys, pwd, grp
R = {}
R["uid_gid"] = [os.getuid(), os.getgid(), os.getgroups(), os.geteuid(), os.getegid()]
try: pwd.getpwuid(os.getuid()); R["passwd_entry"] = True
except KeyError: R["passwd_entry"] = False
R["HOME"] = os.environ.get("HOME"); R["env_names"] = sorted(os.environ)
R["python"] = sys.version.split()[0]; R["executable"] = sys.executable; R["flags_isolated"] = sys.flags.isolated
mods = {}
for m in ("ssl", "http.client", "http.server", "hmac", "ipaddress", "json", "hashlib", "socket", "threading", "decimal", "urllib.parse"):
    try: __import__(m); mods[m] = True
    except Exception as e: mods[m] = repr(e)
R["imports"] = mods
R["openssl"] = ssl.OPENSSL_VERSION
ctx = ssl.create_default_context(); R["ca_certs_loaded"] = ctx.cert_store_stats().get("x509_ca"); R["verify_paths"] = list(ssl.get_default_verify_paths())[:4]
R["check_hostname"], R["verify_mode"] = ctx.check_hostname, str(ctx.verify_mode)
mounts = {}
for l in open("/proc/mounts").read().splitlines():
    f = l.split()
    if f[1] in ("/", "/tmp", "/opt/aiep-gw", "/opt/aiep", "/testbed", "/home/nonroot", "/dev/shm"): mounts[f[1]] = {"type": f[2], "opts": f[3]}
R["mounts"] = mounts
st = {}
for d in ("/tmp", "/opt/aiep-gw", "/opt/aiep", "/testbed", "/home/nonroot", "/dev/shm"):
    try: x = os.stat(d); st[d] = {"uid": x.st_uid, "gid": x.st_gid, "mode": oct(x.st_mode & 0o7777)}
    except Exception as e: st[d] = repr(e)
R["dirs"] = st
def can_write(path):
    try:
        p = os.path.join(path, ".aiep_probe"); open(p, "w").write("x"); os.unlink(p); return True
    except Exception as e: return type(e).__name__
R["writable"] = {"/": can_write("/"), "/tmp": can_write("/tmp"), "/usr/local/bin": can_write("/usr/local/bin")}
status = dict(l.split(":	", 1) for l in open("/proc/self/status").read().splitlines() if ":	" in l)
R["CapEff"] = status.get("CapEff", "").strip(); R["CapBnd"] = status.get("CapBnd", "").strip(); R["NoNewPrivs"] = status.get("NoNewPrivs", "").strip(); R["Seccomp"] = status.get("Seccomp", "").strip()
listen = []
for l in open("/proc/net/tcp").read().splitlines()[1:]:
    f = l.split()
    if f[3] == "0A":
        a, p = f[1].split(":"); ip = ".".join(str(int(a[i:i + 2], 16)) for i in (6, 4, 2, 0)); listen.append("%s:%d" % (ip, int(p, 16)))
R["listening"] = listen
ifs = {}
for l in open("/proc/net/dev").read().splitlines()[2:]: ifs[l.split(":")[0].strip()] = True
R["interfaces"] = sorted(ifs)
print(json.dumps(R))
'''


def phase_gateway(client: Any, rec: Recorder) -> None:
    from aiep.container_policy import GATEWAY_MEM_BYTES, GATEWAY_NANO_CPUS, GATEWAY_PIDS, gateway_tmpfs
    ensure_networks(client)
    pin = load_gateway_image(ROOT / "config" / "gateway_image.json")
    rec.fact("pinned gateway image", {"ref": pin.image, "id": pin.image_id, "python": pin.python, "uid": pin.uid, "gid": pin.gid})
    ctx = gateway_context(pin, agent_network=AGENT_NETWORK, egress_network=EGRESS_NETWORK, real_key=FAKE_KEY)
    gspec = GatewaySpec(run_id="sprint-gw", image=pin.image, ctx=ctx, run_token=DUMMY_TOKEN, budget_usd="0.50", code=build_gateway_component(ROOT))
    mgr = GatewayManager(client, gspec, min_poll_interval=0.0)
    t = Tracked(client)
    try:
        try:
            mgr.setup()  # create (policy-checked), connect egress, verify_inspect, start, inject + verify tree, start process, readiness probe
            rec.check("GatewayManager.setup(): create, verify_inspect, start, inject+verify (owner 10001:10001), process start, readiness", mgr.state == "ready", mgr.state)
        except Exception as e:
            rec.check("GatewayManager.setup()", False, f"{type(e).__name__}: {e}")
            return
        cid = mgr.container.id
        client.api.inspect_container(cid)
        insp = client.api.inspect_container(cid)
        (rec.dir / "gateway_inspect.json").write_text(json.dumps(insp, indent=2, sort_keys=True) + "\n")
        table(rec, "gateway inspect", policy_rows(
            insp, user="10001:10001", image_id=PY_ID, mem=GATEWAY_MEM_BYTES, pids=GATEWAY_PIDS, nano=GATEWAY_NANO_CPUS, networks={AGENT_NETWORK, EGRESS_NETWORK},
            entrypoint=["/bin/sleep"], cmd=["2147483647"], tmpfs=gateway_tmpfs(ctx), env_keys={"AIEP_UPSTREAM_API_KEY", "AIEP_RUN_TOKEN", "AIEP_BUDGET_USD", "AIEP_ALLOWED_HOSTS", "AIEP_LISTEN", "AIEP_LEDGER_PATH", "AIEP_STOP_FILE", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "TZ", "HOME"}))
        rec.fact("gateway inspect: ReadonlyRootfs/ShmSize/IpcMode/Ulimits/Runtime", {k: insp["HostConfig"].get(k) for k in ("ReadonlyRootfs", "ShmSize", "IpcMode", "Ulimits", "Runtime", "RestartPolicy", "LogConfig")})
        rec.fact("gateway inspect: image reference stored", {"Config.Image": insp["Config"]["Image"], "Id": insp["Image"]})
        code, out = run_py(client, mgr.container, STATUS_SCRIPT, user="10001:10001")
        st = json.loads(out) if code == 0 else {}
        rec.fact("gateway runtime status", st)
        (rec.dir / "gateway_runtime_status.json").write_text(json.dumps(st, indent=2, sort_keys=True) + "\n")
        rec.check("runtime uid:gid is 10001:10001 (Docker lists the primary gid in getgroups(); no other group)", st["uid_gid"][0:2] == [10001, 10001]
                  and st["uid_gid"][3:] == [10001, 10001] and set(st["uid_gid"][2]) <= {10001}, st["uid_gid"])
        rec.check("numeric uid has no passwd entry and Python 3.12 still starts", st["passwd_entry"] is False and st["python"].startswith("3.12."), [st["passwd_entry"], st["python"]])
        rec.check("HOME is /tmp", st["HOME"] == "/tmp", st["HOME"])
        for d_, mode in (("/tmp", "0o700"), ("/opt/aiep-gw", "0o700")):
            rec.check(f"tmpfs {d_} is owned 10001:10001 mode {mode}", st["dirs"][d_] == {"uid": 10001, "gid": 10001, "mode": mode}, st["dirs"][d_])
            rec.check(f"tmpfs {d_} mounted nosuid,nodev", all(x in st["mounts"][d_]["opts"].split(",") for x in ("nosuid", "nodev")) and st["mounts"][d_]["type"] == "tmpfs", st["mounts"][d_])
        rec.check("root filesystem is read-only", st["mounts"]["/"]["opts"].split(",")[0] == "ro" and st["writable"]["/"] != True, [st["mounts"]["/"], st["writable"]])  # noqa: E712
        rec.check("effective capabilities are empty and no-new-privileges is on", int(st["CapEff"], 16) == 0 and st["NoNewPrivs"] == "1", [st["CapEff"], st["CapBnd"], st["NoNewPrivs"]])
        rec.check("stdlib imports needed by the gateway all work", all(v is True for v in st["imports"].values()), st["imports"])
        rec.check("CA bundle loads and certificate verification is mandatory", (st["ca_certs_loaded"] or 0) > 100 and st["check_hostname"] and st["verify_mode"] in ("2", "VerifyMode.CERT_REQUIRED"),
                  [st["ca_certs_loaded"], st["check_hostname"], st["verify_mode"], st["openssl"]])
        code, out = run_py(client, mgr.container, probe_script([UPSTREAM_HOST], [], [UPSTREAM_HOST], []), user="10001:10001")
        gp = json.loads(out) if code == 0 else {}
        rec.check("HTTPS capability: DNS + verified TLS handshake to the upstream host from the real gateway container (no HTTP request)",
                  gp.get("tls", {}).get(UPSTREAM_HOST, "").startswith("TLS-OK"), gp.get("tls"))
        rec.fact("gateway listening sockets", st["listening"])
        rec.fact("gateway interfaces", st["interfaces"])
        ips = inspect_nets(client, mgr.container)
        rec.fact("gateway addresses", ips)
        bound_all = any(x.startswith("0.0.0.0:") for x in st["listening"])
        app_sockets = sorted(x for x in st["listening"] if not x.startswith("127.0.0.11:"))  # 127.0.0.11 is Docker's embedded DNS resolver
        rec.check("gateway binds ONLY its internal-interface address and loopback (not 0.0.0.0, not the egress address)",
                  not bound_all and app_sockets == sorted([f"{ips[AGENT_NETWORK]}:8080", "127.0.0.1:8080"]), st["listening"])
        # ---- ledger and stop channel as created by the real process
        code, out = run_py(client, mgr.container, "import os,json;x=os.stat('%s');print(json.dumps([x.st_uid,x.st_gid,oct(x.st_mode&0o777),x.st_size]))" % LEDGER_PATH, user="10001:10001")
        rec.check("ledger file exists and is owned 10001:10001", code == 0 and json.loads(out)[:2] == [10001, 10001], out.strip())
        rec.check("stop channel reports not-stopped (healthy gateway)", mgr.stop_reason() is None, mgr.stop_reason())
        # ---- agent-side client and an outsider
        a = t.create(**hardened("aiep.nettest.agent", "1000:1000", AGENT_NETWORK, extra_env={"ANTHROPIC_AUTH_TOKEN": DUMMY_TOKEN}))
        o = t.create(**hardened("aiep.nettest.outsider", "1000:1000", EGRESS_NETWORK))
        a.start()
        o.start()
        http = ("import http.client, json\nR = {}\n"
                "def req(path, headers, host=None, body=b'{}', method='POST'):\n"
                " c = http.client.HTTPConnection('aiep.gateway.sprint-gw', 8080, timeout=5)\n"
                " h = dict(headers)\n"
                " if host: h['Host'] = host\n"
                " c.request(method, path, body=body, headers={**h, 'Content-Type': 'application/json'})\n"
                " r = c.getresponse(); b = r.read()[:200]; c.close(); return r.status, b.decode('utf-8', 'replace')\n"
                "R['no_auth'] = req('/v1/messages', {})\n"
                "R['bad_token'] = req('/v1/messages', {'Authorization': 'Bearer wrong-token'})\n"
                "R['wrong_host'] = req('/v1/messages', {'Authorization': 'Bearer %s'}, host='evil.example:8080')\n"
                "R['bad_path_authed'] = req('/v1/other', {'Authorization': 'Bearer %s'})\n"
                "R['get_authed'] = req('/v1/messages', {'Authorization': 'Bearer %s'}, body=None, method='GET')\n"
                "print(json.dumps(R))\n") % (DUMMY_TOKEN, DUMMY_TOKEN, DUMMY_TOKEN)
        code, out = run_py(client, a, http, user="1000:1000")
        hr = json.loads(out) if code == 0 else {"error": out[-300:]}
        rec.fact("agent-side HTTP probes against the real gateway", hr)
        rec.check("agent reaches the gateway over the agent network (HTTP answered)", "no_auth" in hr and hr["no_auth"][0] == 401, hr.get("no_auth"))
        rec.check("missing and wrong tokens are rejected with 401", hr["no_auth"][0] == 401 and hr["bad_token"][0] == 401, [hr["no_auth"][0], hr["bad_token"][0]])
        rec.check("a wrong Host header is refused (421) before authentication", hr["wrong_host"][0] == 421, hr["wrong_host"])
        rec.check("an unlisted path and a GET are refused even with the right token", hr["bad_path_authed"][0] == 404 and hr["get_authed"][0] == 405,
                  [hr["bad_path_authed"][0], hr["get_authed"][0]])
        code, out = run_py(client, o, probe_script([], [[ips[EGRESS_NETWORK], 8080], [ips[AGENT_NETWORK], 8080]], [], []), user="1000:1000")
        op = json.loads(out)["tcp"] if code == 0 else {}
        rec.check("an unrelated egress-network client cannot reach the gateway via either interface address", all(v.startswith("FAIL") for v in op.values()), op)
        agent_ins = client.api.inspect_container(a.id)
        rec.check("agent stand-in environment carries only the dummy token, never the real key",
                  FAKE_KEY not in json.dumps(agent_ins) and DUMMY_TOKEN in json.dumps(agent_ins["Config"]["Env"]))
        rec.check("the real (fake) key appears in the gateway's environment in exactly one variable", [e.split("=", 1)[0] for e in insp["Config"]["Env"] if FAKE_KEY in e] == ["AIEP_UPSTREAM_API_KEY"],
                  [e.split("=", 1)[0] for e in insp["Config"]["Env"] if FAKE_KEY in e])
    finally:
        try:
            mgr.stop()
            rec.fact("gateway final stop reason", mgr.final_stop_reason())
            led = mgr.read_ledger()
            rec.fact("gateway ledger bytes (no requests were forwarded)", None if led is None else len(led))
        except Exception as e:
            rec.fact("gateway stop/ledger error", f"{type(e).__name__}: {e}")
        removed, err = mgr.teardown()
        rec.check("gateway container removed by its own manager (exact container)", removed, err)
        rec.fact("temporary containers removed", len(t.cleanup()))


EVIDENCE_BINARY = Path("/path/to/session-scratchpad/gate1b/claude.download")  # placeholder: the earlier session's temporary download of the pinned binary (hash and size below); this superseded script refuses to run
CLAUDE_SHA256 = "a982d38030b97e750848add2cbb2df6d9a70d5544a22ca9b216a349ba25865e8"
CLAUDE_SIZE = 373019152
FLAGS_IN_ARGS = ["--output-format", "--verbose", "--safe-mode", "--permission-mode", "--tools", "--no-session-persistence", "--model", "--max-budget-usd"]


def out_text(o: Any) -> str:
    return (o.stdout + (b"\n--stderr--\n" + o.stderr if o.stderr else b"")).decode("utf-8", "replace")


def prepare_agent(rec: Recorder, label: str = "A"):
    import hashlib
    from aiep import m2_run  # noqa: F401
    st = EVIDENCE_BINARY.stat()
    h = hashlib.sha256()
    with open(EVIDENCE_BINARY, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    rec.check("binary at the Gate 1b evidence path has the pinned size", st.st_size == CLAUDE_SIZE, st.st_size)
    rec.check("binary SHA-256 equals the pinned value (recomputed now)", h.hexdigest() == CLAUDE_SHA256, h.hexdigest())
    return m2_run.prepare(ROOT, instance_id="pytest-dev__pytest-5227", label=label, budget_usd="0.50", wall_clock_s=600, claude_binary=EVIDENCE_BINARY,
                          real_key=FAKE_KEY, agent_network=AGENT_NETWORK, egress_network=EGRESS_NETWORK,
                          stamp=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))


def start_agent(client: Any, rec: Recorder, t: "Tracked", p: Any):
    """Create (policy-checked), verify_inspect, start, identity probe, inject + verify seed and components. Returns (container, ectx)."""
    from aiep.agent_env import IDENTITY_SCRIPT, check_identity
    from aiep.agent_runner import _wait_running, agent_context, build_create_kwargs, exec_context
    from aiep.container_exec import inject_tar, list_tree, run_exec
    from aiep.container_policy import verify_inspect
    from aiep.seed import verify_listing
    spec = p.aspec
    kwargs = build_create_kwargs(spec)
    image, command = kwargs.pop("image"), kwargs.pop("command")
    c = client.containers.create(image, command, labels={**SPRINT_LABEL}, **kwargs) if False else client.containers.create(image, command, **kwargs)
    t.ids.append(c.id)
    insp = client.api.inspect_container(c.id)
    verify_inspect("agent", insp, agent_context(spec))
    rec.check("agent: policy-checked create and verify_inspect pass on the real container", True, c.id)
    c.start()
    _wait_running(c, time.monotonic, time.sleep, 120.0)
    ectx = exec_context(spec)
    probe = run_exec(client.api, c.id, ["/bin/sh", "-c", IDENTITY_SCRIPT], ctx=ectx, workdir="/", timeout_s=60)
    problems = check_identity(probe.stdout, spec.identity)
    rec.check("agent: identity probe (uid, gid, passwd entry, home) matches the verified identity 1000:1000 nonroot", not problems, problems or probe.stdout.decode().splitlines()[:4])
    for seed, dest in ((spec.seed, "/testbed"), (spec.components, "/opt/aiep")):
        t0 = time.monotonic()
        inject_tar(client.api, c.id, seed.tar, dest, ctx=ectx, timeout_s=900)
        t1 = time.monotonic()
        listing = list_tree(client.api, c.id, dest, ctx=ectx, timeout_s=900)
        probs = verify_listing(seed.manifest, listing, expect_owner=(spec.identity.uid, spec.identity.gid))
        rec.check(f"agent: {dest} injected and verified entry by entry (content, sizes, modes, owner 1000:1000)", not probs,
                  f"{len(seed.manifest.entries)} entries, tar {len(seed.tar)} bytes, inject {t1 - t0:.1f}s, verify {time.monotonic() - t1:.1f}s" if not probs else probs[:3])
    return c, ectx, insp


def phase_agent(client: Any, rec: Recorder) -> None:
    from aiep.agent_runner import agent_context, exec_context  # noqa: F401
    from aiep.container_exec import exec_env, run_exec
    ensure_networks(client)
    p = prepare_agent(rec)
    t = Tracked(client)
    ev = rec.dir
    try:
        c, ectx, insp = start_agent(client, rec, t, p)
        (ev / "agent_inspect.json").write_text(json.dumps(insp, indent=2, sort_keys=True) + "\n")
        api = client.api
        from aiep.agent_runner import build_create_kwargs, tmpfs_for
        from aiep.container_policy import AGENT_MEM_BYTES, AGENT_NANO_CPUS, AGENT_PIDS
        spec = p.aspec
        akw = build_create_kwargs(spec)
        table(rec, "agent inspect", policy_rows(insp, user="1000:1000", image_id="sha256:c91459f86ed261ab81871b086d7ca2d9fe16abd2ddf374bf172edd5d991fd28b", mem=AGENT_MEM_BYTES,
                                                pids=AGENT_PIDS, nano=AGENT_NANO_CPUS, networks={AGENT_NETWORK}, entrypoint=insp["Config"]["Entrypoint"], cmd=["/bin/sleep", "2147483647"],
                                                tmpfs=tmpfs_for(spec), env_keys=set(akw["environment"])))
        h_ = insp["HostConfig"]
        env_ = dict(e.split("=", 1) for e in insp["Config"]["Env"])
        table(rec, "agent inspect", [("ShmSize bytes (64m)", 64 * 1024**2, h_.get("ShmSize")), ("IpcMode", "private", h_.get("IpcMode")), ("NetworkMode", AGENT_NETWORK, h_.get("NetworkMode")),
                                     ("working dir", "/testbed", insp["Config"].get("WorkingDir")), ("no exposed ports", None, insp["Config"].get("ExposedPorts") or None),
                                     ("env ANTHROPIC_BASE_URL is the gateway", spec.gateway_url, env_.get("ANTHROPIC_BASE_URL")),
                                     ("env ANTHROPIC_AUTH_TOKEN is the dummy run token", spec.run_token, env_.get("ANTHROPIC_AUTH_TOKEN")),
                                     ("env CLAUDE_CODE_MAX_OUTPUT_TOKENS", "64000", env_.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS")),
                                     ("no ANTHROPIC_API_KEY variable", False, "ANTHROPIC_API_KEY" in env_), ("real key absent from inspect", False, FAKE_KEY in json.dumps(insp))])

        def run_claude(args: list[str], *, stdin: bytes | None = None, timeout: float = 60, name: str = "") -> Any:
            o = run_exec(api, c.id, ["/opt/aiep/claude", *args], ctx=ectx, workdir="/testbed", env=exec_env(ectx), stdin=stdin, timeout_s=timeout)
            if name:
                (ev / f"claude_{name}.txt").write_text(f"exit={o.exit_code} timed_out={o.timed_out} truncated={o.truncated}\n" + out_text(o))
            return o
        # ---- file and permissions
        ex = api.exec_create(c.id, ["/bin/sh", "-c", "ls -l /opt/aiep/claude; id; uname -m; head -c 400 /proc/cpuinfo | head -8; grep -o -w -E 'avx2|avx|sse4_2|avx512f' /proc/cpuinfo | sort -u | tr '\\n' ' '"],
                             user="1000:1000", workdir="/testbed")
        info = api.exec_start(ex["Id"]).decode("utf-8", "replace")
        rec.fact("claude file, id, uname, cpu flags", info.strip().splitlines())
        rec.check("claude is present, executable (-rwxr-xr-x), owned by the agent account (nonroot = 1000:1000, verified entry by entry above)",
                  "-rwxr-xr-x" in info and "nonroot nonroot" in info and str(CLAUDE_SIZE) in info and "uid=1000(nonroot) gid=1000(nonroot)" in info, info.splitlines()[0])
        # ---- --version (Rosetta, CPU instructions, dynamic loading, startup as 1000:1000 on a read-only root)
        t0 = time.monotonic()
        o = run_claude(["--version"], timeout=120, name="version")
        rec.check("claude --version runs as 1000:1000 in the agent image (Rosetta, libraries, read-only root, noexec tmpfs)",
                  o.exit_code == 0 and b"2.1.328" in o.stdout, f"exit={o.exit_code} out={out_text(o)[:200]!r} in {time.monotonic() - t0:.1f}s")
        if o.exit_code != 0:
            rec.fact("FIRST FAILURE: claude --version", out_text(o)[:1500])
            return
        o = run_claude(["--help"], timeout=120, name="help")
        helptext = o.stdout.decode("utf-8", "replace")
        rec.check("claude --help runs", o.exit_code == 0 and "Usage:" in helptext, f"exit={o.exit_code} bytes={len(o.stdout)}")
        for flag in FLAGS_IN_ARGS + ["--print", "bypassPermissions"]:
            rec.check(f"Linux binary --help documents {flag}", flag in helptext)
        for absent in ("--bare", "--max-turns"):
            rec.fact(f"Linux binary --help mentions {absent}", absent in helptext)
        import re
        m = re.search(r"--permission-mode <mode>.*?\(choices: ([^)]*)\)", helptext, re.S)
        rec.fact("permission-mode choices (Linux binary)", m.group(1).split() if m else None)
        m = re.search(r"--output-format <format>.*?\(choices: ([^)]*)\)", helptext, re.S)
        rec.fact("output-format choices (Linux binary)", m.group(1) if m else None)
        # ---- flag syntax with the production argument list: no API request can leave (no gateway exists, the agent has no egress)
        o = run_claude(["--definitely-not-a-flag"], timeout=60, name="unknown_flag")
        rec.check("negative control: an unknown flag is rejected by the parser", o.exit_code not in (0, None) and b"unknown option" in (o.stdout + o.stderr), out_text(o)[:160])
        o = run_claude(["-p", "--permission-mode", "definitely-not-a-mode"], timeout=60, name="bad_mode")
        rec.check("negative control: an invalid --permission-mode is rejected", o.exit_code not in (0, None) and b"permission" in (o.stdout + o.stderr).lower(), out_text(o)[:200])
        args = list(p.aspec.claude_args)
        o = run_claude(args, stdin=b"say ok", timeout=45, name="production_args_no_gateway")
        text = out_text(o)
        rec.check("production argument list is accepted by the Linux binary's parser (no 'unknown option' / 'invalid' / 'required' errors)",
                  not any(k in text.lower() for k in ("unknown option", "error: option", "invalid argument", "missing required")), text[:300])
        init = next((json.loads(l) for l in o.stdout.decode("utf-8", "replace").splitlines() if l.startswith("{") and '"subtype":"init"' in l.replace(" ", "")), None)
        rec.fact("stream-json init event (what --tools/--model/--permission-mode actually produced)", init)
        if init:
            rec.check("init event: model is claude-sonnet-5-5", init.get("model") == "claude-sonnet-5-5", init.get("model"))
            rec.check("init event: permission mode is bypassPermissions", init.get("permissionMode") == "bypassPermissions", init.get("permissionMode"))
            rec.check("init event: tool list is exactly Bash, Read, Edit, Write, Glob, Grep (no WebSearch/WebFetch)",
                      sorted(init.get("tools", [])) == sorted(["Bash", "Read", "Edit", "Write", "Glob", "Grep"]), init.get("tools"))
            rec.fact("init event: other fields", {k: v for k, v in init.items() if k not in ("tools", "model", "permissionMode")})
        else:
            rec.check("a stream-json init event was emitted before any API call", False, text[:300])
        # ---- resources actually used
        api.exec_create  # noqa: B018
        st = api.stats(c.id, stream=False)
        rec.fact("agent memory (bytes): usage / max_usage / limit", [st["memory_stats"].get("usage"), st["memory_stats"].get("max_usage"), st["memory_stats"].get("limit")])
        rec.fact("agent pids current", st.get("pids_stats", {}).get("current"))
        from aiep.container_exec import quiesce
        survivors = quiesce(api, c.id, ctx=ectx)
        rec.check("quiesce kills every agent process (only PID 1 remains)", survivors == [], survivors)
        insp2 = api.inspect_container(c.id)
        rec.check("no secret-looking value or real key in the agent container's inspect output", FAKE_KEY not in json.dumps(insp2)
                  and p.aspec.run_token in json.dumps(insp2["Config"]["Env"]))
    finally:
        rec.fact("temporary containers removed", len(t.cleanup()))


MOCK_NET = "aiep-sprint-mock-net"  # TEMPORARY: joins the gateway and the mock upstream (the egress network has ICC off by design)
MOCK_HOST = "aiep-mock-upstream"
LAUNCH = ("import sys\nfrom aiep.gateway import main, UpstreamConfig\n"
          "sys.exit(main(upstream=UpstreamConfig(host='%s', port=8443, ca_file='/tmp/mock-ca.pem', allow_private_addresses=True)))\n" % MOCK_HOST)


LAUNCH_DIAG = ("import sys\nimport aiep.gateway as G\nfrom dataclasses import dataclass\n\n@dataclass\nclass Cfg(G.GatewayConfig):\n    max_tokens_cap: int = 1000000\n\n"
               "G.GatewayConfig = Cfg\nsys.exit(G.main(upstream=G.UpstreamConfig(host='%s', port=8443, ca_file='/tmp/mock-ca.pem', allow_private_addresses=True)))\n" % MOCK_HOST)


def make_certs(d: Path) -> dict[str, bytes]:
    import subprocess
    d.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(list(a), cwd=d, check=True, capture_output=True)  # noqa: E731
    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-subj", "/CN=AIEP Sprint Test CA", "-days", "2")
    run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "server.key", "-out", "server.csr", "-subj", f"/CN={MOCK_HOST}")
    (d / "ext.cnf").write_text(f"subjectAltName=DNS:{MOCK_HOST}\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n")
    run("openssl", "x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", "server.pem", "-days", "2", "-extfile", "ext.cnf")
    return {n: (d / n).read_bytes() for n in ("ca.pem", "server.pem", "server.key")}


def put_files(client: Any, container: Any, user: str, dest: str, files: dict[str, tuple[bytes, int]]) -> None:
    """Extract files into a tmpfs directory as ``user`` over exec stdin (the same mechanism the orchestrator uses)."""
    from aiep.container_exec import ExecSession
    from aiep.seed import build_component_archive
    run_sh(client, container, user, f"mkdir -p {dest}")
    tar = build_component_archive(files).tar
    sess = ExecSession(client.api, container.id, {"cmd": ["/bin/tar", "-x", "-p", "--no-same-owner", "-f", "-", "-C", dest], "user": user, "workdir": "/",
                                                    "environment": None, "tty": False}, stdin=tar).start()
    sess.wait(60)
    out = sess.outcome()
    if out.exit_code != 0:
        raise RuntimeError(f"putting files failed: {out.stderr[-200:]!r}")


def run_sh(client: Any, container: Any, user: str, cmd: str, env: dict | None = None) -> tuple[int | None, str]:
    ex = client.api.exec_create(container.id, ["/bin/sh", "-c", cmd], user=user, stdout=True, stderr=True, privileged=False, environment=env)
    out = client.api.exec_start(ex["Id"])
    return client.api.exec_inspect(ex["Id"]).get("ExitCode"), (out or b"").decode("utf-8", "replace")


def launch_gateway(client: Any, gw: Any, name: str, port: int, run_id: str, token: str, budget: str, script: str = "launch.py") -> None:
    env = {"AIEP_LISTEN": f"internal:{port}", "AIEP_LEDGER_PATH": f"/tmp/ledger_{name}.jsonl", "AIEP_STOP_FILE": f"/tmp/stop_{name}",
           "AIEP_ALLOWED_HOSTS": f"aiep.gateway.{run_id}:{port}", "AIEP_BUDGET_USD": budget, "AIEP_RUN_TOKEN": token}
    ex = client.api.exec_create(gw.id, ["/bin/sh", "-c", f"exec /usr/local/bin/python3 /tmp/{script} 2>/tmp/gw_{name}.err"], user="10001:10001",
                                stdout=False, stderr=False, privileged=False, environment=env)
    client.api.exec_start(ex["Id"], detach=True)


def read_file(client: Any, c: Any, user: str, path: str) -> str:
    code, out = run_sh(client, c, user, f"cat {path} 2>/dev/null || true")
    return out


CLIENT = r'''
import http.client, json, sys, time
RUN, TOKEN = sys.argv[1], sys.argv[2]
def call(port, path="/v1/messages", body=None, headers=None, token=TOKEN, auth="bearer", read_all=True):
    h = {"Content-Type": "application/json", **(headers or {})}
    if token and auth == "bearer": h["Authorization"] = "Bearer " + token
    if token and auth == "x-api-key": h["x-api-key"] = token
    c = http.client.HTTPConnection("aiep.gateway." + RUN, port, timeout=20)
    try:
        c.request("POST", path, body=json.dumps(body if body is not None else {}).encode(), headers=h)
        r = c.getresponse(); data = r.read() if read_all else b""
        return {"status": r.status, "ctype": r.getheader("content-type"), "body": data.decode("utf-8", "replace")}
    except Exception as e:
        return {"error": type(e).__name__ + ": " + str(e)[:120]}
    finally:
        c.close()
OK = {"model": "claude-sonnet-5-5", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}
R = {}
SC = json.loads(sys.argv[3])
for name, spec in SC.items():
    R[name] = [call(spec["port"], **step) if not isinstance(step, str) else None for step in spec["steps"]]
print(json.dumps(R))
'''


def phase_integration(client: Any, rec: Recorder) -> None:
    from aiep import pricing as PR
    from aiep.agent_env import experiment_env  # noqa: F401
    from aiep.agent_runner import exec_context
    from aiep.container_exec import exec_env, quiesce, run_exec
    from aiep.seed import verify_listing
    from aiep.container_exec import inject_tar, list_tree
    import hashlib
    ensure_networks(client)
    certs = make_certs(rec.dir / "mock_certs")
    p = prepare_agent(rec)
    run_id, token = p.gspec.run_id, p.gspec.run_token
    gspec = p.gspec
    t = Tracked(client)
    mgr = GatewayManager(client, gspec, min_poll_interval=0.0)
    mock_net = None
    try:
        mock_net = client.networks.create(MOCK_NET, driver="bridge", labels={**SPRINT_LABEL})
        # ---- mock upstream (python image, egress-free: only on the temporary mock network)
        mock = t.create(**hardened(MOCK_HOST, "10002:10002", MOCK_NET, extra_env={}))
        mock.start()
        put_files(client, mock, "10002:10002", "/tmp/mock", {"server.pem": (certs["server.pem"], 0o644), "server.key": (certs["server.key"], 0o600),
                                                              "mock.py": ((ROOT / "scripts" / "m2_mock_upstream.py").read_bytes(), 0o644)})
        ex = client.api.exec_create(mock.id, ["/bin/sh", "-c", f"exec {PY} /tmp/mock/mock.py /tmp/requests.jsonl 2>/tmp/mock.err"], user="10002:10002", stdout=False, stderr=False, privileged=False)
        client.api.exec_start(ex["Id"], detach=True)
        # ---- gateway (production create path: policy-checked, verify_inspect), code injected and verified exactly like production, but the process
        #      is started by the validation launcher so it can talk to the mock (the production `-m aiep.gateway` would contact the real upstream)
        mgr.create()
        mgr.start()
        client.networks.get(MOCK_NET).connect(mgr.container)
        gw = mgr.container
        inject_tar(client.api, gw.id, gspec.code.tar, "/opt/aiep-gw", ctx=mgr.ectx)
        probs = verify_listing(gspec.code.manifest, list_tree(client.api, gw.id, "/opt/aiep-gw", ctx=mgr.ectx), expect_owner=(10001, 10001))
        rec.check("gateway code injected and verified (owner 10001:10001) before any process starts", not probs, probs[:3])
        put_files(client, gw, "10001:10001", "/tmp", {"mock-ca.pem": (certs["ca.pem"], 0o644), "launch.py": (LAUNCH.encode(), 0o644),
                                                          "launch_diag.py": (LAUNCH_DIAG.encode(), 0o644)})
        # the mock must be reachable from the gateway over the temporary network, and NOT from the agent
        time.sleep(1.5)
        code, out = run_py(client, gw, probe_script([MOCK_HOST], [[MOCK_HOST, 8443]], [], []), user="10001:10001")
        gp = json.loads(out)
        rec.check("gateway reaches the mock upstream over the temporary mock network", gp["tcp"][f"{MOCK_HOST}:8443"] == "CONNECTED", gp["tcp"])
        # ---- agent (real image, real Claude binary)
        c, ectx, insp = start_agent(client, rec, t, p)
        code, out = run_py(client, mock, probe_script([MOCK_HOST], [[MOCK_HOST, 8443]], [], []), user="10002:10002")
        # ---- 1. real Claude Code -> gateway -> mock
        api = client.api
        gw_ip = inspect_nets(client, gw)

        def mock_requests() -> list[dict]:
            return [json.loads(l) for l in read_file(client, mock, "10002:10002", "/tmp/requests.jsonl").splitlines() if l.strip()]

        def claude_once(tag: str) -> tuple[Any, dict | None]:
            t0 = time.monotonic()
            o = run_exec(api, c.id, ["/opt/aiep/claude", *p.aspec.claude_args], ctx=ectx, workdir="/testbed", env=exec_env(ectx), stdin=b"Reply with the single word: ok",
                         timeout_s=240, max_stdout=8 * 1024 * 1024)
            (rec.dir / f"claude_via_gateway_{tag}_stdout.jsonl").write_bytes(o.stdout)
            lines = [json.loads(l) for l in o.stdout.decode("utf-8", "replace").splitlines() if l.startswith("{")]
            result = next((l for l in reversed(lines) if l.get("type") == "result"), None)
            rec.fact(f"claude run [{tag}]", {"exit": o.exit_code, "seconds": round(time.monotonic() - t0, 1), "stdout_bytes": len(o.stdout),
                                            "result": {k: result.get(k) for k in ("subtype", "is_error", "num_turns", "result", "total_cost_usd")} if result else None})
            return o, result
        # the PRODUCTION gateway cap (64000), exactly as shipped; Claude is pinned to it by CLAUDE_CODE_MAX_OUTPUT_TOKENS (see aiep.limits)
        launch_gateway(client, gw, "claude", 8080, run_id, token, "0.50")
        time.sleep(2.5)
        err = read_file(client, gw, "10001:10001", "/tmp/gw_claude.err")
        listening = json.loads(run_py(client, gw, STATUS_SCRIPT, user="10001:10001")[1])["listening"]
        expect = sorted([f"{gw_ip[AGENT_NETWORK]}:8080", "127.0.0.1:8080", f"{gw_ip[MOCK_NET]}:8080"])
        rec.check("gateway instance listens only on its non-egress interfaces + loopback (here: agent net, the TEMPORARY mock net, loopback; never 0.0.0.0 or the egress address)",
                  sorted(x for x in listening if not x.startswith("127.0.0.11:") and x.endswith(":8080")) == expect and gw_ip[EGRESS_NETWORK] not in " ".join(listening), [listening, err[:200]])
        n_before = len(mock_requests())  # (zero here: the gateway instance is fresh)
        o, result = claude_once("production_cap")
        rec.check("claude completed a turn through the gateway against the mock upstream (result event present, not an error)", bool(result) and not result.get("is_error"), result and result.get("result"))
        reqs = mock_requests()[n_before:]
        rec.fact("requests the mock upstream received from Claude (via the gateway)", [{k: r[k] for k in ("path", "model", "max_tokens", "stream", "tool_types", "n_tools")} for r in reqs])
        msgs = [r for r in reqs if r["path"].startswith("/v1/messages") and "count_tokens" not in r["path"]]
        rec.check("the env pin takes effect: Claude sends max_tokens exactly 64000 (not its 128000 default)", bool(msgs) and {r["max_tokens"] for r in msgs} == {64000}, sorted({r["max_tokens"] for r in msgs}, key=lambda x: (x is None, x)))
        rec.fact("max_tokens values Claude Code 2.1.328 sends with the pin", sorted({r["max_tokens"] for r in msgs}, key=lambda x: (x is None, x)))
        rec.check("every upstream message request used the pinned model claude-sonnet-5-5", bool(msgs) and all(r["model"] == "claude-sonnet-5-5" for r in msgs), sorted({str(r["model"]) for r in reqs}))
        rec.check("every upstream message request had max_tokens within the PRODUCTION gateway cap (<= 64000) [Claude Code's own default is 128000; pinned via CLAUDE_CODE_MAX_OUTPUT_TOKENS]", bool(msgs) and all(isinstance(r["max_tokens"], int) and r["max_tokens"] <= 64000 for r in msgs),
                  [r["max_tokens"] for r in msgs], status="PASS" if msgs and all(isinstance(r["max_tokens"], int) and r["max_tokens"] <= 64000 for r in msgs) else "FAIL")
        rec.check("no request declared a server-side tool type", all(t_ in ([], ["custom"]) for t_ in (r["tool_types"] for r in reqs)), [r["tool_types"] for r in reqs])
        stop = read_file(client, gw, "10001:10001", "/tmp/stop_claude").strip()
        rec.check("the gateway did not stop the run (no model/tool/metering violation by Claude Code itself)", stop == "", stop or "no stop reason")
        real_hash = hashlib.sha256(FAKE_KEY.encode()).hexdigest()
        rec.check("upstream saw the gateway's key (x-api-key) on every request and NEVER the agent's token or an Authorization header",
                  bool(reqs) and all(r["x_api_key_sha256"] == real_hash and not r["has_authorization_header"] for r in reqs), [(r["x_api_key_sha256"][:8], r["has_authorization_header"]) for r in reqs][:4])
        ledger = read_file(client, gw, "10001:10001", "/tmp/ledger_claude.jsonl")
        entries = [json.loads(l) for l in ledger.splitlines() if l.strip()]
        rec.fact("gateway ledger for the Claude run", [{k: e.get(k) for k in ("seq", "status", "outcome", "usage", "cost_usd_computed", "cumulative_usd_computed")} for e in entries])
        metered = [e for e in entries if isinstance(e.get("usage"), dict)]
        rec.check("ledger has a metered entry for every message request Claude made (usage measured from the stream)", len(metered) == len(msgs) and len(msgs) >= 1, [len(metered), len(msgs)])
        pricing = PR.load_pricing(ROOT / "config" / "model_pricing.json", "claude-sonnet-5-5")
        from decimal import Decimal
        # the expected amount is the LITERAL $0.000401 per message (see MOCK_REQUEST_COST_USD for the arithmetic), not recomputed by aiep.pricing
        rec.check("aiep.pricing agrees with the independently worked per-message cost $0.000401",
                  PR.compute_cost(pricing, PR.usage_from_api(MOCK_USAGE)).usd == MOCK_REQUEST_COST_USD)
        exp = MOCK_REQUEST_COST_USD * len(msgs)
        got = Decimal(entries[-1]["cumulative_usd_computed"]) if entries else Decimal(-1)
        rec.check("cumulative computed cost equals the literal expected cost ($0.000401 x number of messages)", got == exp and got > 0, f"got={got} expected={exp}")
        agent_env = run_sh(client, c, "1000:1000", "env")[1]
        stdout_all = o.stdout.decode("utf-8", "replace") + o.stderr.decode("utf-8", "replace")
        rec.check("the real (fake) key appears nowhere in the agent's environment, inspect output, or Claude's stdout/stderr",
                  FAKE_KEY not in agent_env and FAKE_KEY not in json.dumps(client.api.inspect_container(c.id)) and FAKE_KEY not in stdout_all)
        rec.check("the agent's environment holds only the dummy token and the gateway URL for Anthropic access",
                  f"ANTHROPIC_AUTH_TOKEN={token}" in agent_env and "ANTHROPIC_API_KEY" not in agent_env and f"ANTHROPIC_BASE_URL={gspec.url}" in agent_env)
        # ---- 2. scenario gateways, driven from the agent container
        base = {"claude-ok": {"model": "claude-sonnet-5-5", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}}
        OKB = base["claude-ok"]
        scen = {
            "normal": (8091, "5.0", [dict(body={**OKB}), dict(body={**OKB, "stream": True}), dict(path="/v1/messages/count_tokens?beta=true", body={"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": "x"}]}),
                                     dict(body={**OKB}, auth="x-api-key"), dict(body={**OKB}, token="wrong"), dict(body={**OKB}, token=""),
                                     dict(body={**OKB, "max_tokens": 64001}), dict(body={"model": "claude-sonnet-5-5", "messages": []})]),
            "malformed_stream": (8092, "5.0", [dict(body={**OKB, "stream": True}, headers={"anthropic-mock-mode": "malformed_usage"}), dict(body={**OKB})]),
            "no_usage_json": (8093, "5.0", [dict(body={**OKB}, headers={"anthropic-mock-mode": "no_usage"}), dict(body={**OKB})]),
            "early_close": (8094, "5.0", [dict(body={**OKB, "stream": True, "max_tokens": 777}, headers={"anthropic-mock-mode": "early_close"}), dict(body={**OKB})]),
            "model_violation": (8095, "5.0", [dict(body={**OKB, "model": "claude-haiku-4-5"}), dict(body={**OKB})]),
            "tool_violation": (8096, "5.0", [dict(body={**OKB, "tools": [{"type": "web_search_20250305", "name": "web_search"}]}), dict(body={**OKB})]),
            "budget": (8097, None, [dict(body={**OKB}), dict(body={**OKB}), dict(body={**OKB}), dict(body={**OKB})]),
            "upstream_500": (8098, "5.0", [dict(body={**OKB}, headers={"anthropic-mock-mode": "http_500"}), dict(body={**OKB})]),
        }
        one = MOCK_REQUEST_COST_USD  # literal (see the arithmetic at MOCK_REQUEST_COST_USD)
        budget = str((one * Decimal("1.5")).quantize(Decimal("0.0000001")))
        rec.fact("per-request computed cost of the mock's fixed usage; budget used for the budget scenario", [str(one), budget])
        for name, (port, bud, _) in scen.items():
            launch_gateway(client, gw, name, port, run_id, token, bud or budget)
        time.sleep(4)
        for name, (port, _, _) in scen.items():
            e = read_file(client, gw, "10001:10001", f"/tmp/gw_{name}.err")
            if e.strip():
                rec.fact(f"gateway scenario {name} stderr", e[:300])
        spec = {name: {"port": port, "steps": steps} for name, (port, _, steps) in scen.items()}
        code, out = run_py_in_agent(client, c, CLIENT, [run_id, token, json.dumps(spec)])
        R = json.loads(out) if code == 0 else {}
        (rec.dir / "scenario_results.json").write_text(json.dumps(R, indent=2) + "\n")
        st_ = lambda n, i: R[n][i].get("status")  # noqa: E731
        n_ = R["normal"]
        rec.check("normal: non-streaming request is forwarded and the JSON response (with usage) is returned", st_("normal", 0) == 200 and '"output_tokens": 20' in n_[0]["body"], n_[0].get("status"))
        rec.check("normal: streaming response is forwarded intact (SSE content type, all events through message_stop)",
                  st_("normal", 1) == 200 and "text/event-stream" in (n_[1]["ctype"] or "") and "message_start" in n_[1]["body"] and "message_stop" in n_[1]["body"]
                  and '"text": "ok"' in n_[1]["body"] and '"text": " done"' in n_[1]["body"], [n_[1].get("status"), len(n_[1].get("body", ""))])
        rec.check("normal: count_tokens is allowed and not charged", st_("normal", 2) == 200 and '"input_tokens": 42' in n_[2]["body"])
        rec.check("normal: the x-api-key header form of the run token is accepted", st_("normal", 3) == 200)
        rec.check("normal: wrong token and no token are rejected (401)", st_("normal", 4) == 401 and st_("normal", 5) == 401, [st_("normal", 4), st_("normal", 5)])
        rec.check("normal: max_tokens above the cap is refused (400)", st_("normal", 6) == 400, n_[6].get("body", "")[:120])
        rec.check("normal: a request without max_tokens is refused (400)", st_("normal", 7) == 400, n_[7].get("body", "")[:120])
        raw_led = lambda n: read_file(client, gw, "10001:10001", f"/tmp/ledger_{n}.jsonl")  # noqa: E731
        # a request's first ledger line is its durable 'forwarding' marker; the finishing line (checked below) is the one with usage and outcome
        led = lambda n: [e for e in (json.loads(l) for l in raw_led(n).splitlines() if l.strip()) if e.get("outcome") != "forwarding"]  # noqa: E731
        stp = lambda n: read_file(client, gw, "10001:10001", f"/tmp/stop_{n}").strip()  # noqa: E731
        ln = led("normal")
        m_ = [e for e in ln if isinstance(e.get("usage"), dict) and e.get("cost_usd_computed") not in (None, "0")]
        rec.check("normal: ledger usage is exact for streaming and non-streaming (3 metered messages x {in 100, cache-read 10, out 20})",
                  len(m_) == 3 and all(e["usage"]["input_tokens"] == 100 and e["usage"]["output_tokens"] == 20 and e["usage"]["cache_read_tokens"] == 10 for e in m_), [e.get("usage") for e in m_])
        rec.check("normal: cumulative cost equals 3 x the computed per-request cost, count_tokens cost 0",
                  Decimal(ln[-1]["cumulative_usd_computed"]) == one * 3 if ln else False, [ln[-1].get("cumulative_usd_computed") if ln else None, str(one * 3)])
        rec.check("normal: nothing stopped the gateway", stp("normal") == "", stp("normal"))
        from aiep import ledger as LED
        audits = {n: LED.audit(raw_led(n)) for n in scen}
        rec.check("every request the gateway started forwarding has a finishing ledger line in every scenario (no unresolved in-flight request)",
                  all(a["complete"] for a in audits.values()), {n: [len(a["unresolved"]), a["unparseable_lines"]] for n, a in audits.items()})
        rec.check("normal: every ledger line records the requested max_tokens, the cap and the stream mode",
                  all("requested_max_tokens" in e and "max_tokens_cap" in e and "stream" in e for e in (json.loads(l) for l in raw_led("normal").splitlines() if l.strip()) if e.get("rid")))
        ms = R["malformed_stream"]
        rec.check("malformed usage in a stream: fails closed (stop reason metering_failure) and later requests are refused",
                  stp("malformed_stream") == "metering_failure" and st_("malformed_stream", 1) == 403, [stp("malformed_stream"), st_("malformed_stream", 1)])
        nu = R["no_usage_json"]
        rec.check("missing usage in a non-streaming response: fails closed (metering_failure) and later requests are refused",
                  stp("no_usage_json") == "metering_failure" and st_("no_usage_json", 1) == 403, [stp("no_usage_json"), st_("no_usage_json", 1)])
        le = led("early_close")
        rec.check("early stream termination: recorded as an incomplete stream charged at the max_tokens upper bound, gateway keeps running",
                  bool(le) and le[0].get("usage_incomplete_upper_bound") is True and le[0]["usage"]["output_tokens"] == 777 and stp("early_close") == "" and st_("early_close", 1) == 200,
                  [le[0].get("usage") if le else None, le[0].get("usage_incomplete_upper_bound") if le else None, stp("early_close"), st_("early_close", 1)])
        rec.check("model allowlist: a different model is refused (403), the gateway stops (model_violation) and later requests are refused",
                  st_("model_violation", 0) == 403 and stp("model_violation") == "model_violation" and st_("model_violation", 1) == 403, [st_("model_violation", 0), stp("model_violation"), st_("model_violation", 1)])
        rec.check("tool policy: a server-side tool is refused (403), the gateway stops (tool_violation) and later requests are refused",
                  st_("tool_violation", 0) == 403 and stp("tool_violation") == "tool_violation" and st_("tool_violation", 1) == 403, [st_("tool_violation", 0), stp("tool_violation"), st_("tool_violation", 1)])
        b = R["budget"]
        rec.check("budget: requests are served until the computed spend reaches the budget, then refused with stop reason budget_exhausted",
                  [x.get("status") for x in b] == [200, 200, 403, 403] and stp("budget") == "budget_exhausted", [[x.get("status") for x in b], stp("budget"), budget])
        u5 = R["upstream_500"]
        rec.check("upstream error (HTTP 500): relayed, not metered, and the gateway keeps serving", st_("upstream_500", 0) == 500 and st_("upstream_500", 1) == 200 and stp("upstream_500") == "", [st_("upstream_500", 0), st_("upstream_500", 1), stp("upstream_500")])
        rec.check("gateway ledger files and stop files live in the gateway's tmpfs, owned 10001:10001",
                  run_sh(client, gw, "10001:10001", "stat -c '%u:%g' /tmp/ledger_normal.jsonl")[1].strip() == "10001:10001")
        # ---- the agent cannot reach the mock upstream or the temporary network at all
        code, out = run_py_in_agent(client, c, "import socket,json\nr={}\nfor h in ('aiep-mock-upstream',):\n try: r[h]=str(socket.gethostbyname(h))\n except Exception as e: r[h]='FAIL '+type(e).__name__\nprint(json.dumps(r))", [])
        rec.check("the agent cannot resolve or reach the mock upstream (it lives only on the gateway side)", code == 0 and json.loads(out)["aiep-mock-upstream"].startswith("FAIL"), out.strip())
        # ---- agent termination -> gateway shutdown
        survivors = quiesce(api, c.id, ctx=ectx)
        rec.check("agent quiesce leaves no process", survivors == [], survivors)
        stopped = mgr.stop()
        rec.fact("gateway stop() result and final reason", [stopped, mgr.final_stop_reason()])
        code, out = run_sh(client, gw, "10001:10001", "ls /proc | grep -E '^[0-9]+$' | wc -l")
        rec.fact("gateway processes after stop (PID 1 plus the exec shell)", out.strip())
        mgr.container.reload()
        rec.check("after gateway stop() no gateway server process is listening", "8080" not in str(json.loads(run_py(client, gw, STATUS_SCRIPT, user="10001:10001")[1]).get("listening")), "")
    finally:
        try:
            removed, err = mgr.teardown()
            rec.check("gateway container removed by its manager (exact container)", removed, err)
        except Exception as e:
            rec.fact("gateway teardown error", f"{type(e).__name__}: {e}")
        rec.fact("temporary containers removed", len(t.cleanup()))
        if mock_net is not None:
            try:
                mock_net.reload()
                mock_net.remove()
                rec.fact("temporary mock network removed", mock_net.id)
            except Exception as e:
                rec.fact("temporary mock network NOT removed", f"{mock_net.id}: {type(e).__name__}: {e}")


def run_py_in_agent(client: Any, c: Any, script: str, argv: list[str]) -> tuple[int | None, str]:
    """Run the agent image's own miniconda python (3.11) with ``script`` and arguments as the agent user."""
    import shlex
    cmd = "/opt/miniconda3/bin/python -I -c " + shlex.quote(script) + " " + " ".join(shlex.quote(a) for a in argv)
    return run_sh(client, c, "1000:1000", cmd)


def left_over(client: Any, run_id: str) -> list[str]:
    return [c.name for c in client.containers.list(all=True, filters={"label": f"aiep.run_id={run_id}"})]


def phase_lifecycle(client: Any, rec: Recorder) -> None:
    """The production lifecycle against real Docker with NO API request: the agent command is ``claude --version`` (or Claude retrying against an
    unreachable gateway, where the agent network has no egress), so nothing can reach any Anthropic endpoint."""
    import dataclasses
    from aiep import agent_runner as AR
    from aiep import artifacts, m2_run
    from aiep.lease import RunLease
    ensure_networks(client)
    p = prepare_agent(rec)
    spec0 = p.aspec
    # ---------- A. the full production path: lease, gateway (production process), agent, supervise, quiesce, extract, artifacts, teardown
    p_a = dataclasses.replace(p, aspec=dataclasses.replace(spec0, claude_args=("--version",)))
    t0 = time.monotonic()
    ex = m2_run.execute(client, p_a, base=rec.dir / "runs_m2", root=ROOT)
    lc = ex.lifecycle
    rec.fact("full lifecycle run", {"seconds": round(time.monotonic() - t0, 1), "status": lc.status, "agent": m2_run.record_of(lc.agent) if lc.agent else None})
    rec.check("A: full lifecycle completed (gateway ready, agent run, both removed) with no API request", lc.status == "completed" and lc.agent.status == "exited" and lc.agent.valid, [lc.status, lc.gateway_error])
    rec.check("A: Claude exited 0 and printed its version", lc.agent.claude_exit_code == 0 and b"2.1.328" in lc.agent.claude_stdout, lc.agent.claude_stdout[:60])
    rec.check("A: agent and gateway containers were both removed (exact containers), cleanup recorded", lc.agent.removed and lc.gateway_removed and not lc.agent.cleanup_error and not lc.gateway_cleanup_error)
    rec.check("A: nothing is left running for this run id", left_over(client, p.run_id) == [], left_over(client, p.run_id))
    rec.check("A: no patch (the agent changed nothing): status empty, no predictions file", lc.agent.patch is not None and lc.agent.patch.status == "empty" and ex.predictions is None, lc.agent.patch and lc.agent.patch.status)
    rec.check("A: gateway ledger was collected and is empty (zero requests reached it) and no stop reason", lc.ledger == b"" and lc.gateway_stop_reason is None, [lc.ledger, lc.gateway_stop_reason])
    rec.check("A: artifacts were persisted and verify (hash manifest)", ex.problems == [] and artifacts.verify_run_artifacts(ex.directory) == [], ex.problems)
    run_json = json.loads((ex.directory / "run.json").read_text())
    rec.check("A: run.json records the pinned CLI hash, seed hash, prompt hashes, limits and lease summary",
              run_json["claude_cli"]["sha256"] == CLAUDE_SHA256 and run_json["seed"]["sha256"] and run_json["prompt"]["prompt_sha256"] and run_json["limits"]["wall_clock_s"] == 600.0
              and run_json["lease"]["errors"] == [] and run_json["termination"] is None, run_json["lease"])
    blob = b"".join(f.read_bytes() for f in ex.directory.iterdir())
    rec.check("A: no key or run token in any artifact", FAKE_KEY.encode() not in blob and p.aspec.run_token.encode() not in blob)
    rec.check("A: the run lease was released", not (ROOT / ".cache" / "leases" / f"{p.run_id}.lease").exists())
    tl = lc.agent.timeline
    rec.fact("agent timeline (s)", tl)
    # ---------- B. wall-clock watchdog: Claude retries forever against an unreachable gateway; the supervisor must stop it
    spec_b = dataclasses.replace(spec0, run_id="sprint-wd", claude_args=tuple(spec0.claude_args), wall_clock_s=25.0)
    t0 = time.monotonic()
    rb = AR.run_agent(client, spec_b, lambda: None, poll_interval=0.5)
    dur = time.monotonic() - t0
    rec.fact("watchdog run", {"status": rb.status, "valid": rb.valid, "claude_s": round(rb.duration_s, 1), "total_s": round(dur, 1), "survivors": rb.survivors_after_quiesce, "removed": rb.removed,
                              "stdout_bytes": len(rb.claude_stdout)})
    rec.check("B: wall-clock watchdog stops a runaway agent (status timeout after ~25s of Claude time)", rb.status == AR.STATUS_TIMEOUT and 24 <= rb.duration_s < 45, [rb.status, round(rb.duration_s, 1)])
    rec.check("B: after the timeout every agent process was quiesced and the container removed", rb.survivors_after_quiesce == [] and rb.removed and left_over(client, "sprint-wd") == [], [rb.survivors_after_quiesce, rb.removed])
    rec.check("B: the timed-out run is still valid and its (empty) patch extraction ran", rb.valid and rb.patch is not None and rb.patch.status == "empty", rb.patch and rb.patch.status)
    rec.check("B: Claude was retrying (api_retry events), not completing", b"api_retry" in rb.claude_stdout and b'"type":"result"' not in rb.claude_stdout, rb.claude_stdout.count(b"api_retry"))
    # ---------- C. output cap: the stream is cut at the cap (never silently truncated), the run ends, and Claude is killed
    spec_c = dataclasses.replace(spec0, run_id="sprint-oc", wall_clock_s=60.0)
    real_cap = AR.MAX_CLAUDE_OUTPUT
    AR.MAX_CLAUDE_OUTPUT = 1500  # below the size of Claude's first events, so the cap trips immediately
    try:
        t0 = time.monotonic()
        rc = AR.run_agent(client, spec_c, lambda: None, poll_interval=0.5)
    finally:
        AR.MAX_CLAUDE_OUTPUT = real_cap
    rec.fact("output-cap run", {"status": rc.status, "truncated": rc.claude_output_truncated, "stdout_bytes": len(rc.claude_stdout), "total_s": round(time.monotonic() - t0, 1), "survivors": rc.survivors_after_quiesce})
    rec.check("C: output over the cap is flagged as truncated and held to the cap", rc.claude_output_truncated and len(rc.claude_stdout) <= 1500, [rc.claude_output_truncated, len(rc.claude_stdout)])
    rec.check("C: the run ended at the cap (Claude time far below the 60s wall clock), Claude was quiesced and the container removed",
              rc.duration_s < 20 and rc.survivors_after_quiesce == [] and rc.removed and left_over(client, "sprint-oc") == [], [round(rc.duration_s, 1), rc.survivors_after_quiesce, rc.removed])
    # ---------- D. lease loss during a real run: the supervised run is stopped through the lifecycle, nothing else is touched
    import aiep.lease as L
    spec_d = dataclasses.replace(spec0, run_id="sprint-ls", wall_clock_s=120.0)
    lease = RunLease(ROOT / ".cache" / "leases", "sprint-ls", interval_s=0.5, stop_after_s=4.0)
    lease.acquire()
    real_replace = L.os.replace
    L.os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device"))  # the lease can no longer be refreshed
    before = sorted(c.id for c in client.containers.list(all=True))
    try:
        t0 = time.monotonic()
        rd = m2_run.leased_agent_runner(lease)(client, spec_d, lambda: None, poll_interval=0.5)
    finally:
        L.os.replace = real_replace
        lease.release()
    after = sorted(c.id for c in client.containers.list(all=True))
    rec.fact("lease-loss run", {"status": rd.status, "stop_reason": rd.stop_reason, "valid": rd.valid, "total_s": round(time.monotonic() - t0, 1), "lease": lease.summary()})
    rec.check("D: a lease that can no longer be refreshed stops the supervised run (stop reason lease_lost, invalid, no patch)", rd.status == AR.STATUS_GATEWAY_STOPPED and rd.stop_reason == "lease_lost" and not rd.valid and rd.patch is None, [rd.status, rd.stop_reason])
    rec.check("D: the stopped agent container was removed and no other container was created or removed", rd.removed and before == after, [len(before), len(after)])
    rec.check("D: the lease heartbeat error was recorded", any("No space" in e for e in lease.errors), lease.errors[:1])


def phase_reaper(client: Any, rec: Recorder) -> None:
    """The reaper against REAL containers, confined to a dedicated namespace and a scoped API.

    Everything this phase creates carries a unique namespace (``reaptest<hex>``) in its name and run label, ``reaper.reap`` is given a client
    whose API can only see, inspect or remove the containers this phase created (``ScopedReaperApi``), and the simulated six-hour clock is
    used only through that client. A live AIEP run (or any other container on the daemon) is therefore never listed, let alone removed.
    """
    from aiep import reaper
    from aiep.lease import RunLease
    t = Tracked(client)
    ns = f"reaptest{uuid.uuid4().hex[:8]}"
    scoped_api = ScopedReaperApi(client.api, lambda: set(t.ids), ns)
    scoped = ScopedReaperClient(scoped_api)
    leases = rec.dir / "reaper_leases"
    rec.fact("namespace", ns)
    now = time.time
    try:
        def mk(name, role, run, user="1000:1000"):
            kw = hardened(name, user, "none")
            kw["labels"] = {"aiep.run_id": run, "aiep.role": role}
            c = t.create(**kw)
            c.start()
            return c
        dead, live, other_run = f"{ns}-dead", f"{ns}-live", f"{ns}-other"
        dead_a, dead_g = mk(f"aiep.agent.{dead}", "agent", dead), mk(f"aiep.gateway.{dead}", "gateway", dead, "10001:10001")
        live_a, live_g = mk(f"aiep.agent.{live}", "agent", live), mk(f"aiep.gateway.{live}", "gateway", live, "10001:10001")
        other = t.create(**{**hardened(f"aiep.nettest.{ns}-unrelated", "1000:1000", "none"), "labels": {"aiep.run_id": other_run, "aiep.role": "evaluator"}})
        plain = t.create(**hardened(f"aiep.{ns}-plain", "1000:1000", "none"))
        for c in (other, plain):
            c.start()
        lease = RunLease(leases, live, interval_s=0)
        lease.acquire()
        future = lambda: now() + 6 * 3600  # noqa: E731  (simulated clock: everything is 6 hours old)
        rep = reaper.reap(scoped, lease_dir=leases, ttl_s=3 * 3600, execute=False, now=future)
        mine = {i.name: i for i in rep.items}
        rec.fact("dry run decisions", {n: [i.action, i.lease, i.reason] for n, i in mine.items()})
        ids = {c.id for c in (dead_a, dead_g, live_a, live_g, other, plain)}
        rec.check("dry run removes nothing (all six containers still exist)", {c.id for c in client.containers.list(all=True)} >= ids)
        rec.check("the reaper saw only this phase's namespaced containers", all(ns in n for n in mine) and len(mine) == 5, sorted(mine))
        # the "live" lease is fresh at REAL time but the simulated clock is 6 h ahead, so keep it alive by refreshing at that clock
        import json as J
        (leases / f"{live}.lease").write_text(J.dumps({"schema": 1, "run_id": live, "pid": 1, "started_at": future(), "heartbeat_at": future(), "beat": 1}))
        rep = reaper.reap(scoped, lease_dir=leases, ttl_s=3 * 3600, execute=True, now=future)
        mine = {i.name: i for i in rep.items}
        rec.fact("execute decisions", {n: [i.action, i.lease, i.removed, i.reason] for n, i in mine.items()})
        alive = {c.id for c in client.containers.list(all=True)}
        rec.check("execute removed exactly the abandoned run's agent and gateway", dead_a.id not in alive and dead_g.id not in alive and mine[f"aiep.agent.{dead}"].removed and mine[f"aiep.gateway.{dead}"].removed)
        rec.check("the live run (fresh lease) was NOT removed although its containers are 'old'", live_a.id in alive and live_g.id in alive, [mine[f"aiep.agent.{live}"].reason])
        rec.check("an AIEP-labelled evaluator-role container and a label-less container were NOT removed", other.id in alive and plain.id in alive)
        rec.check("the scoped API was never asked to touch a container this phase did not create", scoped_api.denied == [], scoped_api.denied)
        unrelated = [c.name for c in client.containers.list(all=True) if c.name in ("trino", "hive-metastore", "debezium", "postgres", "kafka")]
        rec.check("the five unrelated pre-existing containers were never touched", sorted(unrelated) == ["debezium", "hive-metastore", "kafka", "postgres", "trino"], unrelated)
        lease.release()
    finally:
        rec.fact("temporary containers removed", len(t.cleanup()))


PHASES: dict[str, Callable[[Any, Recorder], None]] = {"networks": phase_networks, "gateway": phase_gateway, "agent": phase_agent, "integration": phase_integration,
                                                      "lifecycle": phase_lifecycle, "reaper": phase_reaper}


# These phases were written for the previous TWO-network topology (gateway on the agent network plus an egress network). That topology is gone: the
# gateway is now on internal networks only and reaches its upstream through the restricted proxy. They are superseded by scripts/m2_egress_validation.py
# and would fail (or, worse, mislead) against the current code, so they refuse to run. This module's helpers are still imported by the new script.
SUPERSEDED_PHASES = frozenset({"networks", "gateway", "agent", "integration", "lifecycle"})


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--evidence-dir", type=Path, required=True)
    p.add_argument("--phase", required=True, choices=sorted(PHASES))
    args = p.parse_args(argv)
    if args.phase in SUPERSEDED_PHASES:
        print(f"phase {args.phase!r} targets the superseded two-network topology; use scripts/m2_egress_validation.py", file=sys.stderr)
        return 2
    import docker

    client = docker.from_env(timeout=300)
    rec = Recorder(args.phase, args.evidence_dir)
    try:
        PHASES[args.phase](client, rec)
    finally:
        rec.save()
        try:
            client.close()
        except Exception as e:
            print(f"WARNING: closing the Docker client failed: {type(e).__name__}: {e}", file=sys.stderr)
    return 1 if any(r["status"] == "FAIL" for r in rec.rows) else 0


if __name__ == "__main__":
    sys.exit(main())

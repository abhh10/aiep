"""The restricted egress proxy: allowlist, address validation, pinning, TLS tunnelling, bounds, and the gateway going THROUGH it.

Loopback only: a fake TLS upstream and in-process proxy/gateway servers. No Docker, no real network, no key. The connection from the proxy to
its destination is observed (``_Handler._connect`` is wrapped), never made to a public address.
"""
import json
import shutil
import socket
import ssl
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest

from aiep import egress_proxy as P
from aiep import gateway as G
from aiep.gateway import Gateway, GatewayConfig, UpstreamConfig
from aiep.ledger import Ledger
from aiep.pricing import load_pricing
from fake_upstream import FakeUpstream, make_cert

REAL = Path(__file__).resolve().parents[1]
HOST = "api.anthropic.com"
KEY = "sk-ant-REAL-UPSTREAM-KEY-DO-NOT-LEAK"
TOKEN = "per-run-token-abc123"
PROMPT = "SECRET-PROMPT-TEXT-xyz"
MODEL = "claude-sonnet-5-5"

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI needed for test certificates")


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    d = tmp_path_factory.mktemp("proxycerts")
    return {"good": make_cert(d, HOST, "good"), "other": make_cert(d, HOST, "other")}


# ------------------------------------------------------------------ parsing
def head(line="CONNECT api.anthropic.com:443 HTTP/1.1", *headers):
    return ("\r\n".join([line, *headers]) + "\r\n\r\n").encode()


def refusal(data):
    with pytest.raises(P.Refusal) as e:
        P.parse_connect(data)
    return e.value.status, e.value.reason


def test_a_well_formed_connect_is_parsed_and_the_host_is_lowercased():
    assert P.parse_connect(head()) == (HOST, 443)
    assert P.parse_connect(head("CONNECT API.Anthropic.COM:443 HTTP/1.0", "Host: x", "Proxy-Connection: keep-alive")) == (HOST, 443)


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "TRACE", "PATCH", "PRI"])
def test_every_method_but_connect_is_refused_with_405(method):
    assert refusal(head(f"{method} /v1/messages HTTP/1.1")) == (405, "method_not_allowed")
    assert refusal(head(f"{method} api.anthropic.com:443 HTTP/1.1")) == (405, "method_not_allowed")


def test_a_lowercase_method_is_not_connect():
    assert refusal(head("connect api.anthropic.com:443 HTTP/1.1")) == (405, "method_not_allowed")


@pytest.mark.parametrize("target", [
    "http://api.anthropic.com:443", "api.anthropic.com", "api.anthropic.com:", ":443", "[::1]:443", "[2001:db8::1]:443", "user@api.anthropic.com:443",
    "api.anthropic.com:0", "api.anthropic.com:65536", "api.anthropic.com:00443", "api.anthropic.com:+443", "api.anthropic.com:443x",
    "api.anthropic.com:-1", "api.anthropic.com.:443", ".api.anthropic.com:443", "api anthropic.com:443", "api.anthropic.com/:443",
    "api.anthropic.com:443/path", "*:443", "api_anthropic.com:443", "a" * 300 + ":443", "api.anthropic.com:44３", "ａpi.anthropic.com:443",
    "api.anthropic.com:443:443", "api.anthropic.com:%34%34%33", "api.anthropic.com%00:443", "-api.anthropic.com:443",
])
def test_malformed_or_exotic_targets_are_refused_at_parse_time(target):
    data = f"CONNECT {target} HTTP/1.1\r\n\r\n".encode("utf-8")
    status, reason = refusal(data)
    assert (status, reason) in {(400, "invalid_target"), (400, "malformed_request")}


@pytest.mark.parametrize("data,expect", [
    (b"", (400, "malformed_request")), (b"\r\n\r\n", (400, "malformed_request")),
    (b"CONNECT api.anthropic.com:443 HTTP/1.1\n\n", (400, "malformed_request")),               # bare LF framing
    (b"CONNECT api.anthropic.com:443 HTTP/1.1\r\nX: a\nGET / HTTP/1.1\r\n\r\n", (400, "malformed_request")),  # smuggled line
    (b"CONNECT api.anthropic.com:443 HTTP/2\r\n\r\n", (400, "malformed_request")),
    (b"CONNECT api.anthropic.com:443\r\n\r\n", (400, "malformed_request")),
    (b"CONNECT  api.anthropic.com:443 HTTP/1.1\r\n\r\n", (400, "malformed_request")),
    (head("CONNECT api.anthropic.com:443 HTTP/1.1", "Bad Header: x"), (400, "malformed_header")),
    (head("CONNECT api.anthropic.com:443 HTTP/1.1", " folded: x"), (400, "malformed_header")),
    (head("CONNECT api.anthropic.com:443 HTTP/1.1", "Content-Length: 5"), (400, "request_body_not_allowed")),
    (head("CONNECT api.anthropic.com:443 HTTP/1.1", "Transfer-Encoding: chunked"), (400, "request_body_not_allowed")),
    (head("CONNECT api.anthropic.com:443 HTTP/1.1", *[f"X-{i}: 1" for i in range(40)]), (431, "too_many_headers")),
    ("CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n".encode("utf-16"), (400, "malformed_request")),
    (b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\xff: x\r\n\r\n", (400, "malformed_request")),
])
def test_malformed_heads_are_refused(data, expect):
    assert refusal(data) == expect


def test_a_zero_content_length_is_harmless_but_any_body_framing_is_not():
    assert P.parse_connect(head("CONNECT api.anthropic.com:443 HTTP/1.1", "Content-Length: 0")) == (HOST, 443)


# ------------------------------------------------------------------ the allowlist
CFG = P.ProxyConfig()


def authz(host, port, cfg=CFG):
    try:
        P.authorize(host, port, cfg)
    except P.Refusal as r:
        return r.status, r.reason
    return None


def test_only_the_exact_configured_destination_is_allowed():
    assert authz(HOST, 443) is None


@pytest.mark.parametrize("port", [80, 22, 25, 53, 444, 4430, 8443, 8080, 3128, 3306, 5000, 7000, 8828, 22617, 24692, 1, 65535])
def test_any_other_port_is_refused_even_for_the_right_host(port):
    assert authz(HOST, port) == (403, "port_not_allowed")


@pytest.mark.parametrize("host", ["anthropic.com", "www.anthropic.com", "api.anthropic.com.evil.com", "evilapi.anthropic.com", "xapi.anthropic.com",
                                  "localhost", "host.docker.internal", "gateway.docker.internal", "metadata.google.internal", "example.com",
                                  "api-anthropic.com", "api.anthropic.co", "aiep.gateway.r1", "aiep.proxy.r1", "mock-upstream"])
def test_any_other_name_is_refused(host):
    assert authz(host, 443) == (403, "destination_not_allowed")


@pytest.mark.parametrize("host", [
    "127.0.0.1", "127.1", "0.0.0.0", "10.0.0.1", "172.16.0.1", "192.168.1.1", "192.168.65.254", "192.168.65.2", "169.254.169.254", "100.64.0.1",
    "8.8.8.8", "1.1.1.1", "93.184.216.34", "2130706433", "0x7f.1", "0x7f000001", "0177.0.0.1", "017700000001", "4294967295", "1.2.3", "01.02.03.04",
    "::1", "fe80::1", "2001:4860:4860::8888", "::ffff:192.168.65.254",
])
def test_ip_literals_and_numeric_spellings_are_refused_before_the_port_or_name_is_even_considered(host):
    assert P.is_ip_literal(host)
    for port in (443, 80, 3306):
        assert authz(host, port) == (403, "ip_literal_refused")


def test_the_discovered_host_gateway_and_host_ports_are_all_refused_as_destinations():
    for port in (3306, 5000, 7000, 8828, 22617, 24692):
        assert authz("192.168.65.254", port) == (403, "ip_literal_refused")
        assert authz("host.docker.internal", port) == (403, "port_not_allowed")


def test_a_custom_config_is_exact_too():
    cfg = P.ProxyConfig(host="mock.test", port=8443)
    assert authz("mock.test", 8443, cfg) is None
    assert authz(HOST, 443, cfg) == (403, "port_not_allowed") and authz(HOST, 8443, cfg) == (403, "destination_not_allowed")


def test_production_config_comes_from_the_experiment_file_and_port_443_only():
    cfg = P.production_config(REAL)
    assert (cfg.host, cfg.port) == (HOST, 443) and cfg.allow_private_addresses is False and cfg.resolver is None


# ------------------------------------------------------------------ address validation and pinning
def pick(addrs, **kw):
    cfg = P.ProxyConfig(resolver=lambda h, p: addrs, **kw)
    try:
        return P.pick_pinned_address(cfg)
    except P.Refusal as r:
        return r.status, r.reason


PUBLIC = ["160.79.104.10", "34.120.1.1", "93.184.216.34", "2607:6bc0::10"]
NON_PUBLIC = ["127.0.0.1", "127.255.255.254", "10.0.0.1", "172.16.5.5", "172.31.255.255", "192.168.0.1", "192.168.65.254", "192.168.65.2",
              "169.254.169.254", "169.254.1.1", "100.64.0.1", "100.127.255.254", "0.0.0.0", "224.0.0.1", "239.255.255.250", "240.0.0.1",
              "255.255.255.255", "192.0.0.8", "198.18.0.1", "192.0.2.1", "198.51.100.1", "203.0.113.1", "::1", "::", "fe80::1", "fc00::1", "fd12::1",
              "ff02::1", "::ffff:127.0.0.1", "::ffff:192.168.65.254", "::ffff:10.0.0.1", "64:ff9b::7f00:1", "2002:c0a8:4101::1", "2001::1", "2001:db8::1"]


@pytest.mark.parametrize("addr", PUBLIC)
def test_public_addresses_are_accepted(addr):
    assert pick([addr]) == addr


@pytest.mark.parametrize("addr", NON_PUBLIC)
def test_private_loopback_link_local_multicast_reserved_and_host_gateway_answers_are_refused(addr):
    assert pick([addr]) == (403, "resolves_to_non_public_address")


@pytest.mark.parametrize("bad", NON_PUBLIC[:12])
@pytest.mark.parametrize("order", ["public_first", "private_first"])
def test_a_mixed_public_and_private_answer_is_refused_whole(bad, order):
    answers = ["160.79.104.10", bad] if order == "public_first" else [bad, "160.79.104.10"]
    assert pick(answers) == (403, "resolves_to_non_public_address")  # one bad address spoils the answer: no 'pick the public one' escape


def test_ipv4_is_preferred_when_several_public_addresses_are_returned():
    assert pick(["2607:6bc0::10", "160.79.104.10", "34.120.1.1"]) == "160.79.104.10"  # an IPv4 address, never the IPv6 one listed first


@pytest.mark.parametrize("answers", [[], ["not-an-ip"], ["160.79.104.10", "evil.example"], [None], [""], ["160.79.104.10/32"]])
def test_empty_or_non_address_answers_are_refused(answers):
    status, reason = pick(answers)
    assert status == 502 and reason in ("dns_failure", "resolver_returned_a_non_address")


def test_a_resolver_failure_is_a_502_not_a_fallback():
    def boom(h, p):
        raise socket.gaierror("no such host")
    assert P.pick_pinned_address.__name__ and pick_with(boom) == (502, "dns_failure")


def pick_with(resolver):
    try:
        return P.pick_pinned_address(P.ProxyConfig(resolver=resolver))
    except P.Refusal as r:
        return r.status, r.reason


def test_the_private_address_seam_exists_only_as_a_python_argument():
    assert pick(["127.0.0.1"], allow_private_addresses=True) == "127.0.0.1"
    assert pick(["10.1.2.3"], allow_private_addresses=True) == "10.1.2.3"
    assert pick(["not-an-ip"], allow_private_addresses=True)[0] == 502  # still must be a literal address
    import inspect
    src = inspect.getsource(P.main)
    assert "allow_private" not in src.replace("config=config", "") and "os.environ" in src


def test_the_proxys_address_rules_agree_with_the_gateways_on_a_wide_corpus():
    corpus = PUBLIC + NON_PUBLIC + ["8.8.8.8", "1.0.0.1", "172.15.255.255", "172.32.0.1", "192.167.255.255", "100.63.255.255", "100.128.0.1",
                                   "198.17.255.255", "198.20.0.1", "223.255.255.254", "2a00:1450::1", "2400::1", "3fff::1", "2000::1",
                                   "1fff::1", "4000::1", "::ffff:8.8.8.8", "64:ff9b::808:808"]
    assert all(P.address_is_public(a) == G.address_is_public(a) for a in corpus)
    import random
    rng = random.Random(7)
    for _ in range(3000):
        a = ".".join(str(rng.randrange(256)) for _ in range(4))
        assert P.address_is_public(a) == G.address_is_public(a), a


# ------------------------------------------------------------------ the running proxy
_REAL_CONNECT = P._Handler.__dict__["_connect"]


class Rig:
    """A proxy on loopback whose upstream connection goes to ``target`` (a local fake) after the REAL validation ran on ``addrs``."""

    def __init__(self, tmp_path, *, addrs=("127.0.0.1",), target=None, host=HOST, port=443, allow_private=True, **bounds):
        self.resolved = []
        self.connects = []
        addrs = list(addrs)

        def resolver(h, p):
            self.resolved.append((h, p))
            return addrs

        self.cfg = P.ProxyConfig(host=host, port=port, allow_private_addresses=allow_private, resolver=resolver, **bounds)
        self.log = tmp_path / "proxy.jsonl"
        self.server = P.ProxyServer(("127.0.0.1", 0), self.cfg, self.log)
        self.port = self.server.server_address[1]
        rig = self

        def connect(ip, port_, cfg):
            rig.connects.append((ip, port_))
            if target is None:  # nothing to connect to: the attempt is recorded and fails, and no packet ever leaves the test
                raise OSError("no test target")
            s = socket.create_connection(target, 5)
            if not P._same_ip(s.getpeername()[0], "127.0.0.1"):
                raise AssertionError("test wiring")
            return _Pretend(s, ip)

        P._Handler._connect = staticmethod(connect)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        P._Handler._connect = _REAL_CONNECT  # always the genuine staticmethod, whatever order several rigs are closed in
        self.server.shutdown()
        self.server.server_close()

    def raw(self, data, *, read=True, timeout=5.0):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
        s.sendall(data)
        if not read:
            return s
        buf = b""
        try:
            while b"\r\n\r\n" not in buf:
                c = s.recv(4096)
                if not c:
                    break
                buf += c
            body = b""
            if buf.startswith(b"HTTP/1.1 4") or buf.startswith(b"HTTP/1.1 5"):
                while True:
                    c = s.recv(4096)
                    if not c:
                        break
                    body += c
        except socket.timeout:
            body = b""
        finally:
            s.close()
        h, _, rest = buf.partition(b"\r\n\r\n")
        return int(h.split(b" ")[1]) if h else 0, (rest + body).decode("utf-8", "replace").strip()

    def wait_events(self, n=1, timeout=3.0):
        t0 = time.monotonic()
        while len(self.server.events) < n and time.monotonic() - t0 < timeout:
            time.sleep(0.01)
        return list(self.server.events)


class _Pretend:
    """A connected socket whose peer address is reported as the validated one (the real connect went to a local fake)."""

    def __init__(self, s, ip):
        self._s, self._ip = s, ip

    def getpeername(self):
        return (self._ip, 443)

    def __getattr__(self, n):
        return getattr(self._s, n)


@pytest.fixture
def rig(tmp_path):
    made = []

    def make(**kw):
        (tmp_path / f"p{len(made)}").mkdir()
        r = Rig(tmp_path / f"p{len(made)}", **kw)
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()


def test_refusals_are_answered_with_a_status_and_a_fixed_reason_and_nothing_is_dialled(rig):
    r = rig()
    cases = [
        (b"GET http://api.anthropic.com/ HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n", 405, "method_not_allowed"),
        (head("CONNECT example.com:443 HTTP/1.1"), 403, "destination_not_allowed"),
        (head("CONNECT 192.168.65.254:3306 HTTP/1.1"), 403, "ip_literal_refused"),
        (head("CONNECT 127.0.0.1:443 HTTP/1.1"), 403, "ip_literal_refused"),
        (head("CONNECT api.anthropic.com:3306 HTTP/1.1"), 403, "port_not_allowed"),
        (head("CONNECT [::1]:443 HTTP/1.1"), 400, "invalid_target"),
        (head("CONNECT api.anthropic.com:443 HTTP/1.1", "Content-Length: 4") + b"abcd", 400, "request_body_not_allowed"),
        (head() + b"\x16\x03\x01 early client hello", 400, "bytes_after_request_not_allowed"),  # nothing may ride behind the CONNECT
    ]
    for data, status, reason in cases:
        assert r.raw(data) == (status, reason), data
    assert r.connects == [] and r.resolved == []  # not a single DNS lookup or connection for any refused request
    ev = r.wait_events(len(cases))
    assert [e["outcome"] for e in ev] == ["refused"] * len(cases) and all("pinned_ip" not in e for e in ev)


def test_a_refusal_for_a_private_resolution_never_connects(rig):
    for bad in ("192.168.65.254", "127.0.0.1", "169.254.169.254", "10.0.0.5"):
        r = rig(addrs=(bad,), allow_private=False)
        assert r.raw(head()) == (403, "resolves_to_non_public_address")
        assert r.connects == [] and r.resolved == [(HOST, 443)]


def test_mixed_public_and_private_dns_is_refused_at_the_server_too(rig):
    r = rig(addrs=("160.79.104.10", "192.168.65.254"), allow_private=False)
    assert r.raw(head()) == (403, "resolves_to_non_public_address") and r.connects == []


def test_dns_rebinding_cannot_change_the_destination_the_name_is_resolved_once_and_the_ip_is_pinned(rig):
    answers = [["160.79.104.10"], ["192.168.65.254"], ["127.0.0.1"]]
    calls = []

    def resolver(h, p):
        calls.append(h)
        return answers[min(len(calls) - 1, 2)]

    r = rig(allow_private=False)
    r.cfg = P.ProxyConfig(host=HOST, port=443, resolver=resolver)
    r.server.cfg = r.cfg
    r.server.resolver = P.BoundedResolver(resolver, 5.0, 2)
    r.raw(head(), read=False).close()
    r.wait_events(1)
    assert calls == [HOST] and r.connects == [("160.79.104.10", 443)]  # one lookup; the connection went to exactly the validated address
    assert r.server.events[0]["pinned_ip"] == "160.79.104.10"


def test_every_connection_dials_the_pinned_address_and_the_configured_port_never_the_clients(rig):
    r = rig(addrs=("34.120.1.1", "160.79.104.10"), allow_private=False)
    r.raw(head(), read=False).close()
    r.wait_events(1)
    assert r.connects == [("160.79.104.10", 443)]  # sorted, IPv4 first; the CONNECT line contributed only the (already authorised) port


def test_a_peer_that_is_not_the_pinned_address_is_refused(monkeypatch):
    class S:
        def getpeername(self):
            return ("203.0.113.9", 443)

        def close(self):
            self.closed = True

    s = S()
    monkeypatch.setattr(P.socket, "create_connection", lambda *a, **k: s)
    with pytest.raises(P.Refusal) as e:
        P._Handler._connect("160.79.104.10", 443, P.ProxyConfig())
    assert e.value.reason == "peer_address_mismatch" and s.closed is True


def test_connect_failure_is_a_502_with_a_fixed_reason(rig, tmp_path):
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()
    r = rig(target=("127.0.0.1", port))
    assert r.raw(head()) == (502, "connect_failed")


def test_a_slow_or_oversized_request_head_is_cut_off(rig):
    r = rig(header_timeout_s=0.4)
    s = r.raw(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n", read=False)  # never finishes the head
    assert s.recv(4096).startswith(b"HTTP/1.1 408")
    s.close()
    assert r.raw(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\nX: " + b"a" * 5000 + b"\r\n\r\n")[0] == 431
    assert r.connects == []


def test_the_connection_cap_refuses_extra_clients_with_503(rig):
    with FakeUpstream() as up:
        r = rig(target=("127.0.0.1", up.port), max_connections=1)
        a = r.raw(head(), read=False)
        assert a.recv(100).startswith(b"HTTP/1.1 200")  # the one slot is held by an open tunnel
        assert r.raw(head()) == (503, "busy")
        a.close()


def test_the_tunnel_ends_at_the_byte_cap_and_the_idle_timeout(rig):
    class Echo(threading.Thread):
        def __init__(self):
            super().__init__(daemon=True)
            self.srv = socket.socket()
            self.srv.bind(("127.0.0.1", 0))
            self.srv.listen(5)
            self.port = self.srv.getsockname()[1]

        def run(self):
            while True:
                try:
                    c, _ = self.srv.accept()
                except OSError:
                    return
                threading.Thread(target=self._echo, args=(c,), daemon=True).start()

        @staticmethod
        def _echo(c):
            try:
                while True:
                    d = c.recv(65536)
                    if not d:
                        break
                    c.sendall(d)
            except OSError:
                pass
            c.close()

    e = Echo()
    e.start()
    r = rig(target=("127.0.0.1", e.port), max_tunnel_bytes=20_000, idle_timeout_s=1.0)
    s = r.raw(head(), read=False)
    assert s.recv(100).startswith(b"HTTP/1.1 200")
    s.sendall(b"x" * 30_000)
    s.settimeout(5)
    got = 0
    try:
        while True:
            d = s.recv(65536)
            if not d:
                break
            got += len(d)
    except (socket.timeout, ConnectionResetError):
        pass
    s.close()
    ev = r.wait_events(1)
    assert ev[0]["reason"] == "max_bytes" and ev[0]["bytes_up"] + ev[0]["bytes_down"] >= 20_000
    s2 = r.raw(head(), read=False)
    assert s2.recv(100).startswith(b"HTTP/1.1 200")
    ev = r.wait_events(2, timeout=6)
    assert ev[1]["reason"] == "idle_timeout"
    s2.close()
    e.srv.close()


def test_decisions_are_logged_without_payloads_and_the_log_is_bounded(rig):
    r = rig()
    r.raw(head("CONNECT evil.example:443 HTTP/1.1", "Authorization: Bearer SECRET-IN-A-HEADER"))
    r.wait_events(1)
    line = r.log.read_text()
    assert "evil.example" in line and "SECRET-IN-A-HEADER" not in line and "Bearer" not in line
    entry = json.loads(line)
    assert entry["outcome"] == "refused" and entry["reason"] == "destination_not_allowed" and set(entry) <= {
        "ts", "host", "port", "outcome", "status", "reason", "pinned_ip"}


# ------------------------------------------------------------------ TLS is tunnelled end to end, never intercepted
def https_get_through(proxy_port, ca_file, *, host=HOST, port=443, path="/hello"):
    s = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
    s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(1024)
    assert buf.startswith(b"HTTP/1.1 200")
    ctx = ssl.create_default_context(cafile=str(ca_file))
    t = ctx.wrap_socket(s, server_hostname=HOST)
    der = t.getpeercert(binary_form=True)
    t.sendall(f"GET {path} HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\n\r\n".encode())
    out = b""
    while True:
        c = t.recv(65536)
        if not c:
            break
        out += c
    t.close()
    return der, out


def test_tls_is_tunnelled_end_to_end_the_client_sees_the_upstreams_own_certificate(rig, certs):
    cert, key = certs["good"]
    with FakeUpstream(cert, key) as up:
        r = rig(target=("127.0.0.1", up.port))
        der, out = https_get_through(r.port, cert)
        assert out.startswith(b"HTTP/1.1 200") and len(up.requests) == 1
        assert der == ssl.PEM_cert_to_DER_cert(cert.read_text())  # the upstream's certificate, byte for byte: no interception, no re-signing
        ev = r.wait_events(1)[0]
        assert ev["outcome"] == "tunnel" and ev["bytes_up"] > 0 and ev["bytes_down"] > 0 and ev["reason"] == "closed"


def test_a_client_that_does_not_trust_the_upstream_certificate_fails_the_handshake_through_the_proxy(rig, certs):
    good, key = certs["good"]
    other, _ = certs["other"]
    with FakeUpstream(good, key) as up:
        r = rig(target=("127.0.0.1", up.port))
        with pytest.raises(ssl.SSLCertVerificationError):
            https_get_through(r.port, other)  # the proxy cannot vouch for the upstream: verification stays with the client
        assert len(up.requests) == 0


def test_the_proxy_never_sees_plaintext_of_a_tls_session(rig, certs, monkeypatch):
    cert, key = certs["good"]
    seen = bytearray()
    real = P._tunnel

    class Tap:
        def __init__(self, s):
            self._s = s

        def recv(self, n):
            d = self._s.recv(n)
            seen.extend(d)
            return d

        def __getattr__(self, name):
            return getattr(self._s, name)

        def fileno(self):
            return self._s.fileno()

    monkeypatch.setattr(P, "_tunnel", lambda a, b, cfg: real(Tap(a), Tap(b), cfg))
    with FakeUpstream(cert, key) as up:
        r = rig(target=("127.0.0.1", up.port))
        https_get_through(r.port, cert, path="/v1/messages?secret=SECRET-PROMPT-TEXT-xyz")
        r.wait_events(1)
    assert len(seen) > 500 and b"SECRET-PROMPT-TEXT-xyz" not in bytes(seen) and b"GET /" not in bytes(seen) and b"HTTP/1.1 200" not in bytes(seen)


# ------------------------------------------------------------------ the gateway goes through the proxy, and only through it
def messages_body():
    return json.dumps({"model": MODEL, "max_tokens": 1024, "messages": [{"role": "user", "content": PROMPT}]}).encode()


class GwRig:
    def __init__(self, tmp_path, up_cfg: UpstreamConfig):
        self.ledger_path = tmp_path / "ledger.jsonl"
        self.cfg = GatewayConfig(run_token=TOKEN, upstream_key=KEY, model=MODEL, pricing=load_pricing(REAL / "config" / "model_pricing.json", MODEL),
                                 ledger=Ledger(self.ledger_path), allowed_hosts=frozenset(), budget_usd=Decimal("100"), upstream=up_cfg)
        self.gw = Gateway(self.cfg)
        self.server = self.gw.make_server("127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.cfg.allowed_hosts = frozenset({f"gateway.test:{self.port}"})
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def post(self):
        body = messages_body()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=15)
        s.sendall((f"POST /v1/messages HTTP/1.1\r\nHost: gateway.test:{self.port}\r\nAuthorization: Bearer {TOKEN}\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body)
        buf = b""
        while True:
            c = s.recv(65536)
            if not c:
                break
            buf += c
        s.close()
        h, _, rest = buf.partition(b"\r\n\r\n")
        return int(h.split(b" ")[1]), rest

    def ledger(self):
        return [json.loads(l) for l in self.ledger_path.read_text().splitlines()]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stack(tmp_path, certs, rig):
    """upstream(TLS, name api.anthropic.com) <- proxy (real validation on a stubbed resolver, loopback allowed) <- gateway."""
    cert, key = certs["good"]
    up = FakeUpstream(cert, key).__enter__()
    made = []

    def build(*, proxy_host="127.0.0.1", **proxy_kw):
        px = rig(target=("127.0.0.1", up.port), **proxy_kw)
        (tmp_path / f"g{len(made)}").mkdir()
        gw = GwRig(tmp_path / f"g{len(made)}", UpstreamConfig(host=HOST, port=443, ca_file=str(cert), proxy_host=proxy_host, proxy_port=px.port,
                                                               read_timeout=10, connect_timeout=3))
        made.append(gw)
        return up, px, gw

    yield build
    for g in made:
        g.close()
    up.__exit__()


def test_gateway_to_proxy_to_upstream_succeeds_with_exact_metering_and_the_key_only_inside_tls(stack, monkeypatch):
    up, px, gw = stack()
    seen = bytearray()
    real = P._tunnel

    class Tap:
        def __init__(self, s):
            self._s = s

        def recv(self, n):
            d = self._s.recv(n)
            seen.extend(d)
            return d

        def __getattr__(self, name):
            return getattr(self._s, name)

        def fileno(self):
            return self._s.fileno()

    monkeypatch.setattr(P, "_tunnel", lambda a, b, cfg: real(Tap(a), Tap(b), cfg))
    status, body = gw.post()
    assert status == 200 and json.loads(body)["usage"]["input_tokens"] == 1000
    assert len(up.requests) == 1 and up.requests[0]["headers"]["x-api-key"] == KEY  # upstream saw the key; the TLS session ran gateway<->upstream
    entry = gw.ledger()[-1]
    assert entry["outcome"] == "ok" and entry["cost_usd_computed"] == "0.0061" and gw.cfg.ledger.spent == Decimal("0.0061")
    px.wait_events(1)
    assert px.connects == [("127.0.0.1", 443)] and px.server.events[0]["outcome"] == "tunnel"
    blob = bytes(seen)
    assert len(blob) > 500 and KEY.encode() not in blob and PROMPT.encode() not in blob and TOKEN.encode() not in blob  # the proxy only ever saw ciphertext
    assert KEY not in json.dumps(px.server.events) and KEY not in px.log.read_text()


def test_with_a_proxy_configured_the_gateway_never_resolves_or_dials_the_upstream_itself(stack, monkeypatch):
    up, px, gw = stack()
    import types
    dialled = []
    real = socket.create_connection
    shim = types.SimpleNamespace(**{k: getattr(socket, k) for k in dir(socket) if not k.startswith("__")})
    shim.create_connection = lambda addr, *a, **k: (dialled.append(addr), real(addr, *a, **k))[1]
    monkeypatch.setattr(G, "socket", shim)  # only the gateway's own use of the socket module is observed
    monkeypatch.setattr(Gateway, "_pick_address", lambda self: (_ for _ in ()).throw(AssertionError("direct resolution attempted")))
    monkeypatch.setattr(G, "_PinnedTLSConnection", lambda *a, **k: (_ for _ in ()).throw(AssertionError("direct connection attempted")))
    assert gw.post()[0] == 200
    assert dialled == [("127.0.0.1", px.port)]  # the ONLY destination the gateway ever connected to is the proxy


def test_if_the_proxy_is_unreachable_the_request_fails_and_the_upstream_is_never_contacted(stack, monkeypatch):
    up, px, gw = stack()
    px.close()
    monkeypatch.setattr(Gateway, "_pick_address", lambda self: (_ for _ in ()).throw(AssertionError("fallback to direct upstream")))
    status, body = gw.post()
    assert status == 502 and b"upstream unavailable (connection_failed)" in body and len(up.requests) == 0
    assert gw.ledger()[-1]["status"] == 502


def test_if_the_proxy_refuses_there_is_no_fallback_and_the_error_is_reported(stack, monkeypatch):
    up, px, gw = stack(host="mock.test", port=8443)  # the proxy is allowed another destination: it answers 403 to api.anthropic.com:443
    monkeypatch.setattr(Gateway, "_pick_address", lambda self: (_ for _ in ()).throw(AssertionError("fallback to direct upstream")))
    status, body = gw.post()
    assert status == 502 and b"proxy_refused" in body and len(up.requests) == 0 and px.connects == []


@pytest.mark.parametrize("reply,code", [
    (b"HTTP/1.1 403 Refused\r\n\r\n", "proxy_refused"), (b"HTTP/1.1 502 Unavailable\r\n\r\n", "proxy_refused"), (b"HTTP/1.1 407 Auth\r\n\r\n", "proxy_refused"),
    (b"HTTP/1.1 2000 OK\r\n\r\n", "proxy_refused"), (b"HTTP/2 200\r\n\r\n", "proxy_refused"),
    (b"garbage\r\n\r\n", "proxy_refused"), (b"HTTP/1.1 200 OK\r\n\r\nearly data", "proxy_unexpected_data"),
    (b"", "proxy_closed_without_reply"), (b"HTTP/1.1 200 OK\r\n" + b"X: a\r\n" * 2000, "proxy_reply_too_large"),
])
def test_a_misbehaving_proxy_never_yields_a_connection(tmp_path, certs, reply, code):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            c.recv(4096)
            if reply:
                c.sendall(reply)
            time.sleep(0.05)
            c.close()
    threading.Thread(target=serve, daemon=True).start()
    with pytest.raises(G.UpstreamError) as e:
        G._proxy_tunnel("127.0.0.1", srv.getsockname()[1], HOST, 443, 2.0)
    assert e.value.code == code
    srv.close()


def test_a_proxy_that_cannot_be_resolved_fails_closed(tmp_path, certs, monkeypatch):
    def nxdomain(*a, **k):
        raise socket.gaierror("no such host")
    import types
    shim = types.SimpleNamespace(**{k: getattr(socket, k) for k in dir(socket) if not k.startswith("__")})
    shim.getaddrinfo = nxdomain
    monkeypatch.setattr(G, "socket", shim)
    gw = GwRig(tmp_path, UpstreamConfig(host=HOST, proxy_host="no-such-proxy.invalid", proxy_port=3128, ca_file=str(certs["good"][0])))
    try:
        status, body = gw.post()
        assert status == 502 and b"proxy_dns_failure" in body
    finally:
        gw.close()


def test_the_gateway_still_verifies_the_upstream_certificate_through_the_tunnel(tmp_path, certs, rig):
    good, key = certs["good"]
    other, _ = certs["other"]
    with FakeUpstream(good, key) as up:
        px = rig(target=("127.0.0.1", up.port))
        gw = GwRig(tmp_path, UpstreamConfig(host=HOST, port=443, ca_file=str(other), proxy_host="127.0.0.1", proxy_port=px.port, read_timeout=10))
        try:
            status, body = gw.post()
            assert status == 502 and b"tls_verification_failed" in body and len(up.requests) == 0
        finally:
            gw.close()


# ------------------------------------------------------------------ startup: no secrets, valid listen
def test_the_proxy_refuses_to_start_if_it_is_given_anything_secret_like():
    assert P.refuse_secrets({"PATH": "/bin", "HOME": "/tmp", "TZ": "UTC", "PYTHONPATH": "/opt/aiep-proxy/src"}) == []
    assert P.refuse_secrets({"AIEP_UPSTREAM_API_KEY": "x", "ANTHROPIC_AUTH_TOKEN": "y", "AWS_SECRET_ACCESS_KEY": "z", "DB_PASSWORD": "p"}) == sorted(
        ["ANTHROPIC_AUTH_TOKEN", "AIEP_UPSTREAM_API_KEY", "AWS_SECRET_ACCESS_KEY", "DB_PASSWORD"])
    assert P.refuse_secrets({"FOO": "sk-ant-api03-abc"}) == ["FOO"]
    fp = "7169605F62C751356D054A26A821E680E5FA6305"
    assert P.refuse_secrets({"GPG_KEY": fp, "PYTHON_SHA256": "b" * 64, "LANG": "C.UTF-8"}) == []  # the python image's own public fingerprint is not a secret
    assert P.refuse_secrets({"GPG_KEY": "hunter2"}) == ["GPG_KEY"] and P.refuse_secrets({"GPG_KEY": fp.lower()}) == ["GPG_KEY"]
    assert P.refuse_secrets({"OTHER_KEY": fp}) == ["OTHER_KEY"]


def test_main_refuses_a_secret_in_the_environment_a_bad_listen_value_or_a_missing_listen(monkeypatch, capsys):
    monkeypatch.setattr(P.os, "environ", {"PATH": "/bin", "HOME": "/tmp"})  # a controlled environment: the developer's own shell is not the test
    monkeypatch.delenv("AIEP_PROXY_LISTEN", raising=False)
    assert P.main([]) == 2 and "AIEP_PROXY_LISTEN" in capsys.readouterr().err
    monkeypatch.setenv("AIEP_PROXY_LISTEN", "net:172.30.2.0/24:3128")
    monkeypatch.setenv("AIEP_UPSTREAM_API_KEY", "sk-ant-api03-whatever")
    assert P.main([]) == 2 and "no secrets" in capsys.readouterr().err
    monkeypatch.delenv("AIEP_UPSTREAM_API_KEY")
    for bad in ("0.0.0.0:3128", "127.0.0.1:3128", "internal:3128", "net:0.0.0.0/0:3128"):
        monkeypatch.setenv("AIEP_PROXY_LISTEN", bad)
        assert P.main([]) == 2 and "invalid AIEP_PROXY_LISTEN" in capsys.readouterr().err


def test_the_proxy_module_is_stdlib_plus_listen_only_so_it_can_be_injected_alone():
    import ast
    tree = ast.parse((REAL / "src" / "aiep" / "egress_proxy.py").read_text())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add(("." * n.level) + (n.module or ""))
    import sys
    assert {m for m in mods if not m.startswith(".")} <= set(sys.stdlib_module_names)
    assert {m for m in mods if m.startswith(".")} == {".listen"}


# ------------------------------------------------------------------ audit hardening
@pytest.mark.parametrize("addr", ["fe80::1%eth0", "2607:6bc0::10%eth0", "2607:6bc0::10%1", "160.79.104.10%eth0"])
def test_a_scoped_address_in_a_dns_answer_is_never_accepted(addr):
    assert pick([addr]) == (502, "resolver_returned_a_non_address")


def test_handler_threads_are_bounded_beyond_the_tunnel_slots(tmp_path, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(P._Handler, "handle", lambda self: release.wait(10))
    cfg = P.ProxyConfig(max_connections=1)
    srv = P.ProxyServer(("127.0.0.1", 0), cfg)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    socks = []
    try:
        for _ in range(P.MAX_THREAD_FACTOR + 6):
            socks.append(socket.create_connection(("127.0.0.1", srv.server_address[1]), 3))
        time.sleep(0.5)
        assert srv._active == P.MAX_THREAD_FACTOR  # the cap: 8 threads for 1 slot
        closed = 0
        for s in socks[P.MAX_THREAD_FACTOR:]:
            s.settimeout(2)
            try:
                closed += s.recv(1) == b""  # over the cap: closed at once, no thread, no reply
            except (ConnectionResetError, socket.timeout):
                closed += 1
        assert closed == 6
        release.set()
        t0 = time.monotonic()
        while srv._active and time.monotonic() - t0 < 5:
            time.sleep(0.02)
        assert srv._active == 0  # every thread is accounted for again
    finally:
        release.set()
        for s in socks:
            s.close()
        srv.shutdown()
        srv.server_close()


def test_production_upstream_refuses_a_direct_connection_even_if_the_proxy_name_were_lost(tmp_path):
    cfg = G.production_upstream("api.anthropic.com", "aiep.proxy.r1:3128")
    assert cfg.require_proxy is True and cfg.proxy_host == "aiep.proxy.r1"
    gw = GwRig(tmp_path, dataclasses_replace(cfg, proxy_host=None))
    try:
        orig = Gateway._pick_address
        Gateway._pick_address = lambda self: (_ for _ in ()).throw(AssertionError("direct resolution attempted"))
        status, body = gw.post()
        assert status == 502 and b"proxy_required" in body
        assert gw.ledger()[-1]["status"] == 502
    finally:
        gw.close()
        Gateway._pick_address = orig


def dataclasses_replace(obj, **kw):
    import dataclasses
    return dataclasses.replace(obj, **kw)


def test_a_dead_client_cannot_pin_a_tunnel_slot_the_half_close_grace_ends_the_tunnel(rig):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    held = []
    threading.Thread(target=lambda: held.append(srv.accept()[0]), daemon=True).start()  # an upstream that never sends and never closes
    r = rig(target=("127.0.0.1", srv.getsockname()[1]), half_close_grace_s=0.6, idle_timeout_s=60.0)
    s = r.raw(head(), read=False)
    assert s.recv(100).startswith(b"HTTP/1.1 200")
    s.shutdown(socket.SHUT_WR)  # the client goes away
    t0 = time.monotonic()
    ev = r.wait_events(1, timeout=6)
    assert ev and ev[0]["reason"] == "half_close_timeout" and time.monotonic() - t0 < 4
    s.close()
    srv.close()


# ------------------------------------------------------------------ bounded DNS resolution
def bounded(fn, timeout=0.3, inflight=2):
    return P.BoundedResolver(fn, timeout, inflight)


def refusal_of(call):
    with pytest.raises(P.Refusal) as e:
        call()
    return e.value.status, e.value.reason


def test_a_resolution_within_the_deadline_returns_the_answers_unchanged():
    r = bounded(lambda h, p: ["160.79.104.10", "34.120.1.1"])
    assert r(HOST, 443) == ["160.79.104.10", "34.120.1.1"] and r.in_flight == 0 and r.timeouts == 0 and r.busy == 0


def test_a_resolver_failure_is_dns_failure_and_other_errors_are_not_disguised():
    def gai(h, p):
        raise socket.gaierror("no such host")

    def bug(h, p):
        raise KeyError("a bug")
    assert refusal_of(lambda: bounded(gai)(HOST, 443)) == (502, "dns_failure")
    with pytest.raises(KeyError):
        bounded(bug)(HOST, 443)
    r = bounded(gai)
    refusal_of(lambda: r(HOST, 443))
    assert r.in_flight == 0  # the slot and the counter come back even when the lookup failed


def test_a_stuck_lookup_times_out_distinctly_and_the_request_does_not_wait_for_it():
    gate = threading.Event()
    r = bounded(lambda h, p: gate.wait(10) and ["160.79.104.10"], timeout=0.3)
    t0 = time.monotonic()
    assert refusal_of(lambda: r(HOST, 443)) == (504, "dns_timeout")
    assert time.monotonic() - t0 < 2.0 and r.timeouts == 1 and r.in_flight == 1  # the worker is still inside the resolver
    gate.set()
    t0 = time.monotonic()
    while r.in_flight and time.monotonic() - t0 < 3:
        time.sleep(0.01)
    assert r.in_flight == 0  # and it ends by itself once the resolver returns; nothing else keeps it


def test_resolver_threads_cannot_accumulate_beyond_the_cap_extra_requests_are_refused_at_once():
    gate = threading.Event()
    before = threading.active_count()
    r = bounded(lambda h, p: gate.wait(10) and [], timeout=0.1, inflight=2)
    results = [refusal_of(lambda: r(HOST, 443)) for _ in range(40)]
    assert results[:2] == [(504, "dns_timeout")] * 2 and results[2:] == [(503, "dns_busy")] * 38
    assert r.in_flight == 2 and r.busy == 38 and threading.active_count() - before <= 2  # forty requests, two threads
    gate.set()
    t0 = time.monotonic()
    while r.in_flight and time.monotonic() - t0 < 3:
        time.sleep(0.01)
    assert r.in_flight == 0
    r2 = bounded(lambda h, p: ["160.79.104.10"])
    assert r2(HOST, 443) == ["160.79.104.10"]


def test_a_late_answer_is_discarded_and_never_used_for_a_later_request():
    calls = []
    gate = threading.Event()

    def resolver(h, p):
        calls.append(len(calls))
        if len(calls) == 1:
            gate.wait(5)
            return ["192.168.65.254"]  # the late answer of the timed-out lookup: private
        return ["160.79.104.10"]
    r = bounded(resolver, timeout=0.2)
    assert refusal_of(lambda: r(HOST, 443)) == (504, "dns_timeout")
    gate.set()
    time.sleep(0.2)
    cfg = P.ProxyConfig()
    assert P.pick_pinned_address(cfg, r) == "160.79.104.10"  # the next request does its own, fresh lookup; the stale answer went nowhere


def test_a_slow_but_in_time_answer_is_still_fully_validated_and_a_mixed_one_refused():
    def slow(addrs):
        return lambda h, p: (time.sleep(0.1), addrs)[1]
    cfg = P.ProxyConfig()
    assert P.pick_pinned_address(cfg, bounded(slow(["160.79.104.10"]), 1.0)) == "160.79.104.10"
    mixed = bounded(slow(["160.79.104.10", "192.168.65.254"]), 1.0)
    assert refusal_of(lambda: P.pick_pinned_address(cfg, mixed)) == (403, "resolves_to_non_public_address")
    assert refusal_of(lambda: P.pick_pinned_address(cfg, bounded(slow(["fe80::1%eth0"]), 1.0))) == (502, "resolver_returned_a_non_address")


def test_the_server_answers_504_and_503_for_dns_and_frees_its_tunnel_slot(tmp_path):
    gate = threading.Event()
    r = Rig(tmp_path, dns_timeout_s=0.3, dns_max_inflight=1)
    try:
        r.server.resolver = P.BoundedResolver(lambda h, p: gate.wait(10) and ["127.0.0.1"], 0.3, 1)
        assert r.raw(head()) == (504, "dns_timeout")
        assert r.raw(head()) == (503, "dns_busy")  # the first lookup is still stuck and holds the only resolver slot
        assert r.connects == []  # nothing was ever dialled
        ev = r.wait_events(2)
        assert [e["reason"] for e in ev] == ["dns_timeout", "dns_busy"] and all(e["outcome"] == "refused" for e in ev)
        t0 = time.monotonic()
        while r.server.slots._value != r.cfg.max_connections and time.monotonic() - t0 < 3:
            time.sleep(0.02)
        assert r.server.slots._value == r.cfg.max_connections  # both tunnel slots released
        gate.set()
    finally:
        gate.set()
        r.close()


def test_the_server_uses_a_bounded_resolver_built_from_the_config_by_default():
    srv = P.ProxyServer(("127.0.0.1", 0), P.ProxyConfig(dns_timeout_s=1.5, dns_max_inflight=3))
    try:
        assert isinstance(srv.resolver, P.BoundedResolver) and srv.resolver.timeout_s == 1.5
    finally:
        srv.server_close()

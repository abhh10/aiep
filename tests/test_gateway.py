"""Gateway security and metering tests. Loopback only: a fake TLS upstream, no real network or key."""

import json
import shutil
import socket
import ssl
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from aiep import gateway as gwmod
from aiep.gateway import Gateway, GatewayConfig, UpstreamConfig
from aiep.ledger import Ledger
from aiep.pricing import load_pricing
from fake_upstream import (FakeUpstream, TcpTripwire, json_ok, make_cert, message_json,
                           sse_events, stream)

REAL = Path(__file__).resolve().parents[1]
MODEL = "claude-sonnet-5-5"
REAL_KEY = "sk-ant-REAL-UPSTREAM-KEY-DO-NOT-LEAK"
TOKEN = "per-run-token-abc123"
PROMPT = "SECRET-PROMPT-TEXT-xyz"
UPSTREAM_HOST = "upstream.test"

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI needed for test certificates")


@pytest.fixture(scope="session")
def certs(tmp_path_factory):
    d = tmp_path_factory.mktemp("certs")
    return {"good": make_cert(d, UPSTREAM_HOST, "good"), "wrong_host": make_cert(d, "other.test", "wrong")}


def body_for(model=MODEL, **extra):
    doc = {"model": model, "max_tokens": 1024, "messages": [{"role": "user", "content": PROMPT}], **extra}
    return json.dumps(doc).encode()


class Rig:
    def __init__(self, tmp_path, certs, **cfg_over):
        self.certs = certs
        self.ledger_path = tmp_path / "ledger.jsonl"
        self.up = FakeUpstream(*certs["good"]).__enter__()
        up_over = cfg_over.pop("upstream_over", {})
        upstream = UpstreamConfig(host=UPSTREAM_HOST, port=self.up.port, ca_file=str(certs["good"][0]),
                                  allow_private_addresses=True, resolver=lambda h, p: ["127.0.0.1"],
                                  read_timeout=10, **up_over)
        self.cfg = GatewayConfig(
            run_token=TOKEN, upstream_key=REAL_KEY, model=MODEL,
            pricing=load_pricing(REAL / "config" / "model_pricing.json", MODEL),
            ledger=Ledger(self.ledger_path), allowed_hosts=frozenset(),
            budget_usd=cfg_over.pop("budget_usd", Decimal("100")), upstream=upstream, **cfg_over)
        self.gw = Gateway(self.cfg)
        self.server = self.gw.make_server("127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.host = f"gateway.test:{self.port}"
        self.cfg.allowed_hosts = frozenset({self.host})
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.up.__exit__()

    def ledger(self):
        return [json.loads(l) for l in self.ledger_path.read_text().splitlines()]

    def request(self, method="POST", path="/v1/messages?beta=true", body=None, headers=None, host="DEFAULT", auth=True):
        body = body_for() if body is None else body
        h = {"Host": self.host if host == "DEFAULT" else host, "Content-Type": "application/json",
             "Content-Length": str(len(body))}
        if auth:
            h["Authorization"] = f"Bearer {TOKEN}"
        h.update(headers or {})
        return self.raw(method, path, h, body)

    def raw(self, method, target, headers, body=b""):
        items = headers.items() if isinstance(headers, dict) else headers  # a list of pairs allows duplicates
        lines = [f"{method} {target} HTTP/1.1"] + [f"{k}: {v}" for k, v in items if v is not None]
        data = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            s.sendall(data)
            buf = b""
            while True:
                try:
                    chunk = s.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
        finally:
            s.close()
        head, _, rest = buf.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1]) if head else 0
        hdrs = {}
        for line in head.split(b"\r\n")[1:]:
            k, _, v = line.partition(b":")
            hdrs[k.decode().lower()] = v.decode().strip()
        return status, hdrs, rest


@pytest.fixture
def rig(tmp_path, certs):
    made = []

    def make(**kw):
        r = Rig(tmp_path / f"r{len(made)}", certs, **kw)
        made.append(r)
        return r

    (tmp_path / "r0").mkdir(parents=True, exist_ok=True)
    for i in range(1, 4):
        (tmp_path / f"r{i}").mkdir(parents=True, exist_ok=True)
    yield make
    for r in made:
        r.close()


# ====================== metering ======================
def test_non_streaming_request_is_forwarded_and_metered(rig):
    r = rig()
    status, hdrs, body = r.request()
    assert status == 200 and json.loads(body)["usage"]["input_tokens"] == 1000
    entry = r.ledger()[-1]
    # (1000*2 + 200*10 + 5000*0.10 + 400*4) / 1e6, cache writes unclassified -> 1h rate
    assert entry["cost_usd_computed"] == "0.0061" and entry["cost_upper_bound"] is True
    assert r.cfg.ledger.spent == Decimal("0.0061")
    assert entry["model"] == MODEL and entry["endpoint"] == "/v1/messages"


def test_streaming_usage_is_metered_and_bytes_are_identical(rig):
    r = rig()
    events = sse_events()
    r.up.set_behavior(lambda h, q: stream(h, events))
    status, hdrs, body = r.request()
    assert status == 200 and hdrs["content-type"] == "text/event-stream"
    assert body == b"".join(events)  # byte-for-byte
    e = r.ledger()[-1]
    assert e["usage"]["output_tokens"] == 200 and e["usage"]["cache_read_tokens"] == 5000
    assert e["cost_usd_computed"] == "0.0061" and "usage_incomplete_upper_bound" not in e


def test_cache_breakdown_when_reported(rig):
    r = rig()
    usage = {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 5000,
             "cache_creation_input_tokens": 400,
             "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 300}}
    r.up.set_behavior(lambda h, q: json_ok(h, q, message_json(usage)))
    r.request()
    e = r.ledger()[-1]
    assert e["cost_usd_computed"] == "0.00595" and e["cost_upper_bound"] is False


def test_streaming_is_not_buffered(rig):
    r = rig()
    gate = threading.Event()
    events = sse_events()

    def behave(h, q):
        stream(h, events, between=lambda i: gate.wait(5) if i == 0 else None)

    r.up.set_behavior(behave)
    body = body_for()
    s = socket.create_connection(("127.0.0.1", r.port), timeout=4)
    s.sendall((f"POST /v1/messages?beta=true HTTP/1.1\r\nHost: {r.host}\r\nAuthorization: Bearer {TOKEN}\r\n"
               f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
    seen = b""
    while b"message_start" not in seen:  # would time out (and fail) if the gateway buffered
        seen += s.recv(4096)
    gate.set()
    rest = b""
    while True:
        c = s.recv(65536)
        if not c:
            break
        rest += c
    s.close()
    assert b"message_stop" in seen + rest


def test_interrupted_stream_is_charged_at_the_request_maximum(rig):
    r = rig()
    r.up.set_behavior(lambda h, q: stream(h, sse_events(stop=False)[:1] and sse_events()[:1]))
    r.request()
    e = r.ledger()[-1]
    assert e["usage_incomplete_upper_bound"] is True and e["usage"]["output_tokens"] == 1024


def test_unmeterable_success_stops_the_gateway(rig):
    r = rig()
    r.up.set_behavior(lambda h, q: json_ok(h, q, b'{"type":"message","content":[]}'))
    r.request()
    assert r.gw.stopped == "metering_failure" and r.ledger()[-1]["metering"] == "failed"
    assert r.request()[0] == 403


def test_count_tokens_is_forwarded_and_free(rig):
    r = rig()
    r.up.set_behavior(lambda h, q: json_ok(h, q, b'{"input_tokens": 123}'))
    status, _, body = r.request(path="/v1/messages/count_tokens", body=json.dumps({"model": MODEL, "messages": []}).encode())
    assert status == 200 and json.loads(body) == {"input_tokens": 123}
    assert r.ledger()[-1]["cost_usd_computed"] == "0" and r.cfg.ledger.spent == 0


def test_budget_exhaustion_refuses_further_requests(rig):
    r = rig(budget_usd=Decimal("0.005"))
    assert r.request()[0] == 200  # costs 0.0061, over budget
    assert r.gw.stopped == "budget_exhausted"
    status, _, body = r.request()
    assert status == 403 and b"stopped" in body
    assert len(r.up.requests) == 1


# ====================== forwarding fidelity and credentials ======================
def test_request_forwarded_verbatim_with_real_key_only_upstream(rig):
    r = rig()
    odd = b'{ "model" :"claude-sonnet-5-5",\n "max_tokens":  5, "system":[{"type":"text","text":"x","cache_control":{"type":"ephemeral"}}],"messages":[]}'
    r.request(body=odd, headers={"anthropic-version": "2023-06-01", "anthropic-beta": "a,b",
                                 "Cookie": "session=1", "X-Forwarded-For": "1.2.3.4", "x-api-key": None})
    up = r.up.requests[0]
    assert up["body"] == odd
    h = up["headers"]
    assert h["x-api-key"] == REAL_KEY and "authorization" not in h and "cookie" not in h
    assert h["anthropic-version"] == "2023-06-01" and h["anthropic-beta"] == "a,b"
    assert h["host"] == f"{UPSTREAM_HOST}:{r.up.port}"
    assert h["accept-encoding"] == "identity"
    assert TOKEN not in json.dumps(h)
    assert up["path"] == "/v1/messages?beta=true"


def test_response_headers_are_allowlisted(rig):
    r = rig()
    r.up.set_behavior(lambda h, q: json_ok(h, q, headers={
        "anthropic-organization-id": "org-secret", "Set-Cookie": "a=b", "request-id": "req_1",
        "anthropic-ratelimit-requests-remaining": "9", "Location": "http://evil/"}))
    _, hdrs, _ = r.request()
    assert hdrs["request-id"] == "req_1" and hdrs["anthropic-ratelimit-requests-remaining"] == "9"
    for banned in ("anthropic-organization-id", "set-cookie", "location"):
        assert banned not in hdrs


def test_secrets_and_prompts_never_in_ledger_or_output(rig, capfd):
    r = rig()
    r.request()
    r.request(auth=False)
    r.request(body=body_for(model="evil"))
    text = r.ledger_path.read_text()
    out = capfd.readouterr()
    for secret in (REAL_KEY, TOKEN, PROMPT):
        assert secret not in text and secret not in out.out and secret not in out.err
    assert REAL_KEY not in repr(r.cfg) and TOKEN not in repr(r.cfg)


def test_real_key_never_sent_to_client(rig):
    r = rig()
    _, hdrs, body = r.request()
    assert REAL_KEY.encode() not in body and REAL_KEY not in json.dumps(hdrs)


# ====================== authentication ======================
@pytest.mark.parametrize("headers", [
    {"Authorization": None}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic " + TOKEN},
    {"Authorization": TOKEN}, {"Authorization": "Bearer "},
])
def test_bad_credentials_rejected_before_anything_else(rig, headers):
    r = rig()
    assert r.request(auth=False, headers=headers)[0] == 401
    assert r.up.requests == []


def test_x_api_key_header_carrying_the_run_token_is_accepted(rig):
    r = rig()
    assert r.request(auth=False, headers={"x-api-key": TOKEN})[0] == 200
    assert r.request(auth=False, headers={"x-api-key": "nope"})[0] == 401


# ====================== Host header, DNS rebinding, ports ======================
@pytest.mark.parametrize("host", [
    "evil.com", "attacker.example:80", "gateway.test:1", "gateway.test", "127.0.0.1", "localhost",
    "", "gateway.test:99999", "169.254.169.254",
])
def test_host_header_manipulation_and_dns_rebinding_refused_even_with_valid_token(rig, host):
    r = rig()
    # a browser tricked by DNS rebinding would carry the attacker's hostname here
    assert r.request(host=host)[0] == 421
    assert r.up.requests == []


def test_duplicate_or_missing_host_refused(rig):
    r = rig()
    body = body_for()
    common = {"Authorization": f"Bearer {TOKEN}", "Content-Length": str(len(body))}
    pairs = [("Host", r.host), ("Host", "evil.com"), *common.items()]
    assert r.raw("POST", "/v1/messages", pairs, body)[0] == 421
    assert r.raw("POST", "/v1/messages", pairs[::-1], body)[0] == 421  # order must not matter
    assert r.raw("POST", "/v1/messages", common, body)[0] == 421  # no Host at all
    assert r.up.requests == []


def test_host_match_is_case_insensitive_but_exact(rig):
    r = rig()
    assert r.request(host=r.host.upper())[0] == 200


def test_forwarding_headers_cannot_redirect_the_upstream_hop(rig, monkeypatch):
    r = rig()
    calls = []
    real = socket.create_connection
    monkeypatch.setattr(socket, "create_connection", lambda addr, *a, **k: (calls.append(addr), real(addr, *a, **k))[1])
    r.request(headers={"X-Forwarded-Host": "evil.com", "X-Forwarded-Port": "22", "Forwarded": "host=evil.com",
                       "X-Original-URL": "http://evil/", "X-Host": "evil.com"})
    up = r.up.requests[0]
    assert up["headers"]["host"] == f"{UPSTREAM_HOST}:{r.up.port}"
    assert "x-forwarded-host" not in up["headers"] and "forwarded" not in up["headers"]
    assert [c for c in calls if c[1] != r.port] == [("127.0.0.1", r.up.port)]  # only the configured port


@pytest.mark.parametrize("resolved", [
    ["10.0.0.5"], ["192.168.1.10"], ["127.0.0.1"], ["169.254.169.254"], ["0.0.0.0"],
    ["::1"], ["::ffff:127.0.0.1"], ["fd00::1"], ["93.184.216.34", "10.0.0.5"], ["224.0.0.1"],
])
def test_upstream_name_resolving_to_non_public_address_is_refused(rig, monkeypatch, resolved):
    r = rig(upstream_over={})
    r.cfg.upstream = UpstreamConfig(host=UPSTREAM_HOST, port=r.up.port, ca_file=str(r.certs["good"][0]),
                                    allow_private_addresses=False, resolver=lambda h, p: resolved)
    connects = []
    real = socket.create_connection

    def guarded(addr, *a, **k):
        if addr[1] == r.port:  # the test client talking to the gateway itself
            return real(addr, *a, **k)
        connects.append(addr)
        raise AssertionError("the gateway connected upstream")

    monkeypatch.setattr(socket, "create_connection", guarded)
    status, _, body = r.request()
    assert status == 502 and b"non_public" in body
    assert connects == [] and r.up.requests == []


def test_dns_answer_is_resolved_once_and_the_connection_is_pinned_to_it(rig, monkeypatch):
    r = rig()
    answers = iter([["127.0.0.1"], ["10.9.9.9"], ["10.9.9.9"]])  # a rebinding resolver
    count = []
    r.cfg.upstream = UpstreamConfig(host=UPSTREAM_HOST, port=r.up.port, ca_file=str(r.certs["good"][0]),
                                    allow_private_addresses=True, resolver=lambda h, p: (count.append(1), next(answers))[1])
    seen = []
    real = socket.create_connection
    monkeypatch.setattr(socket, "create_connection", lambda addr, *a, **k: (seen.append(addr), real(addr, *a, **k))[1])
    assert r.request()[0] == 200
    assert len(count) == 1
    assert [a for a in seen if a[1] == r.up.port] == [("127.0.0.1", r.up.port)]


def test_dns_failure_is_a_clean_502(rig):
    r = rig()
    def boom(h, p):
        raise socket.gaierror("nope")
    r.cfg.upstream = UpstreamConfig(host=UPSTREAM_HOST, port=r.up.port, ca_file=str(r.certs["good"][0]),
                                    allow_private_addresses=True, resolver=boom)
    assert r.request()[0] == 502


# ====================== TLS verification ======================
def test_certificate_for_the_wrong_hostname_is_refused_and_nothing_is_sent(rig, certs):
    r = rig()
    r.up.__exit__()
    r.up = FakeUpstream(*certs["wrong_host"]).__enter__()
    r.cfg.upstream = UpstreamConfig(host=UPSTREAM_HOST, port=r.up.port, ca_file=str(certs["wrong_host"][0]),
                                    allow_private_addresses=True, resolver=lambda h, p: ["127.0.0.1"])
    status, _, body = r.request()
    assert status == 502 and b"tls_verification_failed" in body
    assert r.up.requests == []  # the request (and the real key) were never sent


def test_untrusted_certificate_is_refused_with_default_trust_store(rig):
    r = rig()
    r.cfg.upstream = UpstreamConfig(host=UPSTREAM_HOST, port=r.up.port, ca_file=None,  # system store
                                    allow_private_addresses=True, resolver=lambda h, p: ["127.0.0.1"])
    status, _, body = r.request()
    assert status == 502 and b"tls_verification_failed" in body and r.up.requests == []


def test_gateway_never_speaks_plaintext_to_upstream(rig):
    r = rig()
    r.up.__exit__()
    r.up = FakeUpstream().__enter__()  # plain HTTP listener
    r.cfg.upstream = UpstreamConfig(host=UPSTREAM_HOST, port=r.up.port, ca_file=None,
                                    allow_private_addresses=True, resolver=lambda h, p: ["127.0.0.1"])
    assert r.request()[0] == 502
    assert r.up.requests == []  # no readable HTTP request ever reached it, so no key either


# ====================== redirects ======================
def test_upstream_redirect_is_neither_followed_nor_passed_on(rig):
    r = rig()
    with TcpTripwire() as evil:
        def redirect(h, q):
            h.send_response(302)
            h.send_header("Location", f"http://127.0.0.1:{evil.port}/steal")
            h.send_header("Content-Length", "0")
            h.end_headers()
        r.up.set_behavior(redirect)
        status, hdrs, body = r.request()
        assert status == 502 and "location" not in hdrs and b"redirect" in body
        assert evil.connections == 0
    assert len(r.up.requests) == 1


# ====================== size limits ======================
def test_oversized_request_refused_before_forwarding(rig):
    r = rig(max_request_bytes=1024)
    assert r.request(body=body_for() + b" " * 2048)[0] == 413
    assert r.up.requests == []


def test_huge_declared_content_length_refused_without_reading(rig):
    r = rig()
    status, _, _ = r.raw("POST", "/v1/messages", {"Host": r.host, "Authorization": f"Bearer {TOKEN}",
                                                   "Content-Length": str(10**12)})
    assert status == 413 and r.up.requests == []


def test_oversized_streamed_response_is_cut_off(rig):
    r = rig(max_response_bytes=2000)
    big = [b"data: " + b"x" * 1500 + b"\n\n"] * 20
    r.up.set_behavior(lambda h, q: stream(h, big))
    status, _, body = r.request()
    assert len(body) <= 2000 + 1  # never relays more than the cap
    assert r.ledger()[-1]["outcome"] == "response_too_large"


def test_oversized_declared_response_refused(rig):
    r = rig(max_response_bytes=1000)
    r.up.set_behavior(lambda h, q: json_ok(h, q, b"{" + b" " * 5000 + b"}"))
    status, _, body = r.request()
    assert status == 502 and b"too large" in body


def test_max_tokens_cap_enforced_without_rewriting(rig):
    r = rig(max_tokens_cap=100)
    assert r.request(body=body_for(max_tokens=101))[0] == 400
    assert r.up.requests == []


@pytest.mark.parametrize("mt", [None, 0, -1, "10", True, 1.5])
def test_max_tokens_must_be_a_sane_int(rig, mt):
    r = rig()
    doc = {"model": MODEL, "messages": []}
    if mt is not None:
        doc["max_tokens"] = mt
    assert r.request(body=json.dumps(doc).encode())[0] == 400


# ====================== not an arbitrary proxy ======================
@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
def test_only_post_is_allowed(rig, method):
    r = rig()
    assert r.request(method=method, body=b"")[0] == 405 and r.up.requests == []


@pytest.mark.parametrize("path", [
    "/", "/v1/models", "/v1/messages/batches", "/v1/complete", "/v1/files", "/api/hello", "/v1/messages/",
    "/v1/messages/../models", "/v1/messages/..%2fmodels", "/v1/%6dessages", "/v1/messages;x=1",
    "/v1/messages@evil.com", "/v1/messages#x", "/V1/messages", "/v1/messages\\..\\x", "/v1/messages%00",
    "/v1/messages?beta=true&x=1", "/v1/messages?url=http://evil.com", "/v1/messages?beta=false",
    "/v1/messages?", "/v1/messages?beta=true&beta=true", "/v1//messages", "/./v1/messages",
])
def test_only_exact_paths_and_queries_are_forwarded(rig, path):
    r = rig()
    status, _, _ = r.request(path=path)
    assert status in (404, 400) and r.up.requests == []


@pytest.mark.parametrize("target", [
    "http://evil.com/v1/messages", "https://api.anthropic.com/v1/messages", "//evil.com/v1/messages",
    "evil.com:443", "*", "ftp://x/y",
])
def test_absolute_uri_authority_and_other_forms_refused(rig, target):
    r = rig()
    for method in ("POST", "GET", "CONNECT"):
        assert r.raw(method, target, {"Host": r.host, "Authorization": f"Bearer {TOKEN}", "Content-Length": "0"})[0] in (400, 404, 405)
    assert r.up.requests == []


def test_connect_tunnel_refused(rig):
    r = rig()
    status, _, _ = r.raw("CONNECT", "evil.com:443", {"Host": "evil.com:443"})
    assert status == 400 and r.up.requests == []


def test_chunked_request_bodies_refused(rig):
    r = rig()
    status, _, _ = r.raw("POST", "/v1/messages", {"Host": r.host, "Authorization": f"Bearer {TOKEN}",
                                                   "Transfer-Encoding": "chunked"}, b"5\r\nhello\r\n0\r\n\r\n")
    assert status == 501 and r.up.requests == []


def test_conflicting_content_lengths_refused(rig):
    r = rig()
    body = body_for()
    status, _, _ = r.raw("POST", "/v1/messages", [("Host", r.host), ("Authorization", f"Bearer {TOKEN}"),
                                                   ("Content-Length", str(len(body))), ("Content-Length", "5")], body)
    assert status in (400, 411) and r.up.requests == []


def test_upstream_host_is_not_configurable_from_the_entry_point(monkeypatch):
    src = Path(gwmod.__file__).read_text()
    main_src = src[src.index("def main("):]
    for forbidden in ("ca_file", "allow_private_addresses", "resolver", "port=", "AIEP_UPSTREAM_HOST", "argv"):
        # main() only takes the default UpstreamConfig (host comes from the repo's experiment.json)
        assert forbidden not in main_src.replace("argv: list[str] | None = None", "")
    assert gwmod.DEFAULT_UPSTREAM_HOST == "api.anthropic.com"


def test_main_refuses_to_start_without_its_environment(monkeypatch):
    for k in ("AIEP_UPSTREAM_API_KEY", "AIEP_RUN_TOKEN", "AIEP_BUDGET_USD", "AIEP_ALLOWED_HOSTS", "AIEP_LISTEN", "AIEP_LEDGER_PATH"):
        monkeypatch.delenv(k, raising=False)
    assert gwmod.main() == 2


# ====================== one model, enforced; stop on violation ======================
@pytest.mark.parametrize("model", [
    "claude-haiku-5-5", "claude-sonnet-5", "claude-sonnet-5-5-20260101", "CLAUDE-SONNET-5-5", "", None, 5, ["claude-sonnet-5-5"],
])
def test_any_other_model_is_rejected_and_the_gateway_stops(rig, model):
    r = rig()
    doc = {"max_tokens": 5, "messages": []}
    if model is not None:
        doc["model"] = model
    status, _, body = r.request(body=json.dumps(doc).encode())
    assert status == 403 and r.up.requests == []
    assert r.gw.stopped == "model_violation"
    # the violation is terminal: even a perfectly valid request is now refused
    status, _, body = r.request()
    assert status == 403 and b"stopped: model_violation" in body
    assert r.up.requests == []


def test_model_violation_is_recorded_with_the_requested_id(rig):
    r = rig()
    r.request(body=body_for(model="claude-haiku-5-5"))
    e = [x for x in r.ledger() if x.get("reason") == "model not allowed"][0]
    assert e["requested_model"] == "claude-haiku-5-5"


def test_model_violation_on_count_tokens_also_stops(rig):
    r = rig()
    r.request(path="/v1/messages/count_tokens", body=json.dumps({"model": "claude-haiku-5-5", "messages": []}).encode())
    assert r.gw.stopped == "model_violation"


@pytest.mark.parametrize("extra,ok", [
    ({"tools": [{"name": "Bash", "description": "d", "input_schema": {"type": "object"}}]}, True),
    ({"tools": [{"type": "custom", "name": "x", "input_schema": {}}]}, True),
    ({"tools": []}, True),
    ({"tools": [{"type": "web_search_20250305", "name": "web_search"}]}, False),
    ({"tools": [{"type": "bash_20250124", "name": "bash"}]}, False),
    ({"tools": [{"type": "code_execution_20250522", "name": "code_execution"}]}, False),
    ({"tools": "web_search"}, False),
    ({"tools": ["web_search"]}, False),
    ({"mcp_servers": [{"type": "url", "url": "https://evil.example/mcp", "name": "m"}]}, False),
])
def test_server_side_tools_and_mcp_connectors_are_refused(rig, extra, ok):
    r = rig()
    status, _, _ = r.request(body=body_for(**extra))
    if ok:
        assert status == 200
    else:
        assert status == 403 and r.gw.stopped == "tool_violation" and r.up.requests == []


def test_stopped_gateway_stays_stopped(rig):
    r = rig()
    r.gw.stop("test")
    r.gw.stop("other")
    assert r.gw.stopped == "test" and r.request()[0] == 403


# ====================== ledger hygiene ======================
def test_unauthenticated_flood_cannot_grow_the_ledger_without_bound(rig):
    r = rig()
    for _ in range(gwmod.UNAUTH_LEDGER_CAP + 30):
        r.request(auth=False)
    assert len(r.ledger()) <= gwmod.UNAUTH_LEDGER_CAP
    assert r.gw.unauthenticated_refusals >= gwmod.UNAUTH_LEDGER_CAP + 30


def test_ledger_file_is_never_reused(tmp_path):
    p = tmp_path / "l.jsonl"
    Ledger(p)
    with pytest.raises(FileExistsError):
        Ledger(p)


# ====================== audit fixes (final Phase 1 review) ======================
@pytest.mark.parametrize("addr,public", [
    ("93.184.216.34", True), ("2606:4700:4700::1111", True), ("::ffff:93.184.216.34", True),
    # previously accepted by ipaddress.is_global, now refused:
    ("64:ff9b::a00:1", False),          # NAT64 embedding 10.0.0.1
    ("64:ff9b::5db8:d822", False),      # NAT64 embedding a public address: still refused (strict)
    ("64:ff9b:1::a00:1", False),        # local-use NAT64
    ("2002:a00:1::1", False),           # 6to4 embedding 10.0.0.1
    ("2001:0:4136:e378:8000:63bf:3fff:fdd2", False),  # Teredo
    ("2001:db8::1", False),             # documentation range
    ("192.0.0.8", False),               # IETF protocol assignment
    ("10.0.0.1", False), ("100.64.0.1", False), ("198.18.0.1", False), ("240.0.0.1", False),
    ("127.0.0.1", False), ("169.254.169.254", False), ("::1", False), ("fe80::1", False),
    ("fc00::1", False), ("::ffff:10.0.0.1", False), ("224.0.0.1", False), ("::", False), ("0.0.0.0", False),
])
def test_address_is_public_is_strict(addr, public):
    assert gwmod.address_is_public(addr) is public


def test_resolver_returning_garbage_is_a_clean_502(rig):
    r = rig()
    for bad in (["not-an-ip"], ["example.com"], [""], ["999.1.1.1"]):
        r.cfg.upstream = UpstreamConfig(host=UPSTREAM_HOST, port=r.up.port, ca_file=str(r.certs["good"][0]),
                                        allow_private_addresses=True, resolver=lambda h, p, b=bad: b)
        assert r.request()[0] == 502
    assert r.up.requests == [] and r.gw.internal_errors == 0


def test_tls_floor_and_verification_flags():
    import ssl

    r = gwmod.Gateway.__new__(gwmod.Gateway)
    r.cfg = type("C", (), {"upstream": UpstreamConfig()})()
    ctx = r._ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_connect_timeout_is_applied_to_the_connection(rig, monkeypatch):
    r = rig(upstream_over={"connect_timeout": 3.0})
    seen = []
    real = socket.create_connection

    def spy(addr, timeout=None, *a, **k):
        if addr[1] == r.up.port:
            seen.append(timeout)
        return real(addr, timeout, *a, **k)

    monkeypatch.setattr(socket, "create_connection", spy)
    assert r.request()[0] == 200
    assert seen == [3.0]


@pytest.mark.parametrize("value", ["²", "9" * 5000, "-1", "1e3", "0x10", "", " ", "1_000", "+5", "١٢"])
def test_hostile_content_length_values_never_crash_the_gateway(rig, value):
    r = rig()
    status, _, _ = r.raw("POST", "/v1/messages", {"Host": r.host, "Authorization": f"Bearer {TOKEN}",
                                                   "Content-Length": value})
    assert status in (400, 411, 413) and r.up.requests == []
    assert r.gw.internal_errors == 0
    assert r.request()[0] == 200  # still serving


def test_unexpected_internal_error_is_a_generic_500_without_secrets(rig, capfd, monkeypatch):
    r = rig()

    def boom():
        raise RuntimeError(f"secret detail {REAL_KEY} {PROMPT}")

    monkeypatch.setattr(r.gw, "open_upstream", boom)
    status, _, body = r.request()
    assert status == 500 and b"internal gateway error" in body and REAL_KEY.encode() not in body
    last = r.ledger()[-1]
    assert last["outcome"] == "internal_error" and last["error_type"] == "RuntimeError"
    out = capfd.readouterr()
    for secret in (REAL_KEY, PROMPT, "Traceback"):
        assert secret not in out.err and secret not in out.out
    assert REAL_KEY not in r.ledger_path.read_text()
    monkeypatch.undo()
    assert r.request()[0] == 200  # thread and server survived


def _slow_upstream(r, delay, peak):
    state = {"now": 0}
    lock = threading.Lock()

    def behave(h, q):
        with lock:
            state["now"] += 1
            peak.append(state["now"])
        import time as _t
        _t.sleep(delay)
        json_ok(h, q)
        with lock:
            state["now"] -= 1

    r.up.set_behavior(behave)


def _burst(r, n):
    results = []

    def one():
        try:
            results.append(r.request()[0])
        except OSError as e:  # a reset instead of a status code is a gateway bug
            results.append(f"ERR:{type(e).__name__}")

    threads = [threading.Thread(target=one) for _ in range(n)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    return results


def test_upstream_concurrency_never_exceeds_the_configured_limit(rig):
    r = rig(max_concurrent=2)
    peak = []
    _slow_upstream(r, 0.15, peak)
    results = _burst(r, 8)
    assert results == [200] * 8 and max(peak) <= 2


@pytest.mark.parametrize("limit", [1, 2])
def test_concurrent_requests_cannot_overshoot_the_budget_by_more_than_the_limit(rig, limit):
    # each request costs 0.0061; the budget allows less than one
    r = rig(max_concurrent=limit, budget_usd=Decimal("0.005"))
    peak = []
    _slow_upstream(r, 0.2, peak)
    results = _burst(r, 10)
    assert len(r.up.requests) <= limit, "more requests reached upstream than the overshoot bound allows"
    assert r.cfg.ledger.spent <= limit * Decimal("0.0061")
    assert results.count(200) == len(r.up.requests) and all(s in (200, 403) for s in results), results
    assert r.gw.stopped == "budget_exhausted"


def test_busy_gateway_answers_429_with_retry_after(rig):
    r = rig(max_concurrent=1, queue_timeout_s=0.2)
    peak = []
    _slow_upstream(r, 1.0, peak)
    results, hdrs = [], []
    t = threading.Thread(target=lambda: results.append(r.request()[0]))
    t.start()
    import time as _t
    _t.sleep(0.3)
    status, h, _ = r.request()
    t.join(10)
    assert status == 429 and h["retry-after"] == "1" and h["x-should-retry"] == "true"
    assert results == [200]


def test_refusals_deliver_their_status_even_when_the_body_was_not_read(rig):
    """A stopped gateway must still answer 403 cleanly (not reset the connection) while a large body is in flight."""
    r = rig()
    r.gw.stop("model_violation")
    big = body_for() + b" " * 200_000
    for _ in range(20):
        status, _, body = r.request(body=big)
        assert status == 403 and b"stopped: model_violation" in body
    for _ in range(20):
        assert r.request(auth=False, body=big)[0] == 401


# ====================== stop file (the orchestrator reads it through docker exec) ======================
def test_stop_reason_is_written_atomically_to_the_stop_file(rig, tmp_path):
    r = rig()
    r.cfg.stop_file = tmp_path / "gateway.stop"
    assert not r.cfg.stop_file.exists()
    r.request(body=body_for(model="claude-haiku-5-5"))
    assert r.cfg.stop_file.read_text() == "model_violation"
    assert not Path(f"{r.cfg.stop_file}.tmp").exists()
    r.gw.stop("budget_exhausted")  # a later stop never overwrites the first reason
    assert r.cfg.stop_file.read_text() == "model_violation" and r.gw.stopped == "model_violation"


def test_stop_file_for_each_stop_reason(rig, tmp_path):
    for trigger, expected in ((lambda r: r.request(body=body_for(**{"tools": [{"type": "web_search_20250305", "name": "w"}]})),
                               "tool_violation"),
                              (lambda r: r.request(), "budget_exhausted")):
        r = rig(budget_usd=Decimal("0.001")) if expected == "budget_exhausted" else rig()
        r.cfg.stop_file = tmp_path / f"{expected}.stop"
        trigger(r)
        assert r.cfg.stop_file.read_text() == expected


def test_a_stop_file_that_cannot_be_written_never_unstops_the_gateway(rig, tmp_path):
    r = rig()
    r.cfg.stop_file = tmp_path / "no_such_dir" / "gateway.stop"
    r.request(body=body_for(model="claude-haiku-5-5"))
    assert r.gw.stopped == "model_violation" and r.gw.stop_file_errors == 1
    assert r.request()[0] == 403


def test_main_reads_the_stop_file_path_from_the_environment():
    src = Path(gwmod.__file__).read_text()
    assert 'env.get("AIEP_STOP_FILE")' in src and "stop_file=" in src


# ====================== no leaked upstream sockets ======================
def _track_client_sockets(monkeypatch, up_port):
    """Record every socket the gateway opens toward the fake upstream: raw and TLS-wrapped (client side only)."""
    raw, tls = [], []
    real_cc, real_wrap = socket.create_connection, ssl.SSLContext.wrap_socket

    def cc(addr, *a, **k):
        s = real_cc(addr, *a, **k)
        if addr[1] == up_port:
            raw.append(s)
        return s

    def wrap(self, sock, *a, **k):
        s = real_wrap(self, sock, *a, **k)
        if not k.get("server_side"):
            tls.append(s)
        return s

    monkeypatch.setattr(socket, "create_connection", cc)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", wrap)
    return raw, tls


def test_upstream_sockets_are_closed_after_success_redirect_and_tls_failure(rig, monkeypatch, certs):
    r = rig()
    raw, tls = _track_client_sockets(monkeypatch, r.up.port)
    assert r.request()[0] == 200  # success path
    r.up.set_behavior(lambda h, q: (h.send_response(302), h.send_header("Location", "http://127.0.0.1:9/x"),
                                    h.send_header("Content-Length", "0"), h.end_headers()))
    assert r.request()[0] == 502  # refusal after the response arrived
    r.cfg.upstream = UpstreamConfig(host="other.example.test", port=r.up.port, ca_file=str(r.certs["good"][0]),
                                    allow_private_addresses=True, resolver=lambda h, p: ["127.0.0.1"])
    assert r.request()[0] == 502  # certificate for the wrong name: the handshake fails after the TCP connect
    assert len(tls) == 2 and len(raw) == 3
    assert all(s.fileno() == -1 for s in tls), "a TLS socket to the upstream was left open"
    assert all(s.fileno() == -1 for s in raw), "a raw socket to the upstream was left open"


def test_upstream_socket_is_closed_even_when_the_response_body_was_not_fully_read(rig, monkeypatch):
    r = rig(max_response_bytes=2000)
    raw, tls = _track_client_sockets(monkeypatch, r.up.port)
    big = [b"data: " + b"x" * 1500 + b"\n\n"] * 20
    r.up.set_behavior(lambda h, q: stream(h, big))
    r.request()
    assert r.ledger()[-1]["outcome"] == "response_too_large"  # the gateway stopped reading mid-body
    assert len(tls) == 1 and tls[0].fileno() == -1, "the TLS socket stayed open behind the unread response"

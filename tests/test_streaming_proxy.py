"""Streaming (SSE) through the PRODUCTION proxy and gateway: ``aiep.egress_proxy.ProxyServer`` and ``aiep.gateway.Gateway`` run unmodified, over real TLS,
against a local fake upstream. No Docker, no real network, no key. The only test doubles are the upstream's behaviour and (where noted) a wrapper
that records or severs the sockets the real tunnel is using.
"""
import json
import socket
import threading
import time
import types
from decimal import Decimal

import pytest

from aiep import egress_proxy as P
from aiep import gateway as G
from aiep import ledger as L
from aiep.gateway import Gateway
from aiep.m2_run import summarize_ledger
from fake_upstream import sse_events, stream
from test_egress_proxy import HOST, KEY, MODEL, PROMPT, TOKEN, certs, rig, stack  # noqa: F401 (fixtures and constants)

pytestmark = pytest.mark.skipif(not __import__("shutil").which("openssl"), reason="openssl CLI needed for test certificates")

STREAM_COST = Decimal("0.0061")  # 1000 in, 5000 cache read, 400 cache write (1h rate), 200 out at the test pricing


def stream_body(max_tokens=1024):
    return json.dumps({"model": MODEL, "max_tokens": max_tokens, "stream": True, "messages": [{"role": "user", "content": PROMPT}]}).encode()


@pytest.fixture(autouse=True)
def no_direct_upstream(monkeypatch):
    """Every test here: the gateway may only ever dial the proxy. A direct resolution or connection to the upstream fails the test."""
    monkeypatch.setattr(Gateway, "_pick_address", lambda self: (_ for _ in ()).throw(AssertionError("direct resolution attempted")))
    monkeypatch.setattr(G, "_PinnedTLSConnection", lambda *a, **k: (_ for _ in ()).throw(AssertionError("direct connection attempted")))
    dialled = []
    real = socket.create_connection
    shim = types.SimpleNamespace(**{k: getattr(socket, k) for k in dir(socket) if not k.startswith("__")})
    shim.create_connection = lambda addr, *a, **k: (dialled.append(addr), real(addr, *a, **k))[1]
    monkeypatch.setattr(G, "socket", shim)
    return dialled


class Client:
    """A raw HTTP client of the gateway that can stop reading or hang up part-way through a response."""

    def __init__(self, gw, body=None):
        body = body or stream_body()
        self.s = socket.create_connection(("127.0.0.1", gw.port), timeout=15)
        self.s.sendall((f"POST /v1/messages HTTP/1.1\r\nHost: gateway.test:{gw.port}\r\nAuthorization: Bearer {TOKEN}\r\n"
                        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body)
        self.buf = b""

    def read_until(self, needle):
        while needle not in self.buf:
            c = self.s.recv(65536)
            if not c:
                break
            self.buf += c
        return needle in self.buf

    def read_all(self):
        while True:
            c = self.s.recv(65536)
            if not c:
                break
            self.buf += c
        head, _, rest = self.buf.partition(b"\r\n\r\n")
        return int(head.split(b" ")[1]), head, rest

    def close(self):
        self.s.close()


def ledger_bytes(gw):
    return gw.ledger_path.read_bytes() if gw.ledger_path.exists() else b""


def wait_for(cond, timeout=6.0):
    t0 = time.monotonic()
    while not cond() and time.monotonic() - t0 < timeout:
        time.sleep(0.02)
    return cond()


def finishing(gw):
    return [e for e in (json.loads(l) for l in ledger_bytes(gw).decode().splitlines() if l.strip()) if e.get("outcome") != "forwarding"]


def chunked(h, data: bytes):
    h.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
    h.wfile.flush()


def sse_headers(h):
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Transfer-Encoding", "chunked")
    h.end_headers()


# ------------------------------------------------------------------ the whole stream
def test_a_multi_event_stream_passes_through_the_proxy_byte_for_byte_and_is_metered_exactly(stack, no_direct_upstream):
    up, px, gw = stack()
    events = sse_events()  # message_start, ping, content_block_delta, message_delta (usage), message_stop
    up.set_behavior(lambda h, q: stream(h, events))
    c = Client(gw)
    status, head, body = c.read_all()
    c.close()
    assert status == 200 and b"text/event-stream" in head.lower() and body == b"".join(events)
    assert wait_for(lambda: finishing(gw))
    e = finishing(gw)[-1]
    assert e["outcome"] == "ok" and e["stream"] is True and e["usage"]["output_tokens"] == 200 and e["usage"]["cache_read_tokens"] == 5000
    assert e["cost_usd_computed"] == str(STREAM_COST) and "usage_incomplete_upper_bound" not in e
    audit = L.audit(ledger_bytes(gw))
    assert audit["complete"] and audit["forwarded"] == 1 and audit["finished"] == 1
    s = summarize_ledger(ledger_bytes(gw))
    assert s["exact"] is True and s["unresolved_requests"] == 0 and s["tokens"]["output_tokens"] == 200
    px.wait_events(1)
    ev = px.server.events[0]
    assert ev["outcome"] == "tunnel" and ev["bytes_down"] >= len(body) and ev["reason"] == "closed"
    assert set(no_direct_upstream) == {("127.0.0.1", px.port)}  # the only thing the gateway ever dialled
    assert len(up.requests) == 1 and up.requests[0]["headers"]["x-api-key"] == KEY


@pytest.mark.parametrize("piece", [1, 2, 3, 7, 64, 1000])
def test_fragmented_network_reads_are_reassembled_and_metered_identically(stack, piece):
    up, px, gw = stack()
    events = sse_events()
    blob = b"".join(events)

    def behave(h, q):
        sse_headers(h)
        for i in range(0, len(blob), piece):  # events are cut mid-line, mid-JSON and mid-"\n\n" across chunks and TLS records
            chunked(h, blob[i:i + piece])
            if piece < 8:
                time.sleep(0.0003)
        h.wfile.write(b"0\r\n\r\n")
    up.set_behavior(behave)
    c = Client(gw)
    status, _, body = c.read_all()
    c.close()
    assert status == 200 and body == blob
    assert wait_for(lambda: finishing(gw))
    e = finishing(gw)[-1]
    assert e["cost_usd_computed"] == str(STREAM_COST) and e["usage"]["output_tokens"] == 200 and "usage_incomplete_upper_bound" not in e
    assert L.audit(ledger_bytes(gw))["complete"]


def test_crlf_framed_events_and_a_final_event_without_a_trailing_blank_line_are_handled(stack):
    up, px, gw = stack()
    events = [e.replace(b"\n", b"\r\n") for e in sse_events()]

    def behave(h, q):
        sse_headers(h)
        for ev in events:
            chunked(h, ev)
        h.wfile.write(b"0\r\n\r\n")
    up.set_behavior(behave)
    c = Client(gw)
    assert c.read_all()[0] == 200
    c.close()
    assert wait_for(lambda: finishing(gw))
    assert finishing(gw)[-1]["cost_usd_computed"] == str(STREAM_COST)


def test_a_stream_is_not_buffered_by_the_proxy_the_first_event_arrives_before_the_last_is_sent(stack):
    up, px, gw = stack()
    gate = threading.Event()
    events = sse_events()
    up.set_behavior(lambda h, q: stream(h, events, between=lambda i: gate.wait(8) if i == 0 else None))
    c = Client(gw)
    assert c.read_until(b"message_start")  # would time out if the proxy or the gateway held bytes back
    assert b"message_stop" not in c.buf
    gate.set()
    c.read_all()
    c.close()
    assert wait_for(lambda: finishing(gw))


# ------------------------------------------------------------------ failures in the middle of a stream
def truncated_after(n_events):
    events = sse_events()

    def behave(h, q):
        sse_headers(h)
        for ev in events[:n_events]:
            chunked(h, ev)
        h.close_connection = True  # return without the terminating chunk: the TLS connection simply ends
    return behave


def test_an_upstream_that_disconnects_mid_stream_is_charged_at_the_max_tokens_upper_bound_and_is_never_exact(stack):
    up, px, gw = stack()
    up.set_behavior(truncated_after(3))  # message_start, ping, one content delta: no usage delta, no message_stop
    c = Client(gw, stream_body(max_tokens=1024))
    c.read_all()
    c.close()
    assert wait_for(lambda: finishing(gw))
    e = finishing(gw)[-1]
    assert e["outcome"] == "upstream_read_error" and e["usage_incomplete_upper_bound"] is True and e["usage"]["output_tokens"] == 1024
    assert Decimal(e["cost_usd_computed"]) > STREAM_COST  # input as reported at message_start, output at the cap
    assert L.audit(ledger_bytes(gw))["complete"] is True  # the request IS resolved in the ledger ...
    s = summarize_ledger(ledger_bytes(gw))
    assert s["exact"] is False and s["entries_charged_at_max_tokens_upper_bound"] == 1  # ... but its figures are an upper bound, never exact
    assert gw.cfg.ledger.spent == Decimal(e["cost_usd_computed"])


def test_an_upstream_that_dies_before_any_usage_is_known_stops_the_gateway_fail_closed(stack):
    up, px, gw = stack()

    def behave(h, q):
        sse_headers(h)
        h.close_connection = True  # headers only, no event at all
    up.set_behavior(behave)
    c = Client(gw)
    c.read_all()
    c.close()
    assert wait_for(lambda: gw.gw.stopped)
    assert gw.gw.stopped == "metering_failure" and finishing(gw)[-1]["metering"] == "failed"


def test_a_client_that_hangs_up_mid_stream_does_not_lose_the_usage_the_gateway_keeps_reading_through_the_proxy(stack):
    up, px, gw = stack()
    gate = threading.Event()
    events = sse_events()

    def behave(h, q):
        sse_headers(h)
        chunked(h, events[0])
        gate.wait(8)  # held until the client has gone
        for ev in events[1:]:
            chunked(h, ev)
        h.wfile.write(b"0\r\n\r\n")
    up.set_behavior(behave)
    c = Client(gw)
    assert c.read_until(b"message_start")
    c.close()  # the client is gone while the upstream is still mid-stream
    time.sleep(0.2)
    gate.set()
    assert wait_for(lambda: finishing(gw))
    e = finishing(gw)[-1]
    assert e["outcome"] == "client_disconnected" and e["cost_usd_computed"] == str(STREAM_COST) and "usage_incomplete_upper_bound" not in e
    assert L.audit(ledger_bytes(gw))["complete"] and summarize_ledger(ledger_bytes(gw))["exact"] is True
    assert px.wait_events(1)[0]["bytes_down"] > 0


def test_a_gateway_killed_during_a_stream_leaves_a_forwarding_line_that_the_audit_reports_as_incomplete(stack):
    up, px, gw = stack()
    gate = threading.Event()
    events = sse_events()

    def behave(h, q):
        sse_headers(h)
        chunked(h, events[0])
        gate.wait(8)
        for ev in events[1:]:
            chunked(h, ev)
        h.wfile.write(b"0\r\n\r\n")
    up.set_behavior(behave)
    c = Client(gw)
    assert c.read_until(b"message_start")
    snapshot = ledger_bytes(gw)  # what a SIGKILLed gateway would have left on disk at this instant (the line is fsynced BEFORE the upstream call)
    audit = L.audit(snapshot)
    assert audit["complete"] is False and len(audit["unresolved"]) == 1 and audit["unresolved"][0]["requested_max_tokens"] == 1024
    s = summarize_ledger(snapshot)
    assert s["exact"] is False and s["unresolved_requests"] == 1 and s["requests"] == 0  # nothing finished, one request possibly billed
    gate.set()
    c.read_all()
    c.close()
    assert wait_for(lambda: L.audit(ledger_bytes(gw))["complete"])  # and once the stream really finishes, the same ledger is complete


def test_the_proxy_disappearing_mid_stream_ends_the_request_with_an_upper_bound_and_no_direct_fallback(stack, monkeypatch, no_direct_upstream):
    up, px, gw = stack()
    tunnels = []
    real = P._tunnel
    monkeypatch.setattr(P, "_tunnel", lambda a, b, cfg: (tunnels.append((a, b)), real(a, b, cfg))[1])
    gate = threading.Event()
    events = sse_events()

    def behave(h, q):
        sse_headers(h)
        chunked(h, events[0])
        chunked(h, events[1])
        gate.wait(8)
    up.set_behavior(behave)
    c = Client(gw, stream_body(max_tokens=2048))
    assert c.read_until(b"message_start") and wait_for(lambda: tunnels)
    a, b = tunnels[0]
    for s in (a, b):  # the proxy process is gone: both of its sockets die, exactly as they would when its container is killed
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    c.read_all()
    c.close()
    assert wait_for(lambda: finishing(gw))
    e = finishing(gw)[-1]
    assert e["outcome"] == "upstream_read_error" and e["usage_incomplete_upper_bound"] is True and e["usage"]["output_tokens"] == 2048
    gate.set()
    # and the proxy is now really gone: the next request fails closed, the upstream sees nothing, and nothing but the proxy address is ever dialled
    px.close()
    n = len(up.requests)
    c2 = Client(gw)
    status, _, body = c2.read_all()
    c2.close()
    assert status == 502 and b"connection_failed" in body and len(up.requests) == n
    assert set(no_direct_upstream) == {("127.0.0.1", px.port)}


def test_the_proxys_tunnel_limits_end_a_stream_and_the_gateway_still_accounts_for_it(stack):
    up, px, gw = stack(max_tunnel_bytes=60_000)  # the proxy cuts the tunnel after 60 kB in total (the TLS handshake and request use ~5 kB)
    big = b"".join(sse_events()[:1]) + b"event: ping\ndata: " + b"x" * 400_000 + b"\n\n"

    def behave(h, q):
        sse_headers(h)
        chunked(h, big)
        chunked(h, b"".join(sse_events()[1:]))
        h.wfile.write(b"0\r\n\r\n")
    up.set_behavior(behave)
    c = Client(gw)
    c.read_all()
    c.close()
    assert wait_for(lambda: finishing(gw))
    e = finishing(gw)[-1]
    assert e["usage_incomplete_upper_bound"] is True  # cut off before message_stop: charged at the cap, flagged
    assert px.wait_events(1)[0]["reason"] == "max_bytes"

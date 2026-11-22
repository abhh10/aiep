"""Metering gateway between the agent container and the Anthropic Messages API.

What it is: a minimal, stdlib-only HTTP server that holds the real API key,
authenticates the agent with a per-run bearer token, forwards exactly two
endpoints to one hardcoded upstream host over verified TLS, meters token usage
from responses, enforces a spend budget, and pins one model.

What it is not: a general proxy. There is no CONNECT, no absolute-URI or
caller-chosen host, no redirect following, no other paths or methods.

NETWORK ENFORCEMENT: the gateway container is attached ONLY to internal Docker networks (the agent network and the proxy network), which have
no default route, so it cannot open a connection to the internet, the LAN or the macOS host by itself. Its one upstream, ``api.anthropic.com:443``,
is reached through a separate restricted CONNECT proxy container (``aiep.egress_proxy``) that is the only member of the egress network. The code
controls below are therefore defence in depth, not the boundary. If the proxy cannot be reached or refuses, the request FAILS (502): there is
no direct fallback, and ``main`` refuses to start without a proxy address.

Controls (each has tests):
* Host header must be one of an explicit allowlist (defeats DNS rebinding and Host
  manipulation); exactly one Host header; then bearer-token auth.
* Request target must be origin-form and exactly ``/v1/messages`` or
  ``/v1/messages/count_tokens`` (query empty or ``beta=true`` only).
* POST only; ``Content-Length`` required and capped; chunked requests refused.
* Upstream host/port are constants of ``UpstreamConfig``. In production the gateway opens a TCP connection to the proxy, asks it for
  ``CONNECT <upstream host>:443`` (the proxy resolves the name, validates every address as public and pins one), requires a ``200``, and then
  performs the TLS handshake ITSELF through the tunnel with certificate and hostname verification (``ssl`` default context, CERT_REQUIRED,
  check_hostname). The proxy never sees plaintext. Without a proxy (tests only) the name is resolved here, every address must be public, and the
  connection is made to that exact address.
* Upstream 3xx is never followed and never passed on.
* Response size is capped.
* Requests are inspected, never rewritten: a body naming any model other than the
  pinned one, or declaring a non-custom (server-side) tool, or ``mcp_servers``,
  is refused and the gateway **stops** (every later request is refused) so the
  orchestrator can end the run. Bodies are forwarded byte-for-byte.
* Spend is computed from measured usage; when the budget is reached the gateway
  refuses further requests. Usage is only known after a response, so overshoot is
  bounded by ``max_concurrent`` requests (default 2), not by one: at most that many
  requests are in flight upstream (the rest queue, then are re-checked against the
  budget before being sent), each limited by the request-size and ``max_tokens`` caps.
* If a successful response cannot be metered, the gateway stops (fail closed).
* Prompts, responses, the upstream key and the run token are never logged.
"""

from __future__ import annotations

import hmac
import http.client
import ipaddress
import json
import os
import socket
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .ledger import Ledger
from .listen import default_route_interfaces, parse_listen  # noqa: F401  (re-exported: the listen rules live in aiep.listen)
from .pricing import Pricing, Usage, compute_cost, load_pricing, usage_from_api

MESSAGES_PATH = "/v1/messages"
COUNT_TOKENS_PATH = "/v1/messages/count_tokens"
ALLOWED_PATHS = frozenset({MESSAGES_PATH, COUNT_TOKENS_PATH})
ALLOWED_QUERIES = frozenset({"", "beta=true"})

DEFAULT_UPSTREAM_HOST = "api.anthropic.com"
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_TOKENS_CAP = 64_000
MAX_SSE_EVENT_BYTES = 1024 * 1024
DRAIN_LIMIT_BYTES = 1024 * 1024
UNAUTH_LEDGER_CAP = 100

_FORWARD_REQUEST_EXACT = {"content-type", "accept", "user-agent", "x-app"}
_FORWARD_REQUEST_PREFIX = ("anthropic-", "x-stainless-")
_FORWARD_RESPONSE_EXACT = {"content-type", "retry-after", "x-should-retry", "request-id", "x-request-id", "content-length"}
_FORWARD_RESPONSE_PREFIX = ("anthropic-ratelimit-",)  # anthropic-organization-id is deliberately not forwarded


class UpstreamError(Exception):
    """Could not complete the upstream hop (generic message only; no secrets)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class UpstreamConfig:
    """Where the gateway may connect. Production uses the defaults and nothing else.

    ``ca_file``, ``allow_private_addresses``, ``resolver`` and ``port`` exist so tests can
    use a loopback fake server; the production entry point (``main``) never sets them.
    """

    host: str = DEFAULT_UPSTREAM_HOST
    port: int = 443
    ca_file: str | None = None
    allow_private_addresses: bool = False
    resolver: Callable[[str, int], list[str]] | None = None
    connect_timeout: float = 10.0
    read_timeout: float = 600.0
    proxy_host: str | None = None  # the restricted egress proxy (production: always set; ``main`` refuses to start without it)
    require_proxy: bool = False  # production sets True: ``open_upstream`` then refuses to build a direct connection even if proxy_host were lost
    proxy_port: int = 3128


@dataclass
class GatewayConfig:
    run_token: str = field(repr=False)
    upstream_key: str = field(repr=False)
    model: str
    pricing: Pricing
    ledger: Ledger
    allowed_hosts: frozenset[str]
    budget_usd: Decimal
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)
    max_request_bytes: int = MAX_REQUEST_BYTES
    max_response_bytes: int = MAX_RESPONSE_BYTES
    max_tokens_cap: int = MAX_TOKENS_CAP
    stop_file: Path | None = None  # on stop, the reason is written here so the orchestrator can read it
    max_concurrent: int = 2  # upstream requests in flight; also the budget-overshoot bound
    queue_timeout_s: float = 30.0


def default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({i[4][0] for i in infos}, key=lambda a: (":" in a, a))  # IPv4 first


_V6_GLOBAL_UNICAST = ipaddress.ip_network("2000::/3")
# Inside 2000::/3 but able to embed or tunnel an arbitrary IPv4 address, or not routable.
_V6_EXCLUDED = [ipaddress.ip_network(n) for n in ("2002::/16", "2001::/32", "2001:db8::/32")]
_V4_EXCLUDED = [ipaddress.ip_network("192.0.0.0/24")]  # IETF protocol assignments (incl. 192.0.0.8)


def address_is_public(addr: str) -> bool:
    """True only for ordinary public unicast addresses. Strict on purpose: when in doubt, refuse.

    IPv6 must be global unicast (2000::/3), which excludes NAT64 (64:ff9b::/96), loopback,
    link-local and unique-local; 6to4 and Teredo are excluded explicitly because they can
    embed a private IPv4 address. IPv4-mapped IPv6 is judged by its IPv4 address.
    """
    ip = ipaddress.ip_address(addr)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_multicast or not ip.is_global:
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        return not any(ip in n for n in _V4_EXCLUDED)
    return ip in _V6_GLOBAL_UNICAST and not any(ip in n for n in _V6_EXCLUDED)


class _PinnedTLSConnection(http.client.HTTPSConnection):
    """HTTPS to a fixed, pre-validated IP, verifying the certificate against the hostname (test/no-proxy path only)."""

    def __init__(self, host: str, port: int, ip: str, context: ssl.SSLContext,
                 read_timeout: float, connect_timeout: float):
        super().__init__(host, port, timeout=read_timeout, context=context)
        self._ip = ip
        self._connect_timeout = connect_timeout

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self._connect_timeout)
        sock.settimeout(self.timeout)  # after connecting, reads may wait up to read_timeout
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()  # a failed handshake (e.g. certificate verification) must not leak the raw socket
            raise


MAX_PROXY_REPLY_BYTES = 4096


def _proxy_tunnel(proxy_ip: str, proxy_port: int, host: str, port: int, connect_timeout: float) -> socket.socket:
    """TCP to the proxy, ``CONNECT host:port``, and a ``200`` or ``UpstreamError``. The returned socket is a raw tunnel to the upstream.

    Nothing else is attempted if this fails: no direct connection, no retry through another route.
    """
    sock = socket.create_connection((proxy_ip, proxy_port), connect_timeout)
    try:
        sock.settimeout(connect_timeout)
        sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            if len(buf) > MAX_PROXY_REPLY_BYTES:
                raise UpstreamError("proxy_reply_too_large")
            chunk = sock.recv(1024)
            if not chunk:
                raise UpstreamError("proxy_closed_without_reply")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].split(b" ")
        if len(status) < 2 or status[0] not in (b"HTTP/1.1", b"HTTP/1.0") or status[1] != b"200":
            raise UpstreamError("proxy_refused")
        if rest:  # the upstream speaks only after our ClientHello, so nothing may arrive first
            raise UpstreamError("proxy_unexpected_data")
        return sock
    except BaseException:
        sock.close()
        raise


class _ProxiedTLSConnection(http.client.HTTPSConnection):
    """HTTPS to ``host`` through the restricted proxy. TLS is performed here, end to end, with certificate and hostname verification."""

    def __init__(self, host: str, port: int, proxy_ip: str, proxy_port: int, context: ssl.SSLContext,
                 read_timeout: float, connect_timeout: float):
        super().__init__(host, port, timeout=read_timeout, context=context)
        self._proxy = (proxy_ip, proxy_port)
        self._connect_timeout = connect_timeout

    def connect(self):
        sock = _proxy_tunnel(*self._proxy, self.host, self.port, self._connect_timeout)
        sock.settimeout(self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class Gateway:
    def __init__(self, config: GatewayConfig):
        self.cfg = config
        self._lock = threading.Lock()
        self._stopped: str | None = None
        self.requests_forwarded = 0
        self.unauthenticated_refusals = 0  # counted always, written to the ledger only up to a cap
        self.internal_errors = 0
        self.stop_file_errors = 0
        self._rid = 0
        self.slots = threading.BoundedSemaphore(config.max_concurrent)

    def next_request_id(self) -> str:
        """A per-run request id. A ``forwarding`` ledger line and the line that finishes the request carry the same id."""
        with self._lock:
            self._rid += 1
            return f"r{self._rid}"

    # ---- stop state ----
    @property
    def stopped(self) -> str | None:
        """None while running; else the reason the gateway stopped (terminal)."""
        with self._lock:
            return self._stopped

    def stop(self, reason: str) -> None:
        with self._lock:
            if self._stopped is not None:
                return
            self._stopped = reason
        # Best effort and outside the lock: the orchestrator polls this file (the gateway runs in a
        # container, so it cannot be asked directly). A failure to write must never un-stop the gateway.
        path = self.cfg.stop_file
        if path is not None:
            try:
                tmp = Path(f"{path}.tmp")
                tmp.write_text(reason)
                tmp.replace(path)  # atomic: a reader sees the whole reason or nothing
            except OSError:
                with self._lock:
                    self.stop_file_errors += 1

    # ---- upstream ----
    def _ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(cafile=self.cfg.upstream.ca_file)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    def _pick_address(self) -> str:
        up = self.cfg.upstream
        resolver = up.resolver or default_resolver
        try:
            addrs = list(resolver(up.host, up.port))
        except OSError:
            raise UpstreamError("dns_failure") from None
        if not addrs:
            raise UpstreamError("dns_failure")
        try:
            public = all(address_is_public(a) for a in addrs)
        except ValueError:
            raise UpstreamError("resolver_returned_a_non_address") from None
        if not up.allow_private_addresses and not public:
            raise UpstreamError("upstream_resolves_to_non_public_address")
        if up.allow_private_addresses:
            for a in addrs:
                ipaddress.ip_address(a)  # still must be a literal address (ValueError surfaces as 502 below)
        return addrs[0]

    def _proxy_address(self) -> str:
        """The proxy's address, resolved from its configured name. It must be a literal address; there is no other route to fall back to."""
        up = self.cfg.upstream
        try:
            addrs = list(socket.getaddrinfo(up.proxy_host, up.proxy_port, family=socket.AF_INET, type=socket.SOCK_STREAM))
        except OSError:
            raise UpstreamError("proxy_dns_failure") from None
        if not addrs:
            raise UpstreamError("proxy_dns_failure")
        return addrs[0][4][0]

    def open_upstream(self) -> http.client.HTTPSConnection:
        up = self.cfg.upstream
        if up.require_proxy and not up.proxy_host:
            raise UpstreamError("proxy_required")  # never a silent direct connection in production
        if up.proxy_host:  # production: the only way out is the proxy; the gateway does not resolve or dial the upstream itself
            return _ProxiedTLSConnection(up.host, up.port, self._proxy_address(), up.proxy_port, self._ssl_context(),
                                         up.read_timeout, up.connect_timeout)
        ip = self._pick_address()
        return _PinnedTLSConnection(up.host, up.port, ip, self._ssl_context(), up.read_timeout, up.connect_timeout)

    # ---- server ----
    def make_server(self, bind_host: str, port: int) -> ThreadingHTTPServer:
        handler = _make_handler(self)
        server = _QuietServer((bind_host, port), handler)
        server.daemon_threads = True
        return server


class _QuietServer(ThreadingHTTPServer):
    request_queue_size = 128  # the default (5) resets connections when a client opens a burst
    daemon_threads = True

    def handle_error(self, request, client_address):  # type name only; never a traceback or data
        print(f"gateway: handler error ({type(sys.exc_info()[1]).__name__})", file=sys.stderr)


# ---------------------------------------------------------------------------------------
class _SSEUsage:
    """Incrementally read usage out of an SSE stream; the stream itself is forwarded untouched."""

    def __init__(self):
        self.buf = b""
        self.fields: dict[str, Any] = {}
        self.saw_start = False
        self.saw_stop = False
        self.broken = False

    def feed(self, chunk: bytes) -> None:
        if self.broken:
            return
        self.buf += chunk
        while True:
            cut = [i for i in (self.buf.find(b"\n\n"), self.buf.find(b"\r\n\r\n")) if i >= 0]
            if not cut:
                break
            i = min(cut)
            raw, self.buf = self.buf[:i], self.buf[i:].lstrip(b"\r\n")
            self._event(raw)
        if len(self.buf) > MAX_SSE_EVENT_BYTES:
            self.broken = True

    def _event(self, raw: bytes) -> None:
        for line in raw.splitlines():
            if not line.startswith(b"data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except ValueError:
                continue
            kind = ev.get("type") if isinstance(ev, dict) else None
            if kind == "message_start":
                self.saw_start = True
                usage = (ev.get("message") or {}).get("usage")
                if isinstance(usage, dict):
                    self.fields.update({k: v for k, v in usage.items() if v is not None})
            elif kind == "message_delta":
                usage = ev.get("usage")
                if isinstance(usage, dict):
                    self.fields.update({k: v for k, v in usage.items() if v is not None})
            elif kind == "message_stop":
                self.saw_stop = True


def _error_body(kind: str, message: str) -> bytes:
    return json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()


def _make_handler(gw: Gateway):
    cfg = gw.cfg

    class Handler(BaseHTTPRequestHandler):
        timeout = 120
        protocol_version = "HTTP/1.0"  # close-delimited bodies: nothing buffers a stream
        server_version = "aiep-gateway"
        sys_version = ""

        def log_message(self, *a, **k):  # never log request lines
            pass

        # -- helpers --
        def _drain(self) -> None:
            """Read (a bounded amount of) the unread request body before answering.

            Closing a socket with unread inbound data makes the OS send a reset, and the client
            may then see a connection error instead of our status code (and retry). Bounded to
            1 MiB and 1 s so this cannot be used to hold a thread.
            """
            if self._body_consumed:
                return
            self._body_consumed = True
            try:
                remaining = int((self.headers.get_all("Content-Length") or ["0"])[0].strip())
            except ValueError:
                return
            remaining = min(max(remaining, 0), DRAIN_LIMIT_BYTES)
            old = self.connection.gettimeout()
            try:
                self.connection.settimeout(1.0)
                while remaining > 0:
                    chunk = self.rfile.read1(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except OSError:
                pass
            finally:
                try:
                    self.connection.settimeout(old)
                except OSError:
                    pass

        def _send(self, status: int, body: bytes, extra: dict[str, str] | None = None) -> None:
            self._drain()
            self._responded = True
            headers = {"Content-Type": "application/json", "Content-Length": str(len(body)),
                       "Connection": "close", "x-should-retry": "false", **(extra or {})}
            self.send_response_only(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            self.close_connection = True

        def _refuse(self, status: int, kind: str, reason: str, stop: str | None = None,
                    headers: dict[str, str] | None = None, **entry) -> None:
            if stop:
                gw.stop(stop)
            entry = {**self._req_meta, **entry}  # requested max_tokens / stream (when the body was parsed) and the request id
            if status in (400, 401, 421) and not self._authenticated:
                with gw._lock:
                    gw.unauthenticated_refusals += 1
                    quiet = gw.unauthenticated_refusals > UNAUTH_LEDGER_CAP
                if not quiet:
                    cfg.ledger.record({"outcome": "refused", "reason": reason, "status": status,
                                       "method": self.command, "authenticated": False})
            else:
                cfg.ledger.record({"outcome": "refused", "reason": reason, "status": status,
                                   "method": self.command, **entry})
            self._send(status, _error_body(kind, reason), headers)

        # -- every method funnels here --
        def _handle(self) -> None:
            t0 = time.monotonic()
            self._authenticated = False
            # 1. Origin-form target only (rejects absolute-URI, authority-form/CONNECT, '*').
            target = self.path
            if not target.startswith("/") or target.startswith("//"):
                return self._refuse(400, "invalid_request_error", "request target must be origin-form")
            # 2. Host: exactly one, on the allowlist (DNS rebinding / Host manipulation).
            hosts = self.headers.get_all("Host") or []
            if len(hosts) != 1 or hosts[0].strip().lower() not in cfg.allowed_hosts:
                return self._refuse(421, "invalid_request_error", "host not allowed")
            # 3. Authentication (constant time). Bearer or x-api-key, both carry the run token only.
            presented = ""
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                presented = auth[7:].strip()
            elif self.headers.get("x-api-key"):
                presented = self.headers.get("x-api-key", "").strip()
            if not presented or not hmac.compare_digest(presented.encode(), cfg.run_token.encode()):
                return self._refuse(401, "authentication_error", "invalid credentials")
            self._authenticated = True
            # 4. Stopped?
            if gw.stopped:
                return self._refuse(403, "permission_error", f"gateway stopped: {gw.stopped}")
            # 5. Method and path.
            if self.command != "POST":
                return self._refuse(405, "invalid_request_error", "method not allowed")
            parts = urlsplit(target)
            if (
                parts.path not in ALLOWED_PATHS
                or parts.query not in ALLOWED_QUERIES
                or parts.fragment
                or any(c in target for c in ("%", "\\", ";", "@", "\x00"))
                or ".." in parts.path
                or target != parts.path + (f"?{parts.query}" if parts.query else "")  # canonical form only
            ):
                return self._refuse(404, "not_found_error", "path not allowed")
            # 6. Body framing and size, before reading anything.
            if self.headers.get("Transfer-Encoding") is not None:
                return self._refuse(501, "invalid_request_error", "transfer-encoding not supported")
            lengths = self.headers.get_all("Content-Length") or []
            if (len(lengths) != 1 or not lengths[0].strip().isascii() or not lengths[0].strip().isdigit()
                    or len(lengths[0].strip()) > 18):
                return self._refuse(411, "invalid_request_error", "a single Content-Length is required")
            length = int(lengths[0])
            if length > cfg.max_request_bytes:
                return self._refuse(413, "request_too_large", "request body too large", bytes_in=length)
            if gw.cfg.ledger.spent >= cfg.budget_usd:
                return self._refuse(403, "permission_error", "budget exhausted", stop="budget_exhausted")
            try:
                body = self.rfile.read(length)
            except OSError:
                self._body_consumed = True
                return self._refuse(408, "invalid_request_error", "timed out reading request body")
            self._body_consumed = True
            if len(body) != length:
                return self._refuse(400, "invalid_request_error", "truncated request body")
            # 7. Inspect (never rewrite).
            try:
                doc = json.loads(body)
                if not isinstance(doc, dict):
                    raise ValueError
            except ValueError:
                return self._refuse(400, "invalid_request_error", "body must be a JSON object")
            self._req_meta = _request_meta(doc, cfg.max_tokens_cap)
            if doc.get("model") != cfg.model:
                seen = doc.get("model") if isinstance(doc.get("model"), str) and len(doc["model"]) <= 100 else "?"
                return self._refuse(403, "permission_error", "model not allowed", stop="model_violation",
                                    requested_model=seen)
            bad_tool = _bad_tools(doc)
            if bad_tool:
                return self._refuse(403, "permission_error", bad_tool, stop="tool_violation")
            mt = doc.get("max_tokens")
            if parts.path == MESSAGES_PATH and (
                isinstance(mt, bool) or not isinstance(mt, int) or mt < 1 or mt > cfg.max_tokens_cap
            ):
                return self._refuse(400, "invalid_request_error", "max_tokens missing or above the cap")
            # 8. Forward.
            self._forward(parts, body, mt if isinstance(mt, int) and not isinstance(mt, bool) else 0, t0)

        def _forward(self, parts, body: bytes, max_tokens: int, t0: float) -> None:
            if not gw.slots.acquire(timeout=cfg.queue_timeout_s):
                return self._refuse(429, "rate_limit_error", "gateway busy; retry",
                                    headers={"retry-after": "1", "x-should-retry": "true"})
            try:
                # While we queued, the gateway may have stopped or the budget may have been spent.
                if gw.stopped:
                    return self._refuse(403, "permission_error", f"gateway stopped: {gw.stopped}")
                if cfg.ledger.spent >= cfg.budget_usd:
                    return self._refuse(403, "permission_error", "budget exhausted", stop="budget_exhausted")
                self._forward_now(parts, body, max_tokens, t0)
            finally:
                gw.slots.release()

        def _forward_now(self, parts, body: bytes, max_tokens: int, t0: float) -> None:
            is_count = parts.path == COUNT_TOKENS_PATH
            headers = {"x-api-key": cfg.upstream_key, "Content-Length": str(len(body)),
                       "Accept-Encoding": "identity", "Connection": "close"}
            for name in self.headers.keys():
                low = name.lower()
                if low in _FORWARD_REQUEST_EXACT or low.startswith(_FORWARD_REQUEST_PREFIX):
                    headers[name] = self.headers[name]
            target = parts.path + (f"?{parts.query}" if parts.query else "")
            rid = gw.next_request_id()
            self._req_meta = {**self._req_meta, "rid": rid}
            entry = {"endpoint": parts.path, "model": cfg.model, "bytes_in": len(body), "method": "POST", **self._req_meta}
            # Written (and fsynced) BEFORE anything is sent upstream. The line that finishes this request carries the same ``rid``; a
            # ``forwarding`` line with no finishing line means a request that may have been billed upstream and was never metered here
            # (the gateway was killed, or is still serving it): the orchestrator drains these before stopping the gateway and records any left.
            cfg.ledger.record({**entry, "outcome": "forwarding"})
            conn = resp = None
            try:
                try:
                    conn = gw.open_upstream()
                    conn.request("POST", target, body=body, headers=headers)
                    resp = conn.getresponse()
                except UpstreamError as e:
                    return self._refuse(502, "api_error", f"upstream unavailable ({e.code})", **entry)
                except ssl.SSLError:
                    return self._refuse(502, "api_error", "upstream unavailable (tls_verification_failed)", **entry)
                except (OSError, http.client.HTTPException):
                    return self._refuse(502, "api_error", "upstream unavailable (connection_failed)", **entry)
                gw.requests_forwarded += 1
                self._relay(resp, is_count, max_tokens, entry, t0)
            finally:
                # The response holds a makefile() reference to the TLS socket, so closing only the
                # connection leaves the socket open until the response is closed as well. Close both,
                # on every path (success, refusal, and failure before or after the response arrived).
                if resp is not None:
                    resp.close()
                if conn is not None:
                    conn.close()

        def _relay(self, resp, is_count: bool, max_tokens: int, entry: dict, t0: float) -> None:
            status = resp.status
            if 300 <= status < 400:
                return self._refuse(502, "api_error", "upstream redirect refused", **entry)
            ctype = (resp.getheader("Content-Type") or "").lower()
            streaming = ctype.startswith("text/event-stream")
            declared = resp.getheader("Content-Length")
            if declared is not None and declared.isdigit() and int(declared) > cfg.max_response_bytes:
                return self._refuse(502, "api_error", "upstream response too large", **entry)
            self._responded = True
            self.send_response_only(status)
            for name, value in resp.getheaders():
                low = name.lower()
                if low in _FORWARD_RESPONSE_EXACT or low.startswith(_FORWARD_RESPONSE_PREFIX):
                    if streaming and low == "content-length":
                        continue
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            sse = _SSEUsage() if streaming else None
            plain = bytearray()
            total, outcome, client_gone = 0, "ok", False
            while True:
                try:
                    chunk = resp.read1(65536)
                except (OSError, http.client.HTTPException):
                    outcome = "upstream_read_error"
                    break
                if not chunk:
                    break
                total += len(chunk)
                if total > cfg.max_response_bytes:
                    outcome = "response_too_large"
                    break
                if sse is not None:
                    sse.feed(chunk)
                elif len(plain) < cfg.max_response_bytes:
                    plain += chunk
                if not client_gone:
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except OSError:
                        client_gone = True  # keep reading so usage can still be metered
            if client_gone and outcome == "ok":
                outcome = "client_disconnected"

            self._record(status, is_count, max_tokens, entry, t0, total, outcome, sse, bytes(plain))

        def _record(self, status, is_count, max_tokens, entry, t0, total, outcome, sse, plain) -> None:
            entry = {**entry, "status": status, "outcome": outcome, "bytes_out": total,
                     "duration_s": round(time.monotonic() - t0, 3)}
            usage: Usage | None = None
            incomplete = False
            if status == 200 and not is_count:
                try:
                    if sse is not None:
                        if sse.saw_start:
                            usage = usage_from_api(sse.fields)
                            incomplete = not sse.saw_stop
                    else:
                        doc = json.loads(plain)
                        if isinstance(doc.get("usage"), dict):
                            usage = usage_from_api(doc["usage"])
                except (ValueError, AttributeError, TypeError):
                    usage = None
                except Exception:  # PricingError on malformed usage values
                    usage = None
                if usage is None:
                    entry["metering"] = "failed"
                    gw.stop("metering_failure")
                    cfg.ledger.record(entry)
                    return
                if incomplete:
                    # Interrupted stream: output unknown, so assume the most the request allowed.
                    usage.output_tokens = max(usage.output_tokens, max_tokens)
                    entry["usage_incomplete_upper_bound"] = True
                cost = compute_cost(cfg.pricing, usage)
                entry.update(usage=usage.as_dict(), cost_upper_bound=cost.upper_bound)
                spent = cfg.ledger.record(entry, cost.usd)
                if spent >= cfg.budget_usd:
                    gw.stop("budget_exhausted")
                return
            cfg.ledger.record(entry, Decimal(0) if is_count else None)

        def _safe(self) -> None:
            self._responded = False
            self._body_consumed = False
            self._req_meta: dict = {}
            try:
                self._handle()
            except Exception as e:  # fail closed, say nothing about internals
                try:
                    with gw._lock:
                        gw.internal_errors += 1
                    cfg.ledger.record({"outcome": "internal_error", "error_type": type(e).__name__, **self._req_meta})
                    if not self._responded:
                        self._send(500, _error_body("api_error", "internal gateway error"))
                except Exception:
                    pass
                self.close_connection = True

        # Every HTTP method, including CONNECT, goes through the same checks.
        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_CONNECT = _safe

    return Handler


def _request_meta(doc: dict, cap: int) -> dict:
    """What the client asked for, recorded in every ledger line about the request (accepted or refused): the requested ``max_tokens``
    (the value if it is an int, else the name of its type, so a wrong type is visible too), the configured cap, and the stream mode."""
    mt = doc.get("max_tokens")
    stream = doc.get("stream")
    return {"requested_max_tokens": mt if isinstance(mt, int) and not isinstance(mt, bool) else type(mt).__name__,
            "max_tokens_cap": cap,
            "stream": stream if isinstance(stream, bool) else (None if stream is None else type(stream).__name__)}


def _bad_tools(doc: dict) -> str | None:
    """Reason to refuse if the request declares server-side tools or remote MCP servers."""
    if doc.get("mcp_servers"):
        return "mcp_servers not allowed"
    tools = doc.get("tools")
    if tools is None:
        return None
    if not isinstance(tools, list):
        return "tools must be a list"
    for t in tools:
        if not isinstance(t, dict):
            return "malformed tool entry"
        kind = t.get("type")
        if kind not in (None, "custom"):
            return "server-side tool types are not allowed"
    return None


def parse_max_concurrent(value: str | None) -> int:
    """``AIEP_MAX_CONCURRENT``: requests in flight upstream. Unset keeps the original default of 2; the M3 pilot sets 1 (one concurrent request per agent), which
    also bounds the budget overshoot to ONE request's upper bound. Anything but a plain integer 1..4 is refused."""
    if value is None or value == "":
        return 2
    if not (value.isascii() and value.isdigit() and 1 <= int(value) <= 4 and str(int(value)) == value):
        raise ValueError("AIEP_MAX_CONCURRENT must be an integer from 1 to 4")
    return int(value)


def production_upstream(host: str, proxy: str) -> UpstreamConfig:
    """``AIEP_UPSTREAM_PROXY`` is ``<name>:<port>``; the upstream host itself comes only from config/experiment.json."""
    name, _, port = proxy.rpartition(":")
    if not name or not port.isascii() or not port.isdigit() or not (0 < int(port) < 65536):
        raise ValueError("AIEP_UPSTREAM_PROXY must be <host>:<port>")
    return UpstreamConfig(host=host, proxy_host=name, proxy_port=int(port), require_proxy=True)


def main(argv: list[str] | None = None, *, upstream: UpstreamConfig | None = None) -> int:
    """Production entry point. Configuration comes only from the environment and the repo's config files.

    ``upstream`` is a TEST SEAM, a keyword-only Python argument that cannot be set from the environment or the command line: the runtime
    validation passes a local mock server's ``UpstreamConfig`` here. ``python -m aiep.gateway`` never passes it, so production always
    talks to the host in ``config/experiment.json`` with default TLS verification and public-address checks.
    """
    env = os.environ
    needed = ["AIEP_UPSTREAM_API_KEY", "AIEP_RUN_TOKEN", "AIEP_BUDGET_USD", "AIEP_ALLOWED_HOSTS", "AIEP_LISTEN", "AIEP_LEDGER_PATH"]
    if upstream is None:
        needed.append("AIEP_UPSTREAM_PROXY")  # production has no direct route: a missing proxy is a startup error, never a fallback
    missing = [k for k in needed if not env.get(k)]
    if missing:
        print(f"missing environment: {', '.join(missing)}", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[2]
    experiment = json.loads((root / "config" / "experiment.json").read_text())
    pricing = load_pricing(root / "config" / "model_pricing.json", experiment["model"])
    try:
        hosts, port = parse_listen(env["AIEP_LISTEN"])
    except (ValueError, OSError) as e:
        print(f"invalid AIEP_LISTEN: {e}", file=sys.stderr)
        return 2
    try:
        parse_max_concurrent(env.get("AIEP_MAX_CONCURRENT"))
    except ValueError as e:
        print(f"invalid AIEP_MAX_CONCURRENT: {e}", file=sys.stderr)
        return 2
    if upstream is None:
        try:
            upstream = production_upstream(experiment["upstream_host"], env["AIEP_UPSTREAM_PROXY"])
        except ValueError as e:
            print(f"invalid AIEP_UPSTREAM_PROXY: {e}", file=sys.stderr)
            return 2
    gateway = Gateway(GatewayConfig(
        run_token=env["AIEP_RUN_TOKEN"],
        upstream_key=env["AIEP_UPSTREAM_API_KEY"],
        model=experiment["model"],
        pricing=pricing,
        ledger=Ledger(Path(env["AIEP_LEDGER_PATH"])),
        stop_file=Path(env["AIEP_STOP_FILE"]) if env.get("AIEP_STOP_FILE") else None,
        allowed_hosts=frozenset(h.strip().lower() for h in env["AIEP_ALLOWED_HOSTS"].split(",") if h.strip()),
        budget_usd=Decimal(env["AIEP_BUDGET_USD"]),
        max_concurrent=parse_max_concurrent(env.get("AIEP_MAX_CONCURRENT")),
        upstream=upstream,
    ))
    servers = [gateway.make_server(h, port) for h in hosts]
    for extra in servers[:-1]:
        threading.Thread(target=extra.serve_forever, daemon=True).start()
    try:
        servers[-1].serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

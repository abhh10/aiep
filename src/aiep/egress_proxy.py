"""Restricted egress proxy: the ONLY container with a route to the internet, and it can open exactly one kind of connection.

Why it exists. The gateway holds the real Anthropic key, so it must not be able to reach the macOS host, the LAN, or arbitrary servers. Docker's
``internal`` networks have no default route, so a gateway attached only to internal networks has no way out at all; its one legitimate upstream,
``api.anthropic.com:443``, is reached through this proxy, which sits on the proxy network (internal, shared with the gateway) and on the egress
network (the only one with a route). The proxy holds no key and no other secret: it never sees plaintext, because it is a TCP tunnel.

What it accepts: ``CONNECT <host>:<port>`` where host and port equal the configured upstream EXACTLY (``config/experiment.json``'s
``upstream_host`` and 443), and nothing else. There is no other method, no other host, no other port, no IP literal, no absolute URI, no
plain-HTTP forwarding, no authentication scheme to guess, no pipelined bytes behind the request.

What it does for an accepted request:
 1. resolves the name ITSELF, once, with the system resolver;
 2. requires EVERY returned address to be a public unicast address (a mixed public/private answer is refused whole, which is also the
    DNS-rebinding defence: there is no second lookup and the client never chooses the address);
 3. connects to ONE of those validated addresses (IPv4 first) and checks that the connected peer is that address (the pin);
 4. answers ``200`` and copies bytes in both directions, never interpreting them: TLS is end to end between the gateway and Anthropic
    (certificate and hostname verification happen in the gateway; nothing here terminates, inspects or re-signs it).

Bounds: a few concurrent tunnels, a header-read deadline, a connect deadline, an idle timeout, a total-duration cap and a total-bytes cap.

SECURITY LIMIT (read this): like everything in a Docker Desktop VM, this container's own network namespace can still ROUTE to the VM's host
gateway and to the Mac (it has a default route via the egress network). Docker cannot stop that from inside the VM, and neither can this code at the
packet level. What this code enforces is that no client can make the proxy dial those addresses: the destination is fixed, and every resolved
address must be public. The proxy holds no secret, runs non-root with all capabilities dropped on a read-only root, and is reachable only from
the gateway's network; the residual risk (a bug in this ~300-line file) is stated in the project's report, not hidden.

Stdlib only (it is injected into a bare python image).
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import select
import socket
import socketserver
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .listen import parse_listen

DEFAULT_UPSTREAM_HOST = "api.anthropic.com"
DEFAULT_UPSTREAM_PORT = 443
DEFAULT_PROXY_PORT = 3128
MAX_HEAD_BYTES = 4096
MAX_HEADERS = 32
MAX_LOG_LINES = 10_000
MAX_THREAD_FACTOR = 8  # handler threads allowed per tunnel slot

_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_REQUEST_LINE = re.compile(r"^([A-Za-z]{1,16}) ([^ ]{1,300}) HTTP/1\.[01]$")
_HEADER_LINE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}:[ \t]?[\x20-\x7e\t]{0,1024}$")
_NUMERIC_HOST = re.compile(r"^(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+))*$", re.I)
_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH", re.I)
_SECRET_VALUE = re.compile(r"sk-ant-|sk-[A-Za-z0-9]{20,}")


class Refusal(Exception):
    """A request the proxy will not serve. ``status`` is the HTTP status sent; ``reason`` is a short fixed token (never client data)."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


# ---- address validation (kept byte-for-byte equivalent to aiep.gateway.address_is_public; a test asserts the two agree)
_V6_GLOBAL_UNICAST = ipaddress.ip_network("2000::/3")
_V6_EXCLUDED = [ipaddress.ip_network(n) for n in ("2002::/16", "2001::/32", "2001:db8::/32")]
_V4_EXCLUDED = [ipaddress.ip_network("192.0.0.0/24")]


def address_is_public(addr: str) -> bool:
    """True only for ordinary public unicast addresses (not private, loopback, link-local, multicast, reserved, CGNAT, 6to4, Teredo...)."""
    ip = ipaddress.ip_address(addr)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_multicast or not ip.is_global:
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        return not any(ip in n for n in _V4_EXCLUDED)
    return ip in _V6_GLOBAL_UNICAST and not any(ip in n for n in _V6_EXCLUDED)


def default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({i[4][0] for i in infos}, key=lambda a: (":" in a, a))  # IPv4 first


@dataclass(frozen=True)
class ProxyConfig:
    """The one destination and the bounds. Production uses ``production_config()`` and nothing else.

    ``allow_private_addresses`` and ``resolver`` are TEST SEAMS (a local mock upstream lives on a private address); ``main`` never sets them
    from the environment, only a Python caller can.
    """

    host: str = DEFAULT_UPSTREAM_HOST
    port: int = DEFAULT_UPSTREAM_PORT
    allow_private_addresses: bool = False
    resolver: Callable[[str, int], list[str]] | None = None
    header_timeout_s: float = 10.0
    connect_timeout_s: float = 10.0
    idle_timeout_s: float = 660.0  # above the client's 600 s per-request timeout
    max_tunnel_s: float = 3600.0
    max_tunnel_bytes: int = 128 * 1024 * 1024
    max_connections: int = 8
    dns_timeout_s: float = 5.0  # how long ONE request waits for the resolver (see ``BoundedResolver``)
    dns_max_inflight: int = 2  # resolver threads that may exist at once, including ones whose request already gave up
    half_close_grace_s: float = 30.0  # once ONE side has closed, the other direction may continue for at most this long (a dead client must not pin a slot)


# ---- request parsing and the allowlist (pure functions: unit-tested without sockets)
def parse_connect(head: bytes) -> tuple[str, int]:
    """The (host, port) of a well-formed ``CONNECT host:port HTTP/1.x`` head, or ``Refusal``. Says nothing about whether it is allowed."""
    try:
        text = head.decode("ascii")
    except UnicodeDecodeError:
        raise Refusal(400, "malformed_request") from None
    if "\r\n\r\n" not in text or not text.endswith("\r\n\r\n") or "\n" in text.replace("\r\n", ""):
        raise Refusal(400, "malformed_request")
    lines = text[:-4].split("\r\n")
    m = _REQUEST_LINE.fullmatch(lines[0])
    if not m:
        raise Refusal(400, "malformed_request")
    method, target = m.group(1), m.group(2)
    if method != "CONNECT":
        raise Refusal(405, "method_not_allowed")
    if len(lines) - 1 > MAX_HEADERS:
        raise Refusal(431, "too_many_headers")
    for line in lines[1:]:
        if not _HEADER_LINE.fullmatch(line):
            raise Refusal(400, "malformed_header")
        name, _, value = line.partition(":")
        low = name.lower()
        if low == "transfer-encoding" or (low == "content-length" and value.strip() not in ("", "0")):
            raise Refusal(400, "request_body_not_allowed")
    host, sep, port_text = target.rpartition(":")
    if not sep or not _HOST_RE.fullmatch(host) or not port_text.isascii() or not port_text.isdigit() or len(port_text) > 5 \
            or str(int(port_text)) != port_text or not (0 < int(port_text) < 65536):
        raise Refusal(400, "invalid_target")
    return host.lower(), int(port_text)


def is_ip_literal(host: str) -> bool:
    """IPv4/IPv6 literals and numeric spellings (``2130706433``, ``0x7f.1``, ``127.1``) that a resolver or a client might treat as an address."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return _NUMERIC_HOST.fullmatch(host) is not None


def authorize(host: str, port: int, cfg: ProxyConfig) -> None:
    """Raise ``Refusal`` unless (host, port) is exactly the configured upstream."""
    if is_ip_literal(host):
        raise Refusal(403, "ip_literal_refused")
    if port != cfg.port:
        raise Refusal(403, "port_not_allowed")
    if host != cfg.host:
        raise Refusal(403, "destination_not_allowed")


class BoundedResolver:
    """A resolver call with a deadline and a hard cap on stuck lookups, using only the platform resolver.

    ``getaddrinfo`` cannot be cancelled or given a timeout, and writing a DNS client of our own (raw UDP, ids, retries, TCP fallback) would be a new
    attack surface for a security component, so this does the one safe thing: each lookup runs in a daemon worker thread and the REQUEST waits at most
    ``timeout_s`` for it. A lookup that misses the deadline cannot be killed; it ends when the C library's own resolver timeout expires. Its result
    is then DISCARDED: it is never cached and never used for a later request, so a late answer cannot influence any connection (every request does
    its own lookup, validates every address and pins one: the rebinding guarantees are unchanged). The number of such threads can never grow: a
    semaphore of ``max_inflight`` is held by the thread, not the request, until the lookup really returns, so once that many are stuck further requests
    are refused at once (``dns_busy``) instead of starting another.
    """

    def __init__(self, resolver: Callable[[str, int], list[str]], timeout_s: float, max_inflight: int):
        self._resolver, self.timeout_s = resolver, timeout_s
        self._slots = threading.BoundedSemaphore(max_inflight)
        self._lock = threading.Lock()
        self.in_flight = 0  # worker threads currently inside the resolver (including ones nobody waits for any more)
        self.timeouts = 0
        self.busy = 0

    def __call__(self, host: str, port: int) -> list[str]:
        if not self._slots.acquire(blocking=False):
            with self._lock:
                self.busy += 1
            raise Refusal(503, "dns_busy")
        done = threading.Event()
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["ok"] = list(self._resolver(host, port))
            except BaseException as e:  # the worker never raises into the void; the waiting request re-raises what it needs
                box["err"] = e
            finally:
                with self._lock:
                    self.in_flight -= 1
                self._slots.release()
                done.set()

        with self._lock:
            self.in_flight += 1
        try:
            threading.Thread(target=work, daemon=True, name="aiep-dns").start()
        except BaseException:
            with self._lock:
                self.in_flight -= 1
            self._slots.release()
            raise
        if not done.wait(self.timeout_s):
            with self._lock:
                self.timeouts += 1
            raise Refusal(504, "dns_timeout")
        if "err" in box:
            err = box["err"]
            if isinstance(err, OSError):
                raise Refusal(502, "dns_failure") from None
            raise err
        return box["ok"]


def pick_pinned_address(cfg: ProxyConfig, resolve: Callable[[str, int], list[str]] | None = None) -> str:
    """Resolve the configured name ONCE and return the single validated address to connect to. Every answer must be acceptable.

    ``resolve`` is the server's ``BoundedResolver``; without it (unit tests of the validation rules) the configured or system resolver is called directly.
    """
    resolver = resolve or cfg.resolver or default_resolver
    try:
        addrs = list(resolver(cfg.host, cfg.port))
    except OSError:
        raise Refusal(502, "dns_failure") from None
    if not addrs:
        raise Refusal(502, "dns_failure")
    checked = []
    for a in addrs:
        try:
            if not isinstance(a, str) or "%" in a:  # an IPv6 scope id (fe80::1%eth0) is never a plain public unicast address
                raise ValueError(a)
            ipaddress.ip_address(a)
            public = address_is_public(a)
        except (ValueError, TypeError):
            raise Refusal(502, "resolver_returned_a_non_address") from None
        if not public and not cfg.allow_private_addresses:
            raise Refusal(403, "resolves_to_non_public_address")  # one bad address spoils the whole answer
        checked.append(a)
    checked.sort(key=lambda a: (":" in a, a))
    return checked[0]


def _same_ip(a: str, b: str) -> bool:
    try:
        return ipaddress.ip_address(a) == ipaddress.ip_address(b)
    except ValueError:
        return False


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, addr: tuple[str, int], cfg: ProxyConfig, log_path: Path | None = None):
        super().__init__(addr, _Handler)
        self.cfg = cfg
        self.slots = threading.BoundedSemaphore(cfg.max_connections)
        self._lock = threading.Lock()
        self.log_path = log_path
        self.log_lines = 0
        self.events: list[dict[str, Any]] = []  # in-memory decision log (bounded), for tests and the validation harness
        self.resolver = BoundedResolver(cfg.resolver or default_resolver, cfg.dns_timeout_s, cfg.dns_max_inflight)
        self._active = 0  # handler threads alive (slot holders AND clients being answered 503): bounded by ``MAX_THREAD_FACTOR``

    def process_request(self, request, client_address):
        """Bound the number of handler threads. The slot semaphore only limits TUNNELS; without this a flood of connections would each get a thread
        (and a file descriptor) before being told 'busy'. Beyond the cap a connection is simply closed."""
        with self._lock:
            over = self._active >= self.cfg.max_connections * MAX_THREAD_FACTOR
            if not over:
                self._active += 1
        if over:
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._lock:
                self._active -= 1
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._lock:
                self._active -= 1

    def record(self, entry: dict[str, Any]) -> None:
        with self._lock:
            if len(self.events) < MAX_LOG_LINES:
                self.events.append(entry)
            if self.log_path is not None and self.log_lines < MAX_LOG_LINES:
                try:
                    with open(self.log_path, "a") as f:
                        f.write(json.dumps(entry, sort_keys=True) + "\n")
                    self.log_lines += 1
                except OSError:
                    pass

    def handle_error(self, request, client_address):  # type name only; never a traceback or data
        print(f"proxy: handler error ({type(sys.exc_info()[1]).__name__})", file=sys.stderr)


def _safe_text(s: str) -> str:
    return "".join(c if 0x20 <= ord(c) < 0x7f else "?" for c in s[:100])


class _Handler(socketserver.BaseRequestHandler):
    server: ProxyServer

    def handle(self) -> None:
        srv, cfg, t0 = self.server, self.server.cfg, time.monotonic()
        entry: dict[str, Any] = {"ts": round(time.time(), 3)}
        client: socket.socket = self.request
        if not srv.slots.acquire(blocking=False):
            return self._answer(entry, 503, "busy")
        upstream: socket.socket | None = None
        try:
            try:
                head, extra = self._read_head(client, cfg)
                host, port = parse_connect(head)
                entry.update(host=_safe_text(host), port=port)
                if extra:
                    raise Refusal(400, "bytes_after_request_not_allowed")  # nothing may ride behind the CONNECT (smuggling)
                authorize(host, port, cfg)
                ip = pick_pinned_address(cfg, srv.resolver)
                entry["pinned_ip"] = ip
                upstream = self._connect(ip, port, cfg)
            except Refusal as r:
                return self._answer(entry, r.status, r.reason)
            except OSError:
                return self._answer(entry, 502, "connect_failed")
            try:
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            except OSError:
                entry.update(outcome="client_gone_before_tunnel", duration_s=round(time.monotonic() - t0, 3))
                return srv.record(entry)
            up, down, why = _tunnel(client, upstream, cfg)
            srv.record({**entry, "outcome": "tunnel", "reason": why, "bytes_up": up, "bytes_down": down,
                        "duration_s": round(time.monotonic() - t0, 3)})
        finally:
            if upstream is not None:
                _close(upstream)
            _close(client)
            srv.slots.release()

    def _answer(self, entry: dict[str, Any], status: int, reason: str) -> None:
        self.server.record({**entry, "outcome": "refused", "status": status, "reason": reason})
        body = f"{reason}\n".encode()
        head = (f"HTTP/1.1 {status} {'Refused' if status < 500 else 'Unavailable'}\r\nContent-Type: text/plain\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
        try:
            self.request.sendall(head + body)
            # Close politely: closing a socket that still has unread input makes the OS send a reset, which can destroy the reply before the
            # client reads it. Half-close, then drain (a bounded amount, briefly) what the client already sent.
            self.request.shutdown(socket.SHUT_WR)
            self.request.settimeout(0.3)
            self.request.recv(MAX_HEAD_BYTES)
        except OSError:
            pass

    @staticmethod
    def _read_head(client: socket.socket, cfg: ProxyConfig) -> tuple[bytes, bytes]:
        """Read up to the blank line. Returns (head including the blank line, any bytes already received beyond it)."""
        deadline = time.monotonic() + cfg.header_timeout_s
        buf = b""
        while b"\r\n\r\n" not in buf:
            if len(buf) > MAX_HEAD_BYTES:
                raise Refusal(431, "request_head_too_large")
            left = deadline - time.monotonic()
            if left <= 0:
                raise Refusal(408, "request_head_timeout")
            client.settimeout(left)
            try:
                chunk = client.recv(2048)
            except (socket.timeout, OSError):
                raise Refusal(408, "request_head_timeout") from None
            if not chunk:
                raise Refusal(400, "malformed_request")
            buf += chunk
        cut = buf.index(b"\r\n\r\n") + 4
        if cut > MAX_HEAD_BYTES:
            raise Refusal(431, "request_head_too_large")
        return buf[:cut], buf[cut:]

    @staticmethod
    def _connect(ip: str, port: int, cfg: ProxyConfig) -> socket.socket:
        s = socket.create_connection((ip, port), cfg.connect_timeout_s)
        try:
            peer = s.getpeername()[0]
            if not _same_ip(peer, ip):  # the pin: the connected peer is the validated address, whatever the stack did
                raise Refusal(502, "peer_address_mismatch")
        except BaseException:
            _close(s)
            raise
        return s


def _close(s: socket.socket) -> None:
    try:
        s.close()
    except OSError:
        pass


def _tunnel(a: socket.socket, b: socket.socket, cfg: ProxyConfig) -> tuple[int, int, str]:
    """Copy bytes both ways without looking at them. Returns (bytes a->b, bytes b->a, why it ended)."""
    start = time.monotonic()
    last = start
    sent = {a: 0, b: 0}  # bytes read FROM that socket
    open_ends = {a, b}
    other = {a: b, b: a}
    why = "closed"
    eof_at: float | None = None
    for s in (a, b):
        s.settimeout(30.0)
    while open_ends:
        now = time.monotonic()
        if eof_at is not None and now - eof_at >= cfg.half_close_grace_s:
            why = "half_close_timeout"
            break
        if now - start >= cfg.max_tunnel_s:
            why = "max_duration"
            break
        if now - last >= cfg.idle_timeout_s:
            why = "idle_timeout"
            break
        if sent[a] + sent[b] >= cfg.max_tunnel_bytes:
            why = "max_bytes"
            break
        try:
            ready, _, _ = select.select(list(open_ends), [], [], 1.0)
        except (OSError, ValueError):
            why = "select_error"
            break
        for s in ready:
            try:
                data = s.recv(65536)
            except OSError:
                data, why = b"", "read_error"
            if not data:
                open_ends.discard(s)
                if eof_at is None:
                    eof_at = time.monotonic()
                try:
                    other[s].shutdown(socket.SHUT_WR)  # half-close: let the other direction finish
                except OSError:
                    pass
                continue
            last = time.monotonic()
            sent[s] += len(data)
            try:
                other[s].sendall(data)
            except OSError:
                why = "write_error"
                open_ends.clear()
                break
    return sent[a], sent[b], why


_PUBLIC_IMAGE_ENV = {"GPG_KEY": re.compile(r"[0-9A-F]{40}")}  # the python image's PUBLIC release-signing key fingerprint: named like a key, is not one


def refuse_secrets(env: dict[str, str]) -> list[str]:
    """Names of environment variables that look like a secret. The proxy refuses to start if there are any: it must hold none.

    The only exception is a variable the base image itself declares that is public by design: ``GPG_KEY`` holding a 40-hex-digit key
    FINGERPRINT (any other value of that name is still refused).
    """
    return sorted(k for k, v in env.items()
                  if (_SECRET_NAME.search(k) or _SECRET_VALUE.search(v)) and not (k in _PUBLIC_IMAGE_ENV and _PUBLIC_IMAGE_ENV[k].fullmatch(v)))


def production_config(root: Path | None = None) -> ProxyConfig:
    root = root or Path(__file__).resolve().parents[2]
    experiment = json.loads((root / "config" / "experiment.json").read_text())
    return ProxyConfig(host=str(experiment["upstream_host"]).lower(), port=DEFAULT_UPSTREAM_PORT)


def main(argv: list[str] | None = None, *, config: ProxyConfig | None = None) -> int:
    """Production entry point. ``config`` is a TEST SEAM (Python argument only): ``python -m aiep.egress_proxy`` always uses the experiment's host."""
    env = dict(os.environ)
    listen = env.get("AIEP_PROXY_LISTEN")
    if not listen:
        print("missing environment: AIEP_PROXY_LISTEN", file=sys.stderr)
        return 2
    leaked = refuse_secrets({k: v for k, v in env.items() if k != "AIEP_PROXY_LISTEN"})
    if leaked:
        print(f"refusing to start: the proxy must hold no secrets, but these variables look like one: {leaked}", file=sys.stderr)
        return 2
    try:
        hosts, port = parse_listen(listen)
    except (ValueError, OSError) as e:
        print(f"invalid AIEP_PROXY_LISTEN: {e}", file=sys.stderr)
        return 2
    cfg = config if config is not None else production_config()
    log = Path(env["AIEP_PROXY_LOG"]) if env.get("AIEP_PROXY_LOG") else None
    servers = [ProxyServer((h, port), cfg, log) for h in hosts]
    for extra in servers[:-1]:
        threading.Thread(target=extra.serve_forever, daemon=True).start()
    try:
        servers[-1].serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

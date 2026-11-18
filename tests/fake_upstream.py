"""Loopback fake of the Anthropic API for gateway tests. No real network, no real key."""

from __future__ import annotations

import json
import shutil
import socket
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable


def make_cert(directory: Path, san: str, name: str) -> tuple[Path, Path]:
    """Self-signed cert for DNS:<san>, via the openssl CLI (nothing to install)."""
    if not shutil.which("openssl"):
        raise RuntimeError("openssl CLI not available")
    cert, key = directory / f"{name}.crt", directory / f"{name}.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-keyout", str(key), "-out", str(cert), "-subj", f"/CN={san}",
         "-addext", f"subjectAltName=DNS:{san}"],
        check=True, capture_output=True,
    )
    return cert, key


def message_json(usage: dict | None = None) -> bytes:
    usage = usage or {"input_tokens": 1000, "output_tokens": 200,
                      "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 400}
    return json.dumps({"id": "msg_1", "type": "message", "role": "assistant",
                       "content": [{"type": "text", "text": "ok"}], "usage": usage}).encode()


def sse_events(usage_start: dict | None = None, out_tokens: int = 200, stop: bool = True) -> list[bytes]:
    usage_start = usage_start or {"input_tokens": 1000, "cache_read_input_tokens": 5000,
                                  "cache_creation_input_tokens": 400, "output_tokens": 1}
    ev = [
        b'event: message_start\ndata: ' + json.dumps({"type": "message_start", "message": {"id": "m", "usage": usage_start}}).encode() + b'\n\n',
        b'event: ping\ndata: {"type": "ping"}\n\n',
        b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n',
        b'event: message_delta\ndata: ' + json.dumps({"type": "message_delta", "delta": {}, "usage": {"output_tokens": out_tokens}}).encode() + b'\n\n',
    ]
    if stop:
        ev.append(b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
    return ev


Behavior = Callable[["Handler", dict], None]


def json_ok(handler: "Handler", req: dict, body: bytes | None = None, headers: dict | None = None, status: int = 200):
    body = body if body is not None else message_json()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    for k, v in (headers or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


def stream(handler: "Handler", chunks: list[bytes], between: Callable[[int], None] | None = None):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()
    for i, c in enumerate(chunks):
        handler.wfile.write(f"{len(c):x}\r\n".encode() + c + b"\r\n")
        handler.wfile.flush()
        if between:
            between(i)
    handler.wfile.write(b"0\r\n\r\n")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a, **k):
        pass

    def setup(self):
        self.tls_ok = True
        if self.server.ctx is not None:
            try:
                self.request = self.server.ctx.wrap_socket(self.request, server_side=True)
            except (ssl.SSLError, OSError):
                self.tls_ok = False
                self.server.handshake_failures += 1
                return
        super().setup()

    def handle(self):
        if self.tls_ok:
            super().handle()

    def finish(self):
        if self.tls_ok:
            try:
                super().finish()
            except OSError:
                pass

    def _go(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        req = {"method": self.command, "path": self.path, "body": body,
               "headers": {k.lower(): v for k, v in self.headers.items()}}
        self.server.requests.append(req)
        self.server.behavior(self, req)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = _go


class FakeUpstream:
    def __init__(self, cert: Path | None = None, key: Path | None = None, behavior: Behavior = json_ok):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.requests = []
        self.server.behavior = behavior
        self.server.handshake_failures = 0
        ctx = None
        if cert is not None:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(cert), str(key))
        self.server.ctx = ctx
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    @property
    def requests(self) -> list[dict]:
        return self.server.requests

    @property
    def handshake_failures(self) -> int:
        return self.server.handshake_failures

    def set_behavior(self, b: Behavior) -> None:
        self.server.behavior = b

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class TcpTripwire:
    """Counts connections to a port that must never be contacted (e.g. a redirect target)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        self._stop = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop:
            try:
                c, _ = self.sock.accept()
                self.connections += 1
                c.close()
            except (socket.timeout, OSError):
                pass

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self._stop = True
        self.thread.join(1)
        self.sock.close()

"""A local, Anthropic-Messages-compatible mock upstream for driving the REAL Claude Code binary (integration test; no Anthropic contact).

It runs on the HOST (a TLS server on 127.0.0.1) and is reached by the gateway only through the restricted proxy, like the real API would be. It never
interprets or "simulates" Claude: it answers the model requests Claude Code sends, in the streaming SSE wire format, with a fixed two-turn script:

  turn 1  (the user's task, no tool_result yet) -> a short text block, then a ``tool_use`` block calling the ``Write`` tool with a harmless file;
  turn 2  (the request now carries Claude's own ``tool_result``)            -> a final text block, ``stop_reason: end_turn``.

Claude Code itself parses the stream, executes the Write tool inside its sandbox, builds the follow-up request and decides it is finished.
Any other request (non-streaming helper calls, ``count_tokens``) gets a small valid answer. Usage fields are fixed and recorded per response so the
gateway's accounting can be checked against an independent computation.

Request logging holds only metadata (path, model, stream flag, message and tool names, header NAMES, sha256 of the key header): never a body,
a prompt, a key or a token.

Modes (``Mock.mode``): ``normal``; ``slow`` holds the FIRST streamed response open, after its first text delta, until ``Mock.release`` is set (or 120 s), so a
test can kill the proxy in the middle of a request.
"""

from __future__ import annotations

import hashlib
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

FILE_PATH = "/testbed/AIEP_INTEGRATION.txt"
FILE_CONTENT = "AIEP integration test: this file was written by the pinned Claude Code binary, driven by a local mock upstream.\n"
TURN1_USAGE = {"input_tokens": 1000, "cache_creation_input_tokens": 500, "cache_read_input_tokens": 0}
TURN1_OUT = 60
TURN2_USAGE = {"input_tokens": 1100, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 500}
TURN2_OUT = 12
HELPER_USAGE = {"input_tokens": 50, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
HELPER_OUT = 5
TEXT1 = "I'll create the requested file."
TEXT2 = "Created AIEP_INTEGRATION.txt."


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


class Mock:
    def __init__(self, cert: Path, key: Path, port: int = 0, host: str = "127.0.0.1"):
        self.requests: list[dict[str, Any]] = []
        self.mode = "normal"
        self.release = threading.Event()
        self.in_flight = threading.Event()  # set when a slow response is being held
        self._lock = threading.Lock()
        self._n = 0
        self._first_stream_done = False
        mock = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def setup(self):
                self.tls_ok = True
                try:
                    self.request = mock.ctx.wrap_socket(self.request, server_side=True)
                except (ssl.SSLError, OSError):
                    self.tls_ok = False
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
                    try:
                        self.request.close()
                    except OSError:
                        pass

            def do_POST(self):
                mock._post(self)

            def do_GET(self):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(str(cert), str(key))
        self.server = ThreadingHTTPServer((host, port), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def start(self) -> "Mock":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()

    # ---- request handling
    def _post(self, h: BaseHTTPRequestHandler) -> None:
        n = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(n) if n else b""
        try:
            doc = json.loads(raw) if raw else {}
        except ValueError:
            doc = {}
        msgs = doc.get("messages") if isinstance(doc.get("messages"), list) else []
        tools = [t.get("name") for t in (doc.get("tools") or []) if isinstance(t, dict)]
        last = msgs[-1] if msgs else {}
        blocks = last.get("content") if isinstance(last.get("content"), list) else []
        # Claude Code 2.1.328 does not put the tool_result in the LAST message (it appends further user text after it), so look through the whole conversation
        result_ids = [b.get("tool_use_id") for m in msgs if isinstance(m, dict) and isinstance(m.get("content"), list) for b in m["content"]
                      if isinstance(b, dict) and b.get("type") == "tool_result"]
        has_tool_result = "toolu_mock_0001" in result_ids
        path = h.path
        with self._lock:
            self._n += 1
            seq = self._n
        kind = ("count_tokens" if "count_tokens" in path else "main" if (tools and "Write" in tools) else "helper")
        turn = None
        if kind == "main":
            turn = 2 if has_tool_result else 1
        entry = {"seq": seq, "ts": round(time.time(), 3), "path": path, "model": doc.get("model"), "max_tokens": doc.get("max_tokens"),
                 "stream": doc.get("stream"), "n_messages": len(msgs), "tools": sorted(t for t in tools if t), "last_role": last.get("role"),
                 "last_block_types": [b.get("type") for b in blocks if isinstance(b, dict)], "kind": kind, "turn": turn,
                 "n_tool_results": len(result_ids),
                 "tool_result_is_error": [bool(b.get("is_error")) for m in msgs if isinstance(m, dict) and isinstance(m.get("content"), list) for b in m["content"]
                                          if isinstance(b, dict) and b.get("type") == "tool_result"],
                 "x_api_key_sha256": hashlib.sha256(h.headers.get("x-api-key", "").encode()).hexdigest(),
                 "has_authorization_header": "Authorization" in h.headers, "header_names": sorted(k.lower() for k in h.headers.keys()),
                 "body_bytes": len(raw), "client": h.client_address[0]}
        with self._lock:
            self.requests.append(entry)
        if kind == "count_tokens":
            return self._json(h, 200, {"input_tokens": 1000})
        usage_in, out, text = (TURN1_USAGE, TURN1_OUT, TEXT1) if turn == 1 else (TURN2_USAGE, TURN2_OUT, TEXT2) if turn == 2 else (HELPER_USAGE, HELPER_OUT, "ok")
        entry["usage_sent"] = {**usage_in, "output_tokens": out}
        model = doc.get("model", "unknown")
        if not doc.get("stream"):
            msg = {"id": f"msg_mock_{seq}", "type": "message", "role": "assistant", "model": model, "content": [{"type": "text", "text": text}],
                   "stop_reason": "end_turn", "stop_sequence": None, "usage": {**usage_in, "output_tokens": out}}
            return self._json(h, 200, msg)
        self._stream(h, seq, model, turn, usage_in, out, text, entry)

    def _json(self, h: BaseHTTPRequestHandler, status: int, doc: dict) -> None:
        b = json.dumps(doc).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(b)))
        h.send_header("request-id", "req_mock")
        h.end_headers()
        h.wfile.write(b)

    def _stream(self, h: BaseHTTPRequestHandler, seq: int, model: str, turn: int | None, usage_in: dict, out: int, text: str, entry: dict) -> None:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("Transfer-Encoding", "chunked")
        h.send_header("request-id", "req_mock")
        h.end_headers()

        def chunk(data: bytes) -> None:
            h.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            h.wfile.flush()

        chunk(sse("message_start", {"type": "message_start", "message": {
            "id": f"msg_mock_{seq}", "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {**usage_in, "output_tokens": 1}}}))
        chunk(sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}))
        chunk(sse("ping", {"type": "ping"}))
        chunk(sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}))
        if self.mode == "slow" and not self._first_stream_done:
            self._first_stream_done = True
            self.in_flight.set()
            self.release.wait(120)  # held open mid-response: the test kills the proxy now
        chunk(sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
        stop = "end_turn"
        if turn == 1:  # the tool call Claude Code must execute itself
            args = json.dumps({"file_path": FILE_PATH, "content": FILE_CONTENT})
            chunk(sse("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {
                "type": "tool_use", "id": "toolu_mock_0001", "name": "Write", "input": {}}}))
            for i in range(0, len(args), 40):  # input_json_delta fragments, as the real API streams tool input
                chunk(sse("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": args[i:i + 40]}}))
            chunk(sse("content_block_stop", {"type": "content_block_stop", "index": 1}))
            stop = "tool_use"
        chunk(sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": out}}))
        chunk(sse("message_stop", {"type": "message_stop"}))
        h.wfile.write(b"0\r\n\r\n")
        h.wfile.flush()
        h.close_connection = True

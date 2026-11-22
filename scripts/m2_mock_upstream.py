"""A local MOCK of the Anthropic Messages API for runtime validation. Never contacts any real service; holds no real credentials.

Runs inside a container (TLS with a test certificate). Behaviour is chosen per request by the header ``anthropic-mock-mode`` (the gateway
forwards ``anthropic-*`` headers): normal (SSE stream, or JSON when ``stream`` is false), malformed_usage, no_usage, early_close,
http_500. Every request is recorded (path, model, max_tokens, stream flag, tool types, key hash and header NAMES; never prompt text) to a
JSONL file the validation reads back. Usage numbers are fixed so the gateway's accounting can be checked exactly.
"""
import hashlib
import json
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USAGE_IN = {"input_tokens": 100, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 10, "output_tokens": 1}
OUT_TOKENS = 20
LOG = sys.argv[1] if len(sys.argv) > 1 else "/tmp/requests.jsonl"
TEXT = ["ok", " done"]


def sse(event, data):
    return ("event: %s\ndata: %s\n\n" % (event, json.dumps(data))).encode()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _record(self, body, extra=None):
        try:
            doc = json.loads(body) if body else {}
        except ValueError:
            doc = {}
        tools = doc.get("tools") or []
        rec = {"ts": time.time(), "path": self.path, "method": self.command, "model": doc.get("model"), "max_tokens": doc.get("max_tokens"),
               "stream": doc.get("stream"), "tool_types": sorted({str(t.get("type", "custom")) if isinstance(t, dict) else "?" for t in tools}),
               "n_tools": len(tools), "x_api_key_sha256": hashlib.sha256(self.headers.get("x-api-key", "").encode()).hexdigest(),
               "has_authorization_header": "Authorization" in self.headers, "header_names": sorted(k.lower() for k in self.headers.keys()),
               "mode": self.headers.get("anthropic-mock-mode", "normal"), "body_bytes": len(body or b""), "client_ip": self.client_address[0],
               **(extra or {})}
        with open(LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
        return doc, rec["mode"]

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n)
        doc, mode = self._record(body)
        if self.path.startswith("/v1/messages/count_tokens"):
            return self._json(200, {"input_tokens": 42})
        if mode == "http_500":
            return self._json(500, {"type": "error", "error": {"type": "api_error", "message": "mock upstream failure"}})
        model = doc.get("model", "unknown")
        usage_final = {"output_tokens": OUT_TOKENS}
        if not doc.get("stream"):
            usage = {**USAGE_IN, "output_tokens": OUT_TOKENS}
            msg = {"id": "msg_mock", "type": "message", "role": "assistant", "model": model, "content": [{"type": "text", "text": "".join(TEXT)}],
                   "stop_reason": "end_turn", "stop_sequence": None, "usage": usage}
            if mode == "malformed_usage":
                msg["usage"] = {"input_tokens": "lots", "output_tokens": -5}
            elif mode == "no_usage":
                del msg["usage"]
            return self._json(200, msg)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("request-id", "req_mock")
        self.end_headers()
        start_usage = dict(USAGE_IN)
        if mode == "malformed_usage":
            start_usage = {"input_tokens": "lots", "output_tokens": 1}
            usage_final = {"output_tokens": -5}
        elif mode == "no_usage":
            start_usage = None
            usage_final = None
        msg = {"id": "msg_mock", "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None, "stop_sequence": None}
        if start_usage is not None:
            msg["usage"] = start_usage
        w = self.wfile
        w.write(sse("message_start", {"type": "message_start", "message": msg}))
        w.write(sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}))
        w.flush()
        for i, piece in enumerate(TEXT):
            w.write(sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": piece}}))
            w.flush()
            if mode == "early_close" and i == 0:
                return  # the stream ends mid-response: no message_delta, no message_stop
            if mode == "slow" and i == 0:
                time.sleep(12)  # holds the stream open long enough for a validation to kill the proxy or the gateway in the middle of it
            time.sleep(0.05)
        w.write(sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
        delta = {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}}
        if usage_final is not None:
            delta["usage"] = usage_final
        w.write(sse("message_delta", delta))
        w.write(sse("message_stop", {"type": "message_stop"}))
        w.flush()

    def _json(self, code, doc):
        b = json.dumps(doc).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", 8443), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain("/tmp/mock/server.pem", "/tmp/mock/server.key")
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.serve_forever()

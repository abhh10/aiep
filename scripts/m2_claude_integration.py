#!/usr/bin/env python3
"""Integration test: the REAL pinned Claude Code binary, in the production AIEP agent container, through the production gateway and restricted proxy,
against a local mock upstream on the HOST. NOT a real Anthropic request: fake credentials only, no Anthropic contact.

    PYTHONPATH=src .venv/bin/python scripts/m2_claude_integration.py --evidence-dir <dir outside the repo> [--claude-binary PATH]

What is production: ``m2_run.prepare`` (binary SHA-256 against the pin, real pinned seed, real A prompt, policy-checked specs), ``m2_run.execute``
(network pre-flight, lease, ``run_with_gateway``: proxy, gateway, topology check, agent, post-start attachment check, supervision, drain, teardown, artifacts),
``run_agent`` (real agent image, injection, Claude exec, patch extraction). What is not: the two SERVER PROCESSES inside the proxy and gateway containers are
started through their documented Python-only test seams (``egress_proxy.main(config=...)`` and ``gateway.main(upstream=...)``) instead of ``-m``, because the
production processes can only reach ``api.anthropic.com``. The ONLY overridden methods are ``ProxyManager._start_process`` and ``GatewayManager._start_process``;
everything before them (create, policy, networks, injection, manifest verification) is the production code, and the production ``-m`` start is unchanged.
The mock runs on the Mac; the proxy reaches it through the Docker Desktop host gateway (the documented residual risk, used here deliberately); the gateway
and agent cannot (ENETUNREACH, measured elsewhere), so every upstream byte goes agent -> gateway -> proxy -> mock.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import m2_claude_mock as MOCK  # noqa: E402
import m2_runtime_validation as V  # noqa: E402
from aiep import ledger as ledger_mod  # noqa: E402
from aiep import lifecycle, m2_run  # noqa: E402
from aiep.container_exec import ExecSession, read_file  # noqa: E402
from aiep.networks import AGENT_NETWORK, EGRESS_NETWORK, PROXY_NETWORK, ensure_networks, verify_run_networks  # noqa: E402
from aiep.proxy_lifecycle import ProxyManager  # noqa: E402

MOCK_NAME = "mock-upstream.test"
HOST_GATEWAY = "192.168.65.254"
FAKE_KEY = "sk-ant-api03-INTEGRATION-FAKE-KEY-000000000000000"  # fake: never valid anywhere
REGISTERED = {"template": "86571bc70d453965dfec954dae6c2e3a073bf61ddbdc2656bc7f5667846d7158",
              "A": "5e44cde50fea6d6c70cc65c0c97043a7c8fe9357d9468b4492305c0db4d64082"}
INSTANCE = "pytest-dev__pytest-5227"
# Independent price list ($ per million tokens, claude-sonnet-5-5, from config/model_pricing.json; unclassified cache writes at the 1h rate). Written out here
# so the accounting check is not circular.
PRICE = {"in": Decimal("2"), "cache_read": Decimal("0.10"), "cache_write": Decimal("4"), "out": Decimal("10")}


def cost_of(u: dict) -> Decimal:
    return (u["input_tokens"] * PRICE["in"] + u["cache_read_input_tokens"] * PRICE["cache_read"] + u["cache_creation_input_tokens"] * PRICE["cache_write"]
            + u["output_tokens"] * PRICE["out"]) / Decimal(1_000_000)


class Captured:
    proxy_logs: dict[str, str] = {}


def install_seams(mock_port: int, ca_pem: bytes) -> None:
    """Override ONLY the process start of the two managers (see the module docstring)."""

    class SeamProxy(ProxyManager):
        def _start_process(self) -> ExecSession:
            ctx = self.spec.ctx
            script = ("import sys\nfrom aiep.egress_proxy import main, ProxyConfig\n"
                      f"sys.exit(main(config=ProxyConfig(host={MOCK_NAME!r}, port={mock_port}, allow_private_addresses=True, resolver=lambda h, p: [{HOST_GATEWAY!r}])))\n")
            return ExecSession(self.api, self.container.id, {"cmd": [ctx.python, "-c", script], "user": ctx.user, "workdir": "/", "environment": None,
                                                             "tty": False}).start()

        def teardown(self) -> Any:
            try:  # the proxy's own decision log, read with the production exec path before the container goes
                Captured.proxy_logs[self.spec.run_id] = read_file(self.api, self.container.id, "/tmp/proxy.jsonl", ctx=self.ectx).decode("utf-8", "replace")
            except Exception as e:
                Captured.proxy_logs[self.spec.run_id] = f"<unreadable: {type(e).__name__}>"
            return super().teardown()

    class SeamGateway(lifecycle.GatewayManager):
        def _start_process(self) -> ExecSession:
            ctx = self.spec.ctx
            V.put_files(self.client, self.container, ctx.user, "/tmp", {"mock-ca.pem": (ca_pem, 0o644)})  # the mock's CA; certificate checking stays ON
            script = ("import sys\nfrom aiep.gateway import main, UpstreamConfig\n"
                      f"sys.exit(main(upstream=UpstreamConfig(host={MOCK_NAME!r}, port={mock_port}, ca_file='/tmp/mock-ca.pem', "
                      f"proxy_host='aiep.proxy.{self.spec.run_id}', proxy_port=3128, require_proxy=True)))\n")
            return ExecSession(self.api, self.container.id, {"cmd": [ctx.python, "-c", script], "user": ctx.user, "workdir": "/", "environment": None,
                                                             "tty": False}).start()

    lifecycle.ProxyManager = SeamProxy
    lifecycle.GatewayManager = SeamGateway


class Poller(threading.Thread):
    """Docker's own event stream: the order in which AIEP containers are DESTROYED, as the daemon reports it (independent of the code under test)."""

    def __init__(self, client: Any):
        super().__init__(daemon=True)
        self.client = client
        self.events: list[tuple[float, str, str]] = []  # (daemon time, name, action)
        self.stop_flag = threading.Event()
        self._stream: Any = None

    @property
    def gone(self) -> dict[str, float]:
        return {n: t for t, n, a in self.events if a == "destroy"}

    def run(self) -> None:
        import docker  # noqa: F401
        self._stream = self.client.api.events(decode=True, since=time.time(), filters={"type": "container"})
        try:
            for ev in self._stream:
                name = (ev.get("Actor") or {}).get("Attributes", {}).get("name", "")
                if name.startswith("aiep."):
                    self.events.append((ev.get("timeNano", 0) / 1e9, name, ev.get("Action", "")))
                if self.stop_flag.is_set():
                    break
        except Exception:
            pass

    def mark(self) -> int:
        return len(self.events)

    def destroy_order(self, since: int = 0) -> list[str]:
        return [n.split(".")[1] for t, n, a in sorted(self.events[since:]) if a == "destroy"]


def role_order(poller: Poller, since: int = 0) -> list[str]:
    return poller.destroy_order(since)


def parse_stream(raw: bytes) -> list[dict]:
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence-dir", required=True)
    ap.add_argument("--claude-binary", default=str(ROOT / ".cache" / "claude" / "2.1.328" / "claude"))
    args = ap.parse_args(argv)
    import docker
    client = docker.from_env(timeout=300)
    rec = V.Recorder("claude_integration", Path(args.evidence_dir))
    mock = None
    poller = Poller(client)
    try:
        mock = _run(client, rec, Path(args.claude_binary), poller)
    finally:
        poller.stop_flag.set()
        if mock is not None:
            mock.stop()
        rec.save()
        client.close()
    return 1 if any(r["status"] == "FAIL" for r in rec.rows) else 0


def _run(client: Any, rec: V.Recorder, binary: Path, poller: Poller) -> Any:
    cli = json.loads((ROOT / "config" / "claude_cli.json").read_text())
    # ---- 1. binary identity
    h = hashlib.sha256()
    with open(binary, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    rec.fact("binary", {"path": str(binary), "size": binary.stat().st_size, "sha256": h.hexdigest(), "pin_version": cli["version"], "pin_platform": cli["platform"],
                        "pin_source": cli["source"]})
    rec.check("Claude binary SHA-256 equals the pin in config/claude_cli.json (a982d380...)", h.hexdigest() == cli["sha256"], h.hexdigest())
    rec.check("Claude binary size equals the pin (373019152 bytes)", binary.stat().st_size == cli["size_bytes"], binary.stat().st_size)

    # ---- 2. mock, certificates, seams
    V.MOCK_HOST = MOCK_NAME  # the throwaway certificate is issued for this name
    certs = V.make_certs(rec.dir / "mock_certs")
    cdir = rec.dir / "mock_certs"
    mock = MOCK.Mock(cdir / "server.pem", cdir / "server.key").start()
    rec.fact("mock upstream", {"host": "127.0.0.1", "port": mock.port, "name": MOCK_NAME, "cert_dir_not_preserved": True})
    install_seams(mock.port, certs["ca.pem"])
    ensure_networks(client)
    poller.start()
    time.sleep(1)
    base = ROOT / "runs" / "m2-integration"

    def prepare(label_stamp: str) -> Any:
        return m2_run.prepare(ROOT, instance_id=INSTANCE, label="A", budget_usd="0.50", wall_clock_s=600, claude_binary=binary, real_key=FAKE_KEY,
                              stamp=label_stamp)

    # ================================================================== scenario 1: success
    p = prepare(time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    rec.check("m2_run.prepare accepted the binary (production SHA-256 and size check), the pinned seed and the registered A prompt", True, p.run_id)
    rec.fact("claude_args (production)", list(p.aspec.claude_args))
    mark1 = poller.mark()
    t0 = time.monotonic()
    ex = m2_run.execute(client, p, base=base, root=ROOT)
    wall = round(time.monotonic() - t0, 1)
    rec.fact("scenario 1 execution", {"run_id": ex.run_id, "directory": str(ex.directory), "wall_s": wall, "error": ex.error, "problems": ex.problems})
    _check_success(client, rec, p, ex, mock, poller, mark1)

    # ================================================================== scenario 2: the proxy is killed during a request
    mock.mode = "slow"
    mock.release.clear()
    mock.in_flight.clear()
    n_before = len(mock.requests)
    time.sleep(2)
    p2 = prepare(time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    killed: dict[str, Any] = {}

    def killer() -> None:
        if not mock.in_flight.wait(900):
            return
        time.sleep(1.0)
        for c in client.containers.list(all=True):
            if c.name == f"aiep.proxy.{p2.run_id}":
                killed["at_requests"] = len(mock.requests)
                killed["t"] = time.time()
                client.api.kill(c.id)
                killed["id"] = c.id
        time.sleep(4.0)
        mock.release.set()  # let the mock's held handler finish; nothing can reach the gateway any more

    threading.Thread(target=killer, daemon=True).start()
    t0 = time.monotonic()
    mark2 = poller.mark()
    ex2 = m2_run.execute(client, p2, base=base, root=ROOT)
    rec.fact("scenario 2 execution", {"run_id": ex2.run_id, "directory": str(ex2.directory), "wall_s": round(time.monotonic() - t0, 1), "error": ex2.error, "problems": ex2.problems})
    mock.release.set()
    _check_proxy_kill(client, rec, p2, ex2, mock, poller, killed, n_before, mark2)
    return mock


def _check_success(client: Any, rec: V.Recorder, p: Any, ex: Any, mock: Any, poller: Poller, mark: int) -> None:
    d = ex.directory
    run = json.loads((d / "run.json").read_text())
    lc, agent = ex.lifecycle, ex.lifecycle.agent if ex.lifecycle else None
    rec.check("run completed without an orchestrator error; artifacts verified (no problems)", ex.error is None and ex.problems == [], {"error": ex.error, "problems": ex.problems})
    topo = lc.topology if lc else None
    rec.check("topology verified before the agent: exactly proxy={proxy-net,egress-net}, gateway={agent-net,proxy-net}, egress-net holds only the proxy",
              topo is not None and set(topo["containers"]["gateway"]) == {AGENT_NETWORK, PROXY_NETWORK} and set(topo["containers"]["proxy"]) == {PROXY_NETWORK, EGRESS_NETWORK}
              and len(topo["networks"][EGRESS_NETWORK]["attached"]) == 1 and len(topo["networks"][AGENT_NETWORK]["attached"]) == 1, topo)
    att = agent.attachment if agent else None
    rec.check("post-start agent attachment check PASSED on the real agent container (agent network only, real ID, in-subnet IP, no ports, no default route)",
              att is not None and set(att["networks"]) == {AGENT_NETWORK} and att["published_ports"] == [] and len(att["routes"]) == 1 and "attachment_ok" in agent.timeline,
              att)
    stdout = (d / "claude_stdout.jsonl").read_bytes() if (d / "claude_stdout.jsonl").exists() else b""
    events = parse_stream(stdout)
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    rec.fact("claude invocation", {"argv": ["/opt/aiep/claude", *p.aspec.claude_args], "stdin": "the registered Config A prompt + the issue text (never argv)",
                                   "exit_code": agent.claude_exit_code if agent else None, "status": agent.status if agent else None, "valid": agent.valid if agent else None,
                                   "stdout_bytes": len(stdout), "event_types": sorted({e.get("type") for e in events}),
                                   "result": {k: result.get(k) for k in ("subtype", "is_error", "num_turns", "result", "duration_ms", "total_cost_usd", "stop_reason")} if result else None})
    rec.check("REAL Claude Code exited 0 with status 'exited', the run is valid, and its final stream-json event is result/success (not an error)",
              agent is not None and agent.claude_exit_code == 0 and agent.status == "exited" and agent.valid and result is not None and result.get("subtype") == "success"
              and result.get("is_error") is False, result)
    tool_uses = [b for e in events if e.get("type") == "assistant" for b in (e.get("message", {}).get("content") or []) if b.get("type") == "tool_use"]
    tool_results = [b for e in events if e.get("type") == "user" for b in (e.get("message", {}).get("content") or []) if isinstance(b, dict) and b.get("type") == "tool_result"]
    rec.check("Claude itself parsed the streamed tool_use, executed the Write tool in its sandbox and sent back a non-error tool_result",
              len(tool_uses) == 1 and tool_uses[0]["name"] == "Write" and tool_uses[0]["input"].get("file_path") == MOCK.FILE_PATH and len(tool_results) == 1
              and not tool_results[0].get("is_error"), {"tool_use": tool_uses[0]["input"].get("file_path") if tool_uses else None, "tool_result": str(tool_results[0].get("content"))[:120] if tool_results else None})
    rec.check("Claude's final answer is the mock's turn-2 text (it processed the streamed completion)", result is not None and MOCK.TEXT2 in str(result.get("result")), result.get("result") if result else None)
    # ---- mock traffic
    reqs = list(mock.requests)
    rec.fact("mock requests (metadata only)", [{k: r[k] for k in ("seq", "path", "kind", "turn", "model", "max_tokens", "stream", "n_messages", "tools", "last_block_types", "usage_sent")
                                                  if k in r} for r in reqs])
    mains = [r for r in reqs if r["kind"] == "main"]
    rec.check("the mock received the expected Messages requests: turn 1 (task) then turn 2 (carrying Claude's tool_result), streaming, pinned model, max_tokens 64000, Write tool offered",
              [r["turn"] for r in mains][:2] == [1, 2] and all(r["stream"] and r["model"] == "claude-sonnet-5-5" and r["max_tokens"] == 64000 and "Write" in r["tools"] for r in mains)
              and mains[1]["n_tool_results"] == 1 and all(r["path"].startswith("/v1/messages") for r in reqs), [(r["turn"], r["max_tokens"]) for r in mains])
    key_hash = hashlib.sha256(FAKE_KEY.encode()).hexdigest()
    rec.check("every upstream request carried the gateway's (fake) key in x-api-key and NEVER the agent's token or an Authorization header",
              bool(reqs) and all(r["x_api_key_sha256"] == key_hash and not r["has_authorization_header"] for r in reqs))
    # ---- path
    plog = [json.loads(l) for l in Captured.proxy_logs.get(p.run_id, "").splitlines() if l.startswith("{")]
    tunnels = [e for e in plog if e.get("outcome") == "tunnel"]
    rec.fact("proxy decision log", [{k: e.get(k) for k in ("outcome", "host", "port", "pinned_ip", "reason", "bytes_up", "bytes_down")} for e in plog])
    rec.check("every upstream request crossed the proxy: its log shows tunnels (one per request) to the allowed mock name only, pinned to the host-gateway address, none refused",
              len(tunnels) == len([r for r in reqs if r["kind"] != "count_tokens"]) + len([r for r in reqs if r["kind"] == "count_tokens"]) and all(
                  e["host"] == MOCK_NAME and e["pinned_ip"] == HOST_GATEWAY for e in tunnels) and not any(e.get("outcome") == "refused" and e.get("reason") != "malformed_request" for e in plog),
              {"tunnels": len(tunnels), "requests": len(reqs)})
    rec.check("the mock never saw the key or token in plaintext metadata and the proxy log holds no key, token or prompt", FAKE_KEY not in json.dumps(plog) and p.gspec.run_token not in json.dumps(plog))
    # ---- workspace and patch
    patch = (d / "patch.diff").read_text() if (d / "patch.diff").exists() else ""
    rec.fact("extracted patch", patch)
    rec.check("patch extraction captured exactly the new file Claude wrote (and nothing else)", "+++ b/AIEP_INTEGRATION.txt" in patch and MOCK.FILE_CONTENT.strip() in patch
              and patch.count("diff --git") == 1 and agent is not None and agent.patch is not None and agent.patch.status == "ok", patch[:300])
    pred = (d / "predictions.jsonl").read_text() if (d / "predictions.jsonl").exists() else ""
    rec.check("predictions.jsonl holds that patch (instance id, model name) and the evaluator command is reported but NOT run", bool(pred) and json.loads(pred.splitlines()[0])["instance_id"] == INSTANCE
              and ex.eval_command is not None and "evaluation" in run and "not run" in run["evaluation"], run.get("evaluation"))
    # ---- accounting
    ledger = (d / "gateway_ledger.jsonl").read_bytes()
    entries = [json.loads(l) for l in ledger.decode().splitlines() if l.strip()]
    fin = [e for e in entries if e.get("outcome") != "forwarding"]
    sent = [r["usage_sent"] for r in reqs if "usage_sent" in r]
    expected = sum((cost_of(u) for u in sent), Decimal(0))
    measured = Decimal(str(fin[-1]["cumulative_usd_computed"])) if fin else Decimal(-1)
    audit = ledger_mod.audit(ledger)
    rec.fact("accounting", {"requests_billed": len(sent), "expected_usd_independent": str(expected), "ledger_cumulative_usd": str(measured), "audit": {k: audit[k] for k in ("forwarded", "finished", "complete")},
                            "usage_measured": run["usage_measured"], "accounting": {k: run["accounting"].get(k) for k in ("complete", "exact", "forwarded", "finished", "note")}})
    rec.check("gateway ledger is COMPLETE (every forwarded request has a finishing line) and run.json marks the accounting exact", audit["complete"] and run["accounting"]["exact"] is True
              and run["usage_measured"]["exact"] is True and run["usage_measured"]["entries_charged_at_max_tokens_upper_bound"] == 0, run["accounting"])
    tok = {k: sum(u[k] for u in sent) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")}
    seen = run["usage_measured"]["tokens"]
    rec.check("token usage measured by the gateway equals what the mock sent, per field (input, cache read, cache write, output)", seen.get("input_tokens") == tok["input_tokens"]
              and seen.get("cache_read_tokens") == tok["cache_read_input_tokens"] and seen.get("output_tokens") == tok["output_tokens"]
              and (seen.get("cache_write_unclassified_tokens", 0) + seen.get("cache_write_1h_tokens", 0) + seen.get("cache_write_5m_tokens", 0)) == tok["cache_creation_input_tokens"], {"mock": tok, "gateway": seen})
    rec.check(f"computed cost equals the independent arithmetic (${expected}) exactly", measured == expected, f"{measured} vs {expected}")
    est = run["usage_cli_estimate"]
    rec.fact("Claude's own client-side estimate (NOT measured; shown for comparison only)", est)
    # ---- artifacts / identity
    pr = run["prompt"]
    rec.check("run.json records the registered Config A prompt hashes unchanged (template 86571bc7..., workflow A 5e44cde5...)",
              pr["template_sha256"] == REGISTERED["template"] and pr["workflow_sha256"] == REGISTERED["A"], pr)
    from aiep import artifacts
    rec.check("persisted artifacts verify (hash manifest) and contain no key or token", artifacts.verify_run_artifacts(d) == [] and all(
        FAKE_KEY not in (d / f.name).read_bytes().decode("utf-8", "replace") and p.gspec.run_token not in (d / f.name).read_bytes().decode("utf-8", "replace") for f in d.iterdir() if f.is_file()),
        sorted(f.name for f in d.iterdir()))
    rec.check("run.json records the claude binary identity (sha256, size, version) and the three verified networks", run["claude_cli"]["sha256"] == json.loads((ROOT / "config" / "claude_cli.json").read_text())["sha256"]
              and set(run["networks"]) == {AGENT_NETWORK, PROXY_NETWORK, EGRESS_NETWORK}, run["claude_cli"])
    # ---- cleanup
    left = [c.name for c in client.containers.list(all=True) if c.name.startswith("aiep.")]
    order = role_order(poller, mark)
    rec.check("all three containers removed, in the order agent -> gateway -> proxy (Docker's own destroy events)", left == [] and order == ["agent", "gateway", "proxy"] and lc.gateway_removed
              and lc.proxy_removed and agent.removed, {"left": left, "daemon_destroy_order": order})
    verify_run_networks(client, AGENT_NETWORK, PROXY_NETWORK, EGRESS_NETWORK)
    rec.check("the three AIEP networks are empty and policy-exact afterwards", True)


def _check_proxy_kill(client: Any, rec: V.Recorder, p: Any, ex: Any, mock: Any, poller: Poller, killed: dict, n_before: int, mark: int) -> None:
    d = ex.directory
    run = json.loads((d / "run.json").read_text())
    lc = ex.lifecycle
    agent = lc.agent if lc else None
    rec.fact("proxy kill", {"killed_container": (killed.get("id") or "")[:12], "mock_requests_at_kill": killed.get("at_requests"), "mock_requests_total": len(mock.requests) - n_before})
    rec.check("the proxy container was killed while a streamed request was in flight", bool(killed.get("id")), killed.get("at_requests"))
    rec.check("supervision classified it: stop reason 'proxy_died', the run is INVALID, Claude's output is not graded and no patch/predictions were written",
              agent is not None and agent.status == "gateway_stopped" and agent.stop_reason == "proxy_died" and agent.valid is False and agent.patch is None
              and not (d / "predictions.jsonl").exists() and not (d / "patch.diff").exists(), {"status": agent.status if agent else None, "stop_reason": agent.stop_reason if agent else None,
                                                                                              "invalid_reason": agent.invalid_reason if agent else None})
    after = [r for r in mock.requests[n_before:] if r["ts"] > (killed.get("t") or 1e18)]
    rec.check("NO DIRECT FALLBACK: after the proxy died the mock received no further request from the gateway (Claude's retries reach the gateway and fail there)", len(after) == 0,
              [(r["kind"], r["path"]) for r in after])
    ledger = (d / "gateway_ledger.jsonl").read_bytes() if (d / "gateway_ledger.jsonl").exists() else b""
    entries = [json.loads(l) for l in ledger.decode().splitlines() if l.strip()]
    audit = ledger_mod.audit(ledger)
    outcomes = [(e.get("outcome"), e.get("status"), bool(e.get("usage_incomplete_upper_bound"))) for e in entries if e.get("outcome") != "forwarding"]
    rec.fact("gateway ledger after the kill (outcomes)", {"entries": outcomes, "audit": {k: audit[k] for k in ("forwarded", "finished", "complete")}, "accounting": run["accounting"],
                                                          "usage_measured": run["usage_measured"]})
    held = [e for e in entries if e.get("outcome") != "forwarding" and (e.get("usage_incomplete_upper_bound") or e.get("outcome") == "upstream_read_error")]
    rec.check("the in-flight stream was cut by the proxy's death and is recorded: charged at the max_tokens upper bound and flagged, OR left as an unresolved 'forwarding' line "
              "(accounting then reported INCOMPLETE); the run record says the figures are not exact", (bool(held) or not audit["complete"]) and run["usage_measured"]["exact"] is False
              and (run["accounting"] is None or run["accounting"]["exact"] is False or run["usage_measured"]["entries_charged_at_max_tokens_upper_bound"] > 0), run["usage_measured"])
    left = [c.name for c in client.containers.list(all=True) if c.name.startswith("aiep.")]
    order = role_order(poller, mark)
    rec.check("cleanup removed the agent, the gateway and the (dead) proxy; no AIEP container is left", left == [] and lc.gateway_removed and agent is not None and agent.removed
              and (lc.proxy_removed or lc.proxy_cleanup_error is not None) and lc.proxy_state in ("removed", "stopped"), {"left": left, "removal_order": order, "proxy_state": lc.proxy_state,
                                                                                                                          "proxy_cleanup_error": lc.proxy_cleanup_error})
    rec.check("agent removed before the gateway, gateway before the proxy (Docker destroy events)", order == ["agent", "gateway", "proxy"], order)
    verify_run_networks(client, AGENT_NETWORK, PROXY_NETWORK, EGRESS_NETWORK)
    rec.check("networks empty and policy-exact afterwards (the next run's pre-flight passes)", True)
    from aiep import artifacts
    rec.check("the invalid run's artifacts were still persisted and verify", artifacts.verify_run_artifacts(d) == [], sorted(f.name for f in d.iterdir()))


if __name__ == "__main__":
    sys.exit(main())

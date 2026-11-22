# M2 restricted egress architecture

Status: implemented, unit-tested (fakes, loopback TLS) and validated on real Docker Desktop against a local mock upstream. No real Anthropic API request has ever been made.
Authoritative code: `src/aiep/{egress_proxy,listen,proxy_lifecycle,networks,lifecycle,container_policy,gateway}.py`. Supersedes the "egress network attached to the gateway" design in `M2_PHASE1_NOTES.md` and `M2_PHASE2_DESIGN.md`.

## 1. Topology

```
agent ─ aiep-agent-net (internal) ─ gateway ─ aiep-proxy-net (internal) ─ proxy ─ aiep-egress-net (bridge, ICC off) ─ internet
```

| network | kind | members (exactly) |
|---|---|---|
| `aiep-agent-net` | internal, `inhibit_ipv4` (no VM-side bridge address), no default route | agent, gateway |
| `aiep-proxy-net` | internal, `inhibit_ipv4`, ICC on | gateway, proxy |
| `aiep-egress-net` | ordinary bridge, ICC off | proxy |

* **agent** (uid 1000): agent network only. Never on the proxy or egress network. Holds only the per-run token.
* **gateway** (uid 10001): agent + proxy networks only; **no network with a route out**. It alone holds the real API key. Its only upstream path is a TCP connection to the proxy and `CONNECT api.anthropic.com:443`; it then does the TLS handshake itself (certificate and hostname verification) through the tunnel. It binds only its agent-network address plus loopback.
* **proxy** (uid 10002): proxy + egress networks. Holds **no** secret (policy refuses any secret-like environment variable and any copy of the key or token; the process refuses to start with one). Same pinned python image as the gateway, injected and manifest-verified code, read-only root, all capabilities dropped, no-new-privileges, 128 MiB, 64 pids.

## 2. What the proxy does (and does not)

Accepts only `CONNECT <configured host>:443` (host from `config/experiment.json`). Everything else is refused before any DNS lookup or connection: other methods (405), malformed or oversized heads, request bodies or bytes pipelined behind the CONNECT (400), IP literals and numeric spellings, other names, other ports (403).
For an accepted request it resolves the name itself **once**, requires **every** returned address to be public unicast (a mixed answer, private, loopback, link-local, CGNAT, multicast, reserved, NAT64, 6to4, Teredo, IPv4-mapped private, or a scoped address is refused whole), connects to one validated address (IPv4 first) and verifies the connected peer equals it. The client never chooses the address, and there is no second lookup, so DNS rebinding cannot change the destination. It never terminates, inspects or re-signs TLS: bytes are copied.
Bounds: 8 tunnel slots and 64 handler threads, 10 s head deadline (4 KiB, 32 headers), 5 s DNS deadline (see below), 10 s connect, 660 s idle, 3600 s and 128 MiB per tunnel, 30 s grace after one side closes (a dead client cannot pin a slot).

**Bounded DNS.** `getaddrinfo` has no timeout and cannot be cancelled, and a hand-written DNS client would be a new attack surface, so `BoundedResolver` uses the platform resolver in a daemon worker thread and makes the REQUEST wait at most `dns_timeout_s` (5 s). A lookup that misses the deadline ends by itself at the C library's own resolver timeout and its answer is discarded (never cached, never reused). At most `dns_max_inflight` (2) such threads can exist: the semaphore is held by the thread, not the request, so when that many are stuck further requests are refused at once. Failures are distinct and logged: `dns_failure` (502), `dns_timeout` (504), `dns_busy` (503). Every request still does its own lookup, validates every address and pins one, so the rebinding guarantees are unchanged. Limitation: up to 2 stuck resolver threads can exist for the length of libc's own timeout (typically 5-20 s); the smallest safer alternative would be a local caching resolver with a hard timeout inside the proxy container, which is not warranted by this threat model.

The gateway never falls back: a proxy that is unreachable, refuses, replies oddly or lacks a name ends the request with 502 and a fixed reason. Production sets `require_proxy` so the direct-connection code path (kept for tests) raises `proxy_required` instead.

## 3. Listen addresses

`AIEP_LISTEN` / `AIEP_PROXY_LISTEN` accept only `net:<private ipv4 cidr>:<port>` where the CIDR is the real subnet of the intended network (read from `docker network inspect` by the orchestrator). The process binds the one interface inside it plus loopback (for the readiness probe). Wildcards, bare or loopback-only addresses, `internal:`, a non-private CIDR, no matching interface, several matching interfaces and an interface carrying the default route (the egress side) are refused by the policy at create time and again by the process. Verified in real containers.

## 4. Startup and shutdown ordering

Pre-flight (before any container, before the lease): all three networks inspected and policy-exact, empty, internal subnets private and disjoint from each other and from the egress subnet.
Start: **proxy** (create on proxy-net, attach egress-net, inspect, inject + verify manifest, start process, ready) → **gateway** (create on agent-net, attach proxy-net, same steps) → `verify_topology` on the live attachments (container IDs per network and each container's `NetworkID`) → **agent**, which after it STARTS is re-inspected (`agent_attachment.py`): State running; exactly the agent network, with the verified network ID and an IPv4 address inside its verified subnet; no IPv6/secondary address or gateway; no published ports; its own `/proc/net/route` has no default route and only the agent subnet; the full agent policy again; and `verify_topology` is repeated with the agent's real ID. Any mismatch makes the run invalid before anything is injected, and the container, gateway and proxy are torn down.
Stop: agent (removed by `run_agent`) → gateway (bounded 90 s drain, stop, ledger read, removal) → proxy (stop, removal). Every step is in a `finally`; failures are recorded, never swallowed. A proxy that dies mid-run is the invalid-run stop reason `proxy_died`. The reaper treats the proxy like the agent and gateway (role `proxy`, exact name `aiep.proxy.<run id>`, same run lease) and never touches networks.

## 5. Residual risks (read these)

1. **The proxy container can reach Docker Desktop host services.** Its network namespace has a default route through the egress network, and Docker Desktop's host gateway (`192.168.65.254`) is reachable from it. Measured: it connected to the Mac's listeners on that address (3306, 5000, 7000, 8828, 24692, 33060 at validation time) and to the Mac's LAN IP on the same ports. **The proxy is not network-isolated from host services.** What protects them is the proxy's code (fixed destination, no client-chosen address, every resolved address must be public) plus the facts that it holds no secret, runs non-root with no capabilities, and is reachable only from the gateway. A bug in the ~300-line proxy, or code execution inside it, would expose host services. Docker Desktop offers no verified way to close this from inside the VM, and no unverified workaround was added.
2. The gateway and agent have no such exposure: measured `ENETUNREACH` to every host, private, link-local and public destination.
3. The permitted tunnel goes to whatever public address `api.anthropic.com` resolves to at that moment; it is validated as public, not as "Anthropic's".
4. The gateway's direct-connect code remains in the injected file for tests; production is protected by `require_proxy`, by having no route, and by tests.
5. (Closed) The agent's attachment is now verified on the RUNNING container and the live membership of all three networks is re-verified with its ID; a later out-of-band network change during a run is still not detected.
6. IPv6 is disabled on all three networks. Streaming to the real API, real SSE shapes and the Claude binary through the proxy are unobserved (no real request has been made).
7. DNS is bounded (see above) but a stuck libc lookup still occupies one of two resolver threads until libc gives up.

## 6. Validation

* Unit/integration (loopback TLS, production `ProxyServer` and `Gateway` unmodified): `tests/test_egress_proxy.py`, `tests/test_streaming_proxy.py` (multi-event SSE, fragmented reads from 1 byte, mid-stream upstream disconnect, client disconnect, gateway killed mid-stream, proxy vanishing mid-stream, tunnel byte cap, no direct dial), `tests/test_proxy_lifecycle.py`, `tests/test_networks.py`, `tests/test_container_policy.py`, `tests/test_gateway_listen.py`.
* Real Docker: `scripts/m2_egress_validation.py` (final pass: 81 PASS, 1 LIMITATION = residual risk 1, 0 FAIL). Covers exact attachments, direct connectivity from every container (host gateway incl. the six discovered ports, private, LAN, link-local, public, DNS), the proxy's refusals driven the way the gateway drives it, real-resolver DNS refusals, gateway→proxy→mock success with exact accounting (non-streaming and streaming), upstream cut-off, proxy killed mid-stream, gateway killed mid-stream (incomplete accounting), listen-value refusals, lifecycle failures and reaper dry runs, bounded DNS in the real container, and the REAL agent container through production `run_agent` (valid attachment; wrong subnet; wrong network ID; attached to the proxy or egress network after start: all invalid, nothing injected, container removed).
* Evidence: `runs/validation/egress-2026-11-20/` (git-ignored; README, SHA256SUMS, eight passes, the failed ones kept). Final pass (pass8) `egress.json` sha256 `47a8d0a27114beebea4b73bf716779e75e9aa4a2f52be533e954faa9362b5492`, `connectivity_matrix.json` sha256 `449826baafcd37dfde74f9f9a565b847362e2aea7dbbcf798bcaa661a1989d75`. Original scratchpad location is recorded in the README there.
* `scripts/m2_runtime_validation.py` phases networks, gateway, agent, integration and lifecycle target the old two-network topology and now refuse to run.

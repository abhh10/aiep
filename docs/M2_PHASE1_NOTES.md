# M2 Phase 1 notes: trusted components (no Docker, no network, no spend)

> **Superseded in part (2026-11-20):** the gateway no longer has an egress-capable network. The current topology (agent-net, proxy-net, egress-net with a restricted CONNECT proxy) is in `docs/M2_EGRESS_ARCHITECTURE.md`; statements below about attaching an egress network to the gateway describe the earlier design.


Status: implemented and unit-tested with fake Docker clients, a loopback fake API server and
temporary git repositories, then audited (below). Nothing here has run against Docker, the real
API or a benchmark evaluation. This is not a result: Phases 2 to 4 still have to prove these
components in containers.

## Security limitations (read before relying on any of this)

1. **Gateway outbound restriction is code-enforced, not firewall-enforced.** The gateway
   (`src/aiep/gateway.py`) forwards only to one hardcoded host (`api.anthropic.com:443`) over
   verified TLS (TLS 1.2+), resolves it once per request, refuses non-public addresses and pins the
   connection to the validated IP. But the gateway container will sit on an egress-capable network
   (Phase 2). A bug or compromise of the gateway process could connect anywhere. There is no
   network-level egress filter behind it.
2. **Budget overshoot is bounded by `max_concurrent` requests (default 2), not by one.** The first
   version of this note claimed one request; the audit showed that was false under concurrency.
   Usage is only known after a response. At most `max_concurrent` requests are in flight upstream,
   the rest queue and are re-checked against the budget before being sent, and each is limited by
   the request-size and `max_tokens` caps. A budget must be set with that bound in mind.
3. **The attestation HMAC key shares a host with the code** (`src/aiep/gate.py`). It stops stale,
   accidental or tampered artifacts and anything that cannot read the key (the agent and graded
   code never can). It does not prove a probe actually ran: whoever holds the key can mint an
   attestation. It does not defend against a compromised host. Phase 3 should add probe evidence
   (container inspect hash, probe output hash, timestamps) under the MAC.
4. **Patch extraction must never run git on the host.** Git can execute programs named by a
   repository's config or attributes. The hardened flags in `src/aiep/patch.py` disable
   fsmonitor, hooks, textconv and external diff, but **clean filters cannot be disabled from the
   command line**. Production extraction runs inside the agent container through an injected
   executor. That executor does not exist yet (Phase 2). `extract_patch` has no default executor,
   and tests enforce that the host-side runner is referenced nowhere in production code.
5. **Host lock has no artifact hashes.** `requirements/host.lock` pins exact installed versions,
   derived from declared dependencies. Hashes cannot be derived offline. It is specific to
   CPython 3.12 / darwin / arm64.
6. **The gateway can be slowed by the agent.** It is a threaded server with a 128-connection backlog
   and a concurrency limit on upstream calls, but no per-client connection cap. A hostile agent can
   degrade its own run's gateway. It cannot reach anything else through it.
7. **Unverified against the real client.** Whether Claude Code works over plain http to the
   gateway, which model IDs it requests in the background, what `tools` it sends, whether it uses
   chunked request bodies, and which query strings it adds are all untested. The gateway fails
   closed on each. Phase 2 Step 9b measures them with a fake upstream (no spend).
8. **tmpfs caps are placeholders.** `container_policy` caps each agent tmpfs at 1 GiB and the total
   at 1.25 GiB (tmpfs counts against the 2 GiB memory limit). The numbers are to be set from
   measurement in Phase 2.

## Dataset provenance (verified locally; not verified against the Hub)

| Identifier | What it is | Evidence |
|---|---|---|
| `b0dde1093fe417d83b7184254edf8199c1f0dff5` | The Hugging Face dataset repository commit that the local cache's `refs/main` pointed to when it was downloaded | `refs/main` content; snapshot directory name; the `datasets` Arrow cache directory name. All three agree. |
| `93fe417d` | Not a separate identifier. It is the substring at characters 8 to 15 of the revision above | `grep` finds it only inside that string |
| parquet blobs | Content integrity | sha256 of both parquet files equals their blob names; README blob name equals its git blob hash |

Not verified: that `b0dde109…` is the Hub's actual commit hash (needs a network read of the Hub),
and that the M1 runs used it. M1 logs record `HF_HOME` but not the revision, so for M1 it stays
**inferred** (cache file times plus the absence of any other snapshot). From the first M2 run the
manifest records the pinned revision after verifying `refs/main` and the snapshot directory, so M2
runs are locally verified, not Hub-verified.

## Agent workspace (proposed; awaiting approval)

The agent root filesystem is read-only. `/testbed` is a dedicated tmpfs, populated at start from a
read-only seed copy baked into the agent image (`/opt/seed/testbed`) by a trusted entrypoint,
running as the non-root agent user. Nothing crosses from the host. Because a tmpfs disappears when
its container stops, the patch must be extracted while the container is still running (the
supervisor entrypoint signals completion, the orchestrator extracts through `exec`, then kills).
`agent_runner.run_agent` currently waits for exit and must change for this. Fallback if tmpfs is
too small: one disposable named volume, which needs a policy exception and a disk guard.

## What the gate does

`evaluator.py` refuses to run (exit 2) without an attestation that binds, by SHA-256 and under an
HMAC, the evaluator wrapper, container policy, gate module, wheelhouse manifest, image manifest,
expected image IDs, harness and Docker SDK versions and the network mode. Gold controls pass
through the same gate. The only environment variable the gate reads is `AIEP_ATTEST_KEY_FILE`
(where the key is). The CLI has no flag to skip it. In-process callers (tests) can inject
`run_harness` and `repo_root`; the CLI cannot. While `config/wheelhouse.json` is `unpopulated` no
attestation can be built, so the evaluator cannot grade anything. That is deliberate and ends in
Phase 3.

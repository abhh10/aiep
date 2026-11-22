"""Time limits shared by the orchestrator, the agent runner, the run lease and the reaper: one definition, no drift.

The relationships are enforced by tests (``tests/test_lease.py``, ``tests/test_reaper.py``):

* an agent run may last at most ``MAX_WALL_CLOCK_S`` of Claude time (``run_agent`` and ``m2_run.prepare`` both refuse more);
* a live orchestrator refreshes its lease every ``HEARTBEAT_INTERVAL_S`` and the lease counts as expired after ``LEASE_EXPIRY_S``
  without a refresh (ten missed beats, so a busy host or a short stall never looks like a death);
* a live orchestrator that can no longer refresh its lease (for ``LEASE_STOP_AFTER_S``, well before the lease looks expired to the reaper)
  stops the agent through the lifecycle and tears both containers down, so a run never keeps spending without a lease;
* the reaper never removes a container of an unexpired lease, and for a container with NO lease it waits until the container is
  older than ``MIN_TTL_S = MAX_WALL_CLOCK_S + SETUP_TEARDOWN_MARGIN_S``, i.e. older than any legitimate run could be.
"""

from __future__ import annotations

DEFAULT_WALL_CLOCK_S = 1800.0  # the experiment's 30-minute limit
MIN_WALL_CLOCK_S = 60.0
MAX_WALL_CLOCK_S = 3600.0  # the ceiling: nothing may be configured above it
SETUP_TEARDOWN_MARGIN_S = 3600.0  # gateway and agent setup (400 MiB injection), quiesce, patch extraction, removal, with room to spare

HEARTBEAT_INTERVAL_S = 30.0
LEASE_EXPIRY_S = 300.0  # silence after which a lease is "expired" (not yet reapable: see the grace period)
LEASE_STOP_AFTER_S = 90.0  # the orchestrator STOPS its own run after this long without one successful lease write (three missed beats)
LEASE_FUTURE_TOLERANCE_S = 60.0  # a heartbeat this far ahead of the clock is clock skew; beyond it the lease is invalid (fail closed)

MIN_TTL_S = int(MAX_WALL_CLOCK_S + SETUP_TEARDOWN_MARGIN_S)  # for containers without a lease: older than any possible run
DEFAULT_TTL_S = 3 * 60 * 60
MIN_LEASE_GRACE_S = int(LEASE_EXPIRY_S)
DEFAULT_LEASE_GRACE_S = 15 * 60  # extra silence required after expiry (a suspended laptop must be able to resume heartbeating)

# Claude Code 2.1.328 asks for max_tokens=128000 with claude-sonnet-5-5 by default (measured against the real gateway in M2 runtime validation: the
# gateway refused it with 400 because its cap is 64000). The agent is therefore given CLAUDE_CODE_MAX_OUTPUT_TOKENS, identical for Config A and B, so
# every request stays within the gateway's max_tokens cap. Raising the gateway cap instead would double both the worst-case overshoot of the budget and
# the upper-bound charge for an interrupted stream ($10 per MTok of output). Must be <= aiep.gateway.MAX_TOKENS_CAP (tested).
AGENT_MAX_OUTPUT_TOKENS = 64_000

# The gateway's own max_tokens cap (aiep.gateway.MAX_TOKENS_CAP). The gateway is injected as a self-contained component and cannot import this
# module, so the two constants are kept equal by a test (tests/test_agent_runner.py), the agent policy requires the output-token pin to fit under this
# value at create time and against the created container's inspect, and the gateway itself refuses (400) any request above its own cap and records
# every request's requested max_tokens and the cap in its ledger, so the guarantee is observable after the fact.
GATEWAY_MAX_TOKENS_CAP = 64_000

# Claude Code runtime settings pinned in the agent container's environment: IDENTICAL for Config A and Config B (build_create_kwargs has no label input),
# validated at create time (container_policy._check_agent_env) and again against the created container's inspect (verify_inspect). Every name was found
# in the embedded source of the pinned Linux 2.1.328 binary; the values are:
#   CLAUDE_CODE_MAX_OUTPUT_TOKENS  see AGENT_MAX_OUTPUT_TOKENS above
#   CLAUDE_CODE_MAX_RETRIES        explicit retry count. In the embedded source of 2.1.328 the default (variable unset) is 10 and the maximum is 15
#                                  (larger values are clamped with a warning); 10 therefore pins the binary's own default. Setting the variable also
#                                  turns off the CLI's optional "patient" overloaded-retry mode, which applies only when it is unset. The back-off
#                                  schedule between attempts is not configurable by any variable found, so it stays uncontrolled (see below).
#   API_TIMEOUT_MS                 per-request timeout; the CLI's message states "600000 by default", pinned to that value
#   BASH_DEFAULT_TIMEOUT_MS        Bash tool default timeout; source default 120000
#   BASH_MAX_TIMEOUT_MS            Bash tool maximum timeout; source default 600000
AGENT_RUNTIME_PINS = {
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(AGENT_MAX_OUTPUT_TOKENS),
    "CLAUDE_CODE_MAX_RETRIES": "10",
    "API_TIMEOUT_MS": "600000",
    "BASH_DEFAULT_TIMEOUT_MS": "120000",
    "BASH_MAX_TIMEOUT_MS": "600000",
}
# Claude Code settings that remain at the binary's own defaults (UNCONTROLLED, recorded in every run's metadata so a later change cannot go unnoticed).
CLAUDE_RUNTIME_UNCONTROLLED = (
    "CLAUDE_STREAM_IDLE_TIMEOUT_MS (default derived from the provider classification: not pinned, default not verified for a custom base URL)",
    "CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS", "MAX_THINKING_TOKENS (thinking budget)", "BASH_MAX_OUTPUT_LENGTH",
    "DISABLE_PROMPT_CACHING (caching stays on)", "the CLI's built-in retry back-off schedule",
)
if AGENT_MAX_OUTPUT_TOKENS > GATEWAY_MAX_TOKENS_CAP:  # fail at import: a pin above the cap would make every request refused
    raise RuntimeError("AGENT_MAX_OUTPUT_TOKENS exceeds the gateway's max_tokens cap")

# How long the orchestrator waits, at the end of a run, for requests still being served by the gateway to finish and be written to the ledger
# before it stops the gateway. Bounded: when it expires the run records an explicit incomplete-accounting marker instead of silently losing them.
GATEWAY_DRAIN_TIMEOUT_S = 90.0

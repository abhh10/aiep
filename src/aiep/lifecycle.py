"""Gateway lifecycle and the combined run: the agent never starts before the gateway is verified and ready.

Container order: the restricted egress proxy first (``proxy_lifecycle``), then the gateway, then the agent; teardown is the reverse (agent,
gateway, proxy). The gateway is attached to the agent network and the proxy network ONLY and reaches its upstream through the proxy; the proxy is
the only container on the egress network. After both are ready the ACTUAL attachments are verified (``networks.verify_topology``) before the
agent is created.

Gateway states, in order, each entered only from the previous one:

    new -> created -> started -> verified -> ready -> stopped -> removed

 * created   policy-checked create (inert PID 1, fixed entrypoint), proxy network attached, ``inspect`` re-checked;
 * started   the container is running; nothing of ours is running in it yet and nothing listens;
 * verified  the code tar was injected and matched its manifest entry by entry (modes, links, hashes, owners),
             and only then was ``python -m aiep.gateway`` started (``gateway_runner``);
 * ready     a TCP connect to the gateway's own loopback listener succeeded while its process was alive;
 * stopped   every process of the gateway's user was killed and the exec socket closed;
 * removed   the container was killed and removed together with its anonymous volumes.

``run_with_gateway`` builds on that: gateway setup, then the agent (``agent_runner.run_agent``) supervised with the
manager's ``stop_reason`` (budget exhaustion, model or tool violations, a dead or unreachable gateway), then, in a
``finally`` that cannot be skipped, stop, final read of the stop reason and ledger, and teardown. Docker networks are
NOT created or removed here: that is a separate, approval-gated step, and the manager only attaches the container.

Tested with fakes only; the behaviour against a real Docker daemon is unverified.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .agent_runner import AgentResult, AgentRunSpec, record_of, run_agent
from .container_exec import ExecError, ExecSession, quiesce, read_file, read_stop_reason, run_exec
from .container_policy import (GATEWAY_ENTRYPOINT, GATEWAY_KEEPALIVE_ARGS, PREFLIGHT_CIDR, READY_SCRIPT, GatewayContext, ExecContext,
                               PolicyViolation, gateway_tmpfs, harden, verify_inspect)
from . import ledger as ledger_mod
from .limits import GATEWAY_DRAIN_TIMEOUT_S
from .gateway_runner import (GatewayIntegrityError, gateway_exec_context, inject_and_verify_gateway, inject_verify_start_gateway,  # noqa: F401
                             start_gateway)
from .networks import NetworkError, verify_topology
from .proxy_lifecycle import ProxyManager, ProxySpec
from .seed import Seed

STOP_FILE = "/tmp/gateway.stop"
LEDGER_PATH = "/tmp/ledger.jsonl"
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class GatewayStartError(RuntimeError):
    """The gateway could not be brought to the verified-and-ready state."""


@dataclass(frozen=True)
class GatewaySpec:
    run_id: str
    image: str  # the pinned reference given to ``containers.create`` (see ``gateway_runner.load_gateway_image``)
    ctx: GatewayContext  # networks, pinned image ID, interpreter, uid:gid, tmpfs neutralisation, the real key (hidden)
    run_token: str = field(repr=False)  # the per-run token the agent presents; never the real key
    budget_usd: str
    code: Seed = field(repr=False)  # ``gateway_runner.build_gateway_component``
    proxy_endpoint: str = ""  # ``aiep.proxy.<run id>:<port>``: the ONLY way out of the gateway (``ProxySpec.endpoint``); required by policy
    max_concurrent: int = 2  # upstream requests in flight (AIEP_MAX_CONCURRENT); the M3 pilot uses 1. Bounds the budget overshoot; it is not a spend guarantee.
    port: int = 8080
    ready_timeout_s: float = 30.0
    start_timeout_s: float = 60.0

    def __post_init__(self) -> None:
        if not _RUN_ID.fullmatch(self.run_id):
            raise ValueError("run_id must be a plain identifier")
        if not (0 < self.port < 65536):
            raise ValueError("port out of range")
        if type(self.max_concurrent) is not int or not (1 <= self.max_concurrent <= 4):
            raise ValueError("max_concurrent must be an integer from 1 to 4")

    @property
    def name(self) -> str:
        return f"aiep.gateway.{self.run_id}"

    @property
    def url(self) -> str:
        return f"http://{self.name}:{self.port}"


def build_gateway_create_kwargs(spec: GatewaySpec, *, preflight: bool = False) -> dict[str, Any]:
    """Policy-checked kwargs for ``containers.create``. Raises PolicyViolation on anything off-policy.

    ``AIEP_LISTEN`` names the agent network's real subnet (``net:<cidr>:<port>``), so the gateway binds only its agent-network interface plus
    loopback. ``preflight=True`` (used by ``prepare``, which has no Docker) substitutes a placeholder subnet; a real create never does.
    """
    ctx = dataclasses.replace(spec.ctx, agent_cidr=PREFLIGHT_CIDR) if (preflight and spec.ctx.agent_cidr is None) else spec.ctx
    if ctx.agent_cidr is None:
        raise PolicyViolation("gateway: AIEP_LISTEN: the intended internal network's subnet has not been resolved from Docker")
    env = {
        "AIEP_UPSTREAM_API_KEY": ctx.real_key, "AIEP_RUN_TOKEN": spec.run_token, "AIEP_BUDGET_USD": spec.budget_usd,
        "AIEP_ALLOWED_HOSTS": f"{spec.name}:{spec.port}", "AIEP_LISTEN": f"net:{ctx.agent_cidr}:{spec.port}",
        "AIEP_UPSTREAM_PROXY": spec.proxy_endpoint, "AIEP_MAX_CONCURRENT": str(spec.max_concurrent),
        "AIEP_LEDGER_PATH": LEDGER_PATH, "AIEP_STOP_FILE": STOP_FILE,
        "PYTHONPATH": "/opt/aiep-gw/src", "PYTHONDONTWRITEBYTECODE": "1", "TZ": "UTC", "HOME": "/tmp",
    }
    raw = dict(
        image=spec.image, command=list(GATEWAY_KEEPALIVE_ARGS), entrypoint=list(GATEWAY_ENTRYPOINT), name=spec.name,
        user=ctx.user, detach=True, environment=env, network=ctx.agent_network,
        labels={"aiep.run_id": spec.run_id}, read_only=True, tmpfs=gateway_tmpfs(ctx),
    )
    return harden("gateway", raw, ctx)


class GatewayManager:
    """One gateway container, driven step by step. Every failure leaves it in a state ``teardown`` can clean up."""

    STATES = ("new", "created", "started", "verified", "ready", "stopped", "removed")

    def __init__(self, client: Any, spec: GatewaySpec, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, min_poll_interval: float = 1.0):
        self.client, self.spec, self.clock, self.sleep = client, spec, clock, sleep
        self.min_poll_interval = min_poll_interval
        self.state = "new"
        self.container: Any = None
        self.session: ExecSession | None = None
        self.ectx: ExecContext = gateway_exec_context(
            spec.ctx, readable_paths=frozenset({STOP_FILE, LEDGER_PATH}), forbidden_secrets=(spec.ctx.real_key,))
        self._reason: str | None = None
        self._last_poll: float | None = None
        self.errors: list[str] = []

    # ---- helpers
    def _need(self, *states: str) -> None:
        if self.state not in states:
            raise GatewayStartError(f"gateway is in state {self.state!r}; this step needs {' or '.join(states)}")

    @property
    def api(self) -> Any:
        return self.client.api

    # ---- steps
    def create(self) -> None:
        self._need("new")
        kwargs = build_gateway_create_kwargs(self.spec)
        image, command = kwargs.pop("image"), kwargs.pop("command")
        self.container = self.client.containers.create(image, command, **kwargs)
        self.state = "created"  # from here on a container exists and must be torn down
        self.client.networks.get(self.spec.ctx.proxy_network).connect(self.container)  # the proxy network: the only second network the gateway has
        verify_inspect("gateway", self.api.inspect_container(self.container.id), self.spec.ctx)

    def start(self) -> None:
        self._need("created")
        self.container.start()
        t0 = self.clock()
        while True:
            self.container.reload()
            if self.container.status == "running":
                break
            if self.container.status in ("exited", "dead") or self.clock() - t0 >= self.spec.start_timeout_s:
                raise GatewayStartError(f"gateway container did not reach running (status {self.container.status!r})")
            self.sleep(0.1)
        self.state = "started"

    def inject_verify_start(self) -> None:
        """Inject, verify against the manifest, and only then start the gateway process."""
        self._need("started")
        inject_and_verify_gateway(self.api, self.container.id, self.spec.code, self.spec.ctx, self.ectx)
        self.session = self._start_process()  # only after the files matched their manifest
        self.state = "verified"

    def _start_process(self) -> ExecSession:
        """The production start. An integration test may override ONLY this (to start the process through its Python-only test seam)."""
        return start_gateway(self.api, self.container.id, self.spec.ctx, self.ectx)

    def wait_ready(self) -> None:
        self._need("verified")
        t0 = self.clock()
        argv = [self.spec.ctx.python, "-I", "-c", READY_SCRIPT, f"127.0.0.1:{self.spec.port}"]
        while True:
            if self.session is not None and self.session.done():
                out = self.session.outcome(timed_out=True)
                tail = out.stderr[-300:].decode("utf-8", "replace")
                raise GatewayStartError(f"the gateway process exited before it was ready; stderr: {tail!r}")
            probe = run_exec(self.api, self.container.id, argv, ctx=self.ectx, workdir="/", timeout_s=5)
            if probe.exit_code == 0 and not probe.timed_out:
                self.state = "ready"
                return
            if self.clock() - t0 >= self.spec.ready_timeout_s:
                raise GatewayStartError("the gateway did not accept connections before the readiness deadline")
            self.sleep(0.2)

    def setup(self) -> None:
        """create, start, verify, ready. Raises on any failure; ``teardown`` must still be called."""
        self.create()
        self.start()
        self.inject_verify_start()
        self.wait_ready()

    # ---- supervision
    def stop_reason(self) -> str | None:
        """The agent supervisor's ``gateway_stopped`` callable: a stop reason, or None while the gateway is healthy.

        A dead or unreachable gateway counts as a stop (the run is no longer metered), never as 'fine'.
        The result is sticky and the check is rate-limited.
        """
        if self._reason is not None:
            return self._reason
        if self.state != "ready":
            return "gateway_unreachable"
        now = self.clock()
        if self._last_poll is not None and now - self._last_poll < self.min_poll_interval:
            return None
        self._last_poll = now
        try:
            reason = read_stop_reason(self.api, self.container.id, STOP_FILE, ctx=self.ectx)
        except Exception:
            reason = "gateway_died" if self._process_gone() else "gateway_unreachable"
        else:
            if reason is None and self._process_gone():
                reason = "gateway_died"
        self._reason = reason
        return reason

    def _process_gone(self) -> bool:
        if self.session is not None and self.session.done():
            return True
        try:
            self.container.reload()
            return self.container.status in ("exited", "dead")
        except Exception:
            return False

    # ---- ending
    def stop(self) -> None:
        """Kill every process of the gateway's user (PID 1 is immune) and close the exec socket."""
        if self.session is not None:
            self.session.close()
        if self.state not in ("started", "verified", "ready"):
            return  # never started (or already stopped): there is no process to kill
        try:
            quiesce(self.api, self.container.id, ctx=self.ectx)
        except Exception as e:
            self.errors.append(f"stop: {type(e).__name__}: {e}")
        self.state = "stopped"

    def final_stop_reason(self) -> str | None:
        if self._reason is not None:
            return self._reason
        try:
            return read_stop_reason(self.api, self.container.id, STOP_FILE, ctx=self.ectx)
        except Exception as e:
            self.errors.append(f"final stop reason: {type(e).__name__}: {e}")
            return None

    def read_ledger(self, *, record_error: bool = True) -> bytes | None:
        try:
            return read_file(self.api, self.container.id, LEDGER_PATH, ctx=self.ectx)
        except Exception as e:
            if record_error:
                self.errors.append(f"ledger: {type(e).__name__}: {e}")
            return None

    def process_gone(self) -> bool:
        """True if the gateway process (or its container) is no longer running, so nothing more can be written to the ledger."""
        return self._process_gone()

    def teardown(self) -> tuple[bool, str | None]:
        """Always kill and remove (with anonymous volumes). Returns (removed, error); ordinary errors are reported.

        ``remove`` is attempted even if ``kill`` is cut short by a ``BaseException`` such as ``KeyboardInterrupt``
        (``remove(force=True)`` kills the container itself); that exception then continues to propagate.
        """
        if self.session is not None:
            self.session.close()
        if self.container is None:
            return True, None  # nothing was ever created
        removed, error = False, None
        try:
            try:
                self.container.kill()
            except Exception:
                pass  # already stopped
        finally:
            try:
                self.container.remove(force=True, v=True)
                self.state, removed = "removed", True
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
        return removed, error


@dataclass
class LifecycleResult:
    status: str  # "completed" | "gateway_failed" | "proxy_failed"
    proxy_error: str | None = None
    proxy_state: str = "new"
    proxy_removed: bool = False
    proxy_cleanup_error: str | None = None
    proxy_errors: list[str] = field(default_factory=list)
    topology: dict[str, Any] | None = None  # the verified attachments (``networks.verify_topology`` evidence) taken before the agent started
    gateway_error: str | None = None
    agent: AgentResult | None = None
    gateway_stop_reason: str | None = None
    ledger: bytes | None = field(default=None, repr=False)
    gateway_removed: bool = False
    gateway_cleanup_error: str | None = None
    gateway_errors: list[str] = field(default_factory=list)
    gateway_state: str = "new"
    # The gateway's accounting completeness, from the final ledger (see ``_drain_and_audit``): ``complete`` is True only if every request the
    # gateway started forwarding has a finishing line. None means the gateway never started (nothing could have been billed).
    accounting: dict[str, Any] | None = None


def lifecycle_record(r: LifecycleResult) -> dict[str, Any]:
    """JSON-safe run summary: the agent record plus gateway outcomes and the ledger's hash and size (not its body)."""
    return {
        "status": r.status, "gateway_error": r.gateway_error, "gateway_state": r.gateway_state,
        "gateway_stop_reason": r.gateway_stop_reason, "gateway_removed": r.gateway_removed,
        "gateway_cleanup_error": r.gateway_cleanup_error, "gateway_errors": r.gateway_errors,
        "accounting": r.accounting,
        "proxy": {"state": r.proxy_state, "error": r.proxy_error, "removed": r.proxy_removed, "cleanup_error": r.proxy_cleanup_error,
                  "errors": r.proxy_errors},
        "topology": r.topology,
        "ledger": None if r.ledger is None else {"bytes": len(r.ledger), "sha256": hashlib.sha256(r.ledger).hexdigest()},
        "agent": None if r.agent is None else record_of(r.agent),
    }


_STARTED_STATES = ("started", "verified", "ready", "stopped")


def _drain_and_audit(gw: GatewayManager, result: LifecycleResult, *, clock: Callable[[], float], sleep: Callable[[float], None],
                     timeout_s: float, poll_s: float) -> dict[str, Any]:
    """Wait (bounded) for requests the gateway is still serving to be written to its ledger. Returns the drain's own facts.

    The agent is already gone, so no new request can start; what remains is a request the gateway is relaying upstream (it keeps reading
    after a client disconnects, precisely so that it can meter it). Polling the ledger for ``forwarding`` lines with no finishing line
    ends as soon as there are none, when the gateway process is gone (nothing can finish any more), or at the deadline; the wait is
    therefore at most ``timeout_s`` plus one ledger read, even when a request hangs.
    """
    t0 = clock()
    last: dict[str, Any] | None = None
    while True:
        last = ledger_mod.audit(gw.read_ledger(record_error=False))
        if last["complete"]:
            return {"drained": True, "waited_s": round(clock() - t0, 3), "timed_out": False, "timeout_s": timeout_s}
        if not last["readable"]:  # waiting cannot make an unreadable ledger readable; the final audit records it as incomplete
            return {"drained": False, "waited_s": round(clock() - t0, 3), "timed_out": False, "timeout_s": timeout_s,
                    "note": "the ledger could not be read during the drain"}
        if gw.process_gone():
            return {"drained": False, "waited_s": round(clock() - t0, 3), "timed_out": False, "timeout_s": timeout_s,
                    "note": "the gateway process is gone: nothing more can be recorded"}
        if clock() - t0 >= timeout_s:
            return {"drained": False, "waited_s": round(clock() - t0, 3), "timed_out": True, "timeout_s": timeout_s}
        sleep(poll_s)


def _finish_gateway(gw: GatewayManager, result: LifecycleResult, *, clock: Callable[[], float] = time.monotonic,
                    sleep: Callable[[float], None] = time.sleep, drain_timeout_s: float = GATEWAY_DRAIN_TIMEOUT_S,
                    drain_poll_s: float = 1.0) -> None:
    """Stop the gateway, collect its final stop reason and ledger, then ALWAYS tear it down.

    The agent container is already gone by now (``run_agent`` removes it in its own ``finally`` before returning or
    raising), so the order is agent first, gateway second. Each collection step is attempted independently: an ``Exception``
    in one is recorded in ``gw.errors`` (and so in the result), never hidden, and never prevents the next step or the
    teardown. The teardown sits in a ``finally`` of its own, so it also runs when the steps above are cut short by a
    ``BaseException`` such as ``KeyboardInterrupt``; that exception then continues to propagate (with any exception that was
    already in flight kept as its ``__context__``). Ordinary exceptions never replace the original outcome or exception.
    """
    def attempt(what: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as e:
            gw.errors.append(f"cleanup {what}: {type(e).__name__}: {e}")
            return None

    try:
        drain: dict[str, Any] | None = None
        if gw.state in ("started", "verified", "ready"):
            # Bounded graceful drain BEFORE the gateway is stopped: stopping kills the process, and a request still being relayed would then be
            # billed upstream and never recorded.
            drain = attempt("drain", lambda: _drain_and_audit(gw, result, clock=clock, sleep=sleep, timeout_s=drain_timeout_s,
                                                              poll_s=drain_poll_s))
        attempt("stop", gw.stop)
        if gw.state in _STARTED_STATES:  # only a started gateway has a stop file or a ledger to read
            result.gateway_stop_reason = attempt("final stop reason", gw.final_stop_reason)
            result.ledger = attempt("ledger", gw.read_ledger)
            # The accounting verdict comes from the FINAL ledger (read after the stop), never from the drain's last look.
            final = ledger_mod.audit(result.ledger)
            result.accounting = {**final, "drain": drain,
                                 "exact": final["complete"],
                                 "note": ("every request the gateway forwarded has a finishing ledger line" if final["complete"] else
                                          "INCOMPLETE: usage may have been incurred upstream that this ledger does not contain "
                                          "(unresolved requests, an unreadable ledger or unparseable lines); the measured cost is a lower bound")}
            if not final["complete"]:
                result.accounting["unresolved_max_output_tokens"] = sum(
                    u["requested_max_tokens"] for u in final["unresolved"] if isinstance(u.get("requested_max_tokens"), int))
    finally:
        try:
            result.gateway_removed, result.gateway_cleanup_error = gw.teardown()
        except Exception as e:  # teardown() is built not to raise; if it does, say so instead of hiding it
            result.gateway_removed, result.gateway_cleanup_error = False, f"{type(e).__name__}: {e}"
        finally:
            result.gateway_state, result.gateway_errors = gw.state, list(gw.errors)


def _finish_proxy(px: ProxyManager, result: LifecycleResult) -> None:
    """Stop and ALWAYS remove the proxy (it is removed after the gateway, which is its only client). Never raises an ordinary exception."""
    try:
        try:
            px.stop()
        except Exception as e:
            px.errors.append(f"cleanup proxy stop: {type(e).__name__}: {e}")
    finally:
        try:
            result.proxy_removed, result.proxy_cleanup_error = px.teardown()
        except Exception as e:  # teardown() is built not to raise; if it does, say so instead of hiding it
            result.proxy_removed, result.proxy_cleanup_error = False, f"{type(e).__name__}: {e}"
        finally:
            result.proxy_state, result.proxy_errors = px.state, list(px.errors)


def run_with_gateway(client: Any, gspec: GatewaySpec, aspec: AgentRunSpec, *, pspec: ProxySpec,
                     run_agent_fn: Callable[..., AgentResult] = run_agent,
                     clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                     poll_interval: float = 0.5, min_poll_interval: float = 1.0,
                     drain_timeout_s: float = GATEWAY_DRAIN_TIMEOUT_S, drain_poll_s: float = 1.0) -> LifecycleResult:
    """Bring the proxy, then the gateway, to verified-and-ready, run the agent under their supervision, always clean all three up."""
    # consistency between the specs, checked before anything is created
    if aspec.gateway_url != gspec.url:
        raise ValueError("the agent's gateway_url is not the gateway's own URL")
    if aspec.agent_network != gspec.ctx.agent_network:
        raise ValueError("the agent and the gateway are not on the same agent network")
    if aspec.run_token != gspec.run_token:
        raise ValueError("the agent's token is not the token the gateway enforces")
    if not gspec.ctx.real_key:
        raise ValueError("the gateway context has no real key to hold")
    if gspec.proxy_endpoint != pspec.endpoint:
        raise ValueError("the gateway's proxy endpoint is not the proxy's own endpoint")
    if gspec.ctx.proxy_network != pspec.ctx.proxy_network or pspec.run_id != gspec.run_id:
        raise ValueError("the gateway and the proxy are not on the same proxy network and run")
    if (gspec.ctx.uid, gspec.ctx.gid) == (pspec.ctx.uid, pspec.ctx.gid):
        raise ValueError("the gateway and the proxy must not share a uid:gid")
    missing = tuple(x for x in (gspec.ctx.real_key, gspec.run_token) if x not in pspec.ctx.forbidden_secrets)
    if missing:  # the proxy's policy then refuses either value anywhere in its own create request or created container
        pspec = dataclasses.replace(pspec, ctx=dataclasses.replace(pspec.ctx, forbidden_secrets=(*pspec.ctx.forbidden_secrets, *missing)))
    if gspec.ctx.real_key not in aspec.forbidden_secrets:
        aspec = dataclasses.replace(aspec, forbidden_secrets=(*aspec.forbidden_secrets, gspec.ctx.real_key))

    px = ProxyManager(client, pspec, clock=clock, sleep=sleep)
    gw = GatewayManager(client, gspec, clock=clock, sleep=sleep, min_poll_interval=min_poll_interval)
    result = LifecycleResult(status="gateway_failed")
    proxy_reason: list[str] = []

    def stop_reason() -> str | None:
        """Supervision: a dead proxy ends the run (invalid) just like a dead gateway: without it nothing can be metered or reached."""
        if proxy_reason:
            return proxy_reason[0]
        if not px.alive():
            proxy_reason.append("proxy_died")
            return proxy_reason[0]
        return gw.stop_reason()

    try:
        try:
            try:
                px.setup()
            except Exception as e:
                result.status, result.proxy_error = "proxy_failed", f"{type(e).__name__}: {e}"
                return result
            try:
                gw.setup()
                result.topology = verify_topology(client, {"proxy": px.container.id, "gateway": gw.container.id})
            except Exception as e:  # recorded with its type; cleanup below is unconditional
                result.gateway_error = f"{type(e).__name__}: {e}"
                return result
            def attachment_check(agent_id: str) -> None:
                """Called by ``run_agent`` once the agent is RUNNING: live membership of all three networks with the agent's real ID."""
                verify_topology(client, {"proxy": px.container.id, "gateway": gw.container.id, "agent": agent_id})
            result.agent = run_agent_fn(client, aspec, stop_reason, poll_interval=poll_interval, clock=clock, sleep=sleep,
                                        attachment_check=attachment_check)
            result.status = "completed"
            return result
        finally:
            _finish_gateway(gw, result, clock=clock, sleep=sleep, drain_timeout_s=drain_timeout_s, drain_poll_s=drain_poll_s)
    finally:
        _finish_proxy(px, result)

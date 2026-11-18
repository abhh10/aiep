"""Run one agent container under externally enforced limits.

The orchestrator (this process, outside the container) owns the wall-clock timer,
watches the gateway's stop state, and always tears the container down. The
container's own configuration comes only from ``container_policy``; the created
container is re-checked with ``docker inspect`` before it is started.

The Docker client is injected, so everything here is tested with a fake.
Phase 2 supplies the real client, network and image.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .container_policy import AgentContext, harden, verify_inspect

DEFAULT_MAX_LOG_BYTES = 5 * 1024 * 1024
STATUS_EXITED, STATUS_TIMEOUT, STATUS_GATEWAY_STOPPED = "exited", "timeout", "gateway_stopped"


@dataclass(frozen=True)
class AgentRunSpec:
    run_id: str
    image: str
    command: list[str]
    gateway_url: str
    agent_network: str
    pinned_model: str
    run_token: str = field(repr=False)
    wall_clock_s: float = 1800.0
    user: str = "agent"
    working_dir: str = "/testbed"
    home: str = "/home/agent"
    tmpfs: dict[str, str] = field(default_factory=lambda: {
        "/tmp": "rw,nosuid,nodev,size=256m",
        "/home/agent": "rw,nosuid,nodev,size=256m,mode=0700",
    })
    forbidden_secrets: tuple[str, ...] = ()  # e.g. the real API key: must never reach the container
    image_id: str | None = None  # expected image ID; Phase 2 always sets it


@dataclass
class AgentResult:
    status: str
    exit_code: int | None
    duration_s: float
    stop_reason: str | None
    logs: bytes
    container_id: str
    removed: bool
    cleanup_error: str | None = None


def context_for(spec: AgentRunSpec) -> AgentContext:
    return AgentContext(
        gateway_url=spec.gateway_url,
        agent_network=spec.agent_network,
        pinned_model=spec.pinned_model,
        forbidden_secrets=spec.forbidden_secrets,
        image_id=spec.image_id,
    )


def build_create_kwargs(spec: AgentRunSpec) -> dict[str, Any]:
    """Policy-checked kwargs for ``containers.create``. Raises PolicyViolation on anything off-policy."""
    if not isinstance(spec.command, list) or not all(isinstance(c, str) for c in spec.command) or not spec.command:
        raise ValueError("command must be a non-empty list of strings (no shell string)")
    m = spec.pinned_model
    env = {
        "ANTHROPIC_BASE_URL": spec.gateway_url,
        "ANTHROPIC_AUTH_TOKEN": spec.run_token,
        # one model for everything, background and subagent calls included
        "ANTHROPIC_MODEL": m,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": m,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": m,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": m,
        "ANTHROPIC_DEFAULT_FABLE_MODEL": m,
        "ANTHROPIC_SMALL_FAST_MODEL": m,
        "CLAUDE_CODE_SUBAGENT_MODEL": m,
        "CLAUDE_CODE_AUTO_MODE_MODEL": m,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_UPDATES": "1",
        "HOME": spec.home,
        "TZ": "UTC",
    }
    raw = dict(
        image=spec.image,
        command=list(spec.command),
        name=f"aiep.agent.{spec.run_id}",
        user=spec.user,
        detach=True,
        environment=env,
        working_dir=spec.working_dir,
        network=spec.agent_network,
        labels={"aiep.run_id": spec.run_id},
        read_only=True,
        tmpfs=dict(spec.tmpfs),
    )
    return harden("agent", raw, context_for(spec))


def run_agent(
    client: Any,
    spec: AgentRunSpec,
    gateway_stopped: Callable[[], str | None],
    *,
    poll_interval: float = 0.5,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
) -> AgentResult:
    """Create, verify, start and supervise the agent container; always remove it.

    ``gateway_stopped`` returns the gateway's stop reason (or None). A stopped gateway,
    for example after a model violation, ends the run immediately.
    """
    kwargs = build_create_kwargs(spec)
    image, command = kwargs.pop("image"), kwargs.pop("command")
    container = client.containers.create(image, command, **kwargs)
    status, exit_code, stop_reason, logs = STATUS_EXITED, None, None, b""
    t0 = clock()
    try:
        info = client.api.inspect_container(container.id)
        verify_inspect("agent", info, context_for(spec))  # re-check what Docker actually built
        container.start()
        t0 = clock()
        while True:
            container.reload()
            if container.status in ("exited", "dead"):
                break
            reason = gateway_stopped()
            if reason:
                status, stop_reason = STATUS_GATEWAY_STOPPED, reason
                break
            if clock() - t0 >= spec.wall_clock_s:
                status = STATUS_TIMEOUT
                break
            sleep(poll_interval)
        if status != STATUS_EXITED:
            _kill(container)
        container.reload()
        exit_code = (container.attrs.get("State") or {}).get("ExitCode")
        logs = _tail(container.logs(stdout=True, stderr=True), max_log_bytes)
        duration = clock() - t0
    except BaseException:
        _teardown(container)  # best effort; the original error is what matters
        raise
    removed, err = _teardown(container)
    return AgentResult(status=status, exit_code=exit_code, duration_s=duration, stop_reason=stop_reason,
                       logs=logs, container_id=container.id, removed=removed, cleanup_error=err)


def _kill(container: Any) -> None:
    try:
        container.kill()
    except Exception:
        pass  # already stopped; teardown's forced remove still applies


def _teardown(container: Any) -> tuple[bool, str | None]:
    try:
        container.remove(force=True, v=True)
        return True, None
    except Exception as e:  # reported, not swallowed: the caller sees removed=False
        return False, f"{type(e).__name__}: {e}"


def _tail(data: bytes | None, limit: int) -> bytes:
    data = data or b""
    return data if len(data) <= limit else data[-limit:]

"""Run one agent container: the orchestrator drives everything through policy-checked ``docker exec``.

Lifecycle (the orchestrator, outside the container, is the only authority at every step):

 1. create  a read-only-root container whose PID 1 is ``sleep`` (policy-checked kwargs), then re-check the
            created container with ``docker inspect`` (limits, network, tmpfs, user, pinned image ID);
 2. start    and wait until it is running;
 3. inject   the seed into the ``/testbed`` tmpfs and the trusted components (Claude Code) into ``/opt/aiep``
            by streaming tars over ``exec`` stdin (``put_archive`` is unusable with a read-only root);
 4. verify   what was extracted against the recorded manifests, entry by entry and by SHA-256;
 5. run      Claude as an exec, delivering the task on its **stdin** (never argv, env or a file);
 6. supervise  the wall clock, the gateway's stop state, and the container itself;
 7. quiesce  ``kill -KILL -1`` as the agent user (PID 1 is immune), then confirm nothing else is alive;
 8. extract  the patch with git running **inside the container**, diffed against the seed's own synthetic
            HEAD (recorded in the seed manifest), not the dataset's ``base_commit``;
 9. tear down: kill and remove the container (and its anonymous volumes), always.

Because the workspace is a tmpfs, it vanishes when the container stops: a crash or OOM kill loses the
patch. That outcome is recorded (``container_died``/``oom_killed``), never hidden.

The Docker client is injected; this module is tested with fakes. Real behaviour is a Phase 2 validation item.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .agent_env import IDENTITY_SCRIPT, check_home_overlaps, check_identity, experiment_env
from .container_exec import (REGISTERED_SCRIPTS, ExecError, ExecSession, exec_env, git_exec_env, inject_tar,
                             list_tree, quiesce, run_exec)
from .container_policy import (AGENT_IPC_MODE, AGENT_KEEPALIVE_COMMAND, AGENT_SHM_DEFAULT, AgentContext, AgentIdentity,
                               ExecContext, harden, harden_exec, validate_interpreter_path, verify_inspect)
from .patch import HARDENED_GIT, MAX_PATCH_BYTES, PatchResult, container_git_runner, extract_patch
from .seed import Seed, verify_archive, verify_listing

STATUS_EXITED, STATUS_TIMEOUT = "exited", "timeout"
STATUS_GATEWAY_STOPPED, STATUS_CONTAINER_DIED, STATUS_INVALID = "gateway_stopped", "container_died", "invalid"
# Gateway stop reasons that make the run scientifically invalid (not just a limit being reached).
# "gateway_unreachable" / "gateway_died" are produced by the lifecycle manager: without the gateway the
# run is unmetered, so it cannot count either.
INVALID_STOP_REASONS = frozenset({"model_violation", "tool_violation", "metering_failure",
                                  "gateway_unreachable", "gateway_died"})
CONFIG_LABELS = frozenset({"A", "B"})
MAX_TASK_BYTES = 256 * 1024
MAX_CLAUDE_OUTPUT = 32 * 1024 * 1024


@dataclass(frozen=True)
class AgentRunSpec:
    run_id: str
    image: str
    gateway_url: str
    agent_network: str
    pinned_model: str
    run_token: str = field(repr=False)
    seed: Seed = field(repr=False)  # validated repository seed (HEAD = the image's synthetic commit)
    components: Seed = field(repr=False)  # trusted files for /opt/aiep, e.g. the Claude Code binary
    python: str  # absolute interpreter path inside the instance image, used to verify the injected trees.
    # Explicit on purpose: there is no default and no fallback. For the SWE-bench instance images the
    # recorded evidence is the miniconda install (/opt/miniconda3/bin/python); it is UNVERIFIED until
    # a Docker step runs it, and /usr/bin/python3 must not be assumed to exist.
    identity: AgentIdentity  # the verified non-root account (see config/agent_identity.json); no default
    config_label: str  # "A" or "B": recorded only. It must never influence the environment (see agent_env)
    task_text: str = field(repr=False)  # the issue (or smoke prompt): delivered on stdin only
    claude_args: tuple[str, ...] = ()  # flags only; the task never goes here
    image_id: str | None = None  # expected image ID; Phase 2 always sets it
    wall_clock_s: float = 1800.0
    # PLACEHOLDER sizes, to be set from measurement (seed: 17.4 MiB tmpfs-accurate for pytest-5227)
    testbed_size: str = "192m"
    tmp_size: str = "128m"
    home_size: str = "128m"
    components_size: str = "256m"
    # Docker's own /dev/shm, configured explicitly (see container_policy): private IPC and a pinned size, never left to defaults
    shm_size: str = AGENT_SHM_DEFAULT
    ipc_mode: str = AGENT_IPC_MODE
    max_patch_bytes: int = MAX_PATCH_BYTES
    forbidden_secrets: tuple[str, ...] = ()  # e.g. the real API key: must never reach the container

    @property
    def user(self) -> str:
        return self.identity.user


@dataclass
class AgentResult:
    status: str
    valid: bool
    invalid_reason: str | None
    stop_reason: str | None
    claude_exit_code: int | None
    duration_s: float
    claude_stdout: bytes = field(repr=False, default=b"")
    claude_stderr: bytes = field(repr=False, default=b"")
    claude_output_truncated: bool = False
    patch: PatchResult | None = None
    patch_error: str | None = None
    seed_head: str = ""
    seed_sha256: str = ""
    components_sha256: str = ""
    survivors_after_quiesce: list[str] = field(default_factory=list)
    oom_killed: bool = False
    container_id: str = ""
    removed: bool = False
    cleanup_error: str | None = None
    timeline: dict[str, float] = field(default_factory=dict)
    config_label: str = ""
    account: str = ""  # name:uid:gid of the verified identity the run used


def record_of(result: AgentResult) -> dict[str, Any]:
    """A JSON-safe summary for the run record: hashes, sizes, outcomes. No task text, no Claude output, no patch body."""
    patch = result.patch
    return {
        "status": result.status, "valid": result.valid, "invalid_reason": result.invalid_reason,
        "stop_reason": result.stop_reason, "claude_exit_code": result.claude_exit_code,
        "duration_s": round(result.duration_s, 3), "claude_stdout_bytes": len(result.claude_stdout),
        "claude_stderr_bytes": len(result.claude_stderr), "claude_output_truncated": result.claude_output_truncated,
        "patch": None if patch is None else {"status": patch.status, "sha256": patch.sha256, "size": patch.size,
                                             "reasons": patch.reasons},
        "patch_error": result.patch_error,
        "seed": {"head": result.seed_head, "archive_sha256": result.seed_sha256},
        "components_sha256": result.components_sha256,
        "survivors_after_quiesce": result.survivors_after_quiesce, "oom_killed": result.oom_killed,
        "container_id": result.container_id, "removed": result.removed, "cleanup_error": result.cleanup_error,
        "timeline_s": {k: round(v, 3) for k, v in result.timeline.items()},
        "config_label": result.config_label, "account": result.account,
    }


def agent_context(spec: AgentRunSpec) -> AgentContext:
    return AgentContext(gateway_url=spec.gateway_url, agent_network=spec.agent_network,
                        pinned_model=spec.pinned_model, identity=spec.identity,
                        forbidden_secrets=spec.forbidden_secrets, image_id=spec.image_id,
                        shm_size=spec.shm_size, ipc_mode=spec.ipc_mode)


def exec_context(spec: AgentRunSpec) -> ExecContext:
    secrets = list(spec.forbidden_secrets)
    if len(spec.task_text) >= 20:  # the task may only ever travel on stdin
        secrets.append(spec.task_text[:64])
    return ExecContext(user=spec.user, allowed_scripts=REGISTERED_SCRIPTS, forbidden_secrets=tuple(secrets),
                       python=spec.python, env=experiment_env(spec.identity))


def tmpfs_for(spec: AgentRunSpec) -> dict[str, str]:
    own = f"uid={spec.identity.uid},gid={spec.identity.gid}"
    return {
        "/testbed": f"rw,exec,nosuid,nodev,size={spec.testbed_size},{own},mode=0755",
        "/opt/aiep": f"rw,exec,nosuid,nodev,size={spec.components_size},{own},mode=0755",
        "/tmp": f"rw,nosuid,nodev,size={spec.tmp_size},{own},mode=0700",
        spec.identity.home: f"rw,nosuid,nodev,size={spec.home_size},{own},mode=0700",
    }


def build_create_kwargs(spec: AgentRunSpec) -> dict[str, Any]:
    """Policy-checked kwargs for ``containers.create``. Raises PolicyViolation on anything off-policy."""
    m = spec.pinned_model
    env = {
        "ANTHROPIC_BASE_URL": spec.gateway_url,
        "ANTHROPIC_AUTH_TOKEN": spec.run_token,
        # one model for everything, background and subagent calls included
        "ANTHROPIC_MODEL": m, "ANTHROPIC_DEFAULT_HAIKU_MODEL": m, "ANTHROPIC_DEFAULT_SONNET_MODEL": m,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": m, "ANTHROPIC_DEFAULT_FABLE_MODEL": m, "ANTHROPIC_SMALL_FAST_MODEL": m,
        "CLAUDE_CODE_SUBAGENT_MODEL": m, "CLAUDE_CODE_AUTO_MODE_MODEL": m,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_UPDATES": "1",
    }
    # the same mapping every exec of the run receives, so a plain `docker exec` sees it too
    env.update({k: v for k, v in experiment_env(spec.identity).items() if k != "LC_ALL"})
    raw = dict(
        image=spec.image,
        command=list(AGENT_KEEPALIVE_COMMAND),
        name=f"aiep.agent.{spec.run_id}",
        user=spec.user,
        detach=True,
        environment=env,
        working_dir="/testbed",
        network=spec.agent_network,
        labels={"aiep.run_id": spec.run_id},
        read_only=True,
        tmpfs=tmpfs_for(spec),
        shm_size=spec.shm_size,
        ipc_mode=spec.ipc_mode,
    )
    return harden("agent", raw, agent_context(spec))


def _validate_spec(spec: AgentRunSpec) -> bytes:
    validate_interpreter_path(spec.python, "agent")
    check_home_overlaps(spec.identity, spec.python)
    if spec.config_label not in CONFIG_LABELS:
        raise ValueError(f"config_label must be one of {sorted(CONFIG_LABELS)}")
    task = spec.task_text.encode("utf-8")
    if not task.strip():
        raise ValueError("the task text is empty")
    if len(task) > MAX_TASK_BYTES:
        raise ValueError(f"the task text is {len(task)} bytes; the limit is {MAX_TASK_BYTES}")
    if spec.seed.manifest.head is None or len(spec.seed.manifest.head) != 40:
        raise ValueError("the seed has no HEAD commit recorded")
    verify_archive(spec.seed)  # the in-memory tars still match their manifests
    verify_archive(spec.components)
    return task


def run_agent(
    client: Any,
    spec: AgentRunSpec,
    gateway_stopped: Callable[[], str | None],
    *,
    poll_interval: float = 0.5,
    start_timeout_s: float = 60.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> AgentResult:
    """Create, populate, run, quiesce, extract from, and always remove one agent container."""
    task = _validate_spec(spec)
    kwargs = build_create_kwargs(spec)
    image, command = kwargs.pop("image"), kwargs.pop("command")
    t_begin = clock()
    # Everything that could raise is built BEFORE the container exists, so nothing can fail between create and the
    # try/finally that removes it.
    result = AgentResult(status=STATUS_INVALID, valid=False, invalid_reason="not finished", stop_reason=None,
                         claude_exit_code=None, duration_s=0.0, seed_head=spec.seed.head,
                         seed_sha256=spec.seed.manifest.archive_sha256,
                         components_sha256=spec.components.manifest.archive_sha256, container_id="",
                         config_label=spec.config_label,
                         account=f"{spec.identity.name}:{spec.identity.uid}:{spec.identity.gid}")
    ectx = exec_context(spec)
    container = client.containers.create(image, command, **kwargs)
    result.container_id = container.id
    api = client.api
    session: ExecSession | None = None
    try:
        verify_inspect("agent", api.inspect_container(container.id), agent_context(spec))
        container.start()
        _wait_running(container, clock, sleep, start_timeout_s)
        result.timeline["running"] = clock() - t_begin

        # 2b. the configured account must exist in the image exactly as configured (uid, gid, passwd entry, home)
        probe = run_exec(api, container.id, ["/bin/sh", "-c", IDENTITY_SCRIPT], ctx=ectx, workdir="/", timeout_s=30)
        problems = check_identity(probe.stdout, spec.identity) if not (probe.timed_out or probe.truncated) else ["probe failed"]
        if problems:
            result.invalid_reason = f"agent identity check failed: {'; '.join(problems)}"
            return result
        result.timeline["identity_ok"] = clock() - t_begin

        # 3-4. inject and verify (before the agent exists, so nothing the agent does can interfere)
        for seed, dest in ((spec.seed, "/testbed"), (spec.components, "/opt/aiep")):
            inject_tar(api, container.id, seed.tar, dest, ctx=ectx)
            problems = verify_listing(seed.manifest, list_tree(api, container.id, dest, ctx=ectx))
            if problems:
                result.invalid_reason = f"{dest} does not match its manifest: {'; '.join(problems[:5])}"
                return result
        result.timeline["injected"] = clock() - t_begin

        # 5. run Claude; the task is on stdin only
        request = harden_exec(["/opt/aiep/claude", *spec.claude_args], user=spec.user, workdir="/testbed",
                              env=exec_env(ectx), ctx=ectx)
        session = ExecSession(api, container.id, request, stdin=task, max_stdout=MAX_CLAUDE_OUTPUT,
                              max_stderr=MAX_CLAUDE_OUTPUT).start()
        t_claude = clock()
        result.timeline["claude_started"] = t_claude - t_begin

        # 6. supervise
        status, stop_reason = _supervise(container, session, gateway_stopped, spec.wall_clock_s, t_claude,
                                         poll_interval, clock, sleep)
        result.status, result.stop_reason = status, stop_reason
        result.duration_s = clock() - t_claude
        result.timeline["claude_ended"] = clock() - t_begin
        if status == STATUS_CONTAINER_DIED:
            result.oom_killed = _oom_killed(container)
            result.valid, result.invalid_reason = True, None
            result.patch_error = "the container died, so its tmpfs workspace is gone"
            return result
        out = session.outcome(timed_out=status != STATUS_EXITED)
        if status != STATUS_EXITED:
            session.cancel()
        result.claude_exit_code = out.exit_code
        result.claude_stdout, result.claude_stderr, result.claude_output_truncated = out.stdout, out.stderr, out.truncated

        if status == STATUS_GATEWAY_STOPPED and stop_reason in INVALID_STOP_REASONS:
            result.invalid_reason = f"gateway stopped the run: {stop_reason}"
            return result  # no extraction: this run does not count
        result.valid, result.invalid_reason = True, None

        # 7. quiesce
        survivors = quiesce(api, container.id, ctx=ectx)
        if survivors:
            survivors = quiesce(api, container.id, ctx=ectx)
        result.survivors_after_quiesce = survivors
        result.timeline["quiesced"] = clock() - t_begin

        # 8. extract, inside the container, against the seed's own HEAD
        try:
            _require_seed_commit(api, container.id, spec, ectx)
            exec_git = _git_exec(api, container.id, ectx, spec.max_patch_bytes)
            result.patch = extract_patch(container_git_runner(exec_git), spec.seed.head, spec.max_patch_bytes)
        except (ExecError, RuntimeError) as e:
            result.patch_error = f"{type(e).__name__}: {e}"
        result.timeline["extracted"] = clock() - t_begin
        return result
    finally:
        if session is not None:
            session.close()  # e.g. the container died before the output ended: never leave the socket open
        result.removed, result.cleanup_error = _teardown(container)


def _wait_running(container: Any, clock: Callable[[], float], sleep: Callable[[float], None], timeout_s: float) -> None:
    t0 = clock()
    while True:
        container.reload()
        if container.status == "running":
            return
        if container.status in ("exited", "dead") or clock() - t0 >= timeout_s:
            raise RuntimeError(f"container did not reach the running state (status {container.status!r})")
        sleep(0.1)


def _supervise(container, session, gateway_stopped, wall_clock_s, t_claude, poll_interval, clock, sleep):
    while True:
        if session.done():
            return STATUS_EXITED, None
        container.reload()
        if container.status in ("exited", "dead"):
            return STATUS_CONTAINER_DIED, None
        reason = gateway_stopped()
        if reason:
            return STATUS_GATEWAY_STOPPED, reason
        if clock() - t_claude >= wall_clock_s:
            return STATUS_TIMEOUT, None
        session.wait(poll_interval)  # returns early the moment the exec's output ends


def _oom_killed(container: Any) -> bool:
    try:
        container.reload()
        return bool((container.attrs.get("State") or {}).get("OOMKilled"))
    except Exception:
        return False


def _git_exec(api: Any, container_id: str, ectx: ExecContext, max_patch_bytes: int):
    def exec_git(argv: list[str]):
        return run_exec(api, container_id, argv, ctx=ectx, workdir="/testbed", env=git_exec_env(ectx), timeout_s=120,
                        max_stdout=max_patch_bytes + 1024 * 1024)
    return exec_git


def _require_seed_commit(api: Any, container_id: str, spec: AgentRunSpec, ectx: ExecContext) -> None:
    """The seed's HEAD commit must still exist in the container's repository; otherwise there is no valid base."""
    out = run_exec(api, container_id, ["/usr/bin/git", *HARDENED_GIT, "cat-file", "-e", f"{spec.seed.head}^{{commit}}"],
                   ctx=ectx, workdir="/testbed", env=git_exec_env(ectx), timeout_s=60)
    if out.exit_code != 0 or out.timed_out:
        raise ExecError("the seed's HEAD commit is missing from the container's repository")


def _teardown(container: Any) -> tuple[bool, str | None]:
    try:
        container.kill()
    except Exception:
        pass  # already stopped
    try:
        container.remove(force=True, v=True)  # v=True also removes any anonymous volume
        return True, None
    except Exception as e:  # reported, not swallowed
        return False, f"{type(e).__name__}: {e}"

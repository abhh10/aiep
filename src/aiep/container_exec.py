"""Bounded, policy-checked ``docker exec`` into agent and gateway containers.

Everything the orchestrator does inside a container goes through here:

* each request is validated by ``container_policy.harden_exec`` (absolute-path binaries, fixed argument
  shapes, a registered-script allowlist, a non-root user, an environment allowlist);
* standard input is streamed over the hijacked exec socket, so the issue text and injected archives
  never appear in argv, environment variables or ``docker inspect``;
* output is decoded from Docker's multiplexed stream with hard caps, and every wait has a deadline.

The Docker API object is injected (``client.api``), so this module is tested with socket-pair fakes.
Real behaviour of Docker Desktop's hijacked exec sockets is a Phase 2 validation item.
"""

from __future__ import annotations

import errno
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .agent_env import IDENTITY_SCRIPT
from .container_policy import READY_SCRIPT, ExecContext, PolicyViolation, harden_exec
from .seed import VERIFY_SCRIPT

QUIESCE_SCRIPT = "kill -KILL -1"
LIST_PIDS_SCRIPT = 'for p in /proc/[0-9]*; do n="${p#/proc/}"; if [ "$n" != "$$" ]; then echo "$n"; fi; done'
REGISTERED_SCRIPTS = frozenset({QUIESCE_SCRIPT, LIST_PIDS_SCRIPT, VERIFY_SCRIPT, IDENTITY_SCRIPT, READY_SCRIPT})

# Generic default for a context that carries no environment of its own. Agent contexts always carry one
# (``agent_env.experiment_env``), so this is never what an experiment run executes with.
DEFAULT_ENV = {"HOME": "/home/agent", "PATH": "/usr/bin:/bin", "LC_ALL": "C", "TZ": "UTC"}
_GIT_VARS = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
             "GIT_OPTIONAL_LOCKS": "0"}
GIT_ENV = {**DEFAULT_ENV, **_GIT_VARS}


def exec_env(ctx: ExecContext) -> dict[str, str]:
    """The environment for every exec under ``ctx``: its own if it has one, else the generic default."""
    return dict(ctx.env) if ctx.env else dict(DEFAULT_ENV)


def git_exec_env(ctx: ExecContext) -> dict[str, str]:
    """The context's environment plus the fixed git-hardening variables (nothing else differs)."""
    return {**exec_env(ctx), **_GIT_VARS}

DEFAULT_MAX_OUT = 8 * 1024 * 1024
MAX_STDIN_BYTES = 512 * 1024**2
LISTING_MAX_OUT = 128 * 1024**2


class ExecError(RuntimeError):
    """An exec failed (non-zero exit, truncation, timeout) where success was required."""


@dataclass
class ExecOutcome:
    exit_code: int | None
    stdout: bytes = field(default=b"", repr=False)
    stderr: bytes = field(default=b"", repr=False)
    truncated: bool = False
    timed_out: bool = False
    stdin_bytes: int = 0
    stdin_error: str | None = None
    # Set only when the stdin writer failed on its own (not as a side effect of this session being cancelled or closed): the writer's exception type and errno.
    # The exec's output and exit status are still collected when they are available. run_checked reports this failure ahead of a generic timeout, and the agent runner
    # marks the run invalid: a prompt that was not fully delivered must never pass for an ordinary result.
    stdin_failure: str | None = None


def _raw_socket(sock: Any) -> Any:
    return getattr(sock, "_sock", sock)  # docker-py returns a SocketIO wrapper around the real socket


class ExecSession:
    """One exec with optional stdin, capped output and a deadline. Start it, wait on it, read the outcome."""

    def __init__(self, api: Any, container_id: str, request: dict, *, stdin: bytes | None = None,
                 max_stdout: int = DEFAULT_MAX_OUT, max_stderr: int = DEFAULT_MAX_OUT):
        if stdin is not None and len(stdin) > MAX_STDIN_BYTES:
            raise ExecError("stdin exceeds the hard cap")
        self.api, self.container_id, self.request = api, container_id, request
        self.stdin, self.max_stdout, self.max_stderr = stdin, max_stdout, max_stderr
        self._out, self._err = bytearray(), bytearray()
        self._truncated = False
        self._stdin_error: str | None = None
        self._stdin_failure: str | None = None
        self._cancelled = False
        self._closed = False
        self._sock: Any = None
        self._exec_id: str | None = None
        self._threads: list[threading.Thread] = []
        self._reader_done = threading.Event()
        self._started = False

    # -- lifecycle --
    def start(self) -> "ExecSession":
        self._exec_id = self.api.exec_create(self.container_id, stdin=self.stdin is not None, stdout=True,
                                             stderr=True, privileged=False, **self.request)["Id"]
        self._sock = _raw_socket(self.api.exec_start(self._exec_id, socket=True, tty=False))
        self._started = True
        reader = threading.Thread(target=self._read, daemon=True)
        self._threads.append(reader)
        reader.start()
        if self.stdin is not None:
            writer = threading.Thread(target=self._write, daemon=True)
            self._threads.append(writer)
            writer.start()
        return self

    def _write(self) -> None:
        try:
            self._sock.sendall(self.stdin)
            self._sock.shutdown(socket.SHUT_WR)  # EOF on the process's stdin
        except OSError as e:
            self._stdin_error = f"{type(e).__name__}"
            self._writer_failed(e)
        except BaseException as e:  # never leave the reader waiting for an EOF that can no longer come; the exception itself is not swallowed
            self._stdin_error = f"{type(e).__name__}"
            self._writer_failed(e)
            raise

    def _writer_failed(self, exc: BaseException) -> None:
        """The stdin writer failed. If that is the cause (this session was not already cancelled or closed, which would explain a write error), record
        what failed and make sure the process cannot keep waiting for an EOF that will never come, without cutting off what it has already written.

        First a half-close (``SHUT_WR``): the process sees end-of-input (truncated), runs on, and the reader keeps draining its output and the exec's exit
        status is collected as usual; the caller still gets the failure, because ``stdin_failure`` is set. Only if the half-close itself fails is the whole socket
        shut down (``SHUT_RDWR``), which releases a reader that would otherwise wait for the full deadline. Whatever happens here, the writer's own exception stays
        the recorded failure: errors from this cleanup are never recorded or raised. Nothing here waits: the execution deadline of ``run_exec`` still bounds the exec."""
        if self._closed or self._cancelled:
            return  # a consequence of our own cancel or close, not a cause: keep the ordinary timeout reporting
        code = getattr(exc, "errno", None)
        name = errno.errorcode.get(code, str(code)) if isinstance(code, int) else None
        self._stdin_failure = f"{type(exc).__name__}" + (f" ({name})" if name else "")
        for how in (socket.SHUT_WR, socket.SHUT_RDWR):
            try:
                self._sock.shutdown(how)
                return
            except (OSError, AttributeError):
                continue  # already closed, never connected or already shut down: try the stronger shutdown; the original error is kept either way

    def _recv_exact(self, n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self._sock.recv(n - len(buf))
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        return bytes(buf)

    def _read(self) -> None:
        try:
            while not self._cancelled:
                header = self._recv_exact(8)
                if header is None:
                    return
                kind, size = header[0], struct.unpack(">I", header[4:8])[0]
                body = self._recv_exact(size) if size else b""
                if body is None:
                    return
                target, cap = (self._err, self.max_stderr) if kind == 2 else (self._out, self.max_stdout)
                room = cap - len(target)
                if len(body) > room:
                    target += body[:max(room, 0)]
                    self._truncated = True
                    return  # stop reading; the caller sees truncated=True
                target += body
        finally:
            self._reader_done.set()

    # -- waiting --
    def done(self) -> bool:
        return self._reader_done.is_set()

    def wait(self, timeout: float) -> bool:
        """Block until the exec's output ends or ``timeout`` seconds pass. True if it ended."""
        return self._reader_done.wait(timeout)

    def cancel(self) -> None:
        """Stop reading and close our end. This does NOT stop the process; quiesce the container for that."""
        self._cancelled = True
        self.close()

    def close(self) -> None:
        """Release our end of the exec socket (also unblocks a stdin writer). Safe to call repeatedly."""
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.close()  # threads still using it get an OSError, which they already handle
        except (OSError, AttributeError):
            pass

    def outcome(self, *, timed_out: bool = False, exit_wait_s: float = 3.0) -> ExecOutcome:
        try:
            return self._outcome(timed_out, exit_wait_s)
        finally:
            self.close()  # the output has ended or we gave up: never leave the hijacked socket open

    def _outcome(self, timed_out: bool, exit_wait_s: float) -> ExecOutcome:
        exit_code = None
        if not timed_out and self._exec_id is not None:
            deadline = time.monotonic() + exit_wait_s
            while True:
                info = self.api.exec_inspect(self._exec_id)
                if not info.get("Running"):
                    exit_code = info.get("ExitCode")
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        return ExecOutcome(exit_code, bytes(self._out), bytes(self._err), self._truncated, timed_out,
                           len(self.stdin or b""), self._stdin_error, self._stdin_failure)


def run_exec(api: Any, container_id: str, argv: list[str], *, ctx: ExecContext, workdir: str = "/",
             env: dict[str, str] | None = None, stdin: bytes | None = None, timeout_s: float = 60.0,
             max_stdout: int = DEFAULT_MAX_OUT, max_stderr: int = DEFAULT_MAX_OUT) -> ExecOutcome:
    """Policy-check, run and wait for one exec. Raises PolicyViolation before anything touches Docker."""
    request = harden_exec(argv, user=ctx.user, workdir=workdir, env=exec_env(ctx) if env is None else env, ctx=ctx)
    # exec_create wants `cmd`, `user`, `workdir`, `environment` and `tty`; harden_exec produced exactly those
    session = ExecSession(api, container_id, request, stdin=stdin, max_stdout=max_stdout, max_stderr=max_stderr).start()
    if not session.wait(timeout_s):
        session.cancel()
        return session.outcome(timed_out=True)
    return session.outcome()


def run_checked(*args: Any, what: str, **kw: Any) -> ExecOutcome:
    out = run_exec(*args, **kw)
    if out.timed_out or out.truncated or out.exit_code != 0 or out.stdin_error:
        why = (f"stdin write failed ({out.stdin_failure})" if out.stdin_failure
               else "timed out" if out.timed_out else "output over the cap" if out.truncated
               else f"stdin error {out.stdin_error}" if out.stdin_error else f"exit code {out.exit_code}")
        raise ExecError(f"{what} failed: {why}; stderr: {out.stderr[-300:].decode('utf-8', 'replace')!r}")
    return out


def inject_tar(api: Any, container_id: str, tar_bytes: bytes, dest: str, *, ctx: ExecContext,
               timeout_s: float = 300.0) -> ExecOutcome:
    """Stream a tar into ``dest`` (a tmpfs) as the container's own non-root user."""
    return run_checked(api, container_id, ["/bin/tar", "-x", "-p", "--no-same-owner", "-f", "-", "-C", dest],
                       ctx=ctx, workdir="/", stdin=tar_bytes, timeout_s=timeout_s, what=f"inject into {dest}")


def list_tree(api: Any, container_id: str, root: str, *, ctx: ExecContext, timeout_s: float = 300.0) -> str:
    """What is really under ``root`` (JSON from ``seed.VERIFY_SCRIPT``), to compare with a manifest.

    Runs ``ctx.python`` and nothing else: no ``python3`` lookup, no fallback. A missing or wrong interpreter
    fails the exec (and so the run) instead of being papered over.
    """
    if not ctx.python:
        raise PolicyViolation("exec: no interpreter is configured for this context; refusing to guess one")
    out = run_checked(api, container_id, [ctx.python, "-I", "-c", VERIFY_SCRIPT, root], ctx=ctx,
                      workdir="/", timeout_s=timeout_s, max_stdout=LISTING_MAX_OUT, what=f"list {root}")
    return out.stdout.decode("utf-8")


def quiesce(api: Any, container_id: str, *, ctx: ExecContext, timeout_s: float = 30.0,
            clock: Callable[[], float] = time.monotonic) -> list[str]:
    """Kill every process of the container's user except PID 1, then return the PIDs still alive.

    ``kill -KILL -1`` also kills the exec's own shell, so a non-zero or missing exit code from the first
    exec is expected; what counts is the second exec's process list.
    """
    run_exec(api, container_id, ["/bin/sh", "-c", QUIESCE_SCRIPT], ctx=ctx, workdir="/", timeout_s=timeout_s)
    out = run_checked(api, container_id, ["/bin/sh", "-c", LIST_PIDS_SCRIPT], ctx=ctx, workdir="/",
                      timeout_s=timeout_s, what="list processes")
    return sorted(set(out.stdout.decode().split()) - {"1"}, key=int)


def read_stop_reason(api: Any, container_id: str, path: str, *, ctx: ExecContext, timeout_s: float = 15.0) -> str | None:
    """The gateway's stop reason (it writes ``path`` when it stops), or None while it is still running.

    ``path`` must be in ``ctx.readable_paths``. A missing file is the normal "not stopped" answer; any
    other failure raises, so a broken channel is never mistaken for a healthy gateway.
    """
    out = run_exec(api, container_id, ["/bin/cat", path], ctx=ctx, workdir="/", timeout_s=timeout_s, max_stdout=4096)
    if out.timed_out or out.truncated:
        raise ExecError("reading the gateway stop file timed out or was oversized")
    if out.exit_code == 0:
        reason = out.stdout.decode("utf-8", "replace").strip()
        return reason or None
    if b"No such file" in out.stderr:
        return None
    raise ExecError(f"reading the gateway stop file failed: exit {out.exit_code}")


def read_file(api: Any, container_id: str, path: str, *, ctx: ExecContext, max_bytes: int = 16 * 1024 * 1024,
              timeout_s: float = 30.0) -> bytes:
    """The whole content of a registered file (``ctx.readable_paths``), or an error. Never a partial read."""
    out = run_exec(api, container_id, ["/bin/cat", path], ctx=ctx, workdir="/", timeout_s=timeout_s, max_stdout=max_bytes)
    if out.timed_out or out.truncated or out.exit_code != 0:
        raise ExecError(f"reading {path} failed: " + ("timed out" if out.timed_out else "over the size cap" if out.truncated
                                                     else f"exit code {out.exit_code}"))
    return out.stdout

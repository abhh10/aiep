"""A failed stdin writer must not leave the exec waiting for an EOF that can no longer arrive, and must not hide what failed.

Background: ExecSession's writer thread sends stdin and then half-closes the socket (EOF for the process). If either call raised OSError the old code only recorded the
error: the process never saw EOF, the reader waited for the whole deadline (300 s in production) and the caller saw a bare "timed out". Now the writer's failure is recorded
(``stdin_failure``), the session half-closes (``SHUT_WR``) so the process gets its EOF and the reader keeps draining output and the exit status, and only if that half-close fails
too is the whole socket shut down (``SHUT_RDWR``). The agent runner turns a delivery failure into an invalid run. These tests inject the failure into the socket the
session uses (fake or real, the code path is the same), run the real ExecSession/run_exec/run_agent, and use deadlines long enough that only the fast paths can finish quickly."""

from __future__ import annotations

import errno
import socket
import threading
import time

import pytest

from aiep import container_exec as ce
from aiep.container_exec import ExecContext, ExecError, REGISTERED_SCRIPTS, run_checked, run_exec
from fake_docker import ExecResult, FakeExecApi
from test_agent_runner import go, repo, rig, seed  # noqa: F401  (fixtures: repo, seed)

CTX = ExecContext(user="agent", allowed_scripts=REGISTERED_SCRIPTS, python="/opt/miniconda3/bin/python")
CID = "abc123"
TAR = ["/bin/tar", "-x", "-p", "--no-same-owner", "-f", "-", "-C", "/testbed"]
DEADLINE = 60.0  # far longer than any fail-fast path may take; a regression shows up as a test that takes this long
FAST = 15.0  # generous upper bound for "immediately" on a loaded machine


class FlakySock:
    """The real socket with a chosen stdin failure injected. Everything else is delegated."""

    def __init__(self, real, *, sendall_error=None, shutdown_wr_error=None, block_sendall_until_closed=False):
        self.real, self.sendall_error, self.shutdown_wr_error = real, sendall_error, shutdown_wr_error
        self.block, self.closed_evt, self.shutdowns = block_sendall_until_closed, threading.Event(), []

    def sendall(self, data):
        if self.block:  # a write that is stuck until WE close the socket, then fails the way a closed socket does
            self.closed_evt.wait(30)
            raise OSError(errno.EBADF, "Bad file descriptor")
        if self.sendall_error is not None:
            raise self.sendall_error
        return self.real.sendall(data)

    def shutdown(self, how):
        self.shutdowns.append(how)
        if how == socket.SHUT_WR and self.shutdown_wr_error is not None:
            raise self.shutdown_wr_error
        return self.real.shutdown(how)

    def close(self):
        self.closed_evt.set()
        return self.real.close()

    def __getattr__(self, name):
        return getattr(self.real, name)


@pytest.fixture()
def flaky(monkeypatch):
    """Install a FlakySock around every exec socket and keep the sessions and sockets for inspection."""
    made = {"socks": [], "sessions": []}

    def install(**kw):
        monkeypatch.setattr(ce, "_raw_socket", lambda s: made["socks"].append(FlakySock(getattr(s, "_sock", s), **kw)) or made["socks"][-1])
        real_start = ce.ExecSession.start

        def start(self):
            made["sessions"].append(self)
            return real_start(self)
        monkeypatch.setattr(ce.ExecSession, "start", start)
        return made
    return install


def api():
    return FakeExecApi(lambda argv, rec, stdin: ExecResult())  # never extracts: a half-delivered stdin must not make the fake server raise


ENOBUFS = OSError(errno.ENOBUFS, "No buffer space available")
ENOTCONN = OSError(errno.ENOTCONN, "Socket is not connected")


def _threads_finished(session, wait=10.0):
    for t in session._threads:
        t.join(wait)
    return not any(t.is_alive() for t in session._threads)


def test_a_failing_sendall_fails_the_exec_at_once_and_names_the_error(flaky):
    made = flaky(sendall_error=ENOBUFS)
    t0 = time.monotonic()
    with pytest.raises(ExecError) as ei:
        run_checked(api(), CID, TAR, ctx=CTX, workdir="/", stdin=b"x" * 5000, timeout_s=DEADLINE, what="inject into /testbed")
    assert time.monotonic() - t0 < FAST
    msg = str(ei.value)
    assert "stdin write failed (OSError (ENOBUFS))" in msg and "timed out" not in msg


def test_a_failing_half_close_after_a_successful_send_fails_the_exec_at_once(flaky):
    made = flaky(shutdown_wr_error=ENOTCONN)
    t0 = time.monotonic()
    with pytest.raises(ExecError) as ei:
        run_checked(api(), CID, TAR, ctx=CTX, workdir="/", stdin=b"x" * 5000, timeout_s=DEADLINE, what="inject into /testbed")
    assert time.monotonic() - t0 < FAST
    assert "stdin write failed (OSError (ENOTCONN))" in str(ei.value) and "timed out" not in str(ei.value)
    assert socket.SHUT_WR in made["socks"][0].shutdowns and socket.SHUT_RDWR in made["socks"][0].shutdowns  # the half-close was attempted, then the whole socket was shut down


@pytest.mark.parametrize("kw, how", [({"sendall_error": ENOBUFS}, "sendall"), ({"shutdown_wr_error": ENOTCONN}, "shutdown")])
def test_the_reader_terminates_and_every_thread_and_descriptor_is_released(flaky, kw, how):
    made = flaky(**kw)
    t0 = time.monotonic()
    out = run_exec(api(), CID, TAR, ctx=CTX, workdir="/", stdin=b"x" * 5000, timeout_s=DEADLINE)
    assert time.monotonic() - t0 < FAST
    assert out.timed_out is False and out.stdin_error == "OSError" and out.stdin_failure.startswith("OSError (")
    (session,) = made["sessions"]
    assert session.done() and session._closed and _threads_finished(session)  # reader and writer ended, our end of the socket was closed
    assert made["socks"][0].real.fileno() == -1


def test_the_original_error_survives_a_failing_half_close_and_a_failing_full_shutdown(flaky, monkeypatch):
    """Both cleanup steps fail (EBADF): the writer's own error is still the recorded one, nothing from the cleanup replaces it, and the exec ends within its (short) deadline."""
    made = flaky(sendall_error=ENOBUFS)

    def shutdown(self, how):
        self.shutdowns.append(how)
        raise OSError(errno.EBADF, "Bad file descriptor")
    monkeypatch.setattr(FlakySock, "shutdown", shutdown)
    t0 = time.monotonic()
    out = run_exec(api(), CID, TAR, ctx=CTX, workdir="/", stdin=b"x" * 100, timeout_s=1.5)
    assert out.stdin_failure == "OSError (ENOBUFS)" and out.stdin_error == "OSError"
    assert made["socks"][0].shutdowns == [socket.SHUT_WR, socket.SHUT_RDWR]  # both were attempted, in that order
    assert out.timed_out is True and time.monotonic() - t0 < FAST  # bounded: nothing could release the reader, so the exec deadline applied; the failure is still reported
    with pytest.raises(ExecError, match=r"stdin write failed \(OSError \(ENOBUFS\)\)"):
        run_checked(api(), CID, TAR, ctx=CTX, workdir="/", stdin=b"x" * 100, timeout_s=1.5, what="inject")


def test_a_writer_failure_half_closes_first_and_the_reader_still_drains_output_and_the_exit_status(flaky):
    made = flaky(sendall_error=OSError(errno.EPIPE, "Broken pipe"))  # the peer stopped reading stdin
    sink = []
    server = FakeExecApi(lambda argv, rec, stdin: sink.append(stdin) or ExecResult(stdout=b"partial output", stderr=b"warn", exit_code=2))
    out = run_exec(server, CID, TAR, ctx=CTX, workdir="/", stdin=b"x" * 100, timeout_s=DEADLINE)
    assert made["socks"][0].shutdowns == [socket.SHUT_WR]  # a half-close was enough: no full shutdown, so nothing unread was discarded
    assert sink == [b""] and out.stdout == b"partial output" and out.stderr == b"warn"  # the process got its EOF and its output was drained
    assert out.exit_code == 2 and out.timed_out is False  # and its exit status was collected
    assert out.stdin_failure == "BrokenPipeError (EPIPE)"
    with pytest.raises(ExecError, match=r"stdin write failed \(BrokenPipeError \(EPIPE\)\)"):  # the failure outranks the exit status in the error
        run_checked(FakeExecApi(lambda argv, rec, stdin: ExecResult(exit_code=0)), CID, TAR, ctx=CTX, workdir="/", stdin=b"x", timeout_s=DEADLINE, what="inject")


def test_a_failed_half_close_falls_back_to_a_full_shutdown_which_releases_the_reader(flaky):
    made = flaky(shutdown_wr_error=ENOTCONN)
    out = run_exec(api(), CID, TAR, ctx=CTX, workdir="/", stdin=b"x" * 100, timeout_s=DEADLINE)
    assert made["socks"][0].shutdowns == [socket.SHUT_WR, socket.SHUT_WR, socket.SHUT_RDWR]  # the writer's own half-close failed, ours failed too, then the full shutdown
    assert out.timed_out is False and out.stdin_failure == "OSError (ENOTCONN)"


def test_a_peer_that_really_closes_first_produces_a_bounded_writer_failure(monkeypatch):
    """No injected error: the fake server end is closed before the client writes, so the client's own sendall fails (EPIPE or reset) on a real socket."""
    import fake_docker
    real_pair = fake_docker._socketpair

    def closed_peer():
        client, server = real_pair()
        server.close()
        return client, server
    monkeypatch.setattr(fake_docker, "_socketpair", closed_peer)
    monkeypatch.setattr(threading, "excepthook", lambda a: None)  # the fake server thread finds its socket already closed
    t0 = time.monotonic()
    out = run_exec(api(), CID, TAR, ctx=CTX, workdir="/", stdin=b"x" * (4 * 1024 * 1024), timeout_s=DEADLINE)
    assert time.monotonic() - t0 < FAST and out.timed_out is False
    assert out.stdin_failure is not None and out.stdin_failure.split(" ")[0] in {"BrokenPipeError", "ConnectionResetError", "OSError"}
    assert out.exit_code is None  # the exec never ran, and the bounded exit-status poll said so


def test_a_non_oserror_in_the_writer_still_releases_the_reader_and_is_not_swallowed(flaky, monkeypatch):
    """The exception propagates to the thread excepthook as before; the reader is released instead of waiting for the deadline."""
    flaky(sendall_error=RuntimeError("boom"))
    seen = []
    monkeypatch.setattr(threading, "excepthook", lambda a: seen.append(a.exc_value))
    t0 = time.monotonic()
    out = run_exec(api(), CID, TAR, ctx=CTX, workdir="/", stdin=b"x" * 100, timeout_s=DEADLINE)
    assert time.monotonic() - t0 < FAST and out.stdin_failure == "RuntimeError" and out.timed_out is False
    assert [str(e) for e in seen] == ["boom"]


def test_a_write_error_caused_by_our_own_cancel_keeps_the_ordinary_timeout_report(flaky):
    """A genuine timeout cancels the session; the writer then fails because we closed the socket. That is a consequence, not the cause."""
    flaky(block_sendall_until_closed=True)
    blocked = threading.Event()
    slow = FakeExecApi(lambda argv, rec, stdin: ExecResult(hang=blocked))
    with pytest.raises(ExecError, match="timed out") as ei:
        run_checked(slow, CID, TAR, ctx=CTX, workdir="/", stdin=b"x" * 100, timeout_s=0.4, what="inject into /testbed")
    assert "stdin write failed" not in str(ei.value)
    blocked.set()


def test_ordinary_execs_are_unchanged(flaky):
    flaky()  # sockets wrapped, nothing injected
    out = run_checked(FakeExecApi(lambda argv, rec, stdin: ExecResult(stdout=b"ok")), CID, TAR, ctx=CTX, workdir="/", stdin=b"x" * 50000, timeout_s=DEADLINE, what="inject")
    assert out.exit_code == 0 and out.stdout == b"ok" and out.stdin_error is None and out.stdin_failure is None
    with pytest.raises(ExecError, match="exit code 3"):
        run_checked(FakeExecApi(lambda argv, rec, stdin: ExecResult(exit_code=3)), CID, TAR, ctx=CTX, workdir="/", stdin=b"x", timeout_s=DEADLINE, what="inject")


def test_the_agent_runner_fails_fast_with_the_writer_error_and_still_removes_the_container(tmp_path, seed, flaky):  # noqa: F811
    flaky(sendall_error=ENOBUFS)
    # the fake container must not try to extract the half-delivered tar (that would raise inside its server thread): answer injections with an empty result
    spec, fs, fake_api, client = rig(tmp_path, seed, handler_wrap=lambda fs: (lambda argv, rec, stdin: ExecResult() if argv[0] == "/bin/tar" else fs.handler(argv, rec, stdin)))
    t0 = time.monotonic()
    with pytest.raises(ExecError, match=r"stdin write failed \(OSError \(ENOBUFS\)\)"):
        go(spec, client)
    assert time.monotonic() - t0 < FAST
    assert client.container.calls[-1] == "remove(force=True,v=True)"


# ---------------------------------------------------------------- the agent path: a prompt that was not fully delivered is an invalid run, with the reason on record
def _claude_stdin_flaky(monkeypatch, error):
    """Fail the write of the TASK only (the seed injection and every other exec write normally)."""
    from test_agent_runner import TASK
    real_raw = ce._raw_socket
    made = []

    class TaskSock(FlakySock):
        def sendall(self, data):
            if TASK.encode() in data:
                raise error
            return self.real.sendall(data)
    monkeypatch.setattr(ce, "_raw_socket", lambda s: made.append(TaskSock(real_raw(s))) or made[-1])
    return made


def test_a_failed_prompt_delivery_makes_the_agent_run_invalid_and_records_why(tmp_path, seed, monkeypatch):  # noqa: F811
    from fake_docker import FakeAgent
    _claude_stdin_flaky(monkeypatch, OSError(errno.EPIPE, "Broken pipe"))
    agent = FakeAgent()
    spec, fs, fake_api, client = rig(tmp_path, seed, agent=agent)
    res = go(spec, client)
    assert res.stdin_failure == "BrokenPipeError (EPIPE)" and res.valid is False and res.patch is None
    assert "task prompt was not delivered completely" in res.invalid_reason and "BrokenPipeError (EPIPE)" in res.invalid_reason
    assert res.claude_exit_code == 0 and "exit code 0" in res.invalid_reason  # the exec's real status was still collected, and is named
    assert res.removed and client.container.calls[-1] == "remove(force=True,v=True)"
    from aiep.agent_runner import record_of
    rec = record_of(res)
    assert "task prompt was not delivered completely" in rec["invalid_reason"] and "stdin_failure" not in rec  # on record through the existing field; the record's schema is unchanged


def test_an_ordinary_agent_run_has_no_stdin_failure(tmp_path, seed):  # noqa: F811
    from test_agent_runner import edit_agent
    spec, fs, fake_api, client = rig(tmp_path, seed, agent=edit_agent())
    res = go(spec, client)
    assert res.stdin_failure is None and res.valid is True and res.claude_exit_code == 0 and res.patch is not None


def test_a_genuine_agent_timeout_is_still_a_timeout_not_a_stdin_failure(tmp_path, seed):  # noqa: F811
    from fake_docker import FakeAgent
    hang = threading.Event()
    spec, fs, fake_api, client = rig(tmp_path, seed, agent=FakeAgent(hang=hang), wall_clock_s=0.3)
    res = go(spec, client)
    hang.set()
    assert res.status == "timeout" and res.stdin_failure is None and res.valid is True

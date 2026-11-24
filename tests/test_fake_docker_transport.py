"""The fake Docker's exec transport (tests/fake_docker.py::_socketpair): loopback TCP, one connection per exec.

It replaced an AF_UNIX socketpair that on macOS occasionally never delivered the peer's EOF (reproduced experimentally, about 1 in 10,000 exchanges), which left a fake exec waiting
for its full deadline. These tests pin the properties the replacement must keep: loopback only, an ephemeral port, EOF always delivered, stdin and replies intact, and nothing
left open on success, failure or cancellation. They are evidence for this transport on this machine, not a proof about any operating system."""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from aiep.container_exec import ExecContext, ExecError, REGISTERED_SCRIPTS, run_checked, run_exec
from fake_docker import ExecResult, FakeExecApi, _socketpair, live_server_threads
from test_agent_runner import go, repo, rig, seed  # noqa: F401  (fixtures: repo, seed)

CTX = ExecContext(user="agent", allowed_scripts=REGISTERED_SCRIPTS, python="/opt/miniconda3/bin/python")
CID = "abc123"
TAR = ["/bin/tar", "-x", "-p", "--no-same-owner", "-f", "-", "-C", "/testbed"]
DEADLINE = 60.0


def open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def serve_threads() -> int:
    return live_server_threads()  # the fake tracks its own server threads; no reliance on Thread internals


def settle(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def recording_api(sink, result=None):
    def handler(argv, rec, stdin):
        sink.append(stdin)
        return result or ExecResult()
    return FakeExecApi(handler)


# ---------------------------------------------------------------- the pair itself
def test_the_pair_is_loopback_tcp_on_an_ephemeral_port_and_leaves_no_listener_behind():
    fds = open_fds()
    a, b = _socketpair()
    try:
        assert a.family == b.family == socket.AF_INET and a.type == b.type == socket.SOCK_STREAM
        assert a.getpeername() == b.getsockname() and b.getpeername() == a.getsockname()
        assert a.getsockname()[0] == b.getsockname()[0] == "127.0.0.1"  # never bound to a routable address
        port = b.getsockname()[1]
        assert 1024 <= port <= 65535 and a.getsockname()[1] != port  # an ephemeral server port, not a fixed one
        assert a.gettimeout() is None and b.gettimeout() is None  # both ends block, like the AF_UNIX pair did
        a.sendall(b"ping")
        assert b.recv(4) == b"ping"
        b.sendall(b"pong")
        assert a.recv(4) == b"pong"
        assert open_fds() <= fds + 2  # the listener is already closed: at most our two ends are open (other threads may close descriptors meanwhile, so not an equality)
    finally:
        a.close()
        b.close()
    assert settle(lambda: open_fds() <= fds)


def test_two_pairs_never_cross_their_connections():
    pairs = [_socketpair() for _ in range(8)]
    try:
        for i, (a, _) in enumerate(pairs):
            a.sendall(bytes([i]) * 10)
        for i, (_, b) in enumerate(pairs):
            assert b.recv(10) == bytes([i]) * 10  # each listener serves exactly its own client
    finally:
        for a, b in pairs:
            a.close()
            b.close()


def test_a_failed_pair_closes_everything(monkeypatch):
    fds = open_fds()
    real_connect = socket.socket.connect

    def refuse(self, address):
        raise OSError(111, "injected connect failure")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    with pytest.raises(OSError, match="injected connect failure"):
        _socketpair()
    monkeypatch.setattr(socket.socket, "connect", real_connect)
    assert settle(lambda: open_fds() <= fds)  # neither the listener nor the half-made client leaked


def test_a_setup_failure_after_accept_closes_the_accepted_socket_too(monkeypatch):
    fds = open_fds()
    real = socket.socket.setsockopt

    def failing(self, level, *args):
        if level == socket.IPPROTO_TCP:
            raise OSError(22, "injected setsockopt failure")  # happens after accept(), when both ends exist
        return real(self, level, *args)
    monkeypatch.setattr(socket.socket, "setsockopt", failing)
    with pytest.raises(OSError, match="injected setsockopt failure"):
        _socketpair()
    monkeypatch.setattr(socket.socket, "setsockopt", real)
    assert settle(lambda: open_fds() <= fds)  # the listener, the client and the accepted server socket are all closed


# ---------------------------------------------------------------- stdin and EOF through the fake exec
@pytest.mark.parametrize("size", [0, 1, 8191, 8193, 65536, 3 * 1024 * 1024])
def test_stdin_reaches_the_server_intact_and_eof_is_delivered(size):
    sink = []
    payload = os.urandom(size)
    out = run_checked(recording_api(sink, ExecResult(stdout=b"done")), CID, TAR, ctx=CTX, workdir="/", stdin=payload, timeout_s=DEADLINE, what="inject")
    assert sink == [payload] and out.stdout == b"done" and out.stdin_error is None and out.stdin_failure is None


def test_many_concurrent_injections_all_see_eof_with_nothing_left_open():
    """A repeated EOF exercise against the real fake transport (the old AF_UNIX pair lost about one EOF in 10,000 of these)."""
    fds, base_threads = open_fds(), serve_threads()
    payload = b"t" * 40960
    failures, n_threads, per_thread = [], 8, 150

    def worker():
        for _ in range(per_thread):
            sink = []
            try:
                out = run_exec(recording_api(sink), CID, TAR, ctx=CTX, workdir="/", stdin=payload, timeout_s=30)
                if out.timed_out or sink != [payload] or out.stdin_error:
                    failures.append((out.timed_out, len(sink), out.stdin_error))
            except Exception as e:  # noqa: BLE001
                failures.append(repr(e))
    ts = [threading.Thread(target=worker) for _ in range(n_threads)]
    t0 = time.monotonic()
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)
    assert not any(t.is_alive() for t in ts) and failures == [] and time.monotonic() - t0 < 100
    assert settle(lambda: serve_threads() == base_threads) and settle(lambda: open_fds() <= fds + 2)


# ---------------------------------------------------------------- failure, cancellation, cleanup
def test_cancellation_after_a_timeout_releases_the_server_thread_and_both_sockets():
    fds, base_threads = open_fds(), serve_threads()
    release = threading.Event()
    api = FakeExecApi(lambda argv, rec, stdin: ExecResult(hang=release))
    out = run_exec(api, CID, TAR, ctx=CTX, workdir="/", stdin=b"x" * 1000, timeout_s=0.4)
    assert out.timed_out is True and out.stdin_failure is None
    release.set()
    assert settle(lambda: serve_threads() == base_threads) and settle(lambda: open_fds() <= fds)


def test_a_server_error_ends_the_reader_and_leaves_nothing_open(monkeypatch):
    fds, base_threads = open_fds(), serve_threads()
    seen = []
    monkeypatch.setattr(threading, "excepthook", lambda a: seen.append(str(a.exc_value)))

    def boom(argv, rec, stdin):
        raise RuntimeError("fake handler failed")
    t0 = time.monotonic()
    out = run_exec(FakeExecApi(boom), CID, TAR, ctx=CTX, workdir="/", stdin=b"x" * 1000, timeout_s=DEADLINE)
    assert out.timed_out is False and out.exit_code is None and time.monotonic() - t0 < 30  # EOF from the closed server end, not the deadline
    assert settle(lambda: seen == ["fake handler failed"]) and settle(lambda: serve_threads() == base_threads) and settle(lambda: open_fds() <= fds)


def test_a_failed_exec_through_the_agent_runner_still_removes_the_container(tmp_path, seed, monkeypatch):  # noqa: F811
    monkeypatch.setattr(threading, "excepthook", lambda a: None)  # the fake handler's error is the point of this test

    def failing_tar(fs):
        def handler(argv, rec, stdin):
            if argv[0] == "/bin/tar":
                raise RuntimeError("fake tar failed")
            return fs.handler(argv, rec, stdin)
        return handler
    spec, fs, api, client = rig(tmp_path, seed, handler_wrap=failing_tar)
    with pytest.raises(ExecError, match="inject into /testbed failed"):
        go(spec, client)
    assert client.container.calls[-1] == "remove(force=True,v=True)"

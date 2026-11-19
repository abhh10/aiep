import socket
from pathlib import Path

import pytest


def test_non_loopback_connect_resolve_and_create_connection_are_refused_and_recorded(network_guard):
    with pytest.raises(OSError, match="network blocked"):
        socket.create_connection(("192.0.2.1", 443), timeout=1)  # TEST-NET-1: refused before any packet is sent
    with pytest.raises(OSError, match="network blocked"):
        socket.getaddrinfo("example.invalid", 443)
    s = socket.socket()
    try:
        with pytest.raises(OSError, match="network blocked"):
            s.connect(("203.0.113.9", 80))
        with pytest.raises(OSError, match="network blocked"):
            s.connect_ex(("198.51.100.7", 80))
    finally:
        s.close()
    assert len(network_guard) == 4
    network_guard.clear()  # these attempts were the point of the test


def test_loopback_and_unix_sockets_still_work(network_guard):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    cli = socket.create_connection(srv.getsockname(), timeout=2)
    conn, _ = srv.accept()
    cli.sendall(b"x")
    assert conn.recv(1) == b"x"
    for s in (cli, conn, srv):
        s.close()
    a, b = socket.socketpair()
    a.close()
    b.close()
    assert socket.getaddrinfo("localhost", 80) and network_guard == []


def test_a_swallowed_attempt_still_fails_the_test(pytester):
    pytester.makeconftest(Path(__file__).with_name("conftest.py").read_text())
    pytester.makepyfile('''
        import socket
        def test_swallows():
            try:
                socket.create_connection(("192.0.2.1", 443), timeout=1)
            except OSError:
                pass  # the code under test hides the failure
    ''')
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=1)  # the test body passed; the guard's teardown failed it
    result.stdout.fnmatch_lines(["*attempted network access*"])


@pytest.mark.network
def test_marked_network_tests_are_skipped_unless_explicitly_allowed():
    raise AssertionError("must never run in the unit suite")

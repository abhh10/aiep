"""Unit-suite network guard: no test may reach beyond loopback, and a swallowed attempt still fails the test.

Why this exists: a Phase 1 test called ``datasets.load_dataset``, which asked Hugging Face's dataset viewer over HTTPS even
with the offline variables set; the library swallows every error from that call, so the leak was silent. Now every
connection or name lookup that is not loopback (or a unix socket) is refused and recorded, and the test fails at teardown
even if the code under test caught the exception.

Real connectivity checks are separate and approval-gated: mark a test ``@pytest.mark.network`` and run it only with
``AIEP_ALLOW_NETWORK_TESTS=1``. Otherwise it is skipped, and nothing in the repository sets that variable.
"""

import errno
import ipaddress
import os
import socket

import pytest

pytest_plugins = ["pytester"]  # used only by tests/test_network_guard.py

ALLOW_NETWORK = os.environ.get("AIEP_ALLOW_NETWORK_TESTS") == "1"


def pytest_configure(config):
    config.addinivalue_line("markers", "network: real connectivity check; skipped unless AIEP_ALLOW_NETWORK_TESTS=1 (approval-gated)")


def pytest_collection_modifyitems(config, items):
    if ALLOW_NETWORK:
        return
    skip = pytest.mark.skip(reason="real network check: approval-gated (set AIEP_ALLOW_NETWORK_TESTS=1)")
    for item in items:
        if item.get_closest_marker("network") is not None:  # a real marker, not a name or parameter id containing "network"
            item.add_marker(skip)


def _is_local_host(host) -> bool:
    if host in (None, "", "localhost"):
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def network_guard(monkeypatch, request):
    """Yields the list of blocked attempts (so the guard's own tests can inspect and clear it)."""
    attempts: list[str] = []
    if ALLOW_NETWORK and request.node.get_closest_marker("network"):
        yield attempts
        return

    real_connect, real_connect_ex, real_getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def refuse(what: str, target) -> OSError:
        attempts.append(f"{what} {target!r}")
        return OSError(errno.ENETUNREACH, f"network blocked by tests/conftest.py: {what} {target!r}")

    def allowed(address) -> bool:
        return isinstance(address, (str, bytes)) or _is_local_host(address[0])  # unix socket path, or loopback

    def connect(self, address):
        if not allowed(address):
            raise refuse("connect", address)
        return real_connect(self, address)

    def connect_ex(self, address):
        if not allowed(address):
            raise refuse("connect_ex", address)
        return real_connect_ex(self, address)

    def getaddrinfo(host, *a, **k):
        if not _is_local_host(host):
            raise refuse("resolve", host)
        return real_getaddrinfo(host, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield attempts
    if attempts:
        pytest.fail("the test attempted network access (blocked, even if the code under test swallowed the error): "
                    + "; ".join(attempts), pytrace=False)

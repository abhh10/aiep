"""Fake Docker for lifecycle tests: no daemon and no container; the only sockets are host-local loopback TCP.

The fake daemon serves ``exec`` over real loopback TCP connections, one per exec (so stdin streaming, half-close and Docker's
multiplexed framing are exercised; see ``_socketpair`` for why not AF_UNIX). Nothing leaves the host: every connection is
127.0.0.1 to 127.0.0.1 on an ephemeral port, which the unit-suite network guard allows. It emulates the container's filesystem
with a real temporary directory per mount. What the "container" runs is real where that is safe and local:

* ``tar -x ... -C <dest>``  : extracted with python's tarfile into that mount's directory;
* ``<python> -I -c VERIFY``  : the real VERIFY_SCRIPT, run against that directory. Only the one configured
                              interpreter path exists in this fake; any other path is "not found" (127),
                              which is how a missing /usr/bin/python3 would look;
* ``<python> -m aiep.gateway`` : recorded (``gateway_started``) with the verification state at that moment;
* ``git ...``               : the real git, run in the /testbed directory (a repository we created);
* ``claude ...``            : a scripted fake agent (a callable the test supplies);
* ``sh -c kill -KILL -1``   : kills the fake agent's "processes" (simulated).
Everything else is a test double. Nothing here proves real Docker behaviour.
"""

from __future__ import annotations

import io
import json
import os
import socket
import struct
import subprocess
import sys
import tarfile
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from aiep.agent_env import IDENTITY_SCRIPT
from aiep.container_exec import LIST_PIDS_SCRIPT, QUIESCE_SCRIPT
from aiep.container_policy import READY_SCRIPT
from aiep.seed import VERIFY_SCRIPT


_AGENT_ROUTES = (b"Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
                 b"eth0\t00011EAC\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n")  # 172.30.1.0/24, no default route


def frame(kind: int, data: bytes) -> bytes:
    return bytes([kind, 0, 0, 0]) + struct.pack(">I", len(data)) + data


@dataclass
class ExecResult:
    stdout: bytes = b""
    stderr: bytes = b""
    exit_code: int = 0
    delay: float = 0.0
    hang: threading.Event | None = None  # if set, wait for it before replying (a long-running process)
    chunks: list[bytes] | None = None  # send stdout in these pieces, each as its own frame
    raw_frames: bytes | None = None  # send exactly these bytes (to test odd framing)
    trickle: int | None = None  # send raw_frames in pieces of this many bytes
    exit_lag: float = 0.0  # the exec is still "Running" for this long after the output ends


Handler = Callable[[list[str], dict, bytes], ExecResult | None]


class FakeExecApi:
    """The slice of ``docker.APIClient`` that container_exec uses, plus inspect_container."""

    def __init__(self, handler: Handler | None = None):
        self.handler = handler
        self.calls: list[dict] = []
        self.stdins: list[bytes] = []
        self._execs: dict[str, dict] = {}
        self._n = 0
        self.inspect = {}

    def exec_create(self, container, cmd=None, stdout=True, stderr=True, stdin=False, tty=False,
                    privileged=False, user="", environment=None, workdir=None):
        self._n += 1
        eid = f"exec{self._n}"
        rec = {"container": container, "cmd": cmd, "stdin": stdin, "tty": tty, "privileged": privileged,
               "user": user, "environment": environment, "workdir": workdir}
        self.calls.append(rec)
        self._execs[eid] = {"rec": rec, "running": True, "exit": None}
        return {"Id": eid}

    def exec_start(self, eid, socket=True, tty=False):
        a, b = _socketpair()
        server = threading.Thread(target=self._serve, args=(eid, b), daemon=True, name=f"fake-exec-server-{eid}")
        SERVER_THREADS.add(server)  # tracked explicitly so tests can check that every server thread ends
        server.start()
        return SimpleNamespace(_sock=a)

    def exec_inspect(self, eid):
        e = self._execs[eid]
        return {"Running": e["running"], "ExitCode": e["exit"]}

    def inspect_container(self, _id):
        return self.inspect

    def _serve(self, eid, sock):
        e = self._execs[eid]
        rec = e["rec"]
        data = b""
        try:
            if rec["stdin"]:
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            self.stdins.append(data)
            res = self.handler(list(rec["cmd"]), rec, data) if self.handler else None
            res = res or ExecResult()
            if res.hang is not None:
                res.hang.wait(30)
            if res.delay:
                time.sleep(res.delay)
            if res.exit_lag:
                threading.Timer(res.exit_lag, lambda: e.update(exit=res.exit_code, running=False)).start()
            else:
                e["exit"], e["running"] = res.exit_code, False
            try:
                if res.raw_frames is not None and res.trickle:
                    for i in range(0, len(res.raw_frames), res.trickle):
                        sock.sendall(res.raw_frames[i:i + res.trickle])
                        time.sleep(0.0005)
                elif res.raw_frames is not None:
                    sock.sendall(res.raw_frames)
                elif res.chunks:
                    for c in res.chunks:
                        sock.sendall(frame(1, c))
                        time.sleep(0.001)
                else:
                    if res.stdout:
                        sock.sendall(frame(1, res.stdout))
                    if res.stderr:
                        sock.sendall(frame(2, res.stderr))
            except OSError:
                pass
        finally:
            try:
                sock.close()
            except OSError:
                pass


SERVER_THREADS: "weakref.WeakSet[threading.Thread]" = weakref.WeakSet()  # every fake exec server thread ever started (weakly held)


def live_server_threads() -> int:
    """How many fake exec server threads are still running."""
    return sum(1 for t in list(SERVER_THREADS) if t.is_alive())


def _socketpair():
    """A connected (client, server) pair over loopback TCP: the transport of one fake exec.

    Why not ``socket.socketpair()``: on macOS an AF_UNIX pair occasionally (about 1 in 10,000 exchanges, reproduced with a standalone script) never delivers the peer's EOF
    after ``shutdown(SHUT_WR)``, so a fake server blocked in ``recv`` waited forever and the exec ran into its full deadline. A loopback TCP pair showed no such loss in 640,000
    comparable exchanges. That is experimental evidence on one machine, not a guarantee.

    Each pair gets its own listener, bound to 127.0.0.1 on an ephemeral port (no fixed port, so no collision), which is closed as soon as the connection is accepted; if anything
    fails here nothing stays open. The connection itself never leaves the host and the unit-suite network guard allows it (loopback)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client = server = None
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(10)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(10)
        client.connect(listener.getsockname())
        server, _ = listener.accept()
        client.settimeout(None)
        server.settimeout(None)  # whatever the listener's mode, both ends block like the AF_UNIX pair did
        for end in (client, server):
            end.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return client, server
    except BaseException:
        for end in (client, server):  # whichever ends exist when setup fails (including the accepted one) are closed
            if end is not None:
                end.close()
        raise
    finally:
        listener.close()


# ---------------------------------------------------------------------------- the emulated container
@dataclass
class FakeAgent:
    """What the fake `claude` does. ``act(task_bytes, workdir)`` returns stdout bytes."""

    act: Callable[[bytes, Path], bytes] = lambda task, wd: b'{"type":"result","subtype":"success"}\n'
    exit_code: int = 0
    hang: threading.Event | None = None
    before: Callable[[bytes, Path], None] | None = None  # work done before the (optional) hang
    seen_stdin: list[bytes] = field(default_factory=list)
    seen_argv: list[list[str]] = field(default_factory=list)


class FakeContainerFS:
    """Maps container paths (/testbed, /opt/aiep, ...) to real temp directories and serves execs."""

    def __init__(self, base: Path, agent: FakeAgent | None = None, git_env: dict | None = None,
                 python: str = "/opt/miniconda3/bin/python", owner: tuple[int, int] | None = (1000, 1000),
                 account: tuple[str, int, int, str] | None = ("nonroot", 1000, 1000, "/home/nonroot")):
        self.python = python
        # (name, uid, gid, home) of the container user's passwd entry; None means the image has no such entry
        self.account = account
        self.gateway_alive = threading.Event()  # set while the fake gateway process "runs"
        self.gateway_listening = False
        self.files: dict[str, bytes] = {"/proc/net/route": _AGENT_ROUTES}  # virtual files for /bin/cat (stop file, ledger, the container's route table)
        self.owner = owner  # the listing reports this uid:gid (the simulated tmpfs owner; default is the agent identity 1000:1000); None reports the real file owner
        self.gateway_started: list[list[str]] = []
        self.started_after_listings: int | None = None
        self.listings = 0  # verification listings served so far (lets tests prove verify-before-start)
        self.base = Path(base)
        self.agent = agent or FakeAgent()
        self.dirs: dict[str, Path] = {}
        for mount in ("/testbed", "/opt/aiep", "/opt/aiep-gw", "/opt/aiep-proxy", "/tmp", "/home/agent"):
            d = self.base / mount.strip("/").replace("/", "_")
            d.mkdir(parents=True, exist_ok=True)
            self.dirs[mount] = d
        self.agent_alive = False
        self.log: list[list[str]] = []

    def path(self, mount: str) -> Path:
        return self.dirs[mount]

    def handler(self, argv: list[str], rec: dict, stdin: bytes) -> ExecResult:
        self.log.append(argv)
        binary = argv[0]
        if binary == "/bin/tar":
            dest = self.dirs[argv[argv.index("-C") + 1]]
            _extract(stdin, dest)
            return ExecResult()
        if binary == "/bin/sh" and argv[2:3] == [IDENTITY_SCRIPT]:
            if self.account is None:
                return ExecResult(stdout=b"1000\n1000\nnonroot\n", exit_code=2)
            name, uid, gid, home = self.account
            return ExecResult(stdout=f"{uid}\n{gid}\n{name}\n{name}:x:{uid}:{gid}::{home}:/bin/bash\n".encode())
        if binary == "/bin/cat":
            data = self.files.get(argv[1])
            if data is None:
                return ExecResult(stderr=f"cat: {argv[1]}: No such file or directory".encode(), exit_code=1)
            return ExecResult(stdout=data)
        if READY_SCRIPT in argv:
            return ExecResult(exit_code=0 if self.gateway_listening else 1)
        started_module = argv[1:] in (["-m", "aiep.gateway"], ["-m", "aiep.egress_proxy"])
        if VERIFY_SCRIPT in argv or started_module:
            if binary != self.python:
                return ExecResult(stderr=f"exec: {binary}: no such file or directory".encode(), exit_code=127)
            if started_module:
                self.gateway_started.append(argv)
                self.started_after_listings = self.listings
                self.gateway_listening = True
                self.gateway_alive.set()
                return ExecResult(hang=_GatewayLife(self))
            root = self.dirs[argv[-1]]
            r = subprocess.run([sys.executable, "-I", "-c", VERIFY_SCRIPT, str(root)], capture_output=True)
            self.listings += 1
            out = r.stdout
            if self.owner is not None and r.returncode == 0:
                rows = json.loads(out)
                out = json.dumps([[*row[:6], *self.owner] for row in rows]).encode()
            return ExecResult(stdout=out, stderr=r.stderr, exit_code=r.returncode)
        if binary == "/usr/bin/git":
            r = subprocess.run(["git", *argv[1:]], cwd=self.dirs["/testbed"], capture_output=True,
                               env={"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
            return ExecResult(stdout=r.stdout, stderr=r.stderr, exit_code=r.returncode)
        if binary == "/opt/aiep/claude":
            self.agent.seen_stdin.append(stdin)
            self.agent.seen_argv.append(argv)
            self.agent_alive = True
            if self.agent.before is not None:
                self.agent.before(stdin, self.dirs["/testbed"])
            if self.agent.hang is not None:
                self.agent.hang.wait(30)
                if not self.agent_alive:  # quiesced (killed) while running
                    return ExecResult(exit_code=137)
            out = self.agent.act(stdin, self.dirs["/testbed"])
            self.agent_alive = False
            return ExecResult(stdout=out, exit_code=self.agent.exit_code)
        if binary == "/bin/sh" and argv[2] == QUIESCE_SCRIPT:
            self.gateway_alive.clear()
            self.gateway_listening = False
            self.agent_alive = False
            if self.agent.hang is not None:
                self.agent.hang.set()
            return ExecResult(exit_code=137)
        if binary == "/bin/sh" and argv[2] == LIST_PIDS_SCRIPT:
            return ExecResult(stdout=b"")  # nothing but PID 1 (which the real script also prints is excluded by the caller)
        return ExecResult(stderr=b"fake: unhandled", exit_code=127)


def _extract(data: bytes, dest: Path) -> None:
    """Stand-in for `tar -x -p --no-same-owner` as a non-root user."""
    dirs = []
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        for m in tf:
            target = dest / m.name
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                dirs.append((target, m.mode))
            elif m.issym():
                os.symlink(m.linkname, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tf.extractfile(m).read())
                os.chmod(target, m.mode)
    for d, mode in reversed(dirs):
        os.chmod(d, mode)


class _GatewayLife:
    """An Event-like object: the fake gateway process 'lives' until the container's quiesce clears the flag."""

    def __init__(self, fs):
        self.fs = fs

    def wait(self, timeout=None):
        deadline = time.monotonic() + (timeout or 30)
        while self.fs.gateway_alive.is_set() and time.monotonic() < deadline:
            time.sleep(0.005)
        return True

"""A run whose lease can no longer be refreshed is stopped through the normal lifecycle (fakes only: no Docker, no network)."""
import json
import threading
import time

import pytest

from aiep import artifacts, m2_run
from aiep import lease as L
from aiep import limits as LIM
from aiep.agent_runner import STATUS_EXITED, STATUS_GATEWAY_STOPPED, INVALID_STOP_REASONS
from aiep.lease import RunLease
from fake_docker import FakeAgent
from test_lifecycle import FakeHost, release  # noqa: F401
from test_m2_run import KEY, fake_record, prep, root  # noqa: F401 (fixtures)
from test_agent_runner import repo, seed  # noqa: F401 (fixtures)

RUN = "m2-x-A-20261119T000000Z"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(tmp_path, **kw):
    mono = Clock()
    lease = RunLease(tmp_path / "leases", RUN, interval_s=0, monotonic=mono, stop_after_s=kw.pop("stop_after_s", 90.0), **kw)
    lease.acquire()
    return lease, mono


# ------------------------------------------------------------------ detection
def test_limits_leave_room_between_stopping_and_looking_abandoned():
    assert LIM.HEARTBEAT_INTERVAL_S * 2 < LIM.LEASE_STOP_AFTER_S < LIM.LEASE_EXPIRY_S  # we stop long before the reaper could act
    assert "lease_lost" in INVALID_STOP_REASONS  # a stopped-for-lease run is never counted as a result


def test_a_healthy_lease_is_never_lost_and_an_unheld_one_cannot_be(tmp_path):
    lease, mono = make(tmp_path)
    for _ in range(10):
        mono.t += 30
        assert lease.beat() is True and lease.lost() is None
    lease.release()
    mono.t += 10_000
    assert lease.lost() is None  # released: nothing to lose


def test_lost_after_exactly_stop_after_seconds_without_a_successful_write(tmp_path, monkeypatch):
    lease, mono = make(tmp_path, stop_after_s=90.0)
    monkeypatch.setattr(L.os, "replace", lambda *a: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    mono.t += 89.999
    assert lease.beat() is False and lease.lost() is None
    mono.t += 0.001
    assert lease.lost() == "lease_lost"
    s = lease.summary()
    assert s["lost"] is True and s["lost_detail"]["first_error"].startswith("heartbeat 1:") and "No space" in s["errors"][0]
    monkeypatch.undo()
    mono.t += 1
    assert lease.beat() is True and lease.lost() == "lease_lost"  # sticky: a late recovery does not un-stop the run
    lease.release()


def test_a_successful_beat_before_the_limit_resets_the_clock(tmp_path):
    lease, mono = make(tmp_path, stop_after_s=90.0)
    mono.t += 80
    assert lease.beat() is True
    mono.t += 80
    assert lease.lost() is None
    mono.t += 10
    assert lease.lost() == "lease_lost"
    lease.release()


def test_the_wall_clock_jumping_does_not_count_only_the_monotonic_clock_does(tmp_path):
    wall = Clock()
    mono = Clock()
    lease = RunLease(tmp_path, RUN, interval_s=0, clock=wall, monotonic=mono, stop_after_s=90)
    lease.acquire()
    wall.t += 3600  # e.g. the laptop slept: wall time moved, monotonic did not
    assert lease.lost() is None
    lease.release()


def test_a_dead_heartbeat_thread_is_detected_by_the_clock_and_recorded(tmp_path, monkeypatch):
    mono = Clock()
    lease = RunLease(tmp_path, RUN, interval_s=0.01, monotonic=mono, stop_after_s=90)
    monkeypatch.setattr(RunLease, "beat", lambda self: (_ for _ in ()).throw(TypeError("bug in the heartbeat")))
    lease.acquire()
    deadline = time.monotonic() + 5
    while not lease.errors and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "heartbeat thread: TypeError" in lease.errors[0]  # recorded, and the thread keeps trying instead of dying silently
    mono.t += 91
    assert lease.lost() == "lease_lost"
    monkeypatch.undo()
    lease.release()


def test_lost_never_blocks_and_release_never_hangs_even_if_the_heartbeat_is_stuck_in_a_write(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "RELEASE_WAIT_S", 0.2)
    lease, mono = make(tmp_path)
    lease._lock.acquire()  # a heartbeat thread blocked inside fsync holds this lock
    try:
        mono.t += 500
        t0 = time.monotonic()
        assert lease.lost() == "lease_lost"  # lock-free
        lease.release()  # gives up waiting, removes the file anyway, records it
        assert time.monotonic() - t0 < 3
    finally:
        lease._lock.release()
    assert not (tmp_path / "leases" / f"{RUN}.lease").exists() and "did not finish" in lease.errors[-1]


def test_release_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    lease, _ = make(tmp_path)
    real = type(lease.path).unlink
    monkeypatch.setattr(type(lease.path), "unlink", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError("nope")))
    lease.release()
    assert "release: PermissionError" in lease.errors[-1]
    monkeypatch.setattr(type(lease.path), "unlink", real)


# ------------------------------------------------------------------ the stop condition
def test_the_stop_callable_puts_the_lease_first_and_only_then_asks_the_gateway(tmp_path):
    lease, mono = make(tmp_path, stop_after_s=90)
    asked, seen = [], {}

    def gateway_stopped():
        asked.append(1)
        return "budget_exhausted"

    def fake_run_agent(client, aspec, stopped, **kw):
        seen["kw"] = kw
        seen["healthy"] = stopped()
        mono.t += 91
        seen["lost"] = stopped()
        return "result"

    out = m2_run.leased_agent_runner(lease, fake_run_agent)("client", "aspec", gateway_stopped, poll_interval=0.5)
    assert out == "result" and seen["kw"] == {"poll_interval": 0.5}
    assert seen["healthy"] == "budget_exhausted" and seen["lost"] == "lease_lost" and asked == [1]  # gateway not consulted once lost
    lease.release()


# ------------------------------------------------------------------ end to end with the real lifecycle and fake Docker
@pytest.fixture
def failing_replace(monkeypatch):
    flag = threading.Event()
    real = L.os.replace

    def replace(src, dst, *a, **k):
        if flag.is_set():
            raise OSError(28, "No space left on device")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(L.os, "replace", replace)
    return flag


def build(tmp_path, root, agent):
    p = prep(root)
    host = FakeHost(tmp_path / "host", p.aspec, p.gspec, agent=agent)
    host.gw_fs.files["/tmp/ledger.jsonl"] = b""  # a real gateway creates its (empty) ledger at startup; a missing one is incomplete accounting
    return p, host


def test_a_heartbeat_failure_during_the_run_stops_it_tears_everything_down_and_persists_why(tmp_path, root, failing_replace):
    hang = threading.Event()
    p, host = build(tmp_path, root, FakeAgent(hang=hang))
    orig = host.ag_fs.handler

    def handler(argv, rec, stdin):
        if argv[0] == "/opt/aiep/claude":
            failing_replace.set()  # from the moment Claude is running, the lease can no longer be refreshed
        return orig(argv, rec, stdin)
    host.ag_fs.handler = handler
    ex = m2_run.execute(host, p, base=root / "runs" / "m2", root=root, lease_interval_s=0.02, lease_stop_after_s=0.4)
    failing_replace.clear()
    hang.set()
    release(host)

    agent = ex.lifecycle.agent
    assert agent.status == STATUS_GATEWAY_STOPPED and agent.stop_reason == "lease_lost" and agent.valid is False
    assert agent.patch is None and "host-side lease" in agent.invalid_reason and agent.removed and ex.lifecycle.gateway_removed
    ev = host.events
    assert ev.index("agent.remove") < ev.index("gateway.remove") < ev.index("proxy.remove")  # agent, then gateway, then proxy
    assert ev.count("agent.remove") == ev.count("gateway.remove") == ev.count("proxy.remove") == 1 and ex.lifecycle.proxy_removed
    assert {e.split(".")[0] for e in ev} == {"agent", "gateway", "proxy"}  # only this run's three containers were ever touched
    # artifacts are written, intact, and say exactly what happened
    assert ex.problems == [] and artifacts.verify_run_artifacts(ex.directory) == []
    names = {f.name for f in ex.directory.iterdir()}
    assert "patch.diff" not in names and "predictions.jsonl" not in names and ex.eval_command is None
    run_json = json.loads((ex.directory / "run.json").read_text())
    assert run_json["termination"]["reason"] == "lease_lost" and "OSError" in run_json["termination"]["first_heartbeat_error"]
    assert run_json["lease"]["lost"] is True and any("No space" in e for e in run_json["lease"]["errors"])
    assert run_json["lifecycle"]["agent"]["stop_reason"] == "lease_lost" and run_json["lifecycle"]["agent"]["valid"] is False
    assert KEY.encode() not in b"".join(f.read_bytes() for f in ex.directory.iterdir())
    # and the lease itself is gone
    assert not (root / ".cache" / "leases" / f"{p.run_id}.lease").exists()


def test_a_short_heartbeat_hiccup_does_not_stop_a_healthy_run(tmp_path, root, failing_replace):
    hang = threading.Event()
    p, host = build(tmp_path, root, FakeAgent(hang=hang))
    orig = host.ag_fs.handler

    def handler(argv, rec, stdin):
        if argv[0] == "/opt/aiep/claude":
            failing_replace.set()
            threading.Timer(0.15, failing_replace.clear).start()  # shorter than the stop limit
            threading.Timer(0.5, hang.set).start()  # the agent finishes normally
        return orig(argv, rec, stdin)
    host.ag_fs.handler = handler
    ex = m2_run.execute(host, p, base=root / "runs" / "m2", root=root, lease_interval_s=0.02, lease_stop_after_s=30)
    release(host)
    assert ex.lifecycle.agent.status == STATUS_EXITED and ex.lifecycle.agent.valid is True
    run_json = json.loads((ex.directory / "run.json").read_text())
    assert run_json["termination"] is None and run_json["lease"]["lost"] is False and run_json["lease"]["errors"]  # recorded, harmless


def test_a_healthy_run_is_unaffected_by_the_lease_supervision(tmp_path, root):
    p, host = build(tmp_path, root, None)
    ex = m2_run.execute(host, p, base=root / "runs" / "m2", root=root, lease_interval_s=0.02)
    release(host)
    assert ex.lifecycle.status == "completed" and ex.lifecycle.agent.valid and ex.problems == []
    assert json.loads((ex.directory / "run.json").read_text())["termination"] is None


def test_the_lease_is_released_and_both_containers_removed_when_a_stop_races_with_an_interrupt(tmp_path, root, failing_replace, monkeypatch):
    hang = threading.Event()
    p, host = build(tmp_path, root, FakeAgent(hang=hang))
    orig = host.ag_fs.handler
    seen = {"n": 0}
    real_lost = RunLease.lost

    def lost_then_interrupt(self):
        seen["n"] += 1
        if seen["n"] > 3:
            raise KeyboardInterrupt  # Ctrl-C arrives inside the supervision loop
        return real_lost(self)
    monkeypatch.setattr(RunLease, "lost", lost_then_interrupt)

    def handler(argv, rec, stdin):
        return orig(argv, rec, stdin)
    host.ag_fs.handler = handler
    with pytest.raises(KeyboardInterrupt):
        m2_run.execute(host, p, base=root / "runs" / "m2", root=root, lease_interval_s=0.02)
    hang.set()
    release(host)
    assert "agent.remove" in host.events and "gateway.remove" in host.events
    assert not (root / ".cache" / "leases" / f"{p.run_id}.lease").exists()

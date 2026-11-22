import json
import os
import stat
import threading
import time

import pytest

from aiep import lease as L
from aiep import limits as LIM
from aiep.lease import LeaseError, RunLease, read_lease_status

NOW = 1_800_000_000.0
RUN = "m2-pytest-dev__pytest-5227-A-20261119T231100Z"


def write_lease(d, run=RUN, beat=NOW, **over):
    d.mkdir(parents=True, exist_ok=True)
    doc = {"schema": 1, "run_id": run, "pid": 1, "started_at": beat, "heartbeat_at": beat, "beat": 0}
    doc.update(over)
    (d / f"{run}.lease").write_text(json.dumps(doc))
    return d / f"{run}.lease"


def status(d, now=NOW, run=RUN):
    return read_lease_status(d, run, now=now)


# ------------------------------------------------------------------ central limits
def test_limits_are_consistent():
    assert LIM.MIN_WALL_CLOCK_S < LIM.DEFAULT_WALL_CLOCK_S <= LIM.MAX_WALL_CLOCK_S
    assert LIM.MIN_TTL_S == LIM.MAX_WALL_CLOCK_S + LIM.SETUP_TEARDOWN_MARGIN_S  # older than any legitimate run could be
    assert LIM.MIN_TTL_S > LIM.DEFAULT_WALL_CLOCK_S * 2 and LIM.DEFAULT_TTL_S >= LIM.MIN_TTL_S
    assert LIM.HEARTBEAT_INTERVAL_S * 5 <= LIM.LEASE_EXPIRY_S  # several missed beats before a lease looks expired
    assert LIM.MIN_LEASE_GRACE_S >= LIM.LEASE_EXPIRY_S and LIM.DEFAULT_LEASE_GRACE_S >= LIM.MIN_LEASE_GRACE_S


# ------------------------------------------------------------------ the orchestrator's side
def test_acquire_creates_a_private_exclusive_lease_and_release_removes_it(tmp_path):
    d = tmp_path / "leases"
    lease = RunLease(d, RUN, interval_s=0)
    lease.acquire()
    path = d / f"{RUN}.lease"
    assert path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(d.stat().st_mode) == 0o700
    doc = json.loads(path.read_text())
    assert doc["run_id"] == RUN and doc["pid"] == os.getpid() and doc["schema"] == 1
    assert set(doc) == {"schema", "run_id", "pid", "started_at", "heartbeat_at", "beat"}  # no secrets, nothing else
    lease.release()
    assert not path.exists()
    lease.release()  # idempotent


def test_a_second_lease_for_the_same_run_is_refused_and_never_replaces_the_first(tmp_path):
    first = RunLease(tmp_path, RUN, interval_s=0)
    first.acquire()
    before = (tmp_path / f"{RUN}.lease").read_bytes()
    with pytest.raises(LeaseError, match="already exists"):
        RunLease(tmp_path, RUN, interval_s=0).acquire()
    assert (tmp_path / f"{RUN}.lease").read_bytes() == before
    first.release()


def test_a_stale_leftover_lease_also_blocks_reuse(tmp_path):
    write_lease(tmp_path, beat=1.0)
    with pytest.raises(LeaseError):
        RunLease(tmp_path, RUN, interval_s=0).acquire()


@pytest.mark.parametrize("rid", ["", "../x", "a/b", "-x", "x" * 65, "a b", None])
def test_bad_run_ids_are_refused(tmp_path, rid):
    with pytest.raises(LeaseError):
        RunLease(tmp_path, rid)


def test_a_symlink_in_the_lease_path_is_not_followed(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("x")
    (tmp_path / f"{RUN}.lease").symlink_to(target)
    with pytest.raises(LeaseError):
        RunLease(tmp_path, RUN, interval_s=0).acquire()
    assert target.read_text() == "x"


def test_beat_refreshes_the_heartbeat_atomically_with_the_injected_clock(tmp_path):
    t = [NOW]
    lease = RunLease(tmp_path, RUN, interval_s=0, clock=lambda: t[0])
    lease.acquire()
    for n in range(1, 4):
        t[0] += 30
        assert lease.beat() is True
        assert json.loads((tmp_path / f"{RUN}.lease").read_text())["heartbeat_at"] == NOW + 30 * n
    assert status(tmp_path, now=NOW + 100).status == "fresh"
    assert [p.name for p in tmp_path.iterdir()] == [f"{RUN}.lease"]  # no temp files left behind
    lease.release()


def test_the_background_thread_keeps_beating_until_release(tmp_path):
    lease = RunLease(tmp_path, RUN, interval_s=0.02)
    lease.acquire()
    deadline = time.monotonic() + 5
    while lease.beats < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert lease.beats >= 3 and lease.errors == []
    lease.release()
    n = lease.beats
    time.sleep(0.1)
    assert lease.beats == n and not (tmp_path / f"{RUN}.lease").exists()
    assert not [t for t in threading.enumerate() if t.name == f"lease-{RUN}"]


def test_a_heartbeat_failure_is_recorded_and_never_raised_and_release_still_works(tmp_path, monkeypatch):
    lease = RunLease(tmp_path, RUN, interval_s=0)
    lease.acquire()
    monkeypatch.setattr(L.os, "replace", lambda *a: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    assert lease.beat() is False and "No space" in lease.errors[0]
    assert [p.name for p in tmp_path.iterdir()] == [f"{RUN}.lease"]
    monkeypatch.undo()
    lease.release()
    assert not (tmp_path / f"{RUN}.lease").exists()


def test_context_manager_releases_on_exceptions_and_interrupts(tmp_path):
    for exc in (RuntimeError("x"), KeyboardInterrupt()):
        d = tmp_path / type(exc).__name__
        with pytest.raises(type(exc)):
            with RunLease(d, RUN, interval_s=0):
                assert (d / f"{RUN}.lease").exists()
                raise exc
        assert not (d / f"{RUN}.lease").exists()


def test_a_killed_orchestrator_leaves_a_lease_that_stops_being_refreshed(tmp_path):
    lease = RunLease(tmp_path, RUN, interval_s=0, clock=lambda: NOW)
    lease.acquire()  # the process is then killed: no release, no more beats
    assert status(tmp_path, now=NOW + 10).status == "fresh"
    assert status(tmp_path, now=NOW + LIM.LEASE_EXPIRY_S + 1).status == "expired"


# ------------------------------------------------------------------ the reader: statuses and boundaries
def test_missing_lease_with_and_without_the_directory(tmp_path):
    assert status(tmp_path / "nope").status == "missing"
    (tmp_path / "d").mkdir()
    assert status(tmp_path / "d").status == "missing"


def test_fresh_expired_and_the_exact_boundary(tmp_path):
    write_lease(tmp_path)
    assert status(tmp_path, NOW).status == "fresh" and status(tmp_path, NOW).silence_s == 0
    assert status(tmp_path, NOW + LIM.LEASE_EXPIRY_S - 0.001).status == "fresh"
    s = status(tmp_path, NOW + LIM.LEASE_EXPIRY_S)  # exactly the expiry: expired (the beat is no longer "younger than")
    assert s.status == "expired" and s.silence_s == LIM.LEASE_EXPIRY_S
    assert status(tmp_path, NOW + 10 * LIM.LEASE_EXPIRY_S).status == "expired"


def test_clock_skew_tolerance(tmp_path):
    write_lease(tmp_path, beat=NOW + LIM.LEASE_FUTURE_TOLERANCE_S)  # slightly ahead: still a live lease
    s = status(tmp_path, NOW)
    assert s.status == "fresh" and s.silence_s == 0
    write_lease_path = tmp_path / f"{RUN}.lease"
    write_lease_path.unlink()
    write_lease(tmp_path, beat=NOW + LIM.LEASE_FUTURE_TOLERANCE_S + 1)  # implausible: fail closed
    assert status(tmp_path, NOW).status == "invalid"


@pytest.mark.parametrize("content", [
    "not json", "", "[]", "null", '{"schema": 2, "run_id": "%s", "heartbeat_at": 1.0}' % RUN,
    '{"schema": 1, "run_id": "other", "heartbeat_at": 1000.0}', '{"schema": 1, "run_id": "%s"}' % RUN,
    '{"schema": 1, "run_id": "%s", "heartbeat_at": true}' % RUN, '{"schema": 1, "run_id": "%s", "heartbeat_at": "1000"}' % RUN,
    '{"schema": 1, "run_id": "%s", "heartbeat_at": NaN}' % RUN, '{"schema": 1, "run_id": "%s", "heartbeat_at": Infinity}' % RUN,
    '{"schema": 1, "run_id": "%s", "heartbeat_at": 0}' % RUN, '{"schema": 1, "run_id": "%s", "heartbeat_at": -5}' % RUN,
    "x" * (L.MAX_LEASE_BYTES + 10),
])
def test_unparseable_or_implausible_leases_are_invalid_never_missing(tmp_path, content):
    (tmp_path / f"{RUN}.lease").write_text(content)
    assert status(tmp_path).status == "invalid"


def test_a_symlink_a_directory_and_unreadable_files_are_invalid(tmp_path):
    (tmp_path / "real").write_text("{}")
    (tmp_path / f"{RUN}.lease").symlink_to(tmp_path / "real")
    assert status(tmp_path).status == "invalid"
    (tmp_path / f"{RUN}.lease").unlink()
    (tmp_path / f"{RUN}.lease").mkdir()
    assert status(tmp_path).status == "invalid"
    (tmp_path / f"{RUN}.lease").rmdir()
    p = write_lease(tmp_path)
    os.chmod(p, 0)
    try:
        if os.geteuid() != 0:
            assert status(tmp_path).status == "invalid"
    finally:
        os.chmod(p, 0o600)


def test_an_unreadable_directory_is_invalid_not_missing(tmp_path):
    d = tmp_path / "leases"
    write_lease(d)
    os.chmod(d, 0)
    try:
        if os.geteuid() != 0:
            assert status(d).status == "invalid"
    finally:
        os.chmod(d, 0o700)


def test_a_bad_run_id_is_invalid(tmp_path):
    assert read_lease_status(tmp_path, "../x", now=NOW).status == "invalid"

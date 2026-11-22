"""A host-side run lease: the live orchestrator's proof that its containers are not abandoned.

Single machine, no coordination service. For each run the orchestrator creates ONE small file, ``<lease_dir>/<run_id>.lease``
(exclusively, mode 0600), BEFORE it creates any container, refreshes it every ``HEARTBEAT_INTERVAL_S`` from a daemon thread
(atomic replace), and removes it when the run is over. The reaper reads it and never removes a container whose run has a fresh
lease, however old that container is: abandonment is judged from the lease, not from creation time.

Statuses (``read_lease_status``)::

    fresh     the last heartbeat is younger than LEASE_EXPIRY_S             -> the run is alive: protected
    expired   older than LEASE_EXPIRY_S (the orchestrator stopped beating)   -> reapable only after a further grace period
    missing   no lease file (never taken, or the run ended and released it) -> the caller falls back to the container's age
    invalid   anything unreadable or implausible                           -> NEVER treated as abandoned (fail closed)

A lease file that cannot be read, parsed or trusted is ``invalid``, not ``missing``: failing to read must never lead to a deletion.
The lease holds no secrets (run id, pid, timestamps).
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .limits import HEARTBEAT_INTERVAL_S, LEASE_EXPIRY_S, LEASE_FUTURE_TOLERANCE_S, LEASE_STOP_AFTER_S

SCHEMA = 1
MAX_LEASE_BYTES = 4096
RELEASE_WAIT_S = 5.0
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


class LeaseError(RuntimeError):
    """A lease could not be taken (it already exists, or the directory is unusable)."""


def lease_path(lease_dir: Path, run_id: str) -> Path:
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise LeaseError("run_id must be a plain identifier")
    return Path(lease_dir) / f"{run_id}.lease"


@dataclass(frozen=True)
class LeaseStatus:
    status: str  # "fresh" | "expired" | "missing" | "invalid"
    silence_s: float | None  # seconds since the last heartbeat (fresh and expired only)
    detail: str


def read_lease_status(lease_dir: Path, run_id: str, *, now: float, expiry_s: float = LEASE_EXPIRY_S) -> LeaseStatus:
    """Classify ``run_id``'s lease at time ``now``. Never raises: every failure is ``invalid`` (fail closed)."""
    try:
        path = lease_path(lease_dir, run_id)
    except LeaseError as e:
        return LeaseStatus("invalid", None, str(e))
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)  # a symlink is never followed
    except FileNotFoundError:
        return LeaseStatus("missing", None, "no lease file")
    except OSError as e:
        return LeaseStatus("invalid", None, f"lease not readable: {type(e).__name__}")
    try:
        with os.fdopen(fd, "rb") as f:
            raw = f.read(MAX_LEASE_BYTES + 1)
    except OSError as e:
        return LeaseStatus("invalid", None, f"lease not readable: {type(e).__name__}")
    if len(raw) > MAX_LEASE_BYTES:
        return LeaseStatus("invalid", None, "lease file is too large")
    try:
        doc = json.loads(raw)
    except ValueError:
        return LeaseStatus("invalid", None, "lease file is not valid JSON")
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or doc.get("run_id") != run_id:
        return LeaseStatus("invalid", None, "lease file does not describe this run")
    beat = doc.get("heartbeat_at")
    if isinstance(beat, bool) or not isinstance(beat, (int, float)) or not math.isfinite(beat) or beat <= 0:
        return LeaseStatus("invalid", None, "lease heartbeat is not a valid time")
    silence = now - beat
    if silence < -LEASE_FUTURE_TOLERANCE_S:
        return LeaseStatus("invalid", silence, "lease heartbeat is implausibly far in the future")
    silence = max(silence, 0.0)
    if silence < expiry_s:
        return LeaseStatus("fresh", silence, f"heartbeat {silence:.0f}s ago")
    return LeaseStatus("expired", silence, f"no heartbeat for {silence:.0f}s")


class RunLease:
    """The orchestrator's side: ``acquire`` before any container exists, ``release`` when the run is over (idempotent).

    It also answers ``lost()``: True once no heartbeat has been written successfully for ``stop_after_s`` (default
    ``LEASE_STOP_AFTER_S``, three missed beats), for ANY reason: a failing write, a dead or hung heartbeat thread, a full disk.
    The supervising loop polls it (``m2_run``), and a lost lease ends the run through the normal lifecycle, so a run never keeps
    spending while the reaper's evidence of its life is going stale. ``lost()`` reads only plain attributes, takes no lock and does
    no I/O, so it works even while the heartbeat thread is blocked inside a write. It uses a monotonic clock, so a sleeping laptop
    does not count as silence.
    """

    def __init__(self, lease_dir: Path, run_id: str, *, interval_s: float = HEARTBEAT_INTERVAL_S,
                 clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
                 stop_after_s: float = LEASE_STOP_AFTER_S):
        self.path = lease_path(lease_dir, run_id)
        self.run_id, self.interval_s, self.clock, self.monotonic = run_id, interval_s, clock, monotonic
        self.stop_after_s = stop_after_s
        self.beats = 0
        self.errors: list[str] = []
        self._started_at = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._held = False
        self._last_ok_mono: float | None = None
        self._lost_reason: str | None = None
        self._lost_info: dict[str, object] | None = None

    # -- context manager
    def __enter__(self) -> "RunLease":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def _doc(self) -> bytes:
        return (json.dumps({"schema": SCHEMA, "run_id": self.run_id, "pid": os.getpid(), "started_at": self._started_at,
                            "heartbeat_at": self.clock(), "beat": self.beats}, sort_keys=True) + "\n").encode()

    def acquire(self) -> None:
        """Create the lease exclusively and start the heartbeat. Raises ``LeaseError`` if one already exists."""
        if self._held:
            raise LeaseError("this lease is already held")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._started_at = self.clock()
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError as e:
            raise LeaseError(f"a lease for {self.run_id} already exists ({self.path}); a run id is never reused") from e
        except OSError as e:
            raise LeaseError(f"cannot create the lease: {type(e).__name__}: {e}") from e
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(self._doc())
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            self.path.unlink(missing_ok=True)
            raise LeaseError(f"cannot write the lease: {type(e).__name__}: {e}") from e
        self._last_ok_mono = self.monotonic()
        self._held = True
        if self.interval_s > 0:
            self._thread = threading.Thread(target=self._run, name=f"lease-{self.run_id}", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.beat()
            except Exception as e:  # the thread must never die silently: record it and keep trying (lost() watches the clock)
                self.errors.append(f"heartbeat thread: {type(e).__name__}: {e}")

    def beat(self) -> bool:
        """Refresh the heartbeat now (atomic replace). A failure is recorded and never raises into the run."""
        with self._lock:
            if not self._held:
                return False
            tmp = self.path.with_name(f".{self.run_id}.{os.getpid()}.tmp")
            try:
                self.beats += 1
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(self._doc())
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
                self._last_ok_mono = self.monotonic()
                return True
            except OSError as e:
                self.errors.append(f"heartbeat {self.beats}: {type(e).__name__}: {e}")
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                return False

    def lost(self) -> str | None:
        """``"lease_lost"`` once the lease has gone ``stop_after_s`` without a successful write (sticky), else None."""
        if self._lost_reason is not None:
            return self._lost_reason
        last = self._last_ok_mono
        if not self._held or last is None:
            return None
        silent = self.monotonic() - last
        if silent >= self.stop_after_s:
            self._lost_reason = "lease_lost"
            self._lost_info = {"silent_s": round(silent, 3), "first_error": self.errors[0] if self.errors else None,
                               "stop_after_s": self.stop_after_s}
        return self._lost_reason

    def summary(self) -> dict[str, object]:
        """What a run record stores: counts, every error, and whether (and why) the lease was lost."""
        return {"heartbeats": self.beats, "errors": list(self.errors), "lost": self._lost_reason is not None,
                "lost_detail": self._lost_info}

    def release(self) -> None:
        """Stop the heartbeat and remove the lease. Safe to call twice and from a ``finally``; never raises, never hangs.

        If the heartbeat thread is stuck inside a write (it holds the lock), release does not wait for it forever: it gives up the
        join and the lock after a few seconds, still tries to remove the file, and records what happened.
        """
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=RELEASE_WAIT_S)
        got = self._lock.acquire(timeout=RELEASE_WAIT_S)
        try:
            if not self._held:
                return
            self._held = False
            if not got:
                self.errors.append("release: the heartbeat thread did not finish; removing the lease anyway")
            try:
                self.path.unlink(missing_ok=True)
            except OSError as e:
                self.errors.append(f"release: {type(e).__name__}: {e}")
        finally:
            if got:
                self._lock.release()

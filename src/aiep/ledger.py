"""Append-only usage ledger (JSONL). Never records prompts, responses, keys or tokens."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


class Ledger:
    """Thread-safe running total plus one JSON line per request."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._spent = Decimal(0)
        self._n = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 'x': a run's ledger is never appended to by a second process
        with open(self.path, "x"):
            pass

    @property
    def spent(self) -> Decimal:
        with self._lock:
            return self._spent

    def record(self, entry: dict[str, Any], cost_usd: Decimal | None = None) -> Decimal:
        """Write one entry; returns the cumulative computed spend after it."""
        with self._lock:
            if cost_usd is not None:
                self._spent += cost_usd
            self._n += 1
            line = {
                "seq": self._n,
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                **entry,
                "cost_usd_computed": None if cost_usd is None else str(cost_usd),
                "cumulative_usd_computed": str(self._spent),
            }
            with open(self.path, "a") as f:
                f.write(json.dumps(line, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return self._spent


def audit(ledger: bytes | str | None) -> dict[str, Any]:
    """Which requests the ledger shows as started but never finished.

    A ``forwarding`` line is written before a request goes upstream; every outcome of that request is a later line with the same ``rid``.
    ``unresolved`` are the ``forwarding`` lines with no such line: usage the gateway may owe and has not recorded. A line that is not valid
    JSON is counted (``unparseable_lines``): it could have been a finishing line, so it makes the accounting incomplete too.
    """
    if ledger is None:
        return {"readable": False, "forwarded": 0, "finished": 0, "unresolved": [], "unparseable_lines": 0, "complete": False}
    text = ledger.decode("utf-8", "replace") if isinstance(ledger, (bytes, bytearray)) else ledger
    started: dict[str, dict[str, Any]] = {}
    finished: set[str] = set()
    bad = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if not isinstance(e, dict):
            bad += 1
            continue
        rid = e.get("rid")
        if not isinstance(rid, str):
            continue
        if e.get("outcome") == "forwarding":
            started[rid] = {k: e.get(k) for k in ("rid", "seq", "ts", "endpoint", "requested_max_tokens", "stream")}
        else:
            finished.add(rid)
    unresolved = [v for k, v in started.items() if k not in finished]
    return {"readable": True, "forwarded": len(started), "finished": len(started) - len(unresolved), "unresolved": unresolved,
            "unparseable_lines": bad, "complete": not unresolved and bad == 0}


def request_status_summary(ledger: bytes | str | None) -> dict[str, Any]:
    """What happened to every request the gateway saw, from the ledger alone (retry and error observability).

    Claude Code retries a failed request by sending ANOTHER request, so retries show up here as extra finished requests with a non-success status or outcome.
    ``non_success`` is therefore a lower-bound proxy for retries (a retry of a request the gateway never saw is invisible), never an exact retry count.
    Categories are disjoint and cover every finishing line: ``success`` (HTTP 200, outcome ok), ``rate_limited_429`` (the upstream's or the gateway's 429),
    ``overloaded_529``, ``server_5xx`` (other upstream 5xx), ``client_4xx`` (other 4xx), ``gateway_refused`` (refused before upstream: auth, budget, stop, model),
    ``upstream_unavailable_502`` (proxy or TLS or connection failure), ``interrupted`` (stream cut: upstream_read_error, client_disconnected, response too large)
    and ``other``. ``usage_incomplete_upper_bound`` counts requests charged at the max_tokens upper bound.
    """
    out: dict[str, Any] = {"readable": ledger is not None, "finished_requests": 0, "by_status": {}, "by_outcome": {}, "refused_by_reason": {}, "categories": {
        k: 0 for k in ("success", "rate_limited_429", "overloaded_529", "server_5xx", "client_4xx", "gateway_refused", "upstream_unavailable_502", "interrupted", "other")},
        "usage_incomplete_upper_bound": 0, "gateway_busy_429": 0}
    if ledger is None:
        out["non_success"] = None
        return out
    text = ledger.decode("utf-8", "replace") if isinstance(ledger, (bytes, bytearray)) else ledger
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("outcome") in ("forwarding", None):
            continue
        out["finished_requests"] += 1
        status, outcome, reason = e.get("status"), str(e.get("outcome")), str(e.get("reason", ""))
        out["by_status"][str(status)] = out["by_status"].get(str(status), 0) + 1
        out["by_outcome"][outcome] = out["by_outcome"].get(outcome, 0) + 1
        if e.get("usage_incomplete_upper_bound"):
            out["usage_incomplete_upper_bound"] += 1
        cat = "other"
        if outcome == "refused":
            cat = "gateway_refused"
            out["refused_by_reason"][reason] = out["refused_by_reason"].get(reason, 0) + 1
            if status == 429:
                out["gateway_busy_429"] += 1
                cat = "rate_limited_429"
        elif outcome in ("upstream_read_error", "client_disconnected", "response_too_large"):
            cat = "interrupted"
        elif status == 200 and outcome == "ok":
            cat = "success"
        elif status == 429:
            cat = "rate_limited_429"
        elif status == 529:
            cat = "overloaded_529"
        elif status == 502 and "upstream unavailable" in reason:
            cat = "upstream_unavailable_502"
        elif isinstance(status, int) and 500 <= status < 600:
            cat = "server_5xx"
        elif isinstance(status, int) and 400 <= status < 500:
            cat = "client_4xx"
        out["categories"][cat] += 1
    out["non_success"] = out["finished_requests"] - out["categories"]["success"]
    return out

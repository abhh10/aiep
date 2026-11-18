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

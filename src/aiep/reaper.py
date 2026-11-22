"""Removal of abandoned AIEP containers: a LEASE decides, never container age alone.

An orchestrator holds a host-side lease for its run (``aiep.lease``) and refreshes it while it works. A container is reapable only
if ALL of these hold, on the list entry and again immediately before removal:

* shape (unchanged): label ``aiep.run_id`` (a plain run identifier), ``aiep.role`` in {agent, gateway, proxy, seed_source}, and exactly the
  name AIEP gives that role (``aiep.agent.<run_id>``, ``aiep.gateway.<run_id>``, ``aiep.proxy.<run_id>``, ``aiep.seedsrc.<run_id>``);
* its run's lease says it is abandoned:

  =========  ==========================================================================================================
  fresh      NEVER reapable, however old the container is (this is what protects a live run);
  expired    reapable only after the silence reaches ``LEASE_EXPIRY_S + lease_grace_s`` (the orchestrator stopped beating);
  missing    reapable only when the container is older than ``ttl_s`` (at least ``MIN_TTL_S``, which exceeds the longest
             legitimate run). This covers tools that take no lease (seed extraction, injection check) and leaked containers;
  invalid    NEVER reapable: a lease that cannot be read or trusted fails closed, so a read error never deletes anything.
  =========  ==========================================================================================================

Everything else is skipped and reported, never touched: containers without these labels, evaluator containers (no run label), and
any image, volume or network. Removal is by exact container ID. The default is a dry run; nothing is removed unless ``execute=True``.
A run's agent, gateway and proxy share its run id and therefore its lease, so they are protected or released together.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .lease import LeaseStatus, read_lease_status
from .limits import (DEFAULT_LEASE_GRACE_S, DEFAULT_TTL_S, LEASE_EXPIRY_S, MIN_LEASE_GRACE_S, MIN_TTL_S)

ROLE_PREFIX = {"agent": "aiep.agent.", "gateway": "aiep.gateway.", "proxy": "aiep.proxy.", "seed_source": "aiep.seedsrc."}
RUN_LABEL, ROLE_LABEL = "aiep.run_id", "aiep.role"
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
_ID = re.compile(r"^[0-9a-f]{12,64}$")
__all__ = ["DEFAULT_TTL_S", "MIN_TTL_S", "DEFAULT_LEASE_GRACE_S", "MIN_LEASE_GRACE_S", "Item", "ReapReport", "reap"]


@dataclass
class Item:
    container_id: str
    name: str
    role: str
    age_s: float
    action: str  # "remove" | "skip"
    reason: str
    lease: str = ""  # the lease status that decided it
    state: str = ""  # Docker's state for the container (informational), e.g. running
    removed: bool = False
    error: str | None = None


@dataclass
class ReapReport:
    executed: bool
    ttl_s: int
    lease_grace_s: int
    items: list[Item] = field(default_factory=list)

    @property
    def removed(self) -> list[Item]:
        return [i for i in self.items if i.removed]

    @property
    def errors(self) -> list[Item]:
        return [i for i in self.items if i.error]


def _shape(name: str, labels: dict[str, Any], container_id: Any) -> tuple[str | None, str]:
    """(role, reason). role is None when the container is not an AIEP-owned, correctly shaped one."""
    if not isinstance(container_id, str) or not _ID.fullmatch(container_id):
        return None, "unexpected container id"
    run_id, role = labels.get(RUN_LABEL), labels.get(ROLE_LABEL)
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        return None, "no valid aiep.run_id label"
    if role not in ROLE_PREFIX:
        return None, f"role {role!r} is not reapable"
    if name != f"{ROLE_PREFIX[role]}{run_id}":
        return None, "name does not match the AIEP name for its role and run id"
    return role, "ok"


def decide(lease: LeaseStatus, *, age_s: float, ttl_s: int, lease_grace_s: int) -> tuple[bool, str]:
    """(reapable, reason) from the lease status and, only for a missing lease, the container's age."""
    if lease.status == "fresh":
        return False, f"active lease ({lease.detail})"
    if lease.status == "invalid":
        return False, f"lease unusable, failing closed ({lease.detail})"
    if lease.status == "expired":
        need = LEASE_EXPIRY_S + lease_grace_s
        if lease.silence_s is not None and lease.silence_s >= need:
            return True, f"lease expired and silent for {lease.silence_s:.0f}s >= {need:.0f}s (expiry + grace)"
        return False, f"lease expired but within the grace period ({lease.silence_s:.0f}s < {need:.0f}s)"
    if lease.status == "missing":
        if age_s < 0:
            return False, "no lease and the creation time is unreadable"
        if age_s >= ttl_s:
            return True, f"no lease and the container is older than the TTL ({int(age_s)}s >= {ttl_s}s)"
        return False, f"no lease but younger than the TTL ({int(age_s)}s < {ttl_s}s)"
    return False, f"unknown lease status {lease.status!r}, failing closed"


def reap(client: Any, *, lease_dir: Path, ttl_s: int = DEFAULT_TTL_S, lease_grace_s: int = DEFAULT_LEASE_GRACE_S,
         execute: bool = False, now: Callable[[], float] = time.time) -> ReapReport:
    """Plan (and with ``execute=True``, perform) the removal of AIEP containers whose run lease says they are abandoned."""
    if type(ttl_s) is not int or ttl_s < MIN_TTL_S:
        raise ValueError(f"ttl_s must be an integer of at least {MIN_TTL_S} seconds (the longest legitimate run plus setup and teardown)")
    if type(lease_grace_s) is not int or lease_grace_s < MIN_LEASE_GRACE_S:
        raise ValueError(f"lease_grace_s must be an integer of at least {MIN_LEASE_GRACE_S} seconds")
    lease_dir = Path(lease_dir)
    api = client.api
    report = ReapReport(executed=execute, ttl_s=ttl_s, lease_grace_s=lease_grace_s)
    t = now()
    for entry in api.containers(all=True, filters={"label": [RUN_LABEL]}):
        cid = entry.get("Id")
        names = entry.get("Names") or []
        name = names[0].lstrip("/") if names and isinstance(names[0], str) else ""
        created = entry.get("Created")
        labels = entry.get("Labels") or {}
        role, why = _shape(name, labels, cid)
        age = t - created if isinstance(created, (int, float)) and not isinstance(created, bool) else -1.0
        item = Item(str(cid), name, role or "", age, "skip", why, state=str(entry.get("State", "")))
        report.items.append(item)
        if role is None:
            continue
        run_id = labels[RUN_LABEL]
        lease = read_lease_status(lease_dir, run_id, now=t)
        item.lease = lease.status
        ok, item.reason = decide(lease, age_s=age, ttl_s=ttl_s, lease_grace_s=lease_grace_s)
        if not ok:
            continue
        item.action = "remove"
        if not execute:
            continue
        try:
            # fresh checks immediately before acting: the container is unchanged AND the lease has not come back to life
            info = api.inspect_container(cid)
            fresh_name = str(info.get("Name", "")).lstrip("/")
            fresh_role, fresh_why = _shape(fresh_name, (info.get("Config") or {}).get("Labels") or {}, info.get("Id"))
            if fresh_role != role or info.get("Id") != cid:
                item.action, item.reason = "skip", f"changed since listing: {fresh_why}"
                continue
            again = read_lease_status(lease_dir, run_id, now=now())
            ok_again, why_again = decide(again, age_s=age + (now() - t), ttl_s=ttl_s, lease_grace_s=lease_grace_s)
            if not ok_again:
                item.action, item.lease, item.reason = "skip", again.status, f"lease changed before removal: {why_again}"
                continue
            api.remove_container(cid, v=True, force=True)  # by exact ID; force because an abandoned run may still be running
            item.removed = True
        except Exception as e:  # reported, never swallowed, and never stops the other containers
            item.error = f"{type(e).__name__}: {e}"
    return report

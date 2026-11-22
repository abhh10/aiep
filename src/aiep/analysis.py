"""Paired analysis of the M3 pilot results. DESCRIPTIVE: with 8 tasks nothing here is an effectiveness claim, and every report says so.

Two analysis sets are always produced side by side (pre-registered in the experiment definition):

* ``as_scheduled``: every task whose two scheduled runs both HAVE a result row. A run that is invalid, censored, interrupted, rejected by the handoff or ungradable
  counts as UNRESOLVED: failures are never dropped. Tasks with a missing slot (the batch halted first) are listed as ``not_run`` and not counted.
* ``valid_runs``: only tasks where BOTH runs are ``valid_measurement`` (handoff ready or ready_flagged, and a harness grading completed without errors).

For each set: the 2x2 paired table (both resolved / neither / A only / B only, i.e. concordant and discordant pairs, with the task ids per cell), the paired difference
(B only - A only)/n, an exact two-sided McNemar p-value on the discordant pairs and a seeded percentile bootstrap interval over tasks; per-arm cost, token and cache
figures, paired cost differences, request and retry observability, and test usage / compliance. Rows with inexact accounting are counted and flagged, never pooled silently.
"""

from __future__ import annotations

import hashlib
import random
import statistics
from decimal import Decimal
from math import comb
from typing import Any, Iterable, Mapping, Sequence

ARMS = ("A", "B")
LABEL = "FEASIBILITY PILOT: 8 tasks, one run per arm per task. Descriptive only; not a statistically powered effectiveness study."
BOOTSTRAP_RESAMPLES = 5000


def mcnemar_exact_two_sided(b: int, c: int) -> float:
    """Exact two-sided McNemar test on the discordant counts (b = B only, c = A only): a two-sided binomial test of p = 1/2. 1.0 when there are none."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(comb(n, k) for k in range(0, min(b, c) + 1)) / 2**n
    return min(1.0, 2 * tail)


def bootstrap_interval(diffs: Sequence[int], seed: str, resamples: int = BOOTSTRAP_RESAMPLES, level: float = 0.95) -> tuple[float, float] | None:
    """Percentile interval for the mean of per-task paired differences (+1 B only, -1 A only, 0 concordant), resampling TASKS with a seeded RNG."""
    n = len(diffs)
    if n == 0:
        return None
    rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest(), 16))
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))
    lo, hi = means[int((1 - level) / 2 * resamples)], means[min(resamples - 1, int((1 + level) / 2 * resamples))]
    return lo, hi


def _d(x: Any) -> Decimal:
    return Decimal(str(x))


def _slot_outcome(row: Mapping[str, Any]) -> bool:
    return row.get("resolved") is True and row.get("graded") is True


def _arm_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"runs": 0}
    costs = [_d(r["cost_usd_computed"]) for r in rows]
    unc = [_d(r["uncached_equivalent_usd"]) for r in rows]
    resolved = sum(_slot_outcome(r) for r in rows)
    wall = [r["wall_seconds"] for r in rows if isinstance(r.get("wall_seconds"), (int, float))]
    tok = {k: sum(int((r.get("tokens") or {}).get(k, 0)) for r in rows) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_unclassified_tokens",
                                                                                 "cache_write_5m_tokens", "cache_write_1h_tokens")}
    return {"runs": len(rows), "resolved": resolved, "cost_usd_total": format(sum(costs), "f"), "cost_usd_mean": format(sum(costs) / len(costs), "f"),
            "cost_usd_median": format(statistics.median(costs), "f"), "uncached_equivalent_usd_total": format(sum(unc), "f"),
            "cost_per_resolved_usd": format(sum(costs) / resolved, "f") if resolved else None, "wall_seconds_mean": (sum(wall) / len(wall)) if wall else None,
            "tokens": tok, "requests_total": sum(r.get("requests", 0) for r in rows), "non_success_requests_total": sum(r.get("non_success_requests", 0) for r in rows),
            "turns_mean": (sum(r["turns"] for r in rows if isinstance(r.get("turns"), int)) / max(1, sum(isinstance(r.get("turns"), int) for r in rows))),
            "test_usage_runs": sum(bool(r.get("test_usage")) for r in rows), "compliant_runs": sum(bool(r.get("compliance")) for r in rows),
            "test_runs_mean": sum(r.get("test_runs", 0) for r in rows) / len(rows), "test_fix_cycles_mean": sum(r.get("test_fix_cycles", 0) for r in rows) / len(rows),
            "cap_exceeded_runs": sum(bool(r.get("cap_exceeded")) for r in rows), "inexact_accounting_runs": sum(not r.get("cost_exact") for r in rows),
            "budget_or_stop_limited_runs": sum(1 for r in rows if r.get("stop_reason") == "budget_exhausted")}


def _pair_table(pairs: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]], seed: str, set_name: str) -> dict[str, Any]:
    cells: dict[str, list[str]] = {"both_resolved": [], "neither_resolved": [], "A_only": [], "B_only": []}
    diffs = []
    for task, (a, b) in sorted(pairs.items()):
        ra, rb = _slot_outcome(a), _slot_outcome(b)
        cells["both_resolved" if ra and rb else "A_only" if ra else "B_only" if rb else "neither_resolved"].append(task)
        diffs.append(int(rb) - int(ra))
    n = len(pairs)
    bo, ao = len(cells["B_only"]), len(cells["A_only"])
    ci = bootstrap_interval(diffs, f"{seed}|bootstrap|{set_name}")
    return {"pairs": n, "cells": cells, "concordant": len(cells["both_resolved"]) + len(cells["neither_resolved"]), "discordant": ao + bo,
            "resolution_rate": {"A": (sum(_slot_outcome(a) for a, _ in pairs.values()) / n) if n else None, "B": (sum(_slot_outcome(b) for _, b in pairs.values()) / n) if n else None},
            "difference_B_minus_A": ((bo - ao) / n) if n else None, "bootstrap_95_interval": list(ci) if ci else None,
            "mcnemar_exact_two_sided_p": mcnemar_exact_two_sided(bo, ao) if n else None,
            "interpretation": "descriptive; with this few pairs the interval is wide and the exact test has almost no power"}


def analyze(rows: Iterable[Mapping[str, Any]], definition: Mapping[str, Any], *, tasks: Sequence[str] | None = None, repetition: int = 1) -> dict[str, Any]:
    """``tasks`` is the FINAL task list (selected tasks with any recorded replacements); default: the definition's. The first repetition feeds the paired tests."""
    rows = list(rows)
    seed = definition["preregistration"]["seed"]
    tasks = list(tasks or [t["instance_id"] for t in definition["preregistration"]["tasks"]])
    by_slot: dict[tuple[str, str], Mapping[str, Any]] = {}
    for r in rows:
        if r.get("repetition") == repetition:
            by_slot[(r["task_id"], r["arm"])] = r
    started, as_scheduled, valid_pairs, not_run = [], {}, {}, []
    for t in tasks:
        a, b = by_slot.get((t, "A")), by_slot.get((t, "B"))
        if a is None or b is None:
            not_run.append({"task_id": t, "has_A": a is not None, "has_B": b is not None})
            continue
        started.append(t)
        as_scheduled[t] = (a, b)
        if a.get("valid_measurement") and b.get("valid_measurement"):
            valid_pairs[t] = (a, b)
    def split(pairs: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]]) -> dict[str, list[Mapping[str, Any]]]:
        return {"A": [a for a, _ in pairs.values()], "B": [b for _, b in pairs.values()]}
    out: dict[str, Any] = {"label": LABEL, "experiment_id": definition["experiment_id"], "definition_sha256": definition["definition_sha256"], "tasks_scheduled": len(tasks),
                           "repetition_analysed": repetition, "rows": len(rows), "not_run_tasks": not_run}
    for name, pairs in (("as_scheduled", as_scheduled), ("valid_runs", valid_pairs)):
        sp = split(pairs)
        a_rows, b_rows = sp["A"], sp["B"]
        paired_cost = [_d(b["cost_usd_computed"]) - _d(a["cost_usd_computed"]) for a, b in pairs.values()]
        paired_unc = [_d(b["uncached_equivalent_usd"]) - _d(a["uncached_equivalent_usd"]) for a, b in pairs.values()]
        out[name] = {"outcomes": _pair_table(pairs, seed, name), "per_arm": {"A": _arm_stats(a_rows), "B": _arm_stats(b_rows)},
                     "paired_cost_difference_B_minus_A_usd": {"mean": format(sum(paired_cost) / len(paired_cost), "f") if paired_cost else None,
                                                              "uncached_equivalent_mean": format(sum(paired_unc) / len(paired_unc), "f") if paired_unc else None},
                     "cost_ratio_B_over_A": (format(sum(_d(r["cost_usd_computed"]) for r in b_rows) / sum(_d(r["cost_usd_computed"]) for r in a_rows), ".4f")
                                             if a_rows and sum(_d(r["cost_usd_computed"]) for r in a_rows) > 0 else None)}
    excluded = [r for r in by_slot.values() if not r.get("valid_measurement")]
    out["exclusions_from_valid_runs"] = {"slots": len(excluded), "by_handoff_status": _count(r.get("handoff_status") for r in excluded),
                                         "by_reason": _count((r.get("invalid_reason") or ("not graded" if not r.get("graded") else "handoff rejected")) for r in excluded),
                                         "stopped_by_budget": sum(1 for r in excluded if r.get("stop_reason") == "budget_exhausted"),
                                         "inexact_accounting_in_valid_runs": sum(1 for pr in valid_pairs.values() for r in pr if not r.get("cost_exact"))}
    out["task_table"] = [{"task_id": t, "repo": a.get("repo"), "A": _slot_outcome(a), "B": _slot_outcome(b), "valid_both": t in valid_pairs, "A_status": a.get("handoff_status"),
                          "B_status": b.get("handoff_status")} for t, (a, b) in sorted(as_scheduled.items())]
    return out


def _count(items: Iterable[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in items:
        out[str(i)] = out.get(str(i), 0) + 1
    return out


def render_markdown(report: Mapping[str, Any]) -> str:
    L = [f"# {report['experiment_id']} - paired analysis", "", f"**{report['label']}**", ""]
    for name in ("as_scheduled", "valid_runs"):
        s = report[name]
        o = s["outcomes"]
        L += [f"## {name}", f"pairs: {o['pairs']}  concordant: {o['concordant']}  discordant: {o['discordant']}",
              f"both resolved: {len(o['cells']['both_resolved'])}  neither: {len(o['cells']['neither_resolved'])}  A only: {len(o['cells']['A_only'])}  B only: {len(o['cells']['B_only'])}",
              f"resolution rate A: {o['resolution_rate']['A']}  B: {o['resolution_rate']['B']}  difference (B - A): {o['difference_B_minus_A']}",
              f"exact McNemar two-sided p: {o['mcnemar_exact_two_sided_p']}  bootstrap 95% interval: {o['bootstrap_95_interval']}  ({o['interpretation']})",
              f"cost total A: {s['per_arm']['A'].get('cost_usd_total')}  B: {s['per_arm']['B'].get('cost_usd_total')}  ratio B/A: {s['cost_ratio_B_over_A']}", ""]
    L += [f"not run: {report['not_run_tasks']}", f"exclusions from valid runs: {report['exclusions_from_valid_runs']}"]
    return "\n".join(L) + "\n"

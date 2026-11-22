"""The M3 results table: one JSON line per scheduled run, built from artifacts that already passed (or failed) the handoff, never from memory.

``build_row`` turns a run directory, its handoff report, its grading outcome (or ``None`` if it was not graded) and its transcript metrics into one flat,
analysis-ready row. ``ResultsWriter`` appends rows to a JSONL file: append-only, one row per ``slot_id`` (a slot is never overwritten; an infrastructure-invalid
run is repeated in a NEW repetition slot, not swapped in), fsynced, each row validated.

Money is a decimal STRING (never a float). Cost is the gateway-computed figure; Claude's own estimate is not stored as a cost. ``valid_measurement`` is the single
flag the valid-runs analysis uses: the handoff passed (ready or ready_flagged) AND a harness grading completed without errors.
"""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

SCHEMA = 1
INT_FIELDS = ("repetition", "order_index", "arm_position", "requests", "non_success_requests", "turns", "test_runs", "test_runs_failed", "test_fix_cycles", "edits_total",
              "tool_calls_total")
BOOL_FIELDS = ("valid", "graded", "valid_measurement", "cost_exact", "accounting_complete", "test_usage", "compliance", "cap_exceeded")
STR_FIELDS = ("experiment_id", "slot_id", "task_id", "repo", "arm", "pair_id", "run_id", "handoff_status", "cost_usd_computed", "uncached_equivalent_usd")
REQUIRED = ("schema", *STR_FIELDS, *INT_FIELDS, *BOOL_FIELDS, "resolved", "invalid_reason", "stop_reason", "handoff_rejections", "handoff_flags", "tokens", "request_categories")


_CALIBRATION_ID = re.compile(r"^m3-calibration-[1-9][0-9]*$")


def reject_calibration(run_or_row: Mapping[str, Any]) -> None:
    """A calibration run (or a row claiming a calibration experiment) is never a scored observation, whatever else it contains."""
    if run_or_row.get("calibration") is not None or (isinstance(run_or_row.get("experiment_id"), str) and _CALIBRATION_ID.fullmatch(run_or_row["experiment_id"])):
        raise ResultsError("a calibration run is never a scored observation: it cannot become a result row")


class ResultsError(ValueError):
    pass


def uncached_equivalent_usd(tokens: Mapping[str, int], pricing: Any) -> str:
    """Every input-side token (fresh, cache read, cache write) priced at the plain input rate, plus output at the output rate: a sensitivity figure that removes
    prompt-cache effects (a later run can be cheaper only because an earlier one warmed the cache)."""
    inp = sum(int(tokens.get(k, 0)) for k in ("input_tokens", "cache_read_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens", "cache_write_unclassified_tokens"))
    usd = (Decimal(inp) * pricing.input + Decimal(int(tokens.get("output_tokens", 0))) * pricing.output) / Decimal(1_000_000)
    return format(usd, "f")


def build_row(run_dir: Path, identity: Mapping[str, Any], handoff: Mapping[str, Any], grading: Mapping[str, Any] | None, transcript: Mapping[str, Any] | None,
              *, definition_sha256: str, pricing: Any, repo: str) -> dict[str, Any]:
    run = json.loads((Path(run_dir) / "run.json").read_text())
    reject_calibration(run)
    reject_calibration(identity)
    agent = ((run.get("lifecycle") or {}).get("agent")) or {}
    um, acc = run.get("usage_measured") or {}, run.get("accounting") or {}
    rs = (um.get("request_status") or {})
    tokens = dict(um.get("tokens") or {})
    patch = agent.get("patch") or {}
    handoff_ok = handoff.get("status") in ("ready", "ready_flagged")
    g = grading or {}
    graded = bool(grading) and g.get("valid") is True
    t = transcript or {}
    try:
        cost = format(Decimal(str(um.get("computed_usd_upper_bound_basis"))), "f")
    except Exception:
        cost = "0"
    row = {
        "schema": SCHEMA, "experiment_id": identity["experiment_id"], "definition_sha256": definition_sha256, "slot_id": identity["slot_id"], "task_id": identity["task_id"],
        "repo": repo, "arm": identity["arm"], "repetition": identity["repetition"], "pair_id": identity["pair_id"], "order_index": identity["order_index"],
        "arm_position": identity.get("arm_position", 0), "run_id": run.get("run_id"), "run_dir": str(run_dir), "model": run.get("model"),
        "prompt": run.get("prompt"), "prompt_set_file": run.get("prompt_set_file"), "claude_sha256": (run.get("claude_cli") or {}).get("sha256"),
        "image_id": run.get("image_id"), "seed_sha256": (run.get("seed") or {}).get("sha256"),
        "lifecycle_status": (run.get("lifecycle") or {}).get("status"), "valid": agent.get("valid") is True, "invalid_reason": agent.get("invalid_reason"),
        "stop_reason": agent.get("stop_reason") or (run.get("lifecycle") or {}).get("gateway_stop_reason"), "claude_exit_code": agent.get("claude_exit_code"),
        "wall_seconds": run.get("orchestrator_wall_s"), "claude_duration_ms": (t.get("result") or {}).get("duration_ms"),
        "handoff_status": handoff.get("status"), "handoff_rejections": list(handoff.get("rejections") or []), "handoff_flags": list(handoff.get("flags") or []),
        "handoff_report_sha256": handoff.get("report_sha256"),
        "graded": graded, "grading_run_id": g.get("run_id"), "resolved": (g.get("resolved") if graded else None), "grading_valid": g.get("valid") if grading else None,
        "f2p_passed": g.get("f2p_passed"), "f2p_total": g.get("f2p_total"), "p2p_maintained": g.get("p2p_maintained"), "p2p_total": g.get("p2p_total"),
        "patch_applied": g.get("patch_applied"), "harness_errors": g.get("errors"),
        "patch_sha256": patch.get("sha256"), "patch_size": patch.get("size"),
        "cost_usd_computed": cost, "cost_exact": bool(um.get("exact")) and acc.get("exact") is True, "accounting_complete": acc.get("complete") is True,
        "upper_bound_entries": um.get("entries_charged_at_max_tokens_upper_bound"), "tokens": tokens, "uncached_equivalent_usd": uncached_equivalent_usd(tokens, pricing),
        "requests": int(rs.get("finished_requests", um.get("requests") or 0)), "non_success_requests": int(rs.get("non_success") or 0),
        "request_categories": dict(rs.get("categories") or {}), "gateway_budget_usd": (run.get("limits") or {}).get("budget_usd"),
        "turns": t.get("turns_reported"), "tool_calls_total": int(t.get("tool_calls_total") or 0), "edits_total": int(t.get("edits_total") or 0),
        "test_runs": int(t.get("test_runs") or 0), "test_runs_failed": int(t.get("test_runs_failed") or 0), "test_fix_cycles": int(t.get("test_fix_cycles") or 0),
        "test_usage": bool(t.get("test_usage")), "compliance": bool(t.get("compliance")), "cap_exceeded": bool(t.get("cap_exceeded")),
        "transcript_complete": t.get("complete"), "valid_measurement": bool(handoff_ok and graded)}
    validate_row(row)
    return row


def validate_row(row: Mapping[str, Any]) -> None:
    missing = [k for k in REQUIRED if k not in row]
    if missing:
        raise ResultsError(f"row is missing {missing}")
    if row["schema"] != SCHEMA:
        raise ResultsError("unsupported row schema")
    for k in STR_FIELDS:
        if not isinstance(row[k], str) or not row[k]:
            raise ResultsError(f"{k} must be a non-empty string")
    for k in INT_FIELDS:
        if row[k] is not None and (type(row[k]) is not int):
            raise ResultsError(f"{k} must be an integer")
    for k in BOOL_FIELDS:
        if type(row[k]) is not bool:
            raise ResultsError(f"{k} must be a boolean")
    if row["resolved"] is not None and type(row["resolved"]) is not bool:
        raise ResultsError("resolved must be a boolean or null")
    if row["resolved"] is not None and not row["graded"]:
        raise ResultsError("a run that was not graded cannot have a resolved value")
    if row["valid_measurement"] and not (row["graded"] and row["handoff_status"] in ("ready", "ready_flagged")):
        raise ResultsError("valid_measurement requires a passed handoff and a completed grading")
    reject_calibration(row)
    if row["arm"] not in ("A", "B") or row["slot_id"] != f"{row['experiment_id']}/{row['task_id']}/{row['arm']}/r{row['repetition']}":
        raise ResultsError("slot_id does not match experiment, task, arm and repetition")
    for k in ("cost_usd_computed", "uncached_equivalent_usd"):
        Decimal(row[k])
    json.dumps(row)


class ResultsWriter:
    """Append-only JSONL. Refuses a second row for the same slot (also across process restarts: the file is re-read) and fsyncs every row."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for i, line in enumerate(self.path.read_text().splitlines(), 1):
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError as e:
                    raise ResultsError(f"{self.path.name} line {i} is not JSON") from e
        return out

    def append(self, row: Mapping[str, Any]) -> None:
        validate_row(row)
        if any(r.get("slot_id") == row["slot_id"] for r in self.rows()):
            raise ResultsError(f"slot {row['slot_id']} already has a row: results are append-only, one row per slot")
        with open(self.path, "a") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

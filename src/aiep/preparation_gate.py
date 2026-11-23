"""Which task-preparation failures may trigger the pre-registered replacement of a task.

Replacement is a research-integrity decision: it must depend only on an objectively recorded, DETERMINISTIC property of the task itself, never on A/B outcomes, patch quality, agent
behaviour, runtime or cost. So the set of qualifying failures is closed and explicit. A failure qualifies only if it is raised as a ``PreparationGateFailure`` carrying one of the codes
in ``PERMANENT_CODES``; the batch fails closed on everything else.

QUALIFY (permanent: the same image and the same limits fail the same way every time):
  seed_archive_over_limit   the /testbed archive exceeds the archive-wide limit (task_setup.MAX_ARCHIVE_BYTES) or the parser's raw-size bound
  seed_file_over_limit      one seed file exceeds the individual seed-file limit (seed.MAX_SEED_FILE_BYTES)
  seed_content_over_limit   total extracted content exceeds the content limit (seed.MAX_SEED_BYTES)
  seed_entries_over_limit   the archive has more entries than seed.MAX_ENTRIES
  seed_unsafe_content       an entry the seed format refuses (path traversal, escaping or malformed symlink, hard link, special file, missing parent directory, wrong root)
  seed_lineage_invalid      the seed has no resolvable HEAD, or its HEAD / parent is not the registered dataset base commit
  identity_mismatch         the account observed in the image (completed probe) is not the configured agent identity

DO NOT QUALIFY (halt for review, never replace): a Docker daemon failure, free disk below the gate, a network or registry error, an unavailable registry, a pull that was not approved, a
local image that differs from its pinned digest, an interrupted or timed-out probe, an unreadable or truncated archive, a lineage check that could not run, or any other error.
"""
from __future__ import annotations

from typing import Any

PERMANENT_CODES = frozenset({
    "seed_archive_over_limit", "seed_file_over_limit", "seed_content_over_limit", "seed_entries_over_limit", "seed_unsafe_content", "seed_lineage_invalid", "identity_mismatch",
})


class PreparationGateFailure(Exception):
    """Marker base for deterministic preparation-gate failures. Subclasses (and instances) carry ``code``; only codes in ``PERMANENT_CODES`` are replaceable."""

    code: str = "preparation_gate_failure"


def gate_failure_of(exc: BaseException) -> dict[str, Any] | None:
    """The structured record of ``exc`` if (and only if) it is a permanent preparation-gate failure; None for everything else."""
    if isinstance(exc, PreparationGateFailure) and getattr(exc, "code", None) in PERMANENT_CODES:
        return {"code": exc.code, "exception": type(exc).__name__, "detail": str(exc)[:500]}
    return None

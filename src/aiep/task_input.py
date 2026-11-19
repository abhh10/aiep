"""The only data an agent may receive about a task.

A SWE-bench record carries much more than an agent should see: the gold ``patch``,
``test_patch``, ``FAIL_TO_PASS`` / ``PASS_TO_PASS``, ``eval_script``, ``hints_text``,
``image`` and more. ``export_task_input`` copies exactly four fields into a frozen
schema and nothing else, so a new dataset column can never leak by default.
Everything else stays on the host.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Mapping

from .manifest import ManifestError, load_dataset_config, read_verified_split, verify_dataset_snapshot

MAX_PROBLEM_CHARS = 200_000
_INSTANCE_RE = re.compile(r"^[A-Za-z0-9_.\-]+__[A-Za-z0-9_.\-]+-\d+$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class TaskInputError(ValueError):
    pass


@dataclass(frozen=True)
class TaskInput:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


ALLOWED_FIELDS = tuple(f.name for f in fields(TaskInput))


def export_task_input(record: Mapping[str, Any]) -> TaskInput:
    """Build the agent-visible view of a dataset record (allowlist; validates each field)."""
    values = {}
    for name in ALLOWED_FIELDS:
        v = record.get(name)
        if not isinstance(v, str) or not v.strip():
            raise TaskInputError(f"record field {name!r} must be a non-empty string")
        values[name] = v
    if not _INSTANCE_RE.match(values["instance_id"]):
        raise TaskInputError("instance_id has an unexpected format")
    if not _REPO_RE.match(values["repo"]):
        raise TaskInputError("repo has an unexpected format")
    if not _SHA_RE.match(values["base_commit"]):
        raise TaskInputError("base_commit must be a 40-character lowercase hex commit")
    if len(values["problem_statement"]) > MAX_PROBLEM_CHARS:
        raise TaskInputError("problem_statement is unreasonably large")
    return TaskInput(**values)


def load_record(instance_id: str, hf_home: Path, repo_root: Path | None = None) -> dict:
    """Load one full record (host side only) from the pinned, verified local dataset snapshot.

    Strictly local. The configured revision must equal the cache's ``refs/main`` and its snapshot directory must
    exist (``verify_dataset_snapshot``); the split's parquet file must be the single file of its name and match its
    content-addressed blob name and stay inside this repository's cache (``read_verified_split``, which reads the bytes once
    and hashes those same bytes); the record is then parsed from those bytes with ``pyarrow``. ``datasets`` and ``huggingface_hub`` are neither imported nor called, so there is no hub resolution, no
    dataset-viewer request and no retry, whatever was imported earlier in the process, and no environment variable has to be
    set first. Any missing or invalid piece raises ``ManifestError``; there is no fallback.

    The returned dict contains hidden fields; pass it only to ``export_task_input`` or to evaluator-side code,
    never to an agent.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    cfg = load_dataset_config(repo_root) if repo_root else load_dataset_config()
    snapshot = verify_dataset_snapshot(hf_home, cfg)
    data = read_verified_split(snapshot, cfg["split"], cfg["revision"])  # read once; the parsed bytes are the hashed bytes
    try:
        # use_threads=False: no Arrow thread pool is created, so nothing can deadlock at interpreter exit (observed with
        # pyarrow 26 on macOS when a pool had been used); 300 rows do not need parallelism.
        table = pq.read_table(pa.BufferReader(data), use_threads=False)
    except Exception as e:  # corrupt parquet that still matched its (equally corrupt) name is not usable either
        raise ManifestError(f"cannot read the {cfg['split']!r} parquet of {snapshot.name}: {type(e).__name__}") from e
    if "instance_id" not in table.column_names:
        raise ManifestError(f"the {cfg['split']!r} parquet of {snapshot.name} has no instance_id column")
    ids = table.column("instance_id").to_pylist()
    rows = [i for i, v in enumerate(ids) if v == instance_id]
    if len(rows) != 1:
        raise ManifestError(f"expected exactly one record for {instance_id}, found {len(rows)}")
    return dict(table.slice(rows[0], 1).to_pylist()[0])

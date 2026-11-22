"""Run artifacts: one deterministic directory per run, created exclusively, hashed, and scrubbed of secrets.

``runs/m2/<run_id>/`` holds exactly the files in ``ARTIFACT_NAMES`` (nothing else can be written through this module):

    prompt.txt              the exact text delivered on the agent's stdin (the issue plus the workflow line)
    claude_stdout.jsonl     Claude Code's stream-json output (capped upstream at 32 MiB)
    claude_stderr.txt
    gateway_ledger.jsonl    the gateway's usage ledger: the authoritative usage and cost record (measured at the gateway)
    patch.diff              the extracted, validated patch (only when there is one)
    predictions.jsonl       the evaluator handoff (only when there is a patch)
    run.json                task identity, seed hash, model, configuration, prompt hashes, limits, statuses, redaction counts
    artifacts_manifest.json SHA-256 and size of every file above; written LAST, so its absence marks an incomplete run

Rules: the run directory is created with ``mkdir`` (an existing run is an error, never merged or overwritten), every file is
created with exclusive mode and flushed, and failed or invalid runs are kept like any other (CLAUDE.md). Secrets never reach
disk: the real API key, the run token and anything shaped like an Anthropic key are replaced by ``[REDACTED]`` in every file
before it is written, and the number of replacements per file is recorded in ``run.json`` (the hash in the manifest is of the
bytes actually stored).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = 1
ARTIFACT_NAMES = ("prompt.txt", "claude_stdout.jsonl", "claude_stderr.txt", "gateway_ledger.jsonl", "patch.diff",
                  "predictions.jsonl", "run.json")
MANIFEST_NAME = "artifacts_manifest.json"
REDACTED = b"[REDACTED]"
MIN_SECRET_LEN = 8  # a shorter "secret" would redact ordinary text; callers pass real keys and tokens
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
_KEY_SHAPED = re.compile(rb"sk-ant-[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9]{20,}")
_INSTANCE = re.compile(r"^[A-Za-z0-9_.\-]+__[A-Za-z0-9_.\-]+-\d+$")


class ArtifactError(RuntimeError):
    """The artifact directory or a file could not be written safely."""


def make_run_id(instance_id: str, config_label: str, stamp: str) -> str:
    """``m2-<instance>-<A|B>-<UTC stamp>``: deterministic from its inputs, and a valid container-name component."""
    if not _INSTANCE.match(instance_id) or config_label not in ("A", "B") or not re.fullmatch(r"\d{8}T\d{6}Z", stamp):
        raise ArtifactError("run id inputs are malformed")
    return f"m2-{instance_id}-{config_label}-{stamp}"


def redact(data: bytes, secrets: Sequence[str]) -> tuple[bytes, int]:
    """Replace every occurrence of each secret (and any key-shaped string) with ``[REDACTED]``; return the count too."""
    total = 0
    for secret in sorted({s for s in secrets if isinstance(s, str) and len(s) >= MIN_SECRET_LEN}, key=len, reverse=True):
        needle = secret.encode("utf-8")
        total += data.count(needle)
        data = data.replace(needle, REDACTED)
    data, n = _KEY_SHAPED.subn(REDACTED, data)
    return data, total + n


def _write_new(path: Path, data: bytes) -> None:
    with open(path, "xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(path, 0o444)


@dataclass(frozen=True)
class RunArtifacts:
    directory: Path
    files: dict[str, dict[str, Any]]  # name -> {"sha256", "size"}
    redactions: dict[str, int]


def write_run_artifacts(base: Path, run_id: str, files: Mapping[str, bytes | str], meta: Mapping[str, Any],
                        secrets: Sequence[str]) -> RunArtifacts:
    """Create ``base/run_id`` and write ``files`` (by name from ``ARTIFACT_NAMES``), ``run.json`` (from ``meta``) and the manifest.

    ``files`` must not contain ``run.json``; ``meta`` must be JSON-serialisable. Raises ``ArtifactError`` if the run directory
    exists or a name is not allowed; nothing is overwritten or merged.
    """
    if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
        raise ArtifactError("run_id must be a plain identifier")
    names = set(files)
    if not names <= set(ARTIFACT_NAMES) - {"run.json"}:
        raise ArtifactError(f"file name(s) not allowed: {sorted(names - set(ARTIFACT_NAMES))}")
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    directory = base / run_id
    try:
        directory.mkdir()  # exclusive: FileExistsError if a run with this ID already exists
    except FileExistsError as e:
        raise ArtifactError(f"{directory} already exists; a run directory is never reused") from e

    stored: dict[str, dict[str, Any]] = {}
    redactions: dict[str, int] = {}

    def put(name: str, data: bytes) -> None:
        clean, n = redact(data, secrets)
        redactions[name] = n
        _write_new(directory / name, clean)
        stored[name] = {"sha256": hashlib.sha256(clean).hexdigest(), "size": len(clean)}

    for name in ARTIFACT_NAMES:
        if name in files:
            data = files[name]
            put(name, data.encode("utf-8") if isinstance(data, str) else bytes(data))
    run = {"schema": SCHEMA, "run_id": run_id, **meta, "redactions": dict(redactions), "files": sorted([*stored, "run.json"])}
    try:
        run_bytes = (json.dumps(run, indent=2, sort_keys=True, default=_no_unserialisable) + "\n").encode("utf-8")
    except (TypeError, ValueError) as e:
        raise ArtifactError(f"run metadata is not serialisable: {e}") from e
    put("run.json", run_bytes)
    manifest = {"schema": SCHEMA, "run_id": run_id, "files": dict(sorted(stored.items()))}
    _write_new(directory / MANIFEST_NAME, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return RunArtifacts(directory, stored, redactions)


def _no_unserialisable(obj: Any) -> Any:
    raise TypeError(f"{type(obj).__name__} is not JSON serialisable")


def verify_run_artifacts(directory: Path) -> list[str]:
    """Recompute every hash against the manifest. Returns problems (empty means intact and complete)."""
    directory = Path(directory)
    try:
        manifest = json.loads((directory / MANIFEST_NAME).read_text())
    except (OSError, ValueError) as e:
        return [f"manifest unreadable (an incomplete run has none): {type(e).__name__}"]
    problems: list[str] = []
    listed = manifest.get("files", {})
    for name, rec in listed.items():
        path = directory / name
        try:
            data = path.read_bytes()
        except OSError:
            problems.append(f"missing: {name}")
            continue
        if hashlib.sha256(data).hexdigest() != rec.get("sha256") or len(data) != rec.get("size"):
            problems.append(f"content differs: {name}")
    extra = {p.name for p in directory.iterdir()} - set(listed) - {MANIFEST_NAME}
    problems += [f"unexpected file: {n}" for n in sorted(extra)]
    return problems

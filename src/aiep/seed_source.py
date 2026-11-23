"""Obtain the repository seed from a pristine, never-started container of the pinned instance image.

This is the only place AIEP creates a seed-source container, and it is deliberately narrow: it is not a
general Docker creation path and it is separate from ``agent_runner``. The flow (see ``seed.py`` for the rest):

1. validate every input (image ID, run ID, destination) BEFORE any Docker call;
2. check the exact image ID is already present locally (a missing image is an error, never a pull);
3. create one container from that ID with a fixed, allowlisted configuration, and verify it with
   ``docker inspect``. It is never started;
4. ``get_archive("/testbed")`` and read the stream with a hard byte cap (no truncation, no unbounded memory);
5. ``parse_seed_archive`` (all hard-link, symlink, path and special-file refusals live there, unchanged);
6. ``save_seed`` (never overwrites), reload with ``load_seed(expected_sha256=...)`` and compare;
7. remove exactly the container this call created, in a ``finally``.

The seed must be taken before the agent container exists: an agent's ``/testbed`` tmpfs hides the image's own.

Known limits (documented, not worked around):

* ``containers.create`` in docker-py is ``create_container`` followed by a ``get`` of the new ID. If that second call
  fails after the daemon has created the container, the caller never receives an ID and cannot remove it. This module
  does not guess one (no removal by name or label): that would widen what it may delete. The container is named
  ``aiep.seedsrc.<run_id>`` and labelled ``aiep.role=seed_source`` so an operator can find and remove it by exact ID.
* Closing the chunk generator returned by ``get_archive`` does NOT by itself close the HTTP response: docker-py's
  ``_stream_raw_result`` is a bare ``yield from response.iter_content(...)`` with no ``with``/``finally``. The reliable
  release of that connection is closing the Docker client, which the caller (``scripts/extract_pytest5227_seed.py``)
  does in a ``finally``. ``read_bounded`` still closes the generator, but only as hygiene.
* ``get_archive`` returns ``(stream, stat)`` where ``stat`` is ``None`` if the daemon omitted its stat header (docker-py
  7.2 contract). ``stat`` describes ``/testbed`` itself (its size is the directory's, not the tree's), so nothing here
  trusts or needs it: ``None`` and any dict are accepted, and only the tar is ever used.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .container_policy import PolicyViolation
from .preparation_gate import PreparationGateFailure
from .seed import (MAX_ENTRIES, MAX_SEED_BYTES, Seed, SeedError, load_seed, parse_seed_archive, save_seed)

SEED_ROOT = "/testbed"
NAME_PREFIX = "aiep.seedsrc."
SEED_SOURCE_ROLE = "seed_source"
SEED_SOURCE_COMMAND = ["/bin/true"]  # the container is never started; this only has to be a fixed value
SEED_SOURCE_MEM = "256m"
SEED_SOURCE_PIDS = 64
_TAR_OVERHEAD_PER_ENTRY = 1536  # same header/padding allowance parse_seed_archive uses for its own raw-size check
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
_STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")

# The complete set of create arguments this module will ever pass, and nothing else.
_ALLOWED_CREATE_KEYS = frozenset({"image", "command", "name", "labels", "network_mode", "read_only", "cap_drop",
                                  "security_opt", "mem_limit", "pids_limit"})


class SeedSourceError(RuntimeError):
    """Seed extraction failed (Docker, archive reading, saving or cleanup)."""


class SeedArchiveTooLarge(SeedSourceError, PreparationGateFailure):
    """The /testbed archive is larger than the byte limit the caller set (``task_setup.MAX_ARCHIVE_BYTES`` for task preparation). A deterministic property of the task's own image, so a
    preparation-gate failure that may trigger the pre-registered replacement; every other SeedSourceError (Docker, streaming, saving, cleanup) never does."""

    code = "seed_archive_over_limit"


def max_archive_bytes() -> int:
    """Raw tar limit: the content limit plus tar header/padding overhead, exactly as ``parse_seed_archive`` computes it.

    Computed from the seed module's constants at call time so the two limits cannot drift apart. It bounds the *tar*;
    the content limits (per file, total, entry count) are still enforced by the parser.
    """
    return MAX_SEED_BYTES + MAX_ENTRIES * _TAR_OVERHEAD_PER_ENTRY


# ------------------------------------------------------------------ policy
def seed_source_kwargs(image_id: str, run_id: str) -> dict[str, Any]:
    """The only create arguments a seed-source container may have. No env, user, volumes, ports, devices or network."""
    kwargs = dict(
        image=image_id,
        command=list(SEED_SOURCE_COMMAND),
        name=f"{NAME_PREFIX}{run_id}",
        labels={"aiep.run_id": run_id, "aiep.role": SEED_SOURCE_ROLE},
        network_mode="none",
        read_only=True,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        mem_limit=SEED_SOURCE_MEM,
        pids_limit=SEED_SOURCE_PIDS,
    )
    validate_seed_source_kwargs(kwargs, image_id, run_id)
    return kwargs


def validate_seed_source_kwargs(kwargs: Mapping[str, Any], image_id: str, run_id: str) -> None:
    """Allowlist check of the create arguments (fails closed on any unknown key or different value)."""
    _require_valid_ids(image_id, run_id)
    extra = set(kwargs) - _ALLOWED_CREATE_KEYS
    if extra:
        raise PolicyViolation(f"seed_source: create argument(s) not allowed: {sorted(extra)}")
    want = {
        "image": image_id, "command": SEED_SOURCE_COMMAND, "name": f"{NAME_PREFIX}{run_id}",
        "labels": {"aiep.run_id": run_id, "aiep.role": SEED_SOURCE_ROLE}, "network_mode": "none", "read_only": True,
        "cap_drop": ["ALL"], "security_opt": ["no-new-privileges"], "mem_limit": SEED_SOURCE_MEM,
        "pids_limit": SEED_SOURCE_PIDS,
    }
    for key, value in want.items():
        if key not in kwargs or kwargs[key] != value or type(kwargs[key]) is not type(value):
            raise PolicyViolation(f"seed_source: {key} must be exactly {value!r}")


def verify_seed_source_inspect(inspect: Mapping[str, Any], image_id: str, run_id: str) -> None:
    """Check the CREATED container (``docker inspect``) against the same policy, and that it never started."""
    host = inspect.get("HostConfig") or {}
    state = inspect.get("State") or {}

    def need(cond: bool, msg: str) -> None:
        if not cond:
            raise PolicyViolation(f"seed_source: {msg}")

    need(inspect.get("Image") == image_id, "container is not from the pinned image ID")
    need(host.get("NetworkMode") == "none", "network must be 'none'")
    need(host.get("Privileged") in (False, None), "container is privileged")
    need("ALL" in (host.get("CapDrop") or []) and not host.get("CapAdd"), "capabilities are not dropped")
    need(not host.get("Binds"), "container has bind mounts")
    need(not host.get("Devices"), "container has devices")
    need(not host.get("PortBindings"), "container publishes ports")
    need(not host.get("PidMode"), "container shares a PID namespace")
    need(host.get("IpcMode") != "host", "container shares the host IPC namespace")
    need(not (inspect.get("Mounts") or []), "container has mounts or volumes")
    labels = (inspect.get("Config") or {}).get("Labels") or {}
    need(labels.get("aiep.run_id") == run_id and labels.get("aiep.role") == SEED_SOURCE_ROLE, "AIEP labels are missing")
    started = state.get("StartedAt") or "0001-01-01T00:00:00Z"
    need(state.get("Status") == "created" and state.get("Running") in (False, None) and started.startswith("0001"),
         "container is not in the never-started 'created' state")


def _require_valid_ids(image_id: Any, run_id: Any) -> None:
    if not isinstance(image_id, str) or not _IMAGE_ID_RE.match(image_id):
        raise PolicyViolation("seed_source: image must be an exact immutable image ID (sha256:<64 hex>), never a tag")
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise PolicyViolation("seed_source: run_id must be 1-64 characters of letters, digits, '.', '_' or '-'")


# ------------------------------------------------------------------ bounded reading
def read_bounded(stream: Iterable[Any], max_bytes: int) -> bytes:
    """Consume a ``get_archive`` chunk stream into bytes, refusing (never truncating) anything over ``max_bytes``.

    Only ``bytes``/``bytearray`` chunks are valid; anything else (text, None, integers, a bare bytes object being
    iterated as ints) is refused. The generator is closed afterwards as hygiene; that alone does not guarantee the
    HTTP response is closed (see the module docstring), the caller's client closure does.

    Returns ``bytes`` on purpose. Measured (tracemalloc, a 40 MiB archive): ``parse_seed_archive`` peaks 82 MiB above
    its input for ``bytes`` but 122 MiB for ``bytearray``, because ``io.BytesIO(bytes)`` shares the buffer while
    ``io.BytesIO(bytearray)`` copies it. The one transient extra copy made here is freed before parsing starts.
    """
    if isinstance(stream, (bytes, bytearray, str)) or not hasattr(stream, "__iter__"):
        raise SeedSourceError("the archive stream is not an iterable of chunks")
    buf = bytearray()
    try:
        for chunk in stream:
            if not isinstance(chunk, (bytes, bytearray)):
                raise SeedSourceError(f"invalid archive chunk of type {type(chunk).__name__}")
            if len(buf) + len(chunk) > max_bytes:
                raise SeedArchiveTooLarge(f"archive exceeds the {max_bytes}-byte limit; refused (not truncated)")
            buf += chunk
    except SeedSourceError:
        raise
    except Exception as e:  # Docker/HTTP errors while streaming
        raise SeedSourceError(f"reading the archive stream failed: {type(e).__name__}: {e}") from e
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
    if not buf:
        raise SeedSourceError("the archive stream was empty")
    return bytes(buf)


# ------------------------------------------------------------------ operation
@dataclass(frozen=True)
class SeedExtraction:
    seed: Seed  # re-loaded from disk and verified
    tar_path: Path
    manifest_path: Path
    container_id: str
    removed: bool  # always True when returned: a failed removal raises instead


def extract_seed(client: Any, *, image_id: str, run_id: str, directory: Path, stem: str = "seed",
                 max_bytes: int | None = None) -> SeedExtraction:
    """Take the real ``/testbed`` seed from a pristine container of ``image_id`` and store it. See the module docstring."""
    # 1. everything that can be checked without Docker, before Docker is touched
    _require_valid_ids(image_id, run_id)
    if not isinstance(stem, str) or not _STEM_RE.match(stem):
        raise SeedSourceError("the seed stem must be 1-64 characters of letters, digits, '.', '_' or '-'")
    directory = Path(directory)
    tar_path, manifest_path = directory / f"{stem}.tar", directory / f"{stem}_manifest.json"
    for p in (tar_path, manifest_path):
        if p.exists() or p.is_symlink():
            raise SeedSourceError(f"{p} already exists; a stored seed is never overwritten")
    limit = max_archive_bytes() if max_bytes is None else max_bytes
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise SeedSourceError("the archive byte limit must be a positive integer")
    kwargs = seed_source_kwargs(image_id, run_id)  # also validates

    # 2. the exact image must already be here: creating from a missing reference could make docker-py pull
    try:
        present = client.images.get(image_id)
    except Exception as e:
        raise SeedSourceError(f"image {image_id} is not available locally (nothing is pulled): {type(e).__name__}") from e
    if getattr(present, "id", None) != image_id:
        raise SeedSourceError("the local image does not have the requested ID")

    # 3. create (never start). If create itself raises there is no container ID to clean up (see module docstring).
    image, command = kwargs.pop("image"), kwargs.pop("command")
    try:
        container = client.containers.create(image, command, **kwargs)
    except Exception as e:
        raise SeedSourceError(f"creating the seed-source container failed: {type(e).__name__}: {e}") from e
    failed = True
    written: list[Path] = []
    try:  # begins immediately: from here the exact container we were handed is always removed
        container_id = container.id
        verify_seed_source_inspect(client.api.inspect_container(container_id), image_id, run_id)

        # 4. bounded read
        try:
            got = container.get_archive(SEED_ROOT)
            if not isinstance(got, tuple) or len(got) != 2:
                raise SeedSourceError("get_archive did not return a (stream, stat) pair")
            stream, _stat = got  # _stat may be None (SDK contract); it is never used
        except Exception as e:
            raise SeedSourceError(f"get_archive({SEED_ROOT}) failed: {type(e).__name__}: {e}") from e
        raw = read_bounded(stream, limit)

        # 5-6. parse with the existing parser, store (atomic; cleans up after itself), reload and compare
        seed = parse_seed_archive(raw)
        del raw
        save_seed(directory, seed, stem=stem)
        written = [tar_path, manifest_path]  # save_seed refuses to overwrite, so both are ours from here on
        loaded = load_seed(directory, stem=stem, expected_sha256=seed.manifest.archive_sha256)
        if loaded.manifest.head != seed.manifest.head or loaded.manifest.entries != seed.manifest.entries:
            raise SeedSourceError("the stored seed does not match the extracted seed")
        failed = False
    finally:
        # a seed that was saved but did not verify must not stay behind to block (or poison) a retry
        if failed and written:
            for p in written:
                try:
                    p.unlink(missing_ok=True)
                except OSError as e:
                    sys.stderr.write(f"WARNING: could not remove {p}: {type(e).__name__}: {e}\n")
        # 7. exactly the container handed to us by create, whatever happened
        cid = getattr(container, "id", None)
        cleanup_error: Exception | None = None
        try:
            if not cid:
                raise SeedSourceError("the created container has no ID")
            client.api.remove_container(cid, v=True, force=False)
        except Exception as e:
            cleanup_error = e
        if cleanup_error is not None:
            msg = (f"seed-source container {cid} could not be removed: "
                   f"{type(cleanup_error).__name__}: {cleanup_error}")
            if failed:  # do not mask the original error
                sys.stderr.write(f"WARNING: {msg}\n")
            else:
                raise SeedSourceError(f"seed saved, but {msg}") from cleanup_error
    return SeedExtraction(loaded, tar_path, manifest_path, cid, True)

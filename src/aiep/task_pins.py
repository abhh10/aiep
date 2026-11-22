"""Per-task pins: which image, seed and agent identity a task runs with, and the (never overwriting) writers that record them.

A task is runnable only when all three exist:

* the EVALUATOR IMAGE pin: ``config/evaluator_images.json`` (image reference and the immutable image ID the daemon reports);
* the SEED pin: ``config/seeds.json`` (the pristine /testbed extracted from that exact image: archive hash, HEAD, entry count);
* the AGENT IDENTITY: ``config/task_identities/<instance>.json`` (the account the image really has, observed in that image), or, for the original
  task only, the legacy ``config/agent_identity.json`` whose ``verified_image_id`` equals the task's image.

The writers refuse to replace an existing pin with a different value. Nothing here touches Docker; the scripts that do (``scripts/pin_task_image.py``,
``extract_task_seed.py``, ``verify_task_identity.py``) are approval-gated and call these.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from .agent_env import AgentConfig, IdentityUnverified, load_agent_config
from .task_screening import image_name

_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class PinError(RuntimeError):
    """A pin is missing, malformed, or would overwrite a different existing pin."""


def _read(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise PinError(f"cannot read {path.name}: {type(e).__name__}") from e
    if not isinstance(doc, dict):
        raise PinError(f"{path.name} is not a JSON object")
    return doc


def _write_atomic(path: Path, doc: Mapping[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    os.replace(tmp, path)


def identity_path(root: Path, instance_id: str) -> Path:
    return Path(root) / "config" / "task_identities" / f"{instance_id}.json"


def evaluator_image_pin(root: Path, instance_id: str) -> dict[str, Any] | None:
    return (_read(Path(root) / "config" / "evaluator_images.json").get("images") or {}).get(instance_id)


def seed_pin(root: Path, instance_id: str) -> dict[str, Any] | None:
    return (_read(Path(root) / "config" / "seeds.json").get("seeds") or {}).get(instance_id)


def load_identity(root: Path, instance_id: str, image_id: str) -> AgentConfig:
    """The verified agent identity for this task's image. Raises ``IdentityUnverified`` when there is none (never a default)."""
    p = identity_path(root, instance_id)
    if p.exists():
        return load_agent_config(p, image_id=image_id)
    return load_agent_config(Path(root) / "config" / "agent_identity.json", image_id=image_id)  # the original task's identity; refuses any other image


def pin_status(root: Path, instance_id: str) -> dict[str, Any]:
    """What is pinned for a task and what is missing (read-only)."""
    img, seed = evaluator_image_pin(root, instance_id), seed_pin(root, instance_id)
    missing = []
    if not (img and _IMAGE_ID.fullmatch(str(img.get("image_id", "")))):
        missing.append("evaluator_image")
    if not seed:
        missing.append("seed")
    ident = False
    if img and not missing[:1]:
        try:
            load_identity(root, instance_id, img["image_id"])
            ident = True
        except (IdentityUnverified, OSError, PinError):
            missing.append("agent_identity")
    elif img is None:
        missing.append("agent_identity")
    return {"instance_id": instance_id, "image_pinned": "evaluator_image" not in missing, "seed_pinned": seed is not None, "identity_verified": ident,
            "runnable": not missing, "missing": missing}


def add_evaluator_image(root: Path, instance_id: str, image_id: str, *, source: str) -> None:
    if not _IMAGE_ID.fullmatch(str(image_id)):
        raise PinError("image_id must be sha256:<64 hex>")
    path = Path(root) / "config" / "evaluator_images.json"
    doc = _read(path)
    entry = {"image": image_name(instance_id), "image_id": image_id, "source": source}
    old = (doc.setdefault("images", {})).get(instance_id)
    if old is not None and old != entry:
        raise PinError(f"{instance_id} already has a different evaluator image pin; a pin is never overwritten")
    doc["images"][instance_id] = entry
    _write_atomic(path, doc)


def add_seed_pin(root: Path, instance_id: str, *, directory: str, sha256: str, head: str, entries: int, image_id: str) -> None:
    if not (_SHA256.fullmatch(sha256) and _COMMIT.fullmatch(head) and type(entries) is int and entries > 0 and _IMAGE_ID.fullmatch(image_id)):
        raise PinError("malformed seed pin")
    img = evaluator_image_pin(root, instance_id)
    if not img or img.get("image_id") != image_id:
        raise PinError("the seed must be taken from the pinned evaluator image of this task")
    path = Path(root) / "config" / "seeds.json"
    doc = _read(path)
    entry = {"directory": directory, "sha256": sha256, "head": head, "entries": entries, "image_id": image_id}
    old = (doc.setdefault("seeds", {})).get(instance_id)
    if old is not None and old != entry:
        raise PinError(f"{instance_id} already has a different seed pin; a pin is never overwritten")
    doc["seeds"][instance_id] = entry
    _write_atomic(path, doc)


def write_identity(root: Path, instance_id: str, candidate: AgentConfig, *, image_id: str, evidence: list[str], verified_on: str) -> Path:
    """Record an identity OBSERVED in this image (``candidate`` supplies the account to compare; the evidence list says how it was observed)."""
    if not _IMAGE_ID.fullmatch(image_id) or not evidence:
        raise PinError("an identity pin needs the image ID and evidence of the observation")
    path = identity_path(root, instance_id)
    doc = {"schema": 1, "status": "verified", "name": candidate.identity.name, "uid": candidate.identity.uid, "gid": candidate.identity.gid,
           "home": candidate.identity.home, "shell": candidate.shell, "supplementary_groups": [], "verification_python": candidate.verification_python,
           "testbed_python": candidate.testbed_python, "verified_image_id": image_id, "verified_on": verified_on, "evidence": evidence,
           "still_unverified": ["the per-run identity probe (IDENTITY_SCRIPT) is the run-time re-check of these values and remains mandatory"]}
    if path.exists():
        if _read(path) != doc:
            raise PinError(f"{instance_id} already has a different identity pin; a pin is never overwritten")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, doc)
    load_agent_config(path, image_id=image_id)  # the written file must load under the strict loader
    return path

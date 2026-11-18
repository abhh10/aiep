"""Evaluator gate: no grading without an attestation tied to the exact configuration.

An *attestation* is a JSON document produced by the egress probe (Phase 3) that
binds, by SHA-256, the evaluator wrapper, the container policy, this gate module,
the wheelhouse manifest, the evaluator-image manifest, the expected image IDs and
the harness version, plus the probe result, under an HMAC-SHA256 with a host-held
key. ``verify_attestation`` recomputes every component from the live files and
refuses on any difference, so editing the wrapper, the policy, the wheelhouse or
the image list invalidates it.

There is deliberately no bypass: not for gold controls, not for tests, not by
environment variable. Gold controls are graded in the same attested environment.

Limit: the HMAC key lives on the same host as the code. This stops stale,
accidental or tampered artifacts and anything that cannot read the key (the agent
and the evaluated code never can). It does not defend against a compromised host.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from importlib import metadata
from pathlib import Path
from typing import Any, Mapping

SCHEMA = 1
REPO_ROOT = Path(__file__).resolve().parents[2]
_PROBE_REQUIRED_TRUE = ("network_blocked", "wheelhouse_install_ok")


class GateError(RuntimeError):
    """The evaluator gate refuses to open."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256_canonical_json(path: Path) -> str:
    """Hash a JSON file by content (key order and whitespace do not matter)."""
    return hashlib.sha256(canonical(json.loads(Path(path).read_text()))).hexdigest()


def current_components(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """Recompute, from live files, everything an attestation must bind."""
    root = Path(repo_root)
    wheelhouse = json.loads((root / "config" / "wheelhouse.json").read_text())
    if wheelhouse.get("status") != "populated" or not wheelhouse.get("wheels"):
        raise GateError("wheelhouse manifest is not populated; the evaluator cannot be attested")
    images = json.loads((root / "config" / "evaluator_images.json").read_text())["images"]
    return {
        "schema": SCHEMA,
        "evaluator_sha256": sha256_file(root / "src" / "aiep" / "evaluator.py"),
        "policy_sha256": sha256_file(root / "src" / "aiep" / "container_policy.py"),
        "gate_sha256": sha256_file(root / "src" / "aiep" / "gate.py"),
        "wheelhouse_sha256": sha256_canonical_json(root / "config" / "wheelhouse.json"),
        "image_manifest_sha256": sha256_canonical_json(root / "config" / "evaluator_images.json"),
        "image_ids": {k: v["image_id"] for k, v in sorted(images.items())},
        "network_mode": "none",
        "swebench": metadata.version("swebench"),
        "docker_sdk": metadata.version("docker"),
    }


def load_key(path: Path) -> bytes:
    """Read the HMAC key; it must be a private file of at least 32 bytes."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError as e:
        raise GateError(f"attestation key not readable: {e}") from e
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise GateError("attestation key file must not be accessible by group or others (chmod 600)")
    key = p.read_bytes()
    if len(key) < 32:
        raise GateError("attestation key must be at least 32 bytes")
    return key


def _mac(key: bytes, components: Mapping[str, Any], probe: Mapping[str, Any]) -> str:
    return hmac.new(key, canonical({"components": components, "probe": probe}), hashlib.sha256).hexdigest()


def build_attestation(components: Mapping[str, Any], probe: Mapping[str, Any], key: bytes) -> dict:
    """Create an attestation. ``probe`` must show the network blocked and the wheelhouse install working."""
    for field in _PROBE_REQUIRED_TRUE:
        if probe.get(field) is not True:
            raise GateError(f"probe did not pass: {field} is not true")
    return {
        "schema": SCHEMA,
        "components": dict(components),
        "probe": dict(probe),
        "mac": _mac(key, components, probe),
    }


def verify_attestation(att: Mapping[str, Any], components: Mapping[str, Any], key: bytes) -> None:
    """Raise GateError unless ``att`` is authentic and matches ``components`` exactly."""
    if att.get("schema") != SCHEMA:
        raise GateError("attestation schema mismatch")
    comps, probe, mac = att.get("components"), att.get("probe"), att.get("mac")
    if not isinstance(comps, dict) or not isinstance(probe, dict) or not isinstance(mac, str):
        raise GateError("attestation is malformed")
    if not hmac.compare_digest(mac, _mac(key, comps, probe)):
        raise GateError("attestation MAC is invalid (tampered, or signed with a different key)")
    for field in _PROBE_REQUIRED_TRUE:
        if probe.get(field) is not True:
            raise GateError(f"attestation probe did not pass: {field}")
    if comps != dict(components):
        changed = sorted(k for k in set(comps) | set(components) if comps.get(k) != components.get(k))
        raise GateError(f"attestation does not match the current configuration; changed: {changed}")


def require_gate(attestation_path: Path, key_path: Path, repo_root: Path = REPO_ROOT) -> dict:
    """Open the gate or raise. Returns the verified attestation."""
    key = load_key(key_path)
    try:
        att = json.loads(Path(attestation_path).read_text())
    except (OSError, ValueError) as e:
        raise GateError(f"attestation not readable: {e}") from e
    verify_attestation(att, current_components(repo_root), key)
    return att


def key_path_from_env() -> Path:
    value = os.environ.get("AIEP_ATTEST_KEY_FILE")
    if not value:
        raise GateError("AIEP_ATTEST_KEY_FILE is not set")
    return Path(value)

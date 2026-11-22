"""Immutable image identity for SWE-bench task images: the digest lock, its verification against the registry, and pull / lookup by digest.

The published task images are tagged ``:latest``, a mutable tag. ``config/image_digests.json`` (separate from the hashed experiment definition) records, per task, the OCI
index digest and the linux/amd64 platform manifest digest that were verified against the registry, with their layers and provenance. The preparation path then:

1. loads the record and refuses a missing, malformed or inconsistent one (``load_digest_record``; there is no default and never a fallback to a tag);
2. re-verifies the record against the registry BEFORE a pull (``verify_record_against_registry``): the index body must hash to the recorded index digest and agree with the
   registry's digest header, exactly one linux/amd64 entry must exist and be the recorded platform manifest, that manifest's body must hash to its digest, and its config and
   layers must equal the record;
3. pulls ``<repository>@<index digest>`` for linux/amd64 (``pull_by_digest``), never a tag, and requires the resulting local image ID to equal the index digest (the containerd
   image store reports the index digest as the image ID);
4. finds and pins the local image by the same digest reference (``local_image`` / ``image_present``).

Registry access is injected (``fetch``), so every check is tested with mocks. Only ``docker_hub_fetch`` touches the network, anonymously, for manifests only (never a blob).
The stock evaluator's later need for a ``<repository>:latest`` NAME is a separate matter: nothing here creates a tag.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from .task_screening import image_name

SCHEMA = 1
PLATFORM = "linux/amd64"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
LOCK_FILE = Path("config") / "image_digests.json"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KEYS = frozenset({"repository", "index_digest", "index_media_type", "platform", "platform_manifest_digest", "platform_manifest_media_type", "config_digest", "config_size",
                   "layers", "compressed_layer_bytes", "provenance"})
Fetch = Callable[[str, str, str], "tuple[Mapping[str, str], bytes]"]  # (repository, reference, accept) -> (lower-cased response headers, body)


class DigestError(RuntimeError):
    """The digest record is missing, malformed, stale or does not match the registry or the local image. Never recoverable by guessing."""


def _sha256(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def reference(record: Mapping[str, Any]) -> str:
    """``<repository>@<index digest>``: the one name the preparation uses for this image."""
    return f"{record['repository']}@{record['index_digest']}"


# ------------------------------------------------------------------ the lock file
def _validate(record: Any, instance_id: str) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise DigestError(f"{instance_id}: the digest record is not an object")
    extra, missing = sorted(set(record) - _KEYS), sorted(_KEYS - set(record))
    if extra:
        raise DigestError(f"{instance_id}: unexpected fields in the digest record: {extra}")
    if missing:
        raise DigestError(f"{instance_id}: the digest record lacks {missing[0]}" + (f" (and {len(missing) - 1} more)" if len(missing) > 1 else ""))
    want_repo = image_name(instance_id).partition(":")[0]  # the published repository for this instance (the tag part is discarded)
    if record["repository"] != want_repo:
        raise DigestError(f"{instance_id}: repository {record['repository']!r} is not the task's published repository {want_repo!r}")
    for k in ("index_digest", "platform_manifest_digest", "config_digest"):
        if not (isinstance(record[k], str) and _DIGEST.fullmatch(record[k])):
            raise DigestError(f"{instance_id}: {k} must be sha256:<64 hex>")
    if record["platform"] != PLATFORM:
        raise DigestError(f"{instance_id}: platform must be {PLATFORM}")
    if record["index_media_type"] != OCI_INDEX or record["platform_manifest_media_type"] != OCI_MANIFEST:
        raise DigestError(f"{instance_id}: unsupported manifest media types")
    layers = record["layers"]
    ok = (isinstance(layers, list) and layers and all(isinstance(l, dict) and set(l) == {"digest", "size"} and isinstance(l["digest"], str) and _DIGEST.fullmatch(l["digest"])
                                                      and type(l["size"]) is int and l["size"] > 0 for l in layers))
    if not ok:
        raise DigestError(f"{instance_id}: layers must be a non-empty list of {{digest, size}} with sha256 digests and positive integer sizes")
    if type(record["compressed_layer_bytes"]) is not int or record["compressed_layer_bytes"] != sum(l["size"] for l in layers):
        raise DigestError(f"{instance_id}: compressed_layer_bytes does not equal the sum of the layer sizes")
    if type(record["config_size"]) is not int or record["config_size"] <= 0:
        raise DigestError(f"{instance_id}: config_size must be a positive integer")
    prov = record["provenance"]
    if not (isinstance(prov, dict) and isinstance(prov.get("source"), str) and prov["source"] and isinstance(prov.get("verified_on"), str) and _DATE.fullmatch(prov["verified_on"])):
        raise DigestError(f"{instance_id}: provenance needs a source and a verified_on date")
    return json.loads(json.dumps(record))


def load_digest_record(root: Path, instance_id: str) -> dict[str, Any]:
    """The verified digest record of one task. Raises ``DigestError`` for a missing lock file, a malformed file, no record for the task, or an invalid record."""
    path = Path(root) / LOCK_FILE
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise DigestError(f"cannot read {LOCK_FILE}: {type(e).__name__}") from e
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or doc.get("platform") != PLATFORM or not isinstance(doc.get("images"), dict):
        raise DigestError(f"{LOCK_FILE} is not a schema-{SCHEMA} {PLATFORM} digest lock")
    if instance_id not in doc["images"]:
        raise DigestError(f"no digest record for {instance_id} in {LOCK_FILE}: it must be resolved and verified first (nothing falls back to a tag)")
    return _validate(doc["images"][instance_id], instance_id)


# ------------------------------------------------------------------ registry verification (injected fetch)
def verify_record_against_registry(record: Mapping[str, Any], fetch: Fetch) -> None:
    """Fail-closed check of the record against the registry. Fetches exactly two manifests, both BY DIGEST: the index and the linux/amd64 platform manifest."""
    repo, idx_digest, plat_digest = record["repository"], record["index_digest"], record["platform_manifest_digest"]

    def get(reference_: str, accept: str, what: str) -> "tuple[Mapping[str, str], bytes]":
        try:
            headers, body = fetch(repo, reference_, accept)
        except Exception as e:
            raise DigestError(f"the {what} could not be fetched from the registry ({type(e).__name__})") from e
        if not isinstance(body, (bytes, bytearray)):
            raise DigestError(f"the {what} response is not bytes")
        return headers, bytes(body)

    headers, body = get(idx_digest, OCI_INDEX, "index")
    if _sha256(body) != idx_digest:
        raise DigestError(f"the index body does not hash to the recorded index digest ({_sha256(body)[:19]}… != {idx_digest[:19]}…)")
    if (headers.get("docker-content-digest") if headers else None) != idx_digest:
        raise DigestError("the registry's digest header for the index is missing or differs from the recorded index digest")
    try:
        index = json.loads(body)
        entries = index["manifests"]
    except (ValueError, KeyError, TypeError) as e:
        raise DigestError("the index body is not an OCI index") from e
    if index.get("mediaType") != OCI_INDEX:
        raise DigestError("the index body is not an OCI image index")
    amd64 = [m for m in entries if isinstance(m, dict) and (m.get("platform") or {}).get("os") == "linux" and (m.get("platform") or {}).get("architecture") == "amd64"
             and not (m.get("platform") or {}).get("variant")]
    if len(amd64) != 1:
        raise DigestError(f"the index must contain exactly one {PLATFORM} manifest, found {len(amd64)}")
    if amd64[0].get("digest") != plat_digest:
        if any(isinstance(m, dict) and m.get("digest") == plat_digest for m in entries):
            raise DigestError(f"the recorded platform manifest is a member of the index but not its {PLATFORM} entry")
        raise DigestError("the recorded platform manifest is not a member of the index")
    headers, body = get(plat_digest, OCI_MANIFEST, "platform manifest")
    if _sha256(body) != plat_digest:
        raise DigestError("the platform manifest body does not hash to the recorded platform manifest digest")
    if (headers.get("docker-content-digest") if headers else None) != plat_digest:
        raise DigestError("the registry's digest header for the platform manifest is missing or differs from the recorded digest")
    try:
        mf = json.loads(body)
        got_layers = [{"digest": l["digest"], "size": l["size"]} for l in mf["layers"]]
        got_cfg = (mf["config"]["digest"], mf["config"]["size"])
    except (ValueError, KeyError, TypeError) as e:
        raise DigestError("the platform manifest is not an OCI image manifest") from e
    if got_layers != record["layers"] or sum(l["size"] for l in got_layers) != record["compressed_layer_bytes"]:
        raise DigestError("the platform manifest's layers differ from the digest record")
    if got_cfg != (record["config_digest"], record["config_size"]):
        raise DigestError("the platform manifest's config differs from the digest record")


# ------------------------------------------------------------------ the Docker side (always by digest reference)
def _check_identity(image: Any, record: Mapping[str, Any]) -> Any:
    if getattr(image, "id", None) != record["index_digest"]:
        raise DigestError(f"the local image ID {getattr(image, 'id', None)!r} is not the index digest {record['index_digest']}")
    attrs = getattr(image, "attrs", None) or {}
    if f"{attrs.get('Os')}/{attrs.get('Architecture')}" != PLATFORM:
        raise DigestError(f"the local image is {attrs.get('Os')}/{attrs.get('Architecture')}, not {PLATFORM}")
    return image


def local_image(client: Any, record: Mapping[str, Any]) -> Any | None:
    """The local image for this record, found by its digest reference and identity-checked; None if it is not local. Other daemon errors propagate."""
    try:
        image = client.images.get(reference(record))
    except Exception as e:
        if isinstance(e, LookupError) or type(e).__name__ in ("ImageNotFound", "NotFound"):
            return None
        raise
    return _check_identity(image, record)


def image_present(client: Any, record: Mapping[str, Any]) -> bool:
    return local_image(client, record) is not None


def pull_by_digest(client: Any, record: Mapping[str, Any]) -> Any:
    """Pull ``<repository>@<index digest>`` for linux/amd64 and require the resulting image to be exactly that index. Never a tag."""
    ref = reference(record)
    if "@sha256:" not in ref:
        raise DigestError("refusing to pull by anything other than an index digest")
    return _check_identity(client.images.pull(ref, platform=PLATFORM), record)


# ------------------------------------------------------------------ the one place that touches the network (anonymous, manifests only)
def docker_hub_fetch(repository: str, reference_: str, accept: str) -> "tuple[Mapping[str, str], bytes]":  # pragma: no cover - real network; never called by tests
    import urllib.request
    base = {}
    with urllib.request.urlopen(f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repository}:pull", timeout=60) as r:
        token = json.loads(r.read())["token"]
    req = urllib.request.Request(f"https://registry-1.docker.io/v2/{repository}/manifests/{reference_}", headers={"Authorization": f"Bearer {token}", "Accept": accept, **base})
    with urllib.request.urlopen(req, timeout=60) as r:
        return {k.lower(): v for k, v in r.headers.items()}, r.read()

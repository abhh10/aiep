"""Per-task preparation, generalized from the pytest-5227 scripts: pin the image, extract the seed, verify the agent identity. Docker-touching, approval-gated.

Each step is idempotent in the safe direction (a pin is never overwritten), refuses below the 16 GiB start gate BEFORE any Docker call, never pulls (the image must
already be local: ``client.images.get`` raises otherwise), and records the result through ``aiep.task_pins``. Nothing here runs in the test suite against Docker:
the tests use fakes. The image PULL itself is deliberately not here: it is a separate, explicitly approved download (``scripts/pull_task_image.py`` does only that).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable

from .agent_env import IDENTITY_SCRIPT, check_identity, load_agent_config
from .agent_runner import AgentRunSpec, _teardown, _wait_running, agent_context, build_create_kwargs, exec_context
from .container_exec import run_exec
from .container_policy import verify_inspect
from .seed import build_component_archive, load_seed
from .seed_source import extract_seed
from .task_pins import PinError, add_evaluator_image, add_seed_pin, evaluator_image_pin, write_identity
from .image_digests import load_digest_record, local_image, reference

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
MIN_FREE_BYTES = 16 * 1024**3
# Raw tar limit for extracting a task's /testbed. 128 MiB is validated for sympy__sympy-18621 ONLY: its /testbed streams as 104,282,112 B (99.45 MiB, of which .git is 67.3 MiB),
# measured 2026-11-22 with a bounded read-only probe. It is NOT a universal SWE-bench limit: other tasks (matplotlib, scikit-learn ...) are unmeasured and may need more, and the agent's
# /testbed tmpfs (192 MiB, agent_runner.py) must hold the seed plus the agent's own writes. The seed parser's content (256 MiB), per-file (64 MiB) and entry-count (200,000) limits are separate and unchanged.
MAX_ARCHIVE_BYTES = 128 * 1024**2


class SetupRefused(RuntimeError):
    pass


def _gate(free_bytes: int) -> None:
    if free_bytes < MIN_FREE_BYTES:
        raise SetupRefused(f"{free_bytes / 1024**3:.2f} GiB free, 16 GiB required: no Docker API call was made")


def pin_image(client: Any, root: Path, instance_id: str, *, free_bytes: int) -> str:
    """Record the immutable ID of the task's LOCAL image (read-only inspect; never a pull). The image is found by its verified digest reference
    (``<repository>@<index digest>`` from ``config/image_digests.json``), never by the mutable ``:latest`` name, and its ID must equal the index digest. Returns the ID."""
    _gate(free_bytes)
    record = load_digest_record(root, instance_id)  # DigestError if there is no verified record: no fallback
    ref = reference(record)
    img = local_image(client, record)  # DigestError if the local ID or platform is not the recorded one
    if img is None:
        raise SetupRefused(f"{ref} is not available locally (nothing is pulled)")
    add_evaluator_image(root, instance_id, img.id, digest_reference=ref,
                        source=f"pinned from the local image {ref} on {time.strftime('%Y-%m-%d', time.gmtime())}; the ID equals the verified index digest "
                               "(config/image_digests.json); recorded by scripts/pin_task_image.py after an approved pull")
    return img.id


def extract_and_pin_seed(client: Any, root: Path, instance_id: str, *, free_bytes: int, base_commit: str, lineage: Callable[[Any], dict[str, Any]],
                         run_id: str | None = None, extract: Callable[..., Any] = extract_seed) -> dict[str, Any]:
    """Extract the seed from the pinned image and pin it, but ONLY if its history is the registered one: the seed's HEAD is the synthetic 'SWE-bench' commit and
    its parent is the dataset ``base_commit`` (the same relation ``aiep.handoff`` enforces after a run, here checked before any money is spent).

    ``base_commit`` comes from the dataset record, never from the image. ``lineage(seed)`` is supplied by the script layer (host-side git is confined there) and
    returns ``{"head", "parent"}`` of the extracted seed. On any mismatch nothing is pinned; the extracted files are left in place, unpinned, as evidence.
    """
    _gate(free_bytes)
    if not isinstance(base_commit, str) or not _COMMIT.fullmatch(base_commit):
        raise SetupRefused("base_commit must be the dataset record's 40-character lowercase commit: no Docker API call was made")
    pin = evaluator_image_pin(root, instance_id)
    if not pin:
        raise SetupRefused(f"{instance_id} has no evaluator image pin: run pin_task_image first")
    directory = Path(root) / ".cache" / "seeds" / instance_id
    run_id = run_id or f"seedsrc-{instance_id.split('__')[1]}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"[:64]
    res = extract(client, image_id=pin["image_id"], run_id=run_id, directory=directory, max_bytes=MAX_ARCHIVE_BYTES)
    m = res.seed.manifest
    try:
        got = lineage(res.seed)
        head, parent = got.get("head"), got.get("parent")
    except Exception as e:
        raise SetupRefused(f"the seed's HEAD and parent could not be checked ({type(e).__name__}: {e}); nothing was pinned, the extracted seed in {directory} is unpinned") from e
    if head != m.head:
        raise SetupRefused(f"git reports HEAD {str(head)[:12]} but the seed manifest records {m.head[:12]}; nothing was pinned (extracted seed left in {directory})")
    if parent != base_commit:
        raise SetupRefused(f"the seed's HEAD {m.head[:12]} has parent {str(parent)[:12] or 'none'}, not the dataset base_commit {base_commit[:12]}; nothing was pinned "
                           f"(extracted seed left in {directory}, unpinned)")
    add_seed_pin(root, instance_id, directory=f".cache/seeds/{instance_id}", sha256=m.archive_sha256, head=m.head, entries=len(m.entries), image_id=pin["image_id"])
    return {"seed_sha256": m.archive_sha256, "head": m.head, "base_commit": base_commit, "entries": len(m.entries), "container_removed": res.removed}


def verify_and_pin_identity(client: Any, root: Path, instance_id: str, *, free_bytes: int, candidate_path: Path | None = None) -> dict[str, Any]:
    """Observe the agent account IN THIS IMAGE and record it. The candidate account (name, uid, gid, home, interpreter paths) comes from the original verified identity
    config; the probe must report exactly that uid, gid, name and a passwd entry with the same home, or nothing is written. The per-run probe stays mandatory."""
    _gate(free_bytes)
    pin = evaluator_image_pin(root, instance_id)
    seed_pin = (__import__("json").loads((Path(root) / "config" / "seeds.json").read_text()).get("seeds") or {}).get(instance_id)
    if not pin or not seed_pin:
        raise SetupRefused(f"{instance_id} needs its image pin and seed pin before its identity can be verified")
    candidate = load_agent_config(candidate_path or Path(root) / "config" / "agent_identity.json")
    seed = load_seed(Path(root) / seed_pin["directory"], expected_sha256=seed_pin["sha256"])
    import json as _json
    experiment = _json.loads((Path(root) / "config" / "experiment.json").read_text())
    spec = AgentRunSpec(run_id=f"identity-{instance_id.split('__')[1]}"[:60], image=pin["image_id"], gateway_url="http://gateway.invalid:8080", agent_network="none",
                        pinned_model=experiment["model"], run_token="identity-check-token", seed=seed, components=build_component_archive({"unused": (b"", 0o755)}),
                        python=candidate.verification_python, identity=candidate.identity, config_label="A", task_text="identity-check", image_id=pin["image_id"])
    kwargs = build_create_kwargs(spec)
    image, command = kwargs.pop("image"), kwargs.pop("command")
    present = client.images.get(image)
    if getattr(present, "id", None) != pin["image_id"]:
        raise SetupRefused("the local image does not have the pinned ID")
    container = client.containers.create(image, command, **kwargs)
    try:
        verify_inspect("agent", client.api.inspect_container(container.id), agent_context(spec))
        container.start()
        _wait_running(container, time.monotonic, time.sleep, 120.0)
        probe = run_exec(client.api, container.id, ["/bin/sh", "-c", IDENTITY_SCRIPT], ctx=exec_context(spec), workdir="/", timeout_s=60)
        problems = check_identity(probe.stdout, candidate.identity) if not (probe.timed_out or probe.truncated) else ["identity probe failed"]
    finally:
        removed, err = _teardown(container)
    if err or not removed:
        raise SetupRefused(f"the identity-check container could not be removed: {err}")
    if problems:
        raise SetupRefused(f"the image's account is not the configured one: {'; '.join(problems)}")
    lines = probe.stdout.decode("utf-8", "replace").splitlines()
    path = write_identity(root, instance_id, candidate, image_id=pin["image_id"], verified_on=time.strftime("%Y-%m-%d", time.gmtime()),
                          evidence=[f"identity probe in a throwaway hardened container (network none, all capabilities dropped, no-new-privileges) from image {pin['image_id']}: "
                                    f"uid {lines[0]}, gid {lines[1]}, name {lines[2]}, passwd entry {lines[3] if len(lines) > 3 else 'MISSING'}"])
    return {"identity_file": str(path), "uid": candidate.identity.uid, "name": candidate.identity.name}

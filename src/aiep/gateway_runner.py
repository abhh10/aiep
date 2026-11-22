"""Inject, verify and only then start the gateway process inside its container.

The gateway container's PID 1 is the inert keepalive (``container_policy``), so nothing of ours runs
until this module says so. The order is fixed and cannot be reordered by a caller:

 1. the injection tar must still match its own manifest (``verify_archive``);
 2. stream it into the ``/opt/aiep-gw`` tmpfs as the gateway's own non-root identity;
 3. list what is really there with the gateway image's configured interpreter and compare with the
    manifest: missing, extra, modified, mode, symlink and owner differences all raise
    ``GatewayIntegrityError``;
 4. only then run ``python -m aiep.gateway`` by exec.

The gateway's configuration and the real API key are in the container's environment (set at create,
policy-checked); the start exec carries no environment of its own.

Tested with fakes only. Real Docker behaviour is a Phase 2 validation item.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .container_exec import REGISTERED_SCRIPTS, ExecSession, inject_tar, list_tree
from .container_policy import GATEWAY_COMMAND_TAIL, ExecContext, GatewayContext, harden_exec
from .seed import Seed, build_component_archive, verify_archive, verify_listing

GATEWAY_DEST = "/opt/aiep-gw"
# Everything the gateway imports, and nothing else. ``main()`` finds its configuration at
# ``<package root>/config`` where the package root is two levels above ``src/aiep/gateway.py``.
GATEWAY_FILES = (
    "src/aiep/__init__.py", "src/aiep/gateway.py", "src/aiep/ledger.py", "src/aiep/listen.py", "src/aiep/pricing.py",
    "config/experiment.json", "config/model_pricing.json",
)


class GatewayImageUnpinned(RuntimeError):
    """``config/gateway_image.json`` has no verified, immutable pin; no gateway may be created from it."""


@dataclass(frozen=True)
class GatewayImagePin:
    image: str  # what is given to ``containers.create`` (the pinned repo digest reference)
    image_id: str  # what ``inspect`` must report
    python: str
    uid: int
    gid: int
    declared_volumes: tuple[str, ...]


_DIGEST_REF = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


def load_gateway_image(path: Path) -> GatewayImagePin:
    """Load the gateway image pin. Refuses a tag, a missing digest or ID, or an unverified status."""
    from .container_policy import GATEWAY_ENTRYPOINT, GATEWAY_KEEPALIVE_ARGS
    cfg = json.loads(Path(path).read_text())
    if cfg.get("status") != "verified":
        raise GatewayImageUnpinned(f"gateway image status is {cfg.get('status')!r}, not 'verified'")
    ref, image_id = cfg.get("repo_digest"), cfg.get("image_id")
    if not (isinstance(ref, str) and _DIGEST_REF.fullmatch(ref) and isinstance(image_id, str) and _IMAGE_ID.fullmatch(image_id)):
        raise GatewayImageUnpinned("the gateway image needs a repo digest (name@sha256:...) and an image ID (sha256:...)")
    if cfg.get("entrypoint") != GATEWAY_ENTRYPOINT or cfg.get("command") != GATEWAY_KEEPALIVE_ARGS:
        raise GatewayImageUnpinned("the recorded entrypoint/command differ from the fixed gateway keepalive")
    python, uid, gid = cfg.get("python"), cfg.get("uid"), cfg.get("gid")
    from .container_policy import PolicyViolation, validate_interpreter_path
    try:
        validate_interpreter_path(python, "gateway image")
    except PolicyViolation as e:
        raise GatewayImageUnpinned(f"the gateway image has no valid explicit interpreter path recorded: {e}") from e
    if not (type(uid) is int and type(gid) is int and uid > 0 and gid > 0):
        raise GatewayImageUnpinned("the gateway image has no explicit non-root uid and gid recorded")
    return GatewayImagePin(ref, image_id, python, uid, gid, tuple(cfg.get("declared_volumes") or ()))


def gateway_context(pin: GatewayImagePin, *, agent_network: str, proxy_network: str, real_key: str = "",
                    agent_cidr: str | None = None) -> GatewayContext:
    """Build the policy context from a verified pin: python, uid, gid and VOLUMEs all come from the recorded config."""
    return GatewayContext(agent_network=agent_network, proxy_network=proxy_network, image_id=pin.image_id,
                          python=pin.python, uid=pin.uid, gid=pin.gid, extra_tmpfs=pin.declared_volumes, real_key=real_key,
                          agent_cidr=agent_cidr)


class GatewayIntegrityError(RuntimeError):
    """The injected gateway files are not exactly the manifest; the gateway was NOT started."""


def build_gateway_component(repo_root: Path) -> Seed:
    """The gateway's injection tar plus its own manifest (modes 0644, directories 0755)."""
    repo_root = Path(repo_root)
    files = {rel: ((repo_root / rel).read_bytes(), 0o644) for rel in GATEWAY_FILES}
    return build_component_archive(files)


def gateway_exec_context(ctx: GatewayContext, *, readable_paths: frozenset[str] = frozenset(),
                         forbidden_secrets: tuple[str, ...] = ()) -> ExecContext:
    """The exec policy for the gateway container: its identity, its interpreter, and permission to start it."""
    return ExecContext(user=ctx.user, allowed_scripts=REGISTERED_SCRIPTS, readable_paths=readable_paths,
                       forbidden_secrets=forbidden_secrets, python=ctx.python, may_start_gateway=True,
                       env={"HOME": "/tmp", "PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C", "TZ": "UTC"})


def inject_and_verify_gateway(api: Any, container_id: str, code: Seed, ctx: GatewayContext, ectx: ExecContext) -> None:
    """Inject ``code`` and verify it entry by entry. Nothing is started."""
    if ectx.user != ctx.user or ectx.python != ctx.python or not ectx.may_start_gateway:
        raise GatewayIntegrityError("the exec context does not match the gateway context")
    verify_archive(code)
    inject_tar(api, container_id, code.tar, GATEWAY_DEST, ctx=ectx)
    problems = verify_listing(code.manifest, list_tree(api, container_id, GATEWAY_DEST, ctx=ectx),
                              expect_owner=(ctx.uid, ctx.gid))
    if problems:
        raise GatewayIntegrityError(f"{GATEWAY_DEST} does not match its manifest: {'; '.join(problems[:5])}")


def start_gateway(api: Any, container_id: str, ctx: GatewayContext, ectx: ExecContext) -> ExecSession:
    """The production start: ``python -m aiep.gateway`` by a policy-checked exec. Only called after ``inject_and_verify_gateway``."""
    request = harden_exec([ctx.python, *GATEWAY_COMMAND_TAIL], user=ctx.user, workdir="/", env={}, ctx=ectx)
    return ExecSession(api, container_id, request).start()


def inject_verify_start_gateway(api: Any, container_id: str, code: Seed, ctx: GatewayContext,
                                ectx: ExecContext) -> ExecSession:
    """Inject ``code``, verify it entry by entry, and only then start the gateway. Returns the running exec."""
    inject_and_verify_gateway(api, container_id, code, ctx, ectx)
    return start_gateway(api, container_id, ctx, ectx)

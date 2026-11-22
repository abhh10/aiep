#!/usr/bin/env python3
"""Validate injecting the real pytest-dev__pytest-5227 seed into an isolated agent container (approval-gated Docker step).

Creates ONE agent container exactly as ``agent_runner`` would (same policy-checked kwargs, ``verify_inspect``), but with
``agent_network="none"`` and dummy gateway values, so it has no network at all. It then:

  1. verifies the container's configuration (``verify_inspect``) and starts it (PID 1 is an inert ``sleep``);
  2. checks the account (uid:gid 1000:1000, passwd entry, home);
  3. injects the saved seed into the ``/testbed`` tmpfs with ``inject_tar`` (docker exec stdin);
  4. verifies EVERY manifest entry with ``list_tree`` + ``verify_listing(expect_owner=(1000, 1000))``;
  5. checks ``git rev-parse HEAD`` equals the manifest HEAD and ``git cat-file -e <HEAD>^{commit}``;
  6. runs a scratch-file write/read/delete probe under /testbed, then re-verifies the listing (the probe must leave no trace);
  7. removes exactly the container it created and closes the Docker client.

It never runs Claude Code and never starts the gateway: every exec it issues itself is checked against a fixed list of
exact commands (``approved_commands``), and the seed/listing steps use the existing ``inject_tar``/``list_tree`` shapes.
It never pulls images, creates networks or volumes, mounts anything, or touches evaluator data. The seed files are only read.

    PYTHONPATH=src .venv/bin/python scripts/inject_pytest5227_seed_check.py

Exit codes: 0 every check passed; 1 a check, the container, cleanup or client closure failed; 2 refused by the disk gate
(no Docker API call was made). Constructing the Docker client is itself an API call, so it happens only after the disk gate
and every local check (config, seed hash, policy-checked kwargs) have passed.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiep.agent_env import IDENTITY_SCRIPT, IdentityUnverified, check_identity, load_agent_config  # noqa: E402
from aiep.agent_runner import (AgentRunSpec, _teardown, _wait_running, agent_context, build_create_kwargs,  # noqa: E402
                               exec_context)
from aiep.container_exec import git_exec_env, inject_tar, list_tree, run_exec  # noqa: E402
from aiep.container_policy import PolicyViolation, verify_inspect  # noqa: E402
from aiep.patch import HARDENED_GIT  # noqa: E402
from aiep.seed import SeedError, build_component_archive, load_seed, verify_archive, verify_listing  # noqa: E402

INSTANCE_ID = "pytest-dev__pytest-5227"
IMAGE_ID = "sha256:c91459f86ed261ab81871b086d7ca2d9fe16abd2ddf374bf172edd5d991fd28b"
SEED_DIR = Path(".cache") / "seeds" / INSTANCE_ID
EXPECTED_SEED_SHA256 = "8727a0bccdb2b21b7faaf33f66d3086738105b64744db051f1b944e8abc6e3b6"
EXPECTED_SEED_HEAD = "aa6a504d7ea32c59ae245f90682906db5ac5213c"
MIN_FREE_BYTES = 16 * 1024**3  # project disk policy: below this, no Docker resource may be created
AGENT_NETWORK = "none"  # no network at all (stricter than the experiment's internal agent network)
DUMMY_GATEWAY_URL = "http://gateway.invalid:8080"  # never contacted: nothing here runs a program that could
DUMMY_TOKEN = "injection-check-token"  # not a credential
DUMMY_TASK = "injection-check"
PROBE_FILE = "/testbed/.aiep_write_probe"
PROBE_SCRIPT = (f'set -e; f={PROBE_FILE}; echo aiep-probe > "$f"; test "$(cat "$f")" = aiep-probe; '
                f'rm -f "$f"; test ! -e "$f"; echo probe-ok')


class CheckFailed(RuntimeError):
    """One validation step did not pass."""


def approved_commands(head: str) -> list[list[str]]:
    """Every command this driver itself may run in the container (injection and listing use ``container_exec`` helpers)."""
    return [
        ["/bin/sh", "-c", IDENTITY_SCRIPT],
        ["/usr/bin/git", *HARDENED_GIT, "rev-parse", "HEAD"],
        ["/usr/bin/git", *HARDENED_GIT, "cat-file", "-e", f"{head}^{{commit}}"],
        ["/bin/sh", "-c", PROBE_SCRIPT],
    ]


def _default_client() -> Any:
    import docker  # imported late: importing is harmless, constructing the client is the first API call

    return docker.from_env(timeout=120)


def _exec(api: Any, cid: str, ectx: Any, argv: list[str], head: str, *, workdir: str = "/testbed"):
    """Run one of the approved commands. Claude, the gateway and anything else are refused before Docker is touched."""
    if argv not in approved_commands(head):
        raise PolicyViolation(f"injection check: refusing a command that is not on the approved list: {argv[:2]!r}")
    return run_exec(api, cid, argv, ctx=ectx, workdir=workdir, env=git_exec_env(ectx) if argv[0] == "/usr/bin/git" else None,
                    timeout_s=60, max_stdout=1024 * 1024)


def build_spec(root: Path, seed_dir: Path, expected_sha256: str) -> tuple[AgentRunSpec, Any]:
    """Everything that can be checked without Docker: identity config, the stored seed, and the policy-checked kwargs."""
    cfg = load_agent_config(root / "config" / "agent_identity.json", image_id=IMAGE_ID)
    seed = load_seed(seed_dir, expected_sha256=expected_sha256)
    experiment = json.loads((root / "config" / "experiment.json").read_text())
    spec = AgentRunSpec(
        run_id=f"injcheck-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}", image=IMAGE_ID, gateway_url=DUMMY_GATEWAY_URL,
        agent_network=AGENT_NETWORK, pinned_model=experiment["model"], run_token=DUMMY_TOKEN, seed=seed,
        components=build_component_archive({"unused": (b"", 0o755)}),  # required by the spec; never injected here
        python=cfg.verification_python, identity=cfg.identity, config_label="A", task_text=DUMMY_TASK, image_id=IMAGE_ID)
    verify_archive(spec.seed)
    build_create_kwargs(spec)  # raises PolicyViolation on anything off-policy, before Docker
    return spec, cfg


def run_checks(client: Any, spec: AgentRunSpec, report: list[str]) -> None:
    """The Docker part. Raises on the first failed check; the caller removes the container."""
    head = spec.seed.head
    ectx = dataclasses.replace(exec_context(spec), allowed_scripts=exec_context(spec).allowed_scripts | {PROBE_SCRIPT})
    api = client.api

    present = client.images.get(IMAGE_ID)  # read-only; a missing image is an error, never a pull
    if getattr(present, "id", None) != IMAGE_ID:
        raise CheckFailed("the local image does not have the pinned ID")

    kwargs = build_create_kwargs(spec)
    image, command = kwargs.pop("image"), kwargs.pop("command")
    try:
        container = client.containers.create(image, command, **kwargs)
    except Exception as e:
        raise CheckFailed(f"creating the container failed: {type(e).__name__}: {e}") from e
    report.append(f"container {container.id} created (never run Claude; network={AGENT_NETWORK})")
    try:
        cid = container.id
        verify_inspect("agent", api.inspect_container(cid), agent_context(spec))
        report.append("PASS verify_inspect: configuration matches the agent policy")
        container.start()
        _wait_running(container, time.monotonic, time.sleep, 60.0)
        report.append("PASS container running (inert keepalive)")

        out = _exec(api, cid, ectx, ["/bin/sh", "-c", IDENTITY_SCRIPT], head, workdir="/")
        problems = check_identity(out.stdout, spec.identity) if not (out.timed_out or out.truncated) else ["probe failed"]
        if problems:
            raise CheckFailed(f"identity: {'; '.join(problems)}")
        report.append(f"PASS identity {spec.identity.name}:{spec.identity.uid}:{spec.identity.gid}")

        inject_tar(api, cid, spec.seed.tar, "/testbed", ctx=ectx)
        owner = (spec.identity.uid, spec.identity.gid)
        problems = verify_listing(spec.seed.manifest, list_tree(api, cid, "/testbed", ctx=ectx), expect_owner=owner)
        if problems:
            raise CheckFailed(f"/testbed does not match the manifest: {'; '.join(problems[:5])} ({len(problems)} problems)")
        report.append(f"PASS injected and verified {len(spec.seed.manifest.entries)} entries "
                      f"(content, sizes, modes, owner {owner[0]}:{owner[1]})")

        out = _exec(api, cid, ectx, ["/usr/bin/git", *HARDENED_GIT, "rev-parse", "HEAD"], head)
        got = out.stdout.decode("utf-8", "replace").strip()
        if out.exit_code != 0 or got != head:
            raise CheckFailed(f"git rev-parse HEAD is {got!r} (exit {out.exit_code}), manifest HEAD is {head}")
        report.append(f"PASS git HEAD == manifest HEAD {head}")
        out = _exec(api, cid, ectx, ["/usr/bin/git", *HARDENED_GIT, "cat-file", "-e", f"{head}^{{commit}}"], head)
        if out.exit_code != 0 or out.timed_out:
            raise CheckFailed("the HEAD commit object is missing from the repository")
        report.append("PASS HEAD commit object exists")

        out = _exec(api, cid, ectx, ["/bin/sh", "-c", PROBE_SCRIPT], head)
        if out.exit_code != 0 or b"probe-ok" not in out.stdout:
            raise CheckFailed(f"scratch-file write/read/delete probe failed (exit {out.exit_code})")
        problems = verify_listing(spec.seed.manifest, list_tree(api, cid, "/testbed", ctx=ectx), expect_owner=owner)
        if problems:
            raise CheckFailed(f"the probe left /testbed different from the manifest: {'; '.join(problems[:5])}")
        report.append("PASS scratch-file write/read/delete probe; listing still identical to the manifest")
    finally:
        removed, error = _teardown(container)  # kill + remove(force=True, v=True) of exactly this container object
        report.append(f"container {container.id} removed: {removed}")
        if not removed:
            print(f"WARNING: container {container.id} could not be removed: {error}", file=sys.stderr)
            report.append("CLEANUP FAILED")


def main(*, client_factory: Callable[[], Any] = _default_client, free_bytes: Callable[[Path], int] | None = None,
         root: Path = ROOT, seed_dir: Path | None = None, expected_sha256: str = EXPECTED_SEED_SHA256) -> int:
    free = (free_bytes or (lambda p: shutil.disk_usage(p).free))(root)
    if free < MIN_FREE_BYTES:
        print(f"REFUSED: {free / 1024**3:.2f} GiB free, {MIN_FREE_BYTES / 1024**3:.0f} GiB required. "
              "No Docker API call was made.", file=sys.stderr)
        return 2
    try:
        spec, _cfg = build_spec(root, seed_dir or root / SEED_DIR, expected_sha256)
    except (IdentityUnverified, SeedError, PolicyViolation, OSError, ValueError, KeyError) as e:
        print(f"ERROR before Docker: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    report: list[str] = []
    client = client_factory()
    status = 1
    try:
        try:
            run_checks(client, spec, report)
            status = 0 if "CLEANUP FAILED" not in report else 1
        except Exception as e:  # reported; KeyboardInterrupt still propagates (after cleanup below and in run_checks)
            print(f"FAIL: {type(e).__name__}: {e}", file=sys.stderr)
    finally:
        for line in report:
            print(line)
        # Closing the client is the reliable release of the SDK's HTTP connections.
        try:
            client.close()
        except Exception as e:
            print(f"WARNING: closing the Docker client failed: {type(e).__name__}: {e}", file=sys.stderr)
            status = 1
    print("RESULT: " + ("ALL CHECKS PASSED" if status == 0 else "FAILED"))
    return status


if __name__ == "__main__":
    sys.exit(main())

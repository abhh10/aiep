"""The agent's execution environment and identity checks: one definition, shared by every configuration.

Config A (basic) and Config B (test-and-fix) must differ ONLY in the prescribed workflow (the prompt).
Everything the process sees is therefore built here from the identity alone: ``experiment_env`` has no
parameter that could carry the configuration, and every exec in a run (Claude, git, verification, quiesce)
gets exactly this mapping. The values reproduce what ``conda activate testbed`` produced in the M1 evaluator
logs (``runs/m1/*/logs/.../test_output.txt``, measured), so ``python`` and ``pytest`` of the task's own
environment are found first, then base Miniconda, then the system directories.

The agent's *verification* interpreter (``AgentRunSpec.python``) is pinned separately and is unrelated to
this PATH: the orchestrator never searches a PATH for it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .container_policy import AgentIdentity, PolicyViolation

CONDA_BASE = "/opt/miniconda3"
TESTBED_PREFIX = f"{CONDA_BASE}/envs/testbed"
# Measured: the PATH after `conda activate testbed` in the M1 evaluation logs.
EXPERIMENT_PATH = ":".join([
    f"{TESTBED_PREFIX}/bin", f"{CONDA_BASE}/condabin", f"{CONDA_BASE}/bin",
    "/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin",
])


def experiment_env(identity: AgentIdentity) -> dict[str, str]:
    """The environment of every process the orchestrator starts in the agent container."""
    return {
        "HOME": identity.home,
        "PATH": EXPERIMENT_PATH,
        "CONDA_PREFIX": TESTBED_PREFIX,
        "CONDA_DEFAULT_ENV": "testbed",
        "LC_ALL": "C",
        "TZ": "UTC",
    }


def check_home_overlaps(identity: AgentIdentity, python: str) -> None:
    """The HOME tmpfs must not shadow, or be shadowed by, a directory on the experiment PATH or the pinned interpreter."""
    for d in [*EXPERIMENT_PATH.split(":"), python]:
        if identity.home == d or identity.home.startswith(d + "/") or d.startswith(identity.home + "/"):
            raise PolicyViolation(f"identity: home {identity.home!r} overlaps {d!r}, which the run executes programs from")


# ---------------------------------------------------------------------------- identity probe
# Run as the agent's user inside the started container, before anything is injected. It prints, one per
# line: uid, gid, login name, and the passwd entry for that uid (empty if the image has none).
IDENTITY_SCRIPT = ('/usr/bin/id -u; /usr/bin/id -g; /usr/bin/id -un; '
                   '/usr/bin/getent passwd "$(/usr/bin/id -u)"')


def check_identity(stdout: bytes | str, identity: AgentIdentity) -> list[str]:
    """Compare the probe output with the expected identity. Empty list means the account is what was configured."""
    text = stdout.decode("utf-8", "replace") if isinstance(stdout, bytes) else stdout
    lines = text.splitlines()
    if len(lines) < 3:
        return ["identity probe printed fewer than three lines"]
    problems = []
    if lines[0] != str(identity.uid):
        problems.append(f"uid is {lines[0]!r}, expected {identity.uid}")
    if lines[1] != str(identity.gid):
        problems.append(f"gid is {lines[1]!r}, expected {identity.gid}")
    if len(lines) < 4 or not lines[3].strip():
        problems.append("the image has no passwd entry for this uid (user lookup would fail inside the container)")
        return problems
    fields = lines[3].split(":")
    if len(fields) != 7:
        return problems + ["passwd entry is malformed"]
    if fields[0] != identity.name or lines[2] != identity.name:
        problems.append(f"account name is {fields[0]!r}/{lines[2]!r}, expected {identity.name!r}")
    if fields[2] != str(identity.uid) or fields[3] != str(identity.gid):
        problems.append(f"passwd uid:gid is {fields[2]}:{fields[3]}, expected {identity.uid}:{identity.gid}")
    if fields[5] != identity.home:
        problems.append(f"passwd home is {fields[5]!r}, expected {identity.home!r} (HOME tmpfs and $HOME must match it)")
    return problems


# ---------------------------------------------------------------------------- configuration
class IdentityUnverified(RuntimeError):
    """``config/agent_identity.json`` does not (yet) hold a verified identity; no run may start."""


def load_agent_identity(path: Path) -> AgentIdentity:
    """Load the recorded agent identity, refusing anything not explicitly verified against the real image."""
    cfg: dict[str, Any] = json.loads(Path(path).read_text())
    if cfg.get("status") != "verified":
        raise IdentityUnverified(f"agent identity status is {cfg.get('status')!r}, not 'verified'")
    try:
        return AgentIdentity(name=cfg["name"], uid=cfg["uid"], gid=cfg["gid"], home=cfg["home"])
    except (KeyError, TypeError, PolicyViolation) as e:
        raise IdentityUnverified(f"agent identity is incomplete or invalid: {e}") from e

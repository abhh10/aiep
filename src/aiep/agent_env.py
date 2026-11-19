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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .container_policy import AgentIdentity, PolicyViolation, validate_interpreter_path

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
    """``config/agent_identity.json`` does not hold a complete, consistent, verified identity; no run may start.

    Raised for every way the file can fail to be trusted: unreadable, malformed, not ``verified``, incomplete,
    containing unknown keys, or contradicting itself, the experiment environment, or the expected image.
    """


TESTBED_PYTHON = f"{TESTBED_PREFIX}/bin/python"  # the task's own interpreter; what ``PATH`` finds first
_SCHEMA = 1
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# Every key a verified file may carry. Anything else is refused, so a misspelt key cannot silently fall back to a default.
_REQUIRED_KEYS = frozenset({"schema", "status", "name", "uid", "gid", "home", "shell", "supplementary_groups",
                            "verification_python", "testbed_python", "verified_image_id", "evidence"})
_DOCUMENTATION_KEYS = frozenset({"verified_on", "still_unverified"})


class _DuplicateKey(ValueError):
    """A JSON object repeated a key. Carries the key name only, never a value."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` for ``json``: refuse a repeated key at every nesting level.

    Python's default keeps the last value silently, so ``{"uid": 0, "uid": 1000}`` would load as uid 1000 and the
    conflicting 0 would vanish. Identical repeats are refused too: nothing is resolved by position.
    """
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise _DuplicateKey(key)
        seen.add(key)
    return dict(pairs)


@dataclass(frozen=True)
class AgentConfig:
    """The verified, explicit agent configuration: identity plus the two interpreters, which stay distinct."""

    identity: AgentIdentity
    shell: str  # the account's login shell as recorded; informational (``docker exec -u`` is used, never ``su``)
    verification_python: str  # AIEP's own pinned verification interpreter (AgentRunSpec.python)
    testbed_python: str  # the task's interpreter, found first on the experiment PATH
    verified_image_id: str  # the image the identity was observed in


def load_agent_config(path: Path, *, image_id: str | None = None) -> AgentConfig:
    """Load and fully validate ``config/agent_identity.json``. Fails closed; nothing is defaulted or inferred.

    Refuses: unreadable, excessively nested or non-object JSON; a key repeated in any object at any depth; a wrong schema; any status but ``verified``; any missing or null required key;
    unknown keys; an invalid identity (root, booleans, an unsafe or overlapping home); a shell or interpreter that is
    not a plain absolute path; supplementary groups (``docker exec -u uid:gid`` supplies none, so a non-empty list would
    contradict how the account is used); a testbed interpreter other than the one on the experiment PATH; verification and
    testbed interpreters that are the same file; a home that shadows an interpreter or PATH directory; and, when
    ``image_id`` is given, an identity verified against a different image.
    """
    try:
        cfg: Any = json.loads(Path(path).read_text(), object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateKey as e:  # must precede ValueError, which it subclasses
        raise IdentityUnverified(f"the agent identity config repeats the key {e.args[0]!r}; "
                                 "conflicting values are never resolved by position") from e
    except RecursionError as e:  # only json.loads can raise it here: absurdly deep nesting, never a legitimate config
        raise IdentityUnverified("the agent identity config is nested too deeply to parse") from e
    except (OSError, ValueError) as e:
        raise IdentityUnverified(f"cannot read the agent identity config: {type(e).__name__}") from e
    if not isinstance(cfg, dict):
        raise IdentityUnverified("the agent identity config must be a JSON object")
    if cfg.get("schema") != _SCHEMA or type(cfg.get("schema")) is not int:
        raise IdentityUnverified(f"unsupported agent identity schema {cfg.get('schema')!r}")
    if cfg.get("status") != "verified":
        raise IdentityUnverified(f"agent identity status is {cfg.get('status')!r}, not 'verified'")
    missing = sorted(k for k in _REQUIRED_KEYS if cfg.get(k) is None)
    if missing:
        raise IdentityUnverified(f"agent identity is incomplete: missing or null {missing}")
    unknown = sorted(set(cfg) - _REQUIRED_KEYS - _DOCUMENTATION_KEYS)
    if unknown:
        raise IdentityUnverified(f"agent identity has unknown keys {unknown}")
    try:
        identity = AgentIdentity(name=cfg["name"], uid=cfg["uid"], gid=cfg["gid"], home=cfg["home"])
        shell = validate_interpreter_path(cfg["shell"], "identity shell")
        verification = validate_interpreter_path(cfg["verification_python"], "verification python")
        testbed = validate_interpreter_path(cfg["testbed_python"], "testbed python")
        if verification == testbed:
            raise PolicyViolation("the verification interpreter and the testbed interpreter must be different files")
        if testbed != TESTBED_PYTHON:
            raise PolicyViolation(f"testbed_python must be {TESTBED_PYTHON!r}, the interpreter first on the experiment PATH")
        check_home_overlaps(identity, verification)
        check_home_overlaps(identity, testbed)
        check_home_overlaps(identity, shell)
    except (TypeError, PolicyViolation) as e:
        raise IdentityUnverified(f"agent identity is invalid: {e}") from e
    groups = cfg["supplementary_groups"]
    if not isinstance(groups, list) or groups:
        raise IdentityUnverified("supplementary_groups must be an empty list (the account is used as uid:gid, which carries none)")
    verified = cfg["verified_image_id"]
    if not isinstance(verified, str) or not _IMAGE_ID_RE.fullmatch(verified):
        raise IdentityUnverified("verified_image_id must be a full sha256 image ID")
    if image_id is not None and image_id != verified:
        raise IdentityUnverified("the identity was verified against a different image than the one in use")
    evidence = cfg["evidence"]
    if not (isinstance(evidence, list) and evidence and all(isinstance(e, str) and e.strip() for e in evidence)):
        raise IdentityUnverified("a verified identity must cite its evidence (a non-empty list of strings)")
    return AgentConfig(identity=identity, shell=shell, verification_python=verification, testbed_python=testbed,
                       verified_image_id=verified)


def load_agent_identity(path: Path, *, image_id: str | None = None) -> AgentIdentity:
    """The verified agent identity (see ``load_agent_config`` for everything that is refused)."""
    return load_agent_config(path, image_id=image_id).identity

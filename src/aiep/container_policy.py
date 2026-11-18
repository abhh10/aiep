"""Fail-closed container policy for every container AIEP creates.

One module decides what a container may look like, per role. Callers pass the
kwargs they want; ``harden`` returns the kwargs to actually use, or raises
``PolicyViolation``. Anything not explicitly allowed is an error (allowlist, not
denylist). ``verify_inspect`` re-checks the *created* container against the same
policy, so a harness change that bypasses the kwargs path is still caught.

Roles implemented here: ``evaluator`` and ``agent``. The gateway role is a
Phase 2 item (it needs the Docker network design) and is deliberately absent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

GIB = 1024**3

# Evaluator (grades patches; runs agent-written code, so it gets NO network).
EVAL_MEM = "2g"
EVAL_MEM_BYTES = 2 * GIB
EVAL_PIDS = 1024
EVAL_NANO_CPUS = 2_000_000_000
EVAL_COMMAND = "tail -f /dev/null"  # what swebench 5.0.2 passes
EVAL_NAME_PREFIX = "sweb.eval."
# Wheelhouse is injected here with put_archive (Phase 3); pip is told to use only it.
WHEELHOUSE_DIR = "/opt/aiep-wheelhouse"
EVAL_ENV = {
    "PIP_NO_INDEX": "1",
    "PIP_FIND_LINKS": WHEELHOUSE_DIR,
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
}

# Agent (runs Claude Code on untrusted repo content).
AGENT_MEM = "2g"
AGENT_MEM_BYTES = 2 * GIB
AGENT_PIDS = 512
AGENT_NANO_CPUS = 2_000_000_000
AGENT_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "CLAUDE_CODE_AUTO_MODE_MODEL",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_UPDATES",
        "HOME",
        "TZ",
    }
)
_MODEL_ENV_KEYS = frozenset(k for k in AGENT_ENV_KEYS if "MODEL" in k)
# tmpfs mounts the agent may have. Their sizes count against the container's memory limit, so
# they are capped individually and in total; the caps are to be set from measurement (Phase 2).
TMPFS_ALLOWED_PATHS = frozenset({"/tmp", "/home/agent", "/testbed"})
TMPFS_MAX_EACH_BYTES = 1 * GIB
TMPFS_MAX_TOTAL_BYTES = 1280 * 1024**2
_TMPFS_FLAGS = frozenset({"rw", "exec", "noexec", "nosuid", "nodev"})
_TMPFS_VALUES = frozenset({"size", "uid", "gid", "mode"})
_SIZE_RE = re.compile(r"^(\d+)([kKmMgG]?)$")
_SECRET_LIKE = re.compile(r"sk-ant-|sk-[A-Za-z0-9]{20,}")

_EVAL_ALLOWED_IN = frozenset({"image", "command", "name", "user", "detach", "cap_add"})
_AGENT_ALLOWED_IN = frozenset(
    {
        "image", "command", "name", "user", "detach", "environment", "working_dir",
        "network", "labels", "read_only", "tmpfs", "cap_drop", "security_opt",
    }
)


class PolicyViolation(ValueError):
    """A container request or a created container violates policy."""


def _parse_size(text: str) -> int:
    m = _SIZE_RE.match(text)
    if not m:
        raise PolicyViolation(f"tmpfs size {text!r} is not a plain size like 256m")
    return int(m.group(1)) * {"": 1, "k": 1024, "m": 1024**2, "g": GIB}[m.group(2).lower()]


def validate_tmpfs(mapping: Mapping[str, Any], *, creating: bool) -> None:
    """Allowlist the agent's tmpfs mounts. ``creating`` adds the hardening flags we demand of new mounts."""
    _require(isinstance(mapping, Mapping), "agent: tmpfs must be a mapping of path to options")
    total = 0
    for path, opts in mapping.items():
        _require(path in TMPFS_ALLOWED_PATHS, f"agent: tmpfs path {path!r} is not allowed")
        _require(isinstance(opts, str) and opts, f"agent: tmpfs options for {path} must be a string")
        flags, values = set(), {}
        for tok in opts.split(","):
            if "=" in tok:
                k, _, v = tok.partition("=")
                _require(k in _TMPFS_VALUES and k not in values, f"agent: tmpfs option {tok!r} is not allowed")
                values[k] = v
            else:
                _require(tok in _TMPFS_FLAGS, f"agent: tmpfs option {tok!r} is not allowed")
                flags.add(tok)
        _require("size" in values, f"agent: tmpfs {path} needs an explicit size")
        size = _parse_size(values["size"])
        _require(0 < size <= TMPFS_MAX_EACH_BYTES, f"agent: tmpfs {path} size exceeds the per-mount cap")
        total += size
        for k in ("uid", "gid"):
            if k in values:
                _require(values[k].isascii() and values[k].isdigit() and int(values[k]) > 0,
                         f"agent: tmpfs {k} must be a non-root numeric id")
        if "mode" in values:
            _require(re.fullmatch(r"[0-7]{3,4}", values["mode"]) is not None, "agent: tmpfs mode must be octal")
        _require(not ({"exec", "noexec"} <= flags), f"agent: tmpfs {path} has both exec and noexec")
        if path == "/testbed":
            _require("noexec" not in flags and (not creating or "exec" in flags),
                     "agent: /testbed tmpfs must be explicitly exec (compiled extensions and test tools run from it)")
        else:
            _require("exec" not in flags, f"agent: tmpfs {path} must not be exec")
        if creating:
            _require({"nosuid", "nodev"} <= flags, f"agent: tmpfs {path} must set nosuid and nodev")
    _require(total <= TMPFS_MAX_TOTAL_BYTES, "agent: total tmpfs size exceeds the cap (it counts against memory)")


@dataclass(frozen=True)
class AgentContext:
    """Trusted values the agent policy checks the request against."""

    gateway_url: str  # the only ANTHROPIC_BASE_URL the agent may be given
    agent_network: str  # the internal network the agent must be attached to
    pinned_model: str  # every *_MODEL env var must equal this
    forbidden_secrets: tuple[str, ...] = field(default_factory=tuple)  # never allowed anywhere
    image_id: str | None = None  # if set, the created container must come from exactly this image ID


def _reject_unknown(kwargs: Mapping[str, Any], allowed: frozenset[str], role: str) -> None:
    unknown = sorted(set(kwargs) - allowed)
    if unknown:
        raise PolicyViolation(f"{role}: unexpected container argument(s): {unknown}")


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise PolicyViolation(msg)


def _harden_evaluator(kwargs: Mapping[str, Any]) -> dict:
    _reject_unknown(kwargs, _EVAL_ALLOWED_IN, "evaluator")
    out = dict(kwargs)
    _require(out.get("detach") is True, "evaluator: detach must be True")
    _require(out.get("user") == "root", "evaluator: user must be 'root' (swebench stock)")
    _require(out.get("command") == EVAL_COMMAND, f"evaluator: command must be {EVAL_COMMAND!r}")
    _require(
        isinstance(out.get("name"), str) and out["name"].startswith(EVAL_NAME_PREFIX),
        f"evaluator: name must start with {EVAL_NAME_PREFIX!r}",
    )
    _require(isinstance(out.get("image"), str) and out["image"], "evaluator: image required")
    caps = out.pop("cap_add", None)
    # The stock harness asks for exactly SYS_ADMIN; we strip it. Anything else is refused.
    _require(caps in (None, [], ["SYS_ADMIN"]), f"evaluator: unexpected capabilities {caps!r}")
    out.update(
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        mem_limit=EVAL_MEM,
        memswap_limit=EVAL_MEM,
        pids_limit=EVAL_PIDS,
        nano_cpus=EVAL_NANO_CPUS,
        network_mode="none",
        environment=dict(EVAL_ENV),
        labels={"aiep.role": "evaluator"},
    )
    return out


def _harden_agent(kwargs: Mapping[str, Any], ctx: AgentContext) -> dict:
    _reject_unknown(kwargs, _AGENT_ALLOWED_IN, "agent")
    out = dict(kwargs)
    _require(out.get("detach") is True, "agent: detach must be True")
    user = str(out.get("user", ""))
    _require(user not in ("", "root", "0") and not user.startswith("0:"), "agent: must run as non-root")
    _require(out.get("network") == ctx.agent_network, "agent: must be on the agent network only")
    _require(out.get("read_only") is True, "agent: root filesystem must be read-only")
    _require(isinstance(out.get("image"), str) and out["image"], "agent: image required")
    caps = out.get("cap_drop")
    _require(caps in (None, ["ALL"]), "agent: cap_drop must be ['ALL']")
    sec = out.get("security_opt")
    _require(sec in (None, ["no-new-privileges"]), "agent: security_opt must be no-new-privileges")
    validate_tmpfs(out.get("tmpfs") or {}, creating=True)
    env = dict(out.get("environment") or {})
    _check_agent_env(env, ctx)
    out["environment"] = env
    out.update(
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        mem_limit=AGENT_MEM,
        memswap_limit=AGENT_MEM,
        pids_limit=AGENT_PIDS,
        nano_cpus=AGENT_NANO_CPUS,
        labels={**(out.get("labels") or {}), "aiep.role": "agent"},
    )
    _check_no_secrets(out, ctx)
    return out


def _check_agent_env(env: Mapping[str, Any], ctx: AgentContext) -> None:
    for key, value in env.items():
        _require(key in AGENT_ENV_KEYS, f"agent: environment variable {key!r} is not allowed")
        _require(isinstance(value, str), f"agent: environment {key} must be a string")
        _require(not _SECRET_LIKE.search(value), f"agent: environment {key} looks like a real API key")
    _require("ANTHROPIC_API_KEY" not in env, "agent: ANTHROPIC_API_KEY must never be set")
    _require(env.get("ANTHROPIC_BASE_URL") == ctx.gateway_url, "agent: ANTHROPIC_BASE_URL must be the gateway")
    _require(bool(env.get("ANTHROPIC_AUTH_TOKEN")), "agent: per-run ANTHROPIC_AUTH_TOKEN required")
    for key in _MODEL_ENV_KEYS & set(env):
        _require(env[key] == ctx.pinned_model, f"agent: {key} must equal the pinned model")


def _check_no_secrets(obj: Any, ctx: AgentContext) -> None:
    blob = repr(obj)
    for secret in ctx.forbidden_secrets:
        _require(not secret or secret not in blob, "agent: a forbidden secret appears in the container request")


def harden(role: str, kwargs: Mapping[str, Any], ctx: AgentContext | None = None) -> dict:
    """Return policy-conformant create kwargs for ``role`` or raise PolicyViolation."""
    if role == "evaluator":
        return _harden_evaluator(kwargs)
    if role == "agent":
        _require(ctx is not None, "agent: an AgentContext is required")
        return _harden_agent(kwargs, ctx)
    raise PolicyViolation(f"unknown container role {role!r}")


def verify_inspect(role: str, inspect: Mapping[str, Any], ctx: AgentContext | None = None) -> None:
    """Check a created container (``docker inspect`` JSON) against the role's policy."""
    host = inspect.get("HostConfig") or {}
    cfg = inspect.get("Config") or {}
    _require(host.get("Privileged") in (False, None), f"{role}: container is privileged")
    _require(not host.get("CapAdd"), f"{role}: container has added capabilities")
    _require("ALL" in (host.get("CapDrop") or []), f"{role}: CapDrop must include ALL")
    _require(
        "no-new-privileges" in " ".join(host.get("SecurityOpt") or []),
        f"{role}: no-new-privileges is not set",
    )
    _require(not host.get("Binds"), f"{role}: container has bind mounts")
    _require(not host.get("Devices"), f"{role}: container has devices")
    _require(not host.get("PidMode"), f"{role}: container shares a PID namespace")
    _require(not host.get("IpcMode") in ("host",), f"{role}: container shares the host IPC namespace")
    _require(not host.get("UsernsMode") in ("host",), f"{role}: container shares the host user namespace")
    _require(not host.get("PortBindings"), f"{role}: container publishes ports")
    _require(not host.get("ExtraHosts"), f"{role}: container has extra hosts")
    _require(not host.get("Dns"), f"{role}: container overrides DNS")
    mounts = inspect.get("Mounts") or []
    _require(not mounts, f"{role}: container has mounts")
    if role == "evaluator":
        _require(host.get("NetworkMode") == "none", "evaluator: network must be 'none'")
        _require(host.get("Memory") == EVAL_MEM_BYTES, "evaluator: memory limit not applied")
        _require(host.get("PidsLimit") == EVAL_PIDS, "evaluator: pids limit not applied")
        _require(host.get("NanoCpus") == EVAL_NANO_CPUS, "evaluator: cpu limit not applied")
    elif role == "agent":
        _require(ctx is not None, "agent: an AgentContext is required")
        _require(host.get("NetworkMode") == ctx.agent_network, "agent: wrong network")
        _require(host.get("ReadonlyRootfs") is True, "agent: root filesystem is writable")
        _require(host.get("Memory") == AGENT_MEM_BYTES, "agent: memory limit not applied")
        _require(host.get("PidsLimit") == AGENT_PIDS, "agent: pids limit not applied")
        _require(host.get("NanoCpus") == AGENT_NANO_CPUS, "agent: cpu limit not applied")
        user = str(cfg.get("User", ""))
        _require(user not in ("", "root", "0") and not user.startswith("0:"), "agent: runs as root")
        validate_tmpfs(host.get("Tmpfs") or {}, creating=False)
        if ctx.image_id is not None:
            _require(inspect.get("Image") == ctx.image_id, "agent: container is not from the expected image ID")
        _check_no_secrets(inspect, ctx)
    else:
        raise PolicyViolation(f"unknown container role {role!r}")

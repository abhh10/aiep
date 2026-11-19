"""Fail-closed container policy for every container AIEP creates.

One module decides what a container may look like, per role. Callers pass the
kwargs they want; ``harden`` returns the kwargs to actually use, or raises
``PolicyViolation``. Anything not explicitly allowed is an error (allowlist, not
denylist). ``verify_inspect`` re-checks the *created* container against the same
policy, so a harness change that bypasses the kwargs path is still caught.

Roles: ``evaluator``, ``agent`` and ``gateway``. Container *creation* and every later
``docker exec`` into an agent or gateway container are policy-checked: ``harden_exec``
allows only a short list of absolute-path binaries with fixed argument shapes, so the
orchestrator can never be tricked into running an arbitrary command in a container.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .patch import HARDENED_GIT

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
        "PATH",
        "CONDA_PREFIX",
        "CONDA_DEFAULT_ENV",
    }
)
_MODEL_ENV_KEYS = frozenset(k for k in AGENT_ENV_KEYS if "MODEL" in k)
# PID 1 of every agent container: do nothing, forever. The orchestrator drives everything else
# through ``docker exec`` (inject, run Claude, quiesce, extract), so no control script has to be
# trusted inside the container and the container outlives Claude until the orchestrator is done.
AGENT_KEEPALIVE_COMMAND = ["/bin/sleep", "2147483647"]
AGENT_WORKDIR = "/testbed"
# tmpfs mounts the agent may have, with whether each must be executable. Their sizes count against
# the container's memory limit, so they are capped individually and in total. The caps are
# PLACEHOLDERS until Claude Code's size and memory are measured (Phase 2).
AGENT_FIXED_TMPFS_RULES = {"/tmp": False, "/testbed": True, "/opt/aiep": True}  # the HOME tmpfs is per identity
TMPFS_MAX_EACH_BYTES = 512 * 1024**2
TMPFS_MAX_TOTAL_BYTES = 1 * GIB

# Gateway (holds the real API key; reached by the agent over the internal network).
GATEWAY_MEM = "256m"
GATEWAY_MEM_BYTES = 256 * 1024**2
GATEWAY_PIDS = 128
GATEWAY_NANO_CPUS = 1_000_000_000
GATEWAY_ENV_KEYS = frozenset({
    "AIEP_UPSTREAM_API_KEY", "AIEP_RUN_TOKEN", "AIEP_BUDGET_USD", "AIEP_ALLOWED_HOSTS", "AIEP_LISTEN",
    "AIEP_LEDGER_PATH", "AIEP_STOP_FILE", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "TZ", "HOME",
})
GATEWAY_TMPFS_RULES = {"/opt/aiep-gw": False, "/tmp": False}
# The gateway container's entrypoint and command are fixed and validated, so whatever ENTRYPOINT the base
# image declares is replaced and nothing of the image's own runs: PID 1 is inert (like the agent's).
GATEWAY_ENTRYPOINT = ["/bin/sleep"]
GATEWAY_KEEPALIVE_ARGS = ["2147483647"]
GATEWAY_COMMAND_TAIL = ["-m", "aiep.gateway"]  # run by exec, after verification, as [GatewayContext.python, *tail]

# docker exec policy
MAX_EXEC_ARG_LEN = 512
EXEC_WORKDIRS = frozenset({"/testbed", "/tmp", "/"})
EXEC_ENV_KEYS = frozenset({"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT",
                           "GIT_OPTIONAL_LOCKS", "HOME", "PATH", "LC_ALL", "TZ", "CONDA_PREFIX", "CONDA_DEFAULT_ENV"})
# Readiness probe for the gateway: a TCP connect to its own loopback listener, run by the gateway's interpreter.
READY_SCRIPT = (
    "import socket, sys\n"
    "h, _, p = sys.argv[1].rpartition(':')\n"
    "s = socket.create_connection((h, int(p)), 2)\n"
    "s.close()\n"
)
_READY_ARG_RE = re.compile(r"^127\.0\.0\.1:[0-9]{1,5}$")
TAR_DESTS = frozenset({"/testbed", "/opt/aiep", "/opt/aiep-gw", "/tmp/aiep"})
VERIFY_ROOTS = frozenset({"/testbed", "/opt/aiep", "/opt/aiep-gw"})
# Interpreters must live on the read-only root, never on a tmpfs the container's user can rewrite.
_WRITABLE_PREFIXES = ("/testbed", "/opt/aiep", "/opt/aiep-gw", "/tmp", "/home", "/proc", "/dev", "/sys")
_INTERPRETER_RE = re.compile(r"^/[A-Za-z0-9._+\-]+(/[A-Za-z0-9._+\-]+)*$")
CLAUDE_PATH = "/opt/aiep/claude"
GIT_PATH = "/usr/bin/git"
GIT_SUBCOMMANDS = frozenset({"add", "diff", "rev-parse", "cat-file"})
_GIT_FORBIDDEN_ARGS = ("--output", "--ext-diff", "--textconv", "--exec-path", "--git-dir", "--work-tree",
                       "--upload-pack", "--receive-pack", "-c", "-C", "-O")
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


def validate_tmpfs(mapping: Mapping[str, Any], *, creating: bool,
                   rules: Mapping[str, bool] | None = None) -> None:
    """Allowlist tmpfs mounts. ``rules`` maps each allowed path to "must be exec" (agent rules by default).

    ``creating`` adds the hardening flags we demand of new mounts.
    """
    rules = AGENT_FIXED_TMPFS_RULES if rules is None else rules
    _require(isinstance(mapping, Mapping), "agent: tmpfs must be a mapping of path to options")
    total = 0
    for path, opts in mapping.items():
        _require(path in rules, f"agent: tmpfs path {path!r} is not allowed")
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
        if rules[path]:
            _require("noexec" not in flags and (not creating or "exec" in flags),
                     f"agent: {path} tmpfs must be explicitly exec (programs run from it)")
        else:
            _require("exec" not in flags, f"agent: tmpfs {path} must not be exec")
        if creating:
            _require({"nosuid", "nodev"} <= flags, f"agent: tmpfs {path} must set nosuid and nodev")
    _require(total <= TMPFS_MAX_TOTAL_BYTES, "agent: total tmpfs size exceeds the cap (it counts against memory)")


_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")  # characters only; no naming convention is assumed
_HOME_RE = re.compile(r"^/[A-Za-z0-9._+\-]+(/[A-Za-z0-9._+\-]+)*$")
# A HOME tmpfs is mounted over the account's home, so it must not hide, be hidden by, or alias anything the run relies on.
# Exact matches are refused for the system directories; the mounts and special filesystems are refused as ancestors,
# descendants or equals.
_HOME_SYSTEM_DIRS = frozenset({"/", "/home", "/root", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64",
                               "/var", "/run", "/boot", "/opt", "/mnt", "/media", "/srv", "/opt/miniconda3"})
_HOME_SPECIAL = ("/proc", "/sys", "/dev")
_HOME_MOUNTS = ("/testbed", "/opt/aiep", "/opt/aiep-gw", "/tmp", "/opt/miniconda3")


def validate_home_path(path: Any) -> str:
    """An account's home must be an explicit, absolute, normalised, plain path that overlaps nothing protected.

    No ``/home/<name>`` convention is assumed: the real value comes from the verified image. What is refused is
    anything that would shadow or alias something else: the root and system directories, the special filesystems,
    and any ancestor, descendant or equal of a path this design mounts or relies on (``/testbed``, ``/opt/aiep``,
    ``/opt/aiep-gw``, ``/tmp``, ``/opt/miniconda3``).
    """
    _require(isinstance(path, str) and path != "", "identity: home must be an explicit path")
    _require(len(path) <= MAX_EXEC_ARG_LEN and _HOME_RE.fullmatch(path) is not None,
             f"identity: home {path!r} is not a plain absolute path")
    _require(all(part not in (".", "..") for part in path.split("/")[1:]), f"identity: home {path!r} must be normalised")
    _require(path not in _HOME_SYSTEM_DIRS, f"identity: home {path!r} is the root or a system directory")
    for prot in _HOME_SPECIAL:
        _require(path != prot and not path.startswith(prot + "/"), f"identity: home {path!r} is on a special filesystem")
    for mount in _HOME_MOUNTS:
        overlaps = path == mount or path.startswith(mount + "/") or mount.startswith(path + "/")
        _require(not overlaps, f"identity: home {path!r} overlaps {mount}, which this design mounts or relies on")
    return path


@dataclass(frozen=True)
class AgentIdentity:
    """The agent's one non-root identity: an account that exists in the image, its uid/gid and its HOME.

    Nothing here has a default and nothing is guessed. The values come from ``config/agent_identity.json``
    once they have been verified against the real image, and every run re-checks them inside the container
    (``agent_env.IDENTITY_SCRIPT``) before anything is injected. The container user, the exec user, the owner
    of every tmpfs (HOME included) and ``$HOME`` are all derived from this one object, so they cannot disagree.
    """

    name: str
    uid: int
    gid: int
    home: str

    def __post_init__(self) -> None:
        _require(isinstance(self.name, str) and _ACCOUNT_RE.fullmatch(self.name) is not None and self.name != "root",
                 "identity: the account name must be a plain non-root login name")
        _require(type(self.uid) is int and type(self.gid) is int and self.uid > 0 and self.gid > 0,
                 "identity: uid and gid must be positive integers (never root)")
        validate_home_path(self.home)

    @property
    def user(self) -> str:
        """What Docker is given as ``user``: the numeric pair, so the verified uid:gid is exactly what runs."""
        return f"{self.uid}:{self.gid}"


def agent_tmpfs_rules(identity: AgentIdentity) -> dict[str, bool]:
    return {**AGENT_FIXED_TMPFS_RULES, identity.home: False}


@dataclass(frozen=True)
class AgentContext:
    """Trusted values the agent policy checks the request against."""

    gateway_url: str  # the only ANTHROPIC_BASE_URL the agent may be given
    agent_network: str  # the internal network the agent must be attached to
    pinned_model: str  # every *_MODEL env var must equal this
    identity: AgentIdentity  # required: there is no default identity
    forbidden_secrets: tuple[str, ...] = field(default_factory=tuple)  # never allowed anywhere
    image_id: str | None = None  # if set, the created container must come from exactly this image ID

    def __post_init__(self) -> None:
        _require(isinstance(self.identity, AgentIdentity), "agent: an AgentIdentity is required")


def validate_interpreter_path(path: Any, what: str = "interpreter") -> str:
    """An interpreter must be configured explicitly: an absolute, normalised path on the read-only root.

    There is no default and no search: ``python3`` is never looked up on ``PATH`` and one interpreter is
    never substituted for another, because which Python an image carries differs between images.
    """
    _require(isinstance(path, str) and path != "", f"{what}: an explicit interpreter path is required")
    _require(len(path) <= MAX_EXEC_ARG_LEN and _INTERPRETER_RE.fullmatch(path) is not None,
             f"{what}: {path!r} is not a plain absolute path")
    _require(all(part not in (".", "..") for part in path.split("/")[1:]), f"{what}: {path!r} must be normalised")
    for prefix in _WRITABLE_PREFIXES:
        _require(path != prefix and not path.startswith(prefix + "/"),
                 f"{what}: {path!r} is on a writable or special filesystem, so it could be replaced by the agent")
    return path


def _require_owned_tmpfs(mapping: Mapping[str, Any], uid: int, gid: int, role: str) -> None:
    """Every tmpfs must be created owned by exactly ``uid:gid`` and private to its owner (mode 07x0 or tighter)."""
    for path, opts in mapping.items():
        values = dict(tok.split("=", 1) for tok in str(opts).split(",") if "=" in tok)
        _require(values.get("uid") == str(uid) and values.get("gid") == str(gid),
                 f"{role}: tmpfs {path} must be owned by {uid}:{gid} (uid= and gid= options)")
        mode = values.get("mode", "")
        _require(re.fullmatch(r"[0-7]{3,4}", mode) is not None, f"{role}: tmpfs {path} needs an explicit octal mode")
        bits = int(mode, 8)
        _require(bits & 0o700 == 0o700, f"{role}: tmpfs {path} mode {mode} must give its owner rwx")
        _require(bits & 0o022 == 0, f"{role}: tmpfs {path} mode {mode} must not be group- or world-writable")


@dataclass(frozen=True)
class GatewayContext:
    """Trusted values the gateway policy checks the request against."""

    agent_network: str  # internal network (agents live here)
    egress_network: str  # the network that can reach the API; attached after create
    image_id: str  # pinned image ID of the gateway's base image
    python: str  # interpreter inside the base image: explicit, never defaulted (UNVERIFIED until Docker conformance)
    uid: int  # the ONE identity that owns the gateway's tmpfs, extracts, verifies and runs its code: explicit,
    gid: int  # never defaulted, never root, never a bool; comes from config/gateway_image.json once verified
    extra_tmpfs: tuple[str, ...] = ()  # paths declared as image VOLUMEs, neutralised with a tmpfs
    real_key: str = field(default="", repr=False)  # may appear ONLY in AIEP_UPSTREAM_API_KEY

    def __post_init__(self) -> None:
        validate_interpreter_path(self.python, "gateway")
        _require(type(self.uid) is int and type(self.gid) is int and self.uid > 0 and self.gid > 0,
                 "gateway: uid and gid must be positive integers (never root)")

    @property
    def user(self) -> str:
        return f"{self.uid}:{self.gid}"


@dataclass(frozen=True)
class ExecContext:
    """What ``docker exec`` into a container may do."""

    user: str
    allowed_scripts: frozenset[str] = frozenset()  # exact payloads allowed after `sh -c` / `python3 -I -c`
    readable_paths: frozenset[str] = frozenset()  # exact files `cat` may read
    forbidden_secrets: tuple[str, ...] = field(default_factory=tuple)
    python: str = ""  # the ONLY interpreter this context may run; "" means none (no default, no fallback)
    env: Mapping[str, str] = field(default_factory=dict)  # the environment of every exec; {} means the generic default
    may_start_gateway: bool = False  # only a gateway context may run ``python -m aiep.gateway``

    def __post_init__(self) -> None:
        if self.python:
            validate_interpreter_path(self.python, "exec")


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
    _require(out.get("user") == ctx.identity.user,
             f"agent: user must be exactly {ctx.identity.user!r} (the verified identity's uid:gid)")
    _require(out.get("network") == ctx.agent_network, "agent: must be on the agent network only")
    _require(out.get("command") == AGENT_KEEPALIVE_COMMAND,
             f"agent: command must be the keepalive {AGENT_KEEPALIVE_COMMAND!r} (work is done through exec)")
    _require(out.get("working_dir", AGENT_WORKDIR) == AGENT_WORKDIR, f"agent: working_dir must be {AGENT_WORKDIR}")
    _require(out.get("read_only") is True, "agent: root filesystem must be read-only")
    _require(isinstance(out.get("image"), str) and out["image"], "agent: image required")
    caps = out.get("cap_drop")
    _require(caps in (None, ["ALL"]), "agent: cap_drop must be ['ALL']")
    sec = out.get("security_opt")
    _require(sec in (None, ["no-new-privileges"]), "agent: security_opt must be no-new-privileges")
    rules = agent_tmpfs_rules(ctx.identity)
    validate_tmpfs(out.get("tmpfs") or {}, creating=True, rules=rules)
    _require(set(out.get("tmpfs") or {}) == set(rules), f"agent: tmpfs must be exactly {sorted(rules)}")
    _require_owned_tmpfs(out.get("tmpfs") or {}, ctx.identity.uid, ctx.identity.gid, "agent")
    env = dict(out.get("environment") or {})
    _require(env.get("HOME") == ctx.identity.home, f"agent: HOME must be the identity's home {ctx.identity.home!r}")
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


_GATEWAY_ALLOWED_IN = frozenset({
    "image", "command", "entrypoint", "name", "user", "detach", "environment", "network", "labels", "read_only",
    "tmpfs", "cap_drop", "security_opt",
})


def _gateway_rules(ctx: GatewayContext) -> dict[str, bool]:
    return {**GATEWAY_TMPFS_RULES, **{p: False for p in ctx.extra_tmpfs}}


def gateway_tmpfs(ctx: GatewayContext, *, code_size: str = "16m", tmp_size: str = "16m",
                  extra_size: str = "1m") -> dict[str, str]:
    """The gateway's tmpfs options: all owned by ``ctx.uid:ctx.gid`` and private to that identity."""
    own = f"uid={ctx.uid},gid={ctx.gid},mode=0700"
    sizes = {"/opt/aiep-gw": code_size, "/tmp": tmp_size, **{p: extra_size for p in ctx.extra_tmpfs}}
    return {path: f"rw,nosuid,nodev,size={size},{own}" for path, size in sizes.items()}


def _harden_gateway(kwargs: Mapping[str, Any], ctx: GatewayContext) -> dict:
    _reject_unknown(kwargs, _GATEWAY_ALLOWED_IN, "gateway")
    out = dict(kwargs)
    _require(out.get("detach") is True, "gateway: detach must be True")
    _require(out.get("user") == ctx.user, f"gateway: user must be exactly {ctx.user!r} (the uid:gid that owns its tmpfs)")
    _require(out.get("network") == ctx.agent_network, "gateway: created on the agent network; egress is attached afterwards")
    _require(out.get("read_only") is True, "gateway: root filesystem must be read-only")
    # PID 1 is inert: the gateway process is started by exec only after its files were verified
    _require(out.get("entrypoint") == GATEWAY_ENTRYPOINT,
             f"gateway: entrypoint must be exactly {GATEWAY_ENTRYPOINT!r} (the image's own entrypoint is never run)")
    _require(out.get("command") == GATEWAY_KEEPALIVE_ARGS,
             f"gateway: command must be exactly {GATEWAY_KEEPALIVE_ARGS!r} (the gateway starts after verification)")
    _require(isinstance(out.get("image"), str) and out["image"], "gateway: image required")
    _require(out.get("cap_drop") in (None, ["ALL"]), "gateway: cap_drop must be ['ALL']")
    _require(out.get("security_opt") in (None, ["no-new-privileges"]), "gateway: security_opt must be no-new-privileges")
    validate_tmpfs(out.get("tmpfs") or {}, creating=True, rules=_gateway_rules(ctx))
    _require_owned_tmpfs(out.get("tmpfs") or {}, ctx.uid, ctx.gid, "gateway")
    env = dict(out.get("environment") or {})
    for key, value in env.items():
        _require(key in GATEWAY_ENV_KEYS, f"gateway: environment variable {key!r} is not allowed")
        _require(isinstance(value, str), f"gateway: environment {key} must be a string")
    _require(bool(env.get("AIEP_UPSTREAM_API_KEY")) and bool(env.get("AIEP_RUN_TOKEN")),
             "gateway: AIEP_UPSTREAM_API_KEY and AIEP_RUN_TOKEN are required")
    _require(env.get("AIEP_RUN_TOKEN") != env.get("AIEP_UPSTREAM_API_KEY"), "gateway: run token must differ from the key")
    out["environment"] = env
    out.update(
        cap_drop=["ALL"], security_opt=["no-new-privileges"], mem_limit=GATEWAY_MEM, memswap_limit=GATEWAY_MEM,
        pids_limit=GATEWAY_PIDS, nano_cpus=GATEWAY_NANO_CPUS,
        labels={**(out.get("labels") or {}), "aiep.role": "gateway"},
    )
    if ctx.real_key:  # the real key may appear in exactly one place
        rest = {k: v for k, v in out.items() if k != "environment"}
        _require(ctx.real_key not in repr(rest) and all(
            ctx.real_key not in str(v) for k, v in env.items() if k != "AIEP_UPSTREAM_API_KEY"),
            "gateway: the real API key appears somewhere other than AIEP_UPSTREAM_API_KEY")
    return out


def harden(role: str, kwargs: Mapping[str, Any], ctx: Any = None) -> dict:
    """Return policy-conformant create kwargs for ``role`` or raise PolicyViolation."""
    if role == "evaluator":
        return _harden_evaluator(kwargs)
    if role == "agent":
        _require(isinstance(ctx, AgentContext), "agent: an AgentContext is required")
        return _harden_agent(kwargs, ctx)
    if role == "gateway":
        _require(isinstance(ctx, GatewayContext), "gateway: a GatewayContext is required")
        return _harden_gateway(kwargs, ctx)
    raise PolicyViolation(f"unknown container role {role!r}")


def _arg_ok(a: Any) -> bool:
    return isinstance(a, str) and "\x00" not in a and len(a) <= MAX_EXEC_ARG_LEN


def harden_exec(argv: Any, *, user: str, workdir: str, env: Mapping[str, str], ctx: ExecContext) -> dict:
    """Validate one ``docker exec`` and return the kwargs for ``exec_create``.

    Only absolute-path binaries with fixed argument shapes are allowed. In particular the issue
    text can never ride in argv: every argument is length-capped, and the only long arguments
    allowed are exact, pre-registered scripts.
    """
    _require(isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv), "exec: argv must be a non-empty list of strings")
    binary = argv[0]
    if binary == "/bin/tar":
        _require(len(argv) == 8 and argv[1:7] == ["-x", "-p", "--no-same-owner", "-f", "-", "-C"] and argv[7] in TAR_DESTS,
                 "exec: tar must be `tar -x -p --no-same-owner -f - -C <allowed destination>`")
    elif binary == "/bin/sh":
        _require(len(argv) == 3 and argv[1] == "-c" and argv[2] in ctx.allowed_scripts, "exec: sh -c only runs registered scripts")
    elif ctx.python and binary == ctx.python:  # the one explicitly configured interpreter, nothing else
        shaped = len(argv) == 5 and argv[1:3] == ["-I", "-c"] and argv[3] in ctx.allowed_scripts
        verify = shaped and argv[3] != READY_SCRIPT and argv[4] in VERIFY_ROOTS
        ready = (shaped and argv[3] == READY_SCRIPT and ctx.may_start_gateway
                 and _READY_ARG_RE.fullmatch(argv[4]) is not None and 0 < int(argv[4].rpartition(":")[2]) < 65536)
        start = ctx.may_start_gateway and argv[1:] == GATEWAY_COMMAND_TAIL
        _require(verify or ready or start, "exec: the interpreter only runs a registered script against an allowed"
                                           " root (or, for the gateway context, `-m aiep.gateway` and the readiness probe)")
    elif binary == "/bin/cat":
        _require(len(argv) == 2 and argv[1] in ctx.readable_paths, "exec: cat may only read registered files")
    elif binary == GIT_PATH:
        n = len(HARDENED_GIT)
        _require(argv[1:1 + n] == HARDENED_GIT and len(argv) > n + 1 and argv[n + 1] in GIT_SUBCOMMANDS,
                 "exec: git must start with the hardened flags and use an allowed subcommand")
        _require(all(_arg_ok(a) and not any(a == f or a.startswith(f + "=") for f in _GIT_FORBIDDEN_ARGS) for a in argv[n + 2:]),
                 "exec: git argument is not allowed")
    elif binary == CLAUDE_PATH:
        _require(all(_arg_ok(a) for a in argv[1:]), "exec: claude arguments must be short (the task is delivered on stdin, never in argv)")
    else:
        raise PolicyViolation(f"exec: binary {binary!r} is not allowed")
    _require(user == ctx.user and user.split(":")[0] not in ("", "root", "0"),
             "exec: must run as the container's non-root user")
    _require(workdir in EXEC_WORKDIRS, f"exec: workdir {workdir!r} is not allowed")
    _require(isinstance(env, Mapping), "exec: environment must be a mapping")
    for k, v in env.items():
        _require(k in EXEC_ENV_KEYS and isinstance(v, str) and len(v) <= 1024, f"exec: environment variable {k!r} is not allowed")
    # raw text (so multi-line secrets such as the task are caught) as well as the repr form
    raw = "\x00".join(argv) + "\x00" + "\x00".join(f"{k}={v}" for k, v in env.items())
    blob = raw + repr((argv, dict(env)))
    for secret in ctx.forbidden_secrets:
        _require(not secret or secret not in blob, "exec: a forbidden secret appears in the exec request")
    return {"cmd": list(argv), "user": user, "workdir": workdir, "environment": dict(env), "tty": False}


def verify_inspect(role: str, inspect: Mapping[str, Any], ctx: Any = None) -> None:
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
    elif role == "gateway":
        _require(isinstance(ctx, GatewayContext), "gateway: a GatewayContext is required")
        _require(host.get("ReadonlyRootfs") is True, "gateway: root filesystem is writable")
        _require(host.get("Memory") == GATEWAY_MEM_BYTES, "gateway: memory limit not applied")
        _require(host.get("PidsLimit") == GATEWAY_PIDS, "gateway: pids limit not applied")
        _require(host.get("NanoCpus") == GATEWAY_NANO_CPUS, "gateway: cpu limit not applied")
        _require(str(cfg.get("User", "")) == ctx.user, f"gateway: must run as {ctx.user!r}")
        _require(cfg.get("Entrypoint") == GATEWAY_ENTRYPOINT and cfg.get("Cmd") == GATEWAY_KEEPALIVE_ARGS,
                 "gateway: entrypoint/command are not the fixed keepalive")
        validate_tmpfs(host.get("Tmpfs") or {}, creating=False, rules=_gateway_rules(ctx))
        _require_owned_tmpfs(host.get("Tmpfs") or {}, ctx.uid, ctx.gid, "gateway")
        # exactly the two networks, once the egress network has been attached
        nets = set(((inspect.get("NetworkSettings") or {}).get("Networks") or {}).keys())
        _require(nets == {ctx.agent_network, ctx.egress_network}, f"gateway: attached networks are {sorted(nets)}")
        _require(inspect.get("Image") == ctx.image_id, "gateway: container is not from the pinned base image ID")
    elif role == "agent":
        _require(isinstance(ctx, AgentContext), "agent: an AgentContext is required")
        _require(host.get("NetworkMode") == ctx.agent_network, "agent: wrong network")
        _require(host.get("ReadonlyRootfs") is True, "agent: root filesystem is writable")
        _require(host.get("Memory") == AGENT_MEM_BYTES, "agent: memory limit not applied")
        _require(host.get("PidsLimit") == AGENT_PIDS, "agent: pids limit not applied")
        _require(host.get("NanoCpus") == AGENT_NANO_CPUS, "agent: cpu limit not applied")
        _require(str(cfg.get("User", "")) == ctx.identity.user, f"agent: must run as {ctx.identity.user!r}")
        rules = agent_tmpfs_rules(ctx.identity)
        validate_tmpfs(host.get("Tmpfs") or {}, creating=False, rules=rules)
        _require(set(host.get("Tmpfs") or {}) == set(rules), f"agent: tmpfs must be exactly {sorted(rules)}")
        _require_owned_tmpfs(host.get("Tmpfs") or {}, ctx.identity.uid, ctx.identity.gid, "agent")
        if ctx.image_id is not None:
            _require(inspect.get("Image") == ctx.image_id, "agent: container is not from the expected image ID")
        nets = set(((inspect.get("NetworkSettings") or {}).get("Networks") or {}).keys() or {ctx.agent_network})
        _require(nets == {ctx.agent_network}, f"agent: attached networks are {sorted(nets)}")
        _check_no_secrets(inspect, ctx)
    else:
        raise PolicyViolation(f"unknown container role {role!r}")

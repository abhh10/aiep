"""Turn an agent's working tree into a validated text patch and a predictions file.

Only a *text patch* crosses out of the agent environment. Two layers:

1. ``extract_patch`` runs a fixed git sequence through an injected ``run_git``. In
   production (Phase 2) that executor runs *inside a container*, never on the host:
   the repository is agent-controlled, and git can execute programs named in a
   repo's config or attributes (hooks, fsmonitor, textconv, external diff, clean
   filters). The hardened flags below switch off the ones that can be switched off
   from the command line; clean filters cannot be, which is why the host must not
   run git on an untrusted repository.
2. ``validate_patch`` inspects the resulting text and refuses anything but a plain
   text diff of regular files with safe, repository-relative paths.

Diffs are taken against a fixed ``base_commit`` (not ``HEAD``) so an agent that
commits or resets cannot hide or move its changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

MAX_PATCH_BYTES = 1024 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")
_INSTANCE_RE = re.compile(r"^[A-Za-z0-9_.\-]+__[A-Za-z0-9_.\-]+-\d+$")
_ALLOWED_MODES = {"100644", "100755"}

# Switch off every command-line-controllable way git runs repo-supplied programs.
HARDENED_GIT = [
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.pager=cat",
    "-c", "core.fileMode=false",
    "-c", "core.quotepath=false",
    "-c", "diff.external=",
    "-c", "diff.noprefix=false",
    "-c", "diff.mnemonicPrefix=false",
]

# Generated files that are never part of a fix. Without this, running the tests in the workspace could
# put binary .pyc files into the patch (and get it rejected) in repositories that do not ignore them.
EXCLUDE_PATHSPECS = [
    ":(exclude,glob)**/__pycache__/**",
    ":(exclude,glob)**/*.pyc",
    ":(exclude,glob)**/.pytest_cache/**",
]

GitRunner = Callable[[list[str]], bytes]


@dataclass
class PatchResult:
    status: str  # "ok" | "empty" | "rejected"
    patch: str | None = None
    reasons: list[str] = field(default_factory=list)
    sha256: str | None = None
    size: int = 0


def extract_patch(run_git: GitRunner, base_commit: str, max_bytes: int = MAX_PATCH_BYTES) -> PatchResult:
    """Capture everything changed since ``base_commit`` (including new files), then validate it.

    ``base_commit`` must be the **seed's own HEAD**, the synthetic "SWE-bench" commit that the
    instance image carries (see docs/M1_RESULTS.md section 8.6), not the dataset's ``base_commit``.
    The seed HEAD is recorded when the seed is built (``seed.py``) and is what the working tree
    was checked out at, so the diff contains exactly the agent's changes.
    """
    if not _SHA_RE.match(base_commit):
        raise ValueError("base_commit must be a 40-character lowercase hex commit")
    run_git([*HARDENED_GIT, "add", "-A", "-N", "--", ".", *EXCLUDE_PATHSPECS])  # intent-to-add: new files appear
    raw = run_git([
        *HARDENED_GIT, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
        "--src-prefix=a/", "--dst-prefix=b/", base_commit, "--", ".", *EXCLUDE_PATHSPECS,
    ])
    return validate_patch(raw, max_bytes)


def validate_patch(raw: bytes, max_bytes: int = MAX_PATCH_BYTES) -> PatchResult:
    if not raw.strip():
        return PatchResult(status="empty")
    if len(raw) > max_bytes:
        return PatchResult(status="rejected", reasons=[f"patch is {len(raw)} bytes, limit {max_bytes}"], size=len(raw))
    reasons: list[str] = []
    if b"\x00" in raw:
        reasons.append("patch contains NUL bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return PatchResult(status="rejected", reasons=[*reasons, "patch is not valid UTF-8 text"], size=len(raw))
    for line in text.splitlines():
        if line.startswith("GIT binary patch") or (line.startswith("Binary files ") and line.endswith(" differ")):
            reasons.append("binary content is not allowed")
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ", "similarity index ", "dissimilarity index ")):
            reasons.append("renames and copies are not allowed")
        elif line.startswith(("new file mode ", "new mode ", "old mode ", "deleted file mode ")):
            mode = line.rsplit(" ", 1)[1]
            if mode not in _ALLOWED_MODES:
                reasons.append(f"file mode {mode} is not allowed (symlinks and submodules are refused)")
        elif line.startswith("diff --git "):
            reason = _check_header(line)
            if reason:
                reasons.append(reason)
        elif line.startswith("--- ") or line.startswith("+++ "):
            reason = _check_path_line(line)
            if reason:
                reasons.append(reason)
    if not text.startswith("diff --git "):
        reasons.append("patch does not start with a git diff header")
    reasons = list(dict.fromkeys(reasons))
    digest = hashlib.sha256(raw).hexdigest()
    if reasons:
        return PatchResult(status="rejected", reasons=reasons, sha256=digest, size=len(raw))
    return PatchResult(status="ok", patch=text, sha256=digest, size=len(raw))


def _safe_relative(path: str) -> str | None:
    if not path or path.startswith(("/", '"')) or "\\" in path or "\x00" in path:
        return "unsafe or quoted path in diff"
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return f"unsafe path component in {path!r}"
    if parts[0] == ".git" or ".git" in parts:
        return f"path inside .git is not allowed: {path!r}"
    return None


def _check_header(line: str) -> str | None:
    rest = line[len("diff --git "):]
    n = len(rest)
    if n < 5 or (n - 5) % 2 != 0:
        return "diff header paths differ or are malformed (renames are not allowed)"
    p = rest[2:2 + (n - 5) // 2]
    if rest != f"a/{p} b/{p}":
        return "diff header paths differ or are malformed (renames are not allowed)"
    return _safe_relative(p)


def _check_path_line(line: str) -> str | None:
    target = line[4:].split("\t", 1)[0]
    if target == "/dev/null":
        return None
    if not (target.startswith("a/") or target.startswith("b/")):
        return "---/+++ path lacks the a/ b/ prefix"
    return _safe_relative(target[2:])


def write_predictions(path: Path, instance_id: str, model_name: str, result: PatchResult) -> Path:
    """Write a one-line ``predictions.jsonl`` for the evaluator. Never overwrites, never writes a bad patch."""
    if result.status != "ok" or not result.patch:
        raise ValueError(f"cannot write predictions for a patch with status {result.status!r}")
    if not _INSTANCE_RE.match(instance_id):
        raise ValueError("instance_id has an unexpected format")
    if not _MODEL_NAME_RE.match(model_name):
        raise ValueError("model_name must match [A-Za-z0-9_.-]{1,100}")
    with open(path, "x") as f:
        f.write(json.dumps({"instance_id": instance_id, "model_name_or_path": model_name,
                            "model_patch": result.patch}) + "\n")
    return Path(path)


# ---------------------------------------------------------------------------------------
def container_git_runner(exec_git: Callable[[list[str]], Any]) -> GitRunner:
    """Adapt an in-container exec function to a ``GitRunner``.

    ``exec_git`` takes a full argv (starting with the absolute git path) and returns an object with
    ``exit_code``, ``stdout``, ``truncated`` and ``timed_out``. This is the ONLY git runner production
    code may use: it executes inside the agent container, never on the host.
    """

    def run(args: list[str]) -> bytes:
        r = exec_git(["/usr/bin/git", *args])
        if r.timed_out or r.truncated or r.exit_code != 0:
            what = "timed out" if r.timed_out else "output exceeded the cap" if r.truncated else f"exit code {r.exit_code}"
            raise RuntimeError(f"git in the agent container failed: {what}")
        return r.stdout

    return run


def trusted_local_git_runner(repo_dir: Path, timeout: float = 60.0) -> GitRunner:
    """Run git on a LOCAL directory. For tests on repositories we created ourselves ONLY.

    Never point this at an agent's repository: clean filters and other repo-controlled
    programs can run. It refuses repositories that configure filters or config includes,
    but that is a tripwire for test mistakes, not a security boundary.
    """
    repo_dir = Path(repo_dir)
    _refuse_filters(repo_dir)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }

    def run(args: list[str]) -> bytes:
        r = subprocess.run(["git", *args], cwd=repo_dir, env=env, capture_output=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(a for a in args if not a.startswith('core.'))[:80]} failed: {r.stderr.decode(errors='replace')[:200]}")
        return r.stdout

    return run


def _refuse_filters(repo_dir: Path) -> None:
    candidates = [repo_dir / ".git" / "config", repo_dir / ".git" / "info" / "attributes"]
    candidates += [p for p in repo_dir.rglob(".gitattributes") if ".git" not in p.parts[:-1]]
    for p in candidates:
        if not p.is_file():
            continue
        text = p.read_text(errors="replace").lower()
        if "filter" in text or "[include" in text:
            raise RuntimeError(f"refusing to run git on a repository with filters/includes ({p.name})")

"""Format-aware screening of SWE-bench tasks, and the deterministic, seeded task selection used by the M3 pilot.

Why format-aware: the old M1 rule ("a PASS_TO_PASS entry without ``::`` is a junk log fragment") is right for pytest-style ids only. django ids look like
``test_name (module.Class)`` and sympy ids like ``test_name``, so that rule flags 75 of the 114 django tasks for no reason. ``test_id_format`` names each
repository's id format and ``is_well_formed`` checks an id against it, conservatively: an id that does not match its repository's format is NOT trusted
(a django docstring-style id, say, cannot be verified by shape), and the task is left out of the pilot pool rather than guessed about.

Hidden data discipline: identifiers are only compared here, never returned or printed; the functions return COUNTS and reason codes.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

PYTEST_STYLE, DJANGO_STYLE, SYMPY_STYLE = "pytest", "django", "sympy"
REPO_FORMATS = {"django/django": DJANGO_STYLE, "sympy/sympy": SYMPY_STYLE}  # every other SWE-bench Lite repository uses pytest-style ids
_DJANGO_ID = re.compile(r"\S+ \([\w.]+\)")
_SYMPY_ID = re.compile(r"test_\w+")
DEFAULT_MAX_PASS_TO_PASS = 60  # keeps the in-container test run short under emulation; a pre-registered screening parameter


def test_id_format(repo: str) -> str:
    return REPO_FORMATS.get(repo, PYTEST_STYLE)


def is_well_formed(repo: str, test_id: Any) -> bool:
    if not isinstance(test_id, str) or not test_id or test_id != test_id.strip():
        return False
    fmt = test_id_format(repo)
    if fmt == DJANGO_STYLE:
        return _DJANGO_ID.fullmatch(test_id) is not None
    if fmt == SYMPY_STYLE:
        return _SYMPY_ID.fullmatch(test_id) is not None
    head, sep, tail = test_id.partition("::")
    return bool(sep and tail and head.endswith(".py"))


def as_id_list(value: Any) -> list[str]:
    """The dataset stores these fields as JSON text or as a list."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [value]
    return [str(x) for x in value] if isinstance(value, (list, tuple)) else []


def malformed_count(repo: str, ids: Iterable[str]) -> int:
    return sum(1 for i in ids if not is_well_formed(repo, i))


def screen_record(record: Mapping[str, Any], *, max_pass_to_pass: int = DEFAULT_MAX_PASS_TO_PASS, exclude: Sequence[str] = ()) -> list[str]:
    """Reasons a task is NOT eligible for the pilot pool (empty list: eligible). Reason codes only; no identifiers."""
    reasons: list[str] = []
    instance, repo = record.get("instance_id"), str(record.get("repo", ""))
    if instance in exclude:
        reasons.append("excluded_by_rule")
    f2p, p2p = as_id_list(record.get("FAIL_TO_PASS")), as_id_list(record.get("PASS_TO_PASS"))
    if not f2p:
        reasons.append("no_fail_to_pass")
    if (n := malformed_count(repo, f2p)):
        reasons.append(f"malformed_fail_to_pass:{n}")
    if (n := malformed_count(repo, p2p)):
        reasons.append(f"malformed_pass_to_pass:{n}")
    if len(p2p) > max_pass_to_pass:
        reasons.append(f"pass_to_pass_over_{max_pass_to_pass}")
    if len(set(p2p)) != len(p2p):
        reasons.append("duplicate_pass_to_pass")
    return reasons


def image_name(instance_id: str) -> str:
    """The published SWE-bench instance image (checked against ``swebench``'s own spec for the pinned task in the tests)."""
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+__[A-Za-z0-9_.\-]+-\d+", instance_id):
        raise ValueError("instance_id has an unexpected format")
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}:latest"


def rank_key(seed: str, purpose: str, key: str) -> str:
    """Pre-registered pseudo-random rank: a SHA-256 of ``seed|purpose|key`` (lowest wins). Anyone can recompute it."""
    return hashlib.sha256(f"{seed}|{purpose}|{key}".encode()).hexdigest()


def pool_digest(instance_ids: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(instance_ids)).encode()).hexdigest()


def covariates(record: Mapping[str, Any]) -> dict[str, Any]:
    """Counts only (never ids or patch text): what a reader needs to see how comparable the chosen tasks are."""
    patch = str(record.get("patch") or "")
    return {"gold_patch_changed_lines": sum(1 for l in patch.splitlines() if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))),
            "fail_to_pass": len(as_id_list(record.get("FAIL_TO_PASS"))), "pass_to_pass": len(as_id_list(record.get("PASS_TO_PASS"))),
            "issue_chars": len(str(record.get("problem_statement") or ""))}


def select_tasks(records: Sequence[Mapping[str, Any]], *, seed: str, n: int, max_per_repo: int, min_repos: int, exclude: Sequence[str] = (),
                 max_pass_to_pass: int = DEFAULT_MAX_PASS_TO_PASS, replacements: int = 12) -> dict[str, Any]:
    """The pre-registered selection. Deterministic given (records, seed, parameters).

    1. screen every record (``screen_record``); the survivors are the POOL (its digest is recorded);
    2. rank the pool by ``rank_key(seed, 'select', instance_id)``;
    3. walk the ranking, taking a task unless its repository already has ``max_per_repo`` chosen, until ``n`` are chosen;
    4. the next ``replacements`` tasks in the same walk (without the per-repo cap, which is applied when one is used) are the REPLACEMENT ORDER.
    """
    pool = [r for r in records if not screen_record(r, max_pass_to_pass=max_pass_to_pass, exclude=exclude)]
    ranked = sorted(pool, key=lambda r: rank_key(seed, "select", r["instance_id"]))
    chosen: list[Mapping[str, Any]] = []
    per_repo: dict[str, int] = {}
    rest: list[Mapping[str, Any]] = []
    for r in ranked:
        if len(chosen) < n and per_repo.get(r["repo"], 0) < max_per_repo:
            chosen.append(r)
            per_repo[r["repo"]] = per_repo.get(r["repo"], 0) + 1
        else:
            rest.append(r)
    if len(chosen) < n or len(per_repo) < min_repos:
        raise ValueError(f"the pool cannot satisfy n={n}, max_per_repo={max_per_repo}, min_repos={min_repos}")
    return {"pool_size": len(pool), "pool_sha256": pool_digest(r["instance_id"] for r in pool), "chosen": chosen, "replacements": rest[:replacements]}

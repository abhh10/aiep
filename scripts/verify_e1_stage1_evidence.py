#!/usr/bin/env python3
"""Verify the committed E1 Stage 1 evidence bundle OFFLINE.

    python scripts/verify_e1_stage1_evidence.py [--bundle evidence/e1-stage1] [--report docs/research/E1_STAGE1_REPORT.md]

Needs only the Python standard library, this repository's own ``src/aiep`` and its tracked configuration. It needs no Docker, no credentials, no network and none of the
private run directories. Exit code 0 means every check passed.

The generic privacy scan has no embedded names; point ``AIEP_SCAN_DENYLIST`` at an uncommitted file of terms to also check for specific names.

Provenance (``PROVENANCE.json``, schema 3) is checked strictly: exact fields and types; ``builder.sha256`` against the builder blob committed at ``builder.built_on_commit``; the
ORIGINAL execution commit (``execution.harness_commit``, which belongs to the unpublished pre-release history and is not looked up in Git) against all eight run records and the report;
``execution.public_history_commit`` (its equivalent in this repository's rewritten history) for existence, ancestry of the builder commit, and agreement with the mapping committed at
``builder.built_on_commit``; the definition hashes; and every recorded source hash against the bundle. Checks that need Git history print SKIP, never PASS, when history is
unavailable (no ``git``, no repository, or a shallow clone); ``--require-git`` turns a SKIP into a failure. ``--git-root`` names the repository whose history is consulted (default: this checkout).

What it can establish: that the bundle files match their manifest; that the locked definition hashes to the recorded value; that the eight rows, the ledgers, the state and the
derived summaries agree with one another; and that the report's quantitative figures follow from the ledgers, the price table and the state. What it cannot establish: anything about
the withheld files (transcripts, patches, prompts, evaluator logs). Their hashes in the bundle only show that the bundle is internally consistent, not that those files say what the
summaries say. All dollar figures are gateway-computed, not provider billing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiep.experiment_def import definition_hash, load_experiment  # noqa: E402
from aiep.pricing import Usage, compute_cost, load_pricing  # noqa: E402
from aiep.results import uncached_equivalent_usd  # noqa: E402

DEFINITION = Path("config/experiments/e1-exploratory-1.json")
DEFINITION_SHA256 = "9601b345da705b7e8bbdba251b902f4c7637482fd2e87e732dcba0dc36875eff"
PRICING = Path("config/model_pricing.json")
MODEL = "claude-sonnet-5-5"
EXPECTED_RUNS = [  # execution order
    ("m2-sympy__sympy-18621-A-20261123T171501Z", "sympy__sympy-18621", "A"),
    ("m2-sympy__sympy-18621-B-20261123T172624Z", "sympy__sympy-18621", "B"),
    ("m2-django__django-13933-B-20261123T172904Z", "django__django-13933", "B"),
    ("m2-django__django-13933-A-20261123T174038Z", "django__django-13933", "A"),
    ("m2-django__django-15851-B-20261123T174307Z", "django__django-15851", "B"),
    ("m2-django__django-15851-A-20261123T175434Z", "django__django-15851", "A"),
    ("m2-sympy__sympy-22714-A-20261123T175656Z", "sympy__sympy-22714", "A"),
    ("m2-sympy__sympy-22714-B-20261123T180803Z", "sympy__sympy-22714", "B"),
]
TASKS = ["sympy__sympy-18621", "django__django-13933", "django__django-15851", "sympy__sympy-22714"]

# the figures the research report quotes (gateway-computed unless stated)
CLAIMS = {
    "total_cost": D("0.2337872"), "cost_A": D("0.1118429"), "cost_B": D("0.1219443"), "cost_diff": D("0.0101014"),
    "uncached_A": D("0.268796"), "uncached_B": D("0.403688"), "uncached_diff": D("0.134892"),
    "pair_cost_diff": {"sympy__sympy-18621": D("0.0068249"), "django__django-13933": D("-0.0069583"), "django__django-15851": D("-0.0099522"), "sympy__sympy-22714": D("0.0201870")},
    "pair_uncached_diff": {"sympy__sympy-18621": D("0.061316"), "django__django-13933": D("0.027824"), "django__django-15851": D("0.021232"), "sympy__sympy-22714": D("0.024520")},
    "pair_first_excluded_diff": {"sympy__sympy-18621": D("0.0063324"), "django__django-13933": D("0.0078412"), "django__django-15851": D("0.0046973"), "sympy__sympy-22714": D("0.0050425")},
    "first_excluded_sum": D("0.0239134"),
    "requests": {"total": 34, "A": 14, "B": 20},
    "tokens": {"A": {"input_tokens": 28, "output_tokens": 2637, "cache_read_tokens": 90644, "cache_write_5m_tokens": 30541},
               "B": {"input_tokens": 40, "output_tokens": 3785, "cache_read_tokens": 155493, "cache_write_5m_tokens": 27386}},
    "warm_first_requests": {"m2-django__django-13933-B-20261123T172904Z": (6205, D("0.005772")), "m2-django__django-15851-B-20261123T174307Z": (6205, D("0.0053445")),
                            "m2-sympy__sympy-22714-A-20261123T175656Z": (6205, D("0.0059295"))},
    "pair_gap_s": 600, "between_pairs_s": [72, 69, 69],
    "window_utc": ("17:13:46", "18:09:36"),
    "tests_started_runs": {"A": 0, "B": 4}, "test_attempts": 7, "tests_started": 5, "tests_not_started": 2, "failed_runs": 2, "parser_cycles": 1,
    "fix_revision_cycles": 0, "test_update_cycles": 1,
}
REPORT_STRINGS = ["0.2337872", "0.1118429", "0.1219443", "0.0101014", "0.268796", "0.403688", "0.134892", "0.0239134", "0.0068249", "0.0069583", "0.0099522", "0.0201870",
                  "0.061316", "0.027824", "0.021232", "0.024520", "0.0063324", "0.0078412", "0.0046973", "0.0050425", "246,137", "57,927", "155,493", "90,644", "30,541", "27,386",
                  "2,637", "3,785", "6,205", "0.005772", "0.0053445", "0.0059295", "72, 69 and 69", "17:13:46", "18:09:36", "333.454"]

UNSAFE = {
    "absolute_path": re.compile(r"(?<![\w.~-])/(?:Users|home|tmp|var|private|opt|root|etc|proc|mnt|run|dev|usr|testbed|workspace|Volumes)\b"),
    "home_tilde": re.compile(r"~/"),
    "ipv4": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "uuid": re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
    "credential_word": re.compile(r"sk-ant|bearer\s|x-api-key|authorization|upstream_api_key|password|secret", re.I),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "socket_or_session": re.compile(r"cc-socks|messaging_socket|session_id", re.I),
}

DENYLIST_ENV = "AIEP_SCAN_DENYLIST"
PERSONAL_CATEGORY = "personal_identifier"


def denylist_pattern(path: str | None = None) -> "re.Pattern[str] | None":
    """No personal name is hard-coded. Optionally, the file named by AIEP_SCAN_DENYLIST (one case-insensitive term per line, '#' comments allowed, kept outside the repository) lists
    terms that must not appear anywhere in the bundle. Unset: only the generic patterns above apply. Set but unreadable or malformed: an error, never a silent skip."""
    path = os.environ.get(DENYLIST_ENV, "") if path is None else path
    if not path:
        return None
    try:
        lines = Path(path).read_text().splitlines()
    except OSError as e:
        raise RuntimeError(f"{DENYLIST_ENV} is set but cannot be read ({type(e).__name__})") from e
    terms = sorted({l.strip().lower() for l in lines if l.strip() and not l.lstrip().startswith("#")})
    if any(len(t) < 3 for t in terms):
        raise RuntimeError(f"{DENYLIST_ENV}: every term needs at least 3 characters")
    if not terms:
        return None
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(t) for t in terms) + r")(?![A-Za-z0-9])", re.I)


class Checker:
    def __init__(self, require_git: bool = False) -> None:
        self.failed: list[str] = []
        self.skipped: list[str] = []
        self.require_git = require_git
        self.n = 0

    def skip(self, name: str, reason: str) -> None:
        """A check that could not run. Never counted as a pass; a failure under --require-git."""
        if self.require_git:
            self.check(name + " (--require-git)", False, reason)
            return
        print(f"  SKIP  {name}  [{reason}]")
        self.skipped.append(name)

    def check(self, name: str, cond: bool, detail: Any = "") -> bool:
        self.n += 1
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if (detail != "" and not cond) else ""))
        if not cond:
            self.failed.append(name)
        return bool(cond)


def section(title: str) -> None:
    print(f"\n{title}")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def jl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def pair_of(rows: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["task_id"], {})[r["arm"]] = r
    return out


def fix_revision_cycles(ev: dict[str, Any]) -> tuple[int, int]:
    """(implementation-fix revisions, test-expectation updates), re-derived from the recorded attempts and edits: a started-and-FAILED run, then an edit in a LATER call, then a
    started run in that call or later. Kind of edit target decides the class (source_file versus test_file). This is the report's stricter definition, not the parser's."""
    tr = ev["transcript"]
    edits = [(e["call_index"], e["target_kind"]) for e in tr["edit_tool_calls"]] + [(b["call_index"], k) for b in tr["bash_edit_like"] for k in b["target_kinds"]]
    started = [a["call_index"] for a in tr["test_attempts"] if a["state"] in ("completed_failed", "completed_passed", "started_incomplete")]
    fix = upd = 0
    for f in (a["call_index"] for a in tr["test_attempts"] if a["state"] == "completed_failed"):
        later = [(i, k) for i, k in edits if i > f and any(s >= i and s > f for s in started)]
        if any(k == "source_file" for _, k in later):
            fix += 1
        elif any(k == "test_file" for _, k in later):
            upd += 1
    return fix, upd


SHA256_HEX = re.compile(r"[0-9a-f]{64}")
COMMIT_HEX = re.compile(r"[0-9a-f]{40}")
BUILDER_REL = "scripts/build_e1_stage1_evidence.py"
HISTORY_MAP_REL = "config/history_rewrite.json"
PROVENANCE_KEYS = {"schema", "bundle", "experiment_id", "builder", "execution", "definition", "sanitization", "files"}
TREATMENTS = {"verbatim", "sanitized", "derived", "generated"}


def _exact(obj: Any, keys: set[str], where: str, out: list[str]) -> bool:
    if not isinstance(obj, dict):
        out.append(f"{where}: not an object")
        return False
    missing, extra = keys - set(obj), set(obj) - keys
    if missing:
        out.append(f"{where}: missing fields {sorted(missing)}")
    if extra:
        out.append(f"{where}: unexpected fields {sorted(extra)}")
    return not missing and not extra


def _is_int(v: Any) -> bool:
    return type(v) is int


def provenance_schema_problems(p: Any) -> list[str]:
    """Every deviation from the exact provenance schema 3 (field names, types and formats). Empty list means well formed."""
    out: list[str] = []
    if isinstance(p, dict) and p.get("schema") in (1, 2):
        out.append(f"legacy provenance schema {p['schema']}"
                   + (" (a single, ambiguous harness_head_commit)" if p["schema"] == 1 else " (no execution.public_history_commit)") + ": the bundle predates schema 3 and must be rebuilt")
    if not _exact(p, PROVENANCE_KEYS, "provenance", out) and not isinstance(p, dict):
        return out
    if not (_is_int(p.get("schema")) and p.get("schema") == 3):
        out.append("provenance.schema must be the integer 3")
    if p.get("bundle") != "e1-stage1":
        out.append("provenance.bundle must be 'e1-stage1'")
    if p.get("experiment_id") != "e1-exploratory-1":
        out.append("provenance.experiment_id must be 'e1-exploratory-1'")
    b = p.get("builder")
    if _exact(b, {"script", "sha256", "built_on_commit"}, "builder", out):
        if b["script"] != BUILDER_REL:
            out.append(f"builder.script must be {BUILDER_REL}")
        if not (isinstance(b["sha256"], str) and SHA256_HEX.fullmatch(b["sha256"])):
            out.append("builder.sha256 is not a lowercase 64-hex SHA-256")
        if not (isinstance(b["built_on_commit"], str) and COMMIT_HEX.fullmatch(b["built_on_commit"])):
            out.append("builder.built_on_commit is not a 40-hex commit id")
    e = p.get("execution")
    if _exact(e, {"harness_commit", "public_history_commit", "runs_checked", "all_clean", "source"}, "execution", out):
        if not (isinstance(e["harness_commit"], str) and COMMIT_HEX.fullmatch(e["harness_commit"])):
            out.append("execution.harness_commit is not a 40-hex commit id")
        if not (isinstance(e["public_history_commit"], str) and COMMIT_HEX.fullmatch(e["public_history_commit"])):
            out.append("execution.public_history_commit is not a 40-hex commit id")
        if not (_is_int(e["runs_checked"]) and e["runs_checked"] == len(EXPECTED_RUNS)):
            out.append(f"execution.runs_checked must be the integer {len(EXPECTED_RUNS)}")
        if e["all_clean"] is not True:
            out.append("execution.all_clean must be true")
        if not (isinstance(e["source"], str) and e["source"].strip()):
            out.append("execution.source must be a non-empty string")
    d = p.get("definition")
    if _exact(d, {"path", "definition_sha256", "file_sha256"}, "definition", out):
        if d["path"] != DEFINITION.as_posix():
            out.append(f"definition.path must be {DEFINITION.as_posix()}")
        for k in ("definition_sha256", "file_sha256"):
            if not (isinstance(d[k], str) and SHA256_HEX.fullmatch(d[k])):
                out.append(f"definition.{k} is not a lowercase 64-hex SHA-256")
    s_ = p.get("sanitization")
    if _exact(s_, {"unsafe_patterns", "hidden_data_scan", "statement"}, "sanitization", out):
        if not (isinstance(s_["unsafe_patterns"], list) and s_["unsafe_patterns"] and all(isinstance(x, str) for x in s_["unsafe_patterns"])):
            out.append("sanitization.unsafe_patterns must be a non-empty list of strings")
        h = s_["hidden_data_scan"]
        if _exact(h, {"identifiers_checked", "patch_lines_checked"}, "sanitization.hidden_data_scan", out):
            if not (_is_int(h["identifiers_checked"]) and _is_int(h["patch_lines_checked"])):
                out.append("sanitization.hidden_data_scan counts must be integers")
        if not (isinstance(s_["statement"], str) and s_["statement"].strip()):
            out.append("sanitization.statement must be a non-empty string")
    f = p.get("files")
    if not (isinstance(f, dict) and f):
        out.append("provenance.files must be a non-empty object")
    else:
        for name, entry in f.items():
            where = f"files[{name}]"
            if not isinstance(entry, dict) or not {"treatment", "sources"} <= set(entry) or set(entry) - {"treatment", "sources", "note", "dropped_fields"}:
                out.append(f"{where}: fields must be treatment and sources, plus optional note and dropped_fields")
                continue
            if entry["treatment"] not in TREATMENTS:
                out.append(f"{where}: unknown treatment")
            if not isinstance(entry["sources"], list):
                out.append(f"{where}: sources must be a list")
                continue
            for i, src in enumerate(entry["sources"]):
                if not _exact(src, {"path", "sha256", "bytes"}, f"{where}.sources[{i}]", out):
                    continue
                if not (isinstance(src["path"], str) and src["path"] and not src["path"].startswith("/") and ".." not in src["path"].split("/")):
                    out.append(f"{where}.sources[{i}].path must be a repository-relative path")
                if not (isinstance(src["sha256"], str) and SHA256_HEX.fullmatch(src["sha256"])):
                    out.append(f"{where}.sources[{i}].sha256 is not a lowercase 64-hex SHA-256")
                if not (_is_int(src["bytes"]) and src["bytes"] >= 0):
                    out.append(f"{where}.sources[{i}].bytes must be a non-negative integer")
            if "note" in entry and not isinstance(entry["note"], str):
                out.append(f"{where}.note must be a string")
            if "dropped_fields" in entry and not (isinstance(entry["dropped_fields"], list) and all(isinstance(x, str) for x in entry["dropped_fields"])):
                out.append(f"{where}.dropped_fields must be a list of strings")
    return out


def _git(root: Path, *args: str) -> "subprocess.CompletedProcess[bytes] | None":
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None


def git_availability(root: Path) -> str:
    """'full' (a repository with complete history), 'shallow', or 'unavailable' (no git binary, no repository)."""
    r = _git(root, "rev-parse", "--is-inside-work-tree")
    if r is None or r.returncode != 0 or r.stdout.strip() != b"true":
        return "unavailable"
    sh = _git(root, "rev-parse", "--is-shallow-repository")
    return "shallow" if sh is not None and sh.returncode == 0 and sh.stdout.strip() == b"true" else "full"


def commit_exists(root: Path, commit: str) -> bool:
    r = _git(root, "cat-file", "-e", f"{commit}^{{commit}}")
    return r is not None and r.returncode == 0


def check_provenance(c: Checker, bundle: Path, listed: dict[str, str], git_root: Path, report: Path) -> None:
    section("1b. Provenance (PROVENANCE.json, schema 3)")
    prov_path = bundle / "PROVENANCE.json"
    try:
        prov = json.loads(prov_path.read_text())
    except (OSError, ValueError) as e:
        c.check("PROVENANCE.json is readable JSON", False, type(e).__name__)
        return
    problems = provenance_schema_problems(prov)
    c.check("provenance has exactly the expected schema, field names, types and hash formats (no unexpected or missing fields)", not problems, problems[:4])
    if problems:
        print("  (the remaining provenance checks need a well-formed record)")
        return
    c.check("MANIFEST.sha256 lists PROVENANCE.json and its sha256 is correct", listed.get("PROVENANCE.json") == sha(prov_path.read_bytes()))
    expected_files = {n for n in listed if n not in ("PROVENANCE.json",)}
    c.check("provenance.files describes exactly the other bundle files (no extra, none missing)", set(prov["files"]) == expected_files, sorted(set(prov["files"]) ^ expected_files)[:4])
    # recorded source hashes against the bundle
    bad: list[str] = []
    for name, entry in prov["files"].items():
        data = (bundle / name).read_bytes() if (bundle / name).is_file() else None
        if data is None:
            bad.append(f"{name}: missing")
        elif entry["treatment"] == "verbatim":
            ok = len(entry["sources"]) == 1 and entry["sources"][0]["sha256"] == sha(data) and entry["sources"][0]["bytes"] == len(data)
            if not ok:
                bad.append(f"{name}: verbatim source hash differs from the bundled file")
        elif entry["treatment"] == "sanitized":
            if len(entry["sources"]) != 1 or entry.get("dropped_fields") != ["run_dir"] or any("run_dir" in json.loads(l) for l in data.decode().splitlines() if l.strip()):
                bad.append(f"{name}: sanitized-file record is inconsistent")
        elif entry["treatment"] == "generated" and entry["sources"]:
            bad.append(f"{name}: a generated file cannot have private sources")
    for rid, _, _ in EXPECTED_RUNS:
        name = f"runs/{rid}/evidence.json"
        if name not in prov["files"] or not (bundle / name).is_file():
            continue
        withheld = json.loads((bundle / name).read_text())["run"]["withheld_artifacts"]
        for src in prov["files"][name]["sources"]:
            w = withheld.get(Path(src["path"]).name)
            if w is not None and (w["sha256"] != src["sha256"] or w["size"] != src["bytes"]):
                bad.append(f"{name}: recorded source {Path(src['path']).name} differs from the hash in the run's own record")
    c.check("every recorded source hash agrees with the bundled file (verbatim, sanitized) or with the run's own recorded hash (derived)", not bad, bad[:3])
    d = prov["definition"]
    c.check("provenance.definition hashes equal the locked definition (self-hash and file hash)", d["definition_sha256"] == DEFINITION_SHA256
            and d["file_sha256"] == sha((ROOT / DEFINITION).read_bytes()), "")
    # the execution commit: from the eight run records, not from Git
    ex = prov["execution"]
    recs = [json.loads((bundle / f"runs/{rid}/evidence.json").read_text())["run"]["harness"] for rid, _, _ in EXPECTED_RUNS if (bundle / f"runs/{rid}/evidence.json").is_file()]
    c.check("execution.harness_commit equals the harness commit recorded in all eight evidence.json files, each from a clean tree",
            len(recs) == len(EXPECTED_RUNS) and all(r["commit"] == ex["harness_commit"] and r["dirty"] is False for r in recs))
    if report.is_file():
        c.check("the research report states the same execution commit", ex["harness_commit"] in report.read_text())
    else:
        c.skip("the research report states the same execution commit", f"report not found at {report}")
    # history-dependent checks: SKIP, never PASS, when history is unavailable. The ORIGINAL execution commit (harness_commit) belongs to the unpublished history and is deliberately
    # not looked up here; its published equivalent (public_history_commit) is, and it is tied to the original through the mapping committed at built_on_commit.
    b = prov["builder"]
    avail = git_availability(git_root)
    builder_name = "builder.sha256 equals the builder file committed at builder.built_on_commit"
    pub_name = "execution.public_history_commit exists in this repository's history"
    anc_name = "builder.built_on_commit descends from execution.public_history_commit"
    map_name = "the mapping committed at builder.built_on_commit ties execution.harness_commit to execution.public_history_commit"
    names = (builder_name, pub_name, anc_name, map_name)
    if avail == "unavailable":
        for n in names:
            c.skip(n, f"no usable Git history at {git_root}")
        return
    pub = ex["public_history_commit"]
    have_b, have_p = commit_exists(git_root, b["built_on_commit"]), commit_exists(git_root, pub)
    if have_b:
        r = _git(git_root, "show", f"{b['built_on_commit']}:{b['script']}")
        c.check(builder_name, r is not None and r.returncode == 0 and sha(r.stdout) == b["sha256"],
                "the builder is not committed at that commit" if (r is None or r.returncode != 0) else "the committed builder differs from builder.sha256")
    elif avail == "shallow":
        c.skip(builder_name, "built_on_commit is not present in this shallow clone")
    else:
        c.check(builder_name, False, "built_on_commit does not exist in this repository's history")
    if have_p:
        c.check(pub_name, True)
    elif avail == "shallow":
        c.skip(pub_name, "the public history commit is not present in this shallow clone")
    else:
        c.check(pub_name, False, "the public history commit does not exist in this repository's history")
    if have_b and have_p:
        r = _git(git_root, "merge-base", "--is-ancestor", pub, b["built_on_commit"])
        c.check(anc_name, r is not None and r.returncode == 0)
    else:
        c.skip(anc_name, "needs both commits in the available history")
    if have_b:
        r = _git(git_root, "show", f"{b['built_on_commit']}:{HISTORY_MAP_REL}")
        ok, why = False, "config/history_rewrite.json is not committed at builder.built_on_commit"
        if r is not None and r.returncode == 0:
            try:
                doc = json.loads(r.stdout)
                hits = [m for m in doc["mappings"] if isinstance(m, dict) and m.get("original_commit") == ex["harness_commit"]]
                ok = doc.get("schema") == 1 and len(hits) == 1 and hits[0].get("public_commit") == pub
                why = "the committed mapping has no single entry from harness_commit to public_history_commit"
            except (KeyError, TypeError, ValueError):
                why = "the committed mapping is malformed"
        c.check(map_name, ok, "" if ok else why)
    elif avail == "shallow":
        c.skip(map_name, "built_on_commit is not present in this shallow clone")
    else:
        c.check(map_name, False, "built_on_commit does not exist in this repository's history")


MANIFEST_LINE = re.compile(r"([0-9a-f]{64})  (\S.*)")


class Abort(Exception):
    """Raised after a structural failure has been reported: the dependent checks cannot run on a malformed bundle."""


def _unsafe_name(n: str) -> bool:
    return n.startswith("/") or ".." in n.split("/") or "\\" in n or n == ""


def parse_manifest(c: "Checker", path: Path) -> dict[str, str]:
    """Missing or malformed manifest lines are FAILs (not tracebacks); well-formed lines are still returned so the later integrity checks can report what is wrong."""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        c.check("MANIFEST.sha256 is readable text", False, type(e).__name__)
        raise Abort from e
    listed: dict[str, str] = {}
    bad: list[str] = []
    for n, line in enumerate(text.splitlines(), 1):
        m = MANIFEST_LINE.fullmatch(line)
        if m is None or _unsafe_name(m.group(2)) or m.group(2) in listed:
            bad.append(f"line {n}")
            continue
        listed[m.group(2)] = m.group(1)
    c.check("every MANIFEST.sha256 line is '<64 hex>  <relative path>' with no duplicate or unsafe path", not bad and bool(listed), bad[:4] or "empty manifest")
    return listed


def structural_preflight(c: "Checker", bundle: Path) -> None:
    """The files every later check reads must exist, parse and have the fields those checks index; otherwise report each problem as a FAIL and stop."""
    problems: list[str] = []

    def load(rel: str) -> Any:
        try:
            return json.loads((bundle / rel).read_text())
        except OSError:
            problems.append(f"{rel}: missing or unreadable")
        except ValueError:
            problems.append(f"{rel}: not valid JSON")
        return None

    state = load("batch/state.json")
    if state is not None and not isinstance(state, dict):
        problems.append("batch/state.json: not an object")
    try:
        for n, l in enumerate((bundle / "batch" / "results.jsonl").read_text().splitlines(), 1):
            if l.strip() and not isinstance(json.loads(l), dict):
                problems.append(f"batch/results.jsonl line {n}: not an object")
    except OSError:
        problems.append("batch/results.jsonl: missing or unreadable")
    except ValueError:
        problems.append("batch/results.jsonl: a line is not valid JSON")
    for rid, _, _ in EXPECTED_RUNS:
        rel = f"runs/{rid}/evidence.json"
        ev = load(rel)
        if ev is None:
            continue
        run = ev.get("run") if isinstance(ev, dict) else None
        if not isinstance(run, dict):
            problems.append(f"{rel}: no 'run' object")
            continue
        h = run.get("harness")
        if not (isinstance(h, dict) and {"branch", "commit", "dirty"} <= set(h)):
            problems.append(f"{rel}: run.harness is missing or lacks branch, commit or dirty")
        if not isinstance(run.get("withheld_artifacts"), dict):
            problems.append(f"{rel}: run.withheld_artifacts is missing or not an object")
        for k in ("identity",):
            if not isinstance(run.get(k), dict):
                problems.append(f"{rel}: run.{k} is missing or not an object")
        for f in ("gateway_ledger.jsonl", "artifacts_manifest.json"):
            if not (bundle / "runs" / rid / f).is_file():
                problems.append(f"runs/{rid}/{f}: missing")
    c.check("every file the checks read exists, parses and carries the fields they need (state, results, evidence.json with run.harness and run.withheld_artifacts, ledgers)", not problems, problems[:5])
    if problems:
        print("  (the remaining checks need a well-formed bundle)")
        raise Abort


def summary(c: "Checker") -> int:
    print(f"\n{c.n - len(c.failed)} of {c.n} checks passed" + ("" if not c.failed else f"; FAILED: {len(c.failed)}") + (f"; {len(c.skipped)} SKIPPED (not passes; see above)" if c.skipped else ""))
    print("Dollar figures are gateway-computed from measured tokens and a recorded price table; they are not provider billing.")
    return 0 if not c.failed else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bundle", type=Path, default=ROOT / "evidence" / "e1-stage1")
    ap.add_argument("--report", type=Path, default=ROOT / "docs" / "research" / "E1_STAGE1_REPORT.md")
    ap.add_argument("--git-root", type=Path, default=ROOT, help="repository whose Git history is consulted for the provenance checks (default: this checkout)")
    ap.add_argument("--require-git", action="store_true", help="a provenance check that needs Git history and cannot run is a failure instead of a SKIP")
    a = ap.parse_args(argv)
    c = Checker(require_git=a.require_git)
    try:
        return verify(a, c)
    except Abort:
        return summary(c)
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, OSError) as e:  # safety net: malformed input is a FAIL with a reason, never a traceback
        c.check("the verification ran to completion on this input", False, f"malformed or incomplete input ({type(e).__name__}: {str(e)[:120]})")
        return summary(c)


def verify(a: argparse.Namespace, c: Checker) -> int:
    bundle: Path = a.bundle

    # ------------------------------------------------------------------ 1. integrity
    section("1. Bundle integrity")
    manifest_p = bundle / "MANIFEST.sha256"
    c.check("MANIFEST.sha256 exists", manifest_p.is_file())
    if not manifest_p.is_file():
        raise Abort
    listed = parse_manifest(c, manifest_p)
    actual = sorted(str(p.relative_to(bundle)) for p in bundle.rglob("*") if p.is_file() and p.name != "MANIFEST.sha256")
    c.check("the manifest lists exactly the files present (no extra, none missing)", sorted(listed) == actual, sorted(set(listed) ^ set(actual))[:4])
    bad = [n for n, h in listed.items() if not (bundle / n).is_file() or sha((bundle / n).read_bytes()) != h]
    c.check(f"all {len(listed)} listed files match their sha256", not bad, bad[:3])
    want = {"README.md", "PROVENANCE.json", "batch/state.json", "batch/results.jsonl"}
    for rid, _, _ in EXPECTED_RUNS:
        want |= {f"runs/{rid}/{x}" for x in ("gateway_ledger.jsonl", "artifacts_manifest.json", "evidence.json")}
    c.check("the bundle contains exactly the 4 batch/top files and 3 files for each of the 8 expected run ids", set(listed) == want - {"MANIFEST.sha256"}, sorted(set(listed) ^ want)[:4])
    structural_preflight(c, bundle)
    try:
        personal = denylist_pattern()
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 2
    scan_hits = {}
    for n in listed:
        text = (bundle / n).read_text()
        hits = [k for k, rx in UNSAFE.items() if rx.search(text)]
        if personal is not None and personal.search(text):
            hits.append(PERSONAL_CATEGORY)
        if hits:
            scan_hits[n] = hits
    c.check("no file contains an absolute path, IP address, credential word, e-mail or session id (or any term on the optional AIEP_SCAN_DENYLIST file, when set)", not scan_hits, scan_hits)

    check_provenance(c, bundle, listed, a.git_root, a.report)

    # ------------------------------------------------------------------ 2. definition
    section("2. Locked experiment definition")
    doc = load_experiment(ROOT / DEFINITION, root=ROOT)  # full structural validation against the tracked configuration
    c.check("the definition loads and passes its own validation", True)
    c.check("definition_hash() recomputes to the recorded value", definition_hash(doc) == doc["definition_sha256"] == DEFINITION_SHA256)
    c.check("status locked; per-run budget 0.18; total cap 3.84; pair gap 600 s", doc["status"] == "locked" and doc["limits"]["budget_usd_per_run"] == "0.18"
            and doc["limits"]["max_total_computed_usd"] == "3.84" and doc["design"]["pair_gap_s"] == CLAIMS["pair_gap_s"])
    c.check("model and Claude binary are the pinned ones", doc["model"] == MODEL and doc["claude_cli"]["version"] == "2.1.328")
    state = json.loads((bundle / "batch" / "state.json").read_text())
    rows = jl(bundle / "batch" / "results.jsonl")
    c.check("state.json carries the same definition hash and ends complete with Stage 2 unrun", state["definition_sha256"] == DEFINITION_SHA256 and state["status"] == "complete_with_unrun_stages"
            and state["halt"] is None and state["replacements"] == [] and [n["task_id"] for n in state["not_run"]] == ["sphinx-doc__sphinx-10325", "psf__requests-863"]
            and all(n["reason"] == "stage_not_activated" for n in state["not_run"]))
    sched = {s["slot_id"]: s for s in doc["design"]["schedule"] if s["stage"] == 1}
    c.check("the four Stage 1 tasks and eight scheduled slots are the ones that ran", [t for s in doc["design"]["stages"] if s["stage"] == 1 for t in s["tasks"]] == TASKS and len(sched) == 8)

    # ------------------------------------------------------------------ 3. rows
    section("3. Result rows and run identities")
    c.check("eight rows, in execution order, with the expected run ids, tasks and arms", [(r["run_id"], r["task_id"], r["arm"]) for r in rows] == EXPECTED_RUNS)
    evs = {rid: json.loads((bundle / "runs" / rid / "evidence.json").read_text()) for rid, _, _ in EXPECTED_RUNS}
    ok = True
    for r in rows:
        s = sched.get(r["slot_id"])
        ok &= bool(s) and all(s[k] == r[k] for k in ("arm", "order_index", "pair_id")) and r["experiment_id"] == "e1-exploratory-1" and r["definition_sha256"] == DEFINITION_SHA256
        ok &= evs[r["run_id"]]["run"]["identity"]["slot_id"] == r["slot_id"] and evs[r["run_id"]]["run"]["identity"]["order_index"] == r["order_index"]
    c.check("every row matches its scheduled slot (arm, order index, pair id) and its evidence.json identity", ok)
    c.check("every row is valid, graded, handoff-passed, exact and complete; no halts, no invalid runs", all(
        r["valid"] and r["graded"] and r["grading_valid"] and r["valid_measurement"] and r["handoff_status"] in ("ready", "ready_flagged") and r["cost_exact"]
        and r["accounting_complete"] and r["upper_bound_entries"] == 0 and r["invalid_reason"] is None and r["handoff_rejections"] == [] for r in rows))
    c.check("all eight rows resolved; each has F2P 1/1 and all P2P maintained", all(r["resolved"] is True and r["f2p_passed"] == r["f2p_total"] == 1
            and r["p2p_maintained"] == r["p2p_total"] for r in rows))
    c.check("both arms use the registered workflow hashes and the same template", all(
        r["prompt"]["workflow_sha256"] == doc["prompt_set"]["sha256"][r["arm"]] and r["prompt"]["template_sha256"] == doc["prompt_set"]["sha256"]["template"] for r in rows))
    by_task = pair_of(rows)
    c.check("within each task A and B differ only in the workflow hash (same template and same issue-text hash)", all(
        by_task[t]["A"]["prompt"]["template_sha256"] == by_task[t]["B"]["prompt"]["template_sha256"]
        and evs[by_task[t]["A"]["run_id"]]["prompt"]["issue_sha256"] == evs[by_task[t]["B"]["run_id"]]["prompt"]["issue_sha256"]
        and by_task[t]["A"]["prompt"]["workflow_sha256"] != by_task[t]["B"]["prompt"]["workflow_sha256"] for t in TASKS))

    # ------------------------------------------------------------------ 4. ledgers and costs
    section("4. Gateway ledgers, requests, tokens and cost (recomputed)")
    pricing = load_pricing(ROOT / PRICING, MODEL)
    per: dict[str, dict[str, Any]] = {}
    for r in rows:
        rid = r["run_id"]
        lines = jl(bundle / "runs" / rid / "gateway_ledger.jsonl")
        fwd = [l for l in lines if l["outcome"] == "forwarding"]
        fin = [l for l in lines if "status" in l]
        tot = Usage()
        cost = D(0)
        every_cost_ok = True
        for l in fin:
            u = Usage(**{k: l["usage"][k] for k in l["usage"]})
            cst = compute_cost(pricing, u).usd
            every_cost_ok &= (cst == D(l["cost_usd_computed"]) and l["cost_upper_bound"] is False)
            cost += cst
            for k in l["usage"]:
                setattr(tot, k, getattr(tot, k) + l["usage"][k])
        per[rid] = {"cost": cost, "tokens": tot.as_dict(), "requests": len(fin), "first": fin[0], "first_cost": D(fin[0]["cost_usd_computed"]), "arm": r["arm"], "task": r["task_id"],
                    "uncached": D(uncached_equivalent_usd(tot.as_dict(), pricing)), "ok": every_cost_ok, "fwd": len(fwd), "statuses": {l["status"] for l in fin}, "ts": [l["ts"] for l in lines]}
    c.check("every ledger: forwarded == finished == the row's request count, all HTTP 200", all(p["fwd"] == p["requests"] == r["requests"] and p["statuses"] == {200} for p, r in zip(per.values(), rows)))
    c.check("per-request cost recomputes from tokens x price table (equals the ledger's own figure) and none is an upper bound", all(p["ok"] for p in per.values()))
    c.check("per-run token totals equal the rows' tokens", all(per[r["run_id"]]["tokens"] == r["tokens"] for r in rows))
    c.check("per-run recomputed cost equals the row's cost and the run's recorded accounting", all(
        per[r["run_id"]]["cost"] == D(r["cost_usd_computed"]) == D(evs[r["run_id"]]["run"]["usage_measured"]["computed_usd_upper_bound_basis"]) for r in rows))
    c.check("the ledger bytes equal the hash recorded at run time (manifest, evidence.json, run.json copy)", all(
        sha((bundle / "runs" / r["run_id"] / "gateway_ledger.jsonl").read_bytes()) == json.loads((bundle / "runs" / r["run_id"] / "artifacts_manifest.json").read_text())["files"]["gateway_ledger.jsonl"]["sha256"]
        == evs[r["run_id"]]["run"]["lifecycle"]["ledger_sha256"] for r in rows))
    c.check("per-run uncached-equivalent cost recomputes and equals the row's", all(per[r["run_id"]]["uncached"] == D(r["uncached_equivalent_usd"]) for r in rows))
    total = sum((p["cost"] for p in per.values()), D(0))
    arm_cost = {x: sum((p["cost"] for p in per.values() if p["arm"] == x), D(0)) for x in "AB"}
    arm_unc = {x: sum((p["uncached"] for p in per.values() if p["arm"] == x), D(0)) for x in "AB"}
    arm_req = {x: sum(p["requests"] for p in per.values() if p["arm"] == x) for x in "AB"}
    c.check("total computed cost = 0.2337872 = state.cumulative_cost_usd (gateway-computed, not billing)", total == CLAIMS["total_cost"] == D(state["cumulative_cost_usd"]), str(total))
    c.check("per-arm computed cost A 0.1118429, B 0.1219443; B minus A 0.0101014", arm_cost["A"] == CLAIMS["cost_A"] and arm_cost["B"] == CLAIMS["cost_B"]
            and arm_cost["B"] - arm_cost["A"] == CLAIMS["cost_diff"], str(arm_cost))
    c.check("uncached-equivalent: A 0.268796, B 0.403688, difference 0.134892", arm_unc["A"] == CLAIMS["uncached_A"] and arm_unc["B"] == CLAIMS["uncached_B"]
            and arm_unc["B"] - arm_unc["A"] == CLAIMS["uncached_diff"], str(arm_unc))
    c.check("requests: 34 total (A 14, B 20)", arm_req["A"] + arm_req["B"] == CLAIMS["requests"]["total"] and arm_req["A"] == CLAIMS["requests"]["A"] and arm_req["B"] == CLAIMS["requests"]["B"], str(arm_req))
    tk = {x: {k: sum(p["tokens"][k] for p in per.values() if p["arm"] == x) for k in CLAIMS["tokens"][x]} for x in "AB"}
    c.check("per-arm token totals match the report (input, output, cache read, cache write 5 m)", tk == CLAIMS["tokens"], str(tk))
    c.check("no 1-hour or unclassified cache-write tokens anywhere", all(p["tokens"]["cache_write_1h_tokens"] == 0 and p["tokens"]["cache_write_unclassified_tokens"] == 0 for p in per.values()))
    pair_d, pair_u, pair_x = {}, {}, {}
    for t in TASKS:
        a_, b_ = (per[by_task[t][x]["run_id"]] for x in "AB")
        pair_d[t] = b_["cost"] - a_["cost"]
        pair_u[t] = b_["uncached"] - a_["uncached"]
        pair_x[t] = (b_["cost"] - b_["first_cost"]) - (a_["cost"] - a_["first_cost"])
    c.check("paired B-minus-A computed-cost differences match the report", pair_d == CLAIMS["pair_cost_diff"], str(pair_d))
    c.check("paired uncached-equivalent differences match the report; all four positive", pair_u == CLAIMS["pair_uncached_diff"] and all(v > 0 for v in pair_u.values()))
    c.check("first-request-excluded sensitivity analysis matches the report (per pair and sum 0.0239134); all four positive", pair_x == CLAIMS["pair_first_excluded_diff"]
            and sum(pair_x.values(), D(0)) == CLAIMS["first_excluded_sum"] and all(v > 0 for v in pair_x.values()), str(pair_x))
    warm = {rid: (p["first"]["usage"]["cache_read_tokens"], p["first_cost"]) for rid, p in per.items() if p["first"]["usage"]["cache_read_tokens"] > 0}
    cold = [p["first_cost"] for p in per.values() if p["first"]["usage"]["cache_read_tokens"] == 0]
    c.check("cache warmth of the first request: exactly 13933-B, 15851-B and 22714-A started warm (6,205 cached tokens); the other five cold", warm == CLAIMS["warm_first_requests"] and len(cold) == 5, str(warm))
    c.check("cold first requests cost between 0.0200 and 0.0223; the warm-versus-cold gap (about 0.015) exceeds the unadjusted cost difference in the two Django pairs",
            all(D("0.0199") < x < D("0.0223") for x in cold) and (min(cold) - max(v[1] for v in warm.values())) > D("0.014")
            and abs(pair_d["django__django-13933"]) < D("0.0148") and abs(pair_d["django__django-15851"]) < D("0.0148"))

    # ------------------------------------------------------------------ 5. timeline
    section("5. Timeline: pair gaps and the interval between pairs (state.json)")
    ev = state["events"]
    start = {e["slot"]: e["t"] for e in ev if e["event"] == "run_start"}
    done = {e["slot"]: e["t"] for e in ev if e["event"] == "run_done"}
    gaps, between = [], []
    for i, t in enumerate(TASKS):
        first, second = (by_task[t][a_]["slot_id"] for a_ in ("A", "B")) if by_task[t]["A"]["arm_position"] == 1 else (by_task[t]["B"]["slot_id"], by_task[t]["A"]["slot_id"])
        gaps.append(start[second] - done[first])
    order = sorted(rows, key=lambda r: r["order_index"])
    exact_between = []
    for k in (2, 4, 6):
        exact_between.append(round(start[order[k]["slot_id"]] - done[order[k - 1]["slot_id"]], 1))
        between.append(round(exact_between[-1]))
    c.check("the second arm of every pair started 600 s (+/- 0.5 s) after the first arm finished", all(abs(g - CLAIMS["pair_gap_s"]) < 0.5 for g in gaps), [round(g, 2) for g in gaps])
    c.check("no gap between pairs: the next pair's first arm started 72, 69 and 69 s after the previous pair's second arm finished (72.4, 69.4, 69.4 before rounding)", between == CLAIMS["between_pairs_s"] and all(60 < x < 80 for x in exact_between), exact_between)
    t0 = datetime.fromtimestamp(next(e["t"] for e in ev if e["event"] == "batch_start"), timezone.utc).strftime("%H:%M:%S")
    t1 = datetime.fromtimestamp(next(e["t"] for e in ev if e["event"] == "complete"), timezone.utc).strftime("%H:%M:%S")
    c.check("batch ran 17:13:46 to 18:09:36 UTC", (t0, t1) == CLAIMS["window_utc"], (t0, t1))
    c.check("every run_done cost in the state equals the row's cost and no overshoot was recorded", all(
        D(e["cost"]) == D(next(r for r in rows if r["slot_id"] == e["slot"])["cost_usd_computed"]) for e in ev if e["event"] == "run_done")
        and all(D(e["overshoot"]) == 0 for e in ev if e["event"] == "overshoot_check"))
    c.check("wall seconds total 333.454 (A 135.839, B 197.615)", sum(D(str(r["wall_seconds"])) for r in rows) == D("333.454")
            and sum(D(str(r["wall_seconds"])) for r in rows if r["arm"] == "A") == D("135.839") and sum(D(str(r["wall_seconds"])) for r in rows if r["arm"] == "B") == D("197.615"))

    # ------------------------------------------------------------------ 6. controls
    section("6. Gold and no-op controls (state.json)")
    gold = noop = 0
    for t in TASKS:
        ctl = state["blocks"][t]["controls"][t]
        g, n = ctl["gold"], ctl["noop"]
        gold += bool(g["problems"] == [] and g["summary"]["valid"] and g["summary"]["resolved"] is True and g["summary"]["f2p_passed"] == g["summary"]["f2p_total"] == 1
                     and g["summary"]["p2p_maintained"] == g["summary"]["p2p_total"])
        noop += bool(n["problems"] == [] and n["summary"]["valid"] and n["summary"]["resolved"] is False and n["summary"]["f2p_passed"] == 0 and n["summary"]["f2p_total"] == 1
                     and n["summary"]["p2p_maintained"] == n["summary"]["p2p_total"])
    c.check("four gold controls resolved (all F2P pass, all P2P maintained) and four no-op controls unresolved (0 F2P pass)", (gold, noop) == (4, 4), (gold, noop))
    c.check("controls ran before each task's agent runs (the no-op control event is not later than that task's first run_start, whose timestamp it equals to 0.1 s)", all(
        max(e["t"] for e in ev if e["event"] == "control" and e["task"] == t) <= min(start[by_task[t][x]["slot_id"]] for x in "AB") for t in TASKS))

    # ------------------------------------------------------------------ 7. patches and handoff
    section("7. Patch hashes and handoff, as recorded")
    ok = True
    for r in rows:
        e = evs[r["run_id"]]
        man = json.loads((bundle / "runs" / r["run_id"] / "artifacts_manifest.json").read_text())["files"]["patch.diff"]
        ok &= r["patch_sha256"] == man["sha256"] == e["run"]["agent"]["patch"]["sha256"] == e["handoff"]["patch"]["sha256"] == e["grading"]["evaluator_patch_sha256"] and r["patch_size"] == man["size"]
    c.check("each patch's sha256 is the same in the row, the manifest, the agent record, the handoff and the evaluator's copy (a consistency check on recorded values)", ok)
    c.check("the two sympy-22714 patches (A and B) are byte-identical by hash", by_task["sympy__sympy-22714"]["A"]["patch_sha256"] == by_task["sympy__sympy-22714"]["B"]["patch_sha256"])
    flagged = {r["run_id"] for r in rows if r["handoff_status"] == "ready_flagged"}
    exp_flagged = {"m2-sympy__sympy-18621-B-20261123T172624Z", "m2-django__django-15851-B-20261123T174307Z", "m2-sympy__sympy-22714-A-20261123T175656Z", "m2-sympy__sympy-22714-B-20261123T180803Z"}
    c.check("exactly four runs are ready_flagged (18621-B, 15851-B, 22714-A, 22714-B); the other four are ready with no flags", flagged == exp_flagged and all(
        r["handoff_flags"] == [] for r in rows if r["run_id"] not in flagged))
    trig = {}
    for r in rows:
        for chk in evs[r["run_id"]]["handoff"]["checks"]:
            if not chk["ok"]:
                trig.setdefault(r["run_id"], []).append((chk["check"][:40], chk["detail"]))
    exp_trig = {"m2-sympy__sympy-18621-B-20261123T172624Z": "gold_patch_lines", "m2-sympy__sympy-22714-A-20261123T175656Z": "gold_patch_lines", "m2-sympy__sympy-22714-B-20261123T180803Z": "gold_patch_lines",
                "m2-django__django-15851-B-20261123T174307Z": "count"}
    c.check("flag triggers: the three gold-line flags each have count 1; the 15851-B flag is a test-file overlap of count 1; flag text states the PASSING condition, so a flag means it failed",
            set(trig) == set(exp_trig) and all(len(v) == 1 and v[0][1][exp_trig[k]] == 1 for k, v in trig.items()), trig)

    # ------------------------------------------------------------------ 8. behaviour
    section("8. Test behaviour (recorded classifications; fix-revision re-derived from them)")
    started_by_arm = {x: sum(1 for r in rows if r["arm"] == x and evs[r["run_id"]]["transcript"]["test_runs_started"] >= 1) for x in "AB"}
    c.check("runs that started a test runner: A 0 of 4, B 4 of 4", started_by_arm == CLAIMS["tests_started_runs"], started_by_arm)
    tr = [evs[r["run_id"]]["transcript"] for r in rows]
    c.check("B test attempts 7: 5 started, 2 not started (missing runner); 2 runs with a failed run; the rows agree with the transcript summaries", (
        sum(t["test_runs"] for t in tr), sum(t["test_runs_started"] for t in tr), sum(t["test_runs_not_started"] for t in tr), sum(1 for t in tr if t["test_runs_failed"] > 0))
        == (CLAIMS["test_attempts"], CLAIMS["tests_started"], CLAIMS["tests_not_started"], CLAIMS["failed_runs"])
        and all(r["test_runs"] == evs[r["run_id"]]["transcript"]["test_runs"] and r["test_runs_started"] == evs[r["run_id"]]["transcript"]["test_runs_started"] for r in rows))
    c.check("parser test-fix cycles: 1 in total (15851-B), with 0 confirmed-edit cycles", sum(t["test_fix_cycles"] for t in tr) == CLAIMS["parser_cycles"]
            and sum(t["test_fix_cycles_confirmed_edit"] for t in tr) == 0 and evs["m2-django__django-15851-B-20261123T174307Z"]["transcript"]["test_fix_cycles"] == 1)
    fr = {r["run_id"]: fix_revision_cycles(evs[r["run_id"]]) for r in rows}
    c.check("re-derived: 0 implementation-fix revision cycles in all eight runs; 1 test-expectation update (15851-B, edit target a test file)",
            sum(v[0] for v in fr.values()) == CLAIMS["fix_revision_cycles"] and sum(v[1] for v in fr.values()) == CLAIMS["test_update_cycles"]
            and fr["m2-django__django-15851-B-20261123T174307Z"] == (0, 1), fr)
    c.check("every transcript summary is complete, parser v2, with 0 ambiguous sequences", all(t["complete"] and t["parser_version"] == 2 and t["ambiguous_sequences"] == 0 for t in tr))
    c.check("grading evidence: every run applied its patch, resolved with 0 errors, evaluator container policy drops all capabilities with no-new-privileges", all(
        evs[r["run_id"]]["grading"]["resolved"] and evs[r["run_id"]]["grading"]["errors"] == 0 and evs[r["run_id"]]["grading"]["container_policy"]["cap_drop"] == ["ALL"]
        and evs[r["run_id"]]["grading"]["container_policy"]["security_opt"] == ["no-new-privileges"] for r in rows))

    # ------------------------------------------------------------------ 9. report
    section("9. Research report consistency")
    if a.report.is_file():
        text = a.report.read_text()
        miss = [s for s in REPORT_STRINGS if s not in text]
        c.check(f"the report text contains every one of the {len(REPORT_STRINGS)} figures verified above", not miss, miss[:5])
    else:
        print(f"  SKIP  report not found at {a.report}")

    return summary(c)


if __name__ == "__main__":
    sys.exit(main())

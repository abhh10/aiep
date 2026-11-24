#!/usr/bin/env python3
"""Build the sanitized E1 Stage 1 evidence bundle (``evidence/e1-stage1/``) from the PRIVATE run artifacts.

    PYTHONPATH=src .venv/bin/python scripts/build_e1_stage1_evidence.py [--out evidence/e1-stage1] [--replace]

Reads (read-only; nothing under ``runs/`` is ever opened for writing, and the builder re-hashes every source after building to prove it): the batch state and results,
the eight agent run directories, their handoff reports and their M1 grading directories. Writes only into ``--out``. The bundle holds NO raw transcript, patch, prompt,
evaluator log, container or network identifier, absolute path or free-form transcript text. See ``evidence/e1-stage1/README.md`` for what is kept and why.

The builder FAILS CLOSED: any key outside an explicit keep/drop list, any value outside an explicit finite set, any pattern hit (absolute path, IP, credential word, e-mail,
session id, named person) and any hidden-test identifier or gold/test-patch line in the output aborts the build, and nothing is left behind on a failed build.
Provenance separates three commit identities that must not be confused: ``execution.harness_commit`` is the ORIGINAL commit at which the experiment ran (read from the eight
runs' own records; it belongs to the original, unpublished history and is not expected to exist in this repository), ``execution.public_history_commit`` is its verified equivalent in
this repository's rewritten, published history (taken from the committed config/history_rewrite.json and checked to exist and to be an ancestor of HEAD), and
``builder.built_on_commit`` is the clean commit that contains this builder when the bundle is generated. The builder refuses to generate provenance on a dirty tracked
worktree, when it is not in HEAD, when the committed copy differs from the source being run, or when the eight execution records disagree.
No personal name is hard-coded: the scan also looks for the identity of the account running the builder (read at run time) and for any terms in the optional, uncommitted
file named by ``AIEP_SCAN_DENYLIST``.
Needs the pinned local dataset snapshot (``.cache/hf``) for the hidden-data scan, so it cannot run without the private environment; the VERIFIER can.
"""

from __future__ import annotations

import argparse
import functools
import getpass
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

EXPERIMENT_ID = "e1-exploratory-1"
DEFINITION_PATH = Path("config/experiments/e1-exploratory-1.json")
DEFINITION_SHA256 = "9601b345da705b7e8bbdba251b902f4c7637482fd2e87e732dcba0dc36875eff"
BUILDER_REL = "scripts/build_e1_stage1_evidence.py"
SCHEMA = 1
PROVENANCE_SCHEMA = 3  # 3 adds execution.public_history_commit (the original execution commit is not in the rewritten public history); 2 separated the builder from the execution; 1 recorded one ambiguous `harness_head_commit`
_COMMIT = re.compile(r"[0-9a-f]{40}")
HISTORY_MAP_REL = "config/history_rewrite.json"

TOOLS = ("Bash", "Edit", "Glob", "Grep", "Read", "Write")
TEST_STATES = ("not_started", "completed_failed", "completed_passed", "started_incomplete", "unknown")
EDIT_CLASSES = ("python_write", "sed_i", "perl_i", "tee", "vcs_or_patch", "cat_redirect", "redirect_write")
TARGET_KINDS = ("source_file", "test_file", "unknown")
FINAL_STATES = ("failed", "passed", "unknown", None)

# every key a source object may carry is listed as KEEP (copied or reduced) or DROP (deliberately withheld); anything else aborts the build
RESULT_ROW_KEEP = frozenset("""accounting_complete ambiguous_sequences arm arm_position bash_edit_like_calls cap_exceeded claude_duration_ms claude_exit_code claude_reported_cost_source
claude_reported_cost_usd claude_sha256 compliance cost_exact cost_usd_computed definition_sha256 edits_total experiment_id f2p_passed f2p_total gateway_budget_usd gateway_cost_source
graded grading_run_id grading_valid handoff_flags handoff_rejections handoff_report_sha256 handoff_status harness_errors image_id invalid_reason lifecycle_status model non_success_requests
order_index p2p_maintained p2p_total pair_id patch_applied patch_sha256 patch_size prompt prompt_set_file repetition repo request_categories requests resolved run_id schema
seed_sha256 slot_id stop_reason task_id test_fix_cycles test_fix_cycles_confirmed_edit test_runs test_runs_failed test_runs_not_started test_runs_started test_usage tokens
tool_calls_total transcript_complete transcript_parser_version turns uncached_equivalent_usd upper_bound_entries valid valid_measurement wall_seconds""".split())
RESULT_ROW_DROP = frozenset({"run_dir"})  # an absolute path on the producing machine

STATE_KEYS = frozenset("blocks consecutive_infra_invalid cumulative_cost_usd decisions definition_sha256 events experiment_id halt not_run replacements reservations schema stage_activations status summary".split())
STATE_BLOCK_KEYS = frozenset("controls image_removed overshoot_usd pulled slots status task_id".split())
STATE_EVENT_KEYS = frozenset("""allowance_status arms_reserved budget_usd_per_run cost event funded gateway_budget handoff kind ledger_cost not_run overshoot overshoot_allowance_usd problems
remaining_cap_usd reservation_usd resolved slot stage stages t task""".split())

LEDGER_KEYS = frozenset("""bytes_in bytes_out cost_upper_bound cost_usd_computed cumulative_usd_computed duration_s endpoint max_tokens_cap method model outcome requested_max_tokens rid seq
status stream ts usage""".split())
MANIFEST_KEYS = frozenset({"files", "run_id", "schema"})
MANIFEST_FILES = frozenset("claude_stderr.txt claude_stdout.jsonl gateway_ledger.jsonl patch.diff predictions.jsonl prompt.txt run.json".split())

RUN_KEEP = frozenset("""accounting aiep claude_args claude_cli claude_runtime config experiment image_id lifecycle limits model orchestrator_wall_s prompt seed task usage_cli_estimate
usage_measured""".split())
RUN_DROP = frozenset("""budget_attribution evaluation files gateway_image interrupted lease network_subnets networks orchestrator_error prompt_set_file prompt_set_file_sha256 proxy redactions
run_id schema termination""".split())
LIFE_KEEP = frozenset("accounting agent gateway_removed gateway_state gateway_stop_reason ledger proxy status topology".split())
LIFE_DROP = frozenset("gateway_cleanup_error gateway_error gateway_errors".split())
AGENT_KEEP = frozenset("""account claude_exit_code claude_output_truncated claude_stderr_bytes claude_stdout_bytes components_sha256 config_label duration_s invalid_reason oom_killed patch removed
seed status stop_reason survivors_after_quiesce timeline_s valid attachment""".split())
AGENT_DROP = frozenset({"cleanup_error", "container_id", "patch_error"})
AIEP_KEEP = frozenset({"branch", "commit", "dirty", "untracked_count", "changed_files"})
USAGE_KEEP = frozenset("computed_usd_upper_bound_basis entries_charged_at_max_tokens_upper_bound exact metered request_status requests source tokens unparseable_lines unresolved_requests".split())

HANDOFF_KEEP = frozenset({"checks", "flags", "grading", "rejections", "report_sha256", "schema", "status", "summary"})
HANDOFF_DROP = frozenset({"run_dir"})
HANDOFF_SUMMARY_KEEP = frozenset("accounting base_commit config hidden_data instance_id model patch repo run_id experiment".split())
HANDOFF_SUMMARY_DROP = frozenset({"evaluator", "evaluator_command_not_run"})

META_KEEP = frozenset("exit_code hard_stop_gib hard_stopped image image_id instance kind run_id start_gate_gib summary".split())
META_DROP = frozenset({"command", "disk_before_gib", "disk_min_gib", "ended_unix", "env", "predictions", "started_unix"})
META_SUMMARY_KEEP = frozenset("errors exit_code f2p_passed f2p_total hard_stopped instance_id note p2p_maintained p2p_total patch_applied resolved run_id valid".split())
CREATE_KEEP = frozenset({"cap_drop", "security_opt", "mem_limit", "pids_limit", "nano_cpus", "user"})
CREATE_DROP = frozenset({"command", "detach", "image", "memswap_limit", "name"})

# patterns that must never appear anywhere in the bundle (applied to every file's text)
UNSAFE = {
    "absolute_path": re.compile(r"(?<![\w.~-])/(?:Users|home|tmp|var|private|opt|root|etc|proc|mnt|run|dev|usr|testbed|workspace|Volumes)\b"),
    "home_tilde": re.compile(r"~/"),
    "ipv4": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "uuid": re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
    "credential_word": re.compile(r"sk-ant|bearer\s|x-api-key|authorization|upstream_api_key|password|secret", re.I),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "socket_or_session": re.compile(r"cc-socks|messaging_socket|session_id", re.I),
}

# Personal identifiers are NOT hard-coded here. Two sources, both read at run time and never stored in the repository:
#  * the identity of the machine and account that run the builder (OS user, home-directory name, host name, git author name and e-mail handle);
#  * an optional file named by the environment variable AIEP_SCAN_DENYLIST (one case-insensitive term per line, '#' comments allowed), kept OUTSIDE the repository, for names
#    that cannot be derived (for example third parties who appear in benchmark issue text). If it is set but unreadable the build fails.
DENYLIST_ENV = "AIEP_SCAN_DENYLIST"
PERSONAL_CATEGORY = "personal_identifier"
_COMMON_WORDS = frozenset({"root", "user", "users", "admin", "guest", "runner", "localhost", "nobody", "default", "test", "tests", "ubuntu", "docker"})


class BuildError(RuntimeError):
    """A sanitization or consistency check failed: the build is aborted and nothing is kept."""


def _git(root: Path, *args: str) -> "subprocess.CompletedProcess[bytes]":
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise BuildError(f"git is not usable in {root} ({type(e).__name__})") from e


def committed_builder_state(root: Path, rel: str = BUILDER_REL) -> dict[str, str]:
    """Identify the builder that is about to generate the bundle, or refuse.

    Provenance must name a builder that can be recovered from Git, so this requires, in order: a Git checkout with a commit; a clean TRACKED worktree (untracked files, such as
    the not-yet-committed bundle and report, do not count); the builder present in the HEAD commit; and the committed builder byte-identical to the source being executed.
    Returns ``{"script", "sha256", "built_on_commit"}``. ``built_on_commit`` is the generation commit, which is NOT the commit at which the experiment was executed."""
    head = _git(root, "rev-parse", "HEAD")
    need(head.returncode == 0, "the harness directory is not a Git checkout with a commit: provenance needs one")
    commit = head.stdout.decode().strip()
    need(_COMMIT.fullmatch(commit) is not None, "unexpected output from git rev-parse HEAD")
    status = _git(root, "status", "--porcelain", "--untracked-files=no")
    need(status.returncode == 0, "git status failed")
    changed = [l for l in status.stdout.decode().splitlines() if l.strip()]
    need(not changed, f"the tracked worktree is dirty ({len(changed)} changed path(s)): commit or restore it before generating provenance")
    blob = _git(root, "show", f"HEAD:{rel}")
    need(blob.returncode == 0, f"the builder ({rel}) is not present in the HEAD commit: commit it before generating provenance")
    committed = sha256_bytes(blob.stdout)
    need(committed == sha256_file(root / rel), "the builder source being executed differs from the builder committed at HEAD")
    return {"script": rel, "sha256": committed, "built_on_commit": commit}


def public_history_commit(root: Path, original: str) -> str:
    """The commit of THIS repository's published history that corresponds to the original execution commit, or refuse.

    The mapping must be committed at HEAD (and byte-identical to the working copy), contain exactly one entry for ``original``, name a well-formed commit that exists in this
    repository, and that commit must be an ancestor of HEAD. There is no fallback: an unmapped or unverifiable execution commit stops the build."""
    blob = _git(root, "show", f"HEAD:{HISTORY_MAP_REL}")
    need(blob.returncode == 0, f"{HISTORY_MAP_REL} is not committed at HEAD: the original execution commit cannot be tied to the published history")
    need(hashlib.sha256(blob.stdout).hexdigest() == sha256_file(root / HISTORY_MAP_REL), f"{HISTORY_MAP_REL} differs from the copy committed at HEAD")
    try:
        doc = json.loads(blob.stdout)
        entries = [m for m in doc["mappings"] if isinstance(m, dict) and m.get("original_commit") == original]
        need(doc.get("schema") == 1 and len(entries) == 1, f"{HISTORY_MAP_REL} must have schema 1 and exactly one entry for the execution commit")
        public = entries[0]["public_commit"]
    except (KeyError, TypeError, ValueError) as e:
        raise BuildError(f"{HISTORY_MAP_REL} is malformed ({type(e).__name__})") from e
    need(isinstance(public, str) and _COMMIT.fullmatch(public) is not None, "the mapped public commit is not a 40-character git commit id")
    need(_git(root, "cat-file", "-e", f"{public}^{{commit}}").returncode == 0, "the mapped public commit does not exist in this repository")
    need(_git(root, "merge-base", "--is-ancestor", public, "HEAD").returncode == 0, "the mapped public commit is not an ancestor of HEAD")
    return public


def execution_record(records: "list[Mapping[str, Any]]") -> dict[str, Any]:
    """The commit at which the experiment RAN, taken from the eight scored runs' own records (run.json ``aiep``), never from the current HEAD."""
    need(len(records) == 8, f"expected 8 execution records, got {len(records)}")
    commits = {r.get("commit") for r in records}
    need(len(commits) == 1, f"the execution records disagree about the harness commit ({len(commits)} distinct values)")
    (commit,) = commits
    need(isinstance(commit, str) and _COMMIT.fullmatch(commit) is not None, "the recorded execution commit is not a 40-character git commit id")
    need(all(r.get("dirty") is False for r in records), "an execution record is not from a clean worktree")
    return {"harness_commit": commit, "runs_checked": len(records), "all_clean": True,
            "source": "aiep.commit of each scored run's run.json: the harness commit at execution, not the commit at bundle generation"}


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(p.read_bytes())


def dumps(o: Any) -> str:
    return json.dumps(o, indent=2, sort_keys=True) + "\n"


def need(cond: Any, msg: str) -> None:
    if not cond:
        raise BuildError(msg)


def split_keys(obj: Any, keep: Iterable[str], drop: Iterable[str], where: str, optional: Iterable[str] = ()) -> dict[str, Any]:
    """Fail closed on an unknown key; every required keep key must exist. Returns the kept part."""
    need(isinstance(obj, dict), f"{where}: expected an object")
    keep, drop, optional = set(keep), set(drop), set(optional)
    extra = set(obj) - keep - drop - optional
    need(not extra, f"{where}: unexpected keys {sorted(extra)} (add them to the keep or drop list only after review)")
    missing = (keep - set(obj)) - optional
    need(not missing, f"{where}: missing keys {sorted(missing)}")
    return {k: obj[k] for k in obj if k in keep or k in optional}


def strings_in(o: Any) -> Iterable[str]:
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for k, v in o.items():
            yield str(k)
            yield from strings_in(v)
    elif isinstance(o, (list, tuple)):
        for v in o:
            yield from strings_in(v)


def _denylist_terms(path: str) -> list[str]:
    if not path:
        return []
    try:
        lines = Path(path).read_text().splitlines()
    except OSError as e:
        raise BuildError(f"{DENYLIST_ENV} is set but cannot be read ({type(e).__name__})") from e
    terms = [l.strip() for l in lines if l.strip() and not l.lstrip().startswith("#")]
    need(all(len(t) >= 3 for t in terms), f"{DENYLIST_ENV}: every term needs at least 3 characters")
    return terms


def _local_identity_terms() -> list[str]:
    """Who runs the builder, read at run time: OS user, home-directory name, host name, git author name and the handle part of the git e-mail."""
    raw: list[str] = []
    for getter in (getpass.getuser, lambda: Path.home().name, lambda: socket.gethostname().split(".")[0]):
        try:
            raw.append(str(getter()))
        except Exception:  # an identity that cannot be read is simply not scanned for
            pass
    for key in ("user.name", "user.email"):
        try:
            out = subprocess.run(["git", "-C", str(ROOT), "config", key], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            out = ""
        if key == "user.email" and "@" in out:
            out = out.split("@", 1)[0].split("+")[-1]
        if out:
            raw.append(out)
    return sorted({t for t in raw if len(t) >= 4 and t.lower() not in _COMMON_WORDS})


@functools.lru_cache(maxsize=8)
def personal_pattern(denylist_path: str = "") -> "re.Pattern[str] | None":
    terms = sorted({t.lower() for t in _local_identity_terms() + _denylist_terms(denylist_path)})
    if not terms:
        return None
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(t) for t in terms) + r")(?![A-Za-z0-9])", re.I)


def scan_text(text: str, where: str) -> None:
    hits = {name: sorted(set(m.group(0) for m in rx.finditer(text)))[:3] for name, rx in UNSAFE.items() if rx.search(text)}
    personal = personal_pattern(os.environ.get(DENYLIST_ENV, ""))
    if personal is not None and personal.search(text):
        hits[PERSONAL_CATEGORY] = ["<redacted>"]  # never echo a personal identifier
    need(not hits, f"{where}: unsafe content {hits}")


def repo_relative(p: str, where: str) -> str:
    need(isinstance(p, str) and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./+-]*", p) is not None and ".." not in p.split("/") and not p.startswith("/"),
         f"{where}: not a plain repository-relative path")
    return p


def target_kind(path: str) -> str:
    """A controlled classification of an edit target. The path itself is not retained."""
    p = path.strip().lstrip("./")
    if not p or re.search(r"[$`*?{}\s]", p):
        return "unknown"
    if re.search(r"(^|/)(tests?|testing)/", p) or re.search(r"(^|/)(test_[^/]*|[^/]*_test|conftest)\.py$", p):
        return "test_file"
    return "source_file"


def edit_tool_target_kind(file_path: Any) -> str:
    if isinstance(file_path, str) and file_path.startswith("/testbed/"):
        return target_kind(file_path[len("/testbed/"):])
    return "unknown"


# ---------------------------------------------------------------------------------------------------------------- transcript
def transcript_summary(raw: bytes) -> dict[str, Any]:
    """Controlled, numeric and enumerated facts only: no command text, evidence string, message text, path or id survives."""
    from aiep.transcript import parse_transcript
    p = parse_transcript(raw, cycle_cap=5)
    need(p["complete"] is True and p["unparseable_lines"] == 0 and p["parser_version"] == 2, "transcript: not complete or not parser v2")
    # an independent pass over the tool calls, ordered like the parser's (assistant tool_use blocks in order)
    calls: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        ev = json.loads(line)
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        if ev.get("type") == "assistant":
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    c = {"id": b.get("id"), "name": b.get("name"), "input": b.get("input") if isinstance(b.get("input"), dict) else {}, "is_error": None}
                    calls.append(c)
                    if isinstance(c["id"], str):
                        by_id[c["id"]] = c
        elif ev.get("type") == "user" and isinstance(msg.get("content"), list):
            for b in msg["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in by_id:
                    by_id[b["tool_use_id"]]["is_error"] = b.get("is_error")
    need(len(calls) == p["tool_calls_total"], "transcript: tool call count differs from the parser's")
    need(all(c["name"] in TOOLS for c in calls), f"transcript: unexpected tool names {sorted({c['name'] for c in calls} - set(TOOLS))}")
    need(dict(Counter(c["name"] for c in calls)) == p["tool_calls_by_name"], "transcript: tool name counts differ from the parser's")
    need(len(p["analysis_details"]) < 80, "transcript: analysis_details may be truncated")

    attempts, bash_edits = [], []
    for d in p["analysis_details"]:
        if d["kind"] == "test_attempt":
            need(d["state"] in TEST_STATES and isinstance(d["call_index"], int) and isinstance(d["test_commands_in_call"], int), "transcript: unexpected test attempt value")
            attempts.append({"call_index": d["call_index"], "state": d["state"], "test_commands_in_call": d["test_commands_in_call"]})
        elif d["kind"] == "bash_edit_like":
            need(all(c in EDIT_CLASSES for c in d["classification"]) and isinstance(d["call_index"], int) and isinstance(d["targets_known"], bool), "transcript: unexpected edit class")
            kinds = sorted({target_kind(t) for t in d["targets"]}) if d["targets_known"] and d["targets"] else ["unknown"]
            need(all(k in TARGET_KINDS for k in kinds), "transcript: unexpected target kind")
            bash_edits.append({"call_index": d["call_index"], "classification": sorted(set(d["classification"])), "target_kinds": kinds, "targets_known": d["targets_known"]})
        else:
            raise BuildError(f"transcript: unknown analysis detail kind {d['kind']!r}")
    tool_edits = []
    for i, c in enumerate(calls):
        if c["name"] in ("Edit", "Write"):
            tool_edits.append({"call_index": i, "tool": c["name"], "target_kind": edit_tool_target_kind(c["input"].get("file_path")),
                               "is_error": c["is_error"] if isinstance(c["is_error"], bool) else None})
    need(len(tool_edits) == p["edits_total"], "transcript: Edit/Write count differs from the parser's edits_total")
    need(p["final_test_state"] in FINAL_STATES, "transcript: unexpected final test state")
    ints = ("tool_calls_total", "edits_total", "edit_tool_calls_errored", "bash_edit_like_calls", "test_runs", "test_runs_failed", "test_runs_passed", "test_runs_unknown",
            "test_runs_not_started", "test_runs_started", "test_runs_completed", "test_runs_started_incomplete", "edits_after_first_test_run", "test_fix_cycles",
            "test_fix_cycles_confirmed_edit", "test_fix_cycles_bash_edit_only", "unfinished_cycles", "cycle_cap", "assistant_messages")
    out: dict[str, Any] = {k: p[k] for k in ints}
    need(all(type(v) is int for v in out.values()), "transcript: non-integer metric")
    out.update({"parser_version": p["parser_version"], "complete": True, "first_test_run_call_index": p["first_test_run_call_index"], "final_test_state": p["final_test_state"],
                "ambiguous_sequences": len(p["ambiguous_sequences"]), "compliance": bool(p["compliance"]), "cap_exceeded": bool(p["cap_exceeded"]), "test_usage": bool(p["test_usage"]),
                "ended_with_passing_tests": bool(p["ended_with_passing_tests"]),
                "tool_calls": [{"call_index": i, "tool": c["name"]} for i, c in enumerate(calls)], "test_attempts": attempts, "bash_edit_like": bash_edits, "edit_tool_calls": tool_edits})
    return out


# ---------------------------------------------------------------------------------------------------------------- grading
def runner_summary(test_output: str) -> dict[str, Any]:
    """The harness's final summary line as controlled numbers. Anything else fails the build."""
    lines = test_output.splitlines()
    sym = [m for m in (re.fullmatch(r"=+ tests finished: (\d+) passed, in [\d.]+ seconds =+", l.strip()) for l in lines) if m]
    if sym:
        return {"runner": "sympy_test", "tests_passed": int(sym[-1].group(1)), "tests_failed": 0}
    ran = [(i, int(m.group(1))) for i, m in ((i, re.fullmatch(r"Ran (\d+) tests? in [\d.]+s", l.strip())) for i, l in enumerate(lines)) if m]
    need(ran, "grading: no recognised runner summary line")
    i, n = ran[-1]
    tail = [l.strip() for l in lines[i + 1:] if l.strip()][:1]
    m = re.fullmatch(r"OK(?: \(skipped=(\d+)\))?", tail[0]) if tail else None
    need(m is not None, "grading: runner outcome line is not a plain OK")
    return {"runner": "django_runtests", "tests_run": n, "outcome": "OK", "skipped": int(m.group(1) or 0)}


def grading_section(grading_dir: Path, row: Mapping[str, Any]) -> tuple[dict[str, Any], list[Path]]:
    meta_p = grading_dir / "meta.json"
    meta = json.loads(meta_p.read_text())
    keep = split_keys(meta, META_KEEP, META_DROP, f"{grading_dir.name}/meta.json")
    summ = split_keys(keep["summary"], META_SUMMARY_KEEP, (), f"{grading_dir.name}/meta.json summary")
    inst_dirs = sorted((grading_dir / "logs" / "run_evaluation").glob("*/*/*"))
    need(len(inst_dirs) == 1 and inst_dirs[0].is_dir(), f"{grading_dir.name}: expected exactly one evaluated instance directory")
    d = inst_dirs[0]
    report = json.loads((d / "report.json").read_text())
    need(list(report) == [row["task_id"]], "grading: report.json is not for this task")
    r = report[row["task_id"]]
    ts = r["tests_status"]
    counts = {cat: {k: len(v) for k, v in ts[cat].items()} for cat in ("FAIL_TO_PASS", "PASS_TO_PASS")}  # counts only: identifiers never leave this function
    need(counts["FAIL_TO_PASS"]["success"] == summ["f2p_passed"] and counts["PASS_TO_PASS"]["success"] == summ["p2p_maintained"], "grading: report.json and meta.json disagree")
    log = (d / "run_instance.log").read_text()
    applied = re.findall(r"^Applied patch (\S+) cleanly\.$", log, re.M)
    for a in applied:
        repo_relative(a, "grading: applied patch path")
    ck_lines = (grading_dir / "create_kwargs.jsonl").read_text().splitlines()
    need(len(ck_lines) == 1, "grading: expected exactly one container-create record")
    ck = split_keys(json.loads(ck_lines[0]), CREATE_KEEP, CREATE_DROP, "create_kwargs.jsonl")
    need(isinstance(ck["user"], str) and re.fullmatch(r"[a-z0-9_.-]{1,32}", ck["user"]) is not None, "grading: unexpected container user value")
    out = {
        "resolved": summ["resolved"], "valid": summ["valid"], "patch_applied": summ["patch_applied"], "errors": summ["errors"], "exit_code": summ["exit_code"],
        "f2p_passed": summ["f2p_passed"], "f2p_total": summ["f2p_total"], "p2p_maintained": summ["p2p_maintained"], "p2p_total": summ["p2p_total"],
        "report_json_counts": counts, "report_json_flags": {k: r[k] for k in ("patch_exists", "patch_is_None", "patch_successfully_applied", "resolved", "infra_failure")},
        "evaluator_image_id": keep["image_id"], "evaluator_image_name": keep["image"], "evaluation_kind": keep["kind"],
        "evaluator_patch_sha256": sha256_file(d / "patch.diff"), "patch_files_applied_cleanly": len(applied),
        "runner_summary": runner_summary((d / "test_output.txt").read_text(errors="replace")),
        "container_policy": {k: ck[k] for k in ("cap_drop", "security_opt", "mem_limit", "pids_limit", "nano_cpus", "user")},
        "start_gate_gib": keep["start_gate_gib"], "hard_stop_gib": keep["hard_stop_gib"], "hard_stopped": keep["hard_stopped"],
    }
    srcs = [meta_p, d / "report.json", d / "run_instance.log", d / "test_output.txt", d / "patch.diff", grading_dir / "create_kwargs.jsonl"]
    return out, srcs


# ---------------------------------------------------------------------------------------------------------------- handoff / run
def handoff_section(path: Path, row: Mapping[str, Any]) -> dict[str, Any]:
    h = json.loads(path.read_text())
    keep = split_keys(h, HANDOFF_KEEP, HANDOFF_DROP, f"{path.name}")
    need(keep["report_sha256"] == row["handoff_report_sha256"], "handoff: report_sha256 differs from the results row")
    summ = split_keys(keep["summary"], HANDOFF_SUMMARY_KEEP, HANDOFF_SUMMARY_DROP, f"{path.name} summary", optional=("experiment",))
    patch = split_keys(summ["patch"], ("additions", "deletions", "files", "paths", "sha256", "size"), (), f"{path.name} patch")
    for p in patch["paths"]:
        repo_relative(p, "handoff: patch path")
    checks = []
    for c in keep["checks"]:
        c = split_keys(c, ("check", "detail", "ok", "severity"), (), "handoff check")
        need(isinstance(c["check"], str) and c["severity"] in ("reject", "flag") and isinstance(c["ok"], bool), "handoff: unexpected check shape")
        d = c["detail"]
        # details survive only as small objects of integers and booleans (counts); every other detail (strings, lists, addresses) is withheld
        keep_d = isinstance(d, dict) and 0 < len(d) <= 8 and all(re.fullmatch(r"[a-z_0-9]+", k) and isinstance(v, (bool, int)) for k, v in d.items())
        checks.append({"check": c["check"], "ok": c["ok"], "severity": c["severity"], "detail": d if keep_d else None})
    acc = summ["accounting"]
    return {"status": keep["status"], "flags": keep["flags"], "rejections": keep["rejections"], "report_sha256": keep["report_sha256"], "checks": checks,
            "patch": patch, "hidden_data_counts": summ["hidden_data"] | {}, "accounting_at_handoff": {k: acc[k] for k in ("budget_usd", "complete", "computed_usd", "exact", "requests", "tokens")},
            "details_withheld_for": sum(1 for c in checks if c["detail"] is None)}


def run_section(run: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    r = split_keys(run, RUN_KEEP, RUN_DROP, "run.json")
    life = split_keys(r["lifecycle"], LIFE_KEEP, LIFE_DROP, "run.json lifecycle")
    agent = split_keys(life["agent"], AGENT_KEEP, AGENT_DROP, "run.json lifecycle.agent")
    for k in ("cleanup_error", "patch_error"):
        need(run["lifecycle"]["agent"].get(k) is None, f"run: agent {k} is not null")
    need(not run["lifecycle"]["gateway_errors"] and run["lifecycle"]["gateway_cleanup_error"] is None, "run: gateway errors recorded")
    topo = life["topology"]
    att = agent["attachment"]
    need(not att["published_ports"], "run: the agent published ports")
    um = split_keys(r["usage_measured"], USAGE_KEEP, (), "run.json usage_measured")
    rs = um["request_status"]
    for sub in ("by_outcome", "by_status", "categories", "refused_by_reason"):
        need(all(isinstance(v, int) for v in rs[sub].values()), f"run: request_status.{sub} is not a map of integers")
    aiep = split_keys(r["aiep"], AIEP_KEEP, (), "run.json aiep")
    need(aiep["changed_files"] == [] and aiep["dirty"] is False and aiep["untracked_count"] == 0, "run: the harness tree was not clean when the run executed")
    cli = split_keys(r["claude_cli"], ("platform", "sha256", "size_bytes", "version"), (), "claude_cli")
    est = split_keys(r["usage_cli_estimate"], ("duration_ms", "is_error", "num_turns", "source", "subtype", "total_cost_usd"), (), "usage_cli_estimate")
    need(all(re.fullmatch(r"-{1,2}[a-z-]+|[A-Za-z0-9,._-]+", a) for a in r["claude_args"]), "run: unexpected claude argument")
    return {
        "identity": r["experiment"], "config": r["config"], "model": r["model"], "task": r["task"], "image_id": r["image_id"], "seed": r["seed"],
        "claude_cli": cli, "claude_args": r["claude_args"], "claude_pinned_env": r["claude_runtime"]["pinned_env"], "limits": r["limits"],
        "orchestrator_wall_s": r["orchestrator_wall_s"], "harness": {"branch": aiep["branch"], "commit": aiep["commit"], "dirty": aiep["dirty"]},
        "accounting": r["accounting"] | {}, "usage_measured": um, "usage_cli_estimate": est,
        "lifecycle": {"status": life["status"], "gateway_state": life["gateway_state"], "gateway_removed": life["gateway_removed"], "gateway_stop_reason": life["gateway_stop_reason"],
                      "ledger_sha256": life["ledger"]["sha256"], "ledger_bytes": life["ledger"]["bytes"], "proxy_state": life["proxy"]["state"], "proxy_removed": life["proxy"]["removed"]},
        "agent": {k: agent[k] for k in ("account", "claude_exit_code", "claude_output_truncated", "claude_stderr_bytes", "claude_stdout_bytes", "components_sha256", "config_label",
                                         "duration_s", "invalid_reason", "oom_killed", "patch", "removed", "seed", "status", "stop_reason", "timeline_s", "valid")} |
                 {"survivors_after_quiesce": len(agent["survivors_after_quiesce"])},
        "isolation_record": {"agent_networks": sorted(att["networks"]), "agent_published_ports": len(att["published_ports"]), "agent_route_count": len(att["routes"]),
                             "containers": {role: sorted(nets) for role, nets in topo["containers"].items()},
                             "networks": {name: {"internal": bool(v["internal"])} for name, v in topo["networks"].items()},
                             "note": "network names and the internal flag only; addresses, subnets and container ids are withheld"},
        "withheld_artifacts": {name: manifest["files"][name] for name in ("claude_stdout.jsonl", "patch.diff", "predictions.jsonl", "prompt.txt", "run.json", "claude_stderr.txt")},
    }


def prompt_section(row: Mapping[str, Any], run_dir: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    raw = (run_dir / "prompt.txt").read_bytes()
    text = raw.decode("utf-8")
    need(sha256_bytes(raw) == row["prompt"]["prompt_sha256"], "prompt: prompt.txt differs from the recorded hash")
    m = re.search(r"<issue>\n(.*)\n</issue>", text, re.S)
    need(m is not None, "prompt: no issue block")
    issue = m.group(1)
    public = str(record["problem_statement"]).strip("\n")
    return {"label": row["prompt"]["label"], "prompt_sha256": row["prompt"]["prompt_sha256"], "template_sha256": row["prompt"]["template_sha256"],
            "workflow_sha256": row["prompt"]["workflow_sha256"], "prompt_bytes": len(raw), "issue_sha256": sha256_bytes(issue.encode()),
            "issue_matches_public_dataset_problem_statement": sha256_bytes(public.encode()) == sha256_bytes(issue.encode()),
            "note": "the prompt text is withheld: it embeds the public issue text, which can name third parties"}


# ---------------------------------------------------------------------------------------------------------------- bundle
def readme_text(rows: list[dict[str, Any]]) -> str:
    runs = "\n".join(f"| {r['order_index']} | `{r['run_id']}` | {r['task_id']} | {r['arm']} |" for r in rows)
    return f"""# E1 Stage 1 evidence bundle

A sanitized, committed subset of the evidence behind `docs/research/E1_STAGE1_REPORT.md` (experiment `{EXPERIMENT_ID}`, definition sha256 `{DEFINITION_SHA256}`).
It lets someone who clones this repository check the report's quantitative claims **offline**, without Docker, credentials, network access or the private run directories.

## What is in it

| Path | What | Treatment |
|---|---|---|
| `batch/state.json` | batch state, event timeline, gold and no-op control outcomes, reservations, cumulative cost | copied byte for byte after a key-by-key review and a pattern scan |
| `batch/results.jsonl` | the eight result rows | the same rows with one field removed (`run_dir`, an absolute path on the producing machine) |
| `runs/<run_id>/gateway_ledger.jsonl` | the gateway's per-request ledger (timestamps, token counts, computed cost, status) | copied byte for byte |
| `runs/<run_id>/artifacts_manifest.json` | the file names, sizes and sha256 recorded when the run finished | copied byte for byte |
| `runs/<run_id>/evidence.json` | run, accounting, handoff, grading and transcript summaries | derived from private files, with an explicit keep list and finite value sets |
| `PROVENANCE.json` | for every file: how it was produced, its private sources and their sha256; the builder (its sha256 and the commit that contains it); and, separately, the commit at which the experiment ran | generated |
| `MANIFEST.sha256` | sha256 of every other file in the bundle | generated |

The eight runs, in execution order:

| Order | Run id | Task | Arm |
|---|---|---|---|
{runs}

## Verify it

```bash
python scripts/verify_e1_stage1_evidence.py            # from the repository root; standard library plus this repository's own `src/aiep`
```

It exits 0 only if every check passes. It needs no private file.

## What the verifier establishes, in three tiers

**1. Recomputed from the committed data** (the ledgers, `config/model_pricing.json`, the locked definition and the timestamps in `state.json`): the definition hash; every request count,
HTTP status and token count; every per-run, per-arm and total gateway-computed cost; the uncached-equivalent cost; the first-request-excluded sensitivity analysis; the prompt-cache warmth of
each run's first request; the 600 s pair gaps and the intervals between pairs; and that the report quotes these figures.

**2. Consistency checks among recorded values** (these show the summaries agree with each other, not that they are true): the rows against the schedule, the state and each `evidence.json`;
the manifest hashes; the patch hash recorded in the row, the manifest, the handoff and the evaluator copy being the same value; the four gold and four no-op control outcomes as written in
`state.json`; the provenance record (its schema; `builder.sha256` against the builder committed at `builder.built_on_commit`; `execution.harness_commit` (the original execution commit) against the run records in every `evidence.json`; `execution.public_history_commit` against `config/history_rewrite.json` as committed at `builder.built_on_commit`; the definition hash and the recorded source hashes); the handoff statuses and flag triggers; and the test-behaviour counts, including the stricter fix-revision count, which is re-derived from the recorded attempt and edit
classifications.

Checks that need Git history (the committed builder at `builder.built_on_commit`, and the existence of `execution.public_history_commit` and its mapping) print SKIP, never PASS, when the history is unavailable, for example in a shallow clone; `--require-git` turns a SKIP into a failure.

**3. Recorded summaries that cannot be recomputed here:** the resolved verdicts, F2P/P2P counts and evaluator runner summaries (counts and fixed values only, never test identifiers); handoff
check outcomes; and everything under `transcript` in `evidence.json` (tool-call order, test-attempt states, edit classes and target kinds). These are what the builder read from the private
files. The hashes do not prove them.

## Rebuilding

`scripts/build_e1_stage1_evidence.py` regenerates the bundle from the private `runs/` directory and the pinned local dataset snapshot, so it only runs on the machine that holds them. It reads
those files without modifying them, aborts on any unexpected field or unsafe pattern, scans the result for hidden-test identifiers and patch lines, and is deterministic (no timestamps) apart from `builder.built_on_commit`, the commit it was generated on. It refuses to run unless the tracked worktree is clean and the builder is committed at HEAD exactly as executed.

## What is withheld, and why

- **Raw transcripts**, because they are not needed for the quantitative tables, three of the eight contain a line of the upstream fix, and every one embeds session identifiers and host details.
- **Patches and predictions**, because an agent patch here is close to or equal to the upstream fix, which the project does not publish. Only hashes, sizes, file paths and line counts are kept.
- **Prompts**, because they embed public issue text that can name third parties. Only hashes are kept, plus a hash of the issue text and whether it equals the public dataset's problem statement.
- **Evaluator logs and reports**, because they contain the hidden test patch and test identifiers.
- **Network, container and host details** (addresses, subnets, container ids, socket paths, absolute paths), because they are not needed to check any result.

The file hashes in `artifacts_manifest.json`, `PROVENANCE.json` and `evidence.json` identify withheld files and show that the bundle is internally consistent. They do **not** let a third party
verify the withheld contents, which only the holder of the private files can recheck.

## Scope and cost

- Only E1 Stage 1 is included. **Calibration evidence is excluded**: the two calibration runs are neither scored nor in these files, so any figure about them is not checkable here.
- **Every dollar figure is gateway-computed** (measured token counts multiplied by the recorded price table). None is verified provider billing or a Console figure. Claude Code's own cost
  field is a client-side estimate and is stored separately.
- This is a small exploratory study: four tasks, one run per arm per task. Nothing here supports a statistical claim.
"""


def collect(runs_root: Path) -> dict[str, Any]:
    from aiep.handoff import default_load_record
    rows_p = runs_root / "m3" / EXPERIMENT_ID / "results.jsonl"
    state_p = runs_root / "m3" / EXPERIMENT_ID / "state.json"
    rows_raw = [json.loads(l) for l in rows_p.read_text().splitlines() if l.strip()]
    need(len(rows_raw) == 8, "expected exactly 8 result rows")
    rows = []
    for r in rows_raw:
        split_keys(r, RESULT_ROW_KEEP, RESULT_ROW_DROP, f"results row {r.get('slot_id')}")
        rows.append(r)
    rows.sort(key=lambda r: r["order_index"])
    need([r["order_index"] for r in rows] == list(range(1, 9)), "result rows do not carry order indexes 1..8")
    need(all(r["experiment_id"] == EXPERIMENT_ID and r["definition_sha256"] == DEFINITION_SHA256 for r in rows), "a row belongs to another experiment or definition")
    state_text = state_p.read_text()
    state = json.loads(state_text)
    split_keys(state, STATE_KEYS, (), "state.json")
    for b in state["blocks"].values():
        split_keys(b, STATE_BLOCK_KEYS, (), "state.json block")
    for e in state["events"]:
        split_keys(e, STATE_EVENT_KEYS, (), "state.json event", optional=STATE_EVENT_KEYS)
    need(state["definition_sha256"] == DEFINITION_SHA256 and state["status"] == "complete_with_unrun_stages" and state["halt"] is None, "state.json: unexpected status or definition")
    need(state["cumulative_cost_usd"] == "0.2337872" and state["replacements"] == [], "state.json: unexpected cost or replacements")
    scan_text(state_text, "batch/state.json")  # a copy is only made after this passes
    definition_file = ROOT / DEFINITION_PATH
    dd = json.loads(definition_file.read_text())
    need(dd["definition_sha256"] == DEFINITION_SHA256 and dd["status"] == "locked", "the definition file is not the locked E1 definition")

    files: dict[str, bytes] = {}
    prov: dict[str, dict[str, Any]] = {}
    sources_all: list[Path] = [rows_p, state_p]
    harness_records: list[dict[str, Any]] = []

    def src(p: Path) -> dict[str, Any]:
        return {"path": p.relative_to(ROOT).as_posix(), "sha256": sha256_file(p), "bytes": p.stat().st_size}

    files["batch/state.json"] = state_text.encode()
    prov["batch/state.json"] = {"treatment": "verbatim", "sources": [src(state_p)], "note": "reviewed key by key against an explicit list and pattern-scanned before copying"}
    sanitized_rows = [{k: v for k, v in r.items() if k not in RESULT_ROW_DROP} for r in rows]
    files["batch/results.jsonl"] = ("".join(json.dumps(r, sort_keys=True) + "\n" for r in sanitized_rows)).encode()
    prov["batch/results.jsonl"] = {"treatment": "sanitized", "sources": [src(rows_p)], "dropped_fields": sorted(RESULT_ROW_DROP),
                                   "note": "every row is otherwise byte-for-byte as written by the batch; the file hash therefore differs from the private file's"}

    records = {}
    for task in sorted({r["task_id"] for r in rows}):
        records[task] = default_load_record(ROOT)(task)
    for r in rows:
        rid = r["run_id"]
        run_dir = runs_root / "m2" / rid
        need(run_dir.is_dir(), f"{rid}: run directory is missing")
        manifest_p = run_dir / "artifacts_manifest.json"
        manifest = json.loads(manifest_p.read_text())
        split_keys(manifest, MANIFEST_KEYS, (), f"{rid} manifest")
        need(set(manifest["files"]) == MANIFEST_FILES and manifest["run_id"] == rid, f"{rid}: unexpected manifest contents")
        for name, meta in manifest["files"].items():  # the private artifacts must still match what was recorded when the run finished
            need(sha256_file(run_dir / name) == meta["sha256"] and (run_dir / name).stat().st_size == meta["size"], f"{rid}: {name} no longer matches its recorded hash")
        ledger_p = run_dir / "gateway_ledger.jsonl"
        ledger_text = ledger_p.read_text()
        for n, line in enumerate(ledger_text.splitlines(), 1):
            split_keys(json.loads(line), LEDGER_KEYS, (), f"{rid} ledger line {n}", optional=LEDGER_KEYS)
        scan_text(ledger_text, f"{rid}/gateway_ledger.jsonl")
        scan_text(manifest_p.read_text(), f"{rid}/artifacts_manifest.json")
        run = json.loads((run_dir / "run.json").read_text())
        need(run["run_id"] == rid and run["experiment"]["slot_id"] == r["slot_id"], f"{rid}: run.json identity differs from the results row")
        need(sha256_file(ledger_p) == run["lifecycle"]["ledger"]["sha256"], f"{rid}: ledger differs from the hash recorded in run.json")
        grading, grading_srcs = grading_section(runs_root / "m1" / r["grading_run_id"], r)
        need(grading["evaluator_image_id"] == r["image_id"] == run["image_id"], f"{rid}: image ids disagree")
        need(grading["evaluator_patch_sha256"] == r["patch_sha256"] == manifest["files"]["patch.diff"]["sha256"], f"{rid}: patch hashes disagree")
        handoff_p = runs_root / "handoff" / f"{rid}.handoff.json"
        evidence = {
            "schema": SCHEMA, "run_id": rid, "order_index": r["order_index"],
            "run": run_section(run, manifest), "prompt": prompt_section(r, run_dir, records[r["task_id"]]),
            "handoff": handoff_section(handoff_p, r), "grading": grading,
            "transcript": transcript_summary((run_dir / "claude_stdout.jsonl").read_bytes()) | {"sha256": manifest["files"]["claude_stdout.jsonl"]["sha256"],
                                                                                              "bytes": manifest["files"]["claude_stdout.jsonl"]["size"]},
            "notes": "derived summaries only; see README.md for what is a recorded summary and what is recomputable",
        }
        need(evidence["handoff"]["patch"]["sha256"] == r["patch_sha256"], f"{rid}: handoff patch hash differs")
        harness_records.append(evidence["run"]["harness"])
        base = f"runs/{rid}"
        files[f"{base}/gateway_ledger.jsonl"] = ledger_text.encode()
        files[f"{base}/artifacts_manifest.json"] = manifest_p.read_bytes()
        files[f"{base}/evidence.json"] = dumps(evidence).encode()
        prov[f"{base}/gateway_ledger.jsonl"] = {"treatment": "verbatim", "sources": [src(ledger_p)]}
        prov[f"{base}/artifacts_manifest.json"] = {"treatment": "verbatim", "sources": [src(manifest_p)]}
        derived_sources = [run_dir / "run.json", run_dir / "claude_stdout.jsonl", run_dir / "prompt.txt", handoff_p, *grading_srcs]
        prov[f"{base}/evidence.json"] = {"treatment": "derived", "sources": [src(p) for p in derived_sources],
                                          "note": "explicit keep and drop lists; transcript, prompt and evaluator content reduced to counts, hashes and finite classifications"}
        sources_all += [ledger_p, manifest_p, *derived_sources]
    return {"files": files, "prov": prov, "rows": rows, "sources": sources_all, "records": records, "harness_records": harness_records}


def hidden_scan(files: Mapping[str, bytes], records: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    """No FAIL_TO_PASS/PASS_TO_PASS identifier and no gold or hidden-test-patch line (16+ characters) may appear anywhere in the bundle."""
    from aiep.handoff import _as_list
    blob = "\n".join(b.decode("utf-8", "replace") for b in files.values())
    n_ids = n_lines = 0
    for task, rec in records.items():
        ids = [i for k in ("FAIL_TO_PASS", "PASS_TO_PASS") for i in _as_list(rec.get(k)) if len(i) >= 6]
        for i in ids:
            n_ids += 1
            need(i not in blob, f"hidden-data scan: a test identifier of {task} appears in the bundle")
        for k in ("patch", "test_patch"):
            for line in str(rec.get(k) or "").splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    t = line[1:].strip()
                    if len(t) >= 16:
                        n_lines += 1
                        need(t not in blob, f"hidden-data scan: a line of the {k} of {task} appears in the bundle")
    return {"identifiers_checked": n_ids, "patch_lines_checked": n_lines}


def build(runs_root: Path, out: Path, replace: bool) -> dict[str, Any]:
    need(runs_root.is_dir(), f"no private run directory at {runs_root}")
    need(Path(__file__).resolve() == (ROOT / BUILDER_REL).resolve(), "this is not the repository's builder script")
    builder = committed_builder_state(ROOT)  # refuses on a dirty tracked tree, a builder missing from HEAD, or a committed builder that differs from this source
    before = {p: sha256_file(p) for p in sorted(p for p in runs_root.rglob("*") if p.is_file())}
    c = collect(runs_root)
    files: dict[str, bytes] = c["files"]
    prov: dict[str, Any] = c["prov"]
    files["README.md"] = readme_text(c["rows"]).encode()
    prov["README.md"] = {"treatment": "generated", "sources": [], "note": "static text written by the builder"}
    for name, data in files.items():
        scan_text(data.decode("utf-8"), name)
    scan_counts = hidden_scan(files, c["records"])
    execution = execution_record(c["harness_records"])
    execution["public_history_commit"] = public_history_commit(ROOT, execution["harness_commit"])
    provenance = {"schema": PROVENANCE_SCHEMA, "bundle": "e1-stage1", "experiment_id": EXPERIMENT_ID,
                  "builder": builder, "execution": execution,
                  "definition": {"path": DEFINITION_PATH.as_posix(), "definition_sha256": DEFINITION_SHA256, "file_sha256": sha256_file(ROOT / DEFINITION_PATH)},
                  "sanitization": {"unsafe_patterns": sorted(UNSAFE) + [PERSONAL_CATEGORY], "hidden_data_scan": scan_counts,
                                  "statement": "hashes of private source files identify them and show internal consistency; they do not let a third party verify withheld contents"},
                  "files": {k: prov[k] for k in sorted(prov)}}
    files["PROVENANCE.json"] = dumps(provenance).encode()
    scan_text(files["PROVENANCE.json"].decode(), "PROVENANCE.json")
    manifest = "".join(f"{sha256_bytes(files[k])}  {k}\n" for k in sorted(files))
    files["MANIFEST.sha256"] = manifest.encode()

    if out.exists():
        need(replace, f"{out} exists (use --replace to rebuild)")
        need((out / "MANIFEST.sha256").is_file(), f"{out} is not an evidence bundle: refusing to remove it")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.parent / (out.name + ".build-tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    try:
        for name, data in files.items():
            p = tmp / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        if out.exists():
            shutil.rmtree(out)
        tmp.rename(out)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    after = {p: sha256_file(p) for p in sorted(p for p in runs_root.rglob("*") if p.is_file())}
    need(before == after, "a private run artifact changed during the build (this must never happen)")
    return {"files": len(files), "bytes": sum(len(v) for v in files.values()), "scan": scan_counts, "private_files_unchanged": len(after)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "evidence" / "e1-stage1")
    ap.add_argument("--runs-root", type=Path, default=ROOT / "runs")
    ap.add_argument("--replace", action="store_true", help="replace an existing bundle at --out (only a directory that already holds a MANIFEST.sha256)")
    a = ap.parse_args(argv)
    try:
        res = build(a.runs_root, a.out, a.replace)
    except BuildError as e:
        print(f"BUILD FAILED (closed): {e}", file=sys.stderr)
        return 2
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

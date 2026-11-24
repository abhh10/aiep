"""Tests for the E1 Stage 1 evidence builder's sanitizers and the offline verifier. Offline: no Docker, no private run artifacts, no network."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "evidence" / "e1-stage1"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


builder = _load("build_e1_stage1_evidence")
verifier = _load("verify_e1_stage1_evidence")


def test_target_kind_separates_test_files_from_source_and_never_keeps_the_path():
    assert builder.target_kind("tests/dbshell/test_postgresql.py") == "test_file"
    assert builder.target_kind("sympy/geometry/tests/test_point.py") == "test_file"
    assert builder.target_kind("django/db/backends/postgresql/client.py") == "source_file"
    assert builder.target_kind("point.py") == "source_file"
    assert builder.target_kind("$FILE") == "unknown"
    assert builder.edit_tool_target_kind("/testbed/django/forms/models.py") == "source_file"
    assert builder.edit_tool_target_kind("/elsewhere/x.py") == "unknown"


def test_split_keys_fails_closed_on_unknown_and_missing_keys():
    assert builder.split_keys({"a": 1, "b": 2}, {"a"}, {"b"}, "x") == {"a": 1}
    with pytest.raises(builder.BuildError, match="unexpected keys"):
        builder.split_keys({"a": 1, "surprise": 2}, {"a"}, set(), "x")
    with pytest.raises(builder.BuildError, match="missing keys"):
        builder.split_keys({}, {"a"}, set(), "x")


@pytest.mark.parametrize("bad", ["/Users/someone/project", "see /tmp/x", "host 172.19.0.2", "key sk-ant-abc", "Bearer abc", "a@b.com", "~/work",
                                 "12345678-1234-1234-1234-123456789abc", "session_id"])
def test_scan_text_rejects_unsafe_content(bad):
    with pytest.raises(builder.BuildError):
        builder.scan_text(bad, "t")


def test_scan_text_accepts_ordinary_bundle_content():
    builder.scan_text('{"cost_usd_computed": "0.0397001", "tests/dbshell/test_postgresql.py": 1, "version": "2.1.328"}', "t")


GENERIC_CATEGORIES = {"absolute_path", "home_tilde", "ipv4", "uuid", "credential_word", "email", "socket_or_session"}


def test_scanner_patterns_are_generic_and_embed_no_personal_names():
    for mod in (builder, verifier):
        assert set(mod.UNSAFE) == GENERIC_CATEGORIES  # the former named-person category is gone from both scripts
    assert verifier.denylist_pattern("") is None


def test_denylist_terms_come_from_an_uncommitted_file(tmp_path, monkeypatch):
    deny = tmp_path / "deny.txt"
    deny.write_text("# comment\n\nZyxwvuq Placeholder\nqwertyplaceholder\n")
    monkeypatch.setenv(builder.DENYLIST_ENV, str(deny))
    builder.personal_pattern.cache_clear()
    try:
        with pytest.raises(builder.BuildError, match="personal_identifier"):
            builder.scan_text("a line naming zyxwvuq placeholder in passing", "t")
        with pytest.raises(builder.BuildError) as e:
            builder.scan_text("QWERTYPLACEHOLDER", "t")
        assert "qwertyplaceholder" not in str(e.value).lower()  # the matched identifier is never echoed
        builder.scan_text("an ordinary line with neither term", "t")
        builder.scan_text("qwertyplaceholderx is not a whole-word match", "t")
    finally:
        builder.personal_pattern.cache_clear()
    rx = verifier.denylist_pattern(str(deny))
    assert rx.search("ZYXWVUQ PLACEHOLDER") and not rx.search("nothing relevant")


def test_denylist_fails_closed_when_unreadable_or_malformed(tmp_path, monkeypatch):
    monkeypatch.setenv(builder.DENYLIST_ENV, str(tmp_path / "missing.txt"))
    builder.personal_pattern.cache_clear()
    try:
        with pytest.raises(builder.BuildError, match="cannot be read"):
            builder.scan_text("x", "t")
    finally:
        builder.personal_pattern.cache_clear()
    with pytest.raises(RuntimeError, match="cannot be read"):
        verifier.denylist_pattern(str(tmp_path / "missing.txt"))
    short = tmp_path / "short.txt"
    short.write_text("ab\n")
    with pytest.raises(RuntimeError, match="at least 3"):
        verifier.denylist_pattern(str(short))
    with pytest.raises(builder.BuildError, match="at least 3"):
        builder._denylist_terms(str(short))


def test_builder_scans_for_the_identity_of_the_account_running_it(monkeypatch):
    monkeypatch.delenv(builder.DENYLIST_ENV, raising=False)
    monkeypatch.setattr(builder.getpass, "getuser", lambda: "fixtureaccountname")
    builder.personal_pattern.cache_clear()
    try:
        assert "fixtureaccountname" in builder._local_identity_terms()
        with pytest.raises(builder.BuildError, match="personal_identifier"):
            builder.scan_text("owner: FixtureAccountName", "t")
    finally:
        builder.personal_pattern.cache_clear()
    monkeypatch.setattr(builder.getpass, "getuser", lambda: "root")
    assert "root" not in builder._local_identity_terms()  # generic account names are not treated as personal


def test_repo_relative_rejects_absolute_and_traversal_paths():
    assert builder.repo_relative("tests/dbshell/test_postgresql.py", "t")
    for bad in ("/etc/passwd", "../x", "a/../b", "~/x", ""):
        with pytest.raises(builder.BuildError):
            builder.repo_relative(bad, "t")


def test_runner_summary_accepts_known_forms_and_rejects_anything_else():
    assert builder.runner_summary("x\nRan 10 tests in 0.155s\n\nOK (skipped=1)\n") == {"runner": "django_runtests", "tests_run": 10, "outcome": "OK", "skipped": 1}
    assert builder.runner_summary("====== tests finished: 13 passed, in 7.07 seconds ======")["tests_passed"] == 13
    with pytest.raises(builder.BuildError):
        builder.runner_summary("Ran 3 tests in 0.1s\n\nFAILED (failures=1)\n")
    with pytest.raises(builder.BuildError):
        builder.runner_summary("nothing recognisable")


def _ev(attempts, tool_edits=(), bash_edits=()):
    return {"transcript": {"test_attempts": [{"call_index": i, "state": s, "test_commands_in_call": 1} for i, s in attempts],
                           "edit_tool_calls": [{"call_index": i, "target_kind": k, "tool": "Edit", "is_error": None} for i, k in tool_edits],
                           "bash_edit_like": [{"call_index": i, "classification": ["sed_i"], "target_kinds": [k], "targets_known": True} for i, k in bash_edits]}}


def test_fix_revision_is_not_a_test_expectation_update():
    # failed run, then a SOURCE edit, then a started run: a fix revision
    assert verifier.fix_revision_cycles(_ev([(2, "completed_failed"), (4, "completed_passed")], tool_edits=[(3, "source_file")])) == (1, 0)
    # failed run, then only a TEST-file edit in the rerun's own call: a test-expectation update, not a fix revision
    assert verifier.fix_revision_cycles(_ev([(2, "completed_failed"), (3, "completed_passed")], bash_edits=[(3, "test_file")])) == (0, 1)
    # an edit in the failing run's own call, a failure never followed by an edit, a missing runner: none count
    assert verifier.fix_revision_cycles(_ev([(2, "completed_failed")], bash_edits=[(2, "source_file")])) == (0, 0)
    assert verifier.fix_revision_cycles(_ev([(2, "not_started"), (3, "completed_passed")], tool_edits=[(1, "source_file")])) == (0, 0)


# ---------------------------------------------------------------------------------------------------------------- provenance: builder side
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_NOSYSTEM": "1"}
BUILDER_REL = builder.BUILDER_REL
MAP_REL = builder.HISTORY_MAP_REL
ORIGINAL = "ab" * 20  # the ORIGINAL execution commit: it belongs to the unpublished history and is deliberately absent from every throw-away repository below


def history_map(public: str, original: str = ORIGINAL) -> str:
    return json.dumps({"schema": 1, "mappings": [{"original_commit": original, "public_commit": public, "role": "test"}]}, indent=2) + "\n"


def git(cwd, *args):
    return subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(path: Path, builder_text: str | None = "print('builder')\n", with_map: bool = True) -> tuple[str, str | None]:
    """A throw-away repository: commit E (the PUBLIC equivalent of the original execution commit), then commit B that adds the builder and the committed history mapping
    ORIGINAL -> E (unless with_map is false). Returns (E, B)."""
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / "notes.txt").write_text("execution\n")
    git(path, "add", "notes.txt")
    git(path, "commit", "-q", "-m", "execution")
    e = git(path, "rev-parse", "HEAD")
    if builder_text is None:
        return e, None
    (path / "scripts").mkdir()
    (path / BUILDER_REL).write_text(builder_text)
    git(path, "add", BUILDER_REL)
    if with_map:
        (path / MAP_REL).parent.mkdir(parents=True, exist_ok=True)
        (path / MAP_REL).write_text(history_map(e))
        git(path, "add", MAP_REL)
    git(path, "commit", "-q", "-m", "builder")
    return e, git(path, "rev-parse", "HEAD")


def test_the_builder_state_is_the_committed_builder_on_a_clean_tracked_tree(tmp_path):
    e, b = make_repo(tmp_path / "r")
    (tmp_path / "r" / "untracked-report.md").write_text("an untracked file does not make the tracked tree dirty\n")
    st = builder.committed_builder_state(tmp_path / "r")
    assert st == {"script": BUILDER_REL, "sha256": hashlib.sha256(b"print('builder')\n").hexdigest(), "built_on_commit": b}
    assert st["built_on_commit"] != e  # the generation commit is not the execution commit


def test_the_builder_refuses_a_dirty_tracked_worktree(tmp_path):
    make_repo(tmp_path / "r")
    (tmp_path / "r" / "notes.txt").write_text("changed after the commit\n")
    with pytest.raises(builder.BuildError, match="tracked worktree is dirty"):
        builder.committed_builder_state(tmp_path / "r")


def test_the_builder_refuses_a_staged_but_uncommitted_change(tmp_path):
    make_repo(tmp_path / "r")
    (tmp_path / "r" / "notes.txt").write_text("staged\n")
    git(tmp_path / "r", "add", "notes.txt")
    with pytest.raises(builder.BuildError, match="dirty"):
        builder.committed_builder_state(tmp_path / "r")


def test_the_builder_refuses_when_it_is_not_in_the_head_commit(tmp_path):
    make_repo(tmp_path / "r", builder_text=None)
    (tmp_path / "r" / "scripts").mkdir()
    (tmp_path / "r" / BUILDER_REL).write_text("print('an untracked builder')\n")
    with pytest.raises(builder.BuildError, match="not present in the HEAD commit"):
        builder.committed_builder_state(tmp_path / "r")


def test_the_builder_refuses_when_the_source_differs_from_the_committed_builder(tmp_path):
    make_repo(tmp_path / "r")
    git(tmp_path / "r", "update-index", "--assume-unchanged", BUILDER_REL)  # hides the edit from `git status`, so only the hash comparison can catch it
    (tmp_path / "r" / BUILDER_REL).write_text("print('a different builder')\n")
    assert git(tmp_path / "r", "status", "--porcelain", "--untracked-files=no") == ""
    with pytest.raises(builder.BuildError, match="differs from the builder committed at HEAD"):
        builder.committed_builder_state(tmp_path / "r")


def test_the_builder_refuses_outside_a_git_checkout(tmp_path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(builder.BuildError, match="not a Git checkout"):
        builder.committed_builder_state(tmp_path / "plain")


def test_build_checks_the_builder_before_touching_anything(tmp_path, monkeypatch):
    def refuse(root, rel=BUILDER_REL):
        raise builder.BuildError("the tracked worktree is dirty (1 changed path(s))")
    monkeypatch.setattr(builder, "committed_builder_state", refuse)
    runs = tmp_path / "runs"
    runs.mkdir()
    out = tmp_path / "out" / "bundle"
    with pytest.raises(builder.BuildError, match="dirty"):
        builder.build(runs, out, False)
    assert not out.exists() and not out.parent.exists()


def _rec(commit="a" * 40, dirty=False):
    return {"branch": "main", "commit": commit, "dirty": dirty}


def test_the_execution_commit_comes_from_the_eight_run_records_not_from_git():
    got = builder.execution_record([_rec() for _ in range(8)])
    assert got["harness_commit"] == "a" * 40 and got["runs_checked"] == 8 and got["all_clean"] is True


@pytest.mark.parametrize("records, message", [
    ([_rec("a" * 40)] * 7 + [_rec("b" * 40)], "disagree"),
    ([_rec()] * 7, "expected 8"),
    ([_rec()] * 7 + [_rec(dirty=True)], "not from a clean"),
    ([_rec("abc")] * 8, "40-character"),
    ([_rec(None)] * 8, "40-character"),
])
def test_the_builder_refuses_inconsistent_execution_records(records, message):
    with pytest.raises(builder.BuildError, match=message):
        builder.execution_record(records)


# ---------------------------------------------------------------------------------------------------------------- provenance: verifier side
SHA = hashlib.sha256


def write_manifest(bundle: Path) -> None:
    files = sorted(p for p in bundle.rglob("*") if p.is_file() and p.name != "MANIFEST.sha256")
    (bundle / "MANIFEST.sha256").write_text("".join(f"{SHA(p.read_bytes()).hexdigest()}  {p.relative_to(bundle).as_posix()}\n" for p in files))


def rewrite_provenance(bundle: Path, mutate) -> None:
    p = bundle / "PROVENANCE.json"
    d = json.loads(p.read_text())
    mutate(d)
    p.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")
    write_manifest(bundle)


@pytest.fixture()
def fx(tmp_path):
    """The real bundle's content with a synthetic, valid schema-3 provenance bound to a throw-away repository (O = the original execution commit, absent from the repository; E = its public equivalent; B = builder commit)."""
    assert BUNDLE.is_dir(), "the evidence bundle is missing from this checkout"
    builder_bytes = (ROOT / "scripts" / "build_e1_stage1_evidence.py").read_bytes()
    repo = tmp_path / "repo"
    e, b = make_repo(repo, builder_text=builder_bytes.decode())
    bundle = tmp_path / "bundle"
    shutil.copytree(BUNDLE, bundle)
    for ev in bundle.glob("runs/*/evidence.json"):
        d = json.loads(ev.read_text())
        d["run"]["harness"]["commit"] = ORIGINAL
        ev.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")
    old = json.loads((BUNDLE / "PROVENANCE.json").read_text())
    prov = {"schema": 3, "bundle": "e1-stage1", "experiment_id": "e1-exploratory-1",
            "builder": {"script": BUILDER_REL, "sha256": SHA(builder_bytes).hexdigest(), "built_on_commit": b},
            "execution": {"harness_commit": ORIGINAL, "public_history_commit": e, "runs_checked": 8, "all_clean": True, "source": "run records"},
            "definition": old["definition"], "sanitization": old["sanitization"], "files": old["files"]}
    (bundle / "PROVENANCE.json").write_text(json.dumps(prov, indent=2, sort_keys=True) + "\n")
    write_manifest(bundle)
    return {"bundle": bundle, "repo": repo, "O": ORIGINAL, "E": e, "B": b, "tmp": tmp_path, "report": tmp_path / "no-report.md"}


def run_verifier(fx, capsys, *extra, git_root=None, bundle=None):
    rc = verifier.main(["--bundle", str(bundle or fx["bundle"]), "--git-root", str(git_root or fx["repo"]), "--report", str(fx["report"]), *extra])
    return rc, capsys.readouterr().out


def test_the_fixture_bundle_passes_every_provenance_check(fx, capsys):
    rc, out = run_verifier(fx, capsys)
    assert rc == 0 and "FAIL" not in out
    for name in ("builder.sha256 equals the builder file committed at builder.built_on_commit", "execution.public_history_commit exists in this repository's history",
                 "builder.built_on_commit descends from execution.public_history_commit", "the mapping committed at builder.built_on_commit ties", "exactly the expected schema"):
        assert any(l.startswith("  PASS") and name in l for l in out.splitlines()), name
    assert "SKIP  the research report states the same execution commit" in out  # no report was supplied: reported as SKIP, not PASS


def test_the_report_must_state_the_same_execution_commit(fx, capsys):
    real = (ROOT / "docs" / "research" / "E1_STAGE1_REPORT.md").read_text()
    exec_commit = "28e758e5f3ccf4598fe843fe4a3abbed3628eb83"
    assert exec_commit in real
    fx["report"].write_text(real.replace(exec_commit, fx["O"]))  # the real report, with its (original) execution commit replaced by the fixture's
    rc, out = run_verifier(fx, capsys)
    assert rc == 0 and any(l.startswith("  PASS") and "report states the same execution commit" in l for l in out.splitlines())
    fx["report"].write_text(real.replace(exec_commit, "9" * 40))
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and any(l.startswith("  FAIL") and "report states the same execution commit" in l for l in out.splitlines())


MUTATIONS = {
    "builder.sha256 changed": (lambda p, fx: p["builder"].__setitem__("sha256", "0" * 64), "builder.sha256 equals the builder file committed"),
    "builder.sha256 in upper case": (lambda p, fx: p["builder"].__setitem__("sha256", p["builder"]["sha256"].upper()), "exactly the expected schema"),
    "builder.built_on_commit unknown": (lambda p, fx: p["builder"].__setitem__("built_on_commit", "1" * 40), "builder.sha256 equals the builder file committed"),
    "builder.built_on_commit lacks the builder": (lambda p, fx: p["builder"].__setitem__("built_on_commit", fx["E"]), "builder.sha256 equals the builder file committed"),
    "builder.built_on_commit abbreviated": (lambda p, fx: p["builder"].__setitem__("built_on_commit", p["builder"]["built_on_commit"][:7]), "exactly the expected schema"),
    "builder.script renamed": (lambda p, fx: p["builder"].__setitem__("script", "scripts/other.py"), "exactly the expected schema"),
    "builder gains the legacy field": (lambda p, fx: p["builder"].__setitem__("harness_head_commit", fx["B"]), "exactly the expected schema"),
    "execution.harness_commit changed": (lambda p, fx: p["execution"].__setitem__("harness_commit", "2" * 40), "execution.harness_commit equals the harness commit recorded"),
    "execution.runs_checked changed": (lambda p, fx: p["execution"].__setitem__("runs_checked", 7), "exactly the expected schema"),
    "execution.all_clean false": (lambda p, fx: p["execution"].__setitem__("all_clean", False), "exactly the expected schema"),
    "definition.definition_sha256 changed": (lambda p, fx: p["definition"].__setitem__("definition_sha256", "3" * 64), "provenance.definition hashes equal the locked definition"),
    "definition.file_sha256 changed": (lambda p, fx: p["definition"].__setitem__("file_sha256", "4" * 64), "provenance.definition hashes equal the locked definition"),
    "schema is the legacy 1": (lambda p, fx: p.__setitem__("schema", 1), "exactly the expected schema"),
    "schema is the legacy 2": (lambda p, fx: p.__setitem__("schema", 2), "exactly the expected schema"),
    "public_history_commit missing": (lambda p, fx: p["execution"].pop("public_history_commit"), "exactly the expected schema"),
    "public_history_commit abbreviated": (lambda p, fx: p["execution"].__setitem__("public_history_commit", fx["E"][:7]), "exactly the expected schema"),
    "public_history_commit not hexadecimal": (lambda p, fx: p["execution"].__setitem__("public_history_commit", "z" * 40), "exactly the expected schema"),
    "public_history_commit upper case": (lambda p, fx: p["execution"].__setitem__("public_history_commit", fx["E"].upper()), "exactly the expected schema"),
    "public_history_commit not a string": (lambda p, fx: p["execution"].__setitem__("public_history_commit", 7), "exactly the expected schema"),
    "public_history_commit nonexistent": (lambda p, fx: p["execution"].__setitem__("public_history_commit", "5" * 40), "execution.public_history_commit exists in this repository's history"),
    "public_history_commit differs from the committed mapping": (lambda p, fx: p["execution"].__setitem__("public_history_commit", fx["B"]), "the mapping committed at builder.built_on_commit ties"),
    "public_history_commit gains a sibling field": (lambda p, fx: p["execution"].__setitem__("harness_head_commit", fx["E"]), "exactly the expected schema"),
    "unexpected top-level field": (lambda p, fx: p.__setitem__("extra", 1), "exactly the expected schema"),
    "missing top-level field": (lambda p, fx: p.pop("execution"), "exactly the expected schema"),
    "wrong bundle name": (lambda p, fx: p.__setitem__("bundle", "e1-stage2"), "exactly the expected schema"),
    "a files entry removed": (lambda p, fx: p["files"].pop("batch/state.json"), "provenance.files describes exactly the other bundle files"),
    "a files entry invented": (lambda p, fx: p["files"].__setitem__("bogus.txt", {"treatment": "generated", "sources": []}), "provenance.files describes exactly the other bundle files"),
    "unknown treatment": (lambda p, fx: p["files"]["batch/state.json"].__setitem__("treatment", "magic"), "exactly the expected schema"),
    "absolute source path": (lambda p, fx: p["files"]["batch/state.json"]["sources"][0].__setitem__("path", "/etc/passwd"), "exactly the expected schema"),
    "verbatim source hash changed": (lambda p, fx: p["files"]["batch/state.json"]["sources"][0].__setitem__("sha256", "5" * 64), "every recorded source hash agrees"),
    "derived source hash changed": (lambda p, fx: next(v for k, v in p["files"].items() if k.endswith("evidence.json"))["sources"][1].__setitem__("sha256", "6" * 64), "every recorded source hash agrees"),
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_every_mutated_provenance_field_is_detected_even_with_a_regenerated_manifest(fx, capsys, name):
    mutate, expected = MUTATIONS[name]
    rewrite_provenance(fx["bundle"], lambda p: mutate(p, fx))
    rc, out = run_verifier(fx, capsys)
    assert rc == 1, name
    assert any(l.startswith("  FAIL") and expected in l for l in out.splitlines()), (name, [l for l in out.splitlines() if "FAIL" in l])


def test_invalid_json_in_provenance_is_a_failure_not_a_crash(fx, capsys):
    (fx["bundle"] / "PROVENANCE.json").write_text("{not json")
    write_manifest(fx["bundle"])
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and "PROVENANCE.json is readable JSON" in out


def test_a_changed_execution_commit_in_one_evidence_file_is_detected(fx, capsys):
    ev = next(fx["bundle"].glob("runs/*/evidence.json"))
    d = json.loads(ev.read_text())
    d["run"]["harness"]["commit"] = "7" * 40
    ev.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")
    write_manifest(fx["bundle"])
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and any(l.startswith("  FAIL") and "execution.harness_commit equals the harness commit recorded" in l for l in out.splitlines())


def test_a_wrong_manifest_hash_for_provenance_is_detected(fx, capsys):
    lines = (fx["bundle"] / "MANIFEST.sha256").read_text().splitlines()
    lines = [("0" * 64 + "  PROVENANCE.json") if l.endswith("  PROVENANCE.json") else l for l in lines]
    (fx["bundle"] / "MANIFEST.sha256").write_text("\n".join(lines) + "\n")
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and any(l.startswith("  FAIL") and "listed files match their sha256" in l for l in out.splitlines())
    assert any(l.startswith("  FAIL") and "lists PROVENANCE.json and its sha256 is correct" in l for l in out.splitlines())


def test_removing_provenance_from_the_manifest_is_detected(fx, capsys):
    lines = [l for l in (fx["bundle"] / "MANIFEST.sha256").read_text().splitlines() if not l.endswith("  PROVENANCE.json")]
    (fx["bundle"] / "MANIFEST.sha256").write_text("\n".join(lines) + "\n")
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and any(l.startswith("  FAIL") and "lists PROVENANCE.json and its sha256 is correct" in l for l in out.splitlines())


def test_the_legacy_provenance_schema_is_rejected_with_a_clear_message(fx, capsys):
    legacy = {"schema": 1, "bundle": "e1-stage1", "experiment_id": "e1-exploratory-1", "builder": {"script": BUILDER_REL, "sha256": "a" * 64, "harness_head_commit": fx["E"]},
              "definition": {}, "sanitization": {}, "files": {}}
    (fx["bundle"] / "PROVENANCE.json").write_text(json.dumps(legacy))
    write_manifest(fx["bundle"])
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and "legacy provenance schema 1" in out


def test_missing_git_history_is_reported_as_skip_never_pass(fx, capsys):
    (fx["tmp"] / "nogit").mkdir()
    rc, out = run_verifier(fx, capsys, git_root=fx["tmp"] / "nogit")
    assert rc == 0
    for name in ("builder.sha256 equals the builder file committed", "execution.public_history_commit exists in this repository's history", "descends from execution.public_history_commit",
                 "the mapping committed at builder.built_on_commit ties"):
        assert any(l.startswith("  SKIP") and name in l for l in out.splitlines()), name
        assert not any(l.startswith("  PASS") and name in l for l in out.splitlines()), name
    assert "5 SKIPPED" in out  # the four history checks and the absent report
    rc, out = run_verifier(fx, capsys, "--require-git", git_root=fx["tmp"] / "nogit")
    assert rc == 1 and any(l.startswith("  FAIL") and "(--require-git)" in l for l in out.splitlines())


def test_a_shallow_clone_checks_what_it_has_and_skips_what_it_lacks(fx, capsys):
    shallow = fx["tmp"] / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + str(fx["repo"]), str(shallow)], env=GIT_ENV, check=True, capture_output=True)
    rc, out = run_verifier(fx, capsys, git_root=shallow)
    lines = out.splitlines()
    assert rc == 0
    assert any(l.startswith("  PASS") and "builder.sha256 equals the builder file committed" in l for l in lines)  # B is in a depth-1 clone
    assert any(l.startswith("  SKIP") and "execution.public_history_commit exists in this repository's history" in l for l in lines)  # E (its parent) is not
    assert any(l.startswith("  SKIP") and "descends from execution.public_history_commit" in l for l in lines)
    assert any(l.startswith("  PASS") and "the mapping committed at builder.built_on_commit ties" in l for l in lines)  # the mapping is read from B, which the clone has


def test_a_full_repository_that_lacks_the_recorded_commits_fails_instead_of_skipping(fx, capsys):
    other = fx["tmp"] / "other"
    make_repo(other, builder_text="print('another builder')\n")
    rc, out = run_verifier(fx, capsys, git_root=other)
    assert rc == 1 and any(l.startswith("  FAIL") and "does not exist" in l for l in lines_with_detail(out))


def lines_with_detail(out):
    return [l for l in out.splitlines() if l.startswith("  FAIL")]


def test_the_builder_the_verifier_checks_is_the_committed_one_not_the_current_file(fx, capsys):
    # editing the builder after the fact does not matter: the verifier compares against the blob at built_on_commit, never against the working copy
    (fx["repo"] / BUILDER_REL).write_text("print('edited later')\n")
    rc, out = run_verifier(fx, capsys)
    assert rc == 0 and any(l.startswith("  PASS") and "builder.sha256 equals the builder file committed" in l for l in out.splitlines())


# ---------------------------------------------------------------------------------------------------------------- the real bundle
def test_the_evidence_bundle_is_present():
    assert (BUNDLE / "MANIFEST.sha256").is_file() and (BUNDLE / "PROVENANCE.json").is_file()


REPORT = ROOT / "docs" / "research" / "E1_STAGE1_REPORT.md"
REPORT_SHA256 = "8914aa8a3654a3807ed555467356d37f0ab47ca3de25ef496de452666d0c797a"  # the frozen report; it cites the ORIGINAL execution commit and must never be edited


def test_the_frozen_report_is_byte_identical_to_its_recorded_hash():
    assert hashlib.sha256(REPORT.read_bytes()).hexdigest() == REPORT_SHA256


def test_the_real_bundle_carries_the_current_provenance_schema():
    prov = json.loads((BUNDLE / "PROVENANCE.json").read_text())
    assert prov["schema"] == builder.PROVENANCE_SCHEMA == 3
    assert verifier.provenance_schema_problems(prov) == []
    assert prov["execution"]["harness_commit"] == "28e758e5f3ccf4598fe843fe4a3abbed3628eb83"  # the original execution commit, as recorded by the eight runs


def test_the_real_bundle_verifies(capsys):
    rc = verifier.main(["--bundle", str(BUNDLE)])
    out = capsys.readouterr().out
    assert rc == 0 and "FAIL" not in out, [l for l in out.splitlines() if "FAIL" in l]


def test_the_real_bundle_verifies_without_a_single_skipped_check_when_full_history_is_available(capsys):
    if verifier.git_availability(ROOT) != "full":
        pytest.skip("this checkout has no complete Git history (a shallow clone or no repository); the history-dependent checks would be SKIPped by design")
    rc = verifier.main(["--bundle", str(BUNDLE), "--require-git"])
    out = capsys.readouterr().out
    assert rc == 0 and "FAIL" not in out and "SKIP" not in out
    assert "65 of 65 checks passed" in out


@pytest.mark.parametrize("schema", [1, 2])
def test_genuinely_legacy_provenance_schemas_are_still_rejected_on_a_copy_of_the_real_bundle(tmp_path, capsys, schema):
    bundle = tmp_path / "bundle"
    shutil.copytree(BUNDLE, bundle)
    prov = json.loads((bundle / "PROVENANCE.json").read_text())
    prov["schema"] = schema
    if schema == 2:
        prov["execution"].pop("public_history_commit")
    (bundle / "PROVENANCE.json").write_text(json.dumps(prov, indent=2, sort_keys=True) + "\n")
    write_manifest(bundle)
    rc = verifier.main(["--bundle", str(bundle)])
    out = capsys.readouterr().out
    assert rc == 1 and f"legacy provenance schema {schema}" in out


def test_the_verifier_catches_a_tampered_ledger_even_when_the_manifest_is_regenerated(fx, capsys):
    ledger = fx["bundle"] / "runs" / "m2-sympy__sympy-18621-A-20261123T171501Z" / "gateway_ledger.jsonl"
    text = ledger.read_text()
    assert '"output_tokens": 124' in text
    ledger.write_text(text.replace('"output_tokens": 124', '"output_tokens": 125', 1))
    write_manifest(fx["bundle"])
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and "FAIL" in out


# ---------------------------------------------------------------------------------------------------------------- damaged input: a clear FAIL, never a traceback
def _fails(out):
    return [l for l in out.splitlines() if l.startswith("  FAIL")]


def _first_evidence(fx):
    return sorted(fx["bundle"].glob("runs/*/evidence.json"))[0]


def _edit_json(path: Path, mutate) -> None:
    d = json.loads(path.read_text())
    mutate(d)
    path.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")


def _drop(*keys):
    def f(d):
        for k in keys[:-1]:
            d = d[k]
        del d[keys[-1]]
    return f


def test_a_missing_manifest_is_a_failure_not_a_traceback(fx, capsys):
    (fx["bundle"] / "MANIFEST.sha256").unlink()
    rc, out = run_verifier(fx, capsys)  # an uncaught exception would propagate out of main() and fail this test
    assert rc == 1 and any("MANIFEST.sha256 exists" in l for l in _fails(out)) and "checks passed" in out


@pytest.mark.parametrize("damage", ["garbage line", "short-hash  PROVENANCE.json", ("a" * 64) + " one-space.json", ("a" * 64) + "  /abs/path", ("a" * 64) + "  ../escape", ""])
def test_malformed_manifest_lines_are_a_failure(fx, capsys, damage):
    mp = fx["bundle"] / "MANIFEST.sha256"
    mp.write_text(mp.read_text() + damage + "\n")
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 or damage == ""  # a trailing blank line is tolerated; every real malformation must fail
    if damage:
        assert any("every MANIFEST.sha256 line" in l for l in _fails(out)), damage


def test_a_duplicate_manifest_entry_is_a_failure(fx, capsys):
    mp = fx["bundle"] / "MANIFEST.sha256"
    first = mp.read_text().splitlines()[0]
    mp.write_text(mp.read_text() + first + "\n")
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and any("every MANIFEST.sha256 line" in l for l in _fails(out))


def test_an_empty_or_binary_manifest_is_a_failure(fx, capsys):
    mp = fx["bundle"] / "MANIFEST.sha256"
    mp.write_text("")
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and _fails(out)
    mp.write_bytes(b"\xff\xfe\x00binary")
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and any("MANIFEST.sha256" in l for l in _fails(out))


def test_a_missing_evidence_file_is_a_failure(fx, capsys):
    ev = _first_evidence(fx)
    ev.unlink()
    write_manifest(fx["bundle"])
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and any("every file the checks read exists" in l and "evidence.json: missing" in l for l in _fails(out))


@pytest.mark.parametrize("name, mutate, message", [
    ("no run.harness", _drop("run", "harness"), "run.harness is missing"),
    ("harness lacks commit", _drop("run", "harness", "commit"), "run.harness is missing"),
    ("no withheld_artifacts", _drop("run", "withheld_artifacts"), "withheld_artifacts is missing"),
    ("no run object", _drop("run"), "no 'run' object"),
    ("withheld_artifacts wrong type", lambda d: d["run"].__setitem__("withheld_artifacts", []), "withheld_artifacts is missing"),
])
def test_an_incomplete_evidence_file_is_a_failure(fx, capsys, name, mutate, message):
    _edit_json(_first_evidence(fx), mutate)
    write_manifest(fx["bundle"])
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and any(message in l for l in _fails(out)), name


def test_an_evidence_file_that_is_not_json_is_a_failure(fx, capsys):
    _first_evidence(fx).write_text("{broken")
    write_manifest(fx["bundle"])
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and any("not valid JSON" in l for l in _fails(out))


@pytest.mark.parametrize("rel", ["batch/state.json", "batch/results.jsonl"])
def test_a_missing_batch_file_is_a_failure(fx, capsys, rel):
    (fx["bundle"] / rel).unlink()
    write_manifest(fx["bundle"])
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and any(rel in l and "missing" in l for l in _fails(out))


def test_a_malformed_results_row_is_a_failure(fx, capsys):
    p = fx["bundle"] / "batch" / "results.jsonl"
    p.write_text(p.read_text() + "not json\n")
    write_manifest(fx["bundle"])
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and any("results.jsonl" in l for l in _fails(out))


def test_a_missing_provenance_file_is_a_failure(fx, capsys):
    (fx["bundle"] / "PROVENANCE.json").unlink()
    write_manifest(fx["bundle"])
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and any("PROVENANCE.json" in l for l in _fails(out))


@pytest.mark.parametrize("key", ["schema", "bundle", "experiment_id", "builder", "execution", "definition", "sanitization", "files"])
def test_a_provenance_file_missing_a_required_top_level_field_is_a_failure(fx, capsys, key):
    rewrite_provenance(fx["bundle"], lambda p: p.pop(key))
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and any("exactly the expected schema" in l and "missing fields" in l for l in _fails(out)), key


@pytest.mark.parametrize("path", [("builder", "sha256"), ("builder", "built_on_commit"), ("builder", "script"), ("execution", "harness_commit"), ("execution", "runs_checked"),
                                  ("execution", "all_clean"), ("execution", "source"), ("definition", "definition_sha256"), ("definition", "file_sha256"), ("definition", "path"),
                                  ("sanitization", "hidden_data_scan"), ("sanitization", "statement")])
def test_a_provenance_file_missing_a_required_nested_field_is_a_failure(fx, capsys, path):
    rewrite_provenance(fx["bundle"], lambda p: p[path[0]].pop(path[1]))
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and any("exactly the expected schema" in l for l in _fails(out)), path


def test_a_provenance_file_that_is_not_an_object_is_a_failure(fx, capsys):
    (fx["bundle"] / "PROVENANCE.json").write_text("[1, 2, 3]")
    write_manifest(fx["bundle"])
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and any("exactly the expected schema" in l for l in _fails(out))


def test_every_damage_case_leaves_the_exit_code_nonzero_and_prints_a_summary(fx, capsys):
    (fx["bundle"] / "batch" / "state.json").write_text("null")  # parses, but is not an object
    write_manifest(fx["bundle"])
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and "FAILED:" in out


# ---------------------------------------------------------------------------------------------------------------- the real build() feeding the real verifier
def test_the_builders_own_output_is_accepted_by_the_verifier_end_to_end(tmp_path, monkeypatch, capsys):
    """Runs the real build() (its guard, committed-builder check, scans, execution record, provenance assembly, manifest and writing) in a throw-away Git repository and verifies
    its output with the real verifier. Only collect(), the reader of the private run directories, is replaced: it supplies the real bundle's already-sanitized content, with the
    eight execution records carrying the throw-away repository's execution commit. Offline; the real bundle is read, never written."""
    assert BUNDLE.is_dir()
    repo = tmp_path / "repo"
    builder_src = (ROOT / builder.BUILDER_REL).read_text()
    e, _ = make_repo(repo, builder_text=builder_src)
    (repo / builder.DEFINITION_PATH).parent.mkdir(parents=True)
    shutil.copyfile(ROOT / builder.DEFINITION_PATH, repo / builder.DEFINITION_PATH)
    git(repo, "add", builder.DEFINITION_PATH.as_posix())
    git(repo, "commit", "-q", "-m", "definition")
    head = git(repo, "rev-parse", "HEAD")
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""

    legacy = json.loads((BUNDLE / "PROVENANCE.json").read_text())
    rows = [json.loads(l) for l in (BUNDLE / "batch" / "results.jsonl").read_text().splitlines() if l.strip()]

    def stand_in_for_the_private_reader(runs_root):
        files, prov, harness = {}, {}, []
        for name, entry in legacy["files"].items():
            if name == "README.md":
                continue  # build() writes its own
            data = (BUNDLE / name).read_bytes()
            if name.endswith("/evidence.json"):
                ev = json.loads(data)
                ev["run"]["harness"]["commit"] = ORIGINAL
                harness.append(ev["run"]["harness"])
                data = builder.dumps(ev).encode()
            files[name], prov[name] = data, entry
        return {"files": files, "prov": prov, "rows": rows, "sources": [], "records": {}, "harness_records": harness}

    monkeypatch.setattr(builder, "ROOT", repo)
    monkeypatch.setattr(builder, "__file__", str(repo / builder.BUILDER_REL))
    monkeypatch.setattr(builder, "collect", stand_in_for_the_private_reader)
    runs, out = tmp_path / "runs", tmp_path / "generated"
    runs.mkdir()
    result = builder.build(runs, out, False)
    assert result["files"] == len(list(out.rglob("*"))) - len([d for d in out.rglob("*") if d.is_dir()])

    prov = json.loads((out / "PROVENANCE.json").read_text())
    assert prov["schema"] == 3 and set(prov) == verifier.PROVENANCE_KEYS
    assert prov["builder"] == {"script": builder.BUILDER_REL, "sha256": hashlib.sha256(builder_src.encode()).hexdigest(), "built_on_commit": head}
    assert prov["execution"]["harness_commit"] == ORIGINAL and prov["execution"]["public_history_commit"] == e and e != head
    assert prov["execution"]["runs_checked"] == 8 and prov["execution"]["all_clean"] is True
    assert subprocess.run(["git", "-C", str(repo), "cat-file", "-e", ORIGINAL + "^{commit}"], capture_output=True).returncode != 0  # the original commit is deliberately not in this repository
    assert verifier.provenance_schema_problems(prov) == []  # the builder and the verifier agree on every field, type and format
    manifest = dict(reversed(l.split("  ", 1)) for l in (out / "MANIFEST.sha256").read_text().splitlines())
    assert manifest["PROVENANCE.json"] == hashlib.sha256((out / "PROVENANCE.json").read_bytes()).hexdigest()
    assert set(manifest) == {str(p.relative_to(out)) for p in out.rglob("*") if p.is_file() and p.name != "MANIFEST.sha256"}

    report = tmp_path / "report.md"
    report.write_text((ROOT / "docs" / "research" / "E1_STAGE1_REPORT.md").read_text().replace("28e758e5f3ccf4598fe843fe4a3abbed3628eb83", ORIGINAL))
    rc = verifier.main(["--bundle", str(out), "--git-root", str(repo), "--report", str(report), "--require-git"])
    text = capsys.readouterr().out
    assert rc == 0 and "FAIL" not in text and "SKIP" not in text, [l for l in text.splitlines() if "FAIL" in l or "SKIP" in l]
    for name in ("exactly the expected schema", "lists PROVENANCE.json and its sha256 is correct", "builder.sha256 equals the builder file committed at builder.built_on_commit",
                 "execution.harness_commit equals the harness commit recorded in all eight evidence.json", "builder.built_on_commit descends from execution.public_history_commit",
                 "execution.public_history_commit exists in this repository's history", "the mapping committed at builder.built_on_commit ties",
                 "the research report states the same execution commit"):
        assert any(l.startswith("  PASS") and name in l for l in text.splitlines()), name

    # the real bundle and the real repository were not touched
    assert json.loads((BUNDLE / "PROVENANCE.json").read_text()) == legacy


# ---------------------------------------------------------------------------------------------------------------- schema 3: original versus public-history execution commit
def _commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def test_public_history_commit_is_read_from_the_committed_mapping_and_checked(tmp_path):
    e, b = make_repo(tmp_path / "r")
    assert builder.public_history_commit(tmp_path / "r", ORIGINAL) == e
    assert e != ORIGINAL and e != b


def test_an_unmapped_original_commit_stops_the_build(tmp_path):
    make_repo(tmp_path / "r")
    with pytest.raises(builder.BuildError, match="exactly one entry"):
        builder.public_history_commit(tmp_path / "r", "cd" * 20)


def test_a_mapping_that_is_not_committed_stops_the_build(tmp_path):
    make_repo(tmp_path / "r", with_map=False)
    (tmp_path / "r" / MAP_REL).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "r" / MAP_REL).write_text(history_map("1" * 40))  # present on disk, not in HEAD
    with pytest.raises(builder.BuildError, match="not committed at HEAD"):
        builder.public_history_commit(tmp_path / "r", ORIGINAL)


def test_a_working_copy_mapping_that_differs_from_the_committed_one_stops_the_build(tmp_path):
    e, _ = make_repo(tmp_path / "r")
    (tmp_path / "r" / MAP_REL).write_text(history_map("1" * 40))
    with pytest.raises(builder.BuildError, match="differs from the copy committed"):
        builder.public_history_commit(tmp_path / "r", ORIGINAL)


@pytest.mark.parametrize("mapped, message", [("1" * 40, "does not exist"), ("abc", "40-character"), ("E" * 40, "40-character"), (None, "40-character"), (123, "40-character")])
def test_a_mapped_public_commit_must_be_a_well_formed_commit_that_exists(tmp_path, mapped, message):
    make_repo(tmp_path / "r")
    repo = tmp_path / "r"
    (repo / MAP_REL).write_text(json.dumps({"schema": 1, "mappings": [{"original_commit": ORIGINAL, "public_commit": mapped}]}))
    _commit_all(repo, "bad map")
    with pytest.raises(builder.BuildError, match=message):
        builder.public_history_commit(repo, ORIGINAL)


def test_a_mapped_commit_that_is_not_an_ancestor_of_head_stops_the_build(tmp_path):
    e, _ = make_repo(tmp_path / "r")
    repo = tmp_path / "r"
    git(repo, "checkout", "-q", "-b", "side", e)
    (repo / "side.txt").write_text("x\n")
    side = _commit_all(repo, "side")
    git(repo, "checkout", "-q", "main")
    (repo / MAP_REL).write_text(history_map(side))
    _commit_all(repo, "map to a commit outside the HEAD history")
    with pytest.raises(builder.BuildError, match="not an ancestor of HEAD"):
        builder.public_history_commit(repo, ORIGINAL)


@pytest.mark.parametrize("text", ["not json", "[]", '{"schema": 2, "mappings": []}', '{"schema": 1}', '{"schema": 1, "mappings": [1]}'])
def test_a_malformed_or_wrongly_versioned_mapping_stops_the_build(tmp_path, text):
    make_repo(tmp_path / "r")
    repo = tmp_path / "r"
    (repo / MAP_REL).write_text(text)
    _commit_all(repo, "malformed map")
    with pytest.raises(builder.BuildError):
        builder.public_history_commit(repo, ORIGINAL)


def test_two_mapping_entries_for_the_same_original_commit_are_refused(tmp_path):
    e, _ = make_repo(tmp_path / "r")
    repo = tmp_path / "r"
    (repo / MAP_REL).write_text(json.dumps({"schema": 1, "mappings": [{"original_commit": ORIGINAL, "public_commit": e}, {"original_commit": ORIGINAL, "public_commit": e}]}))
    _commit_all(repo, "duplicate")
    with pytest.raises(builder.BuildError, match="exactly one entry"):
        builder.public_history_commit(repo, ORIGINAL)


def _provenance_at_new_builder_commit(fx, mapping_text):
    """A new commit B2 in the fixture repository whose committed mapping is ``mapping_text`` (None removes it); the builder file is unchanged, so only the mapping check can notice."""
    repo = fx["repo"]
    if mapping_text is None:
        git(repo, "rm", "-q", MAP_REL)
    else:
        (repo / MAP_REL).write_text(mapping_text)
    b2 = _commit_all(repo, "change mapping")
    rewrite_provenance(fx["bundle"], lambda p: p["builder"].__setitem__("built_on_commit", b2))
    return b2


@pytest.mark.parametrize("mapping, why", [
    (None, "not committed at builder.built_on_commit"),
    ("not json", "malformed"),
    (json.dumps({"schema": 1, "mappings": []}), "no single entry"),
    (json.dumps({"schema": 1, "mappings": [{"original_commit": "cd" * 20, "public_commit": "x"}]}), "no single entry"),
    (json.dumps({"schema": 2, "mappings": []}), "no single entry"),
])
def test_the_verifier_rejects_a_missing_or_disagreeing_committed_mapping(fx, capsys, mapping, why):
    _provenance_at_new_builder_commit(fx, mapping)
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and any("the mapping committed at builder.built_on_commit ties" in l and why in l for l in _fails(out)), (why, _fails(out))


def test_the_verifier_rejects_a_mapping_that_names_another_public_commit(fx, capsys):
    _provenance_at_new_builder_commit(fx, history_map(fx["B"]))  # maps the original commit to B instead of E, while provenance says E
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and any("the mapping committed at builder.built_on_commit ties" in l for l in _fails(out))


def test_the_original_execution_commit_is_not_looked_up_in_git_but_the_public_one_is(fx, capsys):
    assert subprocess.run(["git", "-C", str(fx["repo"]), "cat-file", "-e", ORIGINAL + "^{commit}"], capture_output=True).returncode != 0
    rc, out = run_verifier(fx, capsys)
    assert rc == 0 and "FAIL" not in out
    rewrite_provenance(fx["bundle"], lambda p: p["execution"].__setitem__("public_history_commit", ORIGINAL))  # pointing the public field at the absent original must fail
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and any("execution.public_history_commit exists" in l for l in _fails(out))


def test_schema_2_provenance_is_rejected_as_legacy(fx, capsys):
    rewrite_provenance(fx["bundle"], lambda p: (p.__setitem__("schema", 2), p["execution"].pop("public_history_commit")))
    rc, out = run_verifier(fx, capsys)
    assert rc == 1 and "legacy provenance schema 2" in out and "no execution.public_history_commit" in out


def test_inconsistent_original_commits_across_the_eight_run_records_are_refused_by_the_builder():
    records = [{"commit": ORIGINAL, "dirty": False}] * 7 + [{"commit": "cd" * 20, "dirty": False}]
    with pytest.raises(builder.BuildError, match="disagree"):
        builder.execution_record(records)

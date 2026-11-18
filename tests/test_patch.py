import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from aiep.patch import (HARDENED_GIT, MAX_PATCH_BYTES, PatchResult, extract_patch,
                        trusted_local_git_runner, validate_patch, write_predictions)

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": "/dev/null",
       "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    git(d, "init", "-q")
    (d / "a.py").write_text("print('a')\n")
    (d / "keep.txt").write_text("keep\n")
    (d / "gone.txt").write_text("gone\n")
    (d / "sub").mkdir()
    (d / "sub" / "b.py").write_text("b = 1\n")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "base")
    return d, git(d, "rev-parse", "HEAD")


def extract(repo_tuple, **kw):
    d, base = repo_tuple
    return extract_patch(trusted_local_git_runner(d), base, **kw)


# ---------------- extraction ----------------
def test_modified_new_and_deleted_files_all_captured(repo):
    d, _ = repo
    (d / "a.py").write_text("print('changed')\n")
    (d / "new_file.py").write_text("x = 1\n")  # untracked
    (d / "sub" / "deep.py").write_text("y = 2\n")
    (d / "gone.txt").unlink()
    r = extract(repo)
    assert r.status == "ok" and r.sha256 and r.size == len(r.patch.encode())
    for name in ("a/a.py", "b/new_file.py", "b/sub/deep.py", "a/gone.txt"):
        assert name in r.patch


def test_patch_round_trips_onto_a_pristine_checkout(repo, tmp_path):
    d, base = repo
    (d / "a.py").write_text("print('changed')\n")
    (d / "new_file.py").write_text("x = 1\n")
    (d / "gone.txt").unlink()
    r = extract(repo)
    fresh = tmp_path / "fresh"
    git(tmp_path, "clone", "-q", str(d), "fresh")
    git(fresh, "checkout", "-q", base)
    (fresh / "p.diff").write_text(r.patch)
    git(fresh, "apply", "--check", "p.diff")
    git(fresh, "apply", "p.diff")
    assert (fresh / "a.py").read_text() == "print('changed')\n" and (fresh / "new_file.py").exists()
    assert not (fresh / "gone.txt").exists()


def test_no_changes_is_empty(repo):
    assert extract(repo).status == "empty"


def test_mode_only_change_is_ignored_like_m1(repo):
    d, _ = repo
    (d / "a.py").chmod((d / "a.py").stat().st_mode | stat.S_IXUSR)
    assert extract(repo).status == "empty"


def test_agent_commits_and_resets_cannot_hide_changes(repo):
    d, base = repo
    (d / "a.py").write_text("print('committed')\n")
    git(d, "commit", "-qam", "agent commit")
    (d / "new.py").write_text("z = 1\n")
    r = extract(repo)
    assert r.status == "ok" and "committed" in r.patch and "b/new.py" in r.patch
    git(d, "reset", "-q", "--soft", base)  # agent moves HEAD back; still diffed against the fixed base
    assert "committed" in extract(repo).patch


def test_ignored_files_are_not_included(repo):
    d, _ = repo
    (d / ".gitignore").write_text("*.log\n")
    (d / "debug.log").write_text("noise\n")
    r = extract(repo)
    assert r.status == "ok" and "debug.log" not in r.patch and ".gitignore" in r.patch


def test_binary_file_is_rejected(repo):
    d, _ = repo
    (d / "img.bin").write_bytes(bytes(range(256)) * 4)
    r = extract(repo)
    assert r.status == "rejected" and any("binary" in x for x in r.reasons) and r.patch is None


def test_symlink_is_rejected(repo):
    d, _ = repo
    os.symlink("/etc/passwd", d / "link")
    r = extract(repo)
    assert r.status == "rejected" and any("120000" in x for x in r.reasons)


def test_oversized_patch_is_rejected(repo):
    d, _ = repo
    (d / "big.txt").write_text("line\n" * 5000)
    r = extract(repo, max_bytes=1000)
    assert r.status == "rejected" and "limit" in r.reasons[0]


def test_non_hex_base_commit_refused(repo):
    d, _ = repo
    for bad in ("HEAD", "main", "abc", "--output=/tmp/x", "G" * 40):
        with pytest.raises(ValueError):
            extract_patch(trusted_local_git_runner(d), bad)


def test_executor_sees_only_the_fixed_hardened_commands(repo):
    d, base = repo
    (d / "a.py").write_text("x\n")
    seen = []
    real = trusted_local_git_runner(d)
    extract_patch(lambda a: (seen.append(a), real(a))[1], base)
    assert [s[len(HARDENED_GIT)] for s in seen] == ["add", "diff"]
    assert all(s[:len(HARDENED_GIT)] == HARDENED_GIT for s in seen)
    assert "--no-ext-diff" in seen[1] and "--no-textconv" in seen[1]


# ---------------- hostile repository config ----------------
def _marker_repo(repo_tuple, tmp_path):
    d, _ = repo_tuple
    marker = tmp_path / "PWNED"
    (d / "a.py").write_text("print('changed')\n")
    return d, marker


def test_fsmonitor_command_in_repo_config_does_not_run(repo, tmp_path):
    d, marker = _marker_repo(repo, tmp_path)
    git(d, "config", "core.fsmonitor", f"touch {marker}; echo")
    extract(repo)
    assert not marker.exists()


def test_post_index_change_hook_does_not_run(repo, tmp_path):
    d, marker = _marker_repo(repo, tmp_path)
    hook = d / ".git" / "hooks" / "post-index-change"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    # control: without our flags the hook really does fire (so this test means something)
    subprocess.run(["git", "add", "-A", "-N"], cwd=d, env=ENV, check=True, capture_output=True)
    assert marker.exists(), "control failed: hook did not fire, test is not meaningful"
    git(d, "reset", "-q")  # plain git again: this re-fires the hook, so clear the marker afterwards
    marker.unlink(missing_ok=True)
    extract(repo)
    assert not marker.exists()


def test_textconv_and_external_diff_do_not_run(repo, tmp_path):
    d, marker = _marker_repo(repo, tmp_path)
    (d / ".gitattributes").write_text("*.py diff=evil\n")
    git(d, "config", "diff.evil.textconv", f"sh -c 'touch {marker}; cat' --")
    git(d, "config", "diff.external", f"sh -c 'touch {marker}' --")
    extract(repo)
    assert not marker.exists()


def test_clean_filter_repos_are_refused_by_the_local_runner(repo, tmp_path):
    d, marker = _marker_repo(repo, tmp_path)
    (d / ".gitattributes").write_text("* filter=evil\n")
    git(d, "config", "filter.evil.clean", f"sh -c 'touch {marker}; cat' --")
    with pytest.raises(RuntimeError, match="filters"):
        trusted_local_git_runner(d)
    assert not marker.exists()


def test_config_includes_are_refused_by_the_local_runner(repo):
    d, _ = repo
    with open(d / ".git" / "config", "a") as f:
        f.write("[include]\n\tpath = /tmp/evil\n")
    with pytest.raises(RuntimeError):
        trusted_local_git_runner(d)


# ---------------- validate_patch on crafted text ----------------
GOOD = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"


def test_plain_text_diff_accepted():
    r = validate_patch(GOOD.encode())
    assert r.status == "ok" and r.patch == GOOD


@pytest.mark.parametrize("bad,needle", [
    ("diff --git a/../x b/../x\n--- a/../x\n+++ b/../x\n", "unsafe path"),
    ("diff --git a//etc/passwd b//etc/passwd\n", "unsafe"),
    ("diff --git a/.git/hooks/pre-commit b/.git/hooks/pre-commit\n", ".git"),
    ("diff --git a/sub/.git/config b/sub/.git/config\n", ".git"),
    ("diff --git a/x b/y\n", "differ"),
    ("diff --git a/x b/x\nrename from x\nrename to y\n", "renames"),
    ("diff --git a/x b/x\nsimilarity index 90%\n", "renames"),
    ("diff --git a/x b/x\nnew file mode 120000\n", "120000"),
    ("diff --git a/x b/x\nnew file mode 160000\n", "160000"),
    ("diff --git a/x b/x\nnew mode 100666\n", "100666"),
    ("diff --git a/x b/x\nGIT binary patch\nliteral 1\n", "binary"),
    ("diff --git a/x b/x\nBinary files a/x and b/x differ\n", "binary"),
    ('diff --git "a/x\\ty" "b/x\\ty"\n', "differ"),
    ("diff --git a/x b/x\n--- /etc/passwd\n+++ b/x\n", "prefix"),
    ("diff --git a/x b/x\n--- a/..\\x\n+++ b/x\n", "unsafe"),
    ("not a diff at all\n", "start with a git diff"),
    ("diff --git a/x b/x\n\x00\n", "NUL"),
])
def test_validate_rejects_each_hazard(bad, needle):
    r = validate_patch(bad.encode())
    assert r.status == "rejected" and any(needle in x for x in r.reasons), r.reasons


def test_invalid_utf8_and_size_rejected():
    assert validate_patch(b"diff --git a/x b/x\n\xff\xfe\n").status == "rejected"
    assert validate_patch(b"x" * (MAX_PATCH_BYTES + 1)).status == "rejected"


def test_whitespace_only_is_empty():
    assert validate_patch(b"  \n\n").status == "empty" and validate_patch(b"").status == "empty"


# ---------------- predictions file ----------------
def test_predictions_file_matches_swebench_format_and_loader(tmp_path):
    from swebench.harness.utils import get_predictions_from_file

    r = validate_patch(GOOD.encode())
    p = write_predictions(tmp_path / "predictions.jsonl", "pytest-dev__pytest-5227", "aiep-smoke", r)
    row = json.loads(p.read_text())
    assert row == {"instance_id": "pytest-dev__pytest-5227", "model_name_or_path": "aiep-smoke", "model_patch": GOOD}
    loaded = get_predictions_from_file(str(p), "unused", "test")
    assert loaded[0]["model_patch"] == GOOD


def test_predictions_never_written_for_empty_or_rejected_patches(tmp_path):
    for res in (PatchResult(status="empty"), PatchResult(status="rejected", reasons=["x"])):
        with pytest.raises(ValueError):
            write_predictions(tmp_path / "p.jsonl", "a__b-1", "m", res)
    assert not (tmp_path / "p.jsonl").exists()


def test_predictions_refuse_overwrite_and_bad_names(tmp_path):
    r = validate_patch(GOOD.encode())
    write_predictions(tmp_path / "p.jsonl", "a__b-1", "m", r)
    with pytest.raises(FileExistsError):
        write_predictions(tmp_path / "p.jsonl", "a__b-1", "m", r)
    for iid, model in (("bad id", "m"), ("a__b-1", "../evil"), ("a__b-1", "m m"), ("a__b-1", "")):
        with pytest.raises(ValueError):
            write_predictions(tmp_path / f"q{abs(hash(iid + model))}.jsonl", iid, model, r)


# ---------------- where git may run (audit: production git only inside the agent container) ----------------
def test_host_side_git_is_confined_to_two_known_modules():
    src = Path(__file__).resolve().parents[1] / "src" / "aiep"
    users = sorted(p.name for p in src.glob("*.py") if "subprocess" in p.read_text())
    # manifest.py: git on OUR repository (trusted). patch.py: the injected-executor module itself.
    assert users == ["manifest.py", "patch.py"]


def test_the_host_git_runner_is_referenced_nowhere_in_production_code():
    src = Path(__file__).resolve().parents[1] / "src" / "aiep"
    refs = sorted(p.name for p in src.glob("*.py") if "trusted_local_git_runner" in p.read_text())
    assert refs == ["patch.py"]  # defined there, called by no orchestration/runner/gateway module


def test_extract_patch_has_no_default_executor():
    import inspect as _inspect

    params = _inspect.signature(extract_patch).parameters
    assert params["run_git"].default is _inspect.Parameter.empty  # callers must choose where git runs


def test_manifest_git_only_ever_targets_the_given_trusted_repo_root():
    import aiep.manifest as mod

    src = Path(mod.__file__).read_text()
    assert src.count("subprocess.run") == 1 and 'cwd=repo_root' in src

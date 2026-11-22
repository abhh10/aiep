import json
import os

import pytest

from aiep import artifacts as A

KEY = "sk-ant-api03-REAL-KEY-CANARY-abcdefghijklmnop"
TOKEN = "per-run-token-0123456789abcdef"


def write(tmp_path, files=None, meta=None, secrets=(KEY, TOKEN), run_id="m2-x-A-20261119T000000Z"):
    return A.write_run_artifacts(tmp_path / "runs" / "m2", run_id,
                                 files if files is not None else {"prompt.txt": "issue text\n", "claude_stdout.jsonl": b'{"a":1}\n'},
                                 meta or {"model": "claude-sonnet-5-5"}, secrets)


def test_run_id_is_deterministic_and_valid():
    rid = A.make_run_id("pytest-dev__pytest-5227", "A", "20261119T231100Z")
    assert rid == "m2-pytest-dev__pytest-5227-A-20261119T231100Z" and A.make_run_id("pytest-dev__pytest-5227", "A", "20261119T231100Z") == rid
    for bad in [("nope", "A", "20261119T231100Z"), ("pytest-dev__pytest-5227", "C", "20261119T231100Z"),
                ("pytest-dev__pytest-5227", "A", "yesterday")]:
        with pytest.raises(A.ArtifactError):
            A.make_run_id(*bad)


def test_directory_contents_manifest_and_integrity(tmp_path):
    r = write(tmp_path)
    assert sorted(p.name for p in r.directory.iterdir()) == ["artifacts_manifest.json", "claude_stdout.jsonl", "prompt.txt", "run.json"]
    assert A.verify_run_artifacts(r.directory) == []
    run = json.loads((r.directory / "run.json").read_text())
    assert run["run_id"] == "m2-x-A-20261119T000000Z" and run["model"] == "claude-sonnet-5-5" and run["schema"] == 1
    assert run["files"] == ["claude_stdout.jsonl", "prompt.txt", "run.json"] and run["redactions"]["prompt.txt"] == 0


def test_files_are_read_only_and_tampering_is_detected(tmp_path):
    r = write(tmp_path)
    assert (r.directory / "prompt.txt").stat().st_mode & 0o222 == 0
    os.chmod(r.directory / "prompt.txt", 0o644)
    (r.directory / "prompt.txt").write_text("edited")
    assert A.verify_run_artifacts(r.directory) == ["content differs: prompt.txt"]
    (r.directory / "stray.txt").write_text("x")
    (r.directory / "claude_stdout.jsonl").unlink()
    assert set(A.verify_run_artifacts(r.directory)) == {"content differs: prompt.txt", "unexpected file: stray.txt", "missing: claude_stdout.jsonl"}


def test_a_run_directory_is_never_reused_or_overwritten(tmp_path):
    r = write(tmp_path)
    before = (r.directory / "prompt.txt").read_bytes()
    with pytest.raises(A.ArtifactError, match="already exists"):
        write(tmp_path, files={"prompt.txt": "different"})
    assert (r.directory / "prompt.txt").read_bytes() == before


@pytest.mark.parametrize("name", ["run.json", "../evil", "a/b", "notes.txt", "artifacts_manifest.json", ""])
def test_only_the_fixed_names_can_be_written(tmp_path, name):
    with pytest.raises(A.ArtifactError):
        write(tmp_path, files={name: "x"})
    assert not (tmp_path / "runs" / "m2" / "m2-x-A-20261119T000000Z").exists()  # refused before the directory was made


@pytest.mark.parametrize("rid", ["", "../x", "a/b", "-x", "x" * 65, ".", "a b"])
def test_bad_run_ids_are_refused(tmp_path, rid):
    with pytest.raises(A.ArtifactError):
        write(tmp_path, run_id=rid)


def test_secrets_never_reach_disk_in_any_file_including_metadata(tmp_path):
    leaky = (f"key {KEY} and token {TOKEN} and again {KEY}\n").encode()
    r = write(tmp_path, files={"prompt.txt": leaky, "claude_stdout.jsonl": leaky, "claude_stderr.txt": leaky,
                               "gateway_ledger.jsonl": leaky, "patch.diff": leaky, "predictions.jsonl": leaky},
              meta={"note": f"oops {KEY}", "token": TOKEN})
    for p in r.directory.iterdir():
        blob = p.read_bytes()
        assert KEY.encode() not in blob and TOKEN.encode() not in blob, p.name
    assert r.redactions["prompt.txt"] == 3 and r.redactions["run.json"] == 2
    assert A.verify_run_artifacts(r.directory) == []  # the manifest hashes the bytes actually stored
    run = json.loads((r.directory / "run.json").read_text())
    assert run["redactions"]["claude_stdout.jsonl"] == 3


def test_key_shaped_strings_are_redacted_even_when_not_listed(tmp_path):
    other = "sk-ant-api03-SOME-OTHER-KEY-abcdefghijklmnop"
    r = write(tmp_path, files={"claude_stdout.jsonl": f"echo {other}\n".encode()})
    assert other.encode() not in (r.directory / "claude_stdout.jsonl").read_bytes()


def test_short_or_empty_secrets_are_ignored_rather_than_mangling_text():
    data, n = A.redact(b"abc abc", ["abc", "", None])
    assert (data, n) == (b"abc abc", 0)


def test_longer_secret_is_redacted_before_a_shorter_one_it_contains():
    data, n = A.redact(b"x ABCDEFGHIJ-long y", ["ABCDEFGH", "ABCDEFGHIJ-long"])
    assert data == b"x [REDACTED] y" and n == 1


def test_unserialisable_metadata_is_refused_cleanly(tmp_path):
    with pytest.raises(A.ArtifactError, match="serialisable"):
        write(tmp_path, meta={"bad": object()})


def test_incomplete_run_has_no_manifest_and_reports_it(tmp_path, monkeypatch):
    real = A._write_new
    calls = []

    def flaky(path, data):
        calls.append(path.name)
        if path.name == "run.json":
            raise OSError(28, "No space left on device")
        real(path, data)

    monkeypatch.setattr(A, "_write_new", flaky)
    with pytest.raises(OSError):
        write(tmp_path)
    d = tmp_path / "runs" / "m2" / "m2-x-A-20261119T000000Z"
    assert (d / "prompt.txt").exists() and not (d / A.MANIFEST_NAME).exists()  # kept for forensics, visibly incomplete
    assert "manifest unreadable" in A.verify_run_artifacts(d)[0]

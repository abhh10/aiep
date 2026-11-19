"""task_input.load_record: strictly local, deterministic, and loud on any missing or invalid data.

Every test builds a tiny Hugging Face-style cache in a temporary directory. tests/conftest.py (unchanged) fails any test
that touches the network, even if the code under test swallows the error; these tests also assert the attempt list is empty.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from aiep import task_input
from aiep.manifest import (ManifestError, load_dataset_config, read_verified_split, verify_dataset_snapshot,
                           verify_split_file)
from aiep.task_input import export_task_input

REAL = Path(__file__).resolve().parents[1]
REV = "b" * 40
OTHER_REV = "c" * 40
REPO = "SWE-bench/SWE-bench_Lite"
ID = "pkg__repo-1"


def rows(n_target=1, extra=()):
    base = dict(repo="pkg/repo", base_commit="a" * 40, problem_statement="The widget crashes on empty input.",
                patch="CANARY-GOLD-PATCH --- a/x +++ b/x", test_patch="CANARY-TEST-PATCH def test_x(): pass",
                hints_text="CANARY-HINTS look in widget.py please", FAIL_TO_PASS='["t::a"]', PASS_TO_PASS='["t::b"]',
                eval_script="CANARY-EVAL-SCRIPT #!/bin/bash")
    out = [dict(base, instance_id=ID) for _ in range(n_target)]
    out += [dict(base, instance_id=f"other__repo-{i}") for i in range(2)]
    return out + list(extra)


def parquet_bytes(table_rows, tmp_path):
    p = tmp_path / "_tmp.parquet"
    pq.write_table(pa.Table.from_pylist(table_rows), p)
    data = p.read_bytes()
    p.unlink()
    return data


def build(tmp_path, *, data=None, revision=REV, refs=REV, split="test", config=None, snapshot=True, link=True, blob_name=None,
          second_file=False, hf_name="hf"):
    """A cache + repo_root. Returns (hf_home, repo_root)."""
    hf, root = tmp_path / hf_name, tmp_path / "repo"
    (root / "config").mkdir(parents=True, exist_ok=True)
    cfg = {"schema": 1, "repo_id": REPO, "split": split, "revision": revision, "provenance": "test"}
    cfg.update(config or {})
    (root / "config" / "dataset.json").write_text(json.dumps(cfg))
    repo_dir = hf / "hub" / "datasets--SWE-bench--SWE-bench_Lite"
    (repo_dir / "blobs").mkdir(parents=True)
    (repo_dir / "refs").mkdir()
    if refs is not None:
        (repo_dir / "refs" / "main").write_text(refs)
    data = parquet_bytes(rows(), tmp_path) if data is None else data
    name = blob_name or hashlib.sha256(data).hexdigest()
    (repo_dir / "blobs" / name).write_bytes(data)
    if snapshot:
        d = repo_dir / "snapshots" / revision / "data"
        d.mkdir(parents=True)
        entry = d / f"{split}-00000-of-00001.parquet"
        if link:
            os.symlink(f"../../../blobs/{name}", entry)
        else:
            entry.write_bytes(data)
        if second_file:
            os.symlink(f"../../../blobs/{name}", d / f"{split}-00001-of-00002.parquet")
    return hf, root


def load(hf, root, instance=ID):
    return task_input.load_record(instance, hf, root)


# ---------------- the happy path and its independence from imported libraries ----------------
def test_loads_the_record_from_the_verified_local_snapshot(tmp_path, network_guard):
    hf, root = build(tmp_path)
    rec = load(hf, root)
    assert rec["instance_id"] == ID and rec["patch"].startswith("CANARY-GOLD-PATCH")
    out = export_task_input(rec).to_json()
    assert "CANARY" not in out and "widget crashes" in out
    assert network_guard == []


def test_libraries_imported_before_the_call_cannot_cause_network_access_or_be_used(tmp_path, monkeypatch, network_guard):
    import datasets
    import datasets.config
    import huggingface_hub
    import huggingface_hub.constants
    for key in ("HF_HUB_OFFLINE", "HF_DATASETS_OFFLINE", "TRANSFORMERS_OFFLINE"):
        monkeypatch.delenv(key, raising=False)  # offline switches NOT set, as in the failing scenario
    monkeypatch.setattr(huggingface_hub.constants, "HF_HUB_OFFLINE", False)  # import-time constants say "online"
    monkeypatch.setattr(datasets.config, "HF_HUB_OFFLINE", False)
    monkeypatch.setattr(datasets.config, "HF_DATASETS_OFFLINE", False)
    assert "datasets" in sys.modules and "huggingface_hub" in sys.modules

    def forbidden(*a, **k):
        raise AssertionError("the dataset libraries must not be used to load a record")
    monkeypatch.setattr(datasets, "load_dataset", forbidden)
    monkeypatch.setattr(datasets, "load_dataset_builder", forbidden)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", forbidden)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", forbidden)
    hf, root = build(tmp_path)
    assert load(hf, root)["instance_id"] == ID
    assert network_guard == []


def test_in_a_fresh_interpreter_neither_library_is_even_imported(tmp_path):
    hf, root = build(tmp_path)
    code = ("import sys, json\n"
            "from pathlib import Path\n"
            "from aiep import task_input\n"
            f"rec = task_input.load_record({ID!r}, Path({str(hf)!r}), Path({str(root)!r}))\n"
            "print(json.dumps([rec['instance_id'], 'datasets' in sys.modules, 'huggingface_hub' in sys.modules,\n"
            "                  any(m.startswith(('httpx', 'requests', 'urllib3')) for m in sys.modules)]))\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("HF_")}
    env["PYTHONPATH"] = str(REAL / "src")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=tmp_path, timeout=30)  # a hang at exit would time out
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == [ID, False, False, False]


def test_load_record_does_not_touch_process_environment(tmp_path):
    before = dict(os.environ)
    hf, root = build(tmp_path)
    load(hf, root)
    assert dict(os.environ) == before  # it no longer needs, or sets, offline variables


# ---------------- missing or invalid local data fails clearly, never falling back ----------------
def test_missing_local_cache_fails_clearly(tmp_path, network_guard):
    _, root = build(tmp_path)
    with pytest.raises(ManifestError, match="refs/main"):
        load(tmp_path / "no-such-cache", root)
    (tmp_path / "empty").mkdir()
    with pytest.raises(ManifestError, match="refs/main"):
        load(tmp_path / "empty", root)
    assert network_guard == []


@pytest.mark.parametrize("kw, match", [
    (dict(refs=None), "no refs/main"),
    (dict(refs=OTHER_REV), "pinned revision"),
    (dict(refs="not-a-revision"), "pinned revision"),
    (dict(refs=""), "pinned revision"),
    (dict(snapshot=False), "snapshot directory is missing"),
])
def test_invalid_or_unpinned_revision_in_the_cache_fails_clearly(tmp_path, network_guard, kw, match):
    hf, root = build(tmp_path, **kw)
    with pytest.raises(ManifestError, match=match):
        load(hf, root)
    assert network_guard == []


def test_a_pinned_revision_the_cache_does_not_hold_fails_instead_of_resolving_online(tmp_path, network_guard):
    hf, root = build(tmp_path)
    cfg = json.loads((root / "config" / "dataset.json").read_text())
    cfg["revision"] = OTHER_REV  # a perfectly valid hash, just not the cached one
    (root / "config" / "dataset.json").write_text(json.dumps(cfg))
    with pytest.raises(ManifestError, match="pinned revision"):
        load(hf, root)
    assert network_guard == []


@pytest.mark.parametrize("revision", ["", "abc", "B" * 40, "g" * 40, "b" * 39, "b" * 41, None, 123, ["b" * 40]])
def test_a_malformed_revision_in_the_config_is_refused(tmp_path, network_guard, revision):
    hf, root = build(tmp_path)
    cfg = json.loads((root / "config" / "dataset.json").read_text())
    cfg["revision"] = revision
    (root / "config" / "dataset.json").write_text(json.dumps(cfg))
    with pytest.raises(ManifestError, match="revision"):
        load(hf, root)
    assert network_guard == []


def test_a_config_without_a_revision_or_unreadable_is_refused(tmp_path):
    hf, root = build(tmp_path)
    cfgp = root / "config" / "dataset.json"
    cfg = json.loads(cfgp.read_text())
    del cfg["revision"]
    cfgp.write_text(json.dumps(cfg))
    with pytest.raises(ManifestError, match="revision"):
        load(hf, root)
    cfgp.write_text("{not json")
    with pytest.raises(ManifestError, match="cannot read"):
        load(hf, root)
    cfgp.write_text("[]")
    with pytest.raises(ManifestError, match="JSON object"):
        load(hf, root)
    cfgp.unlink()
    with pytest.raises(ManifestError, match="cannot read"):
        load(hf, root)


@pytest.mark.parametrize("over", [{"repo_id": "../../etc/passwd"}, {"repo_id": "no-slash"}, {"repo_id": "a/b/c"},
                                  {"repo_id": "a/../b"}, {"repo_id": ""}, {"repo_id": None}, {"repo_id": "a b/c"}])
def test_a_malformed_repo_id_cannot_steer_the_cache_path(tmp_path, over):
    hf, root = build(tmp_path)
    cfg = json.loads((root / "config" / "dataset.json").read_text())
    cfg.update(over)
    (root / "config" / "dataset.json").write_text(json.dumps(cfg))
    with pytest.raises(ManifestError, match="repo_id"):
        load(hf, root)


@pytest.mark.parametrize("split", ["../test", "Test", "", "te st", "test/x", "x" * 40, None, 5, "test*"])
def test_a_malformed_split_is_refused(tmp_path, network_guard, split):
    hf, root = build(tmp_path)
    cfg = json.loads((root / "config" / "dataset.json").read_text())
    cfg["split"] = split
    (root / "config" / "dataset.json").write_text(json.dumps(cfg))
    with pytest.raises(ManifestError, match="split"):
        load(hf, root)
    assert network_guard == []


def test_missing_or_ambiguous_split_files_are_refused(tmp_path, network_guard):
    hf, root = build(tmp_path, split="dev")  # the config says 'test' but only a 'dev' file exists
    cfg = json.loads((root / "config" / "dataset.json").read_text())
    cfg["split"] = "test"
    (root / "config" / "dataset.json").write_text(json.dumps(cfg))
    with pytest.raises(ManifestError, match="exactly one 'test' parquet file"):
        load(hf, root)
    hf2, root2 = build(tmp_path / "two", second_file=True)
    with pytest.raises(ManifestError, match="exactly one 'test' parquet file"):
        load(hf2, root2)
    hf3, root3 = build(tmp_path / "nodata")
    for f in (hf3 / "hub" / "datasets--SWE-bench--SWE-bench_Lite" / "snapshots" / REV / "data").iterdir():
        f.unlink()
    with pytest.raises(ManifestError, match="exactly one"):
        load(hf3, root3)
    assert network_guard == []


def test_a_modified_substituted_or_dangling_data_file_is_refused(tmp_path, network_guard):
    good = parquet_bytes(rows(), tmp_path)
    # content changed after it was stored under its name
    hf, root = build(tmp_path / "t1", data=good)
    blob = next((hf / "hub" / "datasets--SWE-bench--SWE-bench_Lite" / "blobs").iterdir())
    blob.write_bytes(good + b"\0")
    with pytest.raises(ManifestError, match="modified or corrupt"):
        load(hf, root)
    # stored under a name that is not its hash
    hf, root = build(tmp_path / "t2", data=good, blob_name="f" * 64)
    with pytest.raises(ManifestError, match="modified or corrupt"):
        load(hf, root)
    # blob name is not a sha256
    hf, root = build(tmp_path / "t3", data=good, blob_name="not-a-hash")
    with pytest.raises(ManifestError, match="sha256-named blob"):
        load(hf, root)
    # a regular file where a blob link is required
    hf, root = build(tmp_path / "t4", data=good, link=False)
    with pytest.raises(ManifestError, match="not a link"):
        load(hf, root)
    # dangling link
    hf, root = build(tmp_path / "t5", data=good)
    next((hf / "hub" / "datasets--SWE-bench--SWE-bench_Lite" / "blobs").iterdir()).unlink()
    with pytest.raises(ManifestError, match="dangling"):
        load(hf, root)
    # a link that leaves this repository's blob store
    hf, root = build(tmp_path / "t6", data=good)
    entry = next((hf / "hub" / "datasets--SWE-bench--SWE-bench_Lite" / "snapshots" / REV / "data").iterdir())
    entry.unlink()
    outside = tmp_path / "t6" / "outside" / hashlib_name(good)
    outside.parent.mkdir()
    outside.write_bytes(good)
    os.symlink(str(outside), entry)
    with pytest.raises(ManifestError, match="sha256-named blob"):
        load(hf, root)
    assert network_guard == []


def hashlib_name(data):
    return hashlib.sha256(data).hexdigest()


def test_an_unreadable_parquet_that_matches_its_own_name_is_still_refused(tmp_path, network_guard):
    junk = b"this is not a parquet file"
    hf, root = build(tmp_path, data=junk)  # hash and name agree; the content is still unusable
    with pytest.raises(ManifestError, match="cannot read"):
        load(hf, root)
    assert network_guard == []


def test_missing_column_absent_or_duplicate_instances_are_refused(tmp_path, network_guard):
    no_col = parquet_bytes([{"repo": "x", "problem_statement": "p"}], tmp_path)
    hf, root = build(tmp_path / "a", data=no_col)
    with pytest.raises(ManifestError, match="no instance_id column"):
        load(hf, root)
    hf, root = build(tmp_path / "b")
    with pytest.raises(ManifestError, match="found 0"):
        load(hf, root, instance="does__not-exist")
    hf, root = build(tmp_path / "c", data=parquet_bytes(rows(n_target=2), tmp_path))
    with pytest.raises(ManifestError, match="found 2"):
        load(hf, root)
    assert network_guard == []


# ---------------- the verification helpers directly ----------------
def test_verify_helpers_return_the_verified_paths(tmp_path):
    hf, root = build(tmp_path)
    snap = verify_dataset_snapshot(hf, load_dataset_config(root))
    entry = verify_split_file(snap, "test")
    assert entry.name == "test-00000-of-00001.parquet" and snap.name == REV


def test_the_real_pinned_snapshot_verifies_locally_if_present(network_guard):
    hf = REAL / ".cache" / "hf"
    if not hf.exists():
        pytest.skip("no local HF cache")
    snap = verify_dataset_snapshot(hf, load_dataset_config(REAL))
    verify_split_file(snap, load_dataset_config(REAL)["split"])
    assert network_guard == []


# ---------------- containment: no other snapshot, no link out of the repository cache ----------------
def _repo_dir(hf):
    return hf / "hub" / "datasets--SWE-bench--SWE-bench_Lite"


def test_a_snapshot_directory_that_is_a_link_to_another_snapshot_is_refused(tmp_path, network_guard):
    """refs/main and the directory name say the pinned revision, but the directory is really another snapshot."""
    hf, root = build(tmp_path, revision=OTHER_REV, refs=OTHER_REV)  # a complete, valid snapshot of ANOTHER revision
    cfg = json.loads((root / "config" / "dataset.json").read_text())
    cfg["revision"] = REV
    (root / "config" / "dataset.json").write_text(json.dumps(cfg))
    (_repo_dir(hf) / "refs" / "main").write_text(REV)
    os.symlink(_repo_dir(hf) / "snapshots" / OTHER_REV, _repo_dir(hf) / "snapshots" / REV)  # snapshots/<pinned> -> other
    verify_dataset_snapshot(hf, load_dataset_config(root))  # the pre-existing pin check is satisfied by the directory entry
    with pytest.raises(ManifestError, match="not a real directory"):
        load(hf, root)
    assert network_guard == []


def test_a_data_directory_that_is_a_link_is_refused(tmp_path, network_guard):
    hf, root = build(tmp_path)
    snap = _repo_dir(hf) / "snapshots" / REV
    real = tmp_path / "elsewhere"
    os.rename(snap / "data", real)
    os.symlink(real, snap / "data")
    with pytest.raises(ManifestError, match="missing or is a link"):
        load(hf, root)
    assert network_guard == []


def test_a_blob_link_that_escapes_through_a_symlinked_parent_is_refused(tmp_path, network_guard):
    good = parquet_bytes(rows(), tmp_path)
    hf, root = build(tmp_path, data=good)
    repo = _repo_dir(hf)
    name = hashlib_name(good)
    entry = repo / "snapshots" / REV / "data" / "test-00000-of-00001.parquet"
    entry.unlink()
    other = tmp_path / "other-repo"
    (other / "a" / "b" / "c").mkdir(parents=True)
    (other / "blobs").mkdir()
    (other / "blobs" / name).write_bytes(good)  # byte-identical content, but in another repository's store
    os.symlink(other / "a" / "b" / "c", repo / "snapshots" / REV / "hop")
    # Textually (lexically) this is <repo>/blobs/<name>; to the operating system it is <other-repo>/blobs/<name>.
    os.symlink(f"../hop/../../../blobs/{name}", entry)
    assert entry.is_file()  # the escape really resolves, so only a resolved-path check can catch it
    with pytest.raises(ManifestError, match="sha256-named blob"):
        load(hf, root)
    assert network_guard == []


def test_the_snapshot_directory_name_must_be_the_pinned_revision(tmp_path):
    hf, root = build(tmp_path)
    snap = _repo_dir(hf) / "snapshots" / REV
    assert read_verified_split(snap, "test", REV)
    with pytest.raises(ManifestError, match="not the pinned revision"):
        read_verified_split(snap, "test", OTHER_REV)
    assert verify_split_file(snap, "test", REV).name.startswith("test-")


def test_the_real_two_level_blob_layout_is_accepted(tmp_path, network_guard):
    """Like the real cache: repo blobs/<sha256> is itself a link into a shared store (xet) beside the repository."""
    good = parquet_bytes(rows(), tmp_path)
    hf, root = build(tmp_path, data=good)
    repo = _repo_dir(hf)
    name = hashlib_name(good)
    shared = hf / "hub" / "blobs" / "50"
    shared.mkdir(parents=True)
    (shared / "xet-hash").write_bytes(good)
    (repo / "blobs" / name).unlink()
    os.symlink("../../blobs/50/xet-hash", repo / "blobs" / name)
    assert load(hf, root)["instance_id"] == ID
    (shared / "xet-hash").write_bytes(good + b"x")  # the shared store is tampered: caught by the content hash
    with pytest.raises(ManifestError, match="modified or corrupt"):
        load(hf, root)
    assert network_guard == []


def test_the_bytes_that_are_hashed_are_the_bytes_that_are_parsed(tmp_path, monkeypatch):
    import pyarrow.parquet as pq
    hf, root = build(tmp_path)
    seen = []
    real = pq.read_table

    def spy(source, *a, **k):
        seen.append(type(source).__name__)
        return real(source, *a, **k)
    monkeypatch.setattr(pq, "read_table", spy)
    load(hf, root)
    assert seen == ["BufferReader"]  # never re-opened by path after the hash check


def test_the_parquet_read_creates_no_arrow_thread_pool(tmp_path, monkeypatch):
    import pyarrow.parquet as pq
    hf, root = build(tmp_path)
    kwargs = []
    real = pq.read_table
    monkeypatch.setattr(pq, "read_table", lambda src, *a, **k: (kwargs.append(k), real(src, *a, **k))[1])
    load(hf, root)
    assert kwargs == [{"use_threads": False}]  # a pooled read once deadlocked at interpreter exit


def test_repo_ids_that_would_share_a_cache_directory_are_refused(tmp_path):
    hf, root = build(tmp_path)
    for bad in ("a--b/c", "a/b--c", "SWE-bench/SWE--bench"):
        cfg = json.loads((root / "config" / "dataset.json").read_text())
        cfg["repo_id"] = bad
        (root / "config" / "dataset.json").write_text(json.dumps(cfg))
        with pytest.raises(ManifestError, match="repo_id"):
            load(hf, root)

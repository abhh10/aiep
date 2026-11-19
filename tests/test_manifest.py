import json
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from aiep import manifest
from aiep.manifest import ManifestError

REAL = Path(__file__).resolve().parents[1]
REV = "b0dde1093fe417d83b7184254edf8199c1f0dff5"


def make_cache(tmp_path, ref=REV, snapshot=REV):
    hf = tmp_path / "hf"
    repo = hf / "hub" / "datasets--SWE-bench--SWE-bench_Lite"
    (repo / "refs").mkdir(parents=True)
    (repo / "refs" / "main").write_text(ref)
    if snapshot:
        (repo / "snapshots" / snapshot).mkdir(parents=True)
    return hf


def make_repo(tmp_path):
    root = tmp_path / "repo"
    for d in ("src/aiep", "config", "requirements", "scripts"):
        (root / d).mkdir(parents=True)
    (root / "src/aiep/a.py").write_text("x = 1\n")
    shutil.copy(REAL / "config" / "dataset.json", root / "config")
    shutil.copy(REAL / "config" / "wheelhouse.json", root / "config")
    (root / "requirements/host.lock").write_text("a==1\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)
    return root


def test_dataset_pin_matches_the_real_cache_if_present():
    hf = REAL / ".cache" / "hf"
    if not hf.exists():
        pytest.skip("no local HF cache")
    cfg = manifest.load_dataset_config(REAL)
    assert manifest.verify_dataset_snapshot(hf, cfg).name == cfg["revision"]


def test_verify_ok(tmp_path):
    cfg = manifest.load_dataset_config(REAL)
    assert manifest.verify_dataset_snapshot(make_cache(tmp_path), cfg).name == REV


def test_wrong_ref_missing_ref_and_missing_snapshot_all_fail(tmp_path):
    cfg = manifest.load_dataset_config(REAL)
    with pytest.raises(ManifestError, match="refs/main is"):
        manifest.verify_dataset_snapshot(make_cache(tmp_path / "a", ref="1" * 40), cfg)
    with pytest.raises(ManifestError, match="no refs/main"):
        manifest.verify_dataset_snapshot(tmp_path / "empty", cfg)
    with pytest.raises(ManifestError, match="snapshot directory is missing"):
        manifest.verify_dataset_snapshot(make_cache(tmp_path / "b", snapshot=None), cfg)


def test_bad_revision_in_config_rejected(tmp_path):
    root = make_repo(tmp_path)
    for bad in ("main", "b0dde109", "Z" * 40):
        cfg = json.loads((root / "config" / "dataset.json").read_text())
        cfg["revision"] = bad
        (root / "config" / "dataset.json").write_text(json.dumps(cfg))
        with pytest.raises(ManifestError, match="40-character"):
            manifest.load_dataset_config(root)


def test_offline_env_forces_hf_offline():
    assert manifest.offline_env() == {"HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"}


def test_build_manifest_makes_no_network_call(tmp_path, monkeypatch):
    root, hf = make_repo(tmp_path), make_cache(tmp_path)

    def blocked(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    m = manifest.build_manifest(root, hf_home=hf, run_id="r1", instance_ids=["b", "a"],
                                image_ids={"a": "sha256:x"})
    assert m["instances"] == ["a", "b"] and m["dataset"]["verified"] is True
    assert m["dataset"]["revision"] == REV
    assert m["git"]["dirty"] is False and len(m["git"]["commit"]) == 40
    assert m["wheelhouse"]["status"] == "unpopulated"
    assert "src/aiep/a.py" in m["files"] and "requirements/host.lock" in m["files"]
    assert m["versions"]["swebench"] == "5.0.2"
    assert m["versions"]["pyarrow"] == "26.0.1"  # the library that reads task records is recorded too


def test_dirty_tree_is_flagged_untracked_is_counted_separately(tmp_path):
    root, hf = make_repo(tmp_path), make_cache(tmp_path)
    (root / "untracked.txt").write_text("u")
    m = manifest.build_manifest(root, hf_home=hf, run_id="r", instance_ids=["a"])
    assert m["git"]["dirty"] is False and m["git"]["untracked_count"] == 1
    (root / "src/aiep/a.py").write_text("x = 2\n")
    m = manifest.build_manifest(root, hf_home=hf, run_id="r", instance_ids=["a"])
    assert m["git"]["dirty"] is True and m["git"]["changed_files"] == ["src/aiep/a.py"]


def test_build_manifest_fails_on_unverified_dataset(tmp_path):
    root = make_repo(tmp_path)
    with pytest.raises(ManifestError):
        manifest.build_manifest(root, hf_home=make_cache(tmp_path, ref="2" * 40), run_id="r", instance_ids=["a"])


def test_write_manifest_never_overwrites(tmp_path):
    p = manifest.write_manifest({"a": 1}, tmp_path / "run")
    assert json.loads(p.read_text()) == {"a": 1}
    with pytest.raises(FileExistsError):
        manifest.write_manifest({"a": 2}, tmp_path / "run")
    assert json.loads(p.read_text()) == {"a": 1}

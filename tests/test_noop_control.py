"""Deterministic no-op control prediction, its CLI, and the m3_grade 'noop' shortcut. Nothing here runs the evaluator, Docker or the network."""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiep import grading as g
from aiep.patch import validate_patch

REAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REAL / "scripts"))
INSTANCE = "demo__demo-1"
ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
CONTENT = "AIEP no-op control: this file changes nothing.\n"


def load(name):
    spec = importlib.util.spec_from_file_location(name, REAL / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def git(cwd, *a, **kw):
    return subprocess.run(["git", *a], cwd=cwd, env=ENV, check=True, capture_output=True, **kw)


def test_prediction_is_deterministic_and_has_exactly_the_harness_keys():
    a, b = g.noop_prediction(INSTANCE), g.noop_prediction(INSTANCE)
    assert a == b and set(a) == {"instance_id", "model_name_or_path", "model_patch"} and a["instance_id"] == INSTANCE
    assert hashlib.sha256(a["model_patch"].encode()).hexdigest() == g.NOOP_PATCH_SHA256  # the registered bytes: any change must be deliberate


def test_the_index_line_names_the_blob_the_patch_really_adds(tmp_path):
    h = git(tmp_path, "hash-object", "--stdin", input=CONTENT.encode()).stdout.decode().strip()
    assert f"index 0000000..{h[:7]}" in g.noop_prediction(INSTANCE)["model_patch"]


def test_patch_passes_the_validator_and_applies_to_any_repository_adding_one_file(tmp_path):
    p = g.noop_prediction(INSTANCE)["model_patch"].encode()
    assert validate_patch(p).status == "ok"
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "x.py").write_text("x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "apply", "--check", "-", input=p)
    git(repo, "apply", "-", input=p)
    assert sorted(os.listdir(repo)) == [".git", g.NOOP_FILE, "x.py"] and (repo / g.NOOP_FILE).read_text() == CONTENT
    assert git(repo, "status", "--porcelain").stdout.decode().split() == ["??", g.NOOP_FILE]


def test_write_refuses_to_overwrite(tmp_path):
    p = g.write_noop_predictions(tmp_path / "n.jsonl", INSTANCE)
    assert json.loads(p.read_text()) == g.noop_prediction(INSTANCE) and p.read_text().endswith("\n") and p.read_text().count("\n") == 1
    with pytest.raises(FileExistsError):
        g.write_noop_predictions(p, INSTANCE)


# ---- scripts/m3_noop_predictions.py
def test_generator_cli_writes_a_deterministic_file_and_prints_its_hash(tmp_path, capsys):
    cli = load("m3_noop_predictions")
    out = tmp_path / "a.jsonl"
    assert cli.main([INSTANCE, "--out", str(out)]) == 0
    first = out.read_bytes()
    printed = json.loads(capsys.readouterr().out)
    assert printed["predictions_sha256"] == hashlib.sha256(first).hexdigest() and printed["patch_sha256"] == g.NOOP_PATCH_SHA256 and printed["path"] == str(out)
    out2 = tmp_path / "b.jsonl"
    assert cli.main([INSTANCE, "--out", str(out2)]) == 0 and out2.read_bytes() == first


def test_generator_cli_refuses_bad_ids_relative_paths_and_overwrites(tmp_path, capsys):
    cli = load("m3_noop_predictions")
    assert cli.main(["not an id", "--out", str(tmp_path / "x.jsonl")]) == 2
    assert cli.main([INSTANCE, "--out", "relative.jsonl"]) == 2
    assert cli.main([INSTANCE, "--out", str(tmp_path / "ok.jsonl")]) == 0
    assert cli.main([INSTANCE, "--out", str(tmp_path / "ok.jsonl")]) == 2


def test_generator_cli_never_touches_docker_or_the_evaluator():
    src = (REAL / "scripts" / "m3_noop_predictions.py").read_text()
    for bad in ("docker", "stock_eval", "subprocess", "grade("):
        assert bad not in src


# ---- scripts/m3_grade.py 'noop'
def fake_grade(calls):
    def grade(instance_id, predictions, run_id, *, kind, **kw):
        calls.append((instance_id, predictions, run_id, kind, Path(predictions).read_text() if predictions != "gold" else None))
        return SimpleNamespace(summary={"valid": True, "run_id": run_id})
    return grade


def pin_ok(root, instance):
    return {"image": "img", "image_id": "sha256:" + "a" * 64}


def test_m3_grade_noop_generates_the_deterministic_file_then_grades_as_a_noop_control(tmp_path, capsys):
    m = load("m3_grade")
    calls = []
    rc = m.main([INSTANCE, "m3ctl-noop-test", "noop"], root=tmp_path, grade_fn=fake_grade(calls), pin_fn=pin_ok)
    assert rc == 0
    (inst, preds, run_id, kind, text), = calls
    assert kind == "noop" and Path(preds).is_absolute() and Path(preds).is_file() and json.loads(text) == g.noop_prediction(INSTANCE)
    assert str(tmp_path) in preds


def test_m3_grade_noop_refuses_a_contradicting_kind_and_never_grades(tmp_path):
    m = load("m3_grade")
    calls = []
    for kind in ("gold", "scored"):
        assert m.main([INSTANCE, "r1", "noop", "--kind", kind], root=tmp_path, grade_fn=fake_grade(calls), pin_fn=pin_ok) == 2
    assert calls == []


def test_m3_grade_without_noop_is_unchanged(tmp_path):
    m = load("m3_grade")
    calls = []
    assert m.main([INSTANCE, "r2", "gold", "--kind", "gold"], root=tmp_path, grade_fn=fake_grade(calls), pin_fn=pin_ok) == 0
    assert calls[0][1] == "gold" and calls[0][3] == "gold"

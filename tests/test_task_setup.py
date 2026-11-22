"""task_setup with fake Docker: no daemon, no container, no network. Defect 3: the seed's HEAD/base_commit relation is checked BEFORE the pin is written."""
import json
from pathlib import Path

import pytest

from aiep import task_pins as tp
from aiep import task_setup as ts
from aiep.task_setup import SetupRefused
from test_seed import HEAD
from test_seed_source import FakeHost, IMAGE

GIB = 1024**3
INSTANCE = "demo__demo-1"
BASE = "b" * 40


@pytest.fixture
def root(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "evaluator_images.json").write_text(json.dumps({"images": {INSTANCE: {"image": "x", "image_id": IMAGE, "source": "t"}}}))
    (tmp_path / "config" / "seeds.json").write_text(json.dumps({"seeds": {}}))
    return tmp_path


def lineage_ok(seed):
    return {"head": seed.manifest.head, "parent": BASE}


def go(root, host, **kw):
    kw.setdefault("lineage", lineage_ok)
    kw.setdefault("base_commit", BASE)
    return ts.extract_and_pin_seed(host.client, root, INSTANCE, free_bytes=16 * GIB, run_id="seedsrc-test", **kw)


def pins(root):
    return json.loads((root / "config" / "seeds.json").read_text())["seeds"]


def test_pin_written_when_parent_is_base_commit(root):
    r = go(root, FakeHost())
    assert r["head"] == HEAD and r["base_commit"] == BASE
    assert pins(root)[INSTANCE]["head"] == HEAD


@pytest.mark.parametrize("lineage", [
    lambda s: {"head": s.manifest.head, "parent": "c" * 40},             # parent is some other commit
    lambda s: {"head": BASE, "parent": "c" * 40},                        # HEAD is the base commit itself: not the registered relation
    lambda s: {"head": "d" * 40, "parent": BASE},                        # git's HEAD disagrees with the manifest's
    lambda s: {"head": s.manifest.head, "parent": ""},                   # no parent (root commit / no history)
    lambda s: {},                                                        # provider returned nothing
])
def test_refuses_and_writes_no_pin_on_bad_lineage(root, lineage):
    with pytest.raises(SetupRefused, match="base_commit|HEAD"):
        go(root, FakeHost(), lineage=lineage)
    assert INSTANCE not in pins(root)


def test_refuses_when_lineage_cannot_be_computed(root):
    def boom(seed):
        raise RuntimeError("fatal: no .git")
    with pytest.raises(SetupRefused, match="could not be checked"):
        go(root, FakeHost(), lineage=boom)
    assert INSTANCE not in pins(root)


@pytest.mark.parametrize("bad", ["", "abc", "B" * 40, None, "b" * 39])
def test_refuses_malformed_base_commit_before_any_docker_call(root, bad):
    h = FakeHost()
    with pytest.raises(SetupRefused, match="base_commit"):
        go(root, h, base_commit=bad)
    assert h.calls == [] and INSTANCE not in pins(root)


def test_base_commit_and_lineage_are_mandatory(root):
    with pytest.raises(TypeError):
        ts.extract_and_pin_seed(FakeHost().client, root, INSTANCE, free_bytes=16 * GIB)


def test_gate_still_refuses_before_docker(root):
    h = FakeHost()
    with pytest.raises(SetupRefused, match="16 GiB"):
        ts.extract_and_pin_seed(h.client, root, INSTANCE, free_bytes=15 * GIB, base_commit=BASE, lineage=lineage_ok)
    assert h.calls == []


def test_extracted_files_stay_as_evidence_when_refused(root):
    with pytest.raises(SetupRefused):
        go(root, FakeHost(), lineage=lambda s: {"head": s.manifest.head, "parent": "c" * 40})
    assert list((root / ".cache" / "seeds" / INSTANCE).glob("seed*"))


# ---------------------------------------------------------------- script layer
import importlib.util
import subprocess

REAL = Path(__file__).resolve().parents[1]


def load_script(name):
    import sys
    sys.path.insert(0, str(REAL / "scripts"))
    spec = importlib.util.spec_from_file_location(name, REAL / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_extract_script_pins_on_match_and_refuses_on_mismatch(root, capsys):
    drv = load_script("extract_task_seed")
    h = FakeHost()
    h.client.close = lambda: None
    assert drv.main([INSTANCE], client_factory=lambda: h.client, free_bytes=16 * GIB, root=root, base_commit_of=lambda i: BASE, lineage=lineage_ok) == 0
    assert INSTANCE in pins(root)


def test_extract_script_mismatch_exit_1_no_pin(root, capsys):
    drv = load_script("extract_task_seed")
    h = FakeHost()
    h.client.close = lambda: None
    rc = drv.main([INSTANCE], client_factory=lambda: h.client, free_bytes=16 * GIB, root=root, base_commit_of=lambda i: "e" * 40, lineage=lineage_ok)
    assert rc == 1 and INSTANCE not in pins(root) and "base_commit" in capsys.readouterr().err


def test_extract_script_dataset_failure_is_before_docker(root, capsys):
    drv = load_script("extract_task_seed")
    built = []

    def boom(i):
        raise RuntimeError("no dataset")
    rc = drv.main([INSTANCE], client_factory=lambda: built.append(1), free_bytes=16 * GIB, root=root, base_commit_of=boom, lineage=lineage_ok)
    assert rc == 2 and built == []


def test_extract_script_gate_before_docker(root):
    drv = load_script("extract_task_seed")
    built = []
    assert drv.main([INSTANCE], client_factory=lambda: built.append(1), free_bytes=15 * GIB, root=root) == 2 and built == []


def test_seed_lineage_with_real_git_on_a_real_seed(tmp_path):
    """seed_lineage reports HEAD and HEAD^ of a real git repository packed as a /testbed tar (git only on a throwaway copy)."""
    import io, tarfile
    from aiep.seed import parse_seed_archive
    repo = tmp_path / "testbed"
    repo.mkdir()
    env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z"}

    def git(*a):
        return subprocess.run(["git", *a], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    (repo / "a.txt").write_text("1\n")
    git("add", "-A"); git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "a.txt").write_text("1\n")
    git("commit", "-q", "--allow-empty", "-m", "SWE-bench")
    head = git("rev-parse", "HEAD")
    for h in (repo / ".git" / "hooks").glob("*.sample"):
        h.unlink()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        tf.add(repo, arcname="testbed")
    seed = parse_seed_archive(buf.getvalue())
    lin = load_script("validate_handoff").seed_lineage(seed)
    assert lin == {"head": head, "parent": base}

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


# ---------------------------------------------------------------- archive limit: 128 MiB, validated for SymPy only (measured 104,282,112 B = 99.45 MiB)
import io
import tarfile

from aiep import seed as seed_module
from aiep.agent_runner import AgentRunSpec, tmpfs_for
from aiep.seed_source import SeedSourceError
from test_seed import good_entries, make_tar

MIB = 1024**2
SYMPY_TAR_BYTES = 104_282_112  # measured for sympy__sympy-18621's /testbed (streamed through tarfile; nothing stored)


def big_tar(total_payload, n_files=2):
    """A valid /testbed tar whose total size is about ``total_payload`` bytes, spread over ``n_files`` files (each under the 64 MiB per-file limit)."""
    entries = good_entries()
    each = (total_payload - 60_000) // n_files
    for i in range(n_files):
        entries.append((f"data/blob{i}.bin", "f", b"\0" * each, 0o644))
    entries.insert(0, ("data", "d", None, 0o755))
    return make_tar(entries)


def test_the_archive_limit_is_128_mib_and_the_other_limits_and_the_tmpfs_are_unchanged():
    assert ts.MAX_ARCHIVE_BYTES == 128 * MIB
    assert (seed_module.MAX_SEED_BYTES, seed_module.MAX_FILE_BYTES, seed_module.MAX_ENTRIES) == (256 * MIB, 64 * MIB, 200_000)
    spec_default = AgentRunSpec.__dataclass_fields__["testbed_size"].default
    assert spec_default == "192m" and ts.MAX_ARCHIVE_BYTES < 192 * MIB


def test_the_agent_testbed_tmpfs_stays_exactly_192_mib():
    import test_agent_runner as tar_
    spec = tar_.spec_for(tar_.parse_seed_archive(make_tar(good_entries())))
    assert "size=192m" in tmpfs_for(spec)["/testbed"]


def test_an_archive_of_the_measured_sympy_size_is_accepted_and_pinned(root):
    raw = big_tar(SYMPY_TAR_BYTES)
    assert abs(len(raw) - SYMPY_TAR_BYTES) < 200_000 and len(raw) > 64 * MIB  # larger than the old limit, smaller than the new one
    r = go(root, FakeHost(tar=raw, chunk=4 * MIB))
    assert pins(root)[INSTANCE]["sha256"] == r["seed_sha256"] and r["entries"] > 5


def test_an_archive_over_128_mib_is_refused_not_truncated_and_nothing_is_pinned_or_left_behind(root):
    h = FakeHost()
    block = b"\0" * MIB
    h.chunks = [block] * 128 + [b"x"]  # 128 MiB + 1 byte, referencing one block: no big allocation
    with pytest.raises(SeedSourceError, match="exceeds"):
        go(root, h)
    assert INSTANCE not in pins(root) and not list((root / ".cache" / "seeds" / INSTANCE).glob("seed*"))
    assert [c for c, _ in h.removed]  # the seed-source container was still removed


def test_exactly_128_mib_is_not_over_the_limit():
    from aiep.seed_source import read_bounded
    assert len(read_bounded([b"\0" * MIB] * 128, ts.MAX_ARCHIVE_BYTES)) == 128 * MIB
    with pytest.raises(SeedSourceError):
        read_bounded([b"\0" * MIB] * 128 + [b"x"], ts.MAX_ARCHIVE_BYTES)


def test_the_per_file_limit_still_applies_inside_the_larger_archive(root):
    raw = big_tar(66 * MIB, n_files=1)  # one 65.9 MiB file: under the archive limit, over the 64 MiB per-file limit
    assert len(raw) < ts.MAX_ARCHIVE_BYTES
    with pytest.raises(seed_module.SeedError, match="per-file"):
        go(root, FakeHost(tar=raw, chunk=4 * MIB))
    assert INSTANCE not in pins(root) and not list((root / ".cache" / "seeds" / INSTANCE).glob("seed*"))


def test_the_entry_count_and_total_content_limits_still_apply(root, monkeypatch):
    monkeypatch.setattr(seed_module, "MAX_ENTRIES", 3)
    with pytest.raises(seed_module.SeedError, match="too many"):
        go(root, FakeHost())
    monkeypatch.setattr(seed_module, "MAX_ENTRIES", 200_000)
    monkeypatch.setattr(seed_module, "MAX_SEED_BYTES", 500)
    with pytest.raises(seed_module.SeedError, match="total size"):
        go(root, FakeHost())
    assert INSTANCE not in pins(root) and not list((root / ".cache" / "seeds" / INSTANCE).glob("seed*"))

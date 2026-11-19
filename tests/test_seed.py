import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from aiep import seed as S
from aiep.seed import (VERIFY_SCRIPT, SeedError, TreeManifest, build_component_archive, load_seed,
                       parse_seed_archive, save_seed, verify_archive, verify_listing)

HEAD = "a" * 40


def make_tar(entries, root="testbed"):
    """entries: (name, kind, data/target, mode). Names are relative to root unless they start with '!'."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        ti = tarfile.TarInfo(root)
        ti.type, ti.mode = tarfile.DIRTYPE, 0o755
        tf.addfile(ti)
        for name, kind, payload, mode in entries:
            full = name[1:] if name.startswith("!") else f"{root}/{name}"
            ti = tarfile.TarInfo(full)
            ti.mode, ti.mtime, ti.uid, ti.gid = mode, 1_700_000_000, 1000, 1000
            if kind == "d":
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            elif kind == "l":
                ti.type, ti.linkname = tarfile.SYMTYPE, payload
                tf.addfile(ti)
            elif kind == "h":
                ti.type, ti.linkname = tarfile.LNKTYPE, payload
                tf.addfile(ti)
            elif kind == "c":
                ti.type = tarfile.CHRTYPE
                tf.addfile(ti)
            else:
                ti.size = len(payload)
                tf.addfile(ti, io.BytesIO(payload))
    return buf.getvalue()


def good_entries(head_ref=True):
    e = [
        (".git", "d", None, 0o755),
        (".git/HEAD", "f", b"ref: refs/heads/main\n" if head_ref else (HEAD + "\n").encode(), 0o644),
        (".git/refs", "d", None, 0o755),
        (".git/refs/heads", "d", None, 0o755),
        (".git/refs/heads/main", "f", (HEAD + "\n").encode(), 0o644),
        (".git/objects", "d", None, 0o755),
        (".git/objects/pack", "d", None, 0o755),
        (".git/objects/pack/p.pack", "f", b"PACKDATA" * 100, 0o444),
        ("src", "d", None, 0o755),
        ("src/mod.py", "f", b"print('hi')\n", 0o644),
        ("src/run.sh", "f", b"#!/bin/sh\n", 0o755),
        ("README.rst", "f", b"readme\n", 0o644),
        ("link", "l", "src/mod.py", 0o777),
    ]
    return e


def good(**kw):
    return parse_seed_archive(make_tar(good_entries(**kw)))


# ---------------- happy path ----------------
def test_valid_seed_parses_with_head_counts_and_hashes():
    s = good()
    assert s.head == HEAD
    c = s.manifest.counts
    # HEAD 21 + main 41 + pack 800 + mod.py 12 + run.sh 10 + README 7
    assert c == {"files": 6, "dirs": 6, "symlinks": 1, "bytes": 21 + 41 + 800 + 12 + 10 + 7}
    assert s.manifest.archive_sha256 == hashlib.sha256(s.tar).hexdigest()
    mod = next(e for e in s.manifest.entries if e.path == "src/mod.py")
    assert mod.sha256 == hashlib.sha256(b"print('hi')\n").hexdigest() and mod.mode == 0o644
    verify_archive(s)


def test_detached_head_and_packed_refs_resolve_without_git():
    assert good(head_ref=False).head == HEAD
    entries = [e for e in good_entries() if e[0] != ".git/refs/heads/main"]
    entries.append((".git/packed-refs", "f", f"# pack-refs\n{HEAD} refs/heads/main\n".encode(), 0o644))
    assert parse_seed_archive(make_tar(entries)).head == HEAD


def test_archive_is_deterministic_and_root_relative_and_root_owned():
    a, b = good(), good()
    assert a.tar == b.tar and a.manifest.archive_sha256 == b.manifest.archive_sha256
    with tarfile.open(fileobj=io.BytesIO(a.tar)) as tf:
        names = tf.getnames()
        assert names == sorted(names) and not any(n.startswith(("testbed", "/")) for n in names)
        assert all(m.uid == 0 and m.gid == 0 and m.uname == "" for m in tf)


def test_special_mode_bits_are_stripped():
    entries = good_entries() + [("suid", "f", b"x", 0o4755), ("sgid", "f", b"x", 0o2755), ("stick", "d", None, 0o1777)]
    s = parse_seed_archive(make_tar(entries))
    modes = {e.path: e.mode for e in s.manifest.entries}
    assert modes["suid"] == 0o755 and modes["sgid"] == 0o755 and modes["stick"] == 0o777


# ---------------- rejections ----------------
@pytest.mark.parametrize("bad,needle", [
    ([("!/etc/passwd", "f", b"x", 0o644)], "outside"),
    ([("!other/x", "f", b"x", 0o644)], "outside"),
    ([("../x", "f", b"x", 0o644)], "unsafe path component"),
    ([("a/../../x", "f", b"x", 0o644)], "unsafe path component"),
    ([("a//b", "f", b"x", 0o644)], "unsafe path component"),
    ([("evil\nname", "f", b"x", 0o644)], "control"),
    ([("evil\tname", "f", b"x", 0o644)], "control"),
    ([("back\\slash", "f", b"x", 0o644)], "control"),
    ([("dev", "c", None, 0o644)], "special"),
    ([("hl", "h", "src/mod.py", 0o644)], "hard link"),
    ([("abs", "l", "/etc/passwd", 0o777)], "absolute"),
    ([("esc", "l", "../../etc/passwd", 0o777)], "outside"),
    ([("src/esc", "l", "../../x", 0o777)], "outside"),
    ([("empty", "l", "", 0o777)], "absolute or malformed"),
    ([("orphan/file.py", "f", b"x", 0o644)], "no directory entry"),
    ([("link/inner", "f", b"x", 0o644)], "symlink"),
    ([("src/mod.py/inner", "f", b"x", 0o644)], "under a file"),
    ([("README.rst", "f", b"dup", 0o644)], "duplicate"),
])
def test_dangerous_or_malformed_entries_are_refused(bad, needle):
    with pytest.raises(SeedError, match=needle):
        parse_seed_archive(make_tar(good_entries() + bad))


def test_missing_or_unresolvable_head_refused():
    no_head = [e for e in good_entries() if not e[0].startswith(".git/HEAD")]
    with pytest.raises(SeedError, match="no .git/HEAD"):
        parse_seed_archive(make_tar(no_head))
    no_ref = [e for e in good_entries() if e[0] != ".git/refs/heads/main"]
    with pytest.raises(SeedError, match="cannot resolve"):
        parse_seed_archive(make_tar(no_ref))
    for text in (b"ref: refs/../../etc/x\n", b"ref: heads/main\n", b"garbage\n", b"ref: refs/heads/a b\n"):
        entries = [e for e in good_entries() if e[0] != ".git/HEAD"] + [(".git/HEAD", "f", text, 0o644)]
        with pytest.raises(SeedError):
            parse_seed_archive(make_tar(entries))
    entries = [e for e in good_entries() if e[0] != ".git/refs/heads/main"] + [
        (".git/refs/heads/main", "f", b"not-a-sha\n", 0o644)]
    with pytest.raises(SeedError, match="cannot resolve"):
        parse_seed_archive(make_tar(entries))


def test_limits_are_enforced(monkeypatch):
    monkeypatch.setattr(S, "MAX_FILE_BYTES", 100)
    with pytest.raises(SeedError, match="per-file"):
        parse_seed_archive(make_tar(good_entries()))
    monkeypatch.setattr(S, "MAX_FILE_BYTES", 10**9)
    monkeypatch.setattr(S, "MAX_SEED_BYTES", 500)
    with pytest.raises(SeedError, match="total size"):
        parse_seed_archive(make_tar(good_entries()))
    monkeypatch.setattr(S, "MAX_SEED_BYTES", 10**9)
    monkeypatch.setattr(S, "MAX_ENTRIES", 3)
    with pytest.raises(SeedError, match="too many"):
        parse_seed_archive(make_tar(good_entries()))
    monkeypatch.setattr(S, "MAX_ENTRIES", 10**6)
    monkeypatch.setattr(S, "MAX_PATH_LEN", 20)
    with pytest.raises(SeedError, match="unsafe path"):
        parse_seed_archive(make_tar(good_entries() + [("d/" * 30 + "x", "f", b"x", 0o644)]))


def test_not_a_tar_refused():
    with pytest.raises(SeedError, match="not a readable tar"):
        parse_seed_archive(b"this is not a tar archive" * 100)


def test_root_must_be_a_directory():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        ti = tarfile.TarInfo("testbed")
        ti.size = 1
        tf.addfile(ti, io.BytesIO(b"x"))
    with pytest.raises(SeedError, match="root is not a directory"):
        parse_seed_archive(buf.getvalue())


# ---------------- in-container verification (VERIFY_SCRIPT run here against an extracted copy) ----------------
def extract(seed, dest):
    """Stand-in for `tar -x -p --no-same-owner` in the container (python tarfile, modes applied)."""
    dest.mkdir()
    with tarfile.open(fileobj=io.BytesIO(seed.tar)) as tf:
        dirs = []
        for m in tf:
            target = dest / m.name
            if m.isdir():
                target.mkdir(exist_ok=True)
                dirs.append((target, m.mode))
            elif m.issym():
                os.symlink(m.linkname, target)
            else:
                target.write_bytes(tf.extractfile(m).read())
                os.chmod(target, m.mode)
        for d, mode in reversed(dirs):
            os.chmod(d, mode)


def listing(root):
    r = subprocess.run([sys.executable, "-I", "-c", VERIFY_SCRIPT, str(root)], capture_output=True, text=True, check=True)
    return r.stdout


@pytest.fixture
def extracted(tmp_path):
    s = good()
    root = tmp_path / "work"
    extract(s, root)
    yield s, root
    for p in root.rglob("*"):  # allow cleanup of 0444 files/dirs
        if not p.is_symlink():
            try:
                os.chmod(p, 0o755 if p.is_dir() else 0o644)
            except OSError:
                pass


def test_untouched_extraction_matches_the_manifest_exactly(extracted):
    s, root = extracted
    assert verify_listing(s.manifest, listing(root)) == []


def test_every_kind_of_tampering_is_detected(extracted):
    s, root = extracted

    def problems():
        return verify_listing(s.manifest, listing(root))

    (root / "src" / "mod.py").write_bytes(b"print('HACKED')\n")
    assert any("content differs: src/mod.py" in p for p in problems())
    (root / "src" / "mod.py").write_bytes(b"print('hi')\n")
    assert problems() == []

    (root / "extra.txt").write_text("x")
    assert "unexpected entry: extra.txt" in problems()
    (root / "extra.txt").unlink()

    (root / "newdir").mkdir()
    assert "unexpected entry: newdir" in problems()
    (root / "newdir").rmdir()

    (root / "README.rst").unlink()
    assert "missing: README.rst" in problems()
    (root / "README.rst").write_bytes(b"readme\n")
    os.chmod(root / "README.rst", 0o644)

    os.chmod(root / "src" / "run.sh", 0o644)
    assert any(p.startswith("mode differs: src/run.sh") for p in problems())
    os.chmod(root / "src" / "run.sh", 0o755)

    os.unlink(root / "link")
    os.symlink("README.rst", root / "link")
    assert "symlink target differs: link" in problems()
    os.unlink(root / "link")
    os.symlink("src/mod.py", root / "link")

    os.unlink(root / "README.rst")
    os.symlink("src/mod.py", root / "README.rst")
    assert "type differs: README.rst (f -> l)" in problems()
    os.unlink(root / "README.rst")
    (root / "README.rst").write_bytes(b"readme\n")
    os.chmod(root / "README.rst", 0o644)

    os.mkfifo(root / "pipe")
    assert "unexpected special file: pipe" in problems()
    os.unlink(root / "pipe")
    assert problems() == []


def test_garbage_listing_is_reported_not_trusted():
    s = good()
    for bad in ("not json", "[[1,2]]", '[["f",420,3,"x",null,"a"],["f",420,3,"x",null,"a"]]'):
        assert verify_listing(s.manifest, bad)


# ---------------- tar/manifest integrity, persistence ----------------
def test_tampering_with_the_stored_tar_or_manifest_is_detected(tmp_path):
    s = good()
    save_seed(tmp_path / "s", s)
    assert load_seed(tmp_path / "s", expected_sha256=s.manifest.archive_sha256).head == HEAD

    with pytest.raises(SeedError, match="expected SHA-256"):
        load_seed(tmp_path / "s", expected_sha256="0" * 64)

    tar_path = tmp_path / "s" / "seed.tar"
    original = tar_path.read_bytes()
    tar_path.write_bytes(original + b"\0" * 512)
    with pytest.raises(SeedError, match="recorded SHA-256"):
        load_seed(tmp_path / "s")
    tar_path.write_bytes(original)

    man = json.loads((tmp_path / "s" / "seed_manifest.json").read_text())
    man["entries"][5][3] = "0" * 64  # lie about one file's hash, keep the tar hash
    (tmp_path / "s" / "seed_manifest.json").write_text(json.dumps(man))
    with pytest.raises(SeedError, match="differ from the recorded manifest"):
        load_seed(tmp_path / "s")


def test_save_never_overwrites(tmp_path):
    s = good()
    save_seed(tmp_path, s)
    with pytest.raises(FileExistsError):
        save_seed(tmp_path, s)


def test_manifest_roundtrips_and_schema_is_checked():
    s = good()
    again = TreeManifest.from_json(s.manifest.to_json())
    assert again.entries == s.manifest.entries and again.head == HEAD
    with pytest.raises(SeedError, match="schema"):
        TreeManifest.from_json(json.dumps({"schema": 99}))


def test_describe_has_no_file_contents():
    d = S.describe(good())
    assert set(d) == {"archive_sha256", "head", "files", "dirs", "symlinks", "bytes"} and d["head"] == HEAD


# ---------------- component archives (Claude binary, gateway code) ----------------
def test_component_archive_has_implicit_dirs_modes_and_verifies(tmp_path):
    comp = build_component_archive({"claude": (b"\x7fELF" + b"0" * 100, 0o755), "lib/data.json": (b"{}", 0o644)})
    assert {e.path: (e.type, e.mode) for e in comp.manifest.entries} == {
        "claude": ("f", 0o755), "lib": ("d", 0o755), "lib/data.json": ("f", 0o644)}
    assert comp.manifest.head is None
    verify_archive(comp)
    root = tmp_path / "aiep"
    extract(comp, root)
    assert verify_listing(comp.manifest, listing(root)) == []


def test_component_paths_are_validated():
    for bad in ("../x", "/abs", "a//b", "a\nb"):
        with pytest.raises(SeedError):
            build_component_archive({bad: (b"x", 0o644)})


# ---------------- ownership and interpreter portability ----------------
def test_listing_reports_owner_and_expect_owner_is_enforced(extracted):
    s, root = extracted
    me = (os.getuid(), os.getgid())
    assert verify_listing(s.manifest, listing(root), expect_owner=me) == []
    problems = verify_listing(s.manifest, listing(root), expect_owner=(me[0] + 1, me[1]))
    assert problems and all(p.startswith("owner differs") for p in problems)


def test_expect_owner_fails_when_the_listing_has_no_owner_columns(extracted):
    s, root = extracted
    short = json.dumps([r[:6] for r in json.loads(listing(root))])
    assert verify_listing(s.manifest, short) == []  # without expect_owner, older six-column listings still compare
    assert all(p.startswith("owner not reported") for p in verify_listing(s.manifest, short, expect_owner=(1, 1)))


def test_verify_script_is_plain_stdlib_syntax_the_images_interpreter_can_parse():
    import ast
    ast.parse(VERIFY_SCRIPT, feature_version=(3, 8))
    assert "import hashlib, json, os, stat, sys" in VERIFY_SCRIPT

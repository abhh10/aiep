"""The repository seed, and other archives injected into a container, with integrity records.

Flow (the Docker steps themselves are Phase 2 and approval-gated):

1. The orchestrator reads ``/testbed`` once from a pristine, never-started container of the
   pinned instance image (``get_archive``) and hands the tar to ``parse_seed_archive``.
2. ``parse_seed_archive`` refuses anything but regular files, directories and in-tree relative
   symlinks, with safe paths and size limits; strips setuid/setgid/sticky bits; resolves the seed's
   own ``HEAD`` commit from the archive without running git; and returns a deterministic,
   root-relative *injection tar* plus a ``TreeManifest`` (per-entry type, mode, size, SHA-256).
   The archive SHA-256 is recorded in the manifest.
3. The injection tar is streamed into the agent container's ``/testbed`` tmpfs with
   ``docker exec`` (see ``container_exec``). ``docker cp`` / ``put_archive`` cannot be used for a
   container with a read-only root filesystem and cannot reach a tmpfs before it is mounted.
4. ``VERIFY_SCRIPT`` runs inside the container before the agent starts and lists what was actually
   extracted; ``verify_listing`` compares that to the manifest, entry by entry.

The seed's ``HEAD`` is the instance image's synthetic "SWE-bench" commit, not the dataset's
``base_commit`` (docs/M1_RESULTS.md section 8.6). Patches are diffed against it.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import posixpath
import re
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

SCHEMA = 1
MAX_SEED_BYTES = 256 * 1024**2  # total extracted content of a seed
# One regular file inside a SEED (a repository checkout). Raised from 64 MiB: the measured Git pack of sympy__sympy-22714 is 64.792 MiB (67,939,731 B). It applies to seed files ONLY; the
# trusted-component ceilings below (and agent_runner.MAX_COMPONENT_FILE_BYTES for the Claude binary) are separate constants, so changing this one never changes them.
MAX_SEED_FILE_BYTES = 96 * 1024**2
# The default per-file ceiling of ``build_component_archive`` (gateway/proxy code, probes). It was once derived as 4 x the seed-file limit; it is now its own constant with the same value,
# so that the seed-file limit can change without moving it. The Claude binary passes its own explicit ceiling (agent_runner.MAX_COMPONENT_FILE_BYTES, 400 MiB).
DEFAULT_COMPONENT_FILE_BYTES = 256 * 1024**2
MAX_ENTRIES = 200_000
MAX_PATH_LEN = 4096
MAX_COMPONENT_LEN = 255
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REF_RE = re.compile(r"^refs/[A-Za-z0-9._\-]+(/[A-Za-z0-9._\-]+)*$")
_SMALL_GIT_FILES = 8192


class SeedError(ValueError):
    """The archive or a verification step is not acceptable."""


@dataclass(frozen=True)
class Entry:
    type: str  # "f" file, "d" directory, "l" symlink
    mode: int  # permission bits only (setuid/setgid/sticky stripped)
    size: int
    sha256: str | None
    link: str | None
    path: str  # relative to the root, '/'-separated

    def as_list(self) -> list:
        return [self.type, self.mode, self.size, self.sha256, self.link, self.path]


@dataclass
class TreeManifest:
    archive_sha256: str  # SHA-256 of the injection tar
    entries: list[Entry]
    head: str | None = None  # seed HEAD commit (seeds only)
    schema: int = SCHEMA

    @property
    def counts(self) -> dict[str, int]:
        c = {"f": 0, "d": 0, "l": 0}
        for e in self.entries:
            c[e.type] += 1
        return {"files": c["f"], "dirs": c["d"], "symlinks": c["l"], "bytes": sum(e.size for e in self.entries)}

    def to_json(self) -> str:
        return json.dumps({"schema": self.schema, "head": self.head, "archive_sha256": self.archive_sha256,
                           "counts": self.counts, "entries": [e.as_list() for e in self.entries]},
                          separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "TreeManifest":
        d = json.loads(text)
        if d.get("schema") != SCHEMA:
            raise SeedError("unsupported manifest schema")
        return cls(archive_sha256=d["archive_sha256"], head=d.get("head"),
                   entries=[Entry(*e) for e in d["entries"]])


@dataclass
class Seed:
    manifest: TreeManifest
    tar: bytes = field(repr=False)

    @property
    def head(self) -> str:
        assert self.manifest.head
        return self.manifest.head


# ------------------------------------------------------------------ path / entry validation
def _check_relpath(rel: str) -> None:
    if not rel or rel.startswith("/") or len(rel) > MAX_PATH_LEN:
        raise SeedError(f"unsafe path {rel[:80]!r}")
    for c in rel:
        if c in "\x00\n\t\\" or ord(c) < 0x20:
            raise SeedError(f"control or separator character in path {rel[:80]!r}")
    for comp in rel.split("/"):
        if comp in ("", ".", "..") or len(comp) > MAX_COMPONENT_LEN:
            raise SeedError(f"unsafe path component in {rel[:80]!r}")


def _check_symlink(rel: str, target: str) -> None:
    if not target or target.startswith("/") or "\x00" in target or "\n" in target or len(target) > MAX_PATH_LEN:
        raise SeedError(f"symlink {rel!r} has an absolute or malformed target")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
    if resolved == ".." or resolved.startswith("../"):
        raise SeedError(f"symlink {rel!r} points outside the tree")


def _resolve_head(small: dict[str, bytes]) -> str:
    head = small.get(".git/HEAD")
    if head is None:
        raise SeedError("archive has no .git/HEAD")
    text = head.decode("ascii", "replace").strip()
    if _SHA_RE.match(text):
        return text
    if not text.startswith("ref: "):
        raise SeedError("cannot understand .git/HEAD")
    ref = text[5:].strip()
    if not _REF_RE.match(ref) or ".." in ref:
        raise SeedError("HEAD names an unsafe ref")
    loose = small.get(f".git/{ref}")
    if loose is not None and _SHA_RE.match(loose.decode("ascii", "replace").strip()):
        return loose.decode().strip()
    packed = small.get(".git/packed-refs", b"").decode("ascii", "replace")
    for line in packed.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == ref and _SHA_RE.match(parts[0]):
            return parts[0]
    raise SeedError(f"cannot resolve {ref} to a commit from the archive (no loose ref or packed-refs entry)")


# ------------------------------------------------------------------ building
def _build(items: list[tuple[Entry, bytes | None, int]]) -> bytes:
    """Deterministic GNU tar of entries sorted by path (parents before children)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        for entry, data, mtime in sorted(items, key=lambda t: t[0].path):
            ti = tarfile.TarInfo(entry.path)
            ti.mode, ti.uid, ti.gid, ti.uname, ti.gname, ti.mtime = entry.mode, 0, 0, "", "", mtime
            if entry.type == "d":
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            elif entry.type == "l":
                ti.type, ti.linkname = tarfile.SYMTYPE, entry.link or ""
                tf.addfile(ti)
            else:
                ti.type, ti.size = tarfile.REGTYPE, entry.size
                tf.addfile(ti, io.BytesIO(data or b""))
    return buf.getvalue()


def _check_tree(entries: list[Entry]) -> None:
    kinds = {e.path: e.type for e in entries}
    if len(kinds) != len(entries):
        raise SeedError("archive contains duplicate paths")
    for e in entries:
        parent = posixpath.dirname(e.path)
        while parent:
            k = kinds.get(parent)
            if k is None:
                raise SeedError(f"{e.path!r} has no directory entry for its parent {parent!r}")
            if k != "d":
                raise SeedError(f"{e.path!r} lies under a {'symlink' if k == 'l' else 'file'}, not a directory")
            parent = posixpath.dirname(parent)


def parse_seed_archive(data: bytes | BinaryIO, *, root: str = "testbed") -> Seed:
    """Validate a ``get_archive`` tar of ``/<root>`` and return the normalised seed."""
    raw = data.read() if hasattr(data, "read") else data
    if len(raw) > MAX_SEED_BYTES + MAX_ENTRIES * 1536:  # content limit plus tar header/padding overhead
        raise SeedError("archive is too large")
    try:
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:*")
    except tarfile.TarError as e:
        raise SeedError(f"not a readable tar archive: {e}") from e
    items: list[tuple[Entry, bytes | None, int]] = []
    small: dict[str, bytes] = {}
    total = 0
    with tf:
        for m in tf:
            if len(items) >= MAX_ENTRIES:
                raise SeedError("archive has too many entries")
            name = m.name.rstrip("/")
            first, _, rel = name.partition("/")
            if first != root:
                raise SeedError(f"entry {name[:80]!r} is outside {root}/")
            if not rel:  # the root directory itself
                if not m.isdir():
                    raise SeedError("the archive root is not a directory")
                continue
            _check_relpath(rel)
            mode = m.mode & 0o777
            if m.isdir():
                items.append((Entry("d", mode, 0, None, None, rel), None, int(m.mtime)))
            elif m.issym():
                _check_symlink(rel, m.linkname)
                items.append((Entry("l", 0o777, 0, None, m.linkname, rel), None, int(m.mtime)))
            elif m.isreg():
                if m.size > MAX_SEED_FILE_BYTES:
                    raise SeedError(f"{rel!r} is larger than the per-file limit")
                total += m.size
                if total > MAX_SEED_BYTES:
                    raise SeedError("archive exceeds the total size limit")
                body = tf.extractfile(m).read()  # type: ignore[union-attr]
                if len(body) != m.size:
                    raise SeedError(f"{rel!r} is truncated")
                if rel.startswith(".git/") and len(body) <= _SMALL_GIT_FILES and (
                        rel in (".git/HEAD", ".git/packed-refs") or rel.startswith(".git/refs/")):
                    small[rel] = body
                items.append((Entry("f", mode, m.size, hashlib.sha256(body).hexdigest(), None, rel), body, int(m.mtime)))
            else:
                kind = "hard link" if m.islnk() else "device, fifo or other special file"
                raise SeedError(f"{rel!r} is a {kind}; refused")
    entries = [i[0] for i in items]
    _check_tree(entries)
    head = _resolve_head(small)
    tar = _build(items)
    entries_sorted = sorted(entries, key=lambda e: e.path)
    return Seed(TreeManifest(hashlib.sha256(tar).hexdigest(), entries_sorted, head), tar)


def build_component_archive(files: dict[str, tuple[bytes, int]], *, mtime: int = 0,
                            max_file_bytes: int = DEFAULT_COMPONENT_FILE_BYTES) -> Seed:
    """Injection tar for a few trusted files, e.g. ``{"claude": (binary_bytes, 0o755)}``.

    Directories are created implicitly (mode 0755). Used for the Claude Code binary and the
    gateway's code; paths are validated like seed paths. ``max_file_bytes`` is a per-file ceiling (default 256 MiB); a caller
    that must ship a larger trusted file (the Claude CLI) raises it explicitly, and the file is still refused above that.
    """
    if type(max_file_bytes) is not int or max_file_bytes <= 0:
        raise SeedError("max_file_bytes must be a positive integer")
    items: list[tuple[Entry, bytes | None, int]] = []
    dirs: set[str] = set()
    for rel, (body, mode) in files.items():
        _check_relpath(rel)
        if len(body) > max_file_bytes:
            raise SeedError(f"{rel!r} is too large")
        parent = posixpath.dirname(rel)
        while parent:
            dirs.add(parent)
            parent = posixpath.dirname(parent)
        items.append((Entry("f", mode & 0o777, len(body), hashlib.sha256(body).hexdigest(), None, rel), body, mtime))
    items += [(Entry("d", 0o755, 0, None, None, d), None, mtime) for d in dirs]
    entries = [i[0] for i in items]
    _check_tree(entries)
    tar = _build(items)
    return Seed(TreeManifest(hashlib.sha256(tar).hexdigest(), sorted(entries, key=lambda e: e.path), None), tar)


# ------------------------------------------------------------------ verification
# Runs INSIDE the container: ``<configured interpreter> -I -c VERIFY_SCRIPT <root>`` lists what is really
# there (type, mode, size, SHA-256, link target, path, owner uid, owner gid). It uses only the standard
# library and syntax that every supported CPython 3 accepts, because the image's interpreter is not ours.
VERIFY_SCRIPT = r'''
import hashlib, json, os, stat, sys
root = sys.argv[1]
def fail(e):
    raise e
out = []
for dp, dn, fn in os.walk(root, followlinks=False, onerror=fail):
    for name in sorted(dn + fn):
        p = os.path.join(dp, name)
        st = os.lstat(p)
        rel = os.path.relpath(p, root)
        m = stat.S_IMODE(st.st_mode)
        if stat.S_ISLNK(st.st_mode):
            out.append(["l", 0o777, 0, None, os.readlink(p), rel, st.st_uid, st.st_gid])
        elif stat.S_ISDIR(st.st_mode):
            out.append(["d", m, 0, None, None, rel, st.st_uid, st.st_gid])
        elif stat.S_ISREG(st.st_mode):
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for b in iter(lambda: f.read(1 << 20), b""):
                    h.update(b)
            out.append(["f", m, st.st_size, h.hexdigest(), None, rel, st.st_uid, st.st_gid])
        else:
            out.append(["x", m, 0, None, None, rel, st.st_uid, st.st_gid])
json.dump(out, sys.stdout, separators=(",", ":"))
'''


def verify_listing(manifest: TreeManifest, listing_json: str | bytes, *,
                   expect_owner: tuple[int, int] | None = None) -> list[str]:
    """Compare ``VERIFY_SCRIPT`` output with the manifest. Returns problems (empty means identical).

    With ``expect_owner=(uid, gid)`` every entry must also be owned by exactly that identity, and a
    listing that does not report owners fails (so the check can never be skipped silently).
    """
    try:
        raw = json.loads(listing_json)
        found = {r[5]: Entry(*r[:6]) for r in raw}
        owners = {r[5]: (r[6], r[7]) if len(r) >= 8 else None for r in raw}
        if len(found) != len(raw):
            return ["listing contains duplicate paths"]
    except (ValueError, TypeError, IndexError) as e:
        return [f"listing is not valid: {e}"]
    want = {e.path: e for e in manifest.entries}
    problems: list[str] = []
    for path in sorted(set(want) - set(found)):
        problems.append(f"missing: {path}")
    for path in sorted(set(found) - set(want)):
        problems.append(f"unexpected {'special file' if found[path].type == 'x' else 'entry'}: {path}")
    for path in sorted(set(want) & set(found)):
        w, f = want[path], found[path]
        if w.type != f.type:
            problems.append(f"type differs: {path} ({w.type} -> {f.type})")
        elif w.type == "f" and (w.sha256 != f.sha256 or w.size != f.size):
            problems.append(f"content differs: {path}")
        elif w.type == "l" and w.link != f.link:
            problems.append(f"symlink target differs: {path}")
        elif w.type in ("f", "d") and w.mode != f.mode:
            problems.append(f"mode differs: {path} ({w.mode:o} -> {f.mode:o})")
    if expect_owner is not None:
        for path in sorted(found):
            got = owners[path]
            if got is None:
                problems.append(f"owner not reported: {path}")
            elif tuple(got) != tuple(expect_owner):
                problems.append(f"owner differs: {path} ({got[0]}:{got[1]} -> {expect_owner[0]}:{expect_owner[1]})")
    return problems


def verify_archive(seed: Seed) -> None:
    """Check the injection tar still matches its own manifest (hash and every entry)."""
    if hashlib.sha256(seed.tar).hexdigest() != seed.manifest.archive_sha256:
        raise SeedError("injection tar does not match the recorded SHA-256")
    entries: list[Entry] = []
    with tarfile.open(fileobj=io.BytesIO(seed.tar), mode="r:") as tf:
        for m in tf:
            if m.isdir():
                entries.append(Entry("d", m.mode & 0o777, 0, None, None, m.name))
            elif m.issym():
                entries.append(Entry("l", 0o777, 0, None, m.linkname, m.name))
            elif m.isreg():
                h, f = hashlib.sha256(), tf.extractfile(m)
                for block in iter(lambda: f.read(1 << 20), b""):  # type: ignore[union-attr]  # streamed: no second copy of a large file
                    h.update(block)
                entries.append(Entry("f", m.mode & 0o777, m.size, h.hexdigest(), None, m.name))
            else:
                raise SeedError(f"injection tar contains a special entry: {m.name!r}")
    if sorted(entries, key=lambda e: e.path) != sorted(seed.manifest.entries, key=lambda e: e.path):
        raise SeedError("injection tar entries differ from the recorded manifest")


# ------------------------------------------------------------------ persistence
def _write_new(path: Path, data: bytes) -> None:
    """Create ``path`` (it must not exist), write ``data`` and flush it to disk."""
    with open(path, "xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _remove_own(paths: list[Path]) -> None:
    """Remove files this call created. A failure is reported on stderr and never replaces the error being handled."""
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            sys.stderr.write(f"WARNING: could not remove {p}: {type(e).__name__}: {e}\n")


def save_seed(directory: Path, seed: Seed, *, stem: str = "seed") -> tuple[Path, Path]:
    """Write ``<stem>.tar`` and ``<stem>_manifest.json`` into a run/cache directory; never overwrite.

    Both files are written under temporary names and flushed first, then published with ``os.link``, which fails
    if the target exists (so an existing file is never replaced, and a file that appears meanwhile is not
    clobbered). The manifest is published last: a manifest without its tar cannot occur, and a failure at any step
    removes whatever THIS call created (temporary names and anything it already published) and nothing else, so a
    retry with the same stem can succeed. Only a hard kill (SIGKILL, power loss) between the two publishes can leave
    a tar without a manifest; that file has no authority (``load_seed`` needs the manifest) and is for the operator to
    remove.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    tar_path, man_path = directory / f"{stem}.tar", directory / f"{stem}_manifest.json"
    for p in (tar_path, man_path):
        if p.exists() or p.is_symlink():
            raise FileExistsError(f"{p} already exists; a stored seed is never overwritten")
    token = f"{os.getpid()}.{os.urandom(6).hex()}"
    tmp_tar, tmp_man = directory / f".{stem}.tar.{token}.tmp", directory / f".{stem}_manifest.json.{token}.tmp"
    published: list[Path] = []
    try:
        _write_new(tmp_tar, seed.tar)
        _write_new(tmp_man, (seed.manifest.to_json() + "\n").encode())
        for tmp, final in ((tmp_tar, tar_path), (tmp_man, man_path)):
            os.link(tmp, final)  # FileExistsError if it exists: never replaces
            published.append(final)
    except BaseException:
        _remove_own(published)
        raise
    finally:
        _remove_own([tmp_tar, tmp_man])
    return tar_path, man_path


def load_seed(directory: Path, *, stem: str = "seed", expected_sha256: str | None = None) -> Seed:
    """Load and fully re-verify a stored seed. ``expected_sha256`` pins the tar hash recorded elsewhere."""
    directory = Path(directory)
    seed = Seed(TreeManifest.from_json((directory / f"{stem}_manifest.json").read_text()),
                (directory / f"{stem}.tar").read_bytes())
    if expected_sha256 is not None and seed.manifest.archive_sha256 != expected_sha256:
        raise SeedError("stored seed does not have the expected SHA-256")
    verify_archive(seed)
    return seed


def describe(seed: Seed) -> dict[str, Any]:
    """Small, loggable summary (no file contents)."""
    return {"archive_sha256": seed.manifest.archive_sha256, "head": seed.manifest.head, **seed.manifest.counts}

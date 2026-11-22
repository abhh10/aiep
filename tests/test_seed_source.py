"""Seed-source extraction with a fake Docker client and fake archive streams: no daemon, no container, no network."""
import io
import os
import tarfile

import pytest

from aiep.container_policy import PolicyViolation
from aiep import seed as seed_module
from aiep import seed_source as ss
from aiep.seed import SeedError, load_seed, parse_seed_archive
from aiep.seed_source import (NAME_PREFIX, SEED_ROOT, SeedSourceError, extract_seed, max_archive_bytes, read_bounded,
                              seed_source_kwargs, validate_seed_source_kwargs, verify_seed_source_inspect)
from test_seed import good_entries, make_tar

IMAGE = "sha256:" + "c" * 64
RUN = "seedrun1"


class Stream:
    """A chunk generator-like object that records whether it was closed."""

    def __init__(self, chunks, error=None):
        self._it, self.error, self.closed = iter(chunks), error, False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self._it)
        except StopIteration:
            if self.error:
                raise self.error
            raise

    def close(self):
        self.closed = True


def chunked(data, n):
    return [data[i:i + n] for i in range(0, len(data), n)]


class FakeContainer:
    def __init__(self, host, cid, kwargs):
        self.host, self.id, self.kwargs = host, cid, kwargs

    def start(self):
        self.host.calls.append("start")
        raise AssertionError("a seed-source container must never be started")

    def get_archive(self, path, *a, **kw):
        self.host.calls.append(("get_archive", path))
        if self.host.archive_error:
            raise self.host.archive_error
        self.host.stream = Stream(self.host.chunks, self.host.stream_error)
        if self.host.archive_return is not None:
            return self.host.archive_return
        return self.host.stream, self.host.stat


class FakeContainers:
    def __init__(self, host):
        self.host = host

    def create(self, image, command=None, **kwargs):
        self.host.calls.append("create")
        if self.host.create_error:
            raise self.host.create_error
        self.host.created.append({"image": image, "command": command, **kwargs})
        c = FakeContainer(self.host, "c-" + "1" * 12, kwargs)
        self.host.inspects[c.id] = self.host.make_inspect(image, kwargs)
        return c


class FakeImages:
    def __init__(self, host):
        self.host = host

    def get(self, ref):
        self.host.calls.append(("images.get", ref))
        if ref not in self.host.images:
            raise LookupError("no such image")
        return type("Img", (), {"id": self.host.images[ref]})()


class FakeApi:
    def __init__(self, host):
        self.host = host

    def inspect_container(self, cid):
        self.host.calls.append(("inspect", cid))
        return self.host.inspects[cid]

    def remove_container(self, cid, **kw):
        self.host.calls.append(("remove", cid))
        self.host.removed.append((cid, kw))
        if self.host.remove_error:
            raise self.host.remove_error


class FakeHost:
    def __init__(self, tar=None, chunk=4096):
        self.calls, self.created, self.removed, self.inspects = [], [], [], {}
        self.images = {IMAGE: IMAGE}
        self.unrelated = ["other-container-1", "other-container-2"]  # exist on the "host"; must never be touched
        self.chunks = chunked(tar if tar is not None else make_tar(good_entries()), chunk)
        self.archive_error = self.stream_error = self.remove_error = self.create_error = None
        self.archive_return = None
        self.stat = {"name": "testbed", "size": 4096, "mode": 2147484141, "mtime": "2026-01-01T00:00:00Z", "linkTarget": ""}
        self.stream = None
        self.client = type("Client", (), {})()
        self.client.containers, self.client.images, self.client.api = FakeContainers(self), FakeImages(self), FakeApi(self)

    def make_inspect(self, image, kw):
        return {"Image": image, "State": {"Status": "created", "Running": False, "StartedAt": "0001-01-01T00:00:00Z"},
                "Config": {"Labels": kw["labels"]}, "Mounts": [],
                "HostConfig": {"NetworkMode": kw["network_mode"], "Privileged": False, "CapDrop": kw["cap_drop"],
                               "Binds": None, "Devices": None, "PortBindings": {}, "IpcMode": "private"}}


@pytest.fixture
def host():
    return FakeHost()


def run(host, tmp_path, **kw):
    return extract_seed(host.client, image_id=IMAGE, run_id=RUN, directory=tmp_path / "seeds", **kw)


# ------------------------------------------------------------------ happy path
def test_success_saves_reloads_and_removes_exactly_the_created_container(host, tmp_path):
    r = run(host, tmp_path)
    assert r.seed.head == "a" * 40
    assert r.tar_path.is_file() and r.manifest_path.is_file()
    again = load_seed(tmp_path / "seeds", expected_sha256=r.seed.manifest.archive_sha256)
    assert again.manifest.entries == r.seed.manifest.entries and again.tar == r.seed.tar
    assert r.removed and [c for c, _ in host.removed] == [r.container_id]
    assert not set(host.unrelated) & {c for c, _ in host.removed}
    assert host.removed[0][1] == {"v": True, "force": False}


def test_exact_image_id_network_none_and_nothing_else_is_passed(host, tmp_path):
    run(host, tmp_path)
    (c,) = host.created
    assert c["image"] == IMAGE and c["command"] == ["/bin/true"]
    assert c["name"] == NAME_PREFIX + RUN and c["network_mode"] == "none"
    assert c["labels"]["aiep.run_id"] == RUN
    assert c["read_only"] is True and c["cap_drop"] == ["ALL"] and c["security_opt"] == ["no-new-privileges"]
    for forbidden in ("volumes", "mounts", "ports", "devices", "privileged", "environment", "user", "network",
                      "binds", "tmpfs", "cap_add", "pid_mode", "ipc_mode", "extra_hosts", "dns", "volumes_from"):
        assert forbidden not in c
    assert "sk-" not in repr(c)


def test_container_is_never_started_and_archive_path_is_testbed(host, tmp_path):
    run(host, tmp_path)
    assert "start" not in host.calls
    assert ("get_archive", "/testbed") in host.calls and SEED_ROOT == "/testbed"
    assert not any(isinstance(c, tuple) and c[0] == "images.get" and c[1] != IMAGE for c in host.calls)


def test_chunked_reading_any_chunk_size(tmp_path):
    for n in (1, 7, 511, 1 << 20):
        h = FakeHost(chunk=n)
        r = extract_seed(h.client, image_id=IMAGE, run_id=RUN, directory=tmp_path / f"s{n}")
        assert r.seed.head == "a" * 40
        assert h.stream.closed


def test_stream_is_closed_on_success_and_failure():
    s = Stream([b"abc"])
    assert read_bounded(s, 10) == b"abc" and s.closed
    s = Stream([b"abcdef"])
    with pytest.raises(SeedSourceError):
        read_bounded(s, 3)
    assert s.closed


# ------------------------------------------------------------------ bounded reading
def test_archive_over_limit_is_refused_not_truncated(host, tmp_path):
    n = len(b"".join(host.chunks))
    with pytest.raises(SeedSourceError, match="exceeds"):
        run(host, tmp_path, max_bytes=n - 1)
    assert not (tmp_path / "seeds" / "seed.tar").exists()
    assert [c for c, _ in host.removed] == ["c-" + "1" * 12]


def test_exactly_at_limit_is_accepted(host, tmp_path):
    assert run(host, tmp_path, max_bytes=len(b"".join(host.chunks))).seed.head


def test_limit_stops_consuming_the_stream():
    consumed = []

    def gen():
        for i in range(1000):
            consumed.append(i)
            yield b"x" * 100

    with pytest.raises(SeedSourceError):
        read_bounded(gen(), 250)
    assert len(consumed) == 3  # stopped at the chunk that crossed the limit, did not drain 1000 chunks


def test_default_limit_matches_the_parsers_raw_limit():
    from aiep import seed as S
    assert max_archive_bytes() == S.MAX_SEED_BYTES + S.MAX_ENTRIES * 1536


@pytest.mark.parametrize("chunks", [["text"], [b"ok", None], [b"ok", 7], [[1, 2]], [memoryview(b"ab")]])
def test_invalid_chunks_are_refused(chunks):
    with pytest.raises(SeedSourceError, match="invalid archive chunk"):
        read_bounded(Stream(chunks), 100)


@pytest.mark.parametrize("stream", [b"raw bytes", "text", 5, None])
def test_non_iterable_or_bytes_stream_is_refused(stream):
    with pytest.raises(SeedSourceError):
        read_bounded(stream, 100)


def test_empty_stream_is_refused():
    with pytest.raises(SeedSourceError, match="empty"):
        read_bounded(Stream([]), 100)


def test_stream_error_midway_is_reported(host, tmp_path):
    host.stream_error = ConnectionError("reset")
    with pytest.raises(SeedSourceError, match="ConnectionError"):
        run(host, tmp_path)
    assert len(host.removed) == 1


# ------------------------------------------------------------------ failures: parse, archive, cleanup
def test_invalid_tar_is_refused_and_container_removed(tmp_path):
    h = FakeHost(tar=b"this is not a tar archive" * 50)
    with pytest.raises(SeedError, match="not a readable tar"):
        extract_seed(h.client, image_id=IMAGE, run_id=RUN, directory=tmp_path / "s")
    assert len(h.removed) == 1 and not (tmp_path / "s" / "seed.tar").exists()


def test_hard_link_is_rejected_unchanged(tmp_path):
    h = FakeHost(tar=make_tar(good_entries() + [("hl", "h", "src/mod.py", 0o644)]))
    with pytest.raises(SeedError, match="hard link"):
        extract_seed(h.client, image_id=IMAGE, run_id=RUN, directory=tmp_path / "s")
    assert len(h.removed) == 1
    assert not (tmp_path / "s").exists() or not list((tmp_path / "s").iterdir())


@pytest.mark.parametrize("bad", [("../x", "f", b"x", 0o644), ("abs", "l", "/etc/passwd", 0o777),
                                 ("dev", "c", None, 0o644), ("up", "l", "../../x", 0o777)])
def test_other_protections_still_apply(tmp_path, bad):
    h = FakeHost(tar=make_tar(good_entries() + [bad]))
    with pytest.raises(SeedError):
        extract_seed(h.client, image_id=IMAGE, run_id=RUN, directory=tmp_path / "s")
    assert len(h.removed) == 1


def test_get_archive_failure_cleans_up(host, tmp_path):
    host.archive_error = RuntimeError("daemon says no")
    with pytest.raises(SeedSourceError, match="get_archive"):
        run(host, tmp_path)
    assert [c for c, _ in host.removed] == ["c-" + "1" * 12]


def test_missing_head_is_refused_and_cleans_up(tmp_path):
    entries = [e for e in good_entries() if not e[0].startswith(".git/HEAD")]
    h = FakeHost(tar=make_tar(entries))
    with pytest.raises(SeedError, match="HEAD"):
        extract_seed(h.client, image_id=IMAGE, run_id=RUN, directory=tmp_path / "s")
    assert len(h.removed) == 1


def test_cleanup_failure_after_success_is_reported_not_hidden(host, tmp_path):
    host.remove_error = RuntimeError("busy")
    with pytest.raises(SeedSourceError, match="could not be removed"):
        run(host, tmp_path)
    assert (tmp_path / "seeds" / "seed.tar").is_file()  # the seed itself was saved


def test_cleanup_failure_does_not_mask_the_original_error(tmp_path, capsys):
    h = FakeHost(tar=b"junk" * 100)
    h.remove_error = RuntimeError("busy")
    with pytest.raises(SeedError, match="not a readable tar"):
        extract_seed(h.client, image_id=IMAGE, run_id=RUN, directory=tmp_path / "s")
    assert "could not be removed" in capsys.readouterr().err


def test_no_unrelated_container_is_ever_removed_or_touched(host, tmp_path):
    host.archive_error = RuntimeError("x")
    with pytest.raises(SeedSourceError):
        run(host, tmp_path)
    touched = {c[1] for c in host.calls if isinstance(c, tuple) and len(c) > 1 and c[0] in ("remove", "inspect")}
    assert touched == {"c-" + "1" * 12}


# ------------------------------------------------------------------ duplicate protection
def test_existing_seed_is_never_overwritten_and_docker_is_not_touched(host, tmp_path):
    run(host, tmp_path)
    before = (tmp_path / "seeds" / "seed.tar").read_bytes()
    h2 = FakeHost()
    with pytest.raises(SeedSourceError, match="never overwritten"):
        extract_seed(h2.client, image_id=IMAGE, run_id="seedrun2", directory=tmp_path / "seeds")
    assert h2.calls == []
    assert (tmp_path / "seeds" / "seed.tar").read_bytes() == before


def test_a_lone_existing_manifest_also_blocks(host, tmp_path):
    (tmp_path / "seeds").mkdir()
    (tmp_path / "seeds" / "seed_manifest.json").write_text("{}")
    with pytest.raises(SeedSourceError, match="never overwritten"):
        run(host, tmp_path)
    assert host.calls == []


# ------------------------------------------------------------------ preflight: no Docker calls
@pytest.mark.parametrize("image", ["swebench/sweb.eval.x86_64.pytest-dev_1776_pytest-5227:latest", "sha256:abc",
                                   "c" * 64, "sha256:" + "C" * 64, "sha256:" + "c" * 63, "", None, "sha256:" + "c" * 64 + "x"])
def test_mutable_or_malformed_image_reference_is_refused_before_any_docker_call(host, tmp_path, image):
    with pytest.raises(PolicyViolation, match="immutable image ID"):
        extract_seed(host.client, image_id=image, run_id=RUN, directory=tmp_path / "s")
    assert host.calls == []


@pytest.mark.parametrize("rid", ["", "a b", "../x", "a/b", "-x", "x" * 65, None, "a;b"])
def test_bad_run_id_is_refused_before_any_docker_call(host, tmp_path, rid):
    with pytest.raises(PolicyViolation, match="run_id"):
        extract_seed(host.client, image_id=IMAGE, run_id=rid, directory=tmp_path / "s")
    assert host.calls == []


@pytest.mark.parametrize("stem", ["", "../x", "a/b", ".", "a b"])
def test_bad_stem_and_limit_are_refused_before_any_docker_call(host, tmp_path, stem):
    with pytest.raises(SeedSourceError):
        extract_seed(host.client, image_id=IMAGE, run_id=RUN, directory=tmp_path / "s", stem=stem)
    with pytest.raises(SeedSourceError):
        extract_seed(host.client, image_id=IMAGE, run_id=RUN, directory=tmp_path / "s", max_bytes=0)
    assert host.calls == []


def test_missing_local_image_never_creates_a_container(host, tmp_path):
    host.images.clear()
    with pytest.raises(SeedSourceError, match="not available locally"):
        run(host, tmp_path)
    assert host.created == [] and host.removed == []


def test_image_with_a_different_id_is_refused(host, tmp_path):
    host.images[IMAGE] = "sha256:" + "d" * 64
    with pytest.raises(SeedSourceError, match="requested ID"):
        run(host, tmp_path)
    assert host.created == []


# ------------------------------------------------------------------ policy
def test_policy_rejects_any_extra_or_changed_argument():
    base = seed_source_kwargs(IMAGE, RUN)
    for key, value in [("volumes", {"/a": {}}), ("privileged", True), ("environment", {"K": "v"}), ("user", "0"),
                       ("ports", {"80/tcp": 80}), ("devices", ["/dev/x"]), ("network", "bridge"),
                       ("cap_add", ["SYS_ADMIN"]), ("tmpfs", {"/x": ""}), ("network_mode", "host"),
                       ("read_only", False), ("cap_drop", []), ("image", "sha256:" + "e" * 64),
                       ("command", ["/bin/sh"]), ("mem_limit", "64g")]:
        with pytest.raises(PolicyViolation):
            validate_seed_source_kwargs({**base, key: value}, IMAGE, RUN)
    missing = dict(base)
    del missing["network_mode"]
    with pytest.raises(PolicyViolation):
        validate_seed_source_kwargs(missing, IMAGE, RUN)


def test_created_container_that_violates_policy_is_removed_and_nothing_is_read(host, tmp_path):
    orig = host.make_inspect
    host.make_inspect = lambda image, kw: {**orig(image, kw), "Mounts": [{"Type": "volume"}]}
    with pytest.raises(PolicyViolation, match="mounts"):
        run(host, tmp_path)
    assert not any(isinstance(c, tuple) and c[0] == "get_archive" for c in host.calls)
    assert len(host.removed) == 1


@pytest.mark.parametrize("patch", [
    {"Image": "sha256:" + "d" * 64},
    {"State": {"Status": "running", "Running": True}},
    {"State": {"Status": "created", "Running": False, "StartedAt": "2026-01-01T00:00:00Z"}},
    {"HostConfig": {"NetworkMode": "bridge", "CapDrop": ["ALL"]}},
    {"HostConfig": {"NetworkMode": "none", "CapDrop": ["ALL"], "Privileged": True}},
    {"HostConfig": {"NetworkMode": "none", "CapDrop": ["ALL"], "Binds": ["/:/host"]}},
    {"HostConfig": {"NetworkMode": "none", "CapDrop": []}},
    {"Config": {"Labels": {}}},
])
def test_inspect_verification_catches_each_deviation(patch):
    ok = FakeHost().make_inspect(IMAGE, seed_source_kwargs(IMAGE, RUN))
    verify_seed_source_inspect(ok, IMAGE, RUN)
    with pytest.raises(PolicyViolation):
        verify_seed_source_inspect({**ok, **patch}, IMAGE, RUN)


def test_container_start_is_never_called_even_on_failure(host, tmp_path):
    host.archive_error = RuntimeError("x")
    with pytest.raises(SeedSourceError):
        run(host, tmp_path)
    assert "start" not in host.calls


def test_extracted_tar_is_the_normalised_injection_tar(host, tmp_path):
    r = run(host, tmp_path)
    with tarfile.open(fileobj=io.BytesIO(r.seed.tar)) as tf:
        names = tf.getnames()
    assert ".git/HEAD" in names and not any(n.startswith("testbed") for n in names)


# ================================================================== persistence: partial saves, reload, retries
CID = "c-" + "1" * 12


def files_in(d):
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def fail_nth_write(monkeypatch, n):
    real, calls = seed_module._write_new, []

    def flaky(path, data):
        calls.append(path)
        if len(calls) == n:
            raise OSError(28, "No space left on device")
        real(path, data)

    monkeypatch.setattr(seed_module, "_write_new", flaky)


@pytest.mark.parametrize("n", [1, 2])  # 1: writing the tar; 2: writing the manifest
def test_failure_while_writing_leaves_nothing_and_a_retry_with_the_same_stem_succeeds(tmp_path, monkeypatch, n):
    h = FakeHost()
    fail_nth_write(monkeypatch, n)
    with pytest.raises(OSError, match="No space"):
        extract_seed(h.client, image_id=IMAGE, run_id=RUN, directory=tmp_path / "s")
    assert files_in(tmp_path / "s") == []  # no tar, no manifest, no temp files
    assert [c for c, _ in h.removed] == [CID]
    monkeypatch.undo()
    h2 = FakeHost()
    r = extract_seed(h2.client, image_id=IMAGE, run_id="seedrun2", directory=tmp_path / "s")
    assert files_in(tmp_path / "s") == ["seed.tar", "seed_manifest.json"] and r.seed.head == "a" * 40


def test_failure_publishing_the_manifest_removes_the_tar_it_published(tmp_path, monkeypatch):
    real, calls = os.link, []

    def link(src, dst, **kw):
        calls.append(dst)
        if len(calls) == 2:
            raise OSError(5, "I/O error")
        real(src, dst, **kw)

    monkeypatch.setattr(seed_module.os, "link", link)
    h = FakeHost()
    with pytest.raises(OSError, match="I/O"):
        extract_seed(h.client, image_id=IMAGE, run_id=RUN, directory=tmp_path / "s")
    assert files_in(tmp_path / "s") == [] and len(h.removed) == 1


def test_a_file_appearing_during_the_save_is_never_replaced_or_deleted(tmp_path):
    h = FakeHost()
    d = tmp_path / "s"
    orig = h.client.containers.create

    def create_then_squat(*a, **kw):  # someone else creates the manifest between preflight and publish
        c = orig(*a, **kw)
        d.mkdir(parents=True, exist_ok=True)
        (d / "seed_manifest.json").write_text("someone else's")
        return c

    h.client.containers.create = create_then_squat
    with pytest.raises(FileExistsError):
        extract_seed(h.client, image_id=IMAGE, run_id=RUN, directory=d)
    assert (d / "seed_manifest.json").read_text() == "someone else's"
    assert files_in(d) == ["seed_manifest.json"]  # our tar was removed; theirs untouched
    assert len(h.removed) == 1


@pytest.mark.parametrize("existing", ["seed.tar", "seed_manifest.json"])
def test_preexisting_files_are_never_deleted_by_failures(tmp_path, existing):
    d = tmp_path / "s"
    d.mkdir()
    (d / existing).write_bytes(b"precious")
    h = FakeHost()
    with pytest.raises(SeedSourceError, match="never overwritten"):
        extract_seed(h.client, image_id=IMAGE, run_id=RUN, directory=d)
    assert (d / existing).read_bytes() == b"precious" and files_in(d) == [existing] and h.calls == []


def test_reload_failure_removes_the_files_this_attempt_saved_and_retry_succeeds(tmp_path, monkeypatch):
    def bad_load(*a, **kw):
        raise SeedError("tampered while stored")

    monkeypatch.setattr(ss, "load_seed", bad_load)
    h = FakeHost()
    with pytest.raises(SeedError, match="tampered"):
        extract_seed(h.client, image_id=IMAGE, run_id=RUN, directory=tmp_path / "s")
    assert files_in(tmp_path / "s") == [] and len(h.removed) == 1
    monkeypatch.undo()
    assert extract_seed(FakeHost().client, image_id=IMAGE, run_id="seedrun2", directory=tmp_path / "s").seed.head


def test_reload_that_returns_a_different_seed_is_refused_and_removed(tmp_path, monkeypatch):
    real = ss.load_seed

    def other(*a, **kw):
        s = real(*a, **kw)
        s.manifest.head = "b" * 40
        return s

    monkeypatch.setattr(ss, "load_seed", other)
    with pytest.raises(SeedSourceError, match="does not match"):
        extract_seed(FakeHost().client, image_id=IMAGE, run_id=RUN, directory=tmp_path / "s")
    assert files_in(tmp_path / "s") == []


def test_failed_cleanup_of_saved_files_is_reported_and_the_original_error_survives(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ss, "load_seed", lambda *a, **kw: (_ for _ in ()).throw(SeedError("reload failed")))
    real_unlink = type(tmp_path).unlink

    def stubborn(self, *a, **kw):
        if self.name == "seed.tar":
            raise PermissionError("nope")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(type(tmp_path), "unlink", stubborn)
    with pytest.raises(SeedError, match="reload failed"):
        extract_seed(FakeHost().client, image_id=IMAGE, run_id=RUN, directory=tmp_path / "s")
    assert "could not remove" in capsys.readouterr().err


def test_save_seed_is_atomic_and_never_overwrites_directly(tmp_path):
    seed = parse_seed_archive(make_tar(good_entries()))
    seed_module.save_seed(tmp_path, seed)
    assert files_in(tmp_path) == ["seed.tar", "seed_manifest.json"]  # no temp files remain
    with pytest.raises(FileExistsError):
        seed_module.save_seed(tmp_path, seed)
    assert files_in(tmp_path) == ["seed.tar", "seed_manifest.json"]


# ================================================================== create failure, interrupts, stat, copies
def test_create_raising_is_reported_and_nothing_is_removed(tmp_path):
    h = FakeHost()
    h.create_error = RuntimeError("409 name already in use")
    with pytest.raises(SeedSourceError, match="creating the seed-source container failed"):
        extract_seed(h.client, image_id=IMAGE, run_id=RUN, directory=tmp_path / "s")
    assert h.removed == [] and files_in(tmp_path / "s") == []
    assert not any(isinstance(c, tuple) and c[0] == "remove" for c in h.calls)


def test_keyboard_interrupt_during_create_propagates_with_nothing_to_remove(tmp_path):
    h = FakeHost()
    h.create_error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        extract_seed(h.client, image_id=IMAGE, run_id=RUN, directory=tmp_path / "s")
    assert h.removed == []


def _interrupt(monkeypatch, h, where):
    def boom(*a, **kw):
        raise KeyboardInterrupt()

    if where == "inspect":
        h.client.api.inspect_container = boom
    elif where == "get_archive":
        h.archive_error = KeyboardInterrupt()
    elif where == "stream":
        h.stream_error = KeyboardInterrupt()
    elif where == "parse":
        monkeypatch.setattr(ss, "parse_seed_archive", boom)
    elif where == "save":
        monkeypatch.setattr(ss, "save_seed", boom)
    elif where == "reload":
        monkeypatch.setattr(ss, "load_seed", boom)


@pytest.mark.parametrize("where", ["inspect", "get_archive", "stream", "parse", "save", "reload"])
def test_keyboard_interrupt_at_every_boundary_removes_the_exact_container_and_leaves_no_seed(tmp_path, monkeypatch, where):
    h = FakeHost()
    _interrupt(monkeypatch, h, where)
    with pytest.raises(KeyboardInterrupt):
        extract_seed(h.client, image_id=IMAGE, run_id=RUN, directory=tmp_path / "s")
    assert [c for c, _ in h.removed] == [CID]
    assert "start" not in h.calls
    assert files_in(tmp_path / "s") == []


@pytest.mark.parametrize("ret", [None, "ab", (b"x",), (Stream([b"x"]), {}, 3), 5])
def test_malformed_get_archive_result_is_refused_and_cleaned_up(tmp_path, ret):
    h = FakeHost()
    h.archive_return = ret if ret is not None else ()
    with pytest.raises(SeedSourceError, match="get_archive"):
        extract_seed(h.client, image_id=IMAGE, run_id=RUN, directory=tmp_path / "s")
    assert len(h.removed) == 1


@pytest.mark.parametrize("stat", [None, {}, {"name": "unrelated", "size": 1}])
def test_stat_is_never_used_so_none_and_any_dict_are_accepted(tmp_path, stat):
    h = FakeHost()
    h.stat = stat
    assert extract_seed(h.client, image_id=IMAGE, run_id=RUN, directory=tmp_path / "s").seed.head == "a" * 40


def test_read_bounded_returns_the_exact_bytes_unchanged():
    data = make_tar(good_entries())
    out = read_bounded(Stream(chunked(data, 97)), len(data))
    assert type(out) is bytes and out == data
    assert parse_seed_archive(out).tar == parse_seed_archive(bytearray(data)).tar  # the parser accepts both, identically

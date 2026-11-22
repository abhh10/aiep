"""The pytest-5227 seed extraction driver, with a fake Docker client: no daemon, no container, no network."""
import importlib.util
import shutil
from pathlib import Path

import pytest

from test_seed_source import FakeHost

REAL = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("extract_pytest5227_seed", REAL / "scripts" / "extract_pytest5227_seed.py")
drv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drv)

GIB = 1024**3


class Tracked:
    """A FakeHost client that records when the driver closes it, and builds it only when the factory is called."""

    def __init__(self, close_error=None):
        self.host = FakeHost()
        self.host.images = {drv.IMAGE_ID: drv.IMAGE_ID}
        self.closed = 0
        self.close_error = close_error
        self.built = 0

    def factory(self):
        self.built += 1
        client = self.host.client

        def close():
            self.closed += 1
            if self.close_error:
                raise self.close_error

        client.close = close
        return client


def go(t, tmp_path, free=16 * GIB, **kw):
    return drv.main(client_factory=t.factory, free_bytes=lambda p: free, root=tmp_path, run_id="seedsrc-test", **kw)


def test_constants_are_the_approved_values():
    assert drv.IMAGE_ID == "sha256:c91459f86ed261ab81871b086d7ca2d9fe16abd2ddf374bf172edd5d991fd28b"
    assert drv.MAX_ARCHIVE_BYTES == 64 * 1024**2 and drv.MIN_FREE_BYTES == 16 * GIB
    assert drv.SEED_DIR == Path(".cache/seeds/pytest-dev__pytest-5227")


@pytest.mark.parametrize("free", [0, 15 * GIB, int(15.19 * GIB), 16 * GIB - 1])
def test_disk_below_16_gib_makes_zero_docker_api_calls(tmp_path, capsys, free):
    t = Tracked()
    assert go(t, tmp_path, free=free) == 2
    assert t.built == 0 and t.closed == 0 and t.host.calls == []  # not even the client (its construction is an API call)
    assert "REFUSED" in capsys.readouterr().err
    assert not (tmp_path / ".cache").exists()


def test_disk_exactly_16_gib_is_accepted_and_succeeds(tmp_path, capsys):
    t = Tracked()
    assert go(t, tmp_path, free=16 * GIB) == 0
    out = capsys.readouterr().out
    seed = tmp_path / ".cache/seeds/pytest-dev__pytest-5227"
    assert (seed / "seed.tar").is_file() and (seed / "seed_manifest.json").is_file()
    assert "seed sha256:" in out and "repository HEAD: " + "a" * 40 in out and "manifest entries: " in out
    assert t.built == 1 and t.closed == 1 and len(t.host.removed) == 1


def test_default_probe_reads_the_real_disk_for_the_checkout(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(shutil, "disk_usage", lambda p: seen.append(p) or type("U", (), {"free": 1})())
    t = Tracked()
    assert drv.main(client_factory=t.factory, root=tmp_path) == 2
    assert seen == [tmp_path] and t.built == 0


def test_driver_passes_the_pinned_image_the_64_mib_limit_and_the_fixed_directory(tmp_path, monkeypatch):
    got = {}

    def fake_extract(client, **kw):
        got.update(kw)
        raise drv.SeedSourceError("stop here")

    monkeypatch.setattr(drv, "extract_seed", fake_extract)
    assert go(Tracked(), tmp_path) == 1
    assert got["image_id"] == drv.IMAGE_ID and got["max_bytes"] == 64 * 1024**2
    assert got["directory"] == tmp_path / ".cache/seeds/pytest-dev__pytest-5227"


def test_client_is_closed_after_an_extraction_failure_and_the_error_is_reported(tmp_path, capsys):
    t = Tracked()
    t.host.archive_error = RuntimeError("daemon says no")
    assert go(t, tmp_path) == 1
    assert t.closed == 1 and len(t.host.removed) == 1  # container removed, client closed
    assert "ERROR: SeedSourceError" in capsys.readouterr().err


def test_client_is_closed_on_unexpected_errors_and_interrupts(tmp_path, monkeypatch):
    for exc in (RuntimeError("boom"), KeyboardInterrupt()):
        t = Tracked()

        def explode(client, **kw):
            raise exc

        monkeypatch.setattr(drv, "extract_seed", explode)
        with pytest.raises(type(exc)):
            go(t, tmp_path)
        assert t.closed == 1


def test_close_failure_is_reported_and_fails_the_run(tmp_path, capsys):
    t = Tracked(close_error=OSError("socket"))
    assert go(t, tmp_path) == 1
    assert "closing the Docker client failed" in capsys.readouterr().err
    assert (tmp_path / ".cache/seeds/pytest-dev__pytest-5227/seed.tar").is_file()  # the seed itself was stored


def test_container_removal_failure_is_reported_clearly(tmp_path, capsys):
    t = Tracked()
    t.host.remove_error = RuntimeError("busy")
    assert go(t, tmp_path) == 1
    assert "could not be removed" in capsys.readouterr().err and t.closed == 1


def test_existing_seed_is_never_deleted_and_no_container_is_made(tmp_path, capsys):
    d = tmp_path / ".cache/seeds/pytest-dev__pytest-5227"
    d.mkdir(parents=True)
    (d / "seed.tar").write_bytes(b"keep me")
    t = Tracked()
    assert go(t, tmp_path) == 1
    assert (d / "seed.tar").read_bytes() == b"keep me" and t.host.created == [] and t.closed == 1
    assert "never overwritten" in capsys.readouterr().err


def test_default_client_factory_is_not_touched_by_the_tests():
    assert callable(drv._default_client)  # constructing it would call the daemon; no test does

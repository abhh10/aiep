"""The approved limits: seed archive 160 MiB (was 128) and agent /testbed tmpfs 256 MiB (was 192), with everything else unchanged.

Measured (read-only probes of each pulled image, never prepared): sympy-18621 99.45 MiB, sympy-22714 112.77 MiB, django-13933 133.78 MiB (140,280,320 B) and django-15851 142.56 MiB (149,485,568 B) archives;
the largest page-rounded /testbed footprint is 160.39 MiB (django-15851). Everything here is in-memory or fake Docker: no image, container, seed or task is touched.
"""
import pytest

from aiep import agent_runner as AR
from aiep import seed as seed_module
from aiep import task_setup as ts
from aiep.container_policy import AGENT_MEM_BYTES, TMPFS_MAX_EACH_BYTES, TMPFS_MAX_TOTAL_BYTES, PolicyViolation, _parse_size
from aiep.seed_source import SeedArchiveTooLarge, SeedSourceError, read_bounded
from test_seed import good_entries, make_tar
from test_seed_source import FakeHost
from test_task_setup import INSTANCE, big_tar, go, pins, root  # noqa: F401 (root is a fixture)

MIB = 1024**2
CAP = ts.MAX_ARCHIVE_BYTES
DJANGO_13933_TAR, DJANGO_15851_TAR = 140_280_320, 149_485_568  # measured raw archive sizes
LARGEST_FOOTPRINT = 168_177_664  # django-15851, page-rounded /testbed footprint (160.387 MiB)


def chunks(total, block=MIB):
    """A stream of ``total`` bytes that references ONE block repeatedly (no allocation beyond the reader's own buffer). The filler is not zero bytes: a tar reader treats zeros as a (valid, empty)
    archive and probes every compression format over the whole stream, which takes tens of seconds and says nothing about the size gate; this filler is rejected immediately."""
    full, rest = divmod(total, block)
    return [b"x" * block] * full + ([b"x" * rest] if rest else [])


# ---------------------------------------------------------------- the archive cap
def test_the_cap_is_exactly_160_mib_and_covers_every_measured_stage_1_archive_with_margin():
    assert CAP == 160 * MIB
    measured = {"sympy-18621": 104_284_160, "sympy-22714": 118_251_520, "django-13933": DJANGO_13933_TAR, "django-15851": DJANGO_15851_TAR}
    assert all(size <= CAP for size in measured.values()) and max(measured.values()) == DJANGO_15851_TAR
    assert CAP - DJANGO_15851_TAR == 18_286_592  # 17.44 MiB (12%) of margin over the largest measured archive
    assert [k for k, v in measured.items() if v > 128 * MIB] == ["django-13933", "django-15851"]  # exactly the two that the old 128 MiB cap refused


@pytest.mark.parametrize("size,ok", [(CAP - 1, True), (CAP, True), (CAP + 1, False)])
def test_the_cap_is_inclusive_just_below_at_and_just_over(size, ok):
    if ok:
        assert len(read_bounded(chunks(size), CAP)) == size
    else:
        with pytest.raises(SeedArchiveTooLarge, match="exceeds"):
            read_bounded(chunks(size), CAP)


def test_an_unbounded_stream_is_cut_off_at_the_cap_never_buffered_whole():
    consumed = []

    def endless():
        block = b"x" * MIB
        while True:
            consumed.append(1)
            yield block
    with pytest.raises(SeedArchiveTooLarge):
        read_bounded(endless(), CAP)
    assert len(consumed) == 161  # 160 MiB accepted, the 161st block refused: memory is bounded by the cap however long the stream is


def test_through_the_pipeline_an_archive_at_the_cap_passes_the_size_gate_and_one_byte_more_does_not(root):
    at = FakeHost()
    at.chunks = chunks(CAP)  # exactly at the cap: the size gate passes; it is not a tar, so the PARSER (not the cap) refuses it
    with pytest.raises(seed_module.SeedError) as e:
        go(root, at)
    assert not isinstance(e.value, SeedSourceError) and INSTANCE not in pins(root)
    over = FakeHost()
    over.chunks = chunks(CAP + 1)
    with pytest.raises(SeedArchiveTooLarge):
        go(root, over)
    assert INSTANCE not in pins(root) and [c for c, _ in over.removed]  # nothing pinned, and the seed-source container was removed


@pytest.mark.parametrize("name,size", [("django-13933", DJANGO_13933_TAR), ("django-15851", DJANGO_15851_TAR)])
def test_the_measured_django_archives_are_accepted_and_pinned_and_would_have_been_refused_before(root, name, size):
    raw = big_tar(size, n_files=2)  # two files of 70-75 MB each: under the 96 MiB per-file limit, like the real .git pack (75-80 MiB)
    assert abs(len(raw) - size) < 200_000 and 128 * MIB < len(raw) <= CAP
    with pytest.raises(SeedArchiveTooLarge):  # the previous cap
        read_bounded([raw], 128 * MIB)
    r = go(root, FakeHost(tar=raw, chunk=4 * MIB))
    assert pins(root)[INSTANCE]["sha256"] == r["seed_sha256"] and r["entries"] > 5


def test_a_django_sized_archive_still_cannot_smuggle_an_oversized_file(root):
    raw = big_tar(150 * MIB, n_files=1)  # under the 160 MiB archive cap, but one 150 MiB file: over the (unchanged) 96 MiB per-file limit
    assert len(raw) <= CAP
    with pytest.raises(seed_module.SeedError, match="per-file"):
        go(root, FakeHost(tar=raw, chunk=4 * MIB))
    assert INSTANCE not in pins(root)


# ---------------------------------------------------------------- the 256 MiB /testbed tmpfs and the aggregate ceiling
def spec(**over):
    import test_agent_runner as t
    return t.spec_for(t.parse_seed_archive(make_tar(good_entries())), **over)


def test_the_default_testbed_tmpfs_is_256_mib_and_is_what_the_container_gets():
    assert AR.AgentRunSpec.__dataclass_fields__["testbed_size"].default == "256m"
    assert "size=256m," in AR.tmpfs_for(spec())["/testbed"] and "rw,exec,nosuid,nodev" in AR.tmpfs_for(spec())["/testbed"]
    kwargs = AR.build_create_kwargs(spec())
    assert _parse_size(kwargs["tmpfs"]["/testbed"].split("size=")[1].split(",")[0]) == 256 * MIB


def test_the_largest_measured_footprint_fits_with_working_space():
    assert abs(LARGEST_FOOTPRINT - 160.387 * MIB) < 4096  # 160.387 MiB, page-rounded
    assert 256 * MIB - LARGEST_FOOTPRINT == 100_257_792  # 95.6 MiB left for the agent's own writes (the calibrated sympy-18621 task had 89.6 MiB)
    assert CAP * 1.13 + 64 * MIB <= 256 * MIB  # the coupling rule: archive cap x the Django page-rounding ratio plus working space fits the tmpfs


def test_the_aggregate_tmpfs_is_992_mib_32_mib_below_the_1_gib_ceiling():
    mounts = AR.build_create_kwargs(spec())["tmpfs"]
    sizes = {p: _parse_size(v.split("size=")[1].split(",")[0]) for p, v in mounts.items()}
    shm = _parse_size(spec().shm_size)
    assert sorted(sizes.values()) == sorted([256 * MIB, 416 * MIB, 128 * MIB, 128 * MIB]) and shm == 64 * MIB
    total = sum(sizes.values()) + shm
    assert total == 992 * MIB and TMPFS_MAX_TOTAL_BYTES == 1024 * MIB and TMPFS_MAX_TOTAL_BYTES - total == 32 * MIB
    assert AGENT_MEM_BYTES - total == 1056 * MIB  # a completely full tmpfs still leaves 1056 MiB of the 2 GiB container memory
    assert TMPFS_MAX_EACH_BYTES == 512 * MIB


def test_the_1_gib_aggregate_ceiling_is_still_enforced_exactly():
    AR.build_create_kwargs(spec(testbed_size="288m"))  # 288 + 416 + 128 + 128 + 64 = 1024 MiB: exactly the ceiling is allowed
    with pytest.raises(PolicyViolation):
        AR.build_create_kwargs(spec(testbed_size="289m"))  # one MiB more is refused
    with pytest.raises(PolicyViolation):
        AR.build_create_kwargs(spec(testbed_size="513m"))  # and a single mount above the 512 MiB per-mount cap is refused regardless


# ---------------------------------------------------------------- everything else is unchanged
def test_every_other_limit_is_exactly_as_before():
    assert seed_module.MAX_SEED_FILE_BYTES == 96 * MIB and seed_module.MAX_SEED_BYTES == 256 * MIB and seed_module.MAX_ENTRIES == 200_000
    assert seed_module.DEFAULT_COMPONENT_FILE_BYTES == 256 * MIB and AR.MAX_COMPONENT_FILE_BYTES == 400 * MIB and AR.COMPONENTS_TMPFS_SIZE == "416m"
    f = AR.AgentRunSpec.__dataclass_fields__
    assert (f["tmp_size"].default, f["home_size"].default, f["shm_size"].default) == ("128m", "128m", "64m")
    assert ts.MIN_FREE_BYTES == 16 * 1024**3  # the 16 GiB start gate
    assert not hasattr(seed_module, "MAX_FILE_BYTES")
    kwargs = AR.build_create_kwargs(spec())
    assert kwargs["mem_limit"] == "2g" and kwargs["memswap_limit"] == "2g" and kwargs["pids_limit"] == 512 and kwargs["read_only"] is True

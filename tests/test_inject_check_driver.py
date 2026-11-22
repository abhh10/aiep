"""The seed-injection validation driver, with fake Docker and a real temporary 'container' filesystem: no daemon, no network."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from aiep.container_exec import run_exec
from aiep.container_policy import AGENT_MEM_BYTES, AGENT_NANO_CPUS, AGENT_PIDS, PolicyViolation
from aiep.seed import save_seed
from fake_docker import ExecResult, FakeContainerFS, FakeExecApi
from test_agent_runner import FakeContainer, repo, seed, shm_bytes  # noqa: F401 (fixtures)

REAL = Path(__file__).resolve().parents[1]
spec_ = importlib.util.spec_from_file_location("inject_pytest5227_seed_check", REAL / "scripts" / "inject_pytest5227_seed_check.py")
drv = importlib.util.module_from_spec(spec_)
spec_.loader.exec_module(drv)

GIB = 1024**3
ALLOWED_BINARIES = {"/bin/sh", "/bin/tar", "/opt/miniconda3/bin/python", "/usr/bin/git"}


def inspect_from(kw):
    return {"Image": drv.IMAGE_ID,
            "HostConfig": {"Privileged": False, "CapAdd": None, "CapDrop": kw["cap_drop"], "SecurityOpt": kw["security_opt"],
                           "NetworkMode": kw["network"], "Memory": AGENT_MEM_BYTES, "PidsLimit": AGENT_PIDS,
                           "NanoCpus": AGENT_NANO_CPUS, "ReadonlyRootfs": True, "Binds": None, "Tmpfs": kw["tmpfs"],
                           "IpcMode": kw["ipc_mode"], "ShmSize": shm_bytes(kw["shm_size"])},
            "Mounts": [], "Config": {"User": kw["user"], "Env": [f"{k}={v}" for k, v in kw["environment"].items()]},
            "NetworkSettings": {"Networks": {kw["network"]: {}}}}


class Api(FakeExecApi):
    def inspect_container(self, cid):
        self.client.calls.append("inspect")
        out = inspect_from(self.client.created[-1])
        out.update(self.client.inspect_patch)
        return out


class Client:
    """Tracks every Docker-facing call; anything the driver must never use raises."""

    def __init__(self, api, container, *, images=(drv.IMAGE_ID,), create_error=None, close_error=None):
        self.api, self.container, self.created, self.calls = api, container, [], []
        self.inspect_patch, self.closed, self.built = {}, 0, 0
        self.close_error, self.create_error, self.known = close_error, create_error, set(images)
        api.client = self
        outer = self

        class Containers:
            def create(self, image, command=None, **kw):
                outer.calls.append("create")
                if outer.create_error:
                    raise outer.create_error
                outer.created.append({"image": image, "command": command, **kw})
                return outer.container

            def run(self, *a, **k):
                raise AssertionError("containers.run must never be used")

        class Images:
            def get(self, ref):
                outer.calls.append(("images.get", ref))
                if ref not in outer.known:
                    raise LookupError("no such image")
                return type("I", (), {"id": ref})()

            def pull(self, *a, **k):
                raise AssertionError("images.pull must never be used")

        class Forbidden:
            def __getattr__(self, name):
                raise AssertionError(f"networks/volumes ({name}) must never be used")

        self.containers, self.images, self.networks, self.volumes = Containers(), Images(), Forbidden(), Forbidden()

    def factory(self):
        self.built += 1
        return self

    def close(self):
        self.closed += 1
        if self.close_error:
            raise self.close_error


def real_probe(fs):
    f = fs.dirs["/testbed"] / ".aiep_write_probe"
    f.write_text("aiep-probe\n")
    assert f.read_text() == "aiep-probe\n"
    f.unlink()
    return ExecResult(stdout=b"probe-ok\n")


def make_handler(fs, *, probe=None, rev=None, cat_exit=0, tamper=None):
    base = fs.handler

    def h(argv, rec, stdin):
        if argv[:2] == ["/bin/sh", "-c"] and argv[2] == drv.PROBE_SCRIPT:
            return (probe or real_probe)(fs)
        if argv[0] == "/usr/bin/git" and "rev-parse" in argv and rev:
            return ExecResult(stdout=rev.encode())
        if argv[0] == "/usr/bin/git" and "cat-file" in argv and cat_exit:
            return ExecResult(exit_code=cat_exit)
        res = base(argv, rec, stdin)
        if tamper and argv[0] == "/bin/tar":
            tamper(fs)
        return res

    return h


def rig(tmp_path, seed, *, owner=(1000, 1000), container=None, handler=None, **client_kw):
    seed_dir = tmp_path / "seeds"
    save_seed(seed_dir, seed)
    fs = FakeContainerFS(tmp_path / "fs", owner=owner)
    api = Api(make_handler(fs, **(handler or {})))
    client = Client(api, container or FakeContainer(), **client_kw)
    return fs, api, client, seed_dir


def go(client, seed_dir, seed, free=16 * GIB, root=REAL, sha=None):
    return drv.main(client_factory=client.factory, free_bytes=lambda p: free, root=root, seed_dir=seed_dir,
                    expected_sha256=sha or seed.manifest.archive_sha256)


# ------------------------------------------------------------------ constants and the disk gate
def test_constants_are_the_approved_values():
    assert drv.IMAGE_ID == "sha256:c91459f86ed261ab81871b086d7ca2d9fe16abd2ddf374bf172edd5d991fd28b"
    assert drv.SEED_DIR == Path(".cache/seeds/pytest-dev__pytest-5227") and drv.AGENT_NETWORK == "none"
    assert drv.MIN_FREE_BYTES == 16 * GIB
    assert drv.EXPECTED_SEED_SHA256 == "8727a0bccdb2b21b7faaf33f66d3086738105b64744db051f1b944e8abc6e3b6"
    assert drv.EXPECTED_SEED_HEAD == "aa6a504d7ea32c59ae245f90682906db5ac5213c"


@pytest.mark.parametrize("free", [0, int(15.19 * GIB), 16 * GIB - 1])
def test_disk_below_16_gib_makes_zero_docker_calls(tmp_path, seed, capsys, free):
    fs, api, client, sd = rig(tmp_path, seed)
    assert go(client, sd, seed, free=free) == 2
    assert client.built == 0 and client.calls == [] and api.calls == [] and client.closed == 0
    assert "REFUSED" in capsys.readouterr().err


def test_disk_exactly_16_gib_is_accepted(tmp_path, seed):
    fs, api, client, sd = rig(tmp_path, seed)
    assert go(client, sd, seed, free=16 * GIB) == 0 and client.built == 1


# ------------------------------------------------------------------ success path
def test_success_runs_every_check_in_order_and_cleans_up(tmp_path, seed, capsys):
    c = FakeContainer()
    fs, api, client, sd = rig(tmp_path, seed, container=c)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sd.iterdir()}
    assert go(client, sd, seed) == 0
    out = capsys.readouterr().out
    for needle in ("PASS verify_inspect", "PASS container running", "PASS identity nonroot:1000:1000",
                   f"PASS injected and verified {len(seed.manifest.entries)} entries", "owner 1000:1000",
                   f"PASS git HEAD == manifest HEAD {seed.head}", "PASS HEAD commit object exists",
                   "PASS scratch-file write/read/delete probe", "removed: True", "ALL CHECKS PASSED"):
        assert needle in out, needle
    assert c.calls == ["start", "kill", "remove(force=True,v=True)"]  # started once, removed exactly once
    assert client.closed == 1 and client.built == 1
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sd.iterdir()} == before  # seed only read


def test_container_is_created_from_the_pinned_image_with_no_network_mounts_or_real_credentials(tmp_path, seed):
    fs, api, client, sd = rig(tmp_path, seed)
    go(client, sd, seed)
    (kw,) = client.created
    assert kw["image"] == drv.IMAGE_ID and kw["network"] == "none" and kw["user"] == "1000:1000"
    assert kw["read_only"] is True and kw["cap_drop"] == ["ALL"] and kw["security_opt"] == ["no-new-privileges"]
    assert kw["ipc_mode"] == "private" and kw["shm_size"] == "64m" and kw["command"] == ["/bin/sleep", "2147483647"]
    assert set(kw["tmpfs"]) == {"/testbed", "/opt/aiep", "/tmp", "/home/nonroot"}
    for forbidden in ("volumes", "mounts", "ports", "devices", "privileged", "cap_add", "network_mode", "dns"):
        assert forbidden not in kw
    env = kw["environment"]
    assert env["ANTHROPIC_BASE_URL"] == drv.DUMMY_GATEWAY_URL and env["ANTHROPIC_AUTH_TOKEN"] == drv.DUMMY_TOKEN
    assert "ANTHROPIC_API_KEY" not in env
    assert ("images.get", drv.IMAGE_ID) in client.calls


def test_no_claude_gateway_or_foreign_command_is_ever_executed(tmp_path, seed):
    fs, api, client, sd = rig(tmp_path, seed)
    assert go(client, sd, seed) == 0
    cmds = [r["cmd"] for r in api.calls]
    assert cmds and {c[0] for c in cmds} <= ALLOWED_BINARIES
    blob = json.dumps(cmds) + json.dumps([r["environment"] for r in api.calls])
    assert "/opt/aiep/claude" not in blob and "aiep.gateway" not in blob and "gateway.invalid" not in blob
    assert all(r["privileged"] is False and r["user"] == "1000:1000" and not r["tty"] for r in api.calls)


@pytest.mark.parametrize("argv", [["/opt/aiep/claude", "-p"], ["/opt/miniconda3/bin/python", "-m", "aiep.gateway"],
                                  ["/bin/sh", "-c", "id"], ["/usr/bin/git", "status"], ["/bin/cat", "/etc/passwd"],
                                  ["/usr/bin/git", *drv.HARDENED_GIT, "diff"]])
def test_exec_helper_refuses_anything_off_the_approved_list_before_docker(seed, argv):
    api = FakeExecApi(lambda *a: ExecResult())
    with pytest.raises(PolicyViolation, match="not on the approved list"):
        drv._exec(api, "cid", None, argv, seed.head)
    assert api.calls == []


def test_the_policy_layer_also_refuses_claude_and_the_gateway_in_this_drivers_context(tmp_path, seed):
    from aiep.agent_runner import exec_context
    spec, _ = drv.build_spec(REAL, _saved(tmp_path, seed), seed.manifest.archive_sha256)
    ectx = exec_context(spec)
    api = FakeExecApi(lambda *a: ExecResult())
    assert ectx.may_start_gateway is False
    with pytest.raises(PolicyViolation):
        run_exec(api, "cid", ["/opt/miniconda3/bin/python", "-m", "aiep.gateway"], ctx=ectx)
    assert api.calls == []


def _saved(tmp_path, seed):
    d = tmp_path / "local_seed"
    save_seed(d, seed)
    return d


# ------------------------------------------------------------------ ownership, content, HEAD, probe
@pytest.mark.parametrize("owner", [(0, 0), (1000, 1001), (1001, 1000)])
def test_wrong_ownership_fails_the_run_and_cleans_up(tmp_path, seed, capsys, owner):
    c = FakeContainer()
    fs, api, client, sd = rig(tmp_path, seed, owner=owner, container=c)
    assert go(client, sd, seed) == 1
    cap = capsys.readouterr()
    assert "owner differs" in cap.err and "RESULT: FAILED" in cap.out
    assert "remove(force=True,v=True)" in c.calls and client.closed == 1


def test_real_file_owner_other_than_1000_is_rejected_when_the_listing_reports_it(tmp_path, seed, capsys):
    fs, api, client, sd = rig(tmp_path, seed, owner=None)  # the listing then reports the test user's real uid
    assert go(client, sd, seed) == 1 and "owner differs" in capsys.readouterr().err


def test_content_change_during_injection_is_detected(tmp_path, seed, capsys):
    fs, api, client, sd = rig(tmp_path, seed, handler={"tamper": lambda fs: (fs.dirs["/testbed"] / "keep.txt").write_text("evil\n")})
    assert go(client, sd, seed) == 1 and "content differs: keep.txt" in capsys.readouterr().err


def test_head_mismatch_and_missing_commit_object_fail(tmp_path, seed, capsys):
    fs, api, client, sd = rig(tmp_path, seed, handler={"rev": "b" * 40 + "\n"})
    assert go(client, sd, seed) == 1 and "git rev-parse HEAD" in capsys.readouterr().err
    fs, api, client, sd = rig(tmp_path / "two", seed, handler={"cat_exit": 128})
    assert go(client, sd, seed) == 1 and "commit object is missing" in capsys.readouterr().err


def test_probe_runs_after_verification_and_leaves_the_listing_identical(tmp_path, seed):
    fs, api, client, sd = rig(tmp_path, seed)
    assert go(client, sd, seed) == 0
    cmds = [r["cmd"] for r in api.calls]
    probe_at = next(i for i, c in enumerate(cmds) if c[:2] == ["/bin/sh", "-c"] and c[2] == drv.PROBE_SCRIPT)
    verifies = [i for i, c in enumerate(cmds) if "-I" in c]
    assert verifies[0] < probe_at < verifies[1] and len(verifies) == 2  # verified before, and re-verified after
    assert not (fs.dirs["/testbed"] / ".aiep_write_probe").exists()


def test_a_probe_that_leaves_a_file_behind_is_caught_by_the_second_listing(tmp_path, seed, capsys):
    def leaky(fs):
        (fs.dirs["/testbed"] / ".aiep_write_probe").write_text("left\n")
        return ExecResult(stdout=b"probe-ok\n")

    fs, api, client, sd = rig(tmp_path, seed, handler={"probe": leaky})
    assert go(client, sd, seed) == 1 and "unexpected entry: .aiep_write_probe" in capsys.readouterr().err


def test_a_failing_probe_fails_the_run(tmp_path, seed, capsys):
    fs, api, client, sd = rig(tmp_path, seed, handler={"probe": lambda fs: ExecResult(exit_code=1, stderr=b"read-only")})
    assert go(client, sd, seed) == 1 and "probe failed" in capsys.readouterr().err


# ------------------------------------------------------------------ before Docker: local failures
def test_wrong_seed_hash_or_missing_seed_or_missing_config_make_zero_docker_calls(tmp_path, seed, capsys):
    fs, api, client, sd = rig(tmp_path, seed)
    assert go(client, sd, seed, sha="0" * 64) == 1
    assert drv.main(client_factory=client.factory, free_bytes=lambda p: 16 * GIB, root=REAL, seed_dir=tmp_path / "nope",
                    expected_sha256="0" * 64) == 1
    assert drv.main(client_factory=client.factory, free_bytes=lambda p: 16 * GIB, root=tmp_path, seed_dir=sd,
                    expected_sha256=seed.manifest.archive_sha256) == 1  # no config/agent_identity.json under tmp_path
    assert client.built == 0 and client.calls == [] and api.calls == []
    assert "ERROR before Docker" in capsys.readouterr().err


# ------------------------------------------------------------------ failures and cleanup
def test_missing_image_never_creates_a_container(tmp_path, seed, capsys):
    fs, api, client, sd = rig(tmp_path, seed, images=())
    assert go(client, sd, seed) == 1 and client.created == [] and client.closed == 1
    assert "FAIL" in capsys.readouterr().err


def test_create_failure_is_reported_and_the_client_is_closed(tmp_path, seed, capsys):
    c = FakeContainer()
    fs, api, client, sd = rig(tmp_path, seed, container=c, create_error=RuntimeError("409 conflict"))
    assert go(client, sd, seed) == 1 and c.calls == [] and client.closed == 1
    assert "creating the container failed" in capsys.readouterr().err


def test_inspect_violation_stops_before_the_container_is_started_and_removes_it(tmp_path, seed, capsys):
    c = FakeContainer()
    fs, api, client, sd = rig(tmp_path, seed, container=c)
    client.inspect_patch = {"Mounts": [{"Type": "bind", "Source": "/var/run/docker.sock"}]}
    assert go(client, sd, seed) == 1
    assert "start" not in c.calls and "remove(force=True,v=True)" in c.calls and api.calls == []
    assert "PolicyViolation" in capsys.readouterr().err


def test_start_failure_removes_the_container_and_closes_the_client(tmp_path, seed):
    c = FakeContainer(start_error=RuntimeError("cannot start"))
    fs, api, client, sd = rig(tmp_path, seed, container=c)
    assert go(client, sd, seed) == 1 and c.calls[-1] == "remove(force=True,v=True)" and client.closed == 1


def test_removal_failure_after_success_is_reported_and_fails_the_run(tmp_path, seed, capsys):
    c = FakeContainer(remove_error=RuntimeError("busy"))
    fs, api, client, sd = rig(tmp_path, seed, container=c)
    assert go(client, sd, seed) == 1
    cap = capsys.readouterr()
    assert "could not be removed" in cap.err and "CLEANUP FAILED" in cap.out and client.closed == 1


def test_removal_failure_does_not_hide_the_original_error(tmp_path, seed, capsys):
    c = FakeContainer(remove_error=RuntimeError("busy"))
    fs, api, client, sd = rig(tmp_path, seed, container=c, owner=(0, 0))
    assert go(client, sd, seed) == 1
    err = capsys.readouterr().err
    assert "owner differs" in err and "could not be removed" in err


def test_close_failure_is_reported_and_fails_the_run(tmp_path, seed, capsys):
    fs, api, client, sd = rig(tmp_path, seed, close_error=OSError("socket"))
    assert go(client, sd, seed) == 1 and "closing the Docker client failed" in capsys.readouterr().err


@pytest.mark.parametrize("where", ["start", "inspect", "inject", "list"])
def test_keyboard_interrupt_still_removes_the_container_and_closes_the_client(tmp_path, seed, monkeypatch, where):
    c = FakeContainer(start_error=KeyboardInterrupt() if where == "start" else None)
    fs, api, client, sd = rig(tmp_path, seed, container=c)

    def boom(*a, **k):
        raise KeyboardInterrupt()

    if where == "inspect":
        client.api.inspect_container = boom
    elif where == "inject":
        monkeypatch.setattr(drv, "inject_tar", boom)
    elif where == "list":
        monkeypatch.setattr(drv, "list_tree", boom)
    with pytest.raises(KeyboardInterrupt):
        go(client, sd, seed)
    assert "remove(force=True,v=True)" in c.calls and client.closed == 1


def test_nothing_but_the_one_container_is_created_or_removed(tmp_path, seed):
    c = FakeContainer()
    fs, api, client, sd = rig(tmp_path, seed, container=c)
    go(client, sd, seed)
    assert len(client.created) == 1 and sum(x.startswith("remove") for x in c.calls) == 1

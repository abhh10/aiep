"""Gateway injection integrity, ownership and start ordering (fakes only; nothing here touches Docker)."""
import ast
import os
import threading
from pathlib import Path

import pytest

from aiep.container_policy import GatewayContext, PolicyViolation
from aiep.gateway_runner import (GATEWAY_DEST, GATEWAY_FILES, GatewayIntegrityError, build_gateway_component,
                                 gateway_exec_context, inject_verify_start_gateway)
from aiep.seed import SeedError, verify_archive
from fake_docker import ExecResult, FakeContainerFS, FakeExecApi

REAL = Path(__file__).resolve().parents[1]
CID = "gw123"
PY = "/usr/local/bin/python3"
OWNER = (10001, 10001)  # TEST-ONLY: the real gateway uid:gid is unresolved (config/gateway_image.json)


def gctx(**kw):
    kw.setdefault("uid", OWNER[0])
    kw.setdefault("gid", OWNER[1])
    return GatewayContext(agent_network="aiep-agent-net", egress_network="aiep-egress-net",
                          image_id="sha256:" + "9" * 64, python=PY, **kw)


@pytest.fixture(scope="module")
def code():
    return build_gateway_component(REAL)


def rig(tmp_path, *, wrap=None, owner=OWNER, python=PY):
    fs = FakeContainerFS(tmp_path / "fs", python=python, owner=owner)
    handler = fs.handler if wrap is None else wrap(fs)
    return fs, FakeExecApi(handler)


def run(api, code, ctx=None):
    ctx = ctx or gctx()
    return inject_verify_start_gateway(api, CID, code, ctx, gateway_exec_context(ctx))


def after_tar(action):
    """Handler wrapper: run ``action(fs)`` right after the gateway tar is extracted (a tampering stand-in)."""
    def wrap(fs):
        def handler(argv, rec, stdin):
            res = fs.handler(argv, rec, stdin)
            if argv[0] == "/bin/tar" and argv[-1] == GATEWAY_DEST:
                action(fs.path(GATEWAY_DEST))
            return res
        return handler
    return wrap


# ---------------- the manifest ----------------
def test_gateway_manifest_covers_exactly_the_listed_files_with_fixed_modes(code):
    verify_archive(code)
    files = {e.path: e for e in code.manifest.entries if e.type == "f"}
    assert set(files) == set(GATEWAY_FILES)
    assert all(e.mode == 0o644 for e in files.values())
    assert {e.path: e.mode for e in code.manifest.entries if e.type == "d"} == {"src": 0o755, "src/aiep": 0o755, "config": 0o755}
    assert code.manifest.archive_sha256 and code.manifest.head is None


def test_shipped_modules_import_only_shipped_modules():
    shipped = {Path(f).stem for f in GATEWAY_FILES if f.startswith("src/aiep/")}
    for rel in GATEWAY_FILES:
        if not rel.endswith(".py"):
            continue
        for node in ast.walk(ast.parse((REAL / rel).read_text())):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                assert node.module in shipped, f"{rel} imports aiep.{node.module}, which is not injected"


def test_the_build_is_deterministic(code):
    assert build_gateway_component(REAL).manifest.archive_sha256 == code.manifest.archive_sha256


# ---------------- verify, then start ----------------
def test_gateway_starts_only_after_a_clean_verification(tmp_path, code):
    fs, api = rig(tmp_path)
    session = run(api, code)
    session.cancel()
    names = [(c["cmd"][0], c["cmd"][1] if len(c["cmd"]) > 1 else "") for c in api.calls]
    assert names == [("/bin/tar", "-x"), (PY, "-I"), (PY, "-m")]
    assert fs.listings == 1 and fs.started_after_listings == 1  # exactly one verification came first
    assert api.calls[2]["cmd"] == [PY, "-m", "aiep.gateway"]
    assert api.calls[0]["cmd"][-1] == GATEWAY_DEST and api.stdins[0] == code.tar
    # every exec ran as the gateway's one identity; the start exec carries no environment of its own
    assert {c["user"] for c in api.calls} == {"10001:10001"} and api.calls[2]["environment"] == {}


@pytest.mark.parametrize("tamper, expect", [
    (lambda d: (d / "src/aiep/gateway.py").unlink(), "missing: src/aiep/gateway.py"),
    (lambda d: (d / "src/aiep/evil.py").write_text("x"), "unexpected entry: src/aiep/evil.py"),
    (lambda d: (d / "extra_dir").mkdir(), "unexpected entry: extra_dir"),
    (lambda d: (d / "src/aiep/gateway.py").write_bytes(b"# changed\n"), "content differs: src/aiep/gateway.py"),
    (lambda d: (d / "src/aiep/ledger.py").write_bytes((d / "src/aiep/ledger.py").read_bytes() + b"#"), "content differs: src/aiep/ledger.py"),
    (lambda d: os.chmod(d / "src/aiep/pricing.py", 0o666), "mode differs: src/aiep/pricing.py"),
    (lambda d: os.chmod(d / "src/aiep/pricing.py", 0o755), "mode differs: src/aiep/pricing.py"),
    (lambda d: os.chmod(d / "src", 0o777), "mode differs: src"),
    (lambda d: ((d / "config/experiment.json").unlink(), os.symlink("/etc/passwd", d / "config/experiment.json")),
     "type differs: config/experiment.json"),
    (lambda d: os.symlink("/etc", d / "link"), "unexpected entry: link"),
    (lambda d: os.mkfifo(d / "fifo"), "unexpected special file: fifo"),
])
def test_any_difference_aborts_before_the_gateway_process_exists(tmp_path, code, tamper, expect):
    fs, api = rig(tmp_path, wrap=after_tar(tamper))
    with pytest.raises(GatewayIntegrityError, match="does not match its manifest") as e:
        run(api, code)
    assert expect in str(e.value)
    assert fs.gateway_started == [] and all(c["cmd"][1:2] != ["-m"] for c in api.calls)
    assert [c["cmd"][0] for c in api.calls] == ["/bin/tar", PY]


def test_a_tampered_tar_is_refused_before_anything_is_sent(tmp_path, code):
    from aiep.seed import Seed
    flipped = bytearray(code.tar)
    flipped[len(flipped) // 2] ^= 0xFF
    bad = Seed(code.manifest, bytes(flipped))
    fs, api = rig(tmp_path)
    with pytest.raises(SeedError, match="SHA-256"):
        run(api, bad)
    assert api.calls == []


def test_a_missing_or_wrong_interpreter_is_a_failure_not_a_fallback(tmp_path, code):
    fs, api = rig(tmp_path, python="/opt/miniconda3/bin/python")  # the image does not have the configured one
    from aiep.container_exec import ExecError
    with pytest.raises(ExecError, match="exit code 127"):
        run(api, code)
    assert [c["cmd"][0] for c in api.calls] == ["/bin/tar", PY] and fs.gateway_started == []


def test_the_exec_context_must_match_the_gateway_context(tmp_path, code):
    fs, api = rig(tmp_path)
    ctx = gctx()
    good = gateway_exec_context(ctx)
    for bad in (gateway_exec_context(gctx(uid=10002)), gateway_exec_context(gctx(gid=10002)),
                type(good)(user=good.user, allowed_scripts=good.allowed_scripts, python=PY, may_start_gateway=False),
                type(good)(user=good.user, allowed_scripts=good.allowed_scripts,
                           python="/opt/miniconda3/bin/python", may_start_gateway=True)):
        with pytest.raises(GatewayIntegrityError, match="does not match"):
            inject_verify_start_gateway(api, CID, code, ctx, bad)
    assert api.calls == []


# ---------------- ownership ----------------
@pytest.mark.parametrize("owner", [(0, 0), (0, 10001), (10001, 0), (10002, 10001), (10001, 10002), (1001, 1001)])
def test_files_not_owned_by_the_gateway_identity_abort_the_start(tmp_path, code, owner):
    fs, api = rig(tmp_path, owner=owner)
    with pytest.raises(GatewayIntegrityError, match="owner differs"):
        run(api, code)
    assert fs.gateway_started == []


def test_a_listing_without_owners_cannot_pass_verification(tmp_path, code):
    def wrap(fs):
        def handler(argv, rec, stdin):
            res = fs.handler(argv, rec, stdin)
            if len(argv) > 3 and argv[1:3] == ["-I", "-c"]:
                import json
                res = ExecResult(stdout=json.dumps([r[:6] for r in json.loads(res.stdout)]).encode())
            return res
        return handler
    fs, api = rig(tmp_path, owner=None, wrap=wrap)
    with pytest.raises(GatewayIntegrityError, match="owner not reported"):
        run(api, code)
    assert fs.gateway_started == []


def test_a_different_identity_cannot_even_issue_the_exec(tmp_path, code):
    ctx = gctx()
    ectx = gateway_exec_context(ctx)
    from aiep.container_exec import run_exec
    fs, api = rig(tmp_path)
    for user in ("root", "0", "0:0", "10001", "10002:10001"):
        with pytest.raises(PolicyViolation):
            from aiep.container_policy import harden_exec
            harden_exec([PY, "-m", "aiep.gateway"], user=user, workdir="/", env={}, ctx=ectx)
    assert api.calls == []


def test_the_real_listing_reports_the_owner_of_what_was_extracted(tmp_path, code):
    """No simulation: the unmodified VERIFY_SCRIPT reports this process's own uid:gid for files it wrote."""
    fs, api = rig(tmp_path, owner=None)
    me = (os.getuid(), os.getgid())
    if me[0] == 0:
        pytest.skip("running as root: the identity under test must be non-root")
    ctx = gctx(uid=me[0], gid=me[1])
    session = inject_verify_start_gateway(api, CID, code, ctx, gateway_exec_context(ctx))
    session.cancel()
    assert fs.gateway_started == [[PY, "-m", "aiep.gateway"]]
    ctx_other = gctx(uid=me[0] + 1, gid=me[1])
    fs2, api2 = rig(tmp_path / "b", owner=None)
    with pytest.raises(GatewayIntegrityError, match="owner differs"):
        inject_verify_start_gateway(api2, CID, code, ctx_other, gateway_exec_context(ctx_other))


def test_gateway_stop_file_is_readable_by_the_same_identity(tmp_path):
    from aiep.container_exec import read_stop_reason
    ctx = gctx()
    ectx = gateway_exec_context(ctx, readable_paths=frozenset({"/tmp/gateway.stop"}))
    api = FakeExecApi(lambda argv, rec, stdin: ExecResult(stdout=b"budget_exhausted\n"))
    assert read_stop_reason(api, CID, "/tmp/gateway.stop", ctx=ectx) == "budget_exhausted"
    assert api.calls[0]["user"] == "10001:10001"

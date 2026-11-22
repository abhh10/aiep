import json
import os
import shutil
from pathlib import Path

import pytest
from docker.models.containers import ContainerCollection
from docker.models.images import ImageCollection

from aiep import evaluator, gate
from aiep.container_policy import PolicyViolation
from test_gate import GOOD_PROBE, REAL, make_key, make_repo

IMAGE = "swebench/sweb.eval.x86_64.pytest-dev_1776_pytest-5227:latest"
IMAGE_ID = "sha256:c91459f86ed261ab81871b086d7ca2d9fe16abd2ddf374bf172edd5d991fd28b"
INSTANCE = "pytest-dev__pytest-5227"


class FakeImage:
    def __init__(self, id_):
        self.id = id_


class FakeContainer:
    id = "c0ffee"

    def __init__(self):
        self.removed = False

    def remove(self, force=False):
        self.removed = True


class FakeApi:
    def __init__(self, inspect):
        self.inspect = inspect

    def inspect_container(self, _id):
        return self.inspect


class FakeClient:
    def __init__(self, inspect):
        self.api = FakeApi(inspect)


def good_inspect(image_id=IMAGE_ID):
    return {
        "Image": image_id,
        "HostConfig": {
            "Privileged": False, "CapAdd": None, "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"], "NetworkMode": "none",
            "Memory": 2 * 1024**3, "PidsLimit": 1024, "NanoCpus": 2_000_000_000,
        },
        "Mounts": [],
        "Config": {"User": "root"},
    }


@pytest.fixture
def docker_fakes(monkeypatch):
    """Replace the Docker SDK entry points with recording fakes (no Docker involved)."""
    state = {"created": [], "container": FakeContainer(), "image_id": IMAGE_ID, "pulls": 0}

    def fake_create(self, image, command=None, **kwargs):
        state["created"].append({"image": image, "command": command, **kwargs})
        return state["container"]

    def fake_get(self, name):
        return FakeImage(state["image_id"])

    def fake_pull(self, *a, **k):
        state["pulls"] += 1

    monkeypatch.setattr(ContainerCollection, "create", fake_create)
    monkeypatch.setattr(ImageCollection, "get", fake_get)
    monkeypatch.setattr(ImageCollection, "pull", fake_pull)
    return state


def stock_create(client, image=IMAGE):
    # exactly how swebench 5.0.2 calls it
    return ContainerCollection(client=client).create(
        image=image, name="sweb.eval.pytest-dev__pytest-5227.r1", user="root",
        detach=True, command="tail -f /dev/null", cap_add=["SYS_ADMIN"],
    )


# ---------------- Guards ----------------
def test_guards_harden_create_and_log(tmp_path, docker_fakes):
    g = evaluator.Guards({IMAGE: IMAGE_ID}, tmp_path / "k.jsonl")
    g.install()
    try:
        client = FakeClient(good_inspect())
        ImageCollection(client=client).get(IMAGE)
        stock_create(client)
    finally:
        g.uninstall()
    sent = docker_fakes["created"][0]
    assert sent["network_mode"] == "none" and sent["cap_drop"] == ["ALL"] and "cap_add" not in sent
    assert g.creates == 1
    logged = json.loads((tmp_path / "k.jsonl").read_text().splitlines()[0])
    assert logged["network_mode"] == "none"


def test_guards_uninstall_restores_originals(docker_fakes):
    before = (ContainerCollection.create, ImageCollection.get, ImageCollection.pull)
    g = evaluator.Guards({IMAGE: IMAGE_ID}, Path("/dev/null"))
    g.install()
    assert ContainerCollection.create is not before[0]
    g.uninstall()
    assert (ContainerCollection.create, ImageCollection.get, ImageCollection.pull) == before


@pytest.mark.parametrize("name", ["pull", "build", "load", "push"])
def test_image_acquisition_is_refused(tmp_path, docker_fakes, name):
    g = evaluator.Guards({IMAGE: IMAGE_ID}, tmp_path / "k.jsonl")
    g.install()
    try:
        with pytest.raises(PolicyViolation, match=f"images.{name}"):
            getattr(ImageCollection(client=None), name)("x")
    finally:
        g.uninstall()
    assert docker_fakes["pulls"] == 0 and g.blocked


def test_wrong_image_id_refused(tmp_path, docker_fakes):
    docker_fakes["image_id"] = "sha256:" + "f" * 64
    g = evaluator.Guards({IMAGE: IMAGE_ID}, tmp_path / "k.jsonl")
    g.install()
    try:
        with pytest.raises(PolicyViolation, match="expected"):
            ImageCollection(client=None).get(IMAGE)
    finally:
        g.uninstall()


def test_unknown_image_refused(tmp_path, docker_fakes):
    g = evaluator.Guards({IMAGE: IMAGE_ID}, tmp_path / "k.jsonl")
    g.install()
    try:
        with pytest.raises(PolicyViolation, match="not in config"):
            ImageCollection(client=None).get("alpine:latest")
    finally:
        g.uninstall()


def test_create_without_prior_verification_refused(tmp_path, docker_fakes):
    g = evaluator.Guards({IMAGE: IMAGE_ID}, tmp_path / "k.jsonl")
    g.install()
    try:
        with pytest.raises(PolicyViolation, match="not verified"):
            stock_create(FakeClient(good_inspect()))
    finally:
        g.uninstall()
    assert docker_fakes["created"] == []


def test_create_with_unknown_kwarg_refused_before_docker(tmp_path, docker_fakes):
    g = evaluator.Guards({IMAGE: IMAGE_ID}, tmp_path / "k.jsonl")
    g.install()
    try:
        client = FakeClient(good_inspect())
        ImageCollection(client=client).get(IMAGE)
        with pytest.raises(PolicyViolation, match="unexpected container argument"):
            ContainerCollection(client=client).create(
                image=IMAGE, name="sweb.eval.x", user="root", detach=True,
                command="tail -f /dev/null", cap_add=["SYS_ADMIN"], sysctls={"a": "b"},
            )
    finally:
        g.uninstall()
    assert docker_fakes["created"] == []


def test_container_failing_inspect_is_removed_and_refused(tmp_path, docker_fakes):
    bad = good_inspect()
    bad["HostConfig"]["NetworkMode"] = "bridge"  # e.g. the harness changed in a way kwargs hid
    g = evaluator.Guards({IMAGE: IMAGE_ID}, tmp_path / "k.jsonl")
    g.install()
    try:
        client = FakeClient(bad)
        ImageCollection(client=client).get(IMAGE)
        with pytest.raises(PolicyViolation):
            stock_create(client)
    finally:
        g.uninstall()
    assert docker_fakes["container"].removed and g.creates == 0 and g.blocked


def test_container_from_different_image_is_removed(tmp_path, docker_fakes):
    g = evaluator.Guards({IMAGE: IMAGE_ID}, tmp_path / "k.jsonl")
    g.install()
    try:
        client = FakeClient(good_inspect(image_id="sha256:" + "e" * 64))
        ImageCollection(client=client).get(IMAGE)
        with pytest.raises(PolicyViolation, match="not from the verified image"):
            stock_create(client)
    finally:
        g.uninstall()
    assert docker_fakes["container"].removed


# ---------------- evaluation_problems ----------------
CLEAN = {"submitted_instances": 1, "completed_instances": 1, "error_instances": 0,
         "infra_failure_instances": 0, "empty_patch_instances": 0, "resolved_instances": 0}


def test_unresolved_is_not_a_problem_but_errors_are():
    assert evaluator.evaluation_problems(CLEAN) == []
    for k in ("error_instances", "infra_failure_instances", "empty_patch_instances"):
        assert evaluator.evaluation_problems({**CLEAN, k: 1})
    assert evaluator.evaluation_problems({**CLEAN, "completed_instances": 0})
    assert evaluator.evaluation_problems({**CLEAN, "submitted_instances": 0})
    assert evaluator.evaluation_problems({})


# ---------------- main(): gate, exit codes ----------------
@pytest.fixture
def env(tmp_path, monkeypatch, docker_fakes):
    root = make_repo(tmp_path)
    key = make_key(tmp_path)
    att = tmp_path / "att.json"
    att.write_text(json.dumps(
        gate.build_attestation(gate.current_components(root), GOOD_PROBE, gate.load_key(key))))
    monkeypatch.setenv("AIEP_ATTEST_KEY_FILE", str(key))
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("HF_DATASETS_OFFLINE", "0")
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.chdir(run)
    return dict(root=root, key=key, att=att, run=run, state=docker_fakes)


def argv(env, run_id="r1", predictions="gold", att=None):
    return ["-i", INSTANCE, "-p", predictions, "--run-id", run_id,
            "--attestation", str(att or env["att"])]


def fake_harness(env, report, *, create=True):
    calls = []

    def harness(**kw):
        calls.append(kw)
        if create:
            client = FakeClient(good_inspect())
            ImageCollection(client=client).get(IMAGE)
            stock_create(client)
        out = Path(kw["report_dir"])
        out.mkdir(exist_ok=True)
        path = out / f"gold.{kw['run_id']}.json"
        path.write_text(json.dumps(report))
        return path

    harness.calls = calls
    return harness


def test_main_refuses_without_valid_attestation_and_never_starts_harness(env, capsys):
    h = fake_harness(env, CLEAN)
    rc = evaluator.main(argv(env, att=env["run"] / "nope.json"), run_harness=h, repo_root=env["root"])
    assert rc == evaluator.EXIT_REFUSED and h.calls == []
    assert "REFUSED" in capsys.readouterr().err


def test_main_gold_gets_no_bypass(env):
    # 'gold' predictions go through exactly the same gate as any other predictions
    h = fake_harness(env, CLEAN)
    with open(env["root"] / "src" / "aiep" / "evaluator.py", "a") as f:
        f.write("\n# edited\n")
    rc = evaluator.main(argv(env, predictions="gold"), run_harness=h, repo_root=env["root"])
    assert rc == evaluator.EXIT_REFUSED and h.calls == []


def test_main_refuses_unknown_instance(env):
    h = fake_harness(env, CLEAN)
    a = argv(env)
    a[1] = "django__django-1"
    assert evaluator.main(a, run_harness=h, repo_root=env["root"]) == evaluator.EXIT_REFUSED
    assert h.calls == []


def test_main_clean_run_exits_zero_and_forces_offline_and_one_worker(env):
    h = fake_harness(env, CLEAN)
    assert evaluator.main(argv(env), run_harness=h, repo_root=env["root"]) == 0
    assert h.calls[0]["max_workers"] == 1
    assert os.environ["HF_HUB_OFFLINE"] == "1" and os.environ["HF_DATASETS_OFFLINE"] == "1"


def test_main_unresolved_but_clean_exits_zero(env):
    h = fake_harness(env, {**CLEAN, "unresolved_instances": 1})
    assert evaluator.main(argv(env), run_harness=h, repo_root=env["root"]) == 0


def test_main_zero_intercepted_creates_is_a_failure(env, capsys):
    h = fake_harness(env, CLEAN, create=False)
    assert evaluator.main(argv(env), run_harness=h, repo_root=env["root"]) == evaluator.EXIT_EVAL_PROBLEM
    assert "no container creation" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["error_instances", "infra_failure_instances", "empty_patch_instances"])
def test_main_instance_errors_exit_nonzero(env, bad):
    h = fake_harness(env, {**CLEAN, bad: 1})
    assert evaluator.main(argv(env), run_harness=h, repo_root=env["root"]) == evaluator.EXIT_EVAL_PROBLEM


def test_main_policy_violation_in_harness_exits_refused_and_uninstalls(env):
    before = ImageCollection.pull  # the fixture's fake, i.e. what must be restored

    def harness(**kw):
        ImageCollection(client=None).pull("anything")

    assert evaluator.main(argv(env), run_harness=harness, repo_root=env["root"]) == evaluator.EXIT_REFUSED
    assert ImageCollection.pull is before  # guards fully uninstalled
    assert env["state"]["pulls"] == 0


def test_m1_files_are_untouched():
    """The M1 record stays intact. src/aiep/stock_eval.py is checked separately below: it gained the evaluator image guard (an approved change), nothing else."""
    import subprocess

    out = subprocess.run(
        ["git", "diff", "--stat", "9c84d4d", "--",
         "scripts/m1_eval.sh", "tests/test_stock_eval.py", "docs/M1_RESULTS.md"],
        cwd=REAL, capture_output=True, text=True,
    )
    assert out.returncode == 0 and out.stdout.strip() == ""


def test_stock_eval_keeps_its_m1_container_hardening_byte_for_byte_and_only_gained_the_image_guard():
    """The M1 evaluator's behaviour that the recorded runs depend on (constants, harden_create_kwargs, install_shim) is identical to the M1 commit; the image guard was added."""
    import ast
    import subprocess

    old = subprocess.run(["git", "show", "9c84d4d:src/aiep/stock_eval.py"], cwd=REAL, capture_output=True, text=True)
    assert old.returncode == 0
    new_src = (REAL / "src" / "aiep" / "stock_eval.py").read_text()

    def nodes(src):
        out = {}
        for n in ast.parse(src).body:
            if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                out[n.name] = ast.dump(n)
            elif isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
                out[n.targets[0].id] = ast.dump(n)
        return out
    o, n = nodes(old.stdout), nodes(new_src)
    for name in ("MEM_LIMIT", "PIDS_LIMIT", "NANO_CPUS", "_FORBIDDEN", "harden_create_kwargs", "install_shim"):
        assert o[name] == n[name], f"{name} differs from the M1 version"
    assert {"install_image_guard", "ImageAcquisitionBlocked"} <= set(n) and "main" in n

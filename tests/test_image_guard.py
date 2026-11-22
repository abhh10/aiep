"""The evaluator's fail-closed image policy (aiep.stock_eval.install_image_guard): no pull, build or load inside the evaluator process. Fake Docker only: no daemon, no network."""
import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import docker
import pytest

from aiep import grading as g
from aiep import stock_eval as se
from aiep.stock_eval import ImageAcquisitionBlocked, install_image_guard

REAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REAL / "scripts"))
IMG = "sha256:" + "6" * 64
NAME = "swebench/sweb.eval.x86_64.demo_1776_demo-1:latest"


class FakeAPI:
    """The slice of docker.APIClient the image collection uses; any network-ish call is an error."""

    def __init__(self, have=None):
        self.have, self.calls = dict(have or {}), []

    def inspect_image(self, name):
        self.calls.append(("inspect_image", name))
        if name not in self.have:
            raise docker.errors.ImageNotFound(name)
        return {"Id": self.have[name], "RepoTags": [name], "Os": "linux", "Architecture": "amd64"}

    def pull(self, *a, **k):
        raise AssertionError("NETWORK: the registry must never be contacted")

    def build(self, *a, **k):
        raise AssertionError("NETWORK/BUILD must never happen")

    def load_image(self, *a, **k):
        raise AssertionError("LOAD must never happen")


def images_of(api):
    from docker.models.images import ImageCollection
    return ImageCollection(client=SimpleNamespace(api=api))


@pytest.fixture
def guard(tmp_path):
    restore = install_image_guard(tmp_path / se.POLICY_MARKER)
    yield tmp_path / se.POLICY_MARKER
    restore()


def test_a_local_verified_image_can_still_be_inspected_listed_and_looked_up(guard):
    api = FakeAPI({NAME: IMG})
    img = images_of(api).get(NAME)
    assert img.id == IMG and api.calls == [("inspect_image", NAME)]
    assert not guard.exists()  # lookups never trip the guard


def test_container_creation_is_not_touched_by_the_guard(guard):
    from docker.models.containers import ContainerCollection
    original = ContainerCollection.create
    restore = install_image_guard(guard)  # idempotent second install
    try:
        assert ContainerCollection.create is original and not getattr(original, "_aiep_image_guard", False)
    finally:
        restore()


def test_explicit_pull_raises_the_dedicated_error_and_never_reaches_the_api(guard):
    api = FakeAPI()
    with pytest.raises(ImageAcquisitionBlocked, match="pre-existing verified image"):
        images_of(api).pull(NAME)
    assert api.calls == [] and "ImageCollection.pull" in guard.read_text()


@pytest.mark.parametrize("call", [lambda i: i.build(path="."), lambda i: i.load(b"tar"), lambda i: i.pull("repo", tag="latest")])
def test_build_load_and_pull_are_blocked_at_the_collection(guard, call):
    with pytest.raises(ImageAcquisitionBlocked):
        call(images_of(FakeAPI()))


@pytest.mark.parametrize("name", ["pull", "build", "load_image", "import_image", "import_image_from_data", "import_image_from_file", "import_image_from_image",
                                  "import_image_from_stream", "import_image_from_url"])
def test_the_low_level_api_paths_are_blocked_too(guard, name):
    api = docker.APIClient.__new__(docker.APIClient)  # no connection is made
    with pytest.raises(ImageAcquisitionBlocked):
        getattr(api, name)()


def test_the_guard_error_is_not_an_ordinary_exception():
    assert issubclass(ImageAcquisitionBlocked, BaseException) and not issubclass(ImageAcquisitionBlocked, Exception)


def test_the_upstream_imagenotfound_fallback_is_intercepted_not_converted(guard):
    """The real upstream create_container does get() then, on ImageNotFound, pull(). With the guard the pull raises and the harness's own `except Exception` does not turn it
    into an EvaluationError."""
    from swebench.harness.run_evaluation import create_container
    api = FakeAPI()  # the image is NOT local
    client = SimpleNamespace(images=images_of(api), containers=SimpleNamespace(get=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no container before the image check"))))
    spec = SimpleNamespace(image=NAME, instance_id="demo__demo-1")
    with pytest.raises(ImageAcquisitionBlocked):
        create_container(spec, client, "run1", logging.getLogger("t"))
    assert api.calls == [("inspect_image", NAME)] and guard.exists()


def test_without_the_guard_the_upstream_fallback_reaches_the_registry_and_is_swallowed_into_an_ordinary_failure():
    """Control for the test above: unguarded, the fallback calls the API's pull (the fake turns that into a loud 'NETWORK' error), and the harness's own `except Exception`
    wraps it in an EvaluationError: exactly the conversion the guard's BaseException avoids."""
    from swebench.harness.run_evaluation import create_container
    api = FakeAPI()
    client = SimpleNamespace(images=images_of(api))
    spec = SimpleNamespace(image=NAME, instance_id="demo__demo-1")
    logger = logging.getLogger("t")
    logger.log_file = "none"  # what EvaluationError reads
    with pytest.raises(Exception) as e:
        create_container(spec, client, "run1", logger)
    assert "NETWORK" in str(e.value) and not isinstance(e.value, ImageAcquisitionBlocked)


def test_a_sdk_without_a_guarded_method_refuses_to_install(monkeypatch):
    from docker.models.images import ImageCollection
    monkeypatch.delattr(ImageCollection, "load")
    with pytest.raises(RuntimeError, match="cannot be installed"):
        install_image_guard("m")
    assert not getattr(ImageCollection.__dict__["pull"], "_aiep_image_guard", False)  # nothing is left half-patched


def test_the_guard_can_be_restored_so_it_lives_only_in_this_process_scope(tmp_path):
    from docker.models.images import ImageCollection
    before = ImageCollection.pull
    restore = install_image_guard(tmp_path / "m")
    assert ImageCollection.pull is not before
    restore()
    assert ImageCollection.pull is before


# ---------------------------------------------------------------- the entrypoint applies it before the harness runs
def test_main_installs_the_guard_before_the_upstream_harness_and_exits_97_on_a_violation(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    seen = {}

    def fake_run_evaluation(**kw):
        from docker.models.images import ImageCollection
        seen["guarded"] = getattr(ImageCollection.__dict__["pull"], "_aiep_image_guard", False)
        images_of(FakeAPI()).pull(NAME)  # what the upstream fallback does
        raise AssertionError("unreachable")
    monkeypatch.setitem(sys.modules, "swebench.harness.run_evaluation", SimpleNamespace(main=fake_run_evaluation))
    assert se.main(["-i", "demo__demo-1", "-p", "gold", "--run-id", "r1"]) == se.EXIT_IMAGE_POLICY == 97
    assert seen["guarded"] is True and (tmp_path / se.POLICY_MARKER).exists() and "IMAGE POLICY VIOLATION" in capsys.readouterr().err
    from docker.models.images import ImageCollection
    assert not getattr(ImageCollection.__dict__["pull"], "_aiep_image_guard", False)  # restored on the way out


def test_main_returns_0_when_the_harness_only_uses_a_local_image(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "swebench.harness.run_evaluation", SimpleNamespace(main=lambda **kw: images_of(FakeAPI({NAME: IMG})).get(NAME)))
    assert se.main(["-i", "demo__demo-1", "-p", "gold", "--run-id", "r2"]) == 0 and not (tmp_path / se.POLICY_MARKER).exists()


# ---------------------------------------------------------------- grading.grade() and scripts/m3_grade.py
def grade_kwargs(tmp_path, run_process, **over):
    kw = dict(kind="gold", root=tmp_path, expected_image={"image": NAME, "image_id": IMG}, free_gib=lambda: 30.0, image_id_of=lambda n: IMG, run_process=run_process,
              kill_containers=lambda r: None, sleep=lambda s: None, sample_interval_s=0.01)
    kw.update(over)
    return kw


def test_grade_runs_the_guarded_entrypoint_and_nothing_else(tmp_path):
    seen = []
    g.grade("demo__demo-1", "gold", "r-guarded", **grade_kwargs(tmp_path, lambda cmd, cwd, env: seen.append(list(cmd)) or 1))
    assert seen[0][1:3] == ["-m", "aiep.stock_eval"]


def test_grade_raises_a_dedicated_error_when_the_evaluator_hit_the_guard_via_its_marker(tmp_path):
    def run(cmd, cwd, env):
        (Path(cwd) / se.POLICY_MARKER).write_text("ImageCollection.pull was called: x\n")
        return 1
    with pytest.raises(g.ImagePolicyViolation, match="pull, build or load"):
        g.grade("demo__demo-1", "gold", "r-marker", **grade_kwargs(tmp_path, run))
    import json
    meta = json.loads((tmp_path / "runs" / "m1" / "r-marker" / "meta.json").read_text())
    assert meta["summary"]["valid"] is False and meta["summary"]["image_policy_violation"] is True


def test_grade_raises_on_exit_code_97_even_without_a_marker(tmp_path):
    with pytest.raises(g.ImagePolicyViolation):
        g.grade("demo__demo-1", "gold", "r-97", **grade_kwargs(tmp_path, lambda cmd, cwd, env: se.EXIT_IMAGE_POLICY))


def test_a_mismatched_image_id_is_still_refused_before_anything_starts(tmp_path):
    ran = []
    with pytest.raises(g.GradingRefused, match="never pulled or rebuilt"):
        g.grade("demo__demo-1", "gold", "r-mismatch", **grade_kwargs(tmp_path, lambda *a: ran.append(1) or 0, image_id_of=lambda n: "sha256:" + "0" * 64))
    with pytest.raises(g.GradingRefused, match="never pulled or rebuilt"):
        g.grade("demo__demo-1", "gold", "r-missing", **grade_kwargs(tmp_path, lambda *a: ran.append(1) or 0, image_id_of=lambda n: None))
    assert ran == []


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, REAL / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_m3_grade_goes_through_grading_grade_and_the_guarded_entrypoint(tmp_path, monkeypatch):
    m = load_script("m3_grade")
    seen = []
    monkeypatch.setattr(m.shutil, "disk_usage", lambda p: SimpleNamespace(free=30 * 1024**3))
    monkeypatch.setattr(m, "real_image_id", lambda name: IMG)
    monkeypatch.setattr(m, "run_process", lambda cmd, cwd, env: seen.append(list(cmd)) or 1)
    rc = m.main(["demo__demo-1", "r-m3", "gold", "--kind", "gold"], root=tmp_path, pin_fn=lambda r, i: {"image": NAME, "image_id": IMG})
    assert rc == 1 and seen and seen[0][1:3] == ["-m", "aiep.stock_eval"]


def test_m3_grade_reports_a_policy_violation_with_exit_3(tmp_path, monkeypatch, capsys):
    m = load_script("m3_grade")

    def violating(*a, **k):
        raise g.ImagePolicyViolation("x: the evaluator tried to pull")
    rc = m.main(["demo__demo-1", "r-v", "gold", "--kind", "gold"], root=tmp_path, grade_fn=violating, pin_fn=lambda r, i: {"image": NAME, "image_id": IMG})
    assert rc == 3 and "IMAGE POLICY VIOLATION" in capsys.readouterr().err


# ---------------------------------------------------------------- no supported entrypoint bypasses the guard
def test_every_supported_evaluator_invocation_goes_through_aiep_stock_eval():
    import re
    direct = re.compile(r"(from|import)\s+swebench\.harness\.run_evaluation|-m\s+swebench")
    offenders = []
    for base in ("src", "scripts"):
        for p in sorted((REAL / base).rglob("*")):
            if p.suffix in (".py", ".sh") and p.name != "stock_eval.py" and direct.search(p.read_text()):
                offenders.append(str(p.relative_to(REAL)))
    # the only other direct import is the gated hardened evaluator (attestation required; not runnable today), which carries its OWN image refusals
    assert offenders == ["src/aiep/evaluator.py"], offenders
    own = (REAL / "src" / "aiep" / "evaluator.py").read_text()
    assert 'for name in ("pull", "build", "load", "push")' in own and "require_gate" in own
    assert "-m aiep.stock_eval" in (REAL / "scripts" / "m1_eval.sh").read_text()
    assert g.command_for("a__b-1", "gold", "r", repo=REAL)[1:3] == ["-m", "aiep.stock_eval"]


def test_the_stock_eval_module_installs_the_guard_before_importing_the_harness():
    text = (REAL / "src" / "aiep" / "stock_eval.py").read_text()
    main = text[text.index("def main("):]
    assert main.index("install_image_guard()") < main.index("from swebench.harness.run_evaluation import")

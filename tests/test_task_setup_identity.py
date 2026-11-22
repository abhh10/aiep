"""task_setup.pin_image and verify_and_pin_identity, and the pin/pull/identity scripts, with fake Docker only: no daemon, no container, no network, no download."""
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiep import task_pins as tp
from aiep import task_setup as ts
from aiep.agent_runner import build_create_kwargs
from aiep.seed import parse_seed_archive, save_seed
from aiep.task_setup import SetupRefused
from fake_docker import ExecResult, FakeExecApi
from test_agent_runner import FakeContainer, good_inspect
from test_seed import good_entries, make_tar

REAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REAL / "scripts"))
GIB = 1024**3
INSTANCE = "demo__demo-1"
IMAGE = "sha256:" + "d" * 64
PASSWD = b"1000\n1000\nnonroot\nnonroot:x:1000:1000::/home/nonroot:/bin/bash\n"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, REAL / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def root(tmp_path):
    shutil.copytree(REAL / "config", tmp_path / "config")
    for f in ("evaluator_images.json", "seeds.json"):
        (tmp_path / "config" / f).write_text(json.dumps({"images" if "images" in f else "seeds": {}}))
    shutil.rmtree(tmp_path / "config" / "task_identities", ignore_errors=True)
    return tmp_path


def pin_everything_but_identity(root):
    tp.add_evaluator_image(root, INSTANCE, IMAGE, source="test")
    seed = parse_seed_archive(make_tar(good_entries()))
    d = root / ".cache" / "seeds" / INSTANCE
    save_seed(d, seed)
    tp.add_seed_pin(root, INSTANCE, directory=f".cache/seeds/{INSTANCE}", sha256=seed.manifest.archive_sha256, head=seed.manifest.head, entries=len(seed.manifest.entries), image_id=IMAGE)


class Images:
    def __init__(self, have):
        self.have, self.calls = have, []

    def get(self, ref):
        self.calls.append(("get", ref))
        if ref not in self.have:
            raise LookupError("no such image")
        return SimpleNamespace(id=self.have[ref])

    def pull(self, *a, **k):
        raise AssertionError("an image must never be pulled here")


# ---------------------------------------------------------------- pin_image (by digest reference; see tests/test_image_digests.py)
def digest_record(root, image_id=IMAGE):
    """A verified-looking digest record for the demo task whose index digest is IMAGE, written into the root's lock file."""
    from aiep import image_digests as idg
    rec = {"repository": "swebench/sweb.eval.x86_64.demo_1776_demo-1", "index_digest": image_id, "index_media_type": idg.OCI_INDEX, "platform": "linux/amd64",
           "platform_manifest_digest": "sha256:" + "b" * 64, "platform_manifest_media_type": idg.OCI_MANIFEST, "config_digest": "sha256:" + "c" * 64, "config_size": 10,
           "layers": [{"digest": "sha256:" + "1" * 64, "size": 5}], "compressed_layer_bytes": 5, "provenance": {"source": "test", "verified_on": "2026-11-22"}}
    (root / "config" / "image_digests.json").write_text(json.dumps({"schema": 1, "platform": "linux/amd64", "images": {INSTANCE: rec}}))
    return rec


def local_client(rec, image_id=IMAGE):
    from aiep import image_digests as idg
    img = SimpleNamespace(id=image_id, attrs={"Os": "linux", "Architecture": "amd64"})
    return SimpleNamespace(images=Images({idg.reference(rec): image_id}), _img=img)


class Images2(Images):
    def get(self, ref):
        self.calls.append(("get", ref))
        if ref not in self.have:
            raise LookupError("no such image")
        return SimpleNamespace(id=self.have[ref], attrs={"Os": "linux", "Architecture": "amd64"})


def test_pin_image_records_the_id_of_a_local_image_found_by_digest_and_never_pulls(root):
    from aiep import image_digests as idg
    rec = digest_record(root)
    c = SimpleNamespace(images=Images2({idg.reference(rec): IMAGE}))
    assert ts.pin_image(c, root, INSTANCE, free_bytes=16 * GIB) == IMAGE
    assert tp.evaluator_image_pin(root, INSTANCE)["image_id"] == IMAGE and c.images.calls == [("get", idg.reference(rec))]
    assert ts.pin_image(c, root, INSTANCE, free_bytes=16 * GIB) == IMAGE  # idempotent


def test_pin_image_refuses_a_missing_image_a_low_disk_and_a_changed_id(root):
    from aiep import image_digests as idg
    rec = digest_record(root)
    with pytest.raises(SetupRefused, match="not available locally"):
        ts.pin_image(SimpleNamespace(images=Images2({})), root, INSTANCE, free_bytes=16 * GIB)
    gate = SimpleNamespace(images=Images2({idg.reference(rec): IMAGE}))
    with pytest.raises(SetupRefused, match="16 GiB"):
        ts.pin_image(gate, root, INSTANCE, free_bytes=15 * GIB)
    assert gate.images.calls == []
    ts.pin_image(gate, root, INSTANCE, free_bytes=16 * GIB)
    wrong = SimpleNamespace(images=Images2({idg.reference(rec): "sha256:" + "e" * 64}))
    with pytest.raises(idg.DigestError, match="image ID"):
        ts.pin_image(wrong, root, INSTANCE, free_bytes=16 * GIB)


# ---------------------------------------------------------------- verify_and_pin_identity
class IdentityClient:
    """A fake daemon for the identity probe. The inspect it returns is the one the policy expects for the create kwargs it was given."""

    def __init__(self, stdout=PASSWD, image_id=IMAGE, remove_error=None, inspect_edit=None):
        self.container = FakeContainer(remove_error=remove_error)
        self.created, self.stdout, self.inspect_edit = [], stdout, inspect_edit
        outer = self
        self.api = FakeExecApi(lambda argv, rec, stdin: ExecResult(stdout=outer.stdout))
        self.images = Images({IMAGE: image_id})

        class Containers:
            def create(self, image, command=None, **kw):
                outer.created.append({"image": image, "command": command, **kw})
                from aiep.agent_runner import AgentRunSpec
                # the policy-conforming inspect of exactly this container
                spec = outer.spec
                insp = good_inspect(spec)
                insp["HostConfig"]["NetworkMode"] = "none"
                insp["NetworkSettings"]["Networks"] = {"none": {"NetworkID": "n", "IPAddress": "", "GlobalIPv6Address": "", "Gateway": ""}}
                if outer.inspect_edit:
                    outer.inspect_edit(insp)
                outer.api.inspect = insp
                return outer.container

        self.containers = Containers()
        self.spec = None
        self.close_called = 0

    def close(self):
        self.close_called += 1


def identity_spec(root):
    """The spec verify_and_pin_identity builds, rebuilt here from the same inputs so the fake can answer inspect for it."""
    from aiep.agent_env import load_agent_config
    from aiep.agent_runner import AgentRunSpec
    from aiep.seed import build_component_archive, load_seed
    cand = load_agent_config(root / "config" / "agent_identity.json")
    seed = load_seed(root / ".cache" / "seeds" / INSTANCE, expected_sha256=tp.seed_pin(root, INSTANCE)["sha256"])
    exp = json.loads((root / "config" / "experiment.json").read_text())
    return AgentRunSpec(run_id=f"identity-{INSTANCE.split('__')[1]}"[:60], image=IMAGE, gateway_url="http://gateway.invalid:8080", agent_network="none", pinned_model=exp["model"],
                        run_token="identity-check-token", seed=seed, components=build_component_archive({"unused": (b"", 0o755)}), python=cand.verification_python,
                        identity=cand.identity, config_label="A", task_text="identity-check", image_id=IMAGE)


def make_client(root, **kw):
    c = IdentityClient(**kw)
    c.spec = identity_spec(root)
    return c


def test_identity_is_recorded_when_the_probe_matches_and_the_container_is_removed(root):
    pin_everything_but_identity(root)
    c = make_client(root)
    out = ts.verify_and_pin_identity(c, root, INSTANCE, free_bytes=16 * GIB)
    assert out["uid"] == 1000 and out["name"] == "nonroot"
    assert any("remove(force=True" in x for x in c.container.calls) and "start" in c.container.calls
    (kw,) = c.created
    assert kw["network"] == "none" and kw["cap_drop"] == ["ALL"] and kw["image"] == IMAGE
    assert tp.pin_status(root, INSTANCE)["identity_verified"] is True
    assert json.loads(Path(out["identity_file"]).read_text())["verified_image_id"] == IMAGE


def test_identity_probe_mismatch_writes_nothing_but_still_removes_the_container(root):
    pin_everything_but_identity(root)
    c = make_client(root, stdout=b"0\n0\nroot\nroot:x:0:0::/root:/bin/bash\n")
    with pytest.raises(SetupRefused, match="not the configured one"):
        ts.verify_and_pin_identity(c, root, INSTANCE, free_bytes=16 * GIB)
    assert any("remove(force=True" in x for x in c.container.calls)
    assert not tp.identity_path(root, INSTANCE).exists()


def test_container_that_cannot_be_removed_is_an_error_and_nothing_is_recorded(root):
    pin_everything_but_identity(root)
    c = make_client(root, remove_error=RuntimeError("busy"))
    with pytest.raises(SetupRefused, match="could not be removed"):
        ts.verify_and_pin_identity(c, root, INSTANCE, free_bytes=16 * GIB)
    assert not tp.identity_path(root, INSTANCE).exists()


def test_wrong_local_image_id_refuses_before_any_container_is_created(root):
    pin_everything_but_identity(root)
    c = make_client(root, image_id="sha256:" + "f" * 64)
    with pytest.raises(SetupRefused, match="pinned ID"):
        ts.verify_and_pin_identity(c, root, INSTANCE, free_bytes=16 * GIB)
    assert c.created == []


def test_identity_needs_image_and_seed_pins_and_the_disk_gate(root):
    c = make_client_unpinned = SimpleNamespace(created=[])
    with pytest.raises(SetupRefused, match="image pin and seed pin"):
        ts.verify_and_pin_identity(c, root, INSTANCE, free_bytes=16 * GIB)
    pin_everything_but_identity(root)
    with pytest.raises(SetupRefused, match="16 GiB"):
        ts.verify_and_pin_identity(make_client(root), root, INSTANCE, free_bytes=15 * GIB)


def test_a_policy_violating_container_is_refused_before_it_starts(root):
    from aiep.container_policy import PolicyViolation
    pin_everything_but_identity(root)
    c = make_client(root, inspect_edit=lambda i: i["HostConfig"].update(Privileged=True))
    with pytest.raises(PolicyViolation):
        ts.verify_and_pin_identity(c, root, INSTANCE, free_bytes=16 * GIB)
    assert "start" not in c.container.calls and any("remove(force=True" in x for x in c.container.calls)


# ---------------------------------------------------------------- the scripts
def script_client(**attrs):
    c = SimpleNamespace(closed=[], **attrs)
    c.close = lambda: c.closed.append(1)
    return c


def test_pull_script_needs_the_approval_flag_and_the_start_gate_before_any_client_is_built(root):
    s = load_script("pull_task_image")
    built = []
    assert s.main([INSTANCE], client_factory=lambda: built.append(1), free_bytes=30 * GIB, root=root) == 2
    assert s.main([INSTANCE, "--yes"], client_factory=lambda: built.append(1), free_bytes=30 * GIB, root=root) == 2
    assert s.main([INSTANCE, "--i-approve-this-download"], client_factory=lambda: built.append(1), free_bytes=15 * GIB, root=root) == 2
    assert built == []


def test_pull_script_refuses_without_a_record_and_when_the_registry_disagrees_before_any_client_is_built(root):
    from aiep import image_digests as idg
    s = load_script("pull_task_image")
    built = []
    assert s.main([INSTANCE, "--i-approve-this-download"], client_factory=lambda: built.append(1), free_bytes=16 * GIB, root=root, fetch=lambda *a: (_ for _ in ()).throw(AssertionError)) == 2  # no record
    digest_record(root)
    assert s.main([INSTANCE, "--i-approve-this-download"], client_factory=lambda: built.append(1), free_bytes=16 * GIB, root=root, fetch=lambda *a: (_ for _ in ()).throw(LookupError("404"))) == 2
    assert built == []


def test_pull_script_pulls_by_the_verified_index_digest_for_linux_amd64_once(root, capsys, monkeypatch):
    from aiep import image_digests as idg
    s = load_script("pull_task_image")
    rec = digest_record(root)
    monkeypatch.setattr(idg, "verify_record_against_registry", lambda r, f: None)  # the registry checks are covered in tests/test_image_digests.py
    pulls = []

    class Imgs(Images2):
        def pull(self, repository, tag=None, **kw):
            pulls.append((repository, tag, kw))
            self.have[repository] = IMAGE
            return SimpleNamespace(id=IMAGE, attrs={"Os": "linux", "Architecture": "amd64"})
    c = script_client(images=Imgs({}))
    assert s.main([INSTANCE, "--i-approve-this-download"], client_factory=lambda: c, free_bytes=16 * GIB, root=root) == 0
    assert pulls == [(idg.reference(rec), None, {"platform": "linux/amd64"})] and IMAGE in capsys.readouterr().out and c.closed == [1]
    c2 = script_client(images=Imgs({idg.reference(rec): IMAGE}))
    assert s.main([INSTANCE, "--i-approve-this-download"], client_factory=lambda: c2, free_bytes=16 * GIB, root=root) == 0 and len(pulls) == 1  # already local: not pulled again


def test_pin_script_exit_codes(root, monkeypatch, capsys):
    from aiep import image_digests as idg
    s = load_script("pin_task_image")
    monkeypatch.setattr(s, "ROOT", root)
    built = []
    assert s.main([], client_factory=lambda: built.append(1), free_bytes=30 * GIB) == 2
    assert s.main([INSTANCE], client_factory=lambda: built.append(1), free_bytes=15 * GIB) == 2 and built == []
    rec = digest_record(root)
    assert s.main([INSTANCE], client_factory=lambda: script_client(images=Images2({})), free_bytes=16 * GIB) == 1
    assert s.main([INSTANCE], client_factory=lambda: script_client(images=Images2({idg.reference(rec): IMAGE})), free_bytes=16 * GIB) == 0
    assert IMAGE in capsys.readouterr().out and tp.evaluator_image_pin(root, INSTANCE)["image_id"] == IMAGE


def test_identity_script_exit_codes(root, monkeypatch, capsys):
    s = load_script("verify_task_identity")
    monkeypatch.setattr(s, "ROOT", root)
    built = []
    assert s.main([], client_factory=lambda: built.append(1), free_bytes=30 * GIB) == 2
    assert s.main([INSTANCE], client_factory=lambda: built.append(1), free_bytes=15 * GIB) == 2 and built == []
    c = script_client()
    assert s.main([INSTANCE], client_factory=lambda: c, free_bytes=16 * GIB) == 1  # no pins yet: refused, client closed
    assert c.closed == [1]
    pin_everything_but_identity(root)
    good = make_client(root)
    good.close = lambda: None
    assert s.main([INSTANCE], client_factory=lambda: good, free_bytes=16 * GIB) == 0
    assert json.loads(capsys.readouterr().out)["uid"] == 1000

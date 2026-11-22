"""Digest-pinned image preparation: the digest lock, registry verification, pull by immutable index digest, local identity, and the preparation path.
Mocks only: no network, no Docker daemon, no pull."""
import copy
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiep import image_digests as idg
from aiep import m3_steps
from aiep import task_pins as tp
from aiep import task_setup as ts
from aiep.image_digests import DigestError
from aiep.m3_steps import StepError, prepare_pinned_task

REAL = Path(__file__).resolve().parents[1]
GIB = 1024**3
INSTANCE = "demo__demo-1"
REPO = "swebench/sweb.eval.x86_64.demo_1776_demo-1"
OCI_INDEX, OCI_MANIFEST = "application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json"
sha = lambda b: "sha256:" + hashlib.sha256(b).hexdigest()


class ImageNotFound(Exception):  # docker.errors.ImageNotFound, matched by name
    pass


def build(arch="amd64", os_="linux", amd64_entries=1, tamper_manifest_body=False):
    layers = [(sha(f"layer{i}".encode()), 1000 + i) for i in range(3)]
    cfg = {"digest": sha(b"config"), "size": 10}
    manifest = {"schemaVersion": 2, "mediaType": OCI_MANIFEST, "config": {"mediaType": "application/vnd.oci.image.config.v1+json", **cfg},
                "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "digest": d, "size": s} for d, s in layers]}
    mbody = json.dumps(manifest).encode()
    mdig = sha(mbody)
    entries = [{"mediaType": OCI_MANIFEST, "digest": mdig, "size": len(mbody), "platform": {"architecture": arch, "os": os_}},
               {"mediaType": OCI_MANIFEST, "digest": sha(b"attestation"), "size": 5, "platform": {"architecture": "unknown", "os": "unknown"}}]
    for i in range(amd64_entries - 1):
        entries.append({"mediaType": OCI_MANIFEST, "digest": sha(f"dup{i}".encode()), "size": 5, "platform": {"architecture": "amd64", "os": "linux"}})
    ibody = json.dumps({"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": entries}).encode()
    idig = sha(ibody)
    record = {"repository": REPO, "index_digest": idig, "index_media_type": OCI_INDEX, "platform": "linux/amd64", "platform_manifest_digest": mdig,
              "platform_manifest_media_type": OCI_MANIFEST, "config_digest": cfg["digest"], "config_size": cfg["size"],
              "layers": [{"digest": d, "size": s} for d, s in layers], "compressed_layer_bytes": sum(s for _, s in layers),
              "provenance": {"source": "test registry", "verified_on": "2026-11-22", "checks": ["synthetic"]}}
    served = {idig: ({"docker-content-digest": idig}, ibody), mdig: ({"docker-content-digest": mdig}, mbody + (b" " if tamper_manifest_body else b""))}
    return record, served


class Fetch:
    def __init__(self, served):
        self.served, self.calls = served, []

    def __call__(self, repository, reference, accept):
        self.calls.append((repository, reference))
        if repository != REPO or reference not in self.served:
            raise LookupError("404")
        return self.served[reference]


def lock_root(tmp_path, record=None, instance=INSTANCE):
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "image_digests.json").write_text(json.dumps({"schema": 1, "platform": "linux/amd64", "note": "t", "images": {instance: record} if record else {}}))
    (tmp_path / "config" / "evaluator_images.json").write_text(json.dumps({"images": {}}))
    return tmp_path


# ---------------------------------------------------------------- the lock file
def test_the_shipped_lock_has_exactly_the_verified_record_for_sympy_18621_and_nothing_else():
    doc = json.loads((REAL / "config" / "image_digests.json").read_text())
    assert list(doc["images"]) == ["sympy__sympy-18621"] and doc["platform"] == "linux/amd64"
    r = idg.load_digest_record(REAL, "sympy__sympy-18621")
    assert r["repository"] == "swebench/sweb.eval.x86_64.sympy_1776_sympy-18621"
    assert r["index_digest"] == "sha256:6ceb7424d50163dbea48b63200bb124317e530dd314cb29e1d98d8cafdc1b411"
    assert r["platform_manifest_digest"] == "sha256:1cbb5f16f284d7b1008e62f95484378e86e0f92e14b66c4ed612d6047a3b1c0d"
    assert r["compressed_layer_bytes"] == 1_084_534_866 == sum(l["size"] for l in r["layers"]) and len(r["layers"]) == 10
    assert r["provenance"]["verified_on"] == "2026-11-22" and r["provenance"]["source"]
    assert idg.reference(r) == "swebench/sweb.eval.x86_64.sympy_1776_sympy-18621@" + r["index_digest"] and not re.search(r"sympy-18621:", json.dumps(doc))  # no tagged reference anywhere in the lock


def test_the_hashed_definition_and_prompts_were_not_touched_by_the_lock():
    assert "image_digests" not in (REAL / "config" / "experiments" / "m3-pilot-v2.json").read_text()


def test_valid_record_loads(tmp_path):
    record, _ = build()
    assert idg.load_digest_record(lock_root(tmp_path, record), INSTANCE)["index_digest"] == record["index_digest"]


def test_missing_record_fails_closed_without_inventing_one(tmp_path):
    with pytest.raises(DigestError, match="no digest record"):
        idg.load_digest_record(lock_root(tmp_path), INSTANCE)
    with pytest.raises(DigestError, match="no digest record"):
        idg.load_digest_record(REAL, "sympy__sympy-22714")  # a pilot task that has not been locked


@pytest.mark.parametrize("mut,needle", [
    (lambda r: r.update(index_digest="sha256:abc"), "index_digest"),
    (lambda r: r.update(index_digest="latest"), "index_digest"),
    (lambda r: r.update(platform_manifest_digest="x"), "platform_manifest_digest"),
    (lambda r: r.update(platform="linux/arm64"), "platform"),
    (lambda r: r.update(repository="swebench/other"), "repository"),
    (lambda r: r.update(repository=REPO + ":latest"), "repository"),
    (lambda r: r.update(compressed_layer_bytes=1), "compressed_layer_bytes"),
    (lambda r: r.update(layers=[]), "layers"),
    (lambda r: r["layers"][0].update(size=-1), "layers"),
    (lambda r: r["layers"][0].update(digest="nope"), "layers"),
    (lambda r: r.update(extra="field"), "unexpected"),
    (lambda r: r.pop("config_digest"), "config_digest"),
    (lambda r: r["provenance"].pop("verified_on"), "provenance"),
    (lambda r: r.update(provenance="x"), "provenance"),
])
def test_malformed_records_fail_closed(tmp_path, mut, needle):
    record, _ = build()
    mut(record)
    with pytest.raises(DigestError, match=needle):
        idg.load_digest_record(lock_root(tmp_path, record), INSTANCE)


def test_malformed_files_fail_closed(tmp_path):
    (tmp_path / "config").mkdir()
    for text in ("not json", "[]", json.dumps({"schema": 2, "platform": "linux/amd64", "images": {}}), json.dumps({"schema": 1, "platform": "linux/arm64", "images": {}})):
        (tmp_path / "config" / "image_digests.json").write_text(text)
        with pytest.raises(DigestError):
            idg.load_digest_record(tmp_path, INSTANCE)
    with pytest.raises(DigestError):
        idg.load_digest_record(tmp_path / "nowhere", INSTANCE)


# ---------------------------------------------------------------- registry verification
def test_valid_index_and_platform_manifest_verify_and_only_those_two_are_fetched():
    record, served = build()
    f = Fetch(served)
    idg.verify_record_against_registry(record, f)
    assert f.calls == [(REPO, record["index_digest"]), (REPO, record["platform_manifest_digest"])]  # by digest, never by tag


def test_index_body_that_does_not_hash_to_the_digest_is_rejected():
    record, served = build()
    h, b = served[record["index_digest"]]
    served[record["index_digest"]] = (h, b + b" ")
    with pytest.raises(DigestError, match="index body"):
        idg.verify_record_against_registry(record, Fetch(served))


def test_digest_header_that_disagrees_is_rejected_and_a_missing_header_too():
    record, served = build()
    h, b = served[record["index_digest"]]
    for hdr in ({"docker-content-digest": sha(b"other")}, {}):
        served2 = dict(served, **{record["index_digest"]: (hdr, b)})
        with pytest.raises(DigestError, match="header"):
            idg.verify_record_against_registry(record, Fetch(served2))


def test_platform_manifest_that_is_not_in_the_index_is_rejected():
    record, served = build()
    other, _ = build()
    record2 = dict(record, platform_manifest_digest=sha(b"unrelated"))
    served[record2["platform_manifest_digest"]] = ({"docker-content-digest": record2["platform_manifest_digest"]}, b"{}")
    with pytest.raises(DigestError, match="not .*member|not in the index"):
        idg.verify_record_against_registry(record2, Fetch(served))


@pytest.mark.parametrize("arch,os_", [("arm64", "linux"), ("amd64", "windows")])
def test_wrong_platform_is_rejected(arch, os_):
    record, served = build(arch=arch, os_=os_)
    with pytest.raises(DigestError, match="linux/amd64"):
        idg.verify_record_against_registry(record, Fetch(served))


def test_two_amd64_entries_are_ambiguous_and_rejected():
    record, served = build(amd64_entries=2)
    with pytest.raises(DigestError, match="exactly one"):
        idg.verify_record_against_registry(record, Fetch(served))


def test_platform_manifest_body_that_does_not_hash_is_rejected():
    record, served = build(tamper_manifest_body=True)
    with pytest.raises(DigestError, match="platform manifest body"):
        idg.verify_record_against_registry(record, Fetch(served))


@pytest.mark.parametrize("mut", [lambda r: r["layers"][0].update(size=1), lambda r: r.update(config_digest=sha(b"x")), lambda r: r["layers"].pop()])
def test_layers_or_config_that_differ_from_the_lock_are_rejected(mut):
    record, served = build()
    mut(record)
    record["compressed_layer_bytes"] = sum(l["size"] for l in record["layers"])
    with pytest.raises(DigestError, match="layers|config"):
        idg.verify_record_against_registry(record, Fetch(served))


def test_registry_errors_fail_closed():
    record, _ = build()
    with pytest.raises(DigestError, match="could not be fetched"):
        idg.verify_record_against_registry(record, Fetch({}))


# ---------------------------------------------------------------- the Docker side
class Client:
    def __init__(self, local=None, pull_id=None, platform=("linux", "amd64")):
        self.calls, self.local, self.pull_id, self.platform = [], dict(local or {}), pull_id, platform
        outer = self

        class Images:
            def get(self, ref):
                outer.calls.append(("get", ref))
                if ref not in outer.local:
                    raise ImageNotFound(ref)
                return outer.local[ref]

            def pull(self, repository, tag=None, **kw):
                outer.calls.append(("pull", repository, tag, kw))
                img = outer.image(outer.pull_id)
                outer.local[repository] = img
                return img
        self.images = Images()

    def image(self, iid):
        return SimpleNamespace(id=iid, attrs={"Os": self.platform[0], "Architecture": self.platform[1]})

    def close(self):
        self.calls.append("close")


def test_pull_uses_the_immutable_index_digest_and_linux_amd64_and_never_a_tag():
    record, _ = build()
    c = Client(pull_id=record["index_digest"])
    img = idg.pull_by_digest(c, record)
    (call,) = [x for x in c.calls if x[0] == "pull"]
    assert call[1] == idg.reference(record) == f"{REPO}@{record['index_digest']}" and call[2] is None and call[3] == {"platform": "linux/amd64"}
    assert img.id == record["index_digest"] and "latest" not in json.dumps([str(x) for x in c.calls])


def test_pulled_image_with_a_different_id_is_rejected():
    record, _ = build()
    with pytest.raises(DigestError, match="image ID"):
        idg.pull_by_digest(Client(pull_id=sha(b"someone else")), record)


def test_pulled_image_for_another_platform_is_rejected():
    record, _ = build()
    with pytest.raises(DigestError, match="linux/amd64"):
        idg.pull_by_digest(Client(pull_id=record["index_digest"], platform=("linux", "arm64")), record)


def test_local_lookup_is_by_digest_reference_and_a_wrong_local_id_fails_closed():
    record, _ = build()
    ref = idg.reference(record)
    assert idg.local_image(Client(), record) is None and idg.image_present(Client(), record) is False
    good = Client()
    good.local[ref] = good.image(record["index_digest"])
    assert idg.image_present(good, record) is True and good.calls == [("get", ref)]
    bad = Client()
    bad.local[ref] = bad.image(sha(b"tampered"))
    with pytest.raises(DigestError, match="image ID"):
        idg.image_present(bad, record)


def test_other_daemon_errors_are_not_read_as_absent():
    record, _ = build()

    class Broken:
        class images:
            @staticmethod
            def get(ref):
                raise ConnectionError("daemon down")
    with pytest.raises(ConnectionError):
        idg.local_image(Broken, record)


def test_pin_image_looks_up_by_the_verified_identity_and_records_the_reference(tmp_path):
    record, _ = build()
    root = lock_root(tmp_path, record)
    c = Client()
    c.local[idg.reference(record)] = c.image(record["index_digest"])
    assert ts.pin_image(c, root, INSTANCE, free_bytes=16 * GIB) == record["index_digest"]
    assert c.calls == [("get", idg.reference(record))]
    pin = tp.evaluator_image_pin(root, INSTANCE)
    assert pin["image_id"] == record["index_digest"] and pin["digest_reference"] == idg.reference(record)


def test_pin_image_refuses_without_a_record_with_an_absent_image_and_with_a_wrong_id(tmp_path):
    record, _ = build()
    no_record = lock_root(tmp_path / "none")
    with pytest.raises(DigestError, match="no digest record"):
        ts.pin_image(Client(), no_record, INSTANCE, free_bytes=16 * GIB)
    root = lock_root(tmp_path / "with", record)
    with pytest.raises(ts.SetupRefused, match="not available locally"):
        ts.pin_image(Client(), root, INSTANCE, free_bytes=16 * GIB)
    bad = Client()
    bad.local[idg.reference(record)] = bad.image(sha(b"x"))
    with pytest.raises(DigestError, match="image ID"):
        ts.pin_image(bad, root, INSTANCE, free_bytes=16 * GIB)
    assert tp.evaluator_image_pin(root, INSTANCE) is None


# ---------------------------------------------------------------- the preparation path
def prep(tmp_path, *, record=None, local=False, free=30.0, allow_pull=True, pull_id="same", fetch_ok=True, platform=("linux", "amd64")):
    record = record or build()[0]
    root = lock_root(tmp_path, record)
    c = Client(pull_id=record["index_digest"] if pull_id == "same" else pull_id, platform=platform)
    if local:
        c.local[idg.reference(record)] = c.image(record["index_digest"])
    log = []

    def verify(rec):
        log.append("verify_registry")
        if not fetch_ok:
            raise DigestError("registry mismatch")

    def pull(client, rec):
        log.append("pull")
        idg.pull_by_digest(client, rec)
    kw = dict(root=root, task_id=INSTANCE, client_factory=lambda: c, free_gib=lambda: free, digest_record=lambda t: idg.load_digest_record(root, t), verify_registry=verify,
              image_present=idg.image_present, pull_image=pull, pin_image=ts.pin_image, extract_seed=lambda *a, **k: log.append("seed"),
              verify_identity=lambda *a, **k: log.append("identity"), allow_pull=allow_pull)
    return kw, c, log, root, record


def runnable(monkeypatch):
    seq = {"n": 0}

    def status(root, t):
        pinned = tp.evaluator_image_pin(root, t) is not None
        return {"runnable": pinned, "seed_pinned": False, "identity_verified": False, "missing": []}
    monkeypatch.setattr(m3_steps, "pin_status", status)


def test_prepare_verifies_the_registry_then_pulls_by_digest_then_pins_by_identity(tmp_path, monkeypatch):
    runnable(monkeypatch)
    kw, c, log, root, record = prep(tmp_path)
    out = prepare_pinned_task(**kw)
    assert log == ["verify_registry", "pull", "seed", "identity"] and out["pulled"] is True
    refs = [x[1] for x in c.calls if isinstance(x, tuple) and x[0] in ("get", "pull")]
    assert refs and all(r == idg.reference(record) for r in refs), refs  # EVERY reference the preparation used is the digest reference
    assert tp.evaluator_image_pin(root, INSTANCE)["image_id"] == record["index_digest"]
    assert "latest" not in json.dumps([str(x) for x in c.calls])


def test_a_local_image_with_the_right_id_is_not_pulled_and_the_registry_is_not_contacted(tmp_path, monkeypatch):
    runnable(monkeypatch)
    kw, c, log, root, record = prep(tmp_path, local=True)
    out = prepare_pinned_task(**kw)
    assert out["pulled"] is False and "pull" not in log and "verify_registry" not in log


def test_registry_mismatch_stops_before_any_pull(tmp_path, monkeypatch):
    runnable(monkeypatch)
    kw, c, log, root, _ = prep(tmp_path, fetch_ok=False)
    with pytest.raises(DigestError):
        prepare_pinned_task(**kw)
    assert "pull" not in log and not any(isinstance(x, tuple) and x[0] == "pull" for x in c.calls) and tp.evaluator_image_pin(root, INSTANCE) is None


def test_missing_record_is_a_step_error_before_any_docker_call(tmp_path, monkeypatch):
    runnable(monkeypatch)
    kw, c, log, root, _ = prep(tmp_path)
    empty = lock_root(tmp_path / "empty")
    kw["digest_record"] = lambda t: idg.load_digest_record(empty, t)
    with pytest.raises(StepError, match="no digest record"):
        prepare_pinned_task(**kw)
    assert c.calls == [] and log == []


@pytest.mark.parametrize("over,match", [({"allow_pull": False}, "not approved"), ({"free": 15.0}, "16 GiB")])
def test_pull_needs_approval_and_the_start_gate_and_never_falls_back_to_a_tag(tmp_path, monkeypatch, over, match):
    runnable(monkeypatch)
    kw, c, log, root, _ = prep(tmp_path, **over)
    with pytest.raises(StepError, match=match):
        prepare_pinned_task(**kw)
    assert "pull" not in log and not any(isinstance(x, tuple) and x[0] == "pull" for x in c.calls)


def test_a_pull_that_yields_a_different_image_id_stops_and_pins_nothing(tmp_path, monkeypatch):
    runnable(monkeypatch)
    kw, c, log, root, _ = prep(tmp_path, pull_id=sha(b"not the index"))
    with pytest.raises(DigestError, match="image ID"):
        prepare_pinned_task(**kw)
    assert tp.evaluator_image_pin(root, INSTANCE) is None and "seed" not in log


# ---------------------------------------------------------------- no code path falls back to :latest for preparation
def _code(rel):
    """Source of a module without docstrings and comments."""
    import ast
    tree = ast.parse((REAL / rel).read_text())
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant):
            n.body[0].value.value = ""
    return ast.unparse(tree)


@pytest.mark.parametrize("rel", ["src/aiep/image_digests.py", "src/aiep/task_setup.py", "src/aiep/m3_steps.py", "scripts/pull_task_image.py", "scripts/run_m3_pilot.py", "scripts/pin_task_image.py"])
def test_preparation_code_has_no_tag_based_pull_or_lookup(rel):
    code = _code(rel)
    assert "image_name" not in code or rel in ("src/aiep/image_digests.py",), rel  # only the repository derivation uses it; no preparation module resolves a tag
    assert "tag=" not in code and ":latest" not in code and "'latest'" not in code, rel
    assert "partition(':')" not in code or rel == "src/aiep/image_digests.py", rel  # splitting a tag off a name is only the repository derivation


def test_the_only_image_name_use_in_the_digest_module_is_the_repository_derivation():
    code = _code("src/aiep/image_digests.py")
    assert code.count("image_name(") == 1 and "image_name(instance_id).partition(':')[0]" in code

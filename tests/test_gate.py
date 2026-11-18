import json
import os
import shutil
from pathlib import Path

import pytest

from aiep import gate
from aiep.gate import GateError

REAL = Path(__file__).resolve().parents[1]
GOOD_PROBE = {"network_blocked": True, "wheelhouse_install_ok": True}


def make_repo(tmp_path, populated=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "repo"
    (root / "src" / "aiep").mkdir(parents=True)
    (root / "config").mkdir()
    for name in ("evaluator.py", "container_policy.py", "gate.py"):
        shutil.copy(REAL / "src" / "aiep" / name, root / "src" / "aiep" / name)
    shutil.copy(REAL / "config" / "evaluator_images.json", root / "config")
    wh = {"schema": 1, "status": "populated" if populated else "unpopulated",
          "wheels": [{"name": "setuptools", "version": "1", "sha256": "a" * 64}] if populated else []}
    (root / "config" / "wheelhouse.json").write_text(json.dumps(wh))
    return root


def make_key(tmp_path, mode=0o600, size=32):
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / "key"
    p.write_bytes(os.urandom(size))
    p.chmod(mode)
    return p


def attest(root, keyfile, tmp_path, probe=GOOD_PROBE):
    att = gate.build_attestation(gate.current_components(root), probe, gate.load_key(keyfile))
    out = tmp_path / "att.json"
    out.write_text(json.dumps(att))
    return out


def test_repo_wheelhouse_is_unpopulated_so_real_gate_cannot_open():
    with pytest.raises(GateError, match="not populated"):
        gate.current_components(REAL)


def test_valid_attestation_opens_gate(tmp_path):
    root, key = make_repo(tmp_path), make_key(tmp_path)
    att = attest(root, key, tmp_path)
    assert gate.require_gate(att, key, root)["probe"] == GOOD_PROBE


@pytest.mark.parametrize(
    "target",
    [
        "src/aiep/evaluator.py",
        "src/aiep/container_policy.py",
        "src/aiep/gate.py",
    ],
)
def test_editing_wrapper_policy_or_gate_invalidates_attestation(tmp_path, target):
    root, key = make_repo(tmp_path), make_key(tmp_path)
    att = attest(root, key, tmp_path)
    with open(root / target, "a") as f:
        f.write("\n# one more byte\n")
    with pytest.raises(GateError, match="does not match"):
        gate.require_gate(att, key, root)


def test_changing_wheelhouse_or_image_config_invalidates_attestation(tmp_path):
    root, key = make_repo(tmp_path), make_key(tmp_path)
    att = attest(root, key, tmp_path)
    wh = json.loads((root / "config" / "wheelhouse.json").read_text())
    wh["wheels"][0]["sha256"] = "b" * 64
    (root / "config" / "wheelhouse.json").write_text(json.dumps(wh))
    with pytest.raises(GateError, match="wheelhouse_sha256"):
        gate.require_gate(att, key, root)

    root2, key2 = make_repo(tmp_path / "b"), make_key(tmp_path / "b")
    att2 = attest(root2, key2, tmp_path / "b")
    imgs = json.loads((root2 / "config" / "evaluator_images.json").read_text())
    next(iter(imgs["images"].values()))["image_id"] = "sha256:" + "0" * 64
    (root2 / "config" / "evaluator_images.json").write_text(json.dumps(imgs))
    with pytest.raises(GateError, match="image"):
        gate.require_gate(att2, key2, root2)


def test_json_whitespace_and_key_order_do_not_matter(tmp_path):
    root, key = make_repo(tmp_path), make_key(tmp_path)
    att = attest(root, key, tmp_path)
    wh = json.loads((root / "config" / "wheelhouse.json").read_text())
    (root / "config" / "wheelhouse.json").write_text(json.dumps(wh, indent=4, sort_keys=False))
    gate.require_gate(att, key, root)


def test_tampered_components_probe_or_mac_rejected(tmp_path):
    root, key = make_repo(tmp_path), make_key(tmp_path)
    att_path = attest(root, key, tmp_path)
    good = json.loads(att_path.read_text())

    for mutate in (
        lambda a: a["components"].__setitem__("network_mode", "bridge"),
        lambda a: a["probe"].__setitem__("note", "edited"),
        lambda a: a.__setitem__("mac", "0" * 64),
    ):
        bad = json.loads(json.dumps(good))
        mutate(bad)
        att_path.write_text(json.dumps(bad))
        with pytest.raises(GateError, match="MAC"):
            gate.require_gate(att_path, key, root)


def test_attestation_signed_with_other_key_rejected(tmp_path):
    root = make_repo(tmp_path)
    k1, k2 = make_key(tmp_path), make_key(tmp_path / "other")
    att = attest(root, k1, tmp_path)
    with pytest.raises(GateError, match="MAC"):
        gate.require_gate(att, k2, root)


@pytest.mark.parametrize("probe", [
    {"network_blocked": False, "wheelhouse_install_ok": True},
    {"network_blocked": True, "wheelhouse_install_ok": False},
    {"network_blocked": True},
    {},
])
def test_failed_probe_cannot_be_attested(tmp_path, probe):
    root, key = make_repo(tmp_path), make_key(tmp_path)
    with pytest.raises(GateError, match="probe"):
        gate.build_attestation(gate.current_components(root), probe, gate.load_key(key))


def test_key_file_must_be_private_and_long_enough(tmp_path):
    with pytest.raises(GateError, match="chmod 600"):
        gate.load_key(make_key(tmp_path, mode=0o644))
    with pytest.raises(GateError, match="32 bytes"):
        gate.load_key(make_key(tmp_path / "s", size=8))
    with pytest.raises(GateError, match="not readable"):
        gate.load_key(tmp_path / "missing")


def test_missing_or_garbage_attestation_file_refused(tmp_path):
    root, key = make_repo(tmp_path), make_key(tmp_path)
    with pytest.raises(GateError, match="not readable"):
        gate.require_gate(tmp_path / "none.json", key, root)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(GateError, match="not readable"):
        gate.require_gate(bad, key, root)
    bad.write_text(json.dumps({"schema": 1}))
    with pytest.raises(GateError, match="malformed"):
        gate.require_gate(bad, key, root)


def test_no_environment_or_gold_bypass_exists_in_source():
    src = (REAL / "src" / "aiep" / "gate.py").read_text() + (REAL / "src" / "aiep" / "evaluator.py").read_text()
    for needle in ("SKIP_GATE", "BYPASS", "NO_ATTEST", "allow_gold", "if args.predictions == \"gold\""):
        assert needle not in src


# ---------------- no bypass: environment variables and CLI surface ----------------
def _env_refs(path):
    import re

    text = (REAL / "src" / "aiep" / path).read_text()
    return set(re.findall(r'environ(?:\.get|\.update|\.setdefault)?\(?\[?\s*["\']([A-Z_]+)["\']', text))


def test_gate_reads_exactly_one_environment_variable_and_it_is_only_the_key_path():
    assert _env_refs("gate.py") == {"AIEP_ATTEST_KEY_FILE"}
    src = (REAL / "src" / "aiep" / "gate.py").read_text()
    # that variable only locates the HMAC key file; it can never switch verification off
    i = src.index("def key_path_from_env")
    assert "return Path(value)" in src[i:i + 400] and "raise GateError" in src[i:i + 400]


def test_evaluator_sets_only_offline_variables_and_reads_none():
    assert _env_refs("evaluator.py") == {"HF_HUB_OFFLINE", "HF_DATASETS_OFFLINE"}
    src = (REAL / "src" / "aiep" / "evaluator.py").read_text()
    assert "environ.get" not in src and "getenv" not in src


def test_evaluator_cli_exposes_no_way_to_skip_the_gate():
    import argparse
    from unittest import mock

    from aiep import evaluator

    captured = {}
    real_init = argparse.ArgumentParser.add_argument

    def spy(self, *names, **kw):
        captured.setdefault("opts", []).extend(n for n in names if n.startswith("-"))
        return real_init(self, *names, **kw)

    with mock.patch.object(argparse.ArgumentParser, "add_argument", spy):
        evaluator.parse_args(["-i", "x", "-p", "gold", "--run-id", "r", "--attestation", "a"])
    assert set(captured["opts"]) == {"--dataset", "--split", "-i", "--instance", "-p", "--predictions",
                                     "--run-id", "-t", "--timeout", "--report-dir", "--attestation", "-h", "--help"}
    # --attestation is required: argparse must refuse its absence
    with pytest.raises(SystemExit):
        evaluator.parse_args(["-i", "x", "-p", "gold", "--run-id", "r"])


def test_gate_runs_before_the_harness_is_even_imported():
    src = (REAL / "src" / "aiep" / "evaluator.py").read_text()
    main = src[src.index("def main("):]
    assert main.index("require_gate(") < main.index("from swebench") < main.index("guards.install()") or \
        main.index("require_gate(") < main.index("guards.install()") < main.index("from swebench")
    assert main.index("require_gate(") < main.index("guards = Guards(")

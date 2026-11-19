import inspect
import json
import sys
from pathlib import Path

import pytest

from aiep import agent_env as AE
from aiep.agent_env import (EXPERIMENT_PATH, IDENTITY_SCRIPT, TESTBED_PYTHON, AgentConfig, IdentityUnverified,
                            check_home_overlaps, check_identity, experiment_env, load_agent_config, load_agent_identity)
from aiep.container_policy import AgentIdentity, PolicyViolation, validate_interpreter_path

REAL = Path(__file__).resolve().parents[1]
# The account observed in the image by the Docker conformance probe (2026-11-19); config/agent_identity.json must agree
# (asserted below), and `GOOD` is the probe's real output for it.
IDENT = AgentIdentity(name="nonroot", uid=1000, gid=1000, home="/home/nonroot")
GOOD = b"1000\n1000\nnonroot\nnonroot:x:1000:1000:dog,,,:/home/nonroot:/bin/bash\n"
REAL_CFG = REAL / "config" / "agent_identity.json"
VERIFIED_IMAGE_ID = "sha256:c91459f86ed261ab81871b086d7ca2d9fe16abd2ddf374bf172edd5d991fd28b"


# ---------------- the shared environment ----------------
def test_path_reproduces_the_measured_conda_activate_testbed_path():
    # copied from runs/m1/*/logs/.../test_output.txt after `conda activate testbed` (measured in M1)
    assert EXPERIMENT_PATH == ("/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/condabin:/opt/miniconda3/bin:"
                               "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")


def test_experiment_env_puts_the_testbed_env_before_base_miniconda_before_the_system():
    env = experiment_env(IDENT)
    parts = env["PATH"].split(":")
    assert parts.index("/opt/miniconda3/envs/testbed/bin") < parts.index("/opt/miniconda3/bin") < parts.index("/usr/bin")
    assert env["CONDA_PREFIX"] == "/opt/miniconda3/envs/testbed" and env["CONDA_DEFAULT_ENV"] == "testbed"
    assert env["HOME"] == IDENT.home and env["TZ"] == "UTC"


def test_the_environment_depends_on_the_identity_alone_never_on_the_configuration():
    assert list(inspect.signature(experiment_env).parameters) == ["identity"]
    other = AgentIdentity(name="nonroot", uid=1000, gid=1000, home="/home/nonroot")
    assert experiment_env(IDENT) == experiment_env(other)


def test_the_environment_does_not_select_the_verification_interpreter():
    # AgentRunSpec.python is pinned separately and used as an absolute path; nothing here names or overrides it
    assert not any(k.startswith("PYTHON") or k.endswith("_PYTHON_EXE") for k in experiment_env(IDENT))


# ---------------- identity probe ----------------
def test_identity_probe_accepts_exactly_the_configured_account():
    assert check_identity(GOOD, IDENT) == []
    assert check_identity(GOOD.decode(), IDENT) == []


@pytest.mark.parametrize("output, needle", [
    (b"1001\n1000\nnonroot\nnonroot:x:1000:1000::/home/nonroot:/bin/bash\n", "uid is"),
    (b"1000\n1001\nnonroot\nnonroot:x:1000:1000::/home/nonroot:/bin/bash\n", "gid is"),
    (b"1000\n1000\nnonroot\n", "no passwd entry"),            # numeric uid, no passwd entry: NOT assumed sufficient
    (b"1000\n1000\nnonroot\n\n", "no passwd entry"),
    (b"1000\n1000\nother\nother:x:1000:1000::/home/nonroot:/bin/bash\n", "account name"),
    (b"1000\n1000\nnonroot\nnonroot:x:1000:1000::/home/other:/bin/bash\n", "passwd home"),
    (b"1000\n1000\nnonroot\nnonroot:x:1000:1002::/home/nonroot:/bin/bash\n", "passwd uid:gid"),
    (b"1000\n1000\nnonroot\ngarbage\n", "malformed"),
    (b"", "fewer than three"),
])
def test_identity_probe_rejects_every_inconsistency(output, needle):
    assert any(needle in p for p in check_identity(output, IDENT)), check_identity(output, IDENT)


def test_identity_script_is_absolute_paths_only_and_reads_the_passwd_database():
    import re
    assert "getent passwd" in IDENTITY_SCRIPT
    assert re.findall(r"(?<![/\w])(?:id|getent)\b", IDENTITY_SCRIPT) == []  # no bare command name, so no PATH lookup


# ---------------- configuration: the verified identity ----------------
def real_cfg():
    return json.loads(REAL_CFG.read_text())


def write_cfg(tmp_path, mutate=None, raw=None):
    d = real_cfg()
    if mutate:
        mutate(d)
    p = tmp_path / "id.json"
    p.write_text(raw if raw is not None else json.dumps(d))
    return p


def test_the_repository_config_records_the_verified_identity_exactly():
    cfg = real_cfg()
    assert cfg["status"] == "verified" and cfg["schema"] == 1
    assert (cfg["name"], cfg["uid"], cfg["gid"], cfg["home"], cfg["shell"]) == ("nonroot", 1000, 1000, "/home/nonroot", "/bin/bash")
    assert cfg["supplementary_groups"] == []
    assert cfg["verification_python"] == "/opt/miniconda3/bin/python"
    assert cfg["testbed_python"] == "/opt/miniconda3/envs/testbed/bin/python"
    assert cfg["verified_image_id"] == VERIFIED_IMAGE_ID and cfg["verified_on"] == "2026-11-19"
    assert cfg["evidence"] and cfg["still_unverified"]
    text = REAL_CFG.read_text()
    assert 1001 not in (cfg["uid"], cfg["gid"]) and "/home/agent" not in text  # no placeholder identity survives
    assert [e for e in cfg["evidence"] if "1001" in e] == [e for e in cfg["evidence"] if "uid 1001 has no passwd entry" in e]  # only as a negative finding


def test_the_repository_config_loads_and_matches_the_expected_image_and_the_test_identity():
    ev = json.loads((REAL / "config" / "evaluator_images.json").read_text())["images"]["pytest-dev__pytest-5227"]
    assert ev["image_id"] == VERIFIED_IMAGE_ID  # the identity was verified in the very image the evaluator pins
    config = load_agent_config(REAL_CFG, image_id=ev["image_id"])
    assert isinstance(config, AgentConfig)
    assert config.identity == IDENT == load_agent_identity(REAL_CFG, image_id=ev["image_id"])
    assert config.identity.user == "1000:1000" and config.shell == "/bin/bash"
    assert config.verified_image_id == VERIFIED_IMAGE_ID


def test_the_two_interpreters_stay_explicit_distinct_and_unrelated_to_each_other():
    config = load_agent_config(REAL_CFG)
    assert config.verification_python == "/opt/miniconda3/bin/python"  # AIEP's pinned verification interpreter (3.11.5)
    assert config.testbed_python == TESTBED_PYTHON == "/opt/miniconda3/envs/testbed/bin/python"  # the task's (3.9.20)
    assert config.verification_python != config.testbed_python
    validate_interpreter_path(config.verification_python)
    validate_interpreter_path(config.testbed_python)
    check_home_overlaps(config.identity, config.verification_python)  # HOME shadows neither
    check_home_overlaps(config.identity, config.testbed_python)
    parts = experiment_env(config.identity)["PATH"].split(":")
    testbed_dir, verification_dir = config.testbed_python.rsplit("/", 1)[0], config.verification_python.rsplit("/", 1)[0]
    # PATH order: the testbed environment's bin directory is the FIRST entry, so a bare `python` resolves to the testbed
    # interpreter; the verification interpreter's directory (base miniconda bin) is also on PATH but comes later.
    assert parts[0] == testbed_dir == "/opt/miniconda3/envs/testbed/bin"
    assert parts.index(testbed_dir) < parts.index(verification_dir)
    # Which Python verifies is NOT decided by this PATH order: the verification interpreter is always run by its absolute
    # configured path (harden_exec runs only ExecContext.python; see test_exec_runs_only_the_configured_interpreter...).
    assert experiment_env(config.identity)["HOME"] == config.identity.home == "/home/nonroot"


def test_the_probe_output_observed_in_the_image_satisfies_the_run_time_identity_check():
    assert check_identity(GOOD, load_agent_identity(REAL_CFG)) == []
    assert check_identity(b"1000\n1000\nnonroot\n", IDENT) and check_identity(b"1001\n1000\nnonroot\nnonroot:x:1001:1000::/home/nonroot:/bin/bash\n", IDENT)


def test_a_wrong_image_is_refused_when_the_caller_names_the_image_in_use(tmp_path):
    with pytest.raises(IdentityUnverified, match="different image"):
        load_agent_identity(REAL_CFG, image_id="sha256:" + "0" * 64)
    assert load_agent_identity(REAL_CFG, image_id=None) == IDENT  # no image given: no cross-check, nothing inferred


REJECTED = {
    "not verified": lambda d: d.update(status="unverified"),
    "status missing": lambda d: d.pop("status"),
    "status odd": lambda d: d.update(status="Verified"),
    "schema wrong": lambda d: d.update(schema=2),
    "schema missing": lambda d: d.pop("schema"),
    "schema bool": lambda d: d.update(schema=True),
    "unknown key": lambda d: d.update(uid_=1000),
    "unknown key (typo of a required one)": lambda d: (d.pop("gid"), d.update(gidd=1000)),
    **{f"{k} missing": (lambda k: lambda d: d.pop(k))(k) for k in
       ("name", "uid", "gid", "home", "shell", "supplementary_groups", "verification_python", "testbed_python",
        "verified_image_id", "evidence")},
    **{f"{k} null": (lambda k: lambda d: d.update({k: None}))(k) for k in
       ("name", "uid", "gid", "home", "shell", "supplementary_groups", "verification_python", "testbed_python",
        "verified_image_id", "evidence")},
    "uid root": lambda d: d.update(uid=0), "gid root": lambda d: d.update(gid=0),
    "uid negative": lambda d: d.update(uid=-1), "uid bool": lambda d: d.update(uid=True),
    "gid bool": lambda d: d.update(gid=True), "uid string": lambda d: d.update(uid="1000"),
    "gid float": lambda d: d.update(gid=1000.0), "uid list": lambda d: d.update(uid=[1000]),
    "name root": lambda d: d.update(name="root"), "name empty": lambda d: d.update(name=""),
    "name spaces": lambda d: d.update(name="non root"), "name int": lambda d: d.update(name=1000),
    "home root": lambda d: d.update(home="/"), "home relative": lambda d: d.update(home="home/nonroot"),
    "home dotdot": lambda d: d.update(home="/home/x/../y"), "home tmp": lambda d: d.update(home="/tmp"),
    "home in testbed": lambda d: d.update(home="/testbed/home"), "home placeholder-style but protected": lambda d: d.update(home="/opt/aiep"),
    "home shadows PATH dir": lambda d: d.update(home="/usr/local/bin/h"),
    "home contains an interpreter": lambda d: d.update(home="/opt/miniconda3/envs"),
    "home int": lambda d: d.update(home=5),
    "shell relative": lambda d: d.update(shell="bash"), "shell in tmp": lambda d: d.update(shell="/tmp/bash"),
    "shell not a path": lambda d: d.update(shell=["/bin/bash"]),
    "groups non-empty": lambda d: d.update(supplementary_groups=[27]),
    "groups string": lambda d: d.update(supplementary_groups="none"),
    "groups dict": lambda d: d.update(supplementary_groups={}),
    "verification python relative": lambda d: d.update(verification_python="python"),
    "verification python dotdot": lambda d: d.update(verification_python="/opt/miniconda3/bin/../bin/python"),
    "verification python in writable place": lambda d: d.update(verification_python="/home/nonroot/python"),
    "verification python is the testbed python": lambda d: d.update(verification_python=d["testbed_python"]),
    "testbed python is the base python": lambda d: d.update(testbed_python=d["verification_python"]),
    "testbed python is another file": lambda d: d.update(testbed_python="/opt/miniconda3/envs/testbed/bin/python3.9"),
    "testbed python relative": lambda d: d.update(testbed_python="python"),
    "image id short": lambda d: d.update(verified_image_id="sha256:abc"),
    "image id tag": lambda d: d.update(verified_image_id="swebench/sweb.eval.x86_64.pytest-dev_1776_pytest-5227:latest"),
    "image id uppercase": lambda d: d.update(verified_image_id=d["verified_image_id"].upper()),
    "evidence empty": lambda d: d.update(evidence=[]), "evidence not a list": lambda d: d.update(evidence="seen"),
    "evidence blank entry": lambda d: d.update(evidence=["ok", " "]), "evidence non-string": lambda d: d.update(evidence=[1]),
}


@pytest.mark.parametrize("name", sorted(REJECTED))
def test_every_malformed_incomplete_or_contradictory_config_is_refused(tmp_path, name):
    with pytest.raises(IdentityUnverified):
        load_agent_config(write_cfg(tmp_path, REJECTED[name]))
    with pytest.raises(IdentityUnverified):
        load_agent_identity(write_cfg(tmp_path, REJECTED[name]))


@pytest.mark.parametrize("raw", ["", "{not json", "[]", "null", '"verified"', "123", "{}"])
def test_unreadable_or_non_object_files_are_refused(tmp_path, raw):
    with pytest.raises(IdentityUnverified):
        load_agent_config(write_cfg(tmp_path, raw=raw))


def test_a_missing_file_is_refused_not_defaulted(tmp_path):
    with pytest.raises(IdentityUnverified, match="cannot read"):
        load_agent_config(tmp_path / "absent.json")


def test_the_previous_minimal_shape_is_no_longer_enough_so_nothing_is_inferred(tmp_path):
    old = dict(schema=1, status="verified", name="nonroot", uid=1000, gid=1000, home="/home/nonroot")  # the old minimal shape
    p = tmp_path / "old.json"
    p.write_text(json.dumps(old))
    with pytest.raises(IdentityUnverified, match="incomplete"):
        load_agent_config(p)


def test_a_different_valid_home_or_account_is_accepted_only_when_everything_else_is_explicit(tmp_path):
    cfg = load_agent_config(write_cfg(tmp_path, lambda d: d.update(home="/nonexistent", name="svc", uid=5000, gid=5001)))
    assert (cfg.identity.name, cfg.identity.user, cfg.identity.home) == ("svc", "5000:5001", "/nonexistent")  # no /home/<name> rule
    assert cfg.identity != IDENT


def test_the_loader_never_reads_or_depends_on_su_or_the_environment(monkeypatch):
    monkeypatch.setenv("USER", "someone-else")
    monkeypatch.setenv("HOME", "/home/someone-else")
    assert load_agent_identity(REAL_CFG) == IDENT  # only the file decides; no ambient identity or fallback


def test_policy_errors_do_not_leak_out_as_a_different_exception_type(tmp_path):
    with pytest.raises(IdentityUnverified) as info:
        load_agent_config(write_cfg(tmp_path, lambda d: d.update(home="/tmp")))
    assert not isinstance(info.value, PolicyViolation)
    assert isinstance(info.value.__cause__, PolicyViolation)


# ---------------- duplicate JSON keys are refused at every level ----------------
def raw_with(old, new):
    """The real config text with one exact substring replaced (so duplicates can be written into the JSON itself)."""
    text = REAL_CFG.read_text()
    assert text.count(old) == 1, old
    return text.replace(old, new)


DUPLICATES = {
    "conflicting uid, root first": ('"uid": 1000,', '"uid": 0,\n  "uid": 1000,', "uid"),
    "conflicting uid, root last": ('"uid": 1000,', '"uid": 1000,\n  "uid": 0,', "uid"),
    "identical uid repeated": ('"uid": 1000,', '"uid": 1000,\n  "uid": 1000,', "uid"),
    "conflicting gid": ('"gid": 1000,', '"gid": 0,\n  "gid": 1000,', "gid"),
    "conflicting home": ('"home": "/home/nonroot",', '"home": "/tmp",\n  "home": "/home/nonroot",', "home"),
    "conflicting name": ('"name": "nonroot",', '"name": "root",\n  "name": "nonroot",', "name"),
    "conflicting status": ('"status": "verified",', '"status": "unverified",\n  "status": "verified",', "status"),
    "conflicting verified image id": ('"verified_on": "2026-11-19",', '"verified_on": "2026-11-19",\n  "verified_image_id": "sha256:' + "0" * 64 + '",', "verified_image_id"),
    "conflicting verification python": ('"verification_python": "/opt/miniconda3/bin/python",', '"verification_python": "/tmp/python",\n  "verification_python": "/opt/miniconda3/bin/python",', "verification_python"),
    "repeated documentation key": ('"verified_on": "2026-11-19",', '"verified_on": "2026-11-19",\n  "verified_on": "2026-11-20",', "verified_on"),
    # nested objects: the hook runs while parsing, before any schema rule, so unknown or free-form keys are covered too
    "nested object, conflicting": ('"verified_on": "2026-11-19",', '"verified_on": "2026-11-19",\n  "extra_note": {"x": 1, "x": 2},', "x"),
    "deeply nested object, identical": ('"verified_on": "2026-11-19",', '"verified_on": "2026-11-19",\n  "extra_note": [{"a": {"b": {"k": 1, "k": 1}}}],', "k"),
    "object inside an evidence list": ('"evidence": [', '"evidence": [{"a": 1, "a": 2},', "a"),
}


@pytest.mark.parametrize("name", sorted(DUPLICATES))
def test_a_repeated_key_is_refused_at_every_level_and_never_resolved_by_position(tmp_path, name):
    old, new, key = DUPLICATES[name]
    p = write_cfg(tmp_path, raw=raw_with(old, new))
    with pytest.raises(IdentityUnverified, match=f"repeats the key '{key}'"):  # this rule fired, not a later schema rule
        load_agent_config(p)
    with pytest.raises(IdentityUnverified, match=f"repeats the key '{key}'"):
        load_agent_identity(p)


def test_the_refusal_names_the_key_but_never_echoes_a_value(tmp_path):
    raw = raw_with('"home": "/home/nonroot",', '"home": "/home/SENTINEL-ONE",\n  "home": "/home/SENTINEL-TWO",')
    with pytest.raises(IdentityUnverified) as info:
        load_agent_config(write_cfg(tmp_path, raw=raw))
    assert "'home'" in str(info.value) and "SENTINEL" not in str(info.value)


def test_a_conflicting_uid_is_refused_whichever_order_it_appears_in(tmp_path):
    """The defect: with last-value-wins, `uid: 0` then `uid: 1000` loaded as 1000, and `uid: 1000` then `uid: 0` as root."""
    for old, new, _ in (DUPLICATES["conflicting uid, root first"], DUPLICATES["conflicting uid, root last"]):
        with pytest.raises(IdentityUnverified, match="repeats the key 'uid'"):
            load_agent_config(write_cfg(tmp_path, raw=raw_with(old, new)))


def test_the_duplicate_rule_does_not_reject_a_normal_file_or_the_same_key_name_in_different_objects(tmp_path):
    assert load_agent_identity(REAL_CFG) == IDENT
    cfg = real_cfg()
    cfg["still_unverified"] = {"uid": 1, "inner": {"uid": 2}, "other": {"uid": 3}, "list": [{"uid": 4}, {"uid": 5}]}
    p = tmp_path / "ok.json"
    p.write_text(json.dumps(cfg))  # `uid` appears at several depths and in sibling objects, but never twice in one object
    assert load_agent_identity(p) == IDENT


# ---------------- a home may overlap only the interpreter or only the shell: each call must catch its own ----------------
def test_a_home_overlapping_only_the_verification_interpreter_is_refused(tmp_path):
    def mutate(d):
        d.update(verification_python="/srv/py/bin/python", home="/srv/py")
    AgentIdentity(name="nonroot", uid=1000, gid=1000, home="/srv/py")  # the identity rules alone accept this home...
    check_home_overlaps(AgentIdentity(name="nonroot", uid=1000, gid=1000, home="/srv/py"), "/opt/miniconda3/bin/python")  # ...and so do the PATH dirs
    with pytest.raises(IdentityUnverified, match=r"overlaps '/srv/py/bin/python'"):  # only the verification-interpreter check can reject it
        load_agent_config(write_cfg(tmp_path, mutate))


@pytest.mark.parametrize("home", ["/srv/py", "/srv/py/bin", "/srv/py/bin/python", "/srv/py/bin/python/x"])
def test_the_home_may_not_equal_contain_or_sit_inside_the_verification_interpreter(tmp_path, home):
    with pytest.raises(IdentityUnverified, match="overlaps"):
        load_agent_config(write_cfg(tmp_path, lambda d: d.update(verification_python="/srv/py/bin/python", home=home)))


def test_a_home_overlapping_only_a_custom_shell_is_refused(tmp_path):
    def mutate(d):
        d.update(shell="/srv/sh/bash", home="/srv/sh")
    AgentIdentity(name="nonroot", uid=1000, gid=1000, home="/srv/sh")  # acceptable on its own
    with pytest.raises(IdentityUnverified, match=r"overlaps '/srv/sh/bash'"):  # only the shell check can reject it
        load_agent_config(write_cfg(tmp_path, mutate))


@pytest.mark.parametrize("home", ["/srv/sh", "/srv/sh/bash", "/srv/sh/bash/x"])
def test_the_home_may_not_equal_contain_or_sit_inside_the_shell(tmp_path, home):
    with pytest.raises(IdentityUnverified, match="overlaps"):
        load_agent_config(write_cfg(tmp_path, lambda d: d.update(shell="/srv/sh/bash", home=home)))


def test_the_testbed_interpreter_overlap_check_is_currently_redundant_and_this_test_says_when_it_stops_being_so():
    """No config can make the testbed-interpreter overlap call the only thing that rejects a home.

    `testbed_python` is pinned to TESTBED_PYTHON, and every home that equals, contains or lies inside that path is already
    refused by `AgentIdentity` itself (the path is under the protected /opt/miniconda3). So removing that one call cannot
    change any outcome, which is why no test can distinguish it. If this test starts failing, the protected-mount rules were
    relaxed and the testbed check now carries weight: add a test where only that check rejects the home.
    """
    parts = TESTBED_PYTHON.strip("/").split("/")
    homes = ["/" + "/".join(parts[:i]) for i in range(1, len(parts) + 1)] + [TESTBED_PYTHON + "/x", TESTBED_PYTHON + "/x/y"]
    for home in homes:
        with pytest.raises(PolicyViolation):
            AgentIdentity(name="nonroot", uid=1000, gid=1000, home=home)  # refused before any config-level overlap check runs


def test_the_same_homes_are_accepted_when_nothing_overlaps(tmp_path):
    """Controls: the rejections above come from the overlap rules, not from the paths themselves."""
    cfg = load_agent_config(write_cfg(tmp_path, lambda d: d.update(home="/srv/py")))
    assert cfg.identity.home == "/srv/py" and cfg.verification_python == "/opt/miniconda3/bin/python"
    cfg = load_agent_config(write_cfg(tmp_path, lambda d: d.update(home="/srv/sh")))
    assert cfg.identity.home == "/srv/sh" and cfg.shell == "/bin/bash"
    cfg = load_agent_config(write_cfg(tmp_path, lambda d: d.update(verification_python="/srv/py/bin/python", shell="/srv/sh/bash")))
    assert (cfg.verification_python, cfg.shell, cfg.identity.home) == ("/srv/py/bin/python", "/srv/sh/bash", "/home/nonroot")


# ---------------- excessive nesting is refused as IdentityUnverified, never leaking RecursionError ----------------
def _deep_depth():
    return max(5000, sys.getrecursionlimit() * 5)  # far beyond what the JSON parser can recurse through


def _deep_array():
    return "[" * _deep_depth() + "]" * _deep_depth()


def _deep_object():
    return '{"a":' * _deep_depth() + "1" + "}" * _deep_depth()


def _valid_config_with(key, value_marker, deep):
    """The real config, valid in every respect, except that `key` holds the deeply nested JSON text `deep`."""
    d = real_cfg()
    d[key] = value_marker
    return json.dumps(d).replace(json.dumps(value_marker), deep)


DEEP = {
    "deep array as the whole document": lambda: _deep_array(),
    "deep object chain as the whole document": lambda: _deep_object(),
    # the rest of these files is valid, so only the nesting can be what refuses them
    "deep array under a documentation key": lambda: _valid_config_with("still_unverified", "@@DEEP@@", _deep_array()),
    "deep objects under a documentation key": lambda: _valid_config_with("still_unverified", "@@DEEP@@", _deep_object()),
    # inside an entry of the evidence list (a type error under the schema, but the parser gives up first)
    "deep array as an evidence entry": lambda: json.dumps({**real_cfg(), "evidence": ["@@DEEP@@"]}).replace('"@@DEEP@@"', _deep_array()),
}


@pytest.mark.parametrize("name", sorted(DEEP))
def test_excessively_nested_json_is_refused_as_identity_unverified_and_never_partially_loaded(tmp_path, name):
    p = write_cfg(tmp_path, raw=DEEP[name]())
    for loader in (load_agent_config, load_agent_identity):
        result = None
        with pytest.raises(IdentityUnverified, match="nested too deeply") as info:
            result = loader(p)
        assert result is None  # nothing was returned: no identity, no partial configuration
        assert not isinstance(info.value, RecursionError)  # the low-level error does not leak out as the raised type
        assert isinstance(info.value.__cause__, RecursionError)  # ...and it is preserved as the cause


def test_ordinary_nesting_is_unaffected(tmp_path):
    """Normal behaviour is unchanged: nesting far deeper than any real config, but within the parser's reach, still loads."""
    nested = "[" * 50 + "1" + "]" * 50
    p = write_cfg(tmp_path, raw=_valid_config_with("still_unverified", "@@DEEP@@", nested))
    assert load_agent_identity(p) == IDENT
    assert load_agent_config(p).verification_python == "/opt/miniconda3/bin/python"


@pytest.mark.parametrize("name", ["AgentIdentity", "validate_interpreter_path", "check_home_overlaps"])
def test_a_recursion_error_from_anywhere_else_is_not_swallowed(tmp_path, monkeypatch, name):
    """The nesting handler covers JSON parsing only: the same exception type raised by other code is a bug and must surface."""
    def boom(*a, **k):
        raise RecursionError("a programming error, not a deep config")
    monkeypatch.setattr(AE, name, boom)
    with pytest.raises(RecursionError, match="programming error"):
        load_agent_config(REAL_CFG)

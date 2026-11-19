import inspect
import json
from pathlib import Path

import pytest

from aiep import agent_env as AE
from aiep.agent_env import (EXPERIMENT_PATH, IDENTITY_SCRIPT, IdentityUnverified, check_identity, experiment_env,
                            load_agent_identity)
from aiep.container_policy import AgentIdentity

REAL = Path(__file__).resolve().parents[1]
# TEST-ONLY identity: the real image's account is unverified.
IDENT = AgentIdentity(name="nonroot", uid=1000, gid=1000, home="/home/nonroot")
GOOD = b"1000\n1000\nnonroot\nnonroot:x:1000:1000:dog:/home/nonroot:/bin/bash\n"


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


# ---------------- configuration ----------------
def test_the_repository_identity_config_is_unverified_and_refuses_to_load():
    cfg = json.loads((REAL / "config" / "agent_identity.json").read_text())
    assert cfg["status"] == "unverified" and cfg["uid"] is None and cfg["gid"] is None and cfg["home"] is None
    assert cfg["name"] == "nonroot" and cfg["evidence"] and cfg["to_verify"]
    with pytest.raises(IdentityUnverified):
        load_agent_identity(REAL / "config" / "agent_identity.json")


def test_a_verified_config_loads_and_an_incomplete_or_invalid_one_does_not(tmp_path):
    def write(**over):
        d = dict(status="verified", name="nonroot", uid=1000, gid=1000, home="/home/nonroot")
        d.update(over)
        p = tmp_path / "id.json"
        p.write_text(json.dumps(d))
        return p
    assert load_agent_identity(write()) == IDENT
    for over in (dict(status="unverified"), dict(uid=None), dict(gid=None), dict(uid=0), dict(gid=True), dict(home=None),
                 dict(name="root"), dict(home="/tmp"), dict(home="/"), dict(home="relative"), dict(home="/home/x/../y"),
                 dict(home="/testbed/home")):
        with pytest.raises(IdentityUnverified):
            load_agent_identity(write(**over))
    for key in ("name", "uid", "gid", "home"):  # a missing key fails closed too
        p = write()
        d = json.loads(p.read_text())
        del d[key]
        p.write_text(json.dumps(d))
        with pytest.raises(IdentityUnverified):
            load_agent_identity(p)
    # no /home/<name> convention: any plain, non-overlapping home from the verified image is accepted
    assert load_agent_identity(write(home="/nonexistent")).home == "/nonexistent"

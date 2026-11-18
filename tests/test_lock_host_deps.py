import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from aiep import hostdeps
from aiep.hostdeps import ResolutionError, parse_requirements, resolve

REAL = Path(__file__).resolve().parents[1]


class FakeDist:
    def __init__(self, name, version, requires=()):
        self.version, self.requires = version, list(requires)


def world(**dists):
    def get(name):
        try:
            return dists[name]
        except KeyError:
            raise metadata.PackageNotFoundError(name)
    return get


def test_closure_includes_only_reachable_packages():
    get = world(
        app=FakeDist("app", "1", ["lib>=1", "other; python_version < '3.0'"]),
        lib=FakeDist("lib", "2", ["leaf"]),
        leaf=FakeDist("leaf", "3"),
        stray=FakeDist("stray", "9"),  # installed but nobody asks for it
    )
    assert resolve(parse_requirements("app"), get) == {"app": "1", "leaf": "3", "lib": "2"}


def test_markers_and_extras_are_honoured():
    get = world(
        app=FakeDist("app", "1", ["opt; extra == 'fast'", "win; sys_platform == 'win32'"]),
        opt=FakeDist("opt", "5"),
    )
    assert resolve(parse_requirements("app"), get) == {"app": "1"}
    assert resolve(parse_requirements("app[fast]"), get) == {"app": "1", "opt": "5"}


def test_missing_dependency_is_an_error_not_a_guess():
    get = world(app=FakeDist("app", "1", ["ghost"]))
    with pytest.raises(ResolutionError, match="ghost"):
        resolve(parse_requirements("app"), get)


def test_installed_version_must_satisfy_declared_pin():
    get = world(app=FakeDist("app", "1"))
    with pytest.raises(ResolutionError, match="does not satisfy"):
        resolve(parse_requirements("app==2"), get)


def test_name_normalisation_and_cycles():
    get = world(
        **{"my-pkg": FakeDist("my-pkg", "1", ["My_Other"]), "my-other": FakeDist("my-other", "2", ["my.pkg"])}
    )
    assert resolve(parse_requirements("My_Pkg"), get) == {"my-other": "2", "my-pkg": "1"}


def test_self_is_excluded():
    get = world(app=FakeDist("app", "1", ["aiep"]), aiep=FakeDist("aiep", "0"))
    assert "aiep" not in resolve(parse_requirements("app"), get)


def test_comments_and_blank_lines_ignored():
    assert [r.name for r in parse_requirements("# c\n\nfoo==1  # inline\n")] == ["foo"]


# ---- the real repo ----
def test_committed_lock_is_current_and_exact():
    r = subprocess.run([sys.executable, str(REAL / "scripts" / "lock_host_deps.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_lock_pins_every_line_exactly_and_names_are_canonical():
    lines = [l for l in (REAL / "requirements" / "host.lock").read_text().splitlines() if l and not l.startswith("#")]
    assert lines
    for l in lines:
        m = re.fullmatch(r"([a-z0-9][a-z0-9.\-]*)==([^=\s]+)", l)
        assert m, l
        assert canonicalize_name(m.group(1)) == m.group(1)
    assert any(l.startswith("swebench==5.0.2") for l in lines)


def test_pyproject_dependencies_are_a_subset_of_host_in():
    import tomllib

    py = tomllib.loads((REAL / "pyproject.toml").read_text())
    declared = {str(Requirement(d)) for d in py["project"]["dependencies"]}
    host_in = {str(r) for r in parse_requirements((REAL / "requirements" / "host.in").read_text())}
    assert declared <= host_in


def test_evaluator_build_deps_are_not_mixed_into_host_files():
    import json

    wheel_names = {canonicalize_name(w["name"]) for w in json.loads((REAL / "config" / "wheelhouse.json").read_text())["wheels"]}
    direct = {canonicalize_name(r.name) for r in parse_requirements((REAL / "requirements" / "host.in").read_text())}
    assert not (wheel_names & direct)
    assert "setuptools-scm" not in direct and "wheel" not in direct

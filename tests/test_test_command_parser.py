"""Defect 2: test-command detection by structured command parsing (aiep.transcript), not by 'a runner word appears anywhere'."""
import json

import pytest

from aiep.transcript import TEST_COMMAND_SPEC, is_test_command, parse_transcript

COUNTED = [
    # plain runners
    "pytest", "pytest tests/x.py", "pytest -x -q tests/test_a.py::test_b", "py.test tests", "tox", "tox -e py39", "nosetests x", "nose2 -v",
    "python -m pytest -q", "python3 -m pytest tests/", "python3.9 -m pytest -x", "python -X dev -m pytest x", "python -W ignore -m pytest x", "python -m py.test x",
    "python -m unittest", "python -m unittest discover -s tests", "python3 -m unittest tests.test_x",
    # absolute and relative executables (the agent's PATH pins the conda env directory)
    "/opt/miniconda3/envs/testbed/bin/pytest x", "/opt/miniconda3/envs/testbed/bin/python -m pytest x", "./venv/bin/pytest x", "~/.local/bin/pytest x",
    # django
    "./tests/runtests.py --settings=test_sqlite forms_tests", "cd tests && ./runtests.py x", "python tests/runtests.py x", "python3 ./tests/runtests.py --parallel 1 forms_tests.tests",
    "tests/runtests.py x", "python -m django test", "python -m django test app.tests --settings=s", "python manage.py test app", "django-admin test app --settings=s",
    "cd /testbed/tests && python runtests.py -v 2 admin_views",
    # sympy
    "bin/test sympy/core", "./bin/test sympy/core/tests/test_basic.py", "python bin/test -k test_x sympy/core", "cd /testbed && bin/test sympy/printing",
    "python -c 'import sympy; sympy.test(\"core\")'", "python -c \"import sympy; sympy.test('core', verbose=True)\"", "python -c 'import pytest; pytest.main([\"x\"])'",
    # setup.py, make
    "python setup.py test", "python3 setup.py test -s x", "python setup.py pytest", "./setup.py test", "make test", "make check",
    # compound commands and wrappers
    "cd /testbed && python -m pytest tests/x.py -q", "cd /testbed; pytest x", "source /opt/miniconda3/bin/activate && conda activate testbed && pytest sklearn/x.py",
    "pytest x 2>&1 | tail -30", "pytest x | tee out.txt", "(cd tests && pytest x)", "FOO=1 BAR=2 pytest x", "PYTHONPATH=. python -m pytest x", "env PYTHONPATH=. pytest x",
    "timeout 600 pytest x", "time pytest x", "nohup pytest x &", "sudo pytest x", "conda run -n testbed pytest x", "conda run -n testbed --no-capture-output python -m pytest x",
    "uv run pytest x", "poetry run pytest x", "xvfb-run -a pytest lib/matplotlib/tests/test_axes.py", "coverage run -m pytest x", "coverage run runtests.py x",
    "bash -c 'cd /testbed && pytest x'", "sh -c \"python -m pytest x\"", "echo start && pytest x", "git stash && pytest x; git stash pop",
    "python - <<'EOF'\nimport pytest\npytest.main(['x'])\nEOF",
    "pytest x\npytest y",
    "ls\npytest x",
]
NOT_COUNTED = [
    # the runner word is an ARGUMENT of another program
    "grep -rn pytest .", "echo pytest", "git diff | grep pytest", "pip install pytest", "pip install -e .[test]", "cat tests/runtests.py", "ls tests/runtests.py",
    "grep -n 'def test_' tests/x.py", "head -50 tox.ini", "git log --oneline -- tests/", "sed -n 1,20p bin/test", "less setup.py", "find . -name 'pytest*'", "which pytest",
    "conda install pytest", "python -m pip install pytest", "wc -l tests/test_x.py", "echo 'run: pytest x'", "git grep -n 'setup.py test'", "ls /opt/miniconda3/envs/testbed/bin/pytest",
    "cat setup.py | grep test", "rm -f .pytest_cache", "cd tests", "cd /testbed && ls tests", "mkdir tests", "touch tests/test_new.py",
    # excluded invocations
    "pytest --version", "pytest --help", "pytest -h", "python -m pytest --collect-only", "pytest --co -q tests", "tox --help", "python tests/runtests.py --help",
    # not test runs
    "python reproduce.py", "python tests/test_x.py", "python -c 'print(1)'", "python -c 'import sympy; print(sympy.__version__)'", "python setup.py build_ext --inplace",
    "python setup.py install", "python -m pip --version", "make html", "make", "python manage.py migrate", "git status", "", "   ", "pytest_plugin_dir=1 ls",
    "FOO=pytest ls", "ls | xargs echo pytest",
]


@pytest.mark.parametrize("cmd", COUNTED)
def test_counted(cmd):
    assert is_test_command(cmd), cmd


@pytest.mark.parametrize("cmd", NOT_COUNTED)
def test_not_counted(cmd):
    assert not is_test_command(cmd), cmd


def test_unparseable_quotes_do_not_crash_and_stay_conservative():
    assert is_test_command("pytest 'unterminated") is True  # the executable position is still a runner
    assert is_test_command("echo 'unterminated pytest") is False
    assert is_test_command(None) is False and is_test_command(123) is False


def test_runtime_spec_is_the_registered_spec():
    for key in ("version", "method", "executables", "python_modules", "exclusion_arguments", "not_counted"):
        assert key in TEST_COMMAND_SPEC
    assert json.loads(json.dumps(TEST_COMMAND_SPEC)) == TEST_COMMAND_SPEC  # plain data: it is embedded in the hashed definition


# ---- cycle logic on the shapes Claude Code may emit (is_error true/false/missing, string or list content)
def tu(i, name, **inp):
    return json.dumps({"type": "assistant", "message": {"id": f"m{i}", "content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}})


def tr(i, content, **extra):
    return json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": content, **extra}]}})


RESULT = json.dumps({"type": "result", "subtype": "success", "num_turns": 3})


def parse(lines, cap=5):
    return parse_transcript("\n".join(lines + [RESULT]) + "\n", cycle_cap=cap)


def test_nonzero_exit_flagged_is_error_is_a_failed_run_even_with_quiet_output():
    m = parse([tu(1, "Bash", command="/opt/miniconda3/envs/testbed/bin/pytest x"), tr(1, "Exit code 1", is_error=True), tu(2, "Edit", file_path="f.py"),
               tu(3, "Bash", command="pytest x"), tr(3, "1 passed in 0.1s", is_error=False)])
    assert (m["test_runs"], m["test_runs_failed"], m["test_fix_cycles"], m["compliance"]) == (2, 1, 1, True)


def test_list_shaped_content_is_read():
    m = parse([tu(1, "Bash", command="./tests/runtests.py x"), tr(1, [{"type": "text", "text": "FAILED (failures=2)"}]), tu(2, "Write", file_path="f.py"),
               tu(3, "Bash", command="./tests/runtests.py x"), tr(3, [{"type": "text", "text": "Ran 3 tests in 0.1s\n\nOK"}])])
    assert (m["test_runs_failed"], m["test_runs_passed"], m["test_fix_cycles"]) == (1, 1, 1)


def test_missing_is_error_falls_back_to_the_output():
    m = parse([tu(1, "Bash", command="pytest x"), tr(1, "3 passed in 1s")])
    assert m["test_runs_passed"] == 1 and m["test_runs_failed"] == 0


def test_a_grep_for_pytest_is_not_a_test_run_and_makes_no_cycle():
    m = parse([tu(1, "Bash", command="grep -rn pytest ."), tr(1, "FAILED somewhere"), tu(2, "Edit", file_path="f.py")])
    assert m["test_runs"] == 0 and m["test_fix_cycles"] == 0 and not m["compliance"]


def test_cycle_cap_exceeded_is_reported():
    lines = []
    for i in range(0, 14, 2):
        lines += [tu(i + 1, "Bash", command="pytest x"), tr(i + 1, "1 failed", is_error=True), tu(i + 2, "Edit", file_path="f.py")]
    m = parse(lines, cap=5)
    assert m["test_fix_cycles"] == 7 and m["cap_exceeded"] and not m["compliance"]

"""Metrics from Claude Code's stream-json transcript (``claude_stdout.jsonl``): turns, tool use, test runs, test-fix cycles and compliance.

Everything is computed from what Claude Code itself emitted (assistant ``tool_use`` blocks, the matching ``tool_result`` blocks, the final ``result`` event);
nothing is inferred about hidden tests. Definitions are PRE-REGISTERED in the experiment definition and implemented here exactly:

* TEST RUN: a ``Bash`` tool call whose command RUNS a test runner, decided by parsing the shell command (``TEST_COMMAND_SPEC``), not by finding a runner word in the text;
  ``--version``, ``--help`` and ``--collect-only`` invocations are excluded.
  Its STATE is ``failed`` (the tool result is an error, or the output shows failures), ``passed`` (output shows passes and no failures) or ``unknown``.
* TEST-FIX CYCLE: a FAILED test run followed, before the next test run, by at least one ``Edit`` or ``Write`` call.
* TEST USAGE: at least one test run (measured in both arms; in Config A it is spontaneous).
* COMPLIANCE (Config B): test usage, and (no failed run or at least one cycle), and cycles <= the cap in the B prompt. Reported for A as well so the two
  arms are measured identically, but it only means "followed the prescribed workflow" for B.

Limitations (reported, not hidden): edits made through Bash (sed -i, heredocs) are counted separately as ``bash_edit_like_calls`` and do NOT form cycles;
writes of reproduction scripts count as Edit/Write like any other edit; an unparseable line makes the transcript ``complete: false``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

TEST_COMMAND_SPEC: dict[str, Any] = {
    "version": 2,
    "method": ("the Bash command is split into simple commands at && || ; | & newlines and parentheses (quotes respected; here-documents are set aside); for each simple command "
               "leading VAR=value assignments and the wrappers below are skipped and the EXECUTABLE (basename) and its first arguments decide. A runner word that is only an "
               "argument of another program (grep, echo, cat, pip, git ...) never counts. bash -c / sh -c strings are parsed the same way."),
    "executables": ["pytest", "py.test", "tox", "nosetests", "nose2", "runtests.py", "bin/test (sympy)", "django-admin test", "setup.py test|pytest", "manage.py test"],
    "python_modules": ["pytest", "py.test", "unittest", "django test", "nose", "nose2"],
    "python_c_code_patterns": [r"\bpytest\.main\(", r"\bunittest\.main\(", r"\.test\("],
    "make_targets": ["test", "tests", "check"],
    "wrappers": ["env", "sudo", "time", "nohup", "timeout <N>", "command", "exec", "xvfb-run", "coverage run", "conda|mamba|micromamba run", "uv|poetry|pipenv|pdm|hatch run", "bash|sh|zsh -c"],
    "exclusion_arguments": ["--version", "--help", "-h", "--collect-only", "--co"],
    "not_counted": ["running a test file directly with python (python tests/test_x.py): this is how reproduction scripts look too", "listing, reading or searching test files",
                    "installing a test runner", "pytest --version / --help / --collect-only"]}
_PY = re.compile(r"^(python[0-9.]*|pypy[0-9.]*)$")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SIMPLE_WRAPPERS = frozenset({"env", "sudo", "time", "nohup", "command", "exec", "xvfb-run", "nice", "stdbuf"})
_RUNNER_EXES = frozenset({"pytest", "py.test", "tox", "nosetests", "nose2", "runtests.py"})
_PY_MODULES = frozenset({"pytest", "py.test", "unittest", "nose", "nose2"})
_OPS = frozenset({"&&", "||", ";", "|", "&", "(", ")", "\n", ";;"})  # also any token made only of these characters
_CODE = [re.compile(x) for x in TEST_COMMAND_SPEC["python_c_code_patterns"]]
_SKIP_VALUE_OPTS = {"-n": 1, "-p": 1, "--name": 1, "--prefix": 1, "--cwd": 1, "-c": 1, "-t": 1, "-s": 1, "-k": 1, "-u": 1, "-o": 1, "-e": 1, "--auto-servernum": 0}


def _tokens(command: str) -> list[list[str]]:
    """Simple commands (token lists) of a shell string; an unbalanced quote falls back to a plain whitespace split on the operators."""
    import shlex
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        toks = []
        for part in re.split(r"(&&|\|\||;|\||&|\n)", command):
            toks.extend([part] if part in _OPS else part.split())
    cmds: list[list[str]] = [[]]
    skip_next = False
    for t in toks:
        if skip_next:  # the target of a redirection
            skip_next = False
        elif t in _OPS or (t and set(t) <= set(";&|()\n")):
            cmds.append([])
        elif t and set(t) <= set("<>"):
            skip_next = True
        else:
            cmds[-1].append(t)
    return [c for c in cmds if c]


def _strip_wrappers(argv: list[str]) -> list[str]:
    """Drop leading env assignments and known wrappers, leaving [executable, args...]."""
    a = list(argv)
    while a:
        head = a[0].rsplit("/", 1)[-1]
        if _ENV.match(a[0]):
            a = a[1:]
        elif head in _SIMPLE_WRAPPERS:
            a = a[1:]
            while a and (a[0].startswith("-") or _ENV.match(a[0])):  # options of the wrapper (xvfb-run -a, sudo -E, env -i) and env assignments
                a = a[1:]
        elif head == "timeout":
            a = a[1:]
            while a and a[0].startswith("-"):
                a = a[1:]
            a = a[1:] if a and re.fullmatch(r"[0-9.]+[smhd]?", a[0]) else a
        elif head in ("conda", "mamba", "micromamba") and len(a) > 1 and a[1] == "run":
            a = a[2:]
            while a and a[0].startswith("-"):
                a = a[1 + _SKIP_VALUE_OPTS.get(a[0], 0):]
        elif head in ("uv", "poetry", "pipenv", "pdm", "hatch") and len(a) > 1 and a[1] == "run":
            a = a[2:]
            while a and a[0].startswith("-") and a[0] != "--":
                a = a[1:]
            if a and a[0] == "--":
                a = a[1:]
        elif head == "coverage" and len(a) > 1 and a[1] == "run":
            a = ["python"] + [x for x in a[2:]]  # coverage run [opts] -m pytest / script.py
            break
        else:
            break
    return a


def _python_is_test(args: list[str], raw: str, depth: int) -> bool:
    i = 0
    while i < len(args):
        x = args[i]
        if x in ("-X", "-W", "-Q"):
            i += 2
        elif x == "-m":
            mod = args[i + 1] if i + 1 < len(args) else ""
            rest = args[i + 2:]
            return mod in _PY_MODULES or (mod == "django" and bool(rest) and rest[0] == "test")
        elif x == "-c":
            code = args[i + 1] if i + 1 < len(args) else ""
            return any(p.search(code) for p in _CODE)
        elif x == "-":  # program read from stdin (here-document)
            return "<<" in raw and any(p.search(raw.split("<<", 1)[1]) for p in _CODE)
        elif x.startswith("-"):
            i += 1
        else:
            script = x.rsplit("/", 1)[-1]
            rest = args[i + 1:]
            if script == "runtests.py":
                return True
            if script == "setup.py":
                return bool(rest) and rest[0] in ("test", "pytest")
            if script == "manage.py":
                return bool(rest) and rest[0] == "test"
            return x.endswith("/bin/test") or x == "bin/test"
    return False


def _simple_is_test(argv: list[str], raw: str, depth: int) -> bool:
    a = _strip_wrappers(argv)
    if not a:
        return False
    exe, args = a[0], a[1:]
    base = exe.rsplit("/", 1)[-1]
    if base in ("bash", "sh", "zsh", "dash") and depth < 3:
        for j, x in enumerate(args):
            if x == "-c" or (x.startswith("-") and not x.startswith("--") and x.endswith("c") and len(x) <= 4):
                return j + 1 < len(args) and _is_test(args[j + 1], depth + 1)
        return False
    if base == "eval" and depth < 3:
        return _is_test(" ".join(args), depth + 1)
    if any(x in TEST_COMMAND_SPEC["exclusion_arguments"] for x in args):
        return False
    if _PY.match(base):
        return _python_is_test(args, raw, depth)
    if base in _RUNNER_EXES:
        return True
    if base == "test" and exe.endswith("bin/test"):
        return True
    if base == "setup.py":
        return bool(args) and args[0] in ("test", "pytest")
    if base == "manage.py" or base == "django-admin":
        return bool(args) and args[0] == "test"
    if base == "make":
        return any(x in TEST_COMMAND_SPEC["make_targets"] for x in args if not x.startswith("-"))
    return False


def _is_test(command: str, depth: int) -> bool:
    head, sep, body = command.partition("<<")
    text = head if sep else command
    return any(_simple_is_test(c, command, depth) for c in _tokens(text))


EDIT_TOOLS = frozenset({"Edit", "Write"})
_FAIL = re.compile(r"(\b\d+\s+(failed|errors?)\b|\bFAILED\b|\bFAIL:|\bERROR:|\bErrors?:\s*[1-9]|failures=[1-9]|errors=[1-9]|Traceback \(most recent call last\))", re.I)
_PASS = re.compile(r"(\b\d+\s+passed\b|\bOK\b|\ball tests passed\b|\bpassed in\b|^Ran \d+ tests?.*\n+OK)", re.I | re.M)
_BASH_EDIT = re.compile(r"(\bsed\s+(-[A-Za-z]*i|--in-place)|\bgit\s+apply\b|\bpatch\s+-|\btee\s|<<\s*['\"]?EOF|\bcat\s*>>?\s*\S+|>\s*\S+\.py)")


def is_test_command(command: str, spec: Mapping[str, Any] = TEST_COMMAND_SPEC) -> bool:
    """True when the shell command RUNS a test runner (see ``TEST_COMMAND_SPEC``, which is registered in the experiment definition). ``spec`` exists so a caller can
    state which spec it means; only the registered one is implemented."""
    if not isinstance(command, str) or not command.strip():
        return False
    if spec.get("version") != TEST_COMMAND_SPEC["version"]:
        raise ValueError("unsupported test-command spec version")
    return _is_test(command, 0)


def classify_test_output(output: str, *, is_error: bool | None) -> str:
    """``failed`` / ``passed`` / ``unknown`` for one test run's result."""
    if is_error:
        return "failed"
    if _FAIL.search(output or ""):
        return "failed"
    if _PASS.search(output or ""):
        return "passed"
    return "unknown"


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""


def parse_transcript(raw: bytes | str, *, cycle_cap: int, spec: Mapping[str, Any] = TEST_COMMAND_SPEC) -> dict[str, Any]:
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
    calls: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] | None = None
    bad = 0
    assistant_messages: set[str] = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if not isinstance(ev, dict):
            bad += 1
            continue
        kind = ev.get("type")
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        if kind == "assistant":
            assistant_messages.add(str(msg.get("id") or f"anon{len(assistant_messages)}"))
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    call = {"id": b.get("id"), "name": b.get("name"), "input": b.get("input") if isinstance(b.get("input"), dict) else {}, "output": "", "is_error": None}
                    calls.append(call)
                    if isinstance(call["id"], str):
                        by_id[call["id"]] = call
        elif kind == "user":
            for b in msg.get("content") or [] if isinstance(msg.get("content"), list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in by_id:
                    c = by_id[b["tool_use_id"]]
                    c["output"], c["is_error"] = _text_of(b.get("content")), b.get("is_error")
        elif kind == "result":
            result = ev
    names: dict[str, int] = {}
    runs: list[dict[str, Any]] = []
    cycles = edits = bash_edit_like = 0
    pending_failed = False  # a failed test run with no edit after it yet
    for i, c in enumerate(calls):
        names[str(c["name"])] = names.get(str(c["name"]), 0) + 1
        if c["name"] == "Bash":
            cmd = str(c["input"].get("command", ""))
            if is_test_command(cmd, spec):
                state = classify_test_output(c["output"], is_error=c["is_error"])
                runs.append({"call_index": i, "state": state})
                pending_failed = state == "failed"
            elif _BASH_EDIT.search(cmd):
                bash_edit_like += 1
        elif c["name"] in EDIT_TOOLS:
            edits += 1
            if pending_failed:
                cycles += 1
                pending_failed = False
    failed = sum(r["state"] == "failed" for r in runs)
    first = runs[0]["call_index"] if runs else None
    edits_after_first = sum(1 for c in calls[first:] if c["name"] in EDIT_TOOLS) if first is not None else 0
    used = len(runs) >= 1
    compliant = used and (failed == 0 or cycles >= 1) and cycles <= cycle_cap
    res = {k: result.get(k) for k in ("subtype", "is_error", "num_turns", "duration_ms", "duration_api_ms", "stop_reason", "total_cost_usd")} if result else {}
    return {"schema": 1, "complete": result is not None and bad == 0, "unparseable_lines": bad, "result_present": result is not None,
            "result": {**res, "cost_note": "total_cost_usd is Claude Code's client-side ESTIMATE, not measured"},
            "assistant_messages": len(assistant_messages), "turns_reported": res.get("num_turns"), "tool_calls_total": len(calls), "tool_calls_by_name": names,
            "bash_edit_like_calls": bash_edit_like, "edits_total": edits, "test_runs": len(runs), "test_runs_failed": failed,
            "test_runs_passed": sum(r["state"] == "passed" for r in runs), "test_runs_unknown": sum(r["state"] == "unknown" for r in runs),
            "first_test_run_call_index": first, "edits_after_first_test_run": edits_after_first, "test_fix_cycles": cycles, "cycle_cap": cycle_cap,
            "cap_exceeded": cycles > cycle_cap, "test_usage": used, "compliance": compliant, "final_test_state": runs[-1]["state"] if runs else None,
            "ended_with_passing_tests": bool(runs) and runs[-1]["state"] == "passed"}

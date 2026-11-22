"""Metrics from Claude Code's stream-json transcript (``claude_stdout.jsonl``): turns, tool use, test runs, test-fix cycles and compliance.

Everything is computed from what Claude Code itself emitted (assistant ``tool_use`` blocks, the matching ``tool_result`` blocks, the final ``result`` event);
nothing is inferred about hidden tests. The parser is deterministic and offline. Metrics version 2 (``parser_version``), after calibration-2 exposed two defects
(a here-document hid the rest of a command; edits made through Bash were not recognised):

* The shell command is parsed COMPLETELY: here-document bodies are set aside (they are data or a script, never shell commands) and every command after a
  delimiter is analysed.
* TEST ATTEMPT (``test_runs``, as before): a ``Bash`` call whose command RUNS a test runner (``TEST_COMMAND_SPEC``, unchanged). Its STATE, from the tool output only:
  ``not_started`` (the runner never started: ``No module named pytest``, ``command not found`` ... and no sign of a running suite), ``completed_failed`` (a runner started,
  finished and reported failures or errors), ``completed_passed``, ``started_incomplete`` (a runner started but printed no summary: timeout, truncation, crash) or
  ``unknown`` (no usable evidence; a non-zero exit alone proves nothing, because pipelines such as ``| tail`` hide it). ``test_runs_failed`` counts ONLY ``completed_failed``:
  a missing runner is never a failed suite.
* EDIT, two kinds kept apart: a CONFIRMED edit-tool call (``Edit``/``Write`` whose result was not an error; ``edits_total`` still counts every Edit/Write call, errored ones are
  also in ``edit_tool_calls_errored``) and a HEURISTIC Bash edit-like command (a Python script writing files, ``sed -i``, ``perl -pi``, ``cat >``, ``tee``, a redirect to a source file,
  ``git apply``/``patch``). A Bash edit-like command only shows that a file-writing command ran, never that a file changed; ``bash_edit_like_details`` records how each was classified.
* TEST-FIX CYCLE (conservative): (1) a test runner started and (2) COMPLETED WITH FAILURES, (3) an edit occurred (confirmed or Bash edit-like), then (4) a later test runner started. A command
  that failed before its runner started can neither begin nor end a cycle. ``test_fix_cycles`` counts such cycles; ``test_fix_cycles_confirmed_edit`` and ``test_fix_cycles_bash_edit_only`` split
  them by the kind of edit. Sequences that do not qualify cleanly (a failure and an edit with no later run, a re-run with no edit, a later run of unknown outcome, several test commands in one
  call, ...) are listed in ``ambiguous_sequences`` and never silently counted.
* TEST USAGE: ``test_usage`` is True when a test command was attempted (as before); ``test_usage_started`` when a runner actually started.
* COMPLIANCE (Config B): a runner started, and (no failed run or at least one cycle), and cycles <= the cap in the B prompt. Reported for A as well so the two arms are measured
  identically, but it only means "followed the prescribed workflow" for B. (Before version 2 an attempt that never started counted as test usage.)

Limitations (reported, not hidden): running a script file (``python fix.py``) is not seen as an edit; whether a Bash edit-like command changed anything is not established; shell
features beyond pipelines, lists, quoting, redirections, here-documents and ``bash -c`` are not interpreted; output attribution is per Bash call, so a call with several test commands is
ambiguous; an unparseable line makes the transcript ``complete: false``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

PARSER_VERSION = 2
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



# ------------------------------------------------------------------ shell parsing (here-documents set aside, then every simple command)
_HEREDOC_DELIM = re.compile(r"(-?)[ \t]*(?:'([^']*)'|\"([^\"]*)\"|\\?([A-Za-z_][A-Za-z0-9_]*))")


def _strip_heredocs(command: str) -> tuple[str, list[str]]:
    """(the command with every here-document BODY removed, the bodies in order). Quotes are respected across lines; ``<<<`` and arithmetic ``1 << 3`` are not here-documents; ``<<-``
    strips leading tabs of the terminator; an unterminated here-document runs to the end. The marker itself (``<<'E'``) stays in the text so its command still parses."""
    lines = command.split("\n")
    out: list[str] = []
    bodies: list[str] = []
    q: str | None = None
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        idx += 1
        out.append(line)
        pending: list[tuple[str, bool]] = []
        j = 0
        while j < len(line):
            ch = line[j]
            if q is None:
                if ch == "\\":
                    j += 2
                    continue
                if ch in "'\"":
                    q = ch
                elif line.startswith("<<<", j):
                    j += 3
                    continue
                elif line.startswith("<<", j):
                    m = _HEREDOC_DELIM.match(line, j + 2)
                    if m:
                        pending.append((m.group(2) or m.group(3) or m.group(4) or "", m.group(1) == "-"))
                        j = m.end()
                        continue
                    j += 2
                    continue
            elif ch == "\\" and q == '"':
                j += 2
                continue
            elif ch == q:
                q = None
            j += 1
        if pending and q is None:
            for delim, strip in pending:
                body: list[str] = []
                while idx < len(lines):
                    l = lines[idx].rstrip("\r")
                    idx += 1
                    if (l.lstrip("\t") if strip else l) == delim:
                        break
                    body.append(lines[idx - 1])
                bodies.append("\n".join(body))
    return "\n".join(out), bodies


def _simple_commands(command: str) -> list[dict[str, Any]]:
    """Simple commands in order: ``{"argv", "writes" (redirect-write targets), "bodies" (here-document bodies fed to this command)}``."""
    import shlex
    text, bodies = _strip_heredocs(command)
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>\n")
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        toks = []
        for part in re.split(r"(&&|\|\||;|\||&|\n)", text):
            toks.extend([part] if part in _OPS else part.split())
    cmds: list[dict[str, Any]] = [{"argv": [], "writes": [], "bodies": []}]
    k = 0  # next here-document body
    i = 0
    while i < len(toks):
        t = toks[i]
        cur = cmds[-1]
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if t in _OPS or (t and set(t) <= set(";&|()\n")):
            cmds.append({"argv": [], "writes": [], "bodies": []})
        elif t == "<<":  # here-document marker: its delimiter is the next token and its body was set aside
            if nxt is not None:
                i += 1
            if k < len(bodies):
                cur["bodies"].append(bodies[k])
            k += 1
        elif t == "<<<" or t == "<":
            i += 1  # the word or file fed to stdin
        elif t in (">", ">>", "&>", "&>>"):
            if nxt is not None:
                cur["writes"].append(nxt)
            i += 1
        elif t and set(t) <= set("<>&") and "&" in t:  # fd duplication such as 2>&1 or >&2, or >& file
            if nxt is not None and not re.fullmatch(r"\d+|-", nxt) and ">" in t:
                cur["writes"].append(nxt)
            i += 1
        elif t and set(t) <= set("<>"):
            i += 1
        else:
            cur["argv"].append(t)
        i += 1
    return [c for c in cmds if c["argv"] or c["writes"] or c["bodies"]]


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



def _python_is_test(args: list[str], body: str) -> bool:
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
        elif x == "-":  # program read from stdin (a here-document)
            return any(p.search(body) for p in _CODE)
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
    return any(p.search(body) for p in _CODE)  # python with no script, -c or -m reads its program from stdin


def _simple_is_test(cmd: Mapping[str, Any], depth: int) -> bool:
    a = _strip_wrappers(list(cmd["argv"]))
    if not a:
        return False
    exe, args = a[0], a[1:]
    base = exe.rsplit("/", 1)[-1]
    body = "\n".join(cmd["bodies"])
    if base in ("bash", "sh", "zsh", "dash") and depth < 3:
        for j, x in enumerate(args):
            if x == "-c" or (x.startswith("-") and not x.startswith("--") and x.endswith("c") and len(x) <= 4):
                return j + 1 < len(args) and any(r["test"] for r in _analyze(args[j + 1], depth + 1))
        return False
    if base == "eval" and depth < 3:
        return any(r["test"] for r in _analyze(" ".join(args), depth + 1))
    if any(x in TEST_COMMAND_SPEC["exclusion_arguments"] for x in args):
        return False
    if _PY.match(base):
        return _python_is_test(args, body)
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


# ------------------------------------------------------------------ Bash edit-like commands (HEURISTIC: they show a file-writing command ran, not that a file changed)
_PY_WRITE = re.compile(r"""open\(\s*[^)]*?,\s*(?:mode\s*=\s*)?['"][wax]\+?b?['"]|open\(\s*[^)]*?mode\s*=\s*['"][wax]|\.write_text\(|\.write_bytes\(|\bshutil\.(?:copy|copyfile|move)\w*\(|\bos\.(?:replace|rename)\(|\bfileinput\b""")
_PY_PATH = re.compile(r"""['"]([^'"\s]+\.(?:py|pyx|pxd|c|h|cc|cpp|cfg|toml|ini|rst|md|json|ya?ml|js|html|css|txt))['"]""")
_CODE_EXT = (".py", ".pyx", ".pxd", ".c", ".h", ".cc", ".cpp", ".cfg", ".toml", ".ini", ".rst", ".md", ".json", ".yml", ".yaml", ".js", ".html", ".css", ".in")
_SED_I = re.compile(r"^(-[A-Za-z]*i[A-Za-z]*(\..*)?|--in-place(=.*)?)$")
_PERL_I = re.compile(r"^-[A-Za-z0-9]*i[A-Za-z0-9.]*$")


def _scratch(path: str) -> bool:
    return path.startswith(("/dev/", "/tmp/", "/proc/")) or path in ("/dev/null", "/dev/stdout", "/dev/stderr")


def _edit_like(cmd: Mapping[str, Any], depth: int) -> list[dict[str, Any]]:
    """Edit-like findings of one simple command: ``{"kind", "targets", "targets_known", "source": "heuristic"}``. Scratch paths (/tmp, /dev) never count; reads, searches and test commands do not."""
    out: list[dict[str, Any]] = []
    a = _strip_wrappers(list(cmd["argv"]))
    writes = [w for w in cmd["writes"] if not _scratch(w)]
    base = a[0].rsplit("/", 1)[-1] if a else ""
    args = a[1:]
    body = "\n".join(cmd["bodies"])
    if base in ("bash", "sh", "zsh", "dash") and depth < 3:
        for j, x in enumerate(args):
            if x == "-c" and j + 1 < len(args):
                for r in _analyze(args[j + 1], depth + 1):
                    out += r["edits"]
        return out + _redirect_edits(base, writes)
    if _PY.match(base):
        code = body
        for j, x in enumerate(args):
            if x == "-c" and j + 1 < len(args):
                code = args[j + 1]
        if code and _PY_WRITE.search(code):
            paths = [m.group(1) for m in _PY_PATH.finditer(code) if not _scratch(m.group(1))]
            all_paths = [m.group(1) for m in _PY_PATH.finditer(code)]
            if not all_paths or paths:  # a script whose only visible targets are scratch files is not a source edit
                out.append({"kind": "python_write", "targets": sorted(set(paths)), "targets_known": bool(paths), "source": "heuristic"})
    elif base == "sed" and any(_SED_I.match(x) for x in args):
        opts = [x for x in args if x.startswith("-") and x != "-"]
        files = [x for x in args if not x.startswith("-")]
        script_given = any(x in ("-e", "-f") or x.startswith(("--expression", "--file")) for x in opts)
        targets = files if script_given else files[1:]
        targets = [t for t in targets if not _scratch(t)]
        if targets:
            out.append({"kind": "sed_i", "targets": targets, "targets_known": True, "source": "heuristic"})
    elif base == "perl" and any(_PERL_I.match(x) for x in args):
        files = [x for x in args if not x.startswith("-")]
        targets = [t for t in files[1:] if not _scratch(t)] if not any(x == "-e" for x in args) else [t for t in files if not _scratch(t)]
        if targets:
            out.append({"kind": "perl_i", "targets": targets, "targets_known": True, "source": "heuristic"})
    elif base == "tee":
        files = [x for x in args if not x.startswith("-") and not _scratch(x)]
        if files:
            out.append({"kind": "tee", "targets": files, "targets_known": True, "source": "heuristic"})
    elif base == "patch" or (base == "git" and args[:1] in (["apply"], ["restore"])) or (base == "git" and args[:1] == ["checkout"] and "--" in args):
        out.append({"kind": "vcs_or_patch", "targets": [], "targets_known": False, "source": "heuristic"})
    return out + _redirect_edits(base, writes)


def _redirect_edits(base: str, writes: list[str]) -> list[dict[str, Any]]:
    out = []
    for w in writes:
        if base == "cat":
            out.append({"kind": "cat_redirect", "targets": [w], "targets_known": True, "source": "heuristic"})
        elif base in ("echo", "printf") or w.endswith(_CODE_EXT):
            out.append({"kind": "redirect_write", "targets": [w], "targets_known": True, "source": "heuristic"})
    return out


def _analyze(command: str, depth: int = 0) -> list[dict[str, Any]]:
    """One record per simple command, in order: ``{"test": bool, "edits": [edit-like findings]}``."""
    return [{"test": _simple_is_test(c, depth), "edits": _edit_like(c, depth)} for c in _simple_commands(command)]


def is_test_command(command: str, spec: Mapping[str, Any] = TEST_COMMAND_SPEC) -> bool:
    """True when the shell command RUNS a test runner (see ``TEST_COMMAND_SPEC``, which is registered in the experiment definition). ``spec`` exists so a caller can
    state which spec it means; only the registered one is implemented."""
    if not isinstance(command, str) or not command.strip():
        return False
    if spec.get("version") != TEST_COMMAND_SPEC["version"]:
        raise ValueError("unsupported test-command spec version")
    return any(r["test"] for r in _analyze(command, 0))


def bash_edit_like(command: str) -> list[dict[str, Any]]:
    """The heuristic edit-like findings of a Bash command (empty for reads, searches and test runs)."""
    if not isinstance(command, str) or not command.strip():
        return []
    return [e for r in _analyze(command, 0) for e in r["edits"]]


# ------------------------------------------------------------------ test run classification (from the tool output only)
EDIT_TOOLS = frozenset({"Edit", "Write"})
_NOT_STARTED = re.compile(r"No module named ['\"]?(?:pytest|nose2?|unittest2|tox)|ModuleNotFoundError: No module named ['\"]?(?:pytest|nose)|command not found|: not found\b|can't open file|No such file or directory: ['\"]?(?:\S*(?:pytest|runtests|tox|test))|\bexec(?:utable)? file not found")
_STARTED = re.compile(r"test session starts|collected \d+ items?|^Ran \d+ tests?|^Found \d+ test|System check identified|hash randomization|tests finished|^[\w/.\-]+\.py\[\d+\]|"
                      r"^\s*(?:py\d+|\w+): commands|\b\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|warnings?)\b|no tests ran|^=+ .*(?:passed|failed|error)", re.I | re.M)
_COMPLETED = re.compile(r"\b\d+ (?:passed|failed|errors?)\b|^Ran \d+ tests?|tests finished|no tests ran|^(?:OK|FAILED)\b|\[(?:OK|FAIL)\]", re.M)
_FAILURES = re.compile(r"\b\d+ (?:failed|errors?|exceptions?)\b|^FAILED\b|\bFAIL:|\bERROR:|failures=[1-9]|errors=[1-9]|\[(?:FAIL|ERROR)\]|\bFAILED \(", re.M)


def classify_test_run(output: str, *, is_error: bool | None = None) -> tuple[str, str]:
    """(state, evidence) of one test attempt: ``not_started`` / ``completed_failed`` / ``completed_passed`` / ``started_incomplete`` / ``unknown``. A non-zero exit (``is_error``)
    alone proves nothing: it is the same for a missing runner and for failing tests, and pipelines hide it."""
    text = output or ""
    started = bool(_STARTED.search(text))
    if _NOT_STARTED.search(text) and not started:
        return "not_started", "the runner did not start (missing module or command) and no test output appeared"
    if started or _COMPLETED.search(text):
        if _COMPLETED.search(text):
            if _FAILURES.search(text):
                return "completed_failed", "a summary reports failures or errors"
            return "completed_passed", "a summary reports no failures"
        return "started_incomplete", "test output appeared but no summary line did (timeout, truncation or crash)"
    return "unknown", "no usable test output" + (" (the tool reported an error, which alone does not show a failed suite)" if is_error else "")


def classify_test_output(output: str, *, is_error: bool | None) -> str:
    """Legacy three-way label (``failed`` / ``passed`` / ``unknown``) kept for callers of version 1: ``failed`` now means a completed run with failures."""
    state, _ = classify_test_run(output, is_error=is_error)
    return {"completed_failed": "failed", "completed_passed": "passed"}.get(state, "unknown")


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
    runs: list[dict[str, Any]] = []  # one record per test ATTEMPT (a Bash call with a test command)
    details: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    edits = edit_errored = bash_edit_calls = 0
    cycles = cycles_confirmed = cycles_bash_only = unfinished = 0
    pending: dict[str, Any] | None = None  # the last COMPLETED-WITH-FAILURES run still awaiting its edit and its later run: {"call", "edits": [kinds]}
    for i, c in enumerate(calls):
        names[str(c["name"])] = names.get(str(c["name"]), 0) + 1
        if c["name"] in EDIT_TOOLS:
            edits += 1
            if c["is_error"] is True:
                edit_errored += 1
            elif pending is not None:
                pending["edits"].append("confirmed")
            continue
        if c["name"] != "Bash":
            continue
        cmd = str(c["input"].get("command", ""))
        records = _analyze(cmd, 0) if cmd.strip() else []
        n_tests = sum(1 for r in records if r["test"])
        edit_like = [e for r in records for e in r["edits"]]
        if edit_like:
            bash_edit_calls += 1
            details.append({"call_index": i, "kind": "bash_edit_like", "classification": [e["kind"] for e in edit_like], "targets": sorted({t for e in edit_like for t in e["targets"]})[:6],
                            "targets_known": all(e["targets_known"] for e in edit_like), "source": "heuristic", "command_excerpt": cmd[:160]})
        # order inside the call: edit-like commands that precede the (last) test command happen before it
        last_test = max((k for k, r in enumerate(records) if r["test"]), default=None)
        before = [e for k, r in enumerate(records) if last_test is None or k <= last_test for e in r["edits"]]
        after = [e for k, r in enumerate(records) if last_test is not None and k > last_test for e in r["edits"]]
        if pending is not None:
            pending["edits"] += ["bash_edit_like"] * len(before)
        if n_tests:
            state, evidence = classify_test_run(c["output"], is_error=c["is_error"])
            runs.append({"call_index": i, "state": state, "commands": n_tests})
            details.append({"call_index": i, "kind": "test_attempt", "state": state, "evidence": evidence, "test_commands_in_call": n_tests, "command_excerpt": cmd[:160]})
            if n_tests > 1:
                ambiguous.append({"call_index": i, "kind": "several_test_commands_in_one_call", "note": "the output cannot be attributed to individual commands; classified as one attempt"})
            started = state in ("completed_failed", "completed_passed", "started_incomplete")
            if started and pending is not None:
                if pending["edits"]:
                    cycles += 1
                    if "confirmed" in pending["edits"]:
                        cycles_confirmed += 1
                    else:
                        cycles_bash_only += 1
                    if state == "started_incomplete":
                        ambiguous.append({"call_index": i, "kind": "cycle_closed_by_a_run_with_no_summary", "note": "a runner started after the edit but its outcome is unknown"})
                else:
                    ambiguous.append({"call_index": i, "kind": "rerun_without_an_edit", "note": "a failed run was followed by another started run with no edit in between: not a cycle"})
                pending = None
            elif state == "unknown" and pending is not None:
                ambiguous.append({"call_index": i, "kind": "run_with_no_evidence_after_a_failure", "note": "it cannot be shown that a runner started, so the cycle stays open"})
            if state == "completed_failed":
                pending = {"call": i, "edits": []}
            elif started:
                pending = None
            if after and pending is not None:
                pending["edits"] += ["bash_edit_like"] * len(after)
        elif pending is not None and after:
            pending["edits"] += ["bash_edit_like"] * len(after)
    if pending is not None and pending["edits"]:
        unfinished += 1
        ambiguous.append({"call_index": pending["call"], "kind": "failure_and_edit_without_a_later_run", "note": "a run failed and an edit followed but no later runner started: not a cycle"})

    by_state = {k: sum(1 for r in runs if r["state"] == k) for k in ("not_started", "completed_failed", "completed_passed", "started_incomplete", "unknown")}
    failed, passed = by_state["completed_failed"], by_state["completed_passed"]
    started_n = failed + passed + by_state["started_incomplete"]
    first = runs[0]["call_index"] if runs else None
    edits_after_first = sum(1 for c in calls[first:] if c["name"] in EDIT_TOOLS) if first is not None else 0
    used = len(runs) >= 1
    compliant = started_n >= 1 and (failed == 0 or cycles >= 1) and cycles <= cycle_cap
    res = {k: result.get(k) for k in ("subtype", "is_error", "num_turns", "duration_ms", "duration_api_ms", "stop_reason", "total_cost_usd")} if result else {}
    last_state = runs[-1]["state"] if runs else None
    return {"schema": 1, "parser_version": PARSER_VERSION, "complete": result is not None and bad == 0, "unparseable_lines": bad, "result_present": result is not None,
            "result": {**res, "cost_note": "total_cost_usd is Claude Code's client-side ESTIMATE, not measured"},
            "assistant_messages": len(assistant_messages), "turns_reported": res.get("num_turns"), "tool_calls_total": len(calls), "tool_calls_by_name": names,
            "bash_edit_like_calls": bash_edit_calls, "edits_total": edits, "edit_tool_calls_errored": edit_errored,
            "test_runs": len(runs), "test_runs_failed": failed, "test_runs_passed": passed, "test_runs_unknown": by_state["unknown"] + by_state["started_incomplete"],
            "test_runs_not_started": by_state["not_started"], "test_runs_started": started_n, "test_runs_completed": failed + passed, "test_runs_started_incomplete": by_state["started_incomplete"],
            "first_test_run_call_index": first, "edits_after_first_test_run": edits_after_first, "test_fix_cycles": cycles, "test_fix_cycles_confirmed_edit": cycles_confirmed,
            "test_fix_cycles_bash_edit_only": cycles_bash_only, "unfinished_cycles": unfinished, "cycle_cap": cycle_cap,
            "cap_exceeded": cycles > cycle_cap, "test_usage": used, "test_usage_started": started_n >= 1, "compliance": compliant,
            "final_test_state": {"completed_failed": "failed", "completed_passed": "passed"}.get(last_state, "unknown") if runs else None, "final_test_run_state": last_state,
            "ended_with_passing_tests": bool(runs) and last_state == "completed_passed",
            "ambiguous_sequences": ambiguous, "analysis_details": details[:80]}

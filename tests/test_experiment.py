import json
from pathlib import Path

import pytest

from aiep import experiment as E
from aiep.task_input import TaskInput

REAL = Path(__file__).resolve().parents[1]
# The pre-registered hashes, written out here on purpose: changing a prompt must change this test in the same commit.
REGISTERED = {"template": "86571bc70d453965dfec954dae6c2e3a073bf61ddbdc2656bc7f5667846d7158",
              "A": "5e44cde50fea6d6c70cc65c0c97043a7c8fe9357d9468b4492305c0db4d64082",
              "B": "f7ad00720d74ee2467e1f7854e432d3cb09d435185383f691d8712dad6710111"}
TASK = TaskInput("pytest-dev__pytest-5227", "pytest-dev/pytest", "c" * 40,
                 "Logging format {name} is wrong.\n<<ISSUE>> <<WORKFLOW>> literal braces {} and %s here\n")


def test_registered_hashes_match_the_repository_prompts():
    assert E.load_prompts().hashes == REGISTERED


def test_a_and_b_differ_only_in_the_workflow_line():
    p = E.load_prompts()
    a, b = E.build_task_text("A", TASK, p), E.build_task_text("B", TASK, p)
    assert a != b
    assert a.replace(p.workflows["A"], "@@") == b.replace(p.workflows["B"], "@@")  # identical once the workflow is blanked
    assert a.count(p.workflows["A"]) == 1 and b.count(p.workflows["B"]) == 1 and p.workflows["A"] not in b


def test_issue_text_is_inserted_verbatim_and_never_reinterpreted():
    text = E.build_task_text("A", TASK)
    assert "<<ISSUE>> <<WORKFLOW>> literal braces {} and %s here" in text  # placeholders inside the issue stay literal
    assert text.count("Workflow:") == 1


def test_nothing_but_the_issue_and_workflow_enters_the_prompt():
    text = E.build_task_text("B", TASK)
    assert TASK.base_commit not in text and TASK.instance_id not in text and TASK.repo not in text
    for hidden in ("FAIL_TO_PASS", "PASS_TO_PASS", "test_patch", "gold"):
        assert hidden not in text


def test_prompt_record_has_the_registered_hashes_and_the_hash_of_the_text_sent():
    text = E.build_task_text("A", TASK)
    rec = E.prompt_record("A", text)
    assert rec == {"label": "A", "template_sha256": REGISTERED["template"], "workflow_sha256": REGISTERED["A"],
                   "prompt_sha256": E.sha256_text(text)}


def test_unknown_label_is_refused():
    with pytest.raises(E.PromptError):
        E.build_task_text("C", TASK)


def _doc():
    return json.loads((REAL / "config" / "workflow_prompts.json").read_text())


@pytest.mark.parametrize("mutate", [
    lambda d: d["workflows"].update(A=d["workflows"]["A"] + " Also run tests."),   # edited text, old hash
    lambda d: d.update(template=d["template"] + "extra"),
    lambda d: d["sha256"].update(B="0" * 64),
    lambda d: d["workflows"].pop("B"),
    lambda d: d.update(template=d["template"].replace("<<ISSUE>>", "")),
    lambda d: d.update(schema=2),
    lambda d: d["workflows"].update(A=""),
])
def test_tampered_or_malformed_prompt_files_are_refused(tmp_path, mutate):
    d = _doc()
    mutate(d)
    f = tmp_path / "p.json"
    f.write_text(json.dumps(d))
    with pytest.raises(E.PromptError):
        E.load_prompts(f)


def test_a_workflow_containing_a_placeholder_is_refused(tmp_path):
    d = _doc()
    d["workflows"]["A"] = "see <<ISSUE>>"
    d["sha256"]["A"] = E.sha256_text(d["workflows"]["A"])
    f = tmp_path / "p.json"
    f.write_text(json.dumps(d))
    with pytest.raises(E.PromptError, match="placeholder"):
        E.load_prompts(f)


# ------------------------------------------------------------------ claude arguments and the CLI pin
def test_claude_args_have_no_configuration_parameter_so_a_and_b_cannot_differ():
    import inspect
    assert list(inspect.signature(E.claude_args).parameters) == ["model", "budget_usd"]
    args = E.claude_args("claude-sonnet-5-5", "1.50")
    assert args == E.claude_args("claude-sonnet-5-5", "1.50")


def test_claude_args_contents():
    args = E.claude_args("claude-sonnet-5-5", "1.50")
    assert args[0] == "-p" and ("--output-format", "stream-json") == args[1:3] and "--verbose" in args
    assert "--bare" not in args and "--max-turns" not in args and "--dangerously-skip-permissions" not in args
    assert args[args.index("--tools") + 1] == "Bash,Read,Edit,Write,Glob,Grep"
    for forbidden in ("WebSearch", "WebFetch", "--mcp-config", "--include-partial-messages"):
        assert forbidden not in " ".join(args)
    assert args[args.index("--model") + 1] == "claude-sonnet-5-5" and args[args.index("--max-budget-usd") + 1] == "1.65"  # client budget = gateway budget 1.50 x 1.10
    assert "--safe-mode" in args and "--no-session-persistence" in args


@pytest.mark.parametrize("budget", ["", "0", "0.0", "-1", "abc", "1e999999999", None, "nan", "101", "1000", " 1", "1.00001", 5])
def test_bad_budgets_are_refused(budget):
    with pytest.raises(ValueError):
        E.claude_args("claude-sonnet-5-5", budget)


def test_the_pinned_model_is_the_experiment_model():
    assert json.loads((REAL / "config" / "experiment.json").read_text())["model"] == "claude-sonnet-5-5"


GATE_1B = {"version": "2.1.328", "platform": "linux-x64", "sha256": "a982d38030b97e750848add2cbb2df6d9a70d5544a22ca9b216a349ba25865e8",
           "size_bytes": 373019152, "source": "https://downloads.claude.ai/claude-code-releases/2.1.328/linux-x64/claude"}


def _real_pin():
    return json.loads((REAL / "config" / "claude_cli.json").read_text())


def test_the_repository_cli_pin_matches_the_gate_1b_evidence_exactly():
    cfg = _real_pin()
    assert {k: cfg[k] for k in GATE_1B} == GATE_1B and cfg["status"] == "pinned" and cfg["destination"] == "/opt/aiep/claude"
    pin = E.load_claude_cli()
    assert (pin.version, pin.platform, pin.sha256, pin.size_bytes, pin.destination) == (
        "2.1.328", "linux-x64", GATE_1B["sha256"], 373019152, "/opt/aiep/claude")


def test_the_pin_adds_no_fields_and_keeps_the_original_schema():
    assert set(_real_pin()) == {"schema", "status", "status_note", "version", "platform", "source", "sha256", "size_bytes",
                                "destination", "max_binary_bytes", "max_binary_note", "to_verify_after_download"}
    assert _real_pin()["schema"] == 1 and _real_pin()["max_binary_bytes"] == 400 * 1024**2  # the ceiling was not touched


def test_the_pinned_binary_fits_the_ceiling_and_the_components_tmpfs():
    pin = E.load_claude_cli()
    MiB = 1024**2
    assert pin.size_bytes <= pin.max_binary_bytes and pin.size_bytes <= 416 * MiB - 16 * MiB  # ceiling, and tmpfs less its 16 MiB slack
    assert pin.size_bytes + 2048 < 512 * MiB  # the injection tar fits the exec stdin cap


def test_the_source_url_names_the_same_version_and_platform_as_the_pin():
    cfg = _real_pin()
    assert cfg["source"] == f"https://downloads.claude.ai/claude-code-releases/{cfg['version']}/{cfg['platform']}/claude"


def test_the_status_note_does_not_claim_runtime_compatibility():
    note = _real_pin()["status_note"].lower()
    assert "not been executed" in note and "gate 3" in note


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(status="unpinned"), lambda d: d.update(sha256=None), lambda d: d.pop("sha256"),
    lambda d: d.update(sha256=d["sha256"][:-1]), lambda d: d.update(sha256=d["sha256"].upper()), lambda d: d.update(size_bytes=None),
    lambda d: d.update(size_bytes=0), lambda d: d.update(size_bytes=d["max_binary_bytes"] + 1), lambda d: d.update(size_bytes=str(d["size_bytes"])),
    lambda d: d.update(platform="linux-x64-musl"), lambda d: d.update(platform="darwin-arm64"),
    lambda d: d.update(destination="/usr/local/bin/claude"), lambda d: d.update(version="latest"),
])
def test_corrupted_or_incomplete_variants_of_the_real_pin_are_refused(tmp_path, mutate):
    d = _real_pin()
    mutate(d)
    f = tmp_path / "c.json"
    f.write_text(json.dumps(d))
    with pytest.raises(E.ClaudeCliUnpinned):
        E.load_claude_cli(f)


def test_a_synthetic_unpinned_file_is_still_refused(tmp_path):
    d = _real_pin()
    d.update(status="unpinned", sha256=None, size_bytes=None, source=None)
    f = tmp_path / "c.json"
    f.write_text(json.dumps(d))
    with pytest.raises(E.ClaudeCliUnpinned):
        E.load_claude_cli(f)


def _pin(**over):
    d = {"schema": 1, "status": "pinned", "version": "2.1.328", "platform": "linux-x64", "sha256": "a" * 64,
         "size_bytes": 300_000_000, "destination": "/opt/aiep/claude", "max_binary_bytes": 400 * 1024**2}
    d.update(over)
    return d


def test_a_complete_pin_loads(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps(_pin()))
    pin = E.load_claude_cli(f)
    assert pin.sha256 == "a" * 64 and pin.size_bytes == 300_000_000 and pin.destination == "/opt/aiep/claude"


@pytest.mark.parametrize("over", [{"status": "unpinned"}, {"sha256": None}, {"sha256": "A" * 64}, {"sha256": "a" * 63},
                                  {"size_bytes": 0}, {"size_bytes": 400 * 1024**2 + 1}, {"size_bytes": True},
                                  {"platform": "darwin-arm64"}, {"destination": "/usr/bin/claude"}, {"version": "latest"}])
def test_incomplete_or_unsafe_pins_are_refused(tmp_path, over):
    f = tmp_path / "c.json"
    f.write_text(json.dumps(_pin(**over)))
    with pytest.raises(E.ClaudeCliUnpinned):
        E.load_claude_cli(f)


@pytest.mark.parametrize("gateway,client", [("1.50", "1.65"), ("1.00", "1.1"), ("0.01", "0.011"), ("2.50", "2.75"), ("0.0001", "0.0002"),
                                           ("100", "110"), ("0.3333", "0.3667")])
def test_the_claude_client_budget_is_a_little_above_the_gateway_budget_so_the_gateway_stays_authoritative(gateway, client):
    # literal expectations (budget x 1.10, rounded UP to 4 decimals), not recomputed with the function under test
    assert E.client_budget_usd(gateway) == client
    assert E.claude_args("claude-sonnet-5-5", gateway)[-1] == client
    assert float(client) > float(gateway)


def test_the_gateway_budget_itself_is_not_changed_by_the_client_margin():
    import inspect as _i
    assert "client_budget_usd" not in _i.getsource(__import__("aiep.lifecycle", fromlist=["x"]))  # the gateway gets the budget as given

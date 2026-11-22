"""m2_run.cli_estimate on REAL Claude Code 2.1.328 stream-json result events (the key order puts "type":"result" far beyond character 40), malformed input, and how the
client estimate stays separate from the gateway's measured cost. Fakes only: no Docker, no API."""
import json

import pytest

from aiep import m2_run
from test_agent_runner import repo, seed  # noqa: F401 (fixtures)
from test_m2_run import fake_record, good_nets, lifecycle, prep, root  # noqa: F401 (fixtures)


def real_result_event(**over):
    """A result event in the REAL key order observed in the first calibration run (sanitised values): "type" appears about 1,900 characters into the line."""
    doc = {"duration_api_ms": 14933, "stop_reason": "end_turn", "session_id": "00000000-0000-0000-0000-000000000000", "total_cost_usd": 0.040225,
           "usage": {"input_tokens": 10, "cache_creation_input_tokens": 10148, "cache_read_input_tokens": 35250, "output_tokens": 1131,
                     "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0}, "service_tier": "standard", "iterations": [{"input_tokens": 2, "output_tokens": 169}] * 3},
           "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 10, "outputTokens": 1131, "cacheReadInputTokens": 35250, "cacheCreationInputTokens": 10148, "costUSD": 0.040225,
                                                "contextWindow": 1000000, "maxOutputTokens": 128000, "provider": "firstParty", "costBasis": "list"}},
           "permission_denials": [], "terminal_reason": "completed", "fast_mode_state": "off", "subagent_stats": {}, "safety_stops": 0, "is_error": False, "num_turns": 6,
           "subtype": "success", "api_error_status": None, "result": "Summary text of the agent's final answer. " * 12, "ttft_ms": 3494, "type": "result",
           "duration_ms": 20751, "uuid": "11111111-1111-1111-1111-111111111111", "result_index": 0}
    doc.update(over)
    return json.dumps(doc, separators=(",", ":"))


def stream(*lines):
    return ("\n".join(lines) + "\n").encode()


def test_the_fixture_really_puts_the_type_far_beyond_character_40():
    line = real_result_event()
    assert line.index('"type":"result"') > 1000 and not line.startswith('{"type"')


def test_a_real_format_result_event_yields_the_client_estimate():
    est = m2_run.cli_estimate(stream('{"type":"system","subtype":"init"}', '{"type":"assistant","message":{}}', real_result_event()))
    assert est["total_cost_usd"] == 0.040225 and est["subtype"] == "success" and est["is_error"] is False and est["num_turns"] == 6 and est["duration_ms"] == 20751
    assert "NOT measured" in est["source"] and "client-side" in est["source"]


def test_type_first_whitespace_and_reordered_events_all_work():
    for line in ('{"type":"result","total_cost_usd":1.5}', '{ "type" : "result" , "total_cost_usd" : 1.5 }', '{"total_cost_usd":1.5,"a":"x","type":"result"}',
                 '{"a":' + json.dumps("y" * 5000) + ',"type":"result","total_cost_usd":1.5}'):
        assert m2_run.cli_estimate(stream(line))["total_cost_usd"] == 1.5, line[:60]


def test_the_last_valid_result_event_wins():
    est = m2_run.cli_estimate(stream(real_result_event(total_cost_usd=0.01), real_result_event(total_cost_usd=0.07)))
    assert est["total_cost_usd"] == 0.07
    est = m2_run.cli_estimate(stream(real_result_event(total_cost_usd=0.01), '{"type":"result", this is torn'))
    assert est["total_cost_usd"] == 0.01  # a later malformed line never discards a good estimate


@pytest.mark.parametrize("raw", [b"", b"\n\n", b"nothing here\n", b"[1,2,3]\n42\n\"result\"\nnull\n", b"{\"type\":\"result\", torn\n", b"\xff\xfe\x00 not utf8 result\n",
                                 b'{"type":"assistant","message":{"content":[{"type":"text","text":"the \\"type\\":\\"result\\" marker appears inside a string"}]}}\n',
                                 b'{"type":"user","message":{"content":[{"type":"tool_result","content":"result"}]}}\n',
                                 b'{"type":"system","subtype":"init","note":"result"}\n'])
def test_malformed_empty_and_unrelated_input_gives_none_not_a_crash(raw):
    assert m2_run.cli_estimate(raw) is None


def test_a_result_event_nested_inside_another_event_is_not_the_final_result():
    nested = json.dumps({"type": "assistant", "message": {"type": "result", "total_cost_usd": 9.9}})
    assert m2_run.cli_estimate(stream(nested)) is None


def test_missing_or_unusable_cost_fields_are_omitted_not_invented():
    est = m2_run.cli_estimate(stream('{"type":"result","subtype":"error_max_turns"}'))
    assert est["subtype"] == "error_max_turns" and "total_cost_usd" not in est
    for bad in ('"1.5"', "true", "null", "NaN", "Infinity", "[1]"):
        e = m2_run.cli_estimate(stream('{"type":"result","total_cost_usd":' + bad + "}"))
        assert e is not None and "total_cost_usd" not in e, bad


def test_the_original_mock_format_still_works():
    assert m2_run.cli_estimate(b'{"type":"result","total_cost_usd":1.5,"extra":"x"}\n')["total_cost_usd"] == 1.5
    assert m2_run.cli_estimate(b"nothing here\n") is None


# ---------------------------------------------------------------- gateway cost and client estimate stay separate; attribution uses the estimate
def test_run_json_keeps_gateway_measured_cost_and_client_estimate_apart_with_a_real_format_result(root):
    p = prep(root)
    res = lifecycle(p)
    res.agent.claude_stdout = stream('{"type":"system","subtype":"init"}', real_result_event(total_cost_usd=0.5)).replace(b"@@", b"")
    ex = m2_run.execute(good_nets(), p, base=root / "runs" / "m2", root=root, run_fn=lambda c, g, a: res)
    run = json.loads((ex.directory / "run.json").read_text())
    assert run["usage_measured"]["computed_usd_upper_bound_basis"] == "0.02"  # the gateway ledger's figure
    assert run["usage_cli_estimate"]["total_cost_usd"] == 0.5 and "NOT measured" in run["usage_cli_estimate"]["source"]  # Claude's, separately labelled
    assert run["budget_attribution"] == {"gateway_budget_exhausted": False, "client_budget_exceeded": False, "terminated_by": None}


def test_budget_attribution_uses_the_client_estimate_when_it_is_available():
    est = m2_run.cli_estimate(stream(real_result_event(subtype="error_max_budget_usd", is_error=True)))
    assert m2_run.budget_attribution(None, est) == {"gateway_budget_exhausted": False, "client_budget_exceeded": True, "terminated_by": "client"}
    assert m2_run.budget_attribution("budget_exhausted", est)["terminated_by"] == "both"
    ok = m2_run.cli_estimate(stream(real_result_event()))
    assert m2_run.budget_attribution("budget_exhausted", ok) == {"gateway_budget_exhausted": True, "client_budget_exceeded": False, "terminated_by": "gateway"}
    assert m2_run.budget_attribution(None, ok)["terminated_by"] is None
    assert m2_run.budget_attribution(None, None)["terminated_by"] is None  # no estimate: the client cause cannot be known (the pre-fix behaviour for real transcripts)

"""In-flight gateway metering: bounded graceful drain, and an explicit marker when it cannot complete."""
import json

import pytest

from aiep import ledger as L
from aiep.lifecycle import LifecycleResult, _finish_gateway
from test_agent_runner import repo, seed  # noqa: F401  (fixtures)
from test_lifecycle import LEDGER_PATH, build, release, run  # noqa: F401  (the full-lifecycle fakes)


def line(**kw):
    return json.dumps(kw) + "\n"


FORWARDING = line(rid="r1", outcome="forwarding", seq=1, endpoint="/v1/messages", requested_max_tokens=64000, stream=True)
FINISHED = line(rid="r1", outcome="ok", seq=2, usage={"input_tokens": 1}, cost_usd_computed="0.1")


# ---------------- ledger.audit ----------------
def test_audit_finds_started_but_unfinished_requests_and_unparseable_lines():
    a = L.audit(FORWARDING)
    assert a["complete"] is False and [u["rid"] for u in a["unresolved"]] == ["r1"] and a["unresolved"][0]["requested_max_tokens"] == 64000
    assert L.audit(FORWARDING + FINISHED)["complete"] is True
    assert L.audit(FORWARDING + FINISHED + "{torn")["complete"] is False  # a torn line could have been a finishing line
    assert L.audit("")["complete"] is True and L.audit(None)["complete"] is False and L.audit(None)["readable"] is False
    # a finishing line of a request that never wrote a 'forwarding' line (e.g. refused earlier) is fine; ids do not cross
    assert L.audit(line(rid="r2", outcome="refused") + FORWARDING)["unresolved"][0]["rid"] == "r1"


# ---------------- stub gateway manager, fake clock ----------------
class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = 0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s
        self.sleeps += 1


class StubGw:
    def __init__(self, ledgers, *, gone_after=None, state="ready", clock=None):
        self.ledgers, self.state, self.errors, self.reads, self.stopped_at = list(ledgers), state, [], 0, None
        self.gone_after, self.clock = gone_after, clock

    def read_ledger(self, *, record_error=True):
        self.reads += 1
        item = self.ledgers[min(self.reads - 1, len(self.ledgers) - 1)]
        return item.encode() if isinstance(item, str) else item

    def process_gone(self):
        return self.gone_after is not None and self.reads >= self.gone_after

    def stop(self):
        if self.state == "new":  # like GatewayManager.stop: never started, nothing to stop
            return
        self.stopped_at = self.clock.t
        self.state = "stopped"

    def final_stop_reason(self):
        return None

    def teardown(self):
        self.state = "removed"
        return True, None


def finish(gw, clock, **kw):
    res = LifecycleResult(status="completed")
    _finish_gateway(gw, res, clock=clock.now, sleep=clock.sleep, drain_timeout_s=kw.get("timeout", 30.0), drain_poll_s=kw.get("poll", 1.0))
    return res


def test_nothing_in_flight_stops_at_once_with_exact_accounting():
    c = Clock()
    gw = StubGw([FORWARDING + FINISHED], clock=c)
    res = finish(gw, c)
    assert c.sleeps == 0 and gw.stopped_at == 0.0
    assert res.accounting["complete"] and res.accounting["exact"] and res.accounting["drain"]["drained"]


def test_an_in_flight_request_that_finishes_during_the_drain_is_recorded_before_the_gateway_is_stopped():
    c = Clock()
    gw = StubGw([FORWARDING, FORWARDING, FORWARDING + FINISHED], clock=c)  # finishes on the third look
    res = finish(gw, c)
    assert c.sleeps == 2 and gw.stopped_at == 2.0  # stopped only AFTER the drain
    assert res.accounting["complete"] and res.accounting["drain"] == {"drained": True, "waited_s": 2.0, "timed_out": False, "timeout_s": 30.0}
    assert L.audit(res.ledger)["complete"]  # the final (post-stop) ledger is what was judged


def test_a_request_that_never_finishes_is_bounded_and_marked_incomplete_never_exact():
    c = Clock()
    gw = StubGw([FORWARDING], clock=c)  # hangs forever
    res = finish(gw, c, timeout=10.0, poll=1.0)
    assert gw.stopped_at == pytest.approx(10.0) and c.sleeps == 10  # bounded by the deadline, not by the request
    acc = res.accounting
    assert acc["complete"] is False and acc["exact"] is False and acc["drain"]["timed_out"] is True
    assert [u["rid"] for u in acc["unresolved"]] == ["r1"] and acc["unresolved_max_output_tokens"] == 64000
    assert "INCOMPLETE" in acc["note"] and "lower bound" in acc["note"]


def test_a_gateway_that_is_already_gone_is_not_waited_for_but_is_still_marked():
    c = Clock()
    gw = StubGw([FORWARDING], gone_after=1, clock=c)
    res = finish(gw, c)
    assert c.sleeps == 0 and res.accounting["complete"] is False and "gone" in res.accounting["drain"]["note"]


def test_an_unreadable_ledger_is_never_reported_as_complete():
    c = Clock()
    gw = StubGw([None], clock=c)
    res = finish(gw, c)
    assert c.sleeps == 0  # waiting cannot help
    assert res.accounting["readable"] is False and res.accounting["complete"] is False and res.accounting["exact"] is False


def test_a_gateway_that_never_started_has_no_accounting_to_audit():
    c = Clock()
    gw = StubGw([FORWARDING], state="new", clock=c)
    assert finish(gw, c).accounting is None


# ---------------- through the real lifecycle (fake Docker) ----------------
def test_full_lifecycle_records_the_unresolved_request_in_the_run_record(tmp_path, seed):
    from aiep.lifecycle import lifecycle_record
    host, gspec, aspec = build(tmp_path, seed)
    host.gw_fs.files[LEDGER_PATH] = FORWARDING.encode()
    res = run(host, gspec, aspec, drain_timeout_s=0.2, drain_poll_s=0.02)
    release(host)
    assert res.status == "completed" and res.gateway_removed
    rec = lifecycle_record(res)
    assert rec["accounting"]["complete"] is False and rec["accounting"]["unresolved"][0]["rid"] == "r1"
    assert "r1" not in json.dumps(rec["ledger"])  # the record carries the verdict, the ledger stays an artifact

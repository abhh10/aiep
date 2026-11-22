"""The runtime-validation harness (scripts/m2_runtime_validation.py): the reaper phase cannot reach another run's containers, and the
expected-cost check is an independently specified literal. No Docker is used."""
import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

from aiep import pricing as PR
from aiep import reaper

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("m2_runtime_validation", ROOT / "scripts" / "m2_runtime_validation.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)

OURS, LIVE_A, LIVE_G, FOREIGN = "a" * 64, "b" * 64, "c" * 64, "d" * 64
NOW = 1_800_000_000.0
FUTURE = lambda: NOW + 6 * 3600  # noqa: E731  (the harness's simulated clock)


class FakeDaemonApi:
    """A daemon with this phase's containers AND a live run's (old-looking, AIEP-labelled, no lease on disk) that must never be touched."""

    def __init__(self):
        self.entries = [
            {"Id": OURS, "Names": ["/aiep.agent.reaptestab12-dead"], "Created": NOW, "State": "running",
             "Labels": {"aiep.run_id": "reaptestab12-dead", "aiep.role": "agent"}},
            {"Id": LIVE_A, "Names": ["/aiep.agent.m2-pytest-A-20261120T010101Z"], "Created": NOW, "State": "running",
             "Labels": {"aiep.run_id": "m2-pytest-A-20261120T010101Z", "aiep.role": "agent"}},
            {"Id": LIVE_G, "Names": ["/aiep.gateway.m2-pytest-A-20261120T010101Z"], "Created": NOW, "State": "running",
             "Labels": {"aiep.run_id": "m2-pytest-A-20261120T010101Z", "aiep.role": "gateway"}},
            # same namespace prefix but NOT created by this phase (an ID collision/mislabel): still out of scope
            {"Id": FOREIGN, "Names": ["/aiep.agent.reaptestab12-x"], "Created": NOW, "State": "running",
             "Labels": {"aiep.run_id": "reaptestab12-x", "aiep.role": "agent"}},
        ]
        self.removed, self.inspected = [], []

    def containers(self, all=False, filters=None):
        return [dict(e) for e in self.entries]

    def inspect_container(self, cid):
        self.inspected.append(cid)
        e = next(x for x in self.entries if x["Id"] == cid)
        return {"Id": cid, "Name": e["Names"][0], "Config": {"Labels": e["Labels"]}}

    def remove_container(self, cid, v=False, force=False):
        self.removed.append(cid)


class Client:
    def __init__(self, api):
        self.api = api


def scoped(api, ids, ns="reaptestab12"):
    sa = H.ScopedReaperApi(api, lambda: set(ids), ns)
    return sa, H.ScopedReaperClient(sa)


def test_the_unscoped_reaper_with_a_simulated_clock_WOULD_delete_a_live_runs_containers(tmp_path):
    # the hazard being fixed: without scoping, the destructive simulated-clock sweep removes everything old-looking, live run included
    api = FakeDaemonApi()
    reaper.reap(Client(api), lease_dir=tmp_path, ttl_s=3 * 3600, execute=True, now=FUTURE)
    assert LIVE_A in api.removed and LIVE_G in api.removed


def test_through_the_scoped_api_only_this_phases_containers_are_listed_inspected_or_removed(tmp_path):
    api = FakeDaemonApi()
    sa, client = scoped(api, {OURS})
    rep = reaper.reap(client, lease_dir=tmp_path, ttl_s=3 * 3600, execute=True, now=FUTURE)
    assert [i.name for i in rep.items] == ["aiep.agent.reaptestab12-dead"]
    assert api.removed == [OURS] and api.inspected == [OURS]
    assert sa.denied == []  # the reaper never even asked about the others: they are invisible


def test_a_direct_attempt_on_an_out_of_scope_container_is_refused_and_recorded():
    api = FakeDaemonApi()
    sa, _ = scoped(api, {OURS})
    for call in (lambda: sa.remove_container(LIVE_A, v=True, force=True), lambda: sa.inspect_container(LIVE_G)):
        with pytest.raises(PermissionError, match="did not create"):
            call()
    assert api.removed == [] and api.inspected == [] and [d[0] for d in sa.denied] == ["remove", "inspect"]


def test_the_namespace_must_match_as_well_as_the_id(tmp_path):
    api = FakeDaemonApi()
    sa, client = scoped(api, {OURS, FOREIGN}, ns="somethingelse")  # right IDs, wrong namespace: invisible
    rep = reaper.reap(client, lease_dir=tmp_path, ttl_s=3 * 3600, execute=True, now=FUTURE)
    assert rep.items == [] and api.removed == []


def test_the_scoped_client_exposes_nothing_but_the_three_calls_the_reaper_needs():
    sa, client = scoped(FakeDaemonApi(), set())
    assert [n for n in dir(sa) if not n.startswith("_") and callable(getattr(sa, n))] == ["containers", "inspect_container", "remove_container"]
    assert not hasattr(client, "containers") and not hasattr(sa, "kill") and not hasattr(sa, "prune_containers")


def test_the_phase_uses_a_unique_namespace_and_the_scoped_client_never_the_raw_one():
    import inspect
    src = inspect.getsource(H.phase_reaper)
    assert "reaper.reap(scoped," in src and "reaper.reap(client" not in src and "uuid.uuid4()" in src
    assert '"sprint-' not in src  # the old fixed, collision-prone names are gone


def test_the_expected_mock_cost_is_an_independent_literal_with_its_arithmetic():
    # worked by hand from the published prices ($2 input, $0.10 cache read, $10 output per MTok), NOT by aiep.pricing
    by_hand = Decimal(100) * 2 / 10**6 + Decimal(10) * Decimal("0.10") / 10**6 + Decimal(20) * 10 / 10**6
    assert by_hand == Decimal("0.000401") == H.MOCK_REQUEST_COST_USD
    # and the implementation under test agrees with it (this is the check that can now actually fail)
    p = PR.load_pricing(ROOT / "config" / "model_pricing.json", "claude-sonnet-5-5")
    assert PR.compute_cost(p, PR.usage_from_api(H.MOCK_USAGE)).usd == H.MOCK_REQUEST_COST_USD


def test_the_harness_no_longer_derives_expectations_from_the_function_it_checks():
    import inspect
    src = inspect.getsource(H)
    assert "exp = sum(" not in src and "one = PR.compute_cost" not in src


def test_phases_written_for_the_superseded_two_network_topology_refuse_to_run(capsys, tmp_path):
    for phase in ("networks", "gateway", "agent", "integration", "lifecycle"):
        assert H.main(["--evidence-dir", str(tmp_path), "--phase", phase]) == 2
        assert "m2_egress_validation.py" in capsys.readouterr().err

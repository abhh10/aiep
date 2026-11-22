import importlib.util
import json
import os
from pathlib import Path

import pytest

from aiep import limits as LIM
from aiep import reaper as R
from aiep.lease import RunLease

REAL = Path(__file__).resolve().parents[1]
spec_ = importlib.util.spec_from_file_location("reap_aiep_containers", REAL / "scripts" / "reap_aiep_containers.py")
drv = importlib.util.module_from_spec(spec_)
spec_.loader.exec_module(drv)

NOW = 1_800_000_000.0
TTL = R.MIN_TTL_S  # 7200
GRACE = R.DEFAULT_LEASE_GRACE_S  # 900
EXPIRY = LIM.LEASE_EXPIRY_S  # 300
HOUR = 3600


def c(cid, name, role="agent", run="r1", age=TTL + 1000, labels=None, state="running", extra=None):
    lab = {"aiep.run_id": run, "aiep.role": role} if labels is None else labels
    return {"Id": cid * 12, "Names": [f"/{name}"], "Labels": lab, "Created": NOW - age, "State": state, **(extra or {})}


class FakeApi:
    def __init__(self, containers, inspect_override=None, remove_errors=(), on_inspect=None):
        self.all = containers
        self.calls, self.removed = [], []
        self.override, self.remove_errors, self.on_inspect = inspect_override or {}, set(remove_errors), on_inspect

    def containers(self, all=False, filters=None):
        self.calls.append(("containers", all, filters))
        want = (filters or {}).get("label", [])
        return [x for x in self.all if all_has(x, want)]

    def inspect_container(self, cid):
        self.calls.append(("inspect", cid))
        if self.on_inspect:
            self.on_inspect(cid)
        e = next(x for x in self.all if x["Id"] == cid)
        base = {"Id": cid, "Name": e["Names"][0], "Config": {"Labels": e["Labels"]}}
        return {**base, **self.override.get(cid, {})}

    def remove_container(self, cid, **kw):
        self.calls.append(("remove", cid, kw))
        if cid in self.remove_errors:
            raise RuntimeError("busy")
        self.removed.append(cid)


def all_has(entry, wanted):
    return all(w in entry["Labels"] for w in wanted)


class Client:
    def __init__(self, api):
        self.api, self.closed = api, 0

    def close(self):
        self.closed += 1


def lease_file(d, run, *, silence=None, beat=None, **over):
    """A lease whose last heartbeat was ``silence`` seconds before NOW (or at ``beat``)."""
    d.mkdir(parents=True, exist_ok=True)
    hb = beat if beat is not None else NOW - silence
    doc = {"schema": 1, "run_id": run, "pid": 4242, "started_at": hb, "heartbeat_at": hb, "beat": 1, **over}
    (d / f"{run}.lease").write_text(json.dumps(doc))


@pytest.fixture
def leases(tmp_path):
    return tmp_path / "leases"


def reap(api, leases, **kw):
    kw.setdefault("ttl_s", TTL)
    return R.reap(Client(api), lease_dir=leases, now=lambda: NOW, **kw)


# ------------------------------------------------------------------ a fresh lease protects a run however old its containers are
@pytest.mark.parametrize("age", [0, 60, TTL - 1, TTL, TTL + 1, 10 * HOUR, 1000 * HOUR])
def test_an_active_run_cannot_be_reaped_even_when_its_containers_are_ancient(leases, age):
    lease_file(leases, "r1", silence=5)
    agent, gw = c("a", "aiep.agent.r1", age=age), c("b", "aiep.gateway.r1", role="gateway", age=age)
    api = FakeApi([agent, gw])
    rep = reap(api, leases, execute=True)
    assert api.removed == [] and not any(x[0] in ("inspect", "remove") for x in api.calls)
    assert all(i.action == "skip" and i.lease == "fresh" and "active lease" in i.reason for i in rep.items)


def test_a_lease_that_is_still_fresh_just_inside_the_expiry_protects(leases):
    lease_file(leases, "r1", silence=EXPIRY - 0.5)
    api = FakeApi([c("a", "aiep.agent.r1", age=100 * HOUR)])
    assert reap(api, leases, execute=True).removed == [] and api.removed == []


# ------------------------------------------------------------------ expired lease: only after expiry + grace
def test_an_expired_lease_is_not_enough_the_grace_period_must_also_pass(leases):
    lease_file(leases, "r1", silence=EXPIRY + 10)
    api = FakeApi([c("a", "aiep.agent.r1")])
    rep = reap(api, leases, execute=True)
    assert api.removed == [] and rep.items[0].lease == "expired" and "grace" in rep.items[0].reason


@pytest.mark.parametrize("silence,removed", [(EXPIRY + GRACE - 1, False), (EXPIRY + GRACE, True), (EXPIRY + GRACE + 1, True),
                                             (10 * HOUR, True)])
def test_the_grace_boundary_is_exact(leases, silence, removed):
    lease_file(leases, "r1", silence=silence)
    api = FakeApi([c("a", "aiep.agent.r1", age=60)])  # the container's own age is irrelevant when a lease exists
    rep = reap(api, leases, execute=True)
    assert (api.removed == ["a" * 12]) is removed and rep.items[0].lease == "expired"


def test_a_young_container_with_a_long_expired_lease_is_reapable_because_the_lease_not_the_age_decides(leases):
    lease_file(leases, "r1", silence=5 * HOUR)
    api = FakeApi([c("a", "aiep.agent.r1", age=10)])
    assert reap(api, leases, execute=True).removed != [] and api.removed == ["a" * 12]


# ------------------------------------------------------------------ missing lease: fall back to the container's age
@pytest.mark.parametrize("age,removed", [(TTL - 1, False), (TTL, True), (TTL + 1, True), (0, False)])
def test_without_a_lease_only_a_container_older_than_the_ttl_is_reapable(leases, age, removed):
    api = FakeApi([c("a", "aiep.agent.r1", age=age)])
    rep = reap(api, leases, execute=True)
    assert (api.removed == ["a" * 12]) is removed and rep.items[0].lease == "missing"


def test_a_released_lease_means_missing_not_alive(leases):
    with RunLease(leases, "r1", interval_s=0):
        pass
    api = FakeApi([c("a", "aiep.agent.r1", age=TTL + 5)])
    assert reap(api, leases, execute=True).items[0].lease == "missing" and api.removed == ["a" * 12]


# ------------------------------------------------------------------ one run, two containers; and two runs
def test_stale_agent_with_an_active_gateway_is_protected_by_the_runs_lease(leases):
    lease_file(leases, "r1", silence=3)
    stale_agent = c("a", "aiep.agent.r1", age=20 * HOUR, state="exited")
    live_gw = c("b", "aiep.gateway.r1", role="gateway", age=20 * HOUR, state="running")
    api = FakeApi([stale_agent, live_gw])
    reap(api, leases, execute=True)
    assert api.removed == []  # the run is alive: its containers are released together, by the orchestrator


def test_stale_gateway_with_an_active_agent_is_protected_by_the_runs_lease(leases):
    lease_file(leases, "r1", silence=3)
    live_agent = c("a", "aiep.agent.r1", age=20 * HOUR, state="running")
    stale_gw = c("b", "aiep.gateway.r1", role="gateway", age=20 * HOUR, state="exited")
    api = FakeApi([live_agent, stale_gw])
    reap(api, leases, execute=True)
    assert api.removed == []


def test_leases_are_per_run_an_abandoned_run_is_reaped_while_a_live_run_next_to_it_is_not(leases):
    lease_file(leases, "live", silence=2)
    lease_file(leases, "dead", silence=3 * HOUR)
    live_a, live_g = c("a", "aiep.agent.live", run="live"), c("b", "aiep.gateway.live", role="gateway", run="live")
    dead_a, dead_g = c("c", "aiep.agent.dead", run="dead"), c("d", "aiep.gateway.dead", role="gateway", run="dead")
    api = FakeApi([live_a, live_g, dead_a, dead_g])
    reap(api, leases, execute=True)
    assert sorted(api.removed) == sorted([dead_a["Id"], dead_g["Id"]])


def test_a_fresh_lease_for_another_run_does_not_protect_a_run_without_one(leases):
    lease_file(leases, "other", silence=1)
    api = FakeApi([c("a", "aiep.agent.r1", age=TTL + 1)])
    reap(api, leases, execute=True)
    assert api.removed == ["a" * 12]


# ------------------------------------------------------------------ orchestrator interruption
def test_an_interrupted_orchestrator_becomes_reapable_only_after_expiry_plus_grace(leases):
    run = RunLease(leases, "r1", interval_s=0, clock=lambda: NOW - 7 * HOUR)  # heartbeat at that time, then the process dies
    run.acquire()
    agent = c("a", "aiep.agent.r1", age=7 * HOUR)
    api = FakeApi([agent])
    assert reap(api, leases, execute=True).removed != [] and api.removed == [agent["Id"]]  # 7 h of silence: abandoned
    api2 = FakeApi([agent])
    last_beat = NOW - 10
    (leases / "r1.lease").write_text(json.dumps({"schema": 1, "run_id": "r1", "pid": 1, "started_at": 1, "heartbeat_at": last_beat, "beat": 9}))
    assert reap(api2, leases, execute=True).removed == [] and api2.removed == []  # it was beating 10 s ago


# ------------------------------------------------------------------ fail closed
@pytest.mark.parametrize("content", ["garbage", "", "{}", '{"schema":1,"run_id":"r1","heartbeat_at":"soon"}',
                                     '{"schema":1,"run_id":"other","heartbeat_at":%s}' % (NOW - 10 * HOUR),
                                     '{"schema":1,"run_id":"r1","heartbeat_at":%s}' % (NOW + 10 * HOUR)])
def test_an_unreadable_or_untrustworthy_lease_deletes_nothing(leases, content):
    leases.mkdir()
    (leases / "r1.lease").write_text(content)
    api = FakeApi([c("a", "aiep.agent.r1", age=100 * HOUR)])
    rep = reap(api, leases, execute=True)
    assert api.removed == [] and rep.items[0].lease == "invalid" and "failing closed" in rep.items[0].reason
    assert not any(x[0] in ("inspect", "remove") for x in api.calls)


def test_a_lease_directory_that_cannot_be_read_deletes_nothing(leases):
    lease_file(leases, "r1", silence=10 * HOUR)
    os.chmod(leases, 0)
    try:
        if os.geteuid() == 0:
            pytest.skip("root can read anything")
        api = FakeApi([c("a", "aiep.agent.r1", age=100 * HOUR), c("b", "aiep.gateway.r1", role="gateway", age=100 * HOUR)])
        reap(api, leases, execute=True)
        assert api.removed == []
    finally:
        os.chmod(leases, 0o700)


def test_a_lease_symlink_or_directory_in_its_place_deletes_nothing(leases):
    leases.mkdir()
    (leases / "r1.lease").mkdir()
    api = FakeApi([c("a", "aiep.agent.r1", age=100 * HOUR)])
    reap(api, leases, execute=True)
    assert api.removed == []


def test_an_unexpected_error_while_reading_a_lease_aborts_the_pass_without_deleting(leases, monkeypatch):
    monkeypatch.setattr(R, "read_lease_status", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    api = FakeApi([c("a", "aiep.agent.r1", age=100 * HOUR)])
    with pytest.raises(RuntimeError, match="bug"):
        reap(api, leases, execute=True)
    assert api.removed == []


def test_a_lease_that_comes_back_to_life_between_listing_and_removal_is_respected(leases):
    lease_file(leases, "r1", silence=5 * HOUR)  # looks abandoned when listed

    def orchestrator_resumes(cid):
        lease_file(leases, "r1", silence=0)

    api = FakeApi([c("a", "aiep.agent.r1")], on_inspect=orchestrator_resumes)
    rep = reap(api, leases, execute=True)
    assert api.removed == [] and rep.items[0].action == "skip" and "lease changed before removal" in rep.items[0].reason


# ------------------------------------------------------------------ strict AIEP shape (unchanged) and unrelated containers
OLD_AGENT = c("a", "aiep.agent.r1")
OLD_GW = c("b", "aiep.gateway.r1", role="gateway")
OLD_SEED = c("c", "aiep.seedsrc.r2", role="seed_source", run="r2")


@pytest.mark.parametrize("victim", [
    c("e", "postgres", labels={}),
    c("e", "kafka", labels={"com.docker.compose.project": "x"}),
    c("e", "postgres", labels={"aiep.run_id": "r1", "aiep.role": "agent"}),
    c("e", "aiep.agent.r1", role="evaluator"),
    c("e", "aiep.agent.r1", role="admin"),
    c("e", "aiep.agent.other", run="r1"),
    c("e", "aiep.gateway.r1", role="agent"),
    c("e", "aiep.agent.r1", labels={"aiep.run_id": "../x", "aiep.role": "agent"}),
    c("e", "aiep.agent.r1", labels={"aiep.role": "agent"}),
])
def test_unrelated_or_misshapen_containers_are_never_removed_even_with_an_expired_lease(leases, victim):
    lease_file(leases, "r1", silence=100 * HOUR)
    api = FakeApi([victim])
    rep = reap(api, leases, execute=True)
    assert api.removed == [] and all(i.action == "skip" for i in rep.items) and not any(x[0] == "remove" for x in api.calls)


def test_the_list_is_filtered_by_the_aiep_label_so_other_containers_are_not_even_inspected(leases):
    other = c("f", "trino", labels={})
    api = FakeApi([other, OLD_AGENT])
    reap(api, leases, execute=True)
    assert api.calls[0] == ("containers", True, {"label": ["aiep.run_id"]})
    assert all(x[1] != other["Id"] for x in api.calls if x[0] in ("inspect", "remove"))


# ------------------------------------------------------------------ dry run vs explicit execution
def test_dry_run_is_the_default_and_removes_nothing_even_when_everything_is_reapable(leases):
    api = FakeApi([OLD_AGENT, OLD_GW, OLD_SEED])
    rep = reap(api, leases)
    assert rep.executed is False and [i.action for i in rep.items] == ["remove"] * 3
    assert api.removed == [] and not any(x[0] in ("remove", "inspect") for x in api.calls)


def test_execute_removes_exactly_the_reapable_containers_by_id_with_force_and_volumes(leases):
    fresh = c("d", "aiep.agent.r3", run="r3", age=60)
    api = FakeApi([OLD_AGENT, OLD_GW, OLD_SEED, fresh])
    rep = reap(api, leases, execute=True)
    assert sorted(api.removed) == sorted([OLD_AGENT["Id"], OLD_GW["Id"], OLD_SEED["Id"]]) and fresh["Id"] not in api.removed
    assert all(x[2] == {"v": True, "force": True} for x in api.calls if x[0] == "remove")
    assert "younger" in next(i for i in rep.items if i.container_id == fresh["Id"]).reason


def test_a_container_that_changed_between_listing_and_removal_is_skipped(leases):
    api = FakeApi([OLD_AGENT], inspect_override={OLD_AGENT["Id"]: {"Name": "/postgres"}})
    rep = reap(api, leases, execute=True)
    assert api.removed == [] and "changed since listing" in rep.items[0].reason
    api = FakeApi([OLD_AGENT], inspect_override={OLD_AGENT["Id"]: {"Config": {"Labels": {}}}})
    assert reap(api, leases, execute=True).removed == []


def test_one_failure_is_reported_and_does_not_stop_the_rest(leases):
    api = FakeApi([OLD_AGENT, OLD_GW], remove_errors=[OLD_AGENT["Id"]])
    rep = reap(api, leases, execute=True)
    assert api.removed == [OLD_GW["Id"]] and len(rep.errors) == 1 and "busy" in rep.errors[0].error


def test_unreadable_creation_time_without_a_lease_is_skipped(leases):
    bad = c("a", "aiep.agent.r1")
    bad["Created"] = "yesterday"
    api = FakeApi([bad])
    assert reap(api, leases, execute=True).removed == [] and api.removed == []


def test_only_containers_are_ever_touched(leases):
    api = FakeApi([OLD_AGENT])
    reap(api, leases, execute=True)
    assert {x[0] for x in api.calls} <= {"containers", "inspect", "remove"}


# ------------------------------------------------------------------ limits
@pytest.mark.parametrize("ttl", [0, 1, R.MIN_TTL_S - 1, 900, 1800, -5, 7200.0, True, None, "7200"])
def test_a_short_or_malformed_ttl_is_refused(leases, ttl):
    api = FakeApi([OLD_AGENT])
    with pytest.raises(ValueError):
        reap(api, leases, ttl_s=ttl, execute=True)
    assert api.calls == []


@pytest.mark.parametrize("grace", [0, 1, R.MIN_LEASE_GRACE_S - 1, -1, 900.0, True, None])
def test_a_short_or_malformed_lease_grace_is_refused(leases, grace):
    api = FakeApi([OLD_AGENT])
    with pytest.raises(ValueError):
        reap(api, leases, lease_grace_s=grace, execute=True)
    assert api.calls == []


def test_the_minimum_ttl_exceeds_the_longest_legitimate_run():
    from aiep.limits import MAX_WALL_CLOCK_S, SETUP_TEARDOWN_MARGIN_S
    assert R.MIN_TTL_S == MAX_WALL_CLOCK_S + SETUP_TEARDOWN_MARGIN_S and R.MIN_TTL_S > 2 * LIM.DEFAULT_WALL_CLOCK_S
    assert R.DEFAULT_TTL_S >= R.MIN_TTL_S


# ------------------------------------------------------------------ the script
def run_script(argv, api, leases, **kw):
    client = Client(api)
    rc = drv.main(argv, client_factory=lambda: client, now=lambda: NOW, lease_dir=leases)
    return rc, client


def test_script_defaults_to_dry_run_and_closes_the_client(leases, capsys):
    api = FakeApi([OLD_AGENT])
    rc, client = run_script([], api, leases)
    out = capsys.readouterr().out
    assert rc == 0 and "DRY RUN" in out and api.removed == [] and client.closed == 1 and "lease missing" in out


def test_script_execute_removes_and_closes_the_client(leases, capsys):
    api = FakeApi([OLD_AGENT])
    rc, client = run_script(["--execute", "--ttl-seconds", "7200"], api, leases)
    assert rc == 0 and api.removed == [OLD_AGENT["Id"]] and client.closed == 1 and "REMOVED" in capsys.readouterr().out


def test_script_never_removes_a_live_run_even_with_execute(leases, capsys):
    lease_file(leases, "r1", silence=1)
    api = FakeApi([c("a", "aiep.agent.r1", age=50 * HOUR)])
    rc, client = run_script(["--execute"], api, leases)
    out = capsys.readouterr().out
    assert rc == 0 and api.removed == [] and "lease fresh" in out and "active lease" in out


def test_script_reports_errors_with_a_nonzero_exit(leases, capsys):
    api = FakeApi([OLD_AGENT], remove_errors=[OLD_AGENT["Id"]])
    rc, _ = run_script(["--execute", "--ttl-seconds", "7200"], api, leases)
    assert rc == 1 and "ERROR" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--ttl-seconds", "10"], ["--ttl-seconds", "900"], ["--lease-grace-seconds", "10"]])
def test_script_refuses_short_limits_before_any_docker_call(argv, leases):
    built = []
    assert drv.main(argv, client_factory=lambda: built.append(1), lease_dir=leases) == 2 and built == []


def test_the_scripts_default_lease_dir_is_the_one_the_orchestrator_uses():
    assert drv.LEASE_DIR == REAL / ".cache" / "leases"

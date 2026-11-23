"""Replacement of a task that fails a genuine, deterministic PREPARATION GATE (pre-registered in the E1 definition: "fails the preparation gate"), through the real batch.

Only a typed gate failure (``aiep.preparation_gate``) may replace a task. Transient and environmental failures halt and never replace. The replacement follows the registered order,
keeps the block's pair, stage, repetition and arm order, is verified against the dataset BEFORE it is recorded, and is written as a hash-chained record that can never silently change.
Everything here uses mocked preparation failures: no Docker, no network, no task is actually prepared.
"""
import copy
import json
import tarfile
from pathlib import Path

import pytest

from aiep import m3_batch as mb
from aiep.experiment_def import ExperimentError, apply_replacement
from aiep.image_digests import DigestError
from aiep.m3_steps import RealSteps, StepError
from aiep.preparation_gate import PERMANENT_CODES, PreparationGateFailure, gate_failure_of
from aiep.seed import SeedError, SeedGateError, parse_seed_archive
from aiep.seed_source import SeedArchiveTooLarge, SeedSourceError, read_bounded
from aiep.task_setup import IdentityMismatch, SeedLineageMismatch, SetupRefused, _identity_error
from test_e1_exploratory import D13933, D15851, REQ, SPHINX, SY22714, SYMPY, Fake as E1Fake, go, locked, plain_writer, short  # noqa: F401 (plain_writer is an autouse fixture)
from test_m3_integration import FULL, World, make_def
from test_seed import good_entries, make_tar
from test_seed import header_only_tar

MIB = 1024**2
PREPARED_FIRST = "django__django-16408"  # the first entry of E1's registry that keeps the per-repository cap when a django task is replaced
CHAIN = ["django__django-16408", "pylint-dev__pylint-6506", "pytest-dev__pytest-7168", "sphinx-doc__sphinx-7686"]  # E1's registry order, with the caps applied while D13933's block keeps failing

GATE_FAILURES = {
    "seed_archive_over_limit": lambda: SeedArchiveTooLarge("archive exceeds the 134217728-byte limit; refused (not truncated)"),
    "seed_file_over_limit": lambda: SeedGateError("'x/pack' is larger than the per-file limit", "seed_file_over_limit"),
    "seed_content_over_limit": lambda: SeedGateError("archive exceeds the total size limit", "seed_content_over_limit"),
    "seed_entries_over_limit": lambda: SeedGateError("archive has too many entries", "seed_entries_over_limit"),
    "seed_unsafe_content": lambda: SeedGateError("'x' is a hard link; refused", "seed_unsafe_content"),
    "seed_lineage_invalid": lambda: SeedLineageMismatch("the seed's HEAD aaaa has parent bbbb, not the dataset base_commit cccc; nothing was pinned"),
    "identity_mismatch": lambda: IdentityMismatch("the image's account is not the configured one: uid 0"),
}


class PFake(E1Fake):
    """The E1 batch fake, with scripted preparation failures per task and a record of every slot identity it was asked to run."""

    def __init__(self, *, fail=None, fail_when=None, outcome=True, **kw):
        super().__init__(**kw)
        self.fail, self.fail_when, self.outcome, self.idents = dict(fail or {}), fail_when, outcome, []

    def prepare_task(self, t):
        exc = self.fail.get(t) or (self.fail_when(t) if self.fail_when else None)
        if exc is not None:
            self.log.append(("prepare_failed", t))
            raise (exc() if callable(exc) else exc)
        return super().prepare_task(t)

    def run_arm(self, t, arm, ident, budget):
        self.idents.append(dict(ident))
        return super().run_arm(t, arm, ident, budget)

    def grade_run(self, t, rd, slot):
        return {"valid": True, "resolved": self.outcome}

    def build_row(self, *a, **k):
        row = super().build_row(*a, **k)
        row["resolved"] = self.outcome
        return row

    def prepared_calls(self):
        return [x[1] for x in self.log if x[0] == "prepare"]


def d20():
    return locked(cap="20")


def state(tmp_path, name="s.json"):
    return json.loads((tmp_path / name).read_text())


def approvals(d, **kw):
    kw.setdefault("pulls", True)
    kw.setdefault("controls", True)
    return mb.Approvals(spend_usd=d["limits"]["max_total_computed_usd"], **kw)


def run(tmp_path, fake, d, name="s.json", **ap):
    return mb.run_pilot(d, fake, state_path=tmp_path / name, results_path=tmp_path / "r.jsonl", approvals=approvals(d, **ap), execute=True)


# ---------------------------------------------------------------- which failures qualify
def test_the_permanent_codes_are_exactly_the_documented_seven():
    assert PERMANENT_CODES == frozenset(GATE_FAILURES)
    assert all(issubclass(type(f()), PreparationGateFailure) for f in GATE_FAILURES.values())


@pytest.mark.parametrize("code", sorted(GATE_FAILURES))
def test_every_permanent_gate_failure_selects_the_first_registered_replacement(tmp_path, code):
    d = d20()
    f = PFake(fail={D13933: GATE_FAILURES[code]}, cost="0.1")
    st = go(tmp_path, f, d)
    (rec,) = st["replacements"]
    assert rec["replaced"] == D13933 and rec["replacement"] == PREPARED_FIRST and rec["trigger"] == "preparation_gate"
    assert rec["failure"]["code"] == code and rec["failure"]["exception"] and rec["reasons"] and code in rec["reasons"][0]
    assert st["status"] == "complete_with_unrun_stages" and not st["halt"]


def test_the_replacement_follows_the_registered_order_and_records_why_earlier_entries_were_passed_over(tmp_path):
    d = d20()
    st = go(tmp_path, PFake(fail={D13933: GATE_FAILURES["seed_archive_over_limit"]}, cost="0.1"), d)
    (rec,) = st["replacements"]
    registry = [r["instance_id"] for r in d["preregistration"]["replacements"]]
    assert registry.index(rec["replacement"]) == rec["registry_index"] == 4
    assert [(s["instance_id"], s["reason"]) for s in rec["skipped_before_selection"]] == [(i, "max_per_repo") for i in registry[:4]]  # four SymPy entries, the per-repository cap
    assert rec["uses_ab_outcomes"] is False and rec["order_rule"].startswith("the first unused entry")


@pytest.mark.parametrize("exc", [
    lambda: RuntimeError("Cannot connect to the Docker daemon at unix:///var/run/docker.sock"),
    lambda: OSError(28, "No space left on device"),
    lambda: StepError("django__django-13933: 10.00 GiB free, 16 GiB required before a pull (the start gate is never lowered); nothing was pulled"),
    lambda: DigestError("the index could not be fetched from the registry (URLError)"),
    lambda: SeedSourceError("reading the archive stream failed: ConnectionResetError"),
    lambda: SetupRefused("the seed's HEAD and parent could not be checked (CalledProcessError); nothing was pinned"),
    lambda: SeedError("not a readable tar archive: unexpected end of data"),
    lambda: tarfile.ReadError("unexpected end of data"),
    lambda: _identity_error(["identity probe failed"], probe_incomplete=True),
    lambda: type("Unknown", (PreparationGateFailure,), {"code": "some_future_code"})("a gate failure whose code is not registered"),
])
def test_transient_and_environmental_failures_halt_and_never_replace(tmp_path, exc):
    d = d20()
    f = PFake(fail={SYMPY: exc}, cost="0.1")
    st = go(tmp_path, f, d)
    assert st["status"] == "halted" and st["halt"]["id"] == "orchestrator_error"
    assert st["replacements"] == [] and f.runs() == []
    assert any(e["event"] == "preparation_error_not_replaceable" and e["task"] == SYMPY for e in st["events"])  # the refusal to replace is itself on record
    assert not any(e["event"] == "replacement" for e in st["events"])


def test_missing_pull_approval_halts_without_replacement_or_preparation(tmp_path):
    d = d20()
    f = PFake(cost="0.1")
    st = run(tmp_path, f, d, pulls=False)
    assert st["halt"]["id"] == "task_not_prepared" and st["replacements"] == [] and f.prepared_calls() == [] and f.runs() == []


def test_a_disk_space_shortage_halts_without_replacement(tmp_path):
    f = PFake(free=10.0, cost="0.1")
    st = go(tmp_path, f, d20())
    assert st["status"] == "halted" and st["halt"]["id"].startswith("disk") and st["replacements"] == [] and f.prepared_calls() == []


# ---------------------------------------------------------------- what the replacement keeps
def test_the_replacement_keeps_pair_id_stage_repetition_and_arm_order(tmp_path):
    d = d20()
    f = PFake(fail={D13933: GATE_FAILURES["seed_file_over_limit"]}, cost="0.1")
    st = go(tmp_path, f, d)
    (rec,) = st["replacements"]
    sched = [s for s in d["design"]["schedule"] if s["task_id"] == D13933]
    ours = [i for i in f.idents if i["task_id"] == PREPARED_FIRST]
    assert [s["arm"] for s in sched] == ["B", "A"] and rec["first_arm"] == "B" and rec["arm_order"] == ["B", "A"]  # django-13933 is B-first in E1
    assert [i["arm"] for i in ours] == ["B", "A"]
    assert {i["pair_id"] for i in ours} == {sched[0]["pair_id"]} == {rec["scheduled_pair_id"]} and D13933 in sched[0]["pair_id"]  # the REGISTERED pair id, naming the original pair
    assert [i["order_index"] for i in ours] == [s["order_index"] for s in sched] and {i["repetition"] for i in ours} == {1}
    assert rec["stage"] == 1 and sched[0]["stage"] == 1 and rec["repetition"] == 1
    assert all(i["slot_id"].split("/")[1] == PREPARED_FIRST for i in ours)  # task_id and slot_id name what actually ran
    assert not any(i["task_id"] == D13933 for i in f.idents)  # the replaced task never runs


def test_a_replacement_in_stage_1_never_starts_stage_2(tmp_path):
    f = PFake(fail={D13933: GATE_FAILURES["seed_file_over_limit"]}, cost="0.1")
    st = go(tmp_path, f, d20())
    assert not any(x in " ".join(f.runs()) for x in (SPHINX, REQ)) and {n["reason"] for n in st["not_run"]} == {"stage_not_activated"}


def test_a_task_already_used_or_selected_can_never_be_chosen_again(tmp_path):
    d = d20()
    f = PFake(fail={D13933: GATE_FAILURES["seed_archive_over_limit"], D15851: GATE_FAILURES["seed_archive_over_limit"]}, cost="0.1")
    st = go(tmp_path, f, d)
    first, second = st["replacements"]
    assert first["replacement"] == PREPARED_FIRST and second["replacement"] == "pylint-dev__pylint-6506"
    assert (PREPARED_FIRST, "already_used") in [(s["instance_id"], s["reason"]) for s in second["skipped_before_selection"]]
    selected = {t["instance_id"] for t in d["preregistration"]["tasks"]}
    assert len({first["replacement"], second["replacement"]}) == 2 and not ({first["replacement"], second["replacement"]} & selected)
    doc = copy.deepcopy(d)  # and at the selection function itself: a registry entry that equals a selected task is never returned
    doc["preregistration"]["replacements"][0]["instance_id"] = SYMPY
    out = apply_replacement(doc, D13933)
    assert out["replacement"] != SYMPY and {"instance_id": SYMPY, "reason": "already_a_selected_task"} in out["skipped"]


# ---------------------------------------------------------------- determinism, no A/B outcomes, no silent change
def test_selection_is_deterministic_and_never_depends_on_outcomes_or_cost(tmp_path):
    recs = []
    for i, (outcome, cost) in enumerate([(True, "0.05"), (False, "0.9"), (True, "0.3")]):
        sub = tmp_path / f"r{i}"
        sub.mkdir()
        st = go(sub, PFake(fail={D15851: GATE_FAILURES["seed_file_over_limit"]}, outcome=outcome, cost=cost), d20())  # the failure comes AFTER one complete scored pair
        recs.append(st["replacements"][0])
    assert len({r["record_sha256"] for r in recs}) == 1 and all(r["replacement"] == PREPARED_FIRST for r in recs)
    assert all(r["uses_ab_outcomes"] is False for r in recs)


def test_repeated_invocation_keeps_the_recorded_replacement(tmp_path):
    d = d20()
    f1 = PFake(fail={SYMPY: GATE_FAILURES["seed_file_over_limit"]}, cost="0.1")
    st1 = run(tmp_path, f1, d, controls=False)  # the FIRST block fails its gate: the replacement is recorded, then the batch halts because the controls were not approved
    assert st1["halt"]["id"] == "controls_not_approved" and len(st1["replacements"]) == 1 and st1["replacements"][0]["replacement"] == "sympy__sympy-13043"
    before = copy.deepcopy(st1["replacements"])
    f2 = PFake(fail={SYMPY: GATE_FAILURES["seed_file_over_limit"]}, cost="0.1")
    st2 = run(tmp_path, f2, d, acknowledge_halt="controls_not_approved")
    assert st2["replacements"] == before and st2["blocks"][SYMPY]["task_id"] == "sympy__sympy-13043"  # the same record, byte for byte, and the same task
    assert SYMPY not in f2.prepared_calls() and ("prepare_failed", SYMPY) not in f2.log  # the failed task is never attempted again


def tampered(tmp_path, d, mutate, name="t.json"):
    f = PFake(fail={D13933: GATE_FAILURES["seed_archive_over_limit"], D15851: GATE_FAILURES["seed_archive_over_limit"]}, cost="0.1")
    go(tmp_path, f, d, name="base.json")
    st = state(tmp_path, "base.json")
    mb.verify_replacement_history(d, st)  # the untouched history is valid
    mutate(st)
    (tmp_path / name).write_text(json.dumps(st))
    with pytest.raises(mb.BatchHalt, match="replacement_history_invalid"):
        mb.run_pilot(d, PFake(), state_path=tmp_path / name, results_path=tmp_path / "r2.jsonl", approvals=approvals(d), execute=True)


def reseal(st):
    prev = mb.GENESIS_SHA
    for r in st["replacements"]:
        r["prev_record_sha256"] = prev
        r["record_sha256"] = mb._record_digest(r)
        prev = r["record_sha256"]


@pytest.mark.parametrize("name,mutate", [
    ("changed replacement id", lambda st: st["replacements"][0].__setitem__("replacement", "sympy__sympy-22005")),
    ("removed hash", lambda st: st["replacements"][0].pop("record_sha256")),
    ("removed record", lambda st: st["replacements"].pop(0)),
    ("reordered records", lambda st: st["replacements"].reverse()),
    ("block silently reverted", lambda st: st["blocks"][D13933].__setitem__("task_id", D13933)),
    ("schema marker removed", lambda st: st.__setitem__("replacement_schema", 1)),
    ("FORGED but self-consistent: not in the registry", lambda st: (st["replacements"][0].__setitem__("replacement", "django__django-99999"), reseal(st))),
    ("FORGED but self-consistent: an already selected task", lambda st: (st["replacements"][0].__setitem__("replacement", SYMPY), reseal(st))),
])
def test_a_recorded_replacement_cannot_silently_change(tmp_path, name, mutate):
    tampered(tmp_path, d20(), mutate)


def test_the_history_halt_cannot_be_acknowledged():
    assert "replacement_history_invalid" in mb.NOT_ACKNOWLEDGEABLE


# ---------------------------------------------------------------- the replacement's own identity and fallback
@pytest.mark.parametrize("name,verify", [
    ("wrong base commit", lambda t, r: {"instance_id": t, "repo": r, "base_commit": "not-a-commit"}),
    ("wrong repository", lambda t, r: {"instance_id": t, "repo": "other/other", "base_commit": "c" * 40}),
    ("wrong instance", lambda t, r: {"instance_id": "x__x-1", "repo": r, "base_commit": "c" * 40}),
    ("verification raises", lambda t, r: (_ for _ in ()).throw(StepError("no dataset record"))),
])
def test_a_replacement_whose_identity_cannot_be_verified_halts_and_is_not_recorded(tmp_path, name, verify):
    f = PFake(fail={D13933: GATE_FAILURES["seed_file_over_limit"]}, cost="0.1")
    f.verify_task_identity = verify
    st = go(tmp_path, f, d20())
    assert st["halt"]["id"] == "replacement_identity_invalid" and st["replacements"] == [] and ("run", f"e1-exploratory-1/{PREPARED_FIRST}/B/r1") not in f.log


def test_a_replacement_whose_registered_image_is_not_the_published_image_halts_and_is_not_recorded(tmp_path):
    from test_e1_exploratory import rehash
    d = d20()
    d["preregistration"]["replacements"][4]["image"] = "swebench/sweb.eval.x86_64.django_1776_django-16408:v2"  # the registry entry that would be chosen
    f = PFake(fail={D13933: GATE_FAILURES["seed_file_over_limit"]}, cost="0.1")
    st = go(tmp_path, f, rehash(d))
    assert st["halt"]["id"] == "replacement_identity_invalid" and "published image" in st["halt"]["detail"] and st["replacements"] == []


def test_steps_that_cannot_verify_identity_never_record_a_replacement(tmp_path):
    class NoVerify(PFake):
        verify_task_identity = None
    f = NoVerify(fail={D13933: GATE_FAILURES["seed_file_over_limit"]}, cost="0.1")
    st = go(tmp_path, f, d20())
    assert st["halt"]["id"] == "replacement_identity_unverifiable" and st["replacements"] == []


def test_a_replacement_that_also_fails_its_gate_follows_the_registered_order_then_halts_when_exhausted(tmp_path):
    d = d20()
    registry = {r["instance_id"] for r in d["preregistration"]["replacements"]}
    f = PFake(fail_when=lambda t: GATE_FAILURES["seed_file_over_limit"] if (t == D13933 or t in registry) else None, cost="0.1")
    st = go(tmp_path, f, d)
    assert st["status"] == "halted" and st["halt"]["id"] == "control_replacement_exhausted"  # the pre-registered stop: nothing is invented
    recs = st["replacements"]
    assert [r["replacement"] for r in recs] == CHAIN and [r["replaced"] for r in recs] == [D13933, *CHAIN[:-1]]  # each failure moves down the SAME registry, in order
    assert all(r["block"] == D13933 and r["scheduled_pair_id"] == recs[0]["scheduled_pair_id"] for r in recs)
    mb.verify_replacement_history(d, st)
    assert not any(i["task_id"] in (D13933, *CHAIN) for i in f.idents)  # none of them ever ran


def test_the_registered_definition_already_names_this_trigger_and_this_stop():
    d = d20()
    rules = " ".join(d["preregistration"]["replacement_rules"])
    assert "fails the preparation gate" in rules and "control_replacement_exhausted" in rules and "never because of an agent outcome" in rules
    assert "control_replacement_exhausted" in {c["id"] for c in d["stop_conditions"]}


# ---------------------------------------------------------------- existing triggers are unchanged
def test_control_failures_and_incompatible_decisions_still_replace_with_their_own_trigger(tmp_path):
    d = d20()
    st = go(tmp_path, PFake(bad_controls=[(D13933, "gold")], cost="0.1"), d)
    assert st["replacements"][0]["trigger"] == "control" and st["replacements"][0]["replaced"] == D13933
    sub = tmp_path / "compat"
    sub.mkdir()
    mb.record_compatibility_decision(d, sub / "s.json", REQ, compatible=False, evidence="the tests need a network", decided_by="operator")
    f = PFake(cost="0.1")
    st2 = go(sub, f, d, stages=(1, 2), decide=False)
    assert st2["replacements"][0]["trigger"] == "compatibility" and st2["replacements"][0]["replaced"] == REQ
    mb.verify_replacement_history(d, st2)


# ---------------------------------------------------------------- partial preparation through the REAL steps
def test_the_failed_task_keeps_its_partial_pins_and_never_becomes_runnable(tmp_path):
    world = World(tmp_path)
    d = make_def(8)
    failed = d["design"]["schedule"][0]["task_id"]
    snaps = {}

    def extract(c, root, task, *, free_bytes):
        if task == failed:
            snaps["pin"] = json.loads((root / "config" / "evaluator_images.json").read_text())["images"][task]  # the pin written just before the seed step failed
            raise SeedArchiveTooLarge("archive exceeds the 134217728-byte limit; refused (not truncated)")
        from aiep import task_pins as tp
        tp.add_seed_pin(root, task, directory="d", sha256="c" * 64, head="d" * 40, entries=3, image_id=world.img)

    steps = RealSteps(world.deps(d, extract_seed=extract))
    st = mb.run_pilot(d, steps, state_path=tmp_path / "s.json", results_path=tmp_path / "r.jsonl", approvals=FULL(d["limits"]["max_total_computed_usd"]), execute=True)
    assert st["status"] == "complete", st["halt"]
    (rec,) = st["replacements"]
    assert rec["replaced"] == failed and rec["trigger"] == "preparation_gate" and rec["failure"]["code"] == "seed_archive_over_limit"
    state_at_failure = rec["failed_task_state"]
    assert state_at_failure["image_pinned"] is True and state_at_failure["seed_pinned"] is False and state_at_failure["runnable"] is False  # partial state, recorded
    assert json.loads((world.root / "config" / "evaluator_images.json").read_text())["images"][failed] == snaps["pin"]  # its pin was neither deleted, rewritten nor unpinned afterwards
    assert steps.task_prepared(failed) is False and "seed" in steps.unprepared_reason(failed)
    assert failed not in st["blocks"][failed]["pulled"] and st["blocks"][failed]["task_id"] == rec["replacement"]
    assert all(x["task_id"] != failed for x in json.loads("[" + ",".join((tmp_path / "r.jsonl").read_text().splitlines()) + "]"))


def test_the_replacement_record_never_contains_hidden_dataset_fields(tmp_path):
    world = World(tmp_path)
    d = make_def(8)
    failed = d["design"]["schedule"][0]["task_id"]
    repo_of = {t["instance_id"]: t["repo"] for t in d["preregistration"]["tasks"] + d["preregistration"]["replacements"]}
    hidden = {"FAIL_TO_PASS": ["SECRET_TEST_ID"], "PASS_TO_PASS": ["SECRET_TEST_ID_2"], "test_patch": "SECRET_PATCH", "patch": "SECRET_GOLD"}
    record = lambda t: {"instance_id": t, "repo": repo_of[t], "base_commit": "c" * 40, **hidden}

    def extract(c, root, task, *, free_bytes):
        if task == failed:
            raise SeedGateError("'x' is larger than the per-file limit", "seed_file_over_limit")
        from aiep import task_pins as tp
        tp.add_seed_pin(root, task, directory="d", sha256="c" * 64, head="d" * 40, entries=3, image_id=world.img)

    st = mb.run_pilot(d, RealSteps(world.deps(d, extract_seed=extract, dataset_record=record)), state_path=tmp_path / "s.json", results_path=tmp_path / "r.jsonl",
                      approvals=FULL(d["limits"]["max_total_computed_usd"]), execute=True)
    assert st["replacements"] and "SECRET" not in (tmp_path / "s.json").read_text() and st["replacements"][0]["replacement_identity"]["base_commit"] == "c" * 40


# ---------------------------------------------------------------- RealSteps identity verification and the typed failures at their raise sites
def test_real_steps_verify_the_replacement_identity_and_return_only_non_hidden_fields(tmp_path):
    world = World(tmp_path)
    d = make_def(8)
    cand = d["preregistration"]["replacements"][0]
    good = {"instance_id": cand["instance_id"], "repo": cand["repo"], "base_commit": "a" * 40, "FAIL_TO_PASS": ["SECRET"]}
    steps = RealSteps(world.deps(d, dataset_record=lambda t: good))
    assert steps.verify_task_identity(cand["instance_id"], cand["repo"]) == {"instance_id": cand["instance_id"], "repo": cand["repo"], "base_commit": "a" * 40}
    for bad, why in [({**good, "repo": "other/other"}, "repository"), ({**good, "base_commit": "A" * 40}, "base_commit"), ({**good, "instance_id": "x__x-1"}, "dataset record is for"),
                     ({**good, "base_commit": None}, "base_commit")]:
        with pytest.raises(StepError, match=why):
            RealSteps(world.deps(d, dataset_record=lambda t, b=bad: b)).verify_task_identity(cand["instance_id"], cand["repo"])
    with pytest.raises(StepError, match="no dataset record source"):
        RealSteps(world.deps(d, dataset_record=None)).verify_task_identity(cand["instance_id"], cand["repo"])


def test_gate_failure_classification_only_accepts_registered_codes():
    for code, make in GATE_FAILURES.items():
        got = gate_failure_of(make())
        assert got["code"] == code and got["exception"] and got["detail"]
    for plain in (RuntimeError("x"), SeedError("not a readable tar archive"), SeedSourceError("x"), SetupRefused("x"), DigestError("x"), PreparationGateFailure("no code of its own")):
        assert gate_failure_of(plain) is None


def test_the_raise_sites_produce_the_registered_codes():
    def code(thunk):
        with pytest.raises(PreparationGateFailure) as e:
            thunk()
        return gate_failure_of(e.value)["code"]
    assert code(lambda: read_bounded([b"\0" * MIB] * 3, 2 * MIB)) == "seed_archive_over_limit"
    assert code(lambda: parse_seed_archive(header_only_tar(96 * MIB + 1))) == "seed_file_over_limit"
    assert code(lambda: parse_seed_archive(make_tar(good_entries() + [("hl", "h", "src/mod.py", 0o644)]))) == "seed_unsafe_content"
    assert code(lambda: parse_seed_archive(make_tar(good_entries() + [("esc", "l", "../../etc/passwd", 0o777)]))) == "seed_unsafe_content"
    assert code(lambda: parse_seed_archive(make_tar([e for e in good_entries() if e[0] != ".git/HEAD"]))) == "seed_lineage_invalid"
    with pytest.raises(SeedError) as e:  # an unreadable archive is NOT a gate failure
        parse_seed_archive(b"this is not a tar archive at all" * 100)
    assert gate_failure_of(e.value) is None


def test_the_limit_raise_sites_for_entries_and_content(monkeypatch):
    from aiep import seed as S
    monkeypatch.setattr(S, "MAX_ENTRIES", 3)
    with pytest.raises(SeedGateError) as e:
        parse_seed_archive(make_tar(good_entries()))
    assert e.value.code == "seed_entries_over_limit"
    monkeypatch.setattr(S, "MAX_ENTRIES", 200_000)
    monkeypatch.setattr(S, "MAX_SEED_BYTES", 500)
    with pytest.raises(SeedGateError) as e:
        parse_seed_archive(make_tar(good_entries()))
    assert e.value.code == "seed_content_over_limit"


def test_lineage_and_identity_decision_points_classify_correctly(tmp_path):
    import test_task_setup as tts

    def fresh_root(name):  # a failed extraction leaves its seed files in place (by design), so each case gets its own root
        root = tmp_path / name
        (root / "config").mkdir(parents=True)
        (root / "config" / "evaluator_images.json").write_text(json.dumps({"images": {tts.INSTANCE: {"image": "x", "image_id": tts.IMAGE, "source": "t"}}}))
        (root / "config" / "seeds.json").write_text(json.dumps({"seeds": {}}))
        return root
    with pytest.raises(SeedLineageMismatch) as e:
        tts.go(fresh_root("a"), tts.FakeHost(), lineage=lambda s: {"head": s.manifest.head, "parent": "c" * 40})
    assert gate_failure_of(e.value)["code"] == "seed_lineage_invalid"
    with pytest.raises(SeedLineageMismatch) as e1b:  # HEAD that disagrees with the manifest is the same kind of failure
        tts.go(fresh_root("b"), tts.FakeHost(), lineage=lambda s: {"head": "d" * 40, "parent": tts.BASE})
    assert gate_failure_of(e1b.value)["code"] == "seed_lineage_invalid"
    with pytest.raises(SetupRefused) as e2:  # a lineage check that could not RUN says nothing about the task
        tts.go(fresh_root("c"), tts.FakeHost(), lineage=lambda s: (_ for _ in ()).throw(OSError("git failed")))
    assert gate_failure_of(e2.value) is None
    assert type(_identity_error(["uid 0"], probe_incomplete=False)) is IdentityMismatch and gate_failure_of(_identity_error(["uid 0"], probe_incomplete=False))["code"] == "identity_mismatch"
    assert type(_identity_error(["identity probe failed"], probe_incomplete=True)) is SetupRefused and gate_failure_of(_identity_error(["x"], probe_incomplete=True)) is None

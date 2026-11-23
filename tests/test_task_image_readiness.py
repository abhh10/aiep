"""A task is PREPARED only when its pins are complete AND the exact pinned image ID is present locally. Offline: fake Docker (no daemon, network, API or evaluator).

Before this, ``RealSteps.task_prepared`` looked only at the pin records, so a pinned task whose image had been removed counted as prepared and failed later, at the controls or the agent step
(the evaluator never pulls). These tests pin the corrected behaviour and the unchanged pull-approval boundary."""
from types import SimpleNamespace

import pytest

from aiep import m3_batch as mb
from aiep.m3_steps import RealSteps
from test_m3_grading_pins_batch import AP as PLAIN_AP, Fake as PlainFake, run as plain_run
from test_m3_integration import FULL, World, make_def, rows  # noqa: F401


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def present_by_exact_id(world):
    """A fake daemon lookup: like the real one, it answers with the image's own ID (whatever name or ID it is asked for) while the world holds the image, and None once it is gone."""
    return lambda client, name: world.img if world.images else None


def steps_for(world, definition, **over):
    over.setdefault("image_id_of", present_by_exact_id(world))
    return RealSteps(world.deps(definition, **over))


def pinned(world, definition):
    """A task with complete pins and its image present (prepared through the real preparation path, with the fake pull)."""
    task = definition["preregistration"]["tasks"][0]["instance_id"]
    steps = steps_for(world, definition)
    steps.prepare_task(task)
    assert world.images and steps.task_prepared(task)
    return task, steps


# ---------------------------------------------------------------- RealSteps.task_prepared
def test_complete_pins_and_the_pinned_image_present_is_prepared(world):
    d = make_def(1)
    task, steps = pinned(world, d)
    assert steps.task_prepared(task) is True and steps.unprepared_reason(task) is None


def test_complete_pins_but_the_image_missing_is_not_prepared_and_says_so(world):
    d = make_def(1)
    task, steps = pinned(world, d)
    world.images.clear()  # the image was removed; every pin record is still there
    assert steps.task_prepared(task) is False
    why = steps.unprepared_reason(task)
    assert "pins are complete" in why and "pinned image" in why and world.img in why and "incomplete" not in why


def test_a_docker_lookup_failure_is_not_prepared(world):
    d = make_def(1)
    task, _ = pinned(world, d)

    def boom(client, name):
        raise RuntimeError("daemon unreachable")
    assert steps_for(world, d, image_id_of=boom).task_prepared(task) is False

    def no_client():
        raise OSError("cannot connect to the Docker daemon")
    assert steps_for(world, d, client_factory=no_client).task_prepared(task) is False
    assert "could not be queried" in steps_for(world, d, image_id_of=boom).unprepared_reason(task)


def test_a_different_image_is_never_substituted_for_the_pinned_one(world):
    d = make_def(1)
    task, _ = pinned(world, d)
    other = "sha256:" + "b" * 64
    assert world.img != other
    assert steps_for(world, d, image_id_of=lambda c, n: other).task_prepared(task) is False  # present, but not THE pinned image
    assert steps_for(world, d, image_id_of=lambda c, n: None).task_prepared(task) is False
    asked = []
    steps_for(world, d, image_id_of=lambda c, n: asked.append(n) or n).task_prepared(task)
    assert asked == [world.img]  # looked up by the exact pinned ID, never a tag or name


def test_incomplete_pins_are_not_prepared_even_if_an_image_is_around(world):
    d = make_def(1)
    task = d["preregistration"]["tasks"][0]["instance_id"]
    world.images.add(task)  # an image is local, but nothing is pinned
    steps = steps_for(world, d)
    assert steps.task_prepared(task) is False
    why = steps.unprepared_reason(task)
    assert "pins are incomplete" in why and "evaluator_image" in why and "seed" in why


def test_checking_readiness_never_pulls_pins_or_touches_images(world):
    d = make_def(1)
    task, steps = pinned(world, d)
    world.images.clear()
    before = list(world.log)
    steps.task_prepared(task)
    steps.unprepared_reason(task)
    assert world.log == before and not world.images and not any(e[0] in ("pull", "verify_registry", "remove") for e in world.log[len(before):])


# ---------------------------------------------------------------- the batch
def run_batch(tmp_path, world, definition, ap, **over):
    return mb.run_pilot(definition, steps_for(world, definition, **over), state_path=tmp_path / "state.json", results_path=tmp_path / "results.jsonl", approvals=ap, execute=True)


def test_a_missing_image_cannot_reach_an_agent_run_or_the_evaluator_without_pull_approval(tmp_path, world):
    d = make_def(1)
    task, _ = pinned(world, d)
    world.images.clear()
    world.log.clear()
    ap = mb.Approvals(spend_usd=d["limits"]["max_total_computed_usd"], pulls=False, controls=True)
    st = run_batch(tmp_path, world, d, ap)
    assert st["status"] == "halted" and st["halt"]["id"] == "task_not_prepared"
    detail = st["halt"]["detail"]
    assert "pins are complete" in detail and "pinned image" in detail and "pulls/pins were not approved" in detail
    assert world.grade_calls == [] and world.prepare_calls == []  # no control, no evaluator, no agent
    assert not any(e[0] in ("pull", "run", "verify_registry") for e in world.log) and not world.images  # and above all no pull
    assert rows(tmp_path) == []


def test_incomplete_pins_halt_with_their_own_distinct_message(tmp_path, world):
    d = make_def(1)
    ap = mb.Approvals(spend_usd=d["limits"]["max_total_computed_usd"], pulls=False, controls=True)
    st = run_batch(tmp_path, world, d, ap)
    assert st["halt"]["id"] == "task_not_prepared"
    assert "pins are incomplete" in st["halt"]["detail"] and "pins are complete" not in st["halt"]["detail"]
    assert world.grade_calls == [] and world.prepare_calls == [] and not any(e[0] in ("pull", "run") for e in world.log)


def test_with_pull_approval_the_missing_image_is_pulled_by_the_preparation_step_before_anything_runs(tmp_path, world):
    d = make_def(1)
    task, _ = pinned(world, d)
    world.images.clear()
    world.log.clear()
    st = run_batch(tmp_path, world, d, FULL(d["limits"]["max_total_computed_usd"]))
    assert st["status"] == "complete", st["halt"]
    kinds = [e[0] for e in world.log]
    assert "pull" in kinds and "verify_registry" in kinds and kinds.index("verify_registry") < kinds.index("pull") < kinds.index("run")  # the approved pull is the only way the image returns


def test_steps_that_cannot_explain_still_halt_with_a_generic_reason(tmp_path):
    st = plain_run(tmp_path, PlainFake(), PLAIN_AP(spend_usd="10", pulls=False, controls=True))
    assert st["halt"]["id"] == "task_not_prepared" and "pulls/pins were not approved" in st["halt"]["detail"] and "not prepared" in st["halt"]["detail"]


def test_the_exact_pull_boundary_is_unchanged_prepare_pinned_task_still_needs_allow_pull(tmp_path, world):
    from aiep.m3_steps import StepError, prepare_pinned_task
    d = make_def(1)
    task, steps = pinned(world, d)
    world.images.clear()
    world.log.clear()
    deps = world.deps(d)
    with pytest.raises(StepError, match="not local and pulling was not approved"):
        prepare_pinned_task(root=deps.root, task_id=task, client_factory=deps.client_factory, free_gib=deps.free_gib, digest_record=deps.digest_record, verify_registry=deps.verify_registry,
                            image_present=deps.image_present, pull_image=deps.pull_image, pin_image=deps.pin_image, extract_seed=deps.extract_seed, verify_identity=deps.verify_identity,
                            allow_pull=False)
    assert not any(e[0] in ("pull", "verify_registry") for e in world.log) and not world.images

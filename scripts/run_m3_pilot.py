#!/usr/bin/env python3
"""Run (or plan) the M3 feasibility pilot from its registered definition. PLAN by default: nothing is started, pulled, graded or paid for.

    PYTHONPATH=src .venv/bin/python scripts/run_m3_pilot.py                      # plan + per-task readiness + open blockers (no Docker, no network)

    PYTHONPATH=src .venv/bin/python scripts/run_m3_pilot.py --prepare-only --task <id> [--task <id> ...] [--approve-pulls]
        # preparation only: pull (if not local) / pin the image / extract and verify the seed / verify the agent identity. Without --approve-pulls it only prints each task's
        # pin status. NO agent run, NO API request, NO key, NO evaluator, NO image removal; the 16 GiB start gate applies before any pull.

A real execution needs EVERY one of these (each is a separate approval): --execute, --approve-spend <max_total_computed_usd of the LOCKED definition>,
--approve-pulls (download/pin/seed/identity), --approve-controls (evaluator on gold and no-op), and optionally --approve-image-removal; the key in AIEP_UPSTREAM_API_KEY
and --claude-binary. Continuing a halted batch also needs --ack-halt <id>. The batch state and results go to runs/m3/<experiment_id>/.

Exit codes: 0 complete (or plan printed); 1 halted; 2 refused (nothing started).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from aiep import m3_batch  # noqa: E402
from aiep.experiment_def import DEFAULT_DEFINITION, ExperimentError, load_experiment  # noqa: E402
from aiep.task_pins import pin_status  # noqa: E402


def blockers(root: Path, definition: dict, args: argparse.Namespace, key_present: bool) -> list[str]:
    out = []
    lim = definition["limits"]
    if definition["status"] != "locked" or lim["budget_usd_per_run"] is None or lim["max_total_computed_usd"] is None:
        out.append("the definition is not locked: budgets are null until an approved real-API calibration sets them")
    if args.approve_spend != lim["max_total_computed_usd"]:
        out.append("--approve-spend must equal the definition's max_total_computed_usd exactly")
    for flag, name in ((args.approve_pulls, "--approve-pulls"), (args.approve_controls, "--approve-controls")):
        if not flag:
            out.append(f"{name} not given")
    if not key_present:
        out.append("AIEP_UPSTREAM_API_KEY is not set")
    if args.claude_binary is None or not Path(args.claude_binary).is_file():
        out.append("no --claude-binary file given")
    return out


def image_wiring(root: Path) -> dict:  # pragma: no cover - the real wiring; never exercised by tests
    """The digest-pinned image callables shared by the batch and --prepare-only: the lock record, its registry re-verification, lookup and pull BY DIGEST (never a tag)."""
    from aiep import image_digests as idg
    return dict(digest_record=lambda t: idg.load_digest_record(root, t), verify_registry=lambda rec: idg.verify_record_against_registry(rec, idg.docker_hub_fetch),
                image_present=idg.image_present, pull_image=idg.pull_by_digest)


def real_steps(root: Path, definition: dict, args: argparse.Namespace, key: str):  # pragma: no cover - the real wiring; never exercised by tests
    import docker
    from aiep import grading, m2_run, task_setup
    from aiep.m3_steps import Deps, RealSteps
    from aiep.pricing import load_pricing
    from aiep.handoff import Providers, validate_handoff
    import m3_grade
    from validate_handoff import seed_git, seed_lineage
    from aiep.task_input import load_record

    def client():
        return docker.from_env(timeout=600)

    def handoff(run_dir, expected):
        return validate_handoff(run_dir, root=root, providers=Providers(seed_git=seed_git), expected_slots=expected)

    def grade(instance_id, preds, run_id, *, kind, expected_image):
        return grading.grade(instance_id, preds, run_id, kind=kind, root=root, expected_image=expected_image, free_gib=lambda: shutil.disk_usage(root).free / 1024**3,
                             image_id_of=m3_grade.real_image_id, run_process=m3_grade.run_process, kill_containers=m3_grade.real_kill)

    def image_id_of(c, name):
        try:
            return c.images.get(name).id
        except Exception:
            return None

    pricing = load_pricing(root / "config" / "model_pricing.json", json.loads((root / "config" / "experiment.json").read_text())["model"])
    deps = Deps(root=root, definition=definition, client_factory=client, free_gib=lambda: shutil.disk_usage(root).free / 1024**3, prepare=m2_run.prepare,
                execute=m2_run.execute, handoff=handoff, grade=grade, write_noop=lambda p, t: grading.write_noop_predictions(p, t), pricing=pricing,
                pin_image=task_setup.pin_image, **image_wiring(root), extract_seed=lambda c, r, t, *, free_bytes: task_setup.extract_and_pin_seed(
                    c, r, t, free_bytes=free_bytes, base_commit=load_record(t, root / ".cache" / "hf", root)["base_commit"], lineage=seed_lineage), verify_identity=task_setup.verify_and_pin_identity,
                image_id_of=image_id_of,
                containers_using=lambda c, iid: len(c.containers.list(all=True, filters={"ancestor": iid})),
                remove_image_by_id=lambda c, iid: c.images.remove(iid, force=False), real_key=key, claude_binary=Path(args.claude_binary))
    return RealSteps(deps)


def real_prepare(root: Path, task_id: str, *, approved_pulls: bool) -> dict:  # pragma: no cover - the real wiring; never exercised by tests
    import docker
    from aiep import task_setup
    from aiep.m3_steps import prepare_pinned_task
    from validate_handoff import seed_lineage
    from aiep.task_input import load_record

    return prepare_pinned_task(
        root=root, task_id=task_id, client_factory=lambda: docker.from_env(timeout=600), free_gib=lambda: shutil.disk_usage(root).free / 1024**3,
        **image_wiring(root), pin_image=task_setup.pin_image,
        extract_seed=lambda c, r, t, *, free_bytes: task_setup.extract_and_pin_seed(
            c, r, t, free_bytes=free_bytes, base_commit=load_record(t, root / ".cache" / "hf", root)["base_commit"], lineage=seed_lineage),
        verify_identity=task_setup.verify_and_pin_identity, allow_pull=approved_pulls)


def prepare_only(definition: dict, args: argparse.Namespace, root: Path, prepare_fn) -> int:
    """Preparation only: refuses anything that could run an agent, call the API, run the evaluator or remove an image."""
    conflicts = [n for n, v in (("--execute", args.execute), ("--approve-spend", args.approve_spend), ("--approve-controls", args.approve_controls),
                                ("--approve-image-removal", args.approve_image_removal), ("--ack-halt", args.ack_halt), ("--claude-binary", args.claude_binary)) if v]
    if conflicts:
        print(f"REFUSED: --prepare-only cannot be combined with {', '.join(conflicts)}", file=sys.stderr)
        return 2
    pre = definition["preregistration"]
    allowed = [t["instance_id"] for t in pre["tasks"]] + [r["instance_id"] for r in pre["replacements"]]
    if not args.task:
        print("REFUSED: name at least one --task (a registered pilot task or pre-registered replacement); nothing is prepared by default", file=sys.stderr)
        return 2
    bad = [t for t in args.task if t not in allowed]
    if bad:
        print(f"REFUSED: not a task or replacement of {definition['experiment_id']}: {', '.join(bad)}", file=sys.stderr)
        return 2
    if not args.approve_pulls:
        for t in args.task:
            s = pin_status(root, t)
            print(f"  {t}: runnable={s['runnable']} missing={s['missing']}")
        print("  nothing was done: preparing a task (pull, pin, seed extraction, identity check) needs --approve-pulls")
        return 0
    for t in args.task:
        try:
            res = prepare_fn(root, t, approved_pulls=True)
        except Exception as e:
            print(f"ERROR: {t}: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        print(json.dumps({"task": t, **{k: res.get(k) for k in ("pulled", "status")}}))
    return 0


def main(argv: list[str] | None = None, *, root: Path = ROOT, env: dict | None = None, steps_factory=None, prepare_fn=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--definition", type=Path, default=None)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--approve-spend")
    ap.add_argument("--approve-pulls", action="store_true")
    ap.add_argument("--approve-controls", action="store_true")
    ap.add_argument("--approve-image-removal", action="store_true")
    ap.add_argument("--ack-halt")
    ap.add_argument("--claude-binary", type=Path)
    ap.add_argument("--prepare-only", action="store_true")
    ap.add_argument("--task", action="append", default=[])
    args = ap.parse_args(argv)
    env = os.environ if env is None else env
    try:
        definition = load_experiment(args.definition or root / DEFAULT_DEFINITION, root=root)
    except ExperimentError as e:
        print(f"REFUSED: the definition is invalid: {e}", file=sys.stderr)
        return 2
    if args.prepare_only:
        return prepare_only(definition, args, root, prepare_fn or (lambda r, t, *, approved_pulls: real_prepare(r, t, approved_pulls=approved_pulls)))
    key = env.get("AIEP_UPSTREAM_API_KEY", "")
    if not args.execute:
        for line in m3_batch.plan(definition):
            print(line)
        for t in definition["preregistration"]["tasks"]:
            s = pin_status(root, t["instance_id"])
            print(f"  {t['instance_id']}: runnable={s['runnable']} missing={s['missing']}")
        for b in blockers(root, definition, args, bool(key)):
            print(f"  blocker: {b}")
        return 0
    open_items = blockers(root, definition, args, bool(key))
    if open_items:
        print("REFUSED (nothing was started):", file=sys.stderr)
        for b in open_items:
            print(f"  - {b}", file=sys.stderr)
        return 2
    steps = steps_factory(root, definition, args, key) if steps_factory else real_steps(root, definition, args, key)
    base = root / "runs" / "m3" / definition["experiment_id"]
    approvals = m3_batch.Approvals(spend_usd=args.approve_spend, pulls=args.approve_pulls, controls=args.approve_controls, remove_images=args.approve_image_removal,
                                   acknowledge_halt=args.ack_halt)
    try:
        st = m3_batch.run_pilot(definition, steps, state_path=base / "state.json", results_path=base / "results.jsonl", approvals=approvals, execute=True)
    except m3_batch.BatchHalt as h:
        print(f"REFUSED/HALTED: {h}", file=sys.stderr)
        return 2
    print(json.dumps({"status": st["status"], "halt": st.get("halt"), "cumulative_cost_usd": st["cumulative_cost_usd"], "replacements": st["replacements"]}, indent=2))
    return 0 if st["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())

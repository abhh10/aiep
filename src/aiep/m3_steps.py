"""The real ``Steps`` for the M3 batch: wires the orchestrator to the M2 prepare/execute lifecycle, the handoff validator, the M1 evaluator, results and transcript.

Every side effect enters through an injected callable (Docker client factory, prepare/execute, grading, handoff, disk, sleep), so the same class runs against fakes
in tests and against the real implementations from ``scripts/run_m3_pilot.py``. Constructing it touches nothing.

Guarantees implemented here (each is tested):

* every arm calls ``m2_run.prepare`` afresh: the seed is re-loaded from its pin and hash-verified, new containers and a new run id are made, and the experiment
  identity of the SCHEDULED slot is passed through and recorded in ``run.json``; nothing is reused between arms;
* the evaluator receives ONLY the run's ``predictions.jsonl`` (exactly ``instance_id``, ``model_name_or_path``, ``model_patch``), only after a ready/ready_flagged
  handoff, and only if that file belongs to the slot's task;
* a run that raised is still returned if it persisted a directory, so the handoff rejects it (never graded, never counted as a success);
* an image is removed only by exact pinned ID, only if this batch pulled it (the batch decides), and never while a container uses it.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from . import results as results_mod
from .experiment_def import worst_case_exposure_estimate
from .grading import START_GATE_GIB
from .image_digests import DigestError, reference
from .limits import AGENT_MAX_OUTPUT_TOKENS
from .m3_batch import ArmResult
from .task_pins import evaluator_image_pin, pin_status
from .transcript import parse_transcript

PREDICTION_KEYS = frozenset({"instance_id", "model_name_or_path", "model_patch"})


class StepError(RuntimeError):
    pass


def read_predictions(path: Path, instance_id: str) -> dict[str, Any]:
    """The single prediction the evaluator may see. Anything beyond the three harness fields, another task, or more than one line is refused."""
    lines = [l for l in Path(path).read_text().splitlines() if l.strip()]
    if len(lines) != 1:
        raise StepError("predictions.jsonl must hold exactly one prediction")
    pred = json.loads(lines[0])
    if set(pred) != PREDICTION_KEYS or pred["instance_id"] != instance_id or not isinstance(pred["model_patch"], str) or not pred["model_patch"].strip():
        raise StepError("the prediction is not exactly {instance_id, model_name_or_path, model_patch} for this task with a non-empty patch")
    return pred


def prepare_pinned_task(*, root: Path, task_id: str, client_factory: Callable[[], Any], free_gib: Callable[[], float], digest_record: Callable[[str], Mapping[str, Any]],
                        verify_registry: Callable[[Mapping[str, Any]], None], image_present: Callable[[Any, Mapping[str, Any]], bool],
                        pull_image: Callable[[Any, Mapping[str, Any]], None], pin_image: Callable[..., Any], extract_seed: Callable[..., Any], verify_identity: Callable[..., Any],
                        allow_pull: bool) -> dict[str, Any]:
    """Make one task runnable, by IMMUTABLE image identity. The task's verified digest record is loaded first (a missing or invalid record fails closed, before any Docker
    call; there is no fallback to the ``:latest`` tag). If the image is not local: pulling must be approved, free space must be above the 16 GiB start gate, and the record
    is re-verified against the registry; only then is ``<repository>@<index digest>`` pulled for linux/amd64 and its ID checked. Then the image ID is pinned, the seed
    extracted and verified (base-commit check included) and the agent identity verified; each step is skipped if already done. Shared by the batch and ``--prepare-only``."""
    root = Path(root)
    try:
        record = digest_record(task_id)
    except DigestError as e:
        raise StepError(f"{task_id}: {e}") from e
    free = int(free_gib() * 1024**3)
    client = client_factory()
    pulled = False
    try:
        if not image_present(client, record):
            if not allow_pull:
                raise StepError(f"{task_id}: the image is not local and pulling was not approved")
            if free_gib() < START_GATE_GIB:
                raise StepError(f"{task_id}: {free_gib():.2f} GiB free, {START_GATE_GIB:.0f} GiB required before a pull (the start gate is never lowered); nothing was pulled")
            verify_registry(record)  # the lock is re-verified against the registry BEFORE any layer is downloaded
            pull_image(client, record)
            pulled = True
        if evaluator_image_pin(root, task_id) is None:
            pin_image(client, root, task_id, free_bytes=free)
        if pin_status(root, task_id)["seed_pinned"] is False:
            extract_seed(client, root, task_id, free_bytes=free)
        if not pin_status(root, task_id)["identity_verified"]:
            verify_identity(client, root, task_id, free_bytes=free)
    finally:
        client.close()
    status = pin_status(root, task_id)
    if not status["runnable"]:
        raise StepError(f"{task_id} is still not runnable after preparation: {status['missing']}")
    return {"pulled": pulled, "status": status, "image_reference": reference(record), "index_digest": record["index_digest"]}


@dataclass
class Deps:
    root: Path
    definition: Mapping[str, Any]
    client_factory: Callable[[], Any]
    free_gib: Callable[[], float]
    prepare: Callable[..., Any]  # m2_run.prepare
    execute: Callable[..., Any]  # m2_run.execute
    handoff: Callable[..., dict[str, Any]]  # (run_dir, expected_slots) -> report
    grade: Callable[..., Any]  # grading.grade-like: (instance_id, predictions, run_id, kind=..., ) -> GradingOutcome
    write_noop: Callable[[Path, str], Path]
    pricing: Any
    pin_image: Callable[..., str]
    digest_record: Callable[[str], Mapping[str, Any]]  # task id -> its verified digest record (DigestError if none)
    verify_registry: Callable[[Mapping[str, Any]], None]  # re-verify a record against the registry before a pull
    pull_image: Callable[[Any, Mapping[str, Any]], None]  # (client, record): pull by digest
    extract_seed: Callable[..., Any]
    verify_identity: Callable[..., Any]
    image_present: Callable[[Any, Mapping[str, Any]], bool]  # (client, record)
    image_id_of: Callable[[Any, str], str | None]  # (client, reference or name)
    containers_using: Callable[[Any, str], int]
    remove_image_by_id: Callable[[Any, str], None]
    real_key: str
    claude_binary: Path
    sleep: Callable[[float], None] = time.sleep
    stamp: Callable[[], str] = lambda: time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dataset_record: Callable[[str], Mapping[str, Any]] | None = None  # task id -> its dataset record from the pinned LOCAL snapshot (host side only); needed to verify a replacement's identity


class RealSteps:
    def __init__(self, deps: Deps):
        self.d = deps
        self.root = Path(deps.root)
        self.limits = deps.definition["limits"]
        self.runs_base = self.root / "runs" / "m2"
        self.hs_dir = self.root / "runs" / "handoff"
        self.prepared_ids: list[str] = []

    # ---- environment
    def free_gib(self) -> float:
        return self.d.free_gib()

    def sleep(self, seconds: float) -> None:
        self.d.sleep(seconds)

    # ---- per-task preparation (approval-gated by the batch)
    def unprepared_reason(self, task_id: str) -> str | None:
        """None when the task is prepared; otherwise WHY not. Prepared means BOTH complete, valid pins (image, seed, identity) AND the exact pinned image ID present in the local Docker image
        store. The lookup is by the pinned ID only (never a tag, never another image); an image that is absent, a different ID, or any Docker lookup failure all count as not prepared. This
        never pulls: pulling is only ever done by ``prepare_task``, which the batch calls solely under the pull approval."""
        status = pin_status(self.root, task_id)
        if not status["runnable"]:
            return f"its pins are incomplete (missing: {', '.join(status['missing'])})"
        image_id = evaluator_image_pin(self.root, task_id)["image_id"]
        try:
            client = self.d.client_factory()
            try:
                found = self.d.image_id_of(client, image_id)
            finally:
                client.close()
        except Exception:  # Docker unreachable or the lookup failed: the image cannot be shown to be present
            found = None
        if found != image_id:
            return f"its pins are complete but the pinned image {image_id} is not present locally (or Docker could not be queried)"
        return None

    def task_prepared(self, task_id: str) -> bool:
        return self.unprepared_reason(task_id) is None

    def preparation_state(self, task_id: str) -> dict[str, Any]:
        """What is pinned for a task right now (read-only; no Docker call). Recorded when a task is replaced, so the failed task's partial state stays auditable."""
        ps = pin_status(self.root, task_id)
        return {k: ps[k] for k in ("image_pinned", "seed_pinned", "identity_verified", "runnable", "missing")}

    def verify_task_identity(self, task_id: str, repo: str) -> dict[str, str]:
        """A replacement's identity from the pinned local dataset snapshot: the instance id, its repository (must equal the registered one) and a 40-hex base commit. Returns ONLY those three
        fields: hidden task data never leaves this method. Raises StepError on any mismatch or if no dataset source is wired. (The registered image name is checked by the batch, not here:
        no preparation module derives or resolves an image name.)"""
        if self.d.dataset_record is None:
            raise StepError("no dataset record source is wired: a replacement's identity and base commit cannot be verified")
        rec = self.d.dataset_record(task_id)
        got = {"instance_id": rec.get("instance_id"), "repo": rec.get("repo"), "base_commit": rec.get("base_commit")}
        problems = []
        if got["instance_id"] != task_id:
            problems.append(f"the dataset record is for {got['instance_id']!r}")
        if got["repo"] != repo:
            problems.append(f"the dataset repository is {got['repo']!r}, the registered one is {repo!r}")
        if not (isinstance(got["base_commit"], str) and re.fullmatch(r"[0-9a-f]{40}", got["base_commit"])):
            problems.append("the dataset base_commit is not a 40-character lowercase commit")
        if problems:
            raise StepError(f"{task_id}: " + "; ".join(problems))
        return {k: str(v) for k, v in got.items()}

    def prepare_task(self, task_id: str) -> dict[str, Any]:
        return prepare_pinned_task(root=self.root, task_id=task_id, client_factory=self.d.client_factory, free_gib=self.free_gib, digest_record=self.d.digest_record,
                                   verify_registry=self.d.verify_registry, image_present=self.d.image_present, pull_image=self.d.pull_image, pin_image=self.d.pin_image, extract_seed=self.d.extract_seed, verify_identity=self.d.verify_identity,
                                   allow_pull=True)  # the batch calls this only with the pull approval

    # ---- controls (the stock evaluator, gold and a no-op patch; no agent involved)
    def grade_control(self, task_id: str, kind: str) -> dict[str, Any]:
        pin = evaluator_image_pin(self.root, task_id)
        run_id = f"m3ctl-{kind}-{task_id.split('__')[-1]}-{self.d.stamp()}"[:96]
        if kind == "gold":
            preds = "gold"
        elif kind == "noop":
            preds = str(self.d.write_noop(self.root / "runs" / "m3" / f"{run_id}.noop.jsonl", task_id))
        else:
            raise StepError(f"unknown control kind {kind!r}")
        return dict(self.d.grade(task_id, preds, run_id, kind=kind, expected_image=pin).summary)

    # ---- one scheduled arm
    def run_arm(self, task_id: str, arm: str, identity: dict[str, Any], budget_usd: str) -> ArmResult:
        exp = {k: identity[k] for k in ("experiment_id", "task_id", "arm", "repetition", "pair_id", "order_index", "slot_id")}
        prepared = self.d.prepare(self.root, instance_id=task_id, label=arm, budget_usd=budget_usd, wall_clock_s=self.limits["wall_clock_s"],
                                  claude_binary=self.d.claude_binary, real_key=self.d.real_key, prompts_file=self.d.definition["prompt_set"]["path"],
                                  experiment=exp, max_concurrent=self.limits["gateway_max_concurrent"])
        if prepared.run_id in self.prepared_ids:
            raise StepError("run id reused within a batch")
        self.prepared_ids.append(prepared.run_id)
        client = self.d.client_factory()
        try:
            try:
                ex = self.d.execute(client, prepared, base=self.runs_base, root=self.root)
            except Exception as e:  # execute persists before it re-raises: use the directory if it exists, so the handoff can reject it
                directory = self.runs_base / prepared.run_id
                return ArmResult(directory if directory.is_dir() else None, f"{type(e).__name__}: {e}")
            return ArmResult(ex.directory, "; ".join(ex.problems) or None)
        finally:
            client.close()

    # ---- after the run
    def validate_handoff(self, run_dir: Path, expected_slots: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        report = self.d.handoff(run_dir, expected_slots)
        self.hs_dir.mkdir(parents=True, exist_ok=True)
        (self.hs_dir / f"{Path(run_dir).name}.handoff.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return report

    def grade_run(self, task_id: str, run_dir: Path, slot_id: str) -> dict[str, Any] | None:
        """Grade the run's patch with the stock evaluator. Returns None when there is no patch to grade (an empty patch is never run)."""
        preds = Path(run_dir) / "predictions.jsonl"
        if not preds.is_file():
            return None
        read_predictions(preds, task_id)  # exactly the three harness fields, this task, non-empty
        pin = evaluator_image_pin(self.root, task_id)
        out = self.d.grade(task_id, str(preds.resolve()), f"m3eval-{Path(run_dir).name}"[:96], kind="scored", expected_image=pin)
        return dict(out.summary)

    def transcript(self, run_dir: Path, cycle_cap: int) -> dict[str, Any]:
        p = Path(run_dir) / "claude_stdout.jsonl"
        return parse_transcript(p.read_bytes(), cycle_cap=cycle_cap) if p.is_file() else {}

    def build_row(self, run_dir: Path, identity: dict[str, Any], handoff: dict[str, Any], grading: dict[str, Any] | None, transcript: dict[str, Any] | None,
                  repo: str) -> dict[str, Any]:
        return results_mod.build_row(Path(run_dir), identity, handoff, grading, transcript, definition_sha256=self.d.definition["definition_sha256"],
                                     pricing=self.d.pricing, repo=repo)

    def exposure_estimate_usd(self, budget_usd: str, max_concurrent: int) -> Decimal:
        est = worst_case_exposure_estimate(budget_usd, max_concurrent, AGENT_MAX_OUTPUT_TOKENS, str(self.d.pricing.output))
        return Decimal(est["estimated_exposure_usd"])

    # ---- image lifecycle
    def image_in_use(self, task_id: str) -> bool:
        pin = evaluator_image_pin(self.root, task_id)
        client = self.d.client_factory()
        try:
            return pin is None or self.d.containers_using(client, pin["image_id"]) > 0
        finally:
            client.close()

    def remove_image(self, task_id: str) -> None:
        pin = evaluator_image_pin(self.root, task_id)
        if pin is None:
            raise StepError(f"{task_id} has no pin: refusing to remove an unpinned image")
        client = self.d.client_factory()
        try:
            if self.d.image_id_of(client, pin.get("digest_reference") or pin["image"]) != pin["image_id"]:  # by digest reference when the pin has one
                raise StepError("the local image does not have the pinned ID: it is not removed")
            if self.d.containers_using(client, pin["image_id"]) > 0:
                raise StepError("a container still uses the image: it is not removed")
            self.d.remove_image_by_id(client, pin["image_id"])
        finally:
            client.close()

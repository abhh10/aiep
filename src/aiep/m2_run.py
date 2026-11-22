"""Compose one M2 agent run: prepare everything locally, run it through the gateway lifecycle, persist artifacts.

``prepare`` does every check that needs no Docker (pins, seed hash, prompts, binary hash, policy-checked create kwargs for both
containers) and raises on the first problem, so a run that cannot be trusted never reaches Docker. ``execute`` runs the existing
``run_with_gateway`` (gateway ready first, agent supervised, both always removed) and ALWAYS writes the run's artifacts, including
for failed and invalid runs, then returns the evaluation handoff. Evaluation itself is a separate, separately approved step: this
module only writes ``predictions.jsonl`` and reports the M1 evaluator command; it never runs it and never calls Anthropic itself.

Nothing here starts a paid run by itself: ``scripts/run_m2_agent.py`` defaults to a plan and needs an explicit ``--execute`` plus
``--approve-spend`` equal to the budget.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import secrets
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from . import artifacts
from . import ledger as ledger_mod
from .agent_env import IdentityUnverified
from .task_pins import PinError, load_identity
from .agent_runner import (MAX_COMPONENT_FILE_BYTES, AgentResult, AgentRunSpec, build_create_kwargs, record_of, run_agent)
from .container_policy import AGENT_MEM, AGENT_NANO_CPUS, AGENT_PIDS, AGENT_SHM_DEFAULT
from .experiment import (CONFIG_LABELS, ClaudeCliPin, Prompts, build_task_text, claude_args, client_budget_usd, load_claude_cli,
                         load_prompts, prompt_record)
from .lease import RunLease
from .networks import AGENT_NETWORK, EGRESS_NETWORK, PROXY_NETWORK, verify_run_networks
from .limits import (AGENT_MAX_OUTPUT_TOKENS, AGENT_RUNTIME_PINS, CLAUDE_RUNTIME_UNCONTROLLED, GATEWAY_DRAIN_TIMEOUT_S,
                     HEARTBEAT_INTERVAL_S, LEASE_STOP_AFTER_S, MAX_WALL_CLOCK_S, MIN_WALL_CLOCK_S)
from .gateway_runner import GatewayImagePin, build_gateway_component, gateway_context, load_gateway_image
from .lifecycle import GatewaySpec, LifecycleResult, build_gateway_create_kwargs, lifecycle_record, run_with_gateway
from .proxy_lifecycle import (ProxySpec, build_proxy_component, build_proxy_create_kwargs, load_proxy_pin, proxy_context)
from .patch import write_predictions
from .seed import Seed, build_component_archive, load_seed
from .task_input import TaskInput, export_task_input, load_record

REPO_ROOT = Path(__file__).resolve().parents[2]
KEY_ENV = "AIEP_UPSTREAM_API_KEY"
MIN_KEY_LEN = 20


class PrepareError(RuntimeError):
    """A local precondition failed; nothing was sent to Docker."""


@dataclass
class Prepared:
    run_id: str
    instance_id: str
    label: str
    budget_usd: str
    wall_clock_s: float
    task: TaskInput
    task_text: str
    prompts: Prompts
    seed: Seed
    cli: ClaudeCliPin
    gateway_pin: GatewayImagePin
    aspec: AgentRunSpec = field(repr=False)
    gspec: GatewaySpec = field(repr=False)
    secrets: tuple[str, ...] = field(repr=False, default=())
    image_id: str = ""
    model: str = ""
    networks: dict[str, str] = field(default_factory=dict)  # name -> id, filled by execute() after the run-time network verification
    pspec: ProxySpec | None = field(default=None, repr=False)  # the restricted egress proxy (holds no secret)
    experiment: dict[str, Any] | None = None  # experiment identity (experiment_id, task_id, arm, repetition, pair_id, order_index, slot_id): recorded in run.json
    prompts_file: str = "config/workflow_prompts.json"  # which registered prompt set this run used (recorded; the handoff validates against it)
    max_concurrent: int = 2
    subnets: dict[str, str] = field(default_factory=dict)  # internal network name -> subnet, read from Docker by execute()
    calibration: str | None = None  # a calibration run's id (``CALIBRATION_ID``): recorded in run.json; such a run is never a scored observation


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


CALIBRATION_ID = re.compile(r"^m3-calibration-[1-9][0-9]{0,3}$")  # separate namespace from every experiment definition id (m3-pilot-v*)
EXPERIMENT_KEYS = ("experiment_id", "task_id", "arm", "repetition", "pair_id", "order_index", "slot_id")


def _check_experiment_identity(experiment: Mapping[str, Any], instance_id: str, label: str) -> None:
    """The identity block a batch run carries must be complete and agree with the run it is attached to."""
    if set(experiment) != set(EXPERIMENT_KEYS):
        raise PrepareError(f"the experiment identity must have exactly {list(EXPERIMENT_KEYS)}")
    if experiment["task_id"] != instance_id or experiment["arm"] != label:
        raise PrepareError("the experiment identity names a different task or arm than this run")
    if type(experiment["repetition"]) is not int or type(experiment["order_index"]) is not int or experiment["repetition"] < 1 or experiment["order_index"] < 1:
        raise PrepareError("repetition and order_index must be positive integers")
    if experiment["slot_id"] != f"{experiment['experiment_id']}/{instance_id}/{label}/r{experiment['repetition']}":
        raise PrepareError("slot_id is not experiment_id/task_id/arm/rN")


def load_seed_pin(root: Path, instance_id: str) -> dict[str, Any]:
    cfg = json.loads((Path(root) / "config" / "seeds.json").read_text())
    pin = (cfg.get("seeds") or {}).get(instance_id)
    if not isinstance(pin, dict):
        raise PrepareError(f"no pinned seed for {instance_id}")
    return pin


def prepare(root: Path, *, instance_id: str, label: str, budget_usd: str, wall_clock_s: float, claude_binary: Path,
            real_key: str, agent_network: str = AGENT_NETWORK, proxy_network: str = PROXY_NETWORK, egress_network: str = EGRESS_NETWORK,
            stamp: str | None = None, hf_home: Path | None = None, prompts_file: str = "config/workflow_prompts.json",
            experiment: Mapping[str, Any] | None = None, max_concurrent: int = 2, calibration: str | None = None) -> Prepared:
    """Everything that needs no Docker. Raises ``PrepareError`` (or a pin/policy error) on the first problem.

    ``calibration`` marks the run as a CALIBRATION (``m3-calibration-N``): recorded in run.json, never an A/B observation, so it cannot carry an experiment identity."""
    root = Path(root)
    if type(max_concurrent) is not int or not 1 <= max_concurrent <= 4:
        raise PrepareError("max_concurrent must be an integer from 1 to 4 (the gateway's own range)")
    if calibration is not None:
        if not (isinstance(calibration, str) and CALIBRATION_ID.fullmatch(calibration)):
            raise PrepareError("a calibration id must look like m3-calibration-1")
        if experiment is not None:
            raise PrepareError("a calibration run has no experiment identity: it can never be a scheduled, scored slot")
    if label not in CONFIG_LABELS:
        raise PrepareError(f"config must be one of {list(CONFIG_LABELS)}")
    if (agent_network, proxy_network, egress_network) != (AGENT_NETWORK, PROXY_NETWORK, EGRESS_NETWORK):  # the single definition is aiep.networks
        raise PrepareError(f"a run must use the networks {AGENT_NETWORK!r}, {PROXY_NETWORK!r} and {EGRESS_NETWORK!r} (no override is allowed)")
    if not isinstance(real_key, str) or len(real_key) < MIN_KEY_LEN:
        raise PrepareError(f"{KEY_ENV} is not set to a plausible key")
    if not (isinstance(wall_clock_s, (int, float)) and not isinstance(wall_clock_s, bool)
            and MIN_WALL_CLOCK_S <= wall_clock_s <= MAX_WALL_CLOCK_S):
        raise PrepareError(f"wall_clock_s must be between {MIN_WALL_CLOCK_S:.0f} and {MAX_WALL_CLOCK_S:.0f}")
    if experiment is not None:
        _check_experiment_identity(experiment, instance_id, label)
    prompts = load_prompts(root / prompts_file)  # refuses a file whose text no longer matches its recorded hashes
    cli = load_claude_cli(root / "config" / "claude_cli.json")  # ClaudeCliUnpinned until Gate 1
    gpin = load_gateway_image(root / "config" / "gateway_image.json")  # GatewayImageUnpinned until the image is pinned
    model = json.loads((root / "config" / "experiment.json").read_text())["model"]  # NOT named ``experiment``: that is the scheduled slot's identity argument, recorded in run.json

    seed_pin = load_seed_pin(root, instance_id)
    images = json.loads((root / "config" / "evaluator_images.json").read_text())["images"]
    if instance_id not in images:
        raise PrepareError(f"{instance_id} has no evaluator image pin (config/evaluator_images.json): its image has not been pinned")
    expected_image = images[instance_id]["image_id"]
    if seed_pin.get("image_id") != expected_image:
        raise PrepareError("the pinned seed was taken from a different image than the evaluator's")
    try:
        agent_cfg = load_identity(root, instance_id, expected_image)  # per-task identity, observed in THIS image; the legacy file only for its own image
    except (IdentityUnverified, PinError) as e:
        raise PrepareError(f"{instance_id} has no verified agent identity for its image: {e}") from e
    seed = load_seed(root / seed_pin["directory"], expected_sha256=seed_pin["sha256"])
    if seed.manifest.head != seed_pin["head"] or len(seed.manifest.entries) != seed_pin["entries"]:
        raise PrepareError("the stored seed's HEAD or entry count differs from its pin")

    # the agent-visible task: an allowlisted view of the record; the full record (hidden fields) never leaves this function
    task = export_task_input(load_record(instance_id, hf_home or root / ".cache" / "hf", root))
    text = build_task_text(label, task, prompts)

    binary_path = Path(claude_binary)
    if not binary_path.is_file() or binary_path.is_symlink():
        raise PrepareError("the Claude binary path is not a regular file")
    size = binary_path.stat().st_size
    if size != cli.size_bytes or size > cli.max_binary_bytes or cli.max_binary_bytes > MAX_COMPONENT_FILE_BYTES:
        raise PrepareError("the Claude binary's size does not match its pin (or exceeds the component ceiling)")
    data = binary_path.read_bytes()
    if sha256_bytes(data) != cli.sha256:
        raise PrepareError("the Claude binary does not match its pinned SHA-256")
    components = build_component_archive({"claude": (data, 0o755)}, max_file_bytes=MAX_COMPONENT_FILE_BYTES)
    del data

    stamp = stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_id = artifacts.make_run_id(instance_id, label, stamp)
    run_token = secrets.token_hex(32)
    ppin = load_proxy_pin(root / "config" / "proxy.json", gateway_pin=gpin, agent_uid=agent_cfg.identity.uid)  # distinct uid:gid, fixed port
    pctx = proxy_context(gpin, ppin, proxy_network=proxy_network, egress_network=egress_network, forbidden_secrets=(real_key, run_token))
    pspec = ProxySpec(run_id=run_id, image=gpin.image, ctx=pctx, code=build_proxy_component(root))
    gctx = gateway_context(gpin, agent_network=agent_network, proxy_network=proxy_network, real_key=real_key)
    gspec = GatewaySpec(run_id=run_id, image=gpin.image, ctx=gctx, run_token=run_token, budget_usd=budget_usd,
                        code=build_gateway_component(root), proxy_endpoint=pspec.endpoint, max_concurrent=max_concurrent)
    aspec = AgentRunSpec(
        run_id=run_id, image=expected_image, gateway_url=gspec.url, agent_network=agent_network, pinned_model=model,
        run_token=run_token, seed=seed, components=components, python=agent_cfg.verification_python,
        identity=agent_cfg.identity, config_label=label, task_text=text, claude_args=claude_args(model, budget_usd),
        image_id=expected_image, wall_clock_s=float(wall_clock_s), forbidden_secrets=(real_key,))
    build_create_kwargs(aspec)  # policy-check both containers now, before Docker exists in the picture
    build_gateway_create_kwargs(gspec, preflight=True)
    build_proxy_create_kwargs(pspec, preflight=True)
    return Prepared(run_id, instance_id, label, budget_usd, float(wall_clock_s), task, text, prompts, seed, cli, gpin, aspec,
                    gspec, (real_key, run_token), expected_image, model, pspec=pspec,
                    experiment=dict(experiment) if experiment else None, prompts_file=prompts_file, max_concurrent=max_concurrent, calibration=calibration)


# ------------------------------------------------------------------ what a run recorded
def summarize_ledger(ledger: bytes | None) -> dict[str, Any] | None:
    """Gateway-measured usage: the authoritative record (computed from measured token usage, not a CLI estimate).

    ``requests`` counts finished requests (a ``forwarding`` line is only the start of one). ``exact`` is False whenever the ledger shows a
    request that was started and never finished, a torn line, or a metered request that was cut off (usage charged at its max_tokens upper
    bound): the figures are then a lower bound or an upper bound, never an exact total.
    """
    if ledger is None:
        return None
    totals: dict[str, int] = {}
    requests = metered = upper_bound_entries = 0
    last_cumulative = "0"
    for line in ledger.decode("utf-8", "replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("outcome") == "forwarding":
            continue
        requests += 1
        usage = e.get("usage")
        if isinstance(usage, dict):
            metered += 1
            for k, v in usage.items():
                if isinstance(v, int) and not isinstance(v, bool):
                    totals[k] = totals.get(k, 0) + v
        if e.get("usage_incomplete_upper_bound"):
            upper_bound_entries += 1
        if "cumulative_usd_computed" in e:
            last_cumulative = str(e["cumulative_usd_computed"])
    a = ledger_mod.audit(ledger)
    return {"source": "gateway ledger (measured token usage, computed cost)", "requests": requests, "metered": metered,
            "tokens": totals, "computed_usd_upper_bound_basis": last_cumulative,
            "unresolved_requests": len(a["unresolved"]), "unparseable_lines": a["unparseable_lines"],
            "request_status": ledger_mod.request_status_summary(ledger),
            "entries_charged_at_max_tokens_upper_bound": upper_bound_entries,
            "exact": a["complete"] and upper_bound_entries == 0}


def cli_estimate(stdout: bytes) -> dict[str, Any] | None:
    """Claude Code's own final ``result`` line, labelled as a client-side ESTIMATE (a killed run has none)."""
    last = None
    for line in stdout.decode("utf-8", "replace").splitlines():
        if line.startswith('{"type":"result"') or '"type":"result"' in line[:40]:
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if isinstance(doc, dict) and doc.get("type") == "result":
                last = doc
    if last is None:
        return None
    keep = ("subtype", "is_error", "num_turns", "duration_ms", "total_cost_usd")
    return {"source": "Claude Code client-side estimate (NOT measured)", **{k: last.get(k) for k in keep if k in last}}


def budget_attribution(gateway_stop_reason: str | None, estimate: dict[str, Any] | None) -> dict[str, Any]:
    """Which budget ended the run: the gateway's (authoritative, measured) or Claude Code's own client-side estimate.

    The gateway reports ``budget_exhausted`` as its stop reason; Claude Code reports ``error_max_budget_usd`` as its final result subtype.
    They are different causes with different reliability and are never merged. The client budget is set above the gateway's, so the
    gateway is expected to trip first; ``client_budget_exceeded`` without ``gateway_budget_exhausted`` means the client's estimate led.
    """
    gateway = gateway_stop_reason == "budget_exhausted"
    client = bool(estimate) and estimate.get("subtype") == "error_max_budget_usd"
    return {"gateway_budget_exhausted": gateway, "client_budget_exceeded": client,
            "terminated_by": "both" if gateway and client else "gateway" if gateway else "client" if client else None}


def claude_runtime_record() -> dict[str, Any]:
    """The Claude Code runtime settings pinned for the run (identical for A and B) and the ones left at the binary's defaults."""
    return {"pinned_env": dict(AGENT_RUNTIME_PINS), "uncontrolled": list(CLAUDE_RUNTIME_UNCONTROLLED),
            "note": "names found in the embedded source of the pinned 2.1.328 binary; defaults not listed above are the binary's own"}


@dataclass
class Execution:
    run_id: str
    directory: Path | None
    lifecycle: LifecycleResult | None
    error: str | None
    problems: list[str]
    predictions: Path | None
    eval_command: str | None


def _limits(p: Prepared) -> dict[str, Any]:
    a = p.aspec
    return {"agent_max_output_tokens": AGENT_MAX_OUTPUT_TOKENS, "wall_clock_s": p.wall_clock_s, "budget_usd": p.budget_usd,
            "gateway_budget_usd": p.budget_usd, "gateway_max_concurrent": p.max_concurrent, "budget_is_soft": True, "claude_client_budget_usd": client_budget_usd(p.budget_usd),
            "gateway_drain_timeout_s": GATEWAY_DRAIN_TIMEOUT_S, "agent_memory": AGENT_MEM, "agent_pids": AGENT_PIDS,
            "agent_nano_cpus": AGENT_NANO_CPUS, "shm_size": AGENT_SHM_DEFAULT,
            "tmpfs": {"testbed": a.testbed_size, "components": a.components_size, "tmp": a.tmp_size, "home": a.home_size},
            "max_patch_bytes": a.max_patch_bytes}


def _git(root: Path) -> dict[str, Any]:
    try:
        from .manifest import git_state
        return git_state(root)
    except Exception as e:  # recorded, not fatal: the run itself is what matters
        return {"error": type(e).__name__}


def leased_agent_runner(lease: RunLease, run_agent_fn: Callable[..., AgentResult] = run_agent) -> Callable[..., AgentResult]:
    """``run_with_gateway``'s ``run_agent_fn`` with one extra stop condition: the run's own lease is lost.

    The supervising loop polls the callable it is given; this one answers ``"lease_lost"`` (an invalid-run stop reason) when the
    lease can no longer be refreshed, before it asks the gateway. The agent is then quiesced and removed, and the gateway torn
    down, by the existing lifecycle: nothing here removes any container itself.
    """
    def run(client: Any, aspec: AgentRunSpec, gateway_stopped: Callable[[], str | None], **kw: Any) -> AgentResult:
        def stopped() -> str | None:
            return lease.lost() or gateway_stopped()
        return run_agent_fn(client, aspec, stopped, **kw)
    return run


def execute(client: Any, p: Prepared, *, base: Path, root: Path = REPO_ROOT,
            run_fn: Callable[..., LifecycleResult] | None = None, lease_dir: Path | None = None,
            lease_interval_s: float = HEARTBEAT_INTERVAL_S, lease_stop_after_s: float = LEASE_STOP_AFTER_S) -> Execution:
    """Run once and always persist. The caller owns (and must close) the Docker client. Never runs the evaluator.

    A host-side lease (``aiep.lease``) is taken BEFORE any container exists and refreshed every ``lease_interval_s`` while the run
    lasts, so the reaper can tell this live run from an abandoned one; it is released last, after the lifecycle's own ``finally`` has
    removed both containers and the artifacts are written. If the lease cannot be taken (``LeaseError``) nothing has been created.

    If the lifecycle raised (including ``KeyboardInterrupt``), the artifacts are still written, a failure to write them is reported
    on stderr without replacing the original exception, and the original exception then propagates.
    """
    # Before the lease and before ANY container: inspect both networks as they exist now and require exact policy (aiep.networks). A missing,
    # same-named-but-different, non-internal or occupied network raises NetworkError here and nothing has been created.
    if p.pspec is None:
        raise PrepareError("the run has no proxy spec")
    found = verify_run_networks(client, p.aspec.agent_network, p.gspec.ctx.proxy_network, p.pspec.ctx.egress_network)
    p.networks, p.subnets = dict(found.ids), dict(found.subnets)
    if p.gspec.ctx.agent_network != p.aspec.agent_network or p.gspec.ctx.proxy_network != p.pspec.ctx.proxy_network:
        raise PrepareError("the gateway, the proxy and the agent do not share the expected networks")
    # The listen addresses are the networks' REAL subnets, read from Docker just now: the gateway binds only its agent-network interface and the
    # proxy only its proxy-network interface (plus loopback). The specs are rebuilt (they are frozen) and policy-checked again at create.
    p.gspec = dataclasses.replace(p.gspec, ctx=dataclasses.replace(p.gspec.ctx, agent_cidr=found.subnets[AGENT_NETWORK]))
    p.pspec = dataclasses.replace(p.pspec, ctx=dataclasses.replace(p.pspec.ctx, proxy_cidr=found.subnets[PROXY_NETWORK]))
    # What the RUNNING agent's attachment is checked against after it starts (agent_attachment): the verified network's ID and real subnet.
    p.aspec = dataclasses.replace(p.aspec, network_id=found.ids[AGENT_NETWORK], network_cidr=found.subnets[AGENT_NETWORK])
    build_gateway_create_kwargs(p.gspec)
    build_proxy_create_kwargs(p.pspec)
    lease = RunLease(Path(lease_dir) if lease_dir else Path(root) / ".cache" / "leases", p.run_id, interval_s=lease_interval_s,
                     stop_after_s=lease_stop_after_s)
    lease.acquire()
    if run_fn is None:  # the real lifecycle, supervised against the lease as well as the gateway
        def run_fn(client: Any, gspec: GatewaySpec, aspec: AgentRunSpec) -> LifecycleResult:  # noqa: F811
            return run_with_gateway(client, gspec, aspec, pspec=p.pspec, run_agent_fn=leased_agent_runner(lease))
    try:
        return _execute_leased(client, p, base, root, run_fn, lease)
    finally:
        lease.release()


def _execute_leased(client: Any, p: Prepared, base: Path, root: Path, run_fn: Callable[..., LifecycleResult],
                    lease: RunLease) -> Execution:
    t0 = time.monotonic()
    result: LifecycleResult | None = None
    raised: BaseException | None = None
    try:
        result = run_fn(client, p.gspec, p.aspec)
    except BaseException as e:
        raised = e
    error = None if raised is None else f"{type(raised).__name__}: {raised}"
    execution: Execution | None = None
    persist_error: Exception | None = None
    try:
        execution = _persist(p, base, root, result, error, time.monotonic() - t0, raised, lease)
    except Exception as e:
        persist_error = e
    if raised is not None:
        if persist_error is not None:
            sys.stderr.write(f"WARNING: artifacts could not be written: {type(persist_error).__name__}: {persist_error}\n")
        raise raised
    if persist_error is not None:
        raise persist_error
    assert execution is not None
    return execution


def _persist(p: Prepared, base: Path, root: Path, result: LifecycleResult | None, error: str | None, wall: float,
             exc: BaseException | None, lease: RunLease | None = None) -> Execution:
    agent = result.agent if result else None
    files: dict[str, bytes | str] = {"prompt.txt": p.task_text}
    if agent is not None:
        files["claude_stdout.jsonl"], files["claude_stderr.txt"] = agent.claude_stdout, agent.claude_stderr
    if result is not None and result.ledger is not None:
        files["gateway_ledger.jsonl"] = result.ledger
    predictions: bytes | None = None
    if agent is not None and agent.patch is not None and agent.patch.status == "ok" and agent.valid:
        files["patch.diff"] = agent.patch.patch or ""
        with tempfile.TemporaryDirectory() as tmp:  # reuse the validating writer; never writes a bad patch
            path = write_predictions(Path(tmp) / "predictions.jsonl", p.instance_id, f"aiep-{p.label}-{p.model}", agent.patch)
            predictions = path.read_bytes()
        files["predictions.jsonl"] = predictions
    meta: dict[str, Any] = {
        "task": {"instance_id": p.instance_id, "repo": p.task.repo, "base_commit": p.task.base_commit},
        "seed": {"sha256": p.seed.manifest.archive_sha256, "head": p.seed.manifest.head, "entries": len(p.seed.manifest.entries)},
        "image_id": p.image_id, "model": p.model, "config": p.label,
        "experiment": p.experiment, "prompt_set_file": p.prompts_file,
        "claude_cli": {"version": p.cli.version, "platform": p.cli.platform, "sha256": p.cli.sha256, "size_bytes": p.cli.size_bytes},
        "claude_args": list(p.aspec.claude_args), "gateway_image": {"image": p.gateway_pin.image, "image_id": p.gateway_pin.image_id},
        "prompt": prompt_record(p.label, p.task_text, p.prompts), "limits": _limits(p),
        "aiep": _git(root), "orchestrator_wall_s": round(wall, 3), "orchestrator_error": error,
        "interrupted": exc is not None and not isinstance(exc, Exception),
        "lifecycle": lifecycle_record(result) if result else None,
        "usage_measured": summarize_ledger(result.ledger if result else None),
        "usage_cli_estimate": cli_estimate(agent.claude_stdout) if agent is not None else None,
        "budget_attribution": budget_attribution(result.gateway_stop_reason if result else None,
                                                 cli_estimate(agent.claude_stdout) if agent is not None else None),
        "accounting": result.accounting if result else None,
        "claude_runtime": claude_runtime_record(),
        "networks": dict(p.networks), "network_subnets": dict(p.subnets),
        "proxy": {"endpoint": p.pspec.endpoint if p.pspec else None, "uid_gid": p.pspec.ctx.user if p.pspec else None,
                  "note": "restricted CONNECT proxy: the only container on the egress network; holds no secret"},
        "lease": None if lease is None else lease.summary(),
        "termination": None if lease is None or lease.lost() is None else {
            "reason": "lease_lost", "detail": lease.summary()["lost_detail"],
            "first_heartbeat_error": lease.errors[0] if lease.errors else None},
        "evaluation": "not run: a separate, approved step (the M1 evaluator) consumes predictions.jsonl",
    }
    if p.calibration is not None:  # only calibration runs carry the key, so ordinary run.json files are unchanged
        meta["calibration"] = {"id": p.calibration, "purpose": "calibration", "scored": False, "not_an_ab_observation": True,
                               "note": "A calibration run measures cost, duration and token usage to set the pilot's budgets. It is not an A or B observation, has no experiment identity, "
                                       "and is refused by the results builder, the results writer, the analysis and the scheduled-slot handoff. The gateway budget is a SOFT limit and "
                                       "Claude Code's --max-budget-usd is a client-side ESTIMATE: neither is a hard spending cap."}
    art = artifacts.write_run_artifacts(base, p.run_id, files, meta, p.secrets)
    problems = artifacts.verify_run_artifacts(art.directory)
    if result is not None and result.accounting is not None and not result.accounting.get("complete"):
        problems.append("gateway accounting is INCOMPLETE: usage may have been incurred that the ledger does not contain (see run.json accounting)")
    if result is not None and result.proxy_state != "new" and not result.proxy_removed:
        problems.append(f"the egress proxy container was NOT removed ({result.proxy_cleanup_error}); remove it before another run")
    if agent is not None and agent.claude_output_truncated:
        problems.append("claude output was truncated at the cap: the run is censored and is not graded")
    pred_path = art.directory / "predictions.jsonl" if predictions is not None else None
    cmd = (f"scripts/m1_eval.sh m2eval-{p.run_id} {pred_path}" if pred_path is not None else None)
    return Execution(p.run_id, art.directory, result, error, problems, pred_path, cmd)

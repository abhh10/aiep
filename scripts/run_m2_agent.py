#!/usr/bin/env python3
"""Compose one M2 agent run. PLAN by default; a real, paid run needs --execute AND --approve-spend equal to the budget.

    # plan (no Docker, no network, no key needed): shows the prompt hashes, arguments and every blocker still open
    PYTHONPATH=src .venv/bin/python scripts/run_m2_agent.py --config A --budget-usd 1.00

    # a real run (approval-gated; refuses while any pin is missing)
    AIEP_UPSTREAM_API_KEY=... PYTHONPATH=src .venv/bin/python scripts/run_m2_agent.py --config A --budget-usd 1.00 \
        --claude-binary /path/to/linux-x64/claude --execute --approve-spend 1.00

The real key is read only from the environment variable AIEP_UPSTREAM_API_KEY; it is never accepted as an argument, printed or
written (artifacts are scrubbed of it). Evaluation is NOT run here: the script prints the M1 evaluator command for the
predictions it wrote, and that command needs its own approval.

Spending limits, stated plainly: the gateway budget is a SOFT limit (usage is known only after a response, so a run can end above it by the cost of the request in flight,
and an interrupted request is charged at an upper bound), and Claude Code's --max-budget-usd is a client-side ESTIMATE. Neither is a hard spending cap; only a spend limit
on the dedicated API key itself is.

    --max-concurrent N   requests in flight upstream, 1 to 4, DEFAULT 1 (the experiment's setting; it also bounds the budget overshoot). It is always passed explicitly.
    --calibration ID     mark the run as a calibration (ID like m3-calibration-1): recorded in run.json, written under runs/calibration/, never a scored A/B observation.

Exit codes: 0 ok; 1 failed (a local check, the run, or artifact verification); 2 refused (disk gate, missing approval, open blocker, invalid option).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiep import m2_run, networks  # noqa: E402
from aiep.experiment import (ClaudeCliUnpinned, PromptError, build_task_text, claude_args, client_budget_usd, load_claude_cli,  # noqa: E402
                             load_prompts, prompt_record)
from aiep.gateway import parse_max_concurrent  # noqa: E402
from aiep.gateway_runner import GatewayImageUnpinned, load_gateway_image  # noqa: E402
from aiep.limits import DEFAULT_WALL_CLOCK_S, MAX_WALL_CLOCK_S  # noqa: E402
from aiep.task_input import TaskInput  # noqa: E402

MIN_FREE_BYTES = 16 * 1024**3
DEFAULT_INSTANCE = "pytest-dev__pytest-5227"


def _default_client() -> Any:
    import docker  # imported late: constructing the client is the first API call

    return docker.from_env(timeout=120)


def blockers(root: Path, *, claude_binary: Path | None, key_present: bool, execute_approved: bool) -> list[str]:
    """Open preconditions, found without Docker or network. Empty means nothing local stands in the way."""
    out: list[str] = []
    try:
        load_prompts(root / "config" / "workflow_prompts.json")
    except PromptError as e:
        out.append(f"prompts: {e}")
    try:
        load_claude_cli(root / "config" / "claude_cli.json")
    except ClaudeCliUnpinned as e:
        out.append(f"Claude CLI: {e} (Gate 1)")
    try:
        load_gateway_image(root / "config" / "gateway_image.json")
    except (GatewayImageUnpinned, OSError, ValueError) as e:
        out.append(f"gateway image: {e}")
    if claude_binary is None or not Path(claude_binary).is_file():
        out.append("no --claude-binary file given")
    if not key_present:
        out.append(f"{m2_run.KEY_ENV} is not set")
    if not execute_approved:
        out.append("not approved to spend: --execute and --approve-spend <budget> are both required")
    return out


def main(argv: list[str] | None = None, *, client_factory: Callable[[], Any] = _default_client,
         free_bytes: Callable[[Path], int] | None = None, root: Path = ROOT, env: dict[str, str] | None = None,
         run_fn: Callable[..., Any] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--instance", default=DEFAULT_INSTANCE)
    p.add_argument("--config", required=True, choices=["A", "B"])
    p.add_argument("--budget-usd", required=True, help="spend ceiling for the run (no default: it must be chosen)")
    p.add_argument("--wall-clock-s", type=float, default=DEFAULT_WALL_CLOCK_S, help=f"at most {MAX_WALL_CLOCK_S:.0f}")
    p.add_argument("--claude-binary", type=Path)
    p.add_argument("--agent-network", default=networks.AGENT_NETWORK)
    p.add_argument("--proxy-network", default=networks.PROXY_NETWORK)
    p.add_argument("--egress-network", default=networks.EGRESS_NETWORK)
    p.add_argument("--max-concurrent", default="1", help="requests in flight upstream, 1 to 4 (default 1: it also bounds the budget overshoot)")
    p.add_argument("--calibration", default=None, help="mark this run as a calibration, e.g. m3-calibration-1 (never a scored A/B observation)")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--approve-spend", help="must equal --budget-usd exactly to allow a paid run")
    args = p.parse_args(argv)
    env = os.environ if env is None else env
    key = env.get(m2_run.KEY_ENV, "")
    approved = bool(args.execute and args.approve_spend is not None and args.approve_spend == args.budget_usd)

    try:
        claude_args("claude-sonnet-5-5", args.budget_usd)  # validates the budget format before anything else
    except ValueError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    # option checks come before ANY Docker, key or API activity
    if not args.max_concurrent:
        print("REFUSED: --max-concurrent must be an integer from 1 to 4", file=sys.stderr)
        return 2
    try:
        max_concurrent = parse_max_concurrent(args.max_concurrent)
    except ValueError:
        print(f"REFUSED: --max-concurrent {args.max_concurrent!r} is not an integer from 1 to 4", file=sys.stderr)
        return 2
    if args.calibration is not None and not m2_run.CALIBRATION_ID.fullmatch(args.calibration):
        print(f"REFUSED: --calibration {args.calibration!r} is not a calibration id like m3-calibration-1", file=sys.stderr)
        return 2
    args.max_concurrent = max_concurrent

    if not args.execute:
        return _plan(root, args, bool(key))

    if not approved:
        print("REFUSED: a paid run needs --execute and --approve-spend equal to --budget-usd. Nothing was started.", file=sys.stderr)
        return 2
    open_items = blockers(root, claude_binary=args.claude_binary, key_present=bool(key), execute_approved=approved)
    if open_items:
        print("REFUSED: open blockers (nothing was started):", file=sys.stderr)
        for b in open_items:
            print(f"  - {b}", file=sys.stderr)
        return 2
    free = (free_bytes or (lambda q: shutil.disk_usage(q).free))(root)
    if free < MIN_FREE_BYTES:
        print(f"REFUSED: {free / 1024**3:.2f} GiB free, {MIN_FREE_BYTES / 1024**3:.0f} GiB required. No Docker API call was made.",
              file=sys.stderr)
        return 2

    try:
        prepared = m2_run.prepare(root, instance_id=args.instance, label=args.config, budget_usd=args.budget_usd,
                                  wall_clock_s=args.wall_clock_s, claude_binary=args.claude_binary, real_key=key,
                                  agent_network=args.agent_network, proxy_network=args.proxy_network, egress_network=args.egress_network,
                                  max_concurrent=args.max_concurrent, calibration=args.calibration)
    except Exception as e:  # any local failure: nothing was sent to Docker
        print(f"ERROR before Docker: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    client = client_factory()
    status = 1
    try:
        kwargs = {} if run_fn is None else {"run_fn": run_fn}
        base = root / "runs" / ("calibration" if args.calibration else "m2")  # calibration runs are kept apart from the scored pipeline's directory
        ex = m2_run.execute(client, prepared, base=base, root=root, **kwargs)
        _report(ex)
        status = 0 if not ex.problems and ex.lifecycle is not None and ex.lifecycle.status == "completed" else 1
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
    finally:
        try:
            client.close()
        except Exception as e:
            print(f"WARNING: closing the Docker client failed: {type(e).__name__}: {e}", file=sys.stderr)
            status = 1
    return status


def _plan(root: Path, args: argparse.Namespace, key_present: bool) -> int:
    prompts = load_prompts(root / "config" / "workflow_prompts.json")
    sample = TaskInput(args.instance, "owner/repo", "0" * 40, "<the issue text goes here>")
    text = build_task_text(args.config, sample, prompts)
    print(f"PLAN (nothing is started; no Docker, no network): instance {args.instance}, config {args.config}")
    rec = prompt_record(args.config, text, prompts)
    print("registered prompt hashes:", json.dumps({k: rec[k] for k in ("label", "template_sha256", "workflow_sha256")}, sort_keys=True))
    print("(the hash of the final prompt is computed per run, once the real issue text is inserted)")
    print("claude args:  ", " ".join(claude_args("claude-sonnet-5-5", args.budget_usd)))
    print(f"limits: wall clock {args.wall_clock_s}s, gateway budget ${args.budget_usd} (client estimate ${client_budget_usd(args.budget_usd)}), max concurrent requests {args.max_concurrent}, "
          f"networks {args.agent_network} / {args.proxy_network} / {args.egress_network} (not checked: Docker)")
    print("NOTICE: the gateway budget is a SOFT limit (a run can end above it by the request in flight; an interrupted request is charged at an upper bound) and the client budget "
          "is an ESTIMATE: not a hard spending limit. Only a spend limit on the dedicated API key is one.")
    if args.calibration:
        print(f"CALIBRATION {args.calibration}: recorded in run.json, written under runs/calibration/, never a scored A/B observation.")
    open_items = blockers(root, claude_binary=args.claude_binary, key_present=key_present, execute_approved=False)
    print("open blockers before a real run:" if open_items else "no open local blockers")
    for b in open_items:
        print(f"  - {b}")
    return 0


def _report(ex: m2_run.Execution) -> None:
    lc = ex.lifecycle
    print(f"run {ex.run_id}: lifecycle {lc.status if lc else 'none'}; artifacts in {ex.directory}")
    if "calibration" in str(ex.directory.parent.name):
        print("  CALIBRATION run: not a scored A/B observation.")
    if lc and lc.agent:
        a = lc.agent
        print(f"  agent status {a.status}, valid {a.valid}, claude exit {a.claude_exit_code}, container removed {a.removed}")
    if lc:
        print(f"  gateway stop reason {lc.gateway_stop_reason}, removed {lc.gateway_removed}")
    print("  artifact verification:", "OK" if not ex.problems else "; ".join(ex.problems))
    if ex.eval_command:
        print("  evaluation NOT run. To grade (needs separate approval):")
        print(f"    {ex.eval_command}")
    else:
        print("  no patch: nothing to evaluate")


if __name__ == "__main__":
    sys.exit(main())

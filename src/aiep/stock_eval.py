"""Run the stock SWE-bench harness with reduced evaluation-container privileges.

swebench 5.0.2 (`run_evaluation.create_container`) creates every evaluation
container with ``cap_add=["SYS_ADMIN"]`` unconditionally (the code comment says
it is for browser sandboxes such as karma). The evaluator executes untrusted,
agent-written patches, so we do not want that.

We do not edit the installed package. For this process only, we wrap
``docker.models.containers.ContainerCollection.create`` so containers are created
with: no SYS_ADMIN, all capabilities dropped, no-new-privileges, and CPU, memory
and pid limits. Grading logic, eval scripts and test behaviour are untouched.
Every create call's final kwargs are appended to a JSONL file for the record.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MEM_LIMIT = "2g"
PIDS_LIMIT = 1024
NANO_CPUS = 2_000_000_000

_FORBIDDEN = ("privileged", "volumes", "mounts", "network_mode", "pid_mode", "devices")


def harden_create_kwargs(kwargs: dict) -> dict:
    """Return a copy of container-create kwargs with reduced privileges."""
    out = dict(kwargs)
    for key in _FORBIDDEN:
        if out.get(key):
            raise ValueError(f"refusing to create container with {key}={out[key]!r}")
    caps = [c for c in (out.pop("cap_add", None) or []) if c != "SYS_ADMIN"]
    if caps:
        raise ValueError(f"unexpected extra capabilities requested: {caps}")
    out["cap_drop"] = ["ALL"]
    out["security_opt"] = ["no-new-privileges"]
    out["mem_limit"] = MEM_LIMIT
    out["memswap_limit"] = MEM_LIMIT
    out["pids_limit"] = PIDS_LIMIT
    out["nano_cpus"] = NANO_CPUS
    return out


def install_shim(log_path: Path) -> None:
    """Wrap ContainerCollection.create for this process; log final kwargs."""
    from docker.models.containers import ContainerCollection

    original = ContainerCollection.create

    def create(self, image, command=None, **kwargs):
        hardened = harden_create_kwargs(kwargs)
        with open(log_path, "a") as f:
            f.write(json.dumps({"image": image, "command": command, **hardened}) + "\n")
        return original(self, image, command, **hardened)

    ContainerCollection.create = create


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", default="SWE-bench/SWE-bench_Lite")
    p.add_argument("--split", default="test")
    p.add_argument("-i", "--instance", action="append", required=True)
    p.add_argument("-p", "--predictions", required=True, help="path to .jsonl, or 'gold'")
    p.add_argument("--run-id", required=True)
    p.add_argument("-t", "--timeout", type=int, default=900)
    p.add_argument("--report-dir", default="reports")
    args = p.parse_args(argv)

    install_shim(Path("create_kwargs.jsonl"))

    from swebench.harness.run_evaluation import main as run_evaluation

    run_evaluation(
        dataset_name=args.dataset,
        split=args.split,
        instance_ids=args.instance,
        predictions_path=args.predictions,
        max_workers=1,  # sequential only (8 GB host)
        open_file_limit=4096,
        run_id=args.run_id,
        timeout=args.timeout,
        rewrite_reports=False,
        modal=False,
        report_dir=args.report_dir,
        task_repo=None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

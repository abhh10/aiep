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

IMAGE POLICY (fail closed). The upstream harness falls back to ``client.images.pull(test_spec.image)`` when its image is not found locally. AIEP evaluation must only ever use an
already-present image whose ID was verified before the run, so inside this process ``main`` first installs ``install_image_guard``: every Docker SDK path that acquires or
imports an image (``ImageCollection.pull/build/load`` and the low-level ``pull``, ``build``, ``load_image``, ``import_image*`` they call) raises ``ImageAcquisitionBlocked``.
That error derives from ``BaseException`` ON PURPOSE: the harness wraps container creation in ``except Exception`` and would turn an ordinary exception into a normal
evaluation error. The guard also writes ``IMAGE_POLICY_VIOLATION.txt`` in the working directory, ``main`` exits 97, and ``aiep.grading.grade`` raises ``ImagePolicyViolation``
if it sees either. Ordinary lookup (``images.get``, ``images.list``) and container creation are untouched. The guard exists only in this process; ``scripts/m1_eval.sh`` and
``aiep.grading`` both run ``python -m aiep.stock_eval``, so they are guarded too. Running ``swebench.harness.run_evaluation`` directly is NOT a supported AIEP evaluation path.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

MEM_LIMIT = "2g"
PIDS_LIMIT = 1024
NANO_CPUS = 2_000_000_000

_FORBIDDEN = ("privileged", "volumes", "mounts", "network_mode", "pid_mode", "devices")

POLICY_MARKER = "IMAGE_POLICY_VIOLATION.txt"
EXIT_IMAGE_POLICY = 97


class ImageAcquisitionBlocked(BaseException):
    """AIEP evaluation requires a PRE-EXISTING, locally verified image: the evaluator may not pull, build or load one. A BaseException so that the harness's
    ``except Exception`` handlers cannot convert it into an ordinary evaluation failure."""


# (module, class, methods) of every Docker SDK path that acquires or imports an image. The collection methods call the low-level ones, so both layers are guarded.
_IMAGE_ACQUISITION = (
    ("docker.models.images", "ImageCollection", ("pull", "build", "load")),
    ("docker.api.image", "ImageApiMixin", ("pull", "load_image", "import_image", "import_image_from_data", "import_image_from_file", "import_image_from_image",
                                          "import_image_from_stream", "import_image_from_url")),
    ("docker.api.build", "BuildApiMixin", ("build",)),
)


def _blocker(what: str, marker: Path) -> Callable[..., Any]:
    def blocked(*args: Any, **kwargs: Any) -> Any:
        message = (f"{what} was called: AIEP evaluation requires a pre-existing verified image and never pulls, builds or loads one. "
                   "Pull the image by its verified digest in a separately approved step, check its ID against the pin, then evaluate.")
        try:
            with open(marker, "a") as f:
                f.write(message + "\n")
        except OSError:
            pass  # the exception below is the primary signal
        raise ImageAcquisitionBlocked(message)
    blocked._aiep_image_guard = True  # type: ignore[attr-defined]
    return blocked


def install_image_guard(marker: Path | str = POLICY_MARKER) -> Callable[[], None]:
    """Block image acquisition in THIS process (not Docker itself). Returns a function that restores the original methods. Idempotent. If the SDK no longer has a method
    named here the guard refuses to install (fail closed) instead of silently leaving a path open."""
    marker = Path(marker).resolve()
    patched: list[tuple[type, str, Any]] = []
    for module, cls_name, names in _IMAGE_ACQUISITION:
        cls = getattr(importlib.import_module(module), cls_name)
        for name in names:
            original = cls.__dict__.get(name)
            if original is None:
                for _c, _n, _o in reversed(patched):
                    setattr(_c, _n, _o)
                raise RuntimeError(f"{cls_name}.{name} does not exist in this Docker SDK: the image guard cannot be installed, so evaluation is refused")
            if getattr(original, "_aiep_image_guard", False):
                continue
            setattr(cls, name, _blocker(f"{cls_name}.{name}", marker))
            patched.append((cls, name, original))

    def restore() -> None:
        for c, n, o in reversed(patched):
            setattr(c, n, o)
    return restore


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

    restore_images = install_image_guard()  # BEFORE the upstream harness is imported or run
    try:
        install_shim(Path("create_kwargs.jsonl"))

        from swebench.harness.run_evaluation import main as run_evaluation

        try:
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
        except ImageAcquisitionBlocked as e:
            print(f"IMAGE POLICY VIOLATION: {e}", file=sys.stderr)
            return EXIT_IMAGE_POLICY
        return 0
    finally:
        restore_images()  # the process normally ends here; restoring keeps in-process callers (tests) clean


if __name__ == "__main__":
    sys.exit(main())

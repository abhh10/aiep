"""What stands between an M2 handoff and a graded result: a read-only assessment of the two evaluators. It runs nothing and changes nothing.

Two evaluators exist and only one can grade:

* ``stock_eval`` (M1, driven by ``scripts/m1_eval.sh``) is AUTHORITATIVE. It wraps the stock SWE-bench harness (swebench 5.0.2) with reduced container
  privileges and was validated in M1 (two gold runs, one no-op). It needs network access during grading (``pip install -e .`` fetches its build
  dependencies from PyPI) and at least 16 GiB of free disk to start.
* ``evaluator`` (the hardened successor) refuses to grade without an attestation bound to a populated wheelhouse, and has no network, so it cannot grade
  today. It must not be weakened to make it run.

``assess`` reports, for each, exactly what blocks it now. Inputs that need Docker or the host are injectable, so the logic is unit-tested without either.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any, Callable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
GIB = 1024**3
DRIVER = "scripts/m1_eval.sh"


def _driver_numbers(root: Path) -> dict[str, Any]:
    text = (root / DRIVER).read_text()
    start = re.search(r"^MIN_START=(\d+)", text, re.M)
    instance = re.search(r"^INSTANCE=(\S+)", text, re.M)
    image = re.search(r"^IMAGE=(\S+)", text, re.M)
    hard = re.search(r"below12\(\).*?f<(\d+)", text)
    return {"min_start_gib": int(start.group(1)) if start else None, "hard_stop_gib": int(hard.group(1)) if hard else 12,
            "instance": instance.group(1) if instance else None, "image": image.group(1) if image else None,
            "sets_hf_offline": "HF_DATASETS_OFFLINE" in text or "HF_HUB_OFFLINE" in text,
            "absolute_paths": "/Users/" in text}


def assess(root: Path = REPO_ROOT, *, env: Mapping[str, str] | None = None, free_gib: float | None = None,
           image_id_of: Callable[[str], str | None] | None = None) -> dict[str, Any]:
    root = Path(root)
    env = os.environ if env is None else env
    drv = _driver_numbers(root)
    free = free_gib if free_gib is not None else shutil.disk_usage(root).free / GIB
    images = json.loads((root / "config" / "evaluator_images.json").read_text())["images"]
    wheelhouse = json.loads((root / "config" / "wheelhouse.json").read_text())
    evaluator_src = (root / "src" / "aiep" / "evaluator.py").read_text()
    policy_src = (root / "src" / "aiep" / "container_policy.py").read_text()

    # ------------------------------------------------------------ stock M1 evaluator
    stock_blockers: list[str] = []
    notes: list[str] = []
    if drv["min_start_gib"] is not None and free < drv["min_start_gib"]:
        stock_blockers.append(f"free disk is {free:.1f} GiB; the driver refuses to start below {drv['min_start_gib']} GiB (and hard-stops a run below {drv['hard_stop_gib']} GiB)")
    if image_id_of is None:
        notes.append("the evaluator image was not checked against Docker (pass --check-docker)")
    else:
        for inst, spec in images.items():
            got = image_id_of(spec["image"])
            if got != spec["image_id"]:
                stock_blockers.append(f"image for {inst} is {got!r} locally, expected {spec['image_id']}; the stock harness would silently PULL a missing image by its mutable tag")
    notes += [
        "NETWORK REQUIRED: the evaluation container runs `pip install -e .`; pytest's pyproject.toml requires setuptools>=40.0, setuptools-scm and wheel at build time, "
        "fetched from PyPI by build isolation (measured: with the network off the step failed in 9 s). Versions are not pinned or recorded (docs/M1_RESULTS.md 8.4).",
        "the container runs on Docker's default bridge with unrestricted egress while it executes agent-written code (docs/M1_RESULTS.md 8.3)",
        "the driver is hardcoded to " + str(drv["instance"]) + " and to absolute paths under /Users/<user>; only that instance can be graded through it",
        "pytest-dev__pytest-5227 lists 3 junk PASS_TO_PASS entries (log fragments): it proves the environment, not the agent",
    ]
    if not drv["sets_hf_offline"]:
        notes.append("the driver does not set HF_DATASETS_OFFLINE / HF_HUB_OFFLINE, so the harness's dataset load may contact the hub; the dataset revision is not recorded by it")
    stock = {"authoritative": True, "executable": not stock_blockers, "blockers": stock_blockers, "notes": notes, "needs_network": True,
             "disk": {"free_gib": round(free, 2), "min_start_gib": drv["min_start_gib"], "hard_stop_gib": drv["hard_stop_gib"],
                      "shortfall_gib": round(max(0.0, (drv["min_start_gib"] or 0) - free), 2)}}

    # ------------------------------------------------------------ hardened evaluator
    hb: list[str] = []
    if wheelhouse.get("status") != "populated" or not wheelhouse.get("wheels"):
        hb.append("config/wheelhouse.json is not populated (status %r, %d wheels): gate.current_components refuses, so no attestation can exist" % (
            wheelhouse.get("status"), len(wheelhouse.get("wheels") or [])))
    key = env.get("AIEP_ATTEST_KEY_FILE")
    if not key:
        hb.append("AIEP_ATTEST_KEY_FILE is not set (the HMAC key for attestations; evaluator.py exits 2 without it)")
    else:
        try:
            st = Path(key).stat()
            if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                hb.append("the attestation key file is accessible by group or others")
        except OSError:
            hb.append("the attestation key file named by AIEP_ATTEST_KEY_FILE is not readable")
    probe_tooling = any("network_blocked" in p.read_text() for p in (root / "scripts").glob("*.py")) if (root / "scripts").is_dir() else False
    if not probe_tooling:
        hb.append("no tool exists that runs the egress probe and builds the attestation (gate.build_attestation exists, nothing calls it)")
    if "put_archive" not in evaluator_src and "WHEELHOUSE_DIR" not in evaluator_src:
        hb.append("nothing injects the wheelhouse into the evaluation container: container_policy sets PIP_FIND_LINKS=/opt/aiep-wheelhouse and PIP_NO_INDEX=1, "
                  "but the stock harness creates and runs the container itself and evaluator.py has no injection step")
    if "network_mode" in policy_src and 'network_mode="none"' in policy_src:
        hb.append("evaluation containers get network_mode=none (correct), so with no wheelhouse `pip install -e .` cannot fetch its build dependencies and every grading would fail")
    hardened = {"authoritative": False, "executable": not hb, "blockers": hb, "needs_network": False}
    return {"schema": 1, "authoritative_evaluator": "aiep.stock_eval via scripts/m1_eval.sh (M1; validated)", "stock_m1": stock, "hardened": hardened,
            "driver": drv, "images": {k: v["image_id"] for k, v in images.items()},
            "note": "read-only assessment: nothing was run, downloaded or changed"}

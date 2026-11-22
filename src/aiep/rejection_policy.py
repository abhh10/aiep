"""Classify handoff outcomes for the M3 batch, so it can decide between halting and continuing (defect 1 of the M3.7 audit).

A handoff report lists the checks that REJECTED a run. A rejection can mean four different things, and the batch must treat them differently:

* ``agent``      the agent's own behaviour (it ran out of time, exited non-zero, produced no patch or one that does not apply): a legitimate, recorded, invalid-for-
                 measurement run. The batch continues.
* ``transient``  an infrastructure failure that can plausibly be a one-off (lifecycle, lease, orchestrator, network attachment): counted by the registered
                 consecutive-failure policy (``consecutive_invalid_runs``, N = 2).
* ``accounting`` the gateway ledger cannot be trusted to be complete: usage may have been billed and not recorded. Halts at once.
* ``hidden_leak`` hidden task data reached the agent's input. Halts at once.
* ``integrity``  everything else: a deterministic setup or integrity failure (seed, image, prompts, binary, flags, identity, dataset record, artifacts ...). It will
                 repeat on every run, so the batch halts at once rather than pay for the rest. This is also the DEFAULT for any check name not listed here.

Rules are keyed by a phrase from the stable check names in ``aiep.handoff`` (the first phrase found in a name wins); ``tests/test_rejection_policy.py`` reads handoff.py
and fails if a rejecting check is unclassified or a rule matches nothing. Uncertain financial exposure (a request cut off and charged at the upper bound) is reported
separately, from the handoff's flags: it does not reject the run but it halts the batch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

AGENT, TRANSIENT, ACCOUNTING, HIDDEN_LEAK, INTEGRITY = "agent", "transient", "accounting", "hidden_leak", "integrity"
PATCH_OUTPUTS = frozenset({"predictions.jsonl", "patch.diff"})  # missing because the agent produced no usable patch

RULES: dict[str, str] = {
    # agent behaviour
    "Claude Code exited on its own with status 0": AGENT,
    "Claude's output was NOT truncated": AGENT,
    "the patch passes the production patch validator": AGENT,
    "the exact model_patch applies cleanly": AGENT,
    "every required artifact is present": AGENT,  # refined by detail in classify_check
    # transient infrastructure
    "the lifecycle completed": TRANSIENT,
    "the agent run is VALID": TRANSIENT,
    "the orchestrator was not interrupted": TRANSIENT,
    "the agent's post-start network attachment check passed": TRANSIENT,
    # accounting and financial exposure
    "the gateway ledger is COMPLETE": ACCOUNTING,
    "the ledger recorded at least one billed request": ACCOUNTING,
    # hidden data
    "the agent's INPUT": HIDDEN_LEAK,
    # deterministic setup / integrity (explicit decisions; anything unlisted is the same by default)
    "artifacts_manifest.json verifies": INTEGRITY,
    "run.json is a JSON object": INTEGRITY,
    "run.json schema is the one": INTEGRITY,
    "the directory name is a well-formed run id": INTEGRITY,
    "run.json run_id, task.instance_id and config agree": INTEGRITY,
    "the model is the experiment's single pinned model": INTEGRITY,
    "the task has a registered evaluator image": INTEGRITY,
    "the agent ran in the SAME image": INTEGRITY,
    "predictions.jsonl is exactly one": INTEGRITY,
    "the prediction is a JSON object with exactly the harness keys": INTEGRITY,
    "the prediction's instance_id is this run's task": INTEGRITY,
    "the prediction's model_name_or_path": INTEGRITY,
    "model_patch is byte-identical to patch.diff": INTEGRITY,
    "the patch's sha256, size and status equal": INTEGRITY,
    "the seed the agent started from is the pinned seed": INTEGRITY,
    "the seed was taken from the evaluator's image": INTEGRITY,
    "the pinned local dataset record could be loaded": INTEGRITY,
    "run.json repo and base_commit equal": INTEGRITY,
    "the pinned seed's HEAD equals": INTEGRITY,
    "the pinned seed could be materialised": INTEGRITY,
    "the run used one of the registered prompt sets": INTEGRITY,
    "the run used the registered prompt template": INTEGRITY,
    "the recorded prompt hash equals": INTEGRITY,
    "the Claude binary recorded is the pinned one": INTEGRITY,
    "the Claude flags are exactly": INTEGRITY,
    "the gateway image is the pinned one": INTEGRITY,
    "the run carries a complete experiment identity": INTEGRITY,
    "the run's slot is a scheduled slot": INTEGRITY,
    "the three AIEP networks were verified": INTEGRITY,
    "the run is not a calibration run": INTEGRITY,
    "the gateway did not stop the run for a violation": INTEGRITY,
}
EXACTNESS_FLAG = "the usage figures are EXACT"


def classify_check(name: str, detail: Any) -> tuple[str, str]:
    """(category, why) of one rejecting check. Unknown names are INTEGRITY with why 'unclassified' (fail closed)."""
    for key, cat in RULES.items():
        if key in name:
            if key == "every required artifact is present":
                missing = detail.get("missing") if isinstance(detail, Mapping) else None
                if not (isinstance(missing, list) and missing and set(missing) <= PATCH_OUTPUTS):
                    return INTEGRITY, "required artifacts other than the patch outputs are missing (or not stated)"
                return AGENT, "the agent produced no usable patch"
            if key == "the agent run is VALID":
                return _by_invalid_reason(detail)
            return cat, key
    return INTEGRITY, "unclassified"


def _by_invalid_reason(detail: Any) -> tuple[str, str]:
    """The VALID check fails for several unrelated causes; its detail says which. Unknown or missing detail is INTEGRITY (fail closed)."""
    d = detail if isinstance(detail, Mapping) else {}
    reason, stop = str(d.get("invalid_reason") or ""), d.get("stop_reason")
    if stop == "metering_failure":
        return ACCOUNTING, "the gateway reported a metering failure"
    if stop in ("model_violation", "tool_violation"):
        return INTEGRITY, f"the gateway stopped the run for a violation: {stop}"
    if stop in ("lease_lost", "gateway_unreachable", "gateway_died", "proxy_died") or "lease" in reason or "attachment check failed" in reason:
        return TRANSIENT, "an infrastructure failure (lease, gateway, proxy or network attachment)"
    if "identity check failed" in reason or "does not match its manifest" in reason:
        return INTEGRITY, "the agent environment did not match its registered identity or injected files"
    if "output truncated" in reason:
        return AGENT, "the agent's output exceeded the capture cap"
    return INTEGRITY, "the run is invalid for an unrecognised reason"


@dataclass(frozen=True)
class Classification:
    categories: set[str] = field(default_factory=set)
    items: tuple[tuple[str, str, str], ...] = ()  # (category, check name, why)
    financial_uncertain: bool = False

    def has(self, *cats: str) -> bool:
        return bool(self.categories & set(cats))


def classify_handoff(handoff: Mapping[str, Any]) -> Classification:
    """Classify a handoff report dict. Uses ``checks`` (with details) when present, else the ``rejections`` names. A rejected run with no stated reason is INTEGRITY."""
    status = handoff.get("status")
    flags = [str(f) for f in handoff.get("flags") or []]
    uncertain = any(EXACTNESS_FLAG in f for f in flags)
    if status in ("ready", "ready_flagged"):
        return Classification(set(), (), uncertain)
    details = {c.get("check"): c.get("detail") for c in handoff.get("checks") or [] if isinstance(c, Mapping)}
    names = [str(n) for n in handoff.get("rejections") or []]
    if status != "rejected" or not names:
        return Classification({INTEGRITY}, ((INTEGRITY, str(status), "rejected or unreadable handoff without a stated reason"),), uncertain)
    items = tuple((cat, n, why) for n in names for cat, why in [classify_check(n, details.get(n))])
    return Classification({i[0] for i in items}, items, uncertain)

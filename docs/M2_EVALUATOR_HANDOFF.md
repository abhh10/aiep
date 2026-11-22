# M2 to M1 evaluator handoff

Status (2026-11-20): handoff validation is implemented and tested against the real mock-integration run; **the evaluator cannot grade yet** (section 4). Nothing here grades a task, downloads anything or changes an evaluator.

## 1. Evaluator architecture

| | `aiep.stock_eval` + `scripts/m1_eval.sh` (M1) | `aiep.evaluator` (hardened successor) |
|---|---|---|
| status | **authoritative**; validated in M1 (2 gold runs resolved, 1 no-op unresolved) | cannot grade: blocked by its own gate |
| harness | stock SWE-bench 5.0.2 `run_evaluation`; wrapper only strips `SYS_ADMIN`, drops all capabilities, no-new-privileges, 2 GiB / 1024 pids / 2 CPUs | same harness behind the gate, container-policy allowlist, `images.pull/build/load/push` refused, image ID verified |
| network | **needs it**: `pip install -e .` fetches `setuptools>=40.0`, `setuptools-scm`, `wheel` (and their dependencies) from PyPI; unrestricted egress on the default bridge while agent code runs | none (`network_mode=none`), so it needs a wheelhouse |
| offline | no | yes, once the wheelhouse is injected |
| grading | the harness's own: it applies `model_patch` (`git apply`, with fallbacks), then the hidden `test_patch` through the dataset's eval script, and parses the log. **Resolved = every FAIL_TO_PASS passes and every PASS_TO_PASS is maintained** | identical (same harness) |
| inputs | an instance id, `-p <predictions.jsonl>`, a run id. No configuration label exists anywhere in it | same, plus an attestation |

Required prediction schema (swebench `get_predictions_from_file`, `.jsonl`): one JSON object per line with exactly `instance_id`, `model_name_or_path`, `model_patch` (strings). M2 writes exactly one line (`patch.write_predictions`). The patch is a `git diff` against the seed's own HEAD, the synthetic "SWE-bench" commit whose parent is the dataset `base_commit` (`2051e30b…` for pytest-5227; verified in the pinned seed), repository `pytest-dev/pytest`.

The M1 driver is hardcoded to `pytest-dev__pytest-5227`, to absolute paths under `/Users/<user>` (the original author's home directory), requires 16 GiB free to start and hard-stops a run below 12 GiB. It does not set `HF_*_OFFLINE`, and does not record the dataset revision or the pip build-dependency versions (`M1_RESULTS.md` 8.3 to 8.5).

## 2. Handoff validation (`src/aiep/handoff.py`, `scripts/validate_handoff.py`)

`PYTHONPATH=src .venv/bin/python scripts/validate_handoff.py runs/m2*/<run_id>` returns one of three verdicts and writes a report to `runs/handoff/` (git-ignored). It never contacts Docker or the network and never says a task is solved (`grading.resolved` is always `null`).

* **ready**: every check passed, including complete AND exact accounting.
* **ready_flagged**: the patch handoff is sound and may be graded, but the run is not a clean measurement (upper-bound charge, budget reached or exceeded, cleanup failure, a skipped optional check). The flags travel with the report and the run must be labelled accordingly.
* **rejected**: a missing or tampered artifact, a malformed or mismatched prediction, a patch that does not apply to the pinned seed, an unregistered task / image / seed / prompt / model / binary / flags, an invalid, censored, interrupted, failed or killed run, incomplete accounting, or hidden data in the agent's input.

What it verifies: all artifacts present and the manifest hashes recomputed; `predictions.jsonl` is exactly one line with exactly the three harness keys, and its `model_patch` is byte-identical to `patch.diff`, whose sha256 equals what the agent run recorded and passes the production patch validator; run id, task, config, model and `aiep-<config>-<model>` agree; the agent ran in the evaluator's image; the seed equals its pin, its HEAD's parent equals the dataset `base_commit`, and `git apply --check` succeeds on a fresh copy of the pinned seed; prompt hashes equal the registered ones, the Claude binary and flags and the gateway image equal their pins; the post-start attachment check passed; the gateway ledger is complete and the figures exact; and a leak scan of the agent's input against the hidden FAIL_TO_PASS / PASS_TO_PASS ids, test patch and gold patch (counts only in the report).

Result on the real runs of the mock integration: `...A-20261120T131153Z` **ready** (44 checks, 0 flags); the proxy-killed run `...131220Z` **rejected** (no patch; agent invalid `proxy_died`; Claude never exited); the first looping run `...130942Z` **rejected** (stopped by the gateway budget, flagged $0.5014 > $0.50). None was graded.

## 3. Research integrity

* **Prompts**: template `86571bc7…`, A `5e44cde5…`, B `f7ad0072…` are recomputed from the text on every load (`load_prompts`), asserted by `tests/test_research_integrity.py`, and checked against every handed-off run. Unchanged.
* **Same task and grading conditions for A and B**: the evaluators take no label (an instance id, a predictions file, a run id); the Claude flags and container environment are built without a label input; the prompts differ only in the workflow line. The same pinned image, seed, binary, model, limits and evaluator apply to both.
* **Hidden data**: the agent receives an allowlisted view (`task_input.export_task_input`); agent-side modules never import the dataset loader; FAIL_TO_PASS / PASS_TO_PASS, the test patch and the gold patch exist only host-side. The handoff scan found none in the mock run's input or output.
* **Independence**: the orchestrator never imports or runs the evaluator (`evaluation: "not run"` in every `run.json`); the evaluator starts a fresh container from the evaluator image and receives only a text patch.
* **No silent successes**: predictions exist only for a valid run with an ok patch; a failed, censored, interrupted or incomplete-accounting run has none, or is rejected by the handoff gate, and an inexact one is flagged. Claude's own `total_cost_usd` is a client-side estimate and is never used.
* **Budget overrun (current behaviour, deliberately unchanged)**: a request cut off mid-stream (proxy or gateway death, upstream error) is charged at an upper bound: its input as reported plus `max_tokens` output. With `max_tokens` 64000 at $10 per million output tokens that is about $0.64 for a single request, more than the $0.50 budget in the mock runs. Observed: $0.644 (proxy killed mid-stream) and $0.5014 (the looping run: the gateway stops only after the request that reaches the budget). Bounds: the gateway allows 2 concurrent upstream requests, so the overshoot is at most the two in-flight requests' upper bounds; Claude's own budget is 1.1x the gateway's. Consequence: a budget is a soft limit by up to one or two worst-case requests, and such runs are `ready_flagged` at best. The accounting model was not changed.

## 4. Why the evaluator cannot grade today

Stock M1 evaluator (can run once its gate is met): **free disk 15.07 GiB < 16 GiB start gate (shortfall 0.93 GiB)**. It also needs network access for pip and the pytest-5227-only driver. It would pull a missing image by its mutable tag (the image is present: ID `c91459f8…`).

Hardened evaluator (`scripts/evaluator_readiness.py` lists these live):
1. `config/wheelhouse.json` is unpopulated, so `gate.current_components` refuses and no attestation can exist.
2. `AIEP_ATTEST_KEY_FILE` is unset (no HMAC key).
3. No tool runs the egress probe and builds the attestation; `gate.build_attestation` exists and nothing calls it.
4. Nothing injects the wheelhouse into the evaluation container; the stock harness creates and runs it, and `evaluator.py` has no injection step.
5. With `network_mode=none` and no wheelhouse every `pip install -e .` fails.

## 5. Smallest safe path and what it needs

**A. Grade the one supported task with the M1 evaluator (no code change).** Needs only free disk >= 16 GiB, and the evaluation container's own pip network access (about 1.4 MB of pure-Python wheels, below). Run `scripts/m1_eval.sh m2eval-<run_id> <abs path to predictions.jsonl>` for a run whose handoff report is `ready` or `ready_flagged`.

**B. Make the hardened evaluator executable (the end state, still not weakening anything).** Resolved set of build requirements for pytest-5227's `pyproject.toml` on the image's Python 3.9 (resolved with `uv pip install --dry-run`, nothing downloaded; sizes and hashes from PyPI metadata, NOT yet verified against downloaded files):

| wheel | bytes | sha256 (PyPI listing) |
|---|---|---|
| packaging-26.3-py3-none-any.whl | 129,956 | d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c |
| setuptools-82.0.1-py3-none-any.whl | 1,006,223 | a59e362652f08dcd477c78bb6e7bd9d80a7995bc73ce773050228a348ce2e5bb |
| setuptools_scm-10.3.4-py3-none-any.whl | 33,366 | 82f34c3e3084fc2b57d397200637cc13f2338004052d2fee47baa5f0e902464e |
| tomli-2.5.0-py3-none-any.whl | 14,765 | 32a7b79ac57a2e83670ce329ccf675798bc5a2094783a63676866b70503f2e2b |
| typing_extensions-4.16.0-py3-none-any.whl | 45,571 | 481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8 |
| vcs_versioning-2.6.0-py3-none-any.whl | 123,154 | f8bebd1302ebc12e67ceb30fc7dd698ec969414b6fa43b30c3f14ac9b1e72c41 |
| wheel-0.48.0-py3-none-any.whl | 33,320 | 3217dcc807155e45db462d7ef2431f5ddda0d7273b700d05a67b271ceb1287ab |

Total 1,386,355 bytes (1.3 MiB). Steps, each needing approval: download these 7 wheels and verify their hashes; populate `config/wheelhouse.json`; add a wheelhouse injection step to the evaluator (put_archive into `/opt/aiep-wheelhouse` right after container creation); add the egress-probe and attestation tool; generate the HMAC key (chmod 600, outside the repo); verify offline that `pip install -e .` succeeds with `PIP_NO_INDEX=1` and `network_mode=none`; then grade the gold patch and a no-op under it before trusting it.

## 6. Disk recovery options (nothing was deleted)

Needed: >= 0.93 GiB more free space (a gold run consumed about 1 GiB; the 12 GiB hard stop would then hold). Measured candidates, safest first:
1. Free space outside Docker: `~/Library/Caches` 4.7 GiB, `~/.cache` 1.3 GiB, `~/Downloads` 1.6 GiB (user's choice; none inspected for content).
2. `.cache/claude/2.1.328/claude` 356 MiB: re-downloadable and hash-pinned, but needed for the next real-Claude run.
3. Docker: 6.16 GB of unused images, 3.06 GB of volumes and 0.93 GB of build cache are reclaimable, **all belonging to unrelated projects** (cdc-lakehouse etc.); pruning them is destructive and needs the owner's explicit approval, and compacting `Docker.raw` (22.1 GiB allocated) is a Docker Desktop setting change.
Do not lower the 16 GiB start gate or the 12 GiB hard stop.

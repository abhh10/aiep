# M3 experiment design (feasibility pilot)

**This is a feasibility pilot (8 tasks, one run per arm per task). It is not a statistically powered effectiveness study.** A paired binary comparison needs
roughly 190 tasks to detect a 10-point difference (25% discordant pairs); everything the analysis prints is descriptive.

Status: implemented and unit-tested with fakes only. No API request, image download, control run, agent run or grading has happened for M3.
The CURRENT definition is `config/experiments/m3-pilot-v2.json` (`draft`, hash in `scripts/m3_experiment.py validate`): budgets are `null`, so nothing can spend until an approved
real-API calibration sets `budget_usd_per_run` and `max_total_computed_usd` and the definition is re-hashed and locked. `m3-pilot-v1.json` is preserved UNCHANGED as a superseded,
never-run draft (sha256 `f2ae65e5...`); the batch refuses to execute it (it does not register the M3.7 stop conditions). See "M3.7 fixes" below.

## Definition (`src/aiep/experiment_def.py`, built by `scripts/m3_experiment.py build`)
Self-hashed JSON: experiment id, `claim_scope`, prompt set (v2 hashes), model, Claude CLI sha256, dataset revision, pre-registration (seed, selection rules, pool
hash, 8 tasks, 12 ordered replacements, replacement and control rules), design (derived schedule, 600 s pair gap), limits (one block shared by both arms),
`financial_notice`, stop conditions, metric definitions, analysis plan. `validate` recomputes everything that is derived (schedule, hashes, repo caps) and checks
it against the repo configs. Tasks: at most 2 per repository, at least 4 repositories, `pytest-dev__pytest-5227` excluded. Selection: format-aware screening
(`task_screening.py`), then rank by `sha256(seed|select|instance_id)`. A definition file is never overwritten.

## Prompts
`config/workflow_prompts_v2.json`: template and Config A are byte-identical to v1 (same hashes); Config B adds "at most 5 test-fix cycles". v1 is untouched.

## Run identity
`run_id` stays `m2-<instance>-<arm>-<stamp>` (64-char limit). `run.json` gains `experiment` = `{experiment_id, task_id, arm, repetition, pair_id, order_index, slot_id}`,
`prompt_set_file`, `gateway_max_concurrent`, `budget_is_soft`. `slot_id = experiment_id/task_id/arm/rN`. The handoff validator checks the block and, given the schedule,
that the slot is scheduled with the scheduled order index and pair id.

## Batch flow (`m3_batch.py`; plan-only by default)
Per block: disk gate -> prepare task (pull, pin, seed, identity; approval) -> gold then no-op control (approval; failure replaces the task BEFORE any agent run) ->
first arm (cost-cap check, run, handoff, grading only if the handoff passed, transcript, results row) -> 600 s gap -> second arm -> image removal by ID.
Halts (need `--ack-halt <id>` to continue): accounting_incomplete, disk_start_gate, disk_hard_stop, cumulative_cost_cap, hidden_data_leak,
control_replacement_exhausted, consecutive_invalid_runs, grading_invalid, task_not_prepared, orchestrator_error. State is bound to the definition hash.
The 16 GiB start gate and 12 GiB hard stop are unchanged (a test compares them with `m1_eval.sh`). The real wiring of `Steps` to Docker, the gateway and the
evaluator is intentionally not included: the per-task scripts (`pull_task_image.py` with an explicit approval flag, `pin_task_image.py`, `extract_task_seed.py`,
`verify_task_identity.py`, `m3_grade.py`) are the approval-gated building blocks.

## Financial safety
The gateway budget is a SOFT limit, not a guaranteed maximum liability. `AIEP_MAX_CONCURRENT=1` (default stays 2) gives one request in flight per agent; this bounds,
not removes, overshoot. Interrupted requests keep the max_tokens upper-bound charge. The accounting model is unchanged; `run.json` adds
`usage_measured.request_status` (by status/outcome, 429, 5xx, gateway-busy, interrupted; `non_success` is a lower-bound retry proxy). Calibration must check
whether Claude Code issues concurrent requests (a gateway-busy 429 would show up there).

## Analysis (`analysis.py`, `scripts/m3_analyze.py`)
`as_scheduled` (invalid/rejected/ungradable runs count as unresolved; unexecuted slots listed as not run) and `valid_runs` (both runs handoff ready/ready_flagged and
graded cleanly), each with the 2x2 concordant/discordant table, exact McNemar, seeded paired bootstrap, cost (also uncached-equivalent), tokens, turns, test usage,
compliance. Transcript metrics (`transcript.py`) use pre-registered definitions: test run, test-fix cycle (failed run then an edit), compliance (B: cycles <= 5).

## M3.6 operational integration (implemented; validated with fakes only)
`m3_steps.RealSteps` wires the batch to the existing components through injected callables; `scripts/run_m3_pilot.py` supplies the real ones (never executed so far).
- **Schedule:** `run_pilot` first re-hashes the definition and re-derives the schedule from the registered seed and tasks; any difference halts (`definition_invalid`) before a step runs. Blocks and slots come from the registered schedule; a replacement changes only the task id inside its slot (same `order_index`, arm, position).
- **Identity:** each slot's `{experiment_id, task_id, arm, repetition, pair_id, order_index, slot_id}` goes into `m2_run.prepare` -> `run.json` -> handoff (checked against the schedule) -> results row (checked again by the batch, `identity_mismatch` halts).
- **Fresh per arm:** every arm calls `prepare` (seed re-loaded and hash-verified from its pin, new containers, new run id); `execute` still owns networks, lease and teardown.
- **Evaluator input:** only the run's `predictions.jsonl` (exactly `instance_id`, `model_name_or_path`, `model_patch`; one line; this task; non-empty), only after ready/ready_flagged. No patch means nothing is graded and the run is never a success.
- **Invalid runs:** rejected or ungradable runs get a row with `graded=false`, `resolved=null`, `valid_measurement=false`: unresolved in `as_scheduled`, excluded from `valid_runs`. An evaluator or control-grading infrastructure failure halts (`grading_invalid`); it never replaces a task or reads as a model failure.
- **Interruption:** a slot is marked `started` durably before its run; Ctrl-C records `interrupted`. `slot_interrupted`, `interrupted`, `definition_*` and `identity_mismatch` halts cannot be acknowledged: an unrecorded run may have spent money.
- **Images:** removed only after both slots of the block have result rows, only if this batch pulled the image, only by exact pinned ID, and never while a container uses it (`image_still_required`). A failed removal is retried on resume without re-running anything.
- **Accounting:** unchanged. The cost cap uses the existing soft-budget exposure estimate.

Mocked validation: `tests/test_m3_integration.py` drives the real batch, `RealSteps`, pins, results builder, transcript parser and analysis against fake Docker, agent, evaluator and handoff. Not validated against reality: the Docker pull/pin/extract/identity calls, `m2_run.execute` under the batch, the real handoff and `grading.grade` inside the batch, and Claude's actual transcript format for B-arm test runs.

## M3.7 fixes (operational-readiness audit; implemented, unit-tested with fakes only)
Why `m3-pilot-v2`: two defects lived inside the hashed v1 definition, so v1 was superseded by a new id before any run instead of being edited. v2 keeps the 8 tasks, the 12
ordered replacements, the seed, arm assignment, block and slot order (only the id embedded in `slot_id`/`pair_id` differs; `tests/test_m3_v2_definition.py` proves it), prompts
v2, limits, analysis plan and financial notice. It changes exactly: the stop-condition list, `metrics.test_command_patterns` -> `metrics.test_command_spec`, the `test_run` wording,
and the `supersedes` block (reason, changes, what is unchanged).

* **Defect 3, seed base commit** (`task_setup.extract_and_pin_seed`): the seed is pinned only if git reports its HEAD equal to the manifest HEAD and `HEAD^` equal to the dataset
  `base_commit` (from the dataset record, never the image). On mismatch nothing is pinned and the extracted files stay as unpinned evidence. Host git stays in the script layer
  (`scripts/validate_handoff.py::seed_lineage`).
* **Defect 1, batch halts** (`rejection_policy.py`, `m3_batch._run_block`): handoff rejections are classified, fail closed, anchored to the check names in `handoff.py` (a test reads
  that file and fails if a rejecting check is unclassified or a rule matches nothing). *Integrity/setup* (and any unclassified check) halts at once (`handoff_integrity_failure`);
  *accounting* (incomplete ledger, no billed request, metering failure) halts at once (`accounting_incomplete`); *uncertain exposure* (the handoff's EXACT flag: a request charged at the
  max_tokens upper bound) halts at once (`financial_exposure_uncertain`); *transient infrastructure* (lifecycle, lease, gateway/proxy death, attachment, orchestrator) uses the
  registered `consecutive_invalid_runs` (n=2) policy; *agent behaviour* (timeout, non-zero exit, no or non-applying patch, truncated output) records an invalid run and continues.
  Accounting semantics are unchanged; the new halts only stop earlier.
* **Defect 2, test-command detection** (`transcript.py`, `TEST_COMMAND_SPEC`): shell commands are parsed into simple commands; the executable position decides. Runner words that are
  arguments of grep/echo/cat/pip/git no longer count; absolute-path pytest, `./tests/runtests.py`, `setup.py test`, `sympy.test(...)` and common wrappers now count. Running a test
  file directly with python is deliberately not counted. The real Claude Code 2.1.328 `tool_result` shape (`is_error`, subagent `parent_tool_use_id`) is STILL UNVERIFIED against a real
  transcript; the parser tolerates string and list content and a missing `is_error`.
* **DRIVER_INSTANCE** (`handoff.evaluator_support`): the flag is now raised only for a task that is neither the M1 driver's instance nor registered (pinned image, seed, verified identity).
  M1/M2 behaviour is unchanged.
* **Preparation only** (`run_m3_pilot.py --prepare-only --task <id> [--approve-pulls]`): pull (if not local, above the 16 GiB gate), pin, seed (with the base-commit check), identity.
  No agent, API, key, evaluator or image removal; it refuses to combine with any execution, spend, control or removal flag.
* **No-op control** (`grading.noop_prediction`, `scripts/m3_noop_predictions.py`, `m3_grade.py <id> <run_id> noop`): deterministic bytes (`NOOP_PATCH_SHA256`), a truthful `index` line,
  never overwritten. The earlier handoff text said its index line was `e69de29` (the empty blob) although the file has one line; it is now git's real blob id for the content.
  The control patch has still never been run through the evaluator.

## Digest-pinned image preparation (implemented; mocks only, no image prepared yet)
The task images are published under the mutable tag `:latest`. `config/image_digests.json` (separate from the hashed definition) holds, per task, the verified OCI index digest, the linux/amd64
platform manifest digest, config and layer digests and sizes, and provenance; today it contains ONLY `sympy__sympy-18621` (verified 2026-11-22). `aiep.image_digests` loads a record
(missing, malformed or inconsistent records fail closed), re-verifies it against the registry before a pull (index body hash and digest header, exactly one linux/amd64 entry equal to the
recorded platform manifest, platform manifest body hash and header, config and layers), pulls `<repository>@<index digest>` for linux/amd64, and requires the local image ID to equal the
index digest. `prepare_pinned_task`, the pull wrapper (`scripts/pull_task_image.py`), `pin_image` and the batch's image-removal lookup all use that one reference; the pin records it as
`digest_reference`. No code path pulls or looks up by tag, and nothing creates a `:latest` tag. The stock evaluator still looks its image up by the `:latest` NAME (`grading.grade`
checks `image_id_of(<repository>:latest)`), so a digest-only image cannot be graded until a separate, approved step adds that local tag; that is deliberately not done here.
Adding a task needs its record resolved and verified first (a `scripts/` resolver for that is not written yet; records so far were produced by hand from anonymous manifest requests).

## Seed archive limit (160 MiB) and the agent /testbed tmpfs (256 MiB); measured for the four E1 Stage 1 tasks
`task_setup.MAX_ARCHIVE_BYTES` is the raw-tar limit when a task's `/testbed` is extracted into a seed. History: 64 MiB (a placeholder sized for pytest-5227, 17.4 MiB), then 128 MiB (validated for `sympy__sympy-18621` only), now **160 MiB**. The agent's `/testbed` tmpfs
(`agent_runner.AgentRunSpec.testbed_size`) was 192 MiB and is now **256 MiB**. Both changes are code constants, not part of the hashed E1 definition, so its hash is unchanged; they apply identically to both arms and to every task.

**Measured** (one-off, read-only probes of each pulled image: a never-started hardened container, `/testbed` streamed through a byte counter, nothing saved; none of these tasks has been prepared):

| Task | Raw archive | Largest file (the `.git` pack) | Page-rounded `/testbed` footprint | Headroom in 256 MiB |
|---|---|---|---|---|
| `sympy__sympy-18621` | 99.45 MiB | 56.94 MiB | 102.42 MiB | 153.6 MiB |
| `sympy__sympy-22714` | 112.77 MiB | 64.79 MiB | 116.04 MiB | 139.96 MiB |
| `django__django-13933` | 133.78 MiB (140,280,320 B) | 75.07 MiB | 151.20 MiB | 104.80 MiB |
| `django__django-15851` | 142.56 MiB (149,485,568 B) | 80.25 MiB | 160.39 MiB | 95.61 MiB |

Both Django archives exceeded the old 128 MiB cap (13933 by 5.78 MiB, 15851 by 14.56 MiB). 160 MiB leaves 17.44 MiB (12%) over the largest measured archive. 256 MiB leaves the largest footprint 95.6 MiB for the agent's own writes, about what the calibrated SymPy task had at 192 MiB (89.6 MiB);
the footprint is the sum of every file rounded up to 4 KiB pages (the model reproduces the code's own 17.4 MiB figure for pytest-5227), which is 1.03 times the archive for SymPy and about 1.13 times for Django (many small files). Coupling rule: cap x 1.13 + 64 MiB working space <= tmpfs (160 x 1.13 + 64 = 245 <= 256).
**This is measured for these four tasks only**, not a universal SWE-bench limit: tasks that have not been probed (Stage 2, the registered replacements, matplotlib and scikit-learn repositories) may exceed it; a task over the cap fails its preparation gate (`seed_archive_over_limit`) and may be replaced under the pre-registered rule (next section).

**Aggregate tmpfs and memory.** `/testbed` 256 + `/opt/aiep` 416 + `/tmp` 128 + HOME 128 + `/dev/shm` 64 = **992 MiB**, 32 MiB below the unchanged 1 GiB aggregate ceiling (it was 928 MiB); `/testbed` could rise to at most 288 MiB without also changing that ceiling. With the unchanged 2 GiB container memory limit a completely full tmpfs
leaves 1056 MiB for processes (it was 1120 MiB). The expected fill is the Claude binary (338 MiB measured on macOS; the Linux build is unmeasured) plus the seed footprint, up to about 500 MiB.

**Host memory during extraction (measured, fake host).** The archive is buffered up to the cap (`seed_source.read_bounded` refuses, never truncates, and stops reading at cap + 1 block however long the stream is) and then parsed and rebuilt in memory; nothing is extracted on the host. For a realistic 157 MiB archive (9,816 entries, an 80 MiB pack) the peak Python allocation
during `extract_and_pin_seed` was 490 MiB (3.1x the archive; 4.8 s), so about 500 MiB at the 160 MiB cap, bounded by the cap and not by the stream. The files are extracted only inside the agent container's tmpfs by `tar -x -p --no-same-owner` as the non-root user, after the parser's path, symlink, hard-link and special-file checks.

**What is unchanged.** One seed file 96 MiB (`seed.MAX_SEED_FILE_BYTES`; the largest measured file is 80.25 MiB), total extracted content 256 MiB, 200,000 entries, the trusted-component ceilings (`seed.DEFAULT_COMPONENT_FILE_BYTES` 256 MiB and `agent_runner.MAX_COMPONENT_FILE_BYTES` 400 MiB), `/tmp` and HOME 128 MiB, `/dev/shm` 64 MiB, the 1 GiB aggregate ceiling, the 2 GiB memory
limit and every network and security limit. The individual seed-file limit was raised from 64 MiB to 96 MiB earlier (sympy-22714's 64.792 MiB pack). The calibration runs used the old 192 MiB tmpfs and remain excluded from scoring; cost and tokens do not depend on tmpfs capacity. `.git` is not excluded: the agent extracts its patch with `git diff`
against the seed HEAD and the handoff checks HEAD and its parent.

## Replacement of a task that fails a preparation gate (implemented; mocks only; NO task has been replaced)
The E1 definition already pre-registers the trigger ("a task is replaced ONLY if it fails its gold or no-op control, its image cannot be pinned or fails the preparation gate, or ... its recorded compatibility decision is
'incompatible', always BEFORE any agent run on it; never because of an agent outcome") and the order (the first unused entry of `preregistration.replacements` that keeps at most 2 tasks per repository and at least 4 repositories).
This section describes how the batch applies that rule to a preparation failure. It adds no new order or rule and edits no definition. Before this change a preparation error was not caught and ended every batch as a generic `orchestrator_error`.

**Which failures qualify (`src/aiep/preparation_gate.py`).** Only a typed, deterministic failure that is a property of the task itself, raised as a `PreparationGateFailure` carrying one of seven codes; the batch fails closed on anything else.
`seed_archive_over_limit` (the /testbed archive exceeds the archive cap, `task_setup.MAX_ARCHIVE_BYTES`, or the parser's raw bound), `seed_file_over_limit` (one file over `seed.MAX_SEED_FILE_BYTES`), `seed_content_over_limit`, `seed_entries_over_limit`,
`seed_unsafe_content` (path traversal, an escaping or malformed symlink, a hard link, a special file, a missing parent directory, a wrong root), `seed_lineage_invalid` (no resolvable HEAD, or HEAD/parent is not the registered dataset base commit) and
`identity_mismatch` (a COMPLETED probe reports another account). **Never replaced, always a halt for review:** a Docker daemon failure, free disk below the gate, a network or registry error, an unavailable registry, a pull that was not approved, a local
image that differs from its pinned digest, an interrupted or timed-out identity probe, an unreadable or truncated archive, a lineage check that could not run, and any other error (the batch records `preparation_error_not_replaceable` and halts as before).

**How a replacement is chosen and what it keeps.** `experiment_def.apply_replacement` (unchanged order and rules) picks the first registry entry, in registry order, that is not already used, is not a selected task and keeps the per-repository and repository-minimum rules;
the record lists every earlier entry that was passed over and why (`already_used`, `already_a_selected_task`, `max_per_repo`, `min_repos`). The replacement takes over the failed task's block, so its slots, **stage, repetition,
arm order and order indexes** are exactly the registered ones; `pair_id` stays the REGISTERED pair id (it names the original pair; it used to be rewritten to embed the replacement) while `task_id` and `slot_id` name the task that actually runs. If the replacement itself fails
a gate, the batch continues down the SAME registry (the pre-registered fallback); when no entry is allowed it halts with the pre-registered `control_replacement_exhausted`. A block is never replaced after any of its slots has started (`replacement_after_run`).

**Stage minimum (human decision required).** Each stage of an exploratory definition registers its own repository minimum (`design.stages[i].min_repos`; E1: 2 for each stage). Stage 1 can never violate it (four tasks, at most two per repository, always span two repositories), but Stage 2's two tasks can end up in one repository while every global rule still holds; exhaustive simulation over E1's real tasks and registry found 784 of 11,538 reachable states in that shape, the shortest after four failures (three consecutive Stage 1 gate failures that consume the registry's django, pylint and pytest entries, then `psf__requests-863` replaced by `sphinx-doc__sphinx-7686` next to `sphinx-doc__sphinx-10325`). So after the registered order has selected a candidate, it is checked against ITS OWN stage's minimum (`apply_replacement(..., stage_tasks, stage_min_repos)`; the batch passes the stage's current tasks). A violating candidate raises `StageMinimumViolation`: **no later entry is tried** (the registered order is never altered or skipped), nothing is recorded, the batch records a `replacement_blocked_by_stage_minimum` event and halts **`replacement_violates_stage_minimum`**, which is NOT acknowledgeable (acknowledging would only retry the same candidate): continuing needs a human decision, in practice a new, explicitly reasoned experiment id. Accepted replacements record a `stage_check` (stage, minimum, repositories after). Feasibility pilots have no stages and are unchanged.

**Verified before it is recorded.** The replacement's identity comes from the pinned local dataset snapshot (instance id, registered repository, a 40-character base commit; `RealSteps.verify_task_identity` returns only those three fields, never hidden task data) and its
registered image must be the published image of that instance. An unverifiable candidate halts (`replacement_identity_invalid`, or `replacement_identity_unverifiable` for steps that cannot verify); the batch never skips to the next candidate silently.

**The record (batch state `replacements`, `replacement_schema` 2).** Per replacement: sequence, block, stage, `scheduled_pair_id`, first arm and arm order, repetition, replaced and replacement ids, repository, the structured failure (`trigger`, `code`, exception type, detail),
registry index and skipped entries, the verified identity and base commit, the failed task's pin state at that moment, `uses_ab_outcomes: false`, and a SHA-256 chained to the previous record (no timestamps, so the same inputs give the same record). Every start re-verifies the chain, the
registry, the block assignments and that no replacement is a selected task or used twice; any edit, removal, reordering or inconsistency refuses to start with `replacement_history_invalid`, which cannot be acknowledged.

**Partial preparation.** Nothing of a failed task is deleted, overwritten, unpinned or re-used: a pulled image, an evaluator-image pin or a left-over seed directory stay exactly as they are and the record says so. The failed task cannot become runnable by the replacement
(its block now runs the replacement; its own pins are still incomplete, and `task_prepared` also requires the exact pinned image). An image pulled by a failed preparation is never removed by the batch.

**Scope and limits.** The replacement is recorded by the batch, in its state, never by editing the definition. `--prepare-only` only reports a failure; it records no replacement, so a task that failed there is replaced when the batch next prepares it (and fails the same way), under the
batch's pull and control approvals. The control-failure and compatibility triggers use the same code path and are unchanged (they are labelled `control` and `compatibility` in the record), and Stage 2 still starts only by explicit activation. No real replacement has occurred.

## Evaluator image policy (fail closed)
The upstream harness falls back to `client.images.pull(test_spec.image)` when its image is missing locally. `aiep.stock_eval.main` now installs `install_image_guard()` BEFORE importing or running the
harness: `ImageCollection.pull/build/load` and the low-level `pull`, `build`, `load_image`, `import_image*` raise `ImageAcquisitionBlocked`, a `BaseException` (the harness wraps container creation in
`except Exception`, which would otherwise turn the refusal into an ordinary evaluation error). The guard writes `IMAGE_POLICY_VIOLATION.txt`, `main` exits 97, and `aiep.grading.grade` raises
`ImagePolicyViolation` (`scripts/m3_grade.py` exits 3); lookups and container creation are untouched, and the patches are removed when `main` returns. `grading.grade` (hence `m3_grade.py` and the batch) and
`scripts/m1_eval.sh` all run `python -m aiep.stock_eval`, so all are guarded; the `grade()` image-ID check remains as a second, earlier layer. Remaining limits: (1) running
`swebench.harness.run_evaluation` directly is not a supported path and is not guarded; (2) the gated hardened `aiep.evaluator` (attestation required, blocked today, never run) has its OWN refusals of
`ImageCollection.pull/build/load/push` only: it does not cover the low-level API and refuses with an ordinary exception that the harness's `except Exception` would convert, so it should adopt this
guard before it is ever used; (3) `images.tag`, `remove_image` and `commit` are not blocked (the harness does not use them).

## Calibration run `m3-calibration-1` (prepared; NOT yet executed)
Purpose: measure real cost, duration, request count and token usage (input, output, cache read, cache write) for one agent run, to set the pilot's `budget_usd_per_run` and `max_total_computed_usd`. It is NOT an A/B
observation. Planned configuration: task `sympy__sympy-18621`, Config A, gateway budget $2.00 (Claude Code's `--max-budget-usd` becomes the client estimate $2.2), concurrency 1, wall clock 1,200 s,
`CLAUDE_CODE_MAX_OUTPUT_TOKENS` 64,000 (the pilot's own setting, so the measurement matches the pilot).

**The limits are not hard spending limits.** The gateway budget is SOFT: usage is known only after a response, so a run can end above the budget by the cost of the request in flight (output at most $0.64 at
the 64,000 cap, plus input, which the gateway does not bound), and an interrupted stream is charged at an upper bound. Claude Code's `--max-budget-usd` is a client-side ESTIMATE. Only a spend limit on the dedicated
API key itself is a hard cap, and it is set outside this repository. Gateway measured cost, the client estimate, exact versus upper-bound usage, unrecorded requests (a `forwarding` ledger line with no finishing
line) and interrupted streams stay separate in `run.json` and the ledger; nothing here changed how they are computed.

Code: `scripts/run_m2_agent.py --max-concurrent N` (1 to 4, DEFAULT 1, validated before any Docker, key or API activity, always passed explicitly to the gateway; `m2_run.prepare`'s own default of 2 is unchanged for other callers)
and `--calibration m3-calibration-N`. A calibration run records `calibration: {id, purpose, scored: false, not_an_ab_observation: true}` in `run.json`, has NO experiment identity (it cannot be a slot of any definition),
is written under `runs/calibration/`, and is refused by `results.build_row`, `results.validate_row` / `ResultsWriter.append`, `analysis.analyze`, and (as a rejection) by the handoff when a schedule is supplied; plain handoff
validation only flags it.

Bug found and fixed while doing this: `m2_run.prepare` reused the name `experiment` for the model config it loads, so `run.json["experiment"]` held the model config instead of the scheduled slot's identity (and for ordinary
runs, a non-null dict). It is now the identity or `null`; a regression test covers the round trip.

## Two cost measurements per result row
Every scored row records BOTH costs as separate fields that never overwrite or substitute for each other: `cost_usd_computed` (the gateway's measured token usage priced from the ledger; source in
`gateway_cost_source`) and `claude_reported_cost_usd` (Claude Code's own `total_cost_usd` from `run.json["usage_cli_estimate"]`; a client-side ESTIMATE, source in `claude_reported_cost_source`). A missing, null
or invalid Claude estimate is stored as null, never as the gateway cost or zero; an unreadable gateway cost keeps its existing value `"0"` and is not filled from the Claude figure. The new fields are optional in
`validate_row`, so rows written before them stay valid (results schema version unchanged). `aiep.analysis` reports them in separate fields (per-arm `claude_reported_cost_usd_total/mean/runs/missing_runs`, the paired
Claude-reported difference over pairs where both runs have one, and a descriptive `gateway_vs_claude_reported` agreement check); the gateway figures, the cost ratio and the uncached-equivalent sensitivity are unchanged.

## Transcript metrics version 2 (after calibration-2)
Calibration-2 showed two parser defects: a here-document hid the commands after its delimiter (a `python -m pytest` after a Python heredoc edit was not seen), and an edit made by a Python script writing a file
through Bash was not recognised. `aiep.transcript` (parser_version 2) now parses the whole shell command with here-document bodies set aside, classifies every test ATTEMPT from the tool output (`not_started` for a missing
runner such as `No module named pytest`, `completed_failed`, `completed_passed`, `started_incomplete`, `unknown`; a non-zero exit alone proves nothing), and keeps CONFIRMED edit-tool calls apart from HEURISTIC Bash
edit-like commands (Python file writes, `sed -i`, `perl -pi`, `cat >`, `tee`, redirects to source files, `git apply`/`patch`; recorded with their classification in `analysis_details`). A test-fix cycle needs a runner that started
and completed with failures, then an edit, then a later runner that started; sequences that do not qualify cleanly are listed in `ambiguous_sequences` and never counted. Existing fields keep their names (`test_runs` still counts
attempts; `test_runs_failed` now counts only completed failing runs; `test_fix_cycles` is the stricter count; `compliance` now needs a runner that actually started). New fields: `test_runs_started/completed/not_started/started_incomplete`,
`test_usage_started`, `test_fix_cycles_confirmed_edit/bash_edit_only`, `unfinished_cycles`, `edit_tool_calls_errored`, `ambiguous_sequences`, `analysis_details`, `parser_version`; result rows carry six optional extras. The registered
`test_command_spec` (hashed into the v2 definition) is unchanged. The m3-pilot-v2 definition's metric TEXT predates these refinements; a later definition (E1) should state them.

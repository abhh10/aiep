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

## Seed archive limit (128 MiB, validated for SymPy only)
`task_setup.MAX_ARCHIVE_BYTES` (the raw tar limit when a task's `/testbed` is extracted into a seed) was 64 MiB, a placeholder sized for pytest-5227 (17.4 MiB). The first real preparation
(`sympy__sympy-18621`) failed closed against it: a bounded, read-only probe (a disposable never-started container, `docker cp ... -` into a byte-counting tar reader) measured the SymPy
`/testbed` at 104,282,112 B (99.45 MiB): 1,981 files, 358 directories, `.git` 70.6 MB (a 59.7 MB pack) and `sympy/` 28.3 MB. The limit is now 128 MiB. This is MEASURED FOR SYMPY ONLY, not a
universal SWE-bench limit: other tasks are unmeasured (matplotlib and scikit-learn repositories are likely larger) and each needs the same probe once its image is local. The parser's own limits
(256 MiB total content, 200,000 entries) and the agent `/testbed` tmpfs (192 MiB; SymPy content is 97.8 MiB) are unchanged; the INDIVIDUAL seed-file limit (`seed.MAX_SEED_FILE_BYTES`) was 64 MiB and is now **96 MiB**.
Reason: measuring `sympy__sympy-22714` (image pulled, never prepared; no seed exists) found a 64.792 MiB Git pack, 811 KiB over the old per-file limit (its whole archive is 112.77 MiB, under the 128 MiB cap), and `django__django-13933` a 75.07 MiB pack
(archive 133.78 MiB, over the archive cap, so the per-file change alone does not make it preparable). The four limits are distinct: the archive-wide raw tar cap (128 MiB, `task_setup.MAX_ARCHIVE_BYTES`), the individual seed-file limit (96 MiB), the trusted-component
ceilings (`seed.DEFAULT_COMPONENT_FILE_BYTES`, 256 MiB for gateway/proxy code and probes, and `agent_runner.MAX_COMPONENT_FILE_BYTES`, 400 MiB for the Claude binary; both unchanged and independent of the seed-file limit), and the agent tmpfs capacity (`/testbed` 192 MiB, aggregate 1 GiB; unchanged). `.git` is not excluded: the agent extracts its patch with `git diff`
against the seed HEAD and the handoff checks HEAD and its parent. Extraction still buffers the whole archive (estimated 300-400 MiB peak host memory for SymPy; not measured).

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

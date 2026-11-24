# E1 Stage 1: Exploratory Paired Evaluation of a Prescribed Test-and-Fix Workflow

**Experiment:** `e1-exploratory-1` (locked, kind `exploratory_ab`) · **Stage reported:** Stage 1 only · **Executed:** 2026-11-23, 17:13:46 to 18:09:36 UTC
**Definition SHA-256 (self-hash):** `9601b345da705b7e8bbdba251b902f4c7637482fd2e87e732dcba0dc36875eff`
**Harness commit at execution:** `28e758e5f3ccf4598fe843fe4a3abbed3628eb83` (clean tree, from each `run.json`)
**Status of this document:** descriptive report of an exploratory feasibility study. It makes no statistical claim and no causal claim.

**Evidence labels used throughout.** **[Observed]** means read directly in a transcript, patch, ledger, log or state file. **[Parser]** means derived by `src/aiep/transcript.py` (parser version 2) or other project code. **[Interpretation]** means the author's reading, which may be wrong. Costs are **gateway-computed** (measured token counts multiplied by `config/model_pricing.json`). They are not Anthropic Console billing and were not independently verified against it.

---

## Abstract

This report documents Stage 1 of E1, a budget-constrained exploratory study. It asks whether prescribing an iterative test-and-fix workflow (Config B) changes Claude Code's issue resolution on SWE-bench Lite, and at what cost, compared with a basic investigate-fix-stop workflow (Config A). Four tasks (two SymPy, two Django) were each run once per arm with the same model (`claude-sonnet-5-5`), Claude Code binary (2.1.328), container image, limits and evaluator. The two arms differ only in one sentence of the prompt. Runs were executed in an isolated container behind a metering gateway and a restricted egress proxy, then graded independently by the stock SWE-bench harness.

All eight scored runs (four tasks, each run once per arm) were valid, were graded without harness errors, and resolved their task. All eight gold and no-op evaluator controls passed. Gateway-computed spend was $0.2337872 over 34 API requests, with exact and complete accounting. Config B started a test runner in 4 of 4 runs and Config A in 0 of 4, so the behavioural manipulation worked. A manual transcript audit of all eight runs found **no confirmed fix-revision cycle**: in no run did a test failure lead to a revision of the implementation. The parser reports one test-fix cycle (django-15851, B), but the edit in that cycle changed a test expectation, not the fix. Because every task resolved in both arms and two of four task pairs produced identical implementation changes, Stage 1 provides no evidence about whether iteration improves resolution. It does show that Config B used more requests (20 against 14). Its cost was also higher in all four pairs on two descriptive bases that reduce prompt-cache effects without fully removing them: the uncached-equivalent repricing and a first-request-excluded sensitivity analysis. Neither is a billing difference or a causal estimate. Stage 1 demonstrates end-to-end harness feasibility and a successful behavioural manipulation. It does not establish whether iteration improves resolution.

---

## 1. Introduction

Coding agents are increasingly given explicit workflow instructions, such as "run the tests, analyse failures, fix, and repeat". The intuition is that feedback from an executable oracle should let an agent correct early mistakes. That intuition has costs: extra tool calls, extra requests, and extra tokens. Whether the instruction improves outcomes for a strong model that already tends to produce a correct fix in one pass is an empirical question.

Three things must be kept apart when evaluating such a workflow:

1. **Test invocation.** The agent runs a test command. A command may fail to start (for example a missing runner), start and fail, or start and pass.
2. **A test failure that matters.** A runner starts, completes, and reports failures. Some failures indicate a defect in the fix. Others indicate stale tests that assert behaviour the fix intentionally changes.
3. **Iteration.** A failure caused by the fix is analysed, the implementation is revised, and the tests are run again. Only this third behaviour is "iterative test-and-fix". Running tests once after an edit is confirmation, not iteration. Editing a test expectation and rerunning is not a revision of the fix.

This report keeps those distinctions explicit because a prompt that asks for iteration can raise test invocation without producing any iteration.

---

## 2. Research Question and Hypotheses

**Research question** (verbatim from `config/experiments/e1-exploratory-1.json`): *Does prescribing an iterative test-and-fix workflow improve Claude Code's software issue-resolution performance, and at what additional cost?*

**Expected mechanism (a hypothesis, not a result).** If a first implementation is sometimes wrong, tests might expose it, and a prompt that prescribes running tests, analysing failures and revising might raise the share of issues resolved. The prescribed loop would also add requests and tokens, so cost would rise. The magnitude of any benefit would depend on how often the first attempt is wrong and on how informative the available tests are.

**What E1 can address.** The locked definition (`claim_scope`) states that E1 is an exploratory feasibility study with one run per arm per task. It is not a statistically powered effectiveness test, supports no claim that one workflow is better than the other, and reports no significance tests. Its analysis is descriptive only. No pre-specified directional hypothesis about effect size was registered.

---

## 3. Experimental Design

### 3.1 Configurations and prompts

The two arms use one prompt template (`config/workflow_prompts_v2.json`, SHA-256 `a1a59d3a…` for the file; template `86571bc7…`). They differ only in the Workflow sentence. Across all four tasks a line-level diff of A and B prompts shows exactly one differing line [Observed, `runs/m2/*/prompt.txt`].

- **Config A** (prompt hash `5e44cde5…`): *"Workflow: investigate the code to understand the issue, implement a fix, and then stop."*
- **Config B** (prompt hash `cd6fbb95…`): *"Workflow: investigate the code to understand the issue and implement a fix. Then run the repository's existing tests that are relevant to your change, analyse any failures, revise the fix, and repeat. Do this for at most 5 test-fix cycles, where one cycle is one test run followed by a revision of your fix in response to a failure. Stop earlier if the relevant tests pass or you can make no further progress. Then stop."*

Both prompts end with the same constraints: work only inside `/testbed`, do not commit or branch, and reply with a one-line summary. Config A does not forbid running tests. Test use in A is spontaneous and is measured.

### 3.2 Model, client and dataset

| Item | Value | Source |
|---|---|---|
| Model | `claude-sonnet-5-5` | definition, every `run.json` |
| Claude Code | 2.1.328, linux-x64, SHA-256 `a982d380…5865e8` | definition, `run.json` |
| Claude flags | `-p --output-format stream-json --verbose --safe-mode --permission-mode bypassPermissions --tools Bash,Read,Edit,Write,Glob,Grep --no-session-persistence --max-budget-usd 0.198` | `run.json` `claude_args` |
| Pinned env | `CLAUDE_CODE_MAX_OUTPUT_TOKENS=64000`, `BASH_DEFAULT_TIMEOUT_MS=120000`, `API_TIMEOUT_MS=600000` | `run.json` `claude_runtime` |
| Uncontrolled | thinking budget, stream idle timeouts, CLI retry back-off | `run.json` `claude_runtime.uncontrolled` |
| Dataset | `SWE-bench/SWE-bench_Lite`, split `test`, revision `b0dde1093fe417d83b7184254edf8199c1f0dff5` | definition |
| Pricing | `config/model_pricing.json` ($2 / $10 per MTok input / output; $0.10 cache read; $2.50 5-minute cache write), retrieved 2026-11-19 | pricing file |

### 3.3 Tasks

Stage 1 is the study. Stage 2 is conditional and was never activated.

| Stage | Task | Repository | Base commit | F2P / P2P | Status |
|---|---|---|---|---|---|
| 1 | `sympy__sympy-18621` | sympy | `b17ef6ef…` | 1 / 15 | executed |
| 1 | `django__django-13933` | django | `42e8cf47…` | 1 / 19 | executed |
| 1 | `django__django-15851` | django | `b4817d20…` | 1 / 8 | executed |
| 1 | `sympy__sympy-22714` | sympy | `3ff4717b…` | 1 / 11 | executed |
| 2 (conditional) | `sphinx-doc__sphinx-10325` | sphinx | `7bdc11e8…` | 1 / 5 | **not run, inactive** |
| 2 (conditional) | `psf__requests-863` | requests | `a0df2cbb…` | 4 / 60 | **not run, inactive**; compatibility decision still pending |

Tasks are a convenience subset of an earlier pre-registered pool (`m3-pilot-v2`) filtered by eligibility rules (matplotlib and scikit-learn excluded, at most 2 tasks per repository, `pytest-dev__pytest-5227` excluded) and chosen to fit a $5 prepaid budget. The definition records that they "may be easier or harder than typical SWE-bench tasks". `state.json` records Stage 2 as `stage_not_activated` for both tasks [Observed]. The definition's schedule counts (6 blocks, 12 runs) include the inactive Stage 2. This report concerns only the four active Stage 1 pairs and eight scored runs.

### 3.4 Pair order and spacing

Both arms of a task run back to back inside one block. Blocks run in a seeded order, and the first arm is balanced across blocks. The 600-second gap is stated in the definition as longer than the measured 5-minute prompt-cache lifetime, so the second arm of a pair does not start inside the first arm's cache window.

| Order | Pair | First arm | Second arm | Measured gap [Observed, `state.json` events] |
|---|---|---|---|---|
| 1, 2 | sympy-18621 | A | B | run_done 17:16:22 → run_start 17:26:22 = 600 s |
| 3, 4 | django-13933 | B | A | 17:30:36 → 17:40:36 = 600 s |
| 5, 6 | django-15851 | B | A | 17:44:32 → 17:54:32 = 600 s |
| 7, 8 | sympy-22714 | A | B | 17:58:00 → 18:08:00 = 600 s |

No gap was imposed between pairs (see Section 7.4). Evaluator controls for the next task and its grading ran in between.

### 3.5 Execution environment and isolation

For each run, an orchestrator started three containers: a **restricted CONNECT proxy**, a **metering gateway**, and the **agent**. Design details are in `docs/M2_EGRESS_ARCHITECTURE.md`. The statements below that cite `run.json` are [Observed].

- **Agent container.** Non-root (uid:gid 1000:1000), same SWE-bench per-instance image as the evaluator (image ID checked equal by the handoff validator), memory 2 GiB, 2 CPUs, 512 pids, `/testbed` on a 256 MiB tmpfs seeded from a SHA-256-verified archive, wall-clock limit 1800 s. The agent is attached only to `aiep-agent-net`, an internal network with no route out. It holds no API key.
- **Gateway.** The only holder of the API key. It reaches `api.anthropic.com:443` only through the proxy, serves one request at a time (`gateway_max_concurrent=1`), enforces the model, and writes a per-request ledger.
- **Egress proxy.** The only container on the egress network. It holds no secret, allows one destination and validates resolved addresses. The architecture document records a **residual risk**: the proxy container can reach Docker Desktop host services at the network level. It is not network-isolated from them, and its safety rests on its code and on holding no secret.
- **Hidden data.** The agent never receives the hidden test patch, `FAIL_TO_PASS` / `PASS_TO_PASS`, or the gold patch. Only the issue text crosses in and only a text patch crosses out. The handoff validator checks the prompt and output for hidden lines (Section 4.8).
- **Host.** Apple Silicon, 8 GB RAM, Docker Desktop VM 4 GiB, amd64 images under emulation, runs strictly sequential.

### 3.6 Budgets and accounting

- Per-run gateway budget `$0.18` (soft); Claude client budget `$0.198`; per-run overshoot allowance `$0.30` (provisional); Stage 1 cap `$3.84` = 4 × 2 × ($0.18 + $0.30); protected reserve `$0.50`.
- The budget is a **soft limit**. Usage is known only after a response is metered, and a request cut off mid-stream is charged at an upper bound. No Stage 1 run reached its budget: `overshoot_usd` is `0` for all eight slots, and `upper_bound_entries` is 0 in every results row [Observed].
- Spend and pair reservations are tracked in `state.json` (`cumulative_cost_usd`, `reservations`).
- Two earlier real-API **calibration runs** (about $0.0906 by ledger, `limits.calibration_spend_observed_usd`) were used to set budgets and are **excluded** from all results here.

### 3.7 Patch extraction, grading and controls

- After the agent exits, the patch is extracted as a text diff and written to `predictions.jsonl` (`instance_id`, `model_name_or_path`, `model_patch`). `scripts/validate_handoff.py` logic (`src/aiep/handoff.py`) must return `ready` or `ready_flagged` before grading.
- Grading uses the **stock SWE-bench harness** (`swebench==5.0.2`) via `src/aiep/stock_eval.py`. A fresh container is created from the official image, the model patch is applied, the hidden test patch is applied, and the specified tests run. The wrapper drops all capabilities, sets `no-new-privileges`, 2 GiB memory, 1024 pids and 2 CPUs (verified in `create_kwargs.jsonl` for a sample of runs). The evaluator needs network access for pip build isolation, so it is not fully offline.
- **Controls.** Before each task's agent runs, the batch re-ran a **gold** control (must resolve) and a **no-op** control (adds one new text file; must not resolve, with 0 of the F2P tests passing). Controls are never result rows. All eight passed (Section 5.5).

---

## 4. Methodology and Metrics

### 4.1 Resolution, FAIL_TO_PASS and PASS_TO_PASS

The primary outcome is the harness verdict **resolved**: every `FAIL_TO_PASS` (F2P) test passes and every `PASS_TO_PASS` (P2P) test is maintained. F2P tests fail before the reference fix and pass after it. P2P tests pass both before and after. Each task here has exactly one F2P test. The P2P tests come from the test files the hidden test patch touches (Section 7.7). Test identifiers are intentionally not reproduced in this report.

### 4.2 Test invocation and completion (parser v2)

A **test attempt** is a Bash call whose command runs a test runner according to `test_command_spec` (pytest, `runtests.py`, `sympy.test(...)` via `python -c`, and others). Each attempt has a state determined from the tool output: `not_started` (the runner never started, for example "No module named pytest"), `completed_failed`, `completed_passed`, `started_incomplete`, or `unknown`. A missing runner is never counted as a failed suite. `test_runs_started` counts attempts where a runner actually started. Running a repro with `python -c` that does not invoke a runner is not a test attempt.

### 4.3 Edits and fix revisions

- The parser counts **confirmed edits** (`Edit` / `Write` tool calls) in `edits_total` and **heuristic Bash edit-like commands** (python heredoc writes, `sed -i`, and similar) in `bash_edit_like_calls`. It does not show that a file changed. For that reason `edits_total` is not comparable across arms (Section 7.8).
- **Implementation edit** means a change to repository source. **Test-file edit** means a change under a test directory. **A test-expectation change is not a revision of the implementation fix.** This distinction is made manually in this report. The parser does not make it.

### 4.4 Test-fix cycle and confirmed fix-revision cycle

- **Parser cycle** (`test_fix_cycles`): a runner started and completed with failures, then an edit of any kind, then a later runner started. `test_fix_cycles_confirmed_edit` counts cycles whose edit was an `Edit`/`Write` tool call.
- **Confirmed fix-revision cycle** (this report's stricter definition): a test failure caused by the fix, followed by a revision of implementation code in response, followed by a repeated test run. It is established by reading the transcript. Zero such cycles occurred (Section 5.4).
- A passing run after the first implementation edit is **confirmation**, not iteration.

### 4.5 Requests, turns, cost and tokens

- **Requests** and **tokens** come from the gateway ledger (`gateway_ledger.jsonl`), which records every forwarded request and a finishing line with measured usage. **Turns** are the transcript's reported turn count.
- **Computed cost** = measured tokens × `config/model_pricing.json`. **Uncached-equivalent cost** (in `results.jsonl`) prices all input, cache-read and cache-write tokens at the base input rate, which is a cache-neutral repricing of the total context processed. Both are computed, not billed. A third quantity, used only in Section 5.3 and Section 7.4, is a **first-request-excluded sensitivity analysis**: each run's computed cost minus the computed cost of its first request. It is this report's own analysis, not a project metric, and it reduces but does not remove cache effects.
- **Claude-reported cost** (`total_cost_usd`) is a client-side estimate and is reported separately. It equals the computed cost in every run.

### 4.6 Runtime

`wall_seconds` is the orchestrator's wall time for the run (container start to extraction). `claude_duration_ms` is the Claude CLI's own reported duration (client-side). Neither includes grading.

### 4.7 Handoff status and integrity checks

`src/aiep/handoff.py` runs checks before grading: artifact manifest hashes, run identity, pinned model, same-image check, one-line predictions file, accounting completeness and exactness, containers removed, no gateway violation, and a hidden-data scan. A check with severity `reject` blocks grading. A check with severity `flag` lets the run through but travels with it. Status is `ready` (no flags), `ready_flagged`, or `rejected`. Handoff `ready` does not state that the task is solved.

### 4.8 Hidden-data scan and its blind spot

The scan compares the prompt, transcript and patch with hidden-patch lines, using only added lines of at least 40 characters (`MIN_LEAK_LINE`). Shorter lines are invisible to it.

---

## 5. Results

### 5.1 Execution summary

- **Batch status:** `complete_with_unrun_stages`. `halt` is `null`. There were no replacements, interruptions or invalid runs. `consecutive_infra_invalid` is 0. Stage 2 was not activated [Observed, `state.json`].
- **Scored runs:** 8 of 8 completed, valid (`valid_measurement` true in every row), handed off (`ready` ×4, `ready_flagged` ×4), graded without harness errors, with complete transcripts (8 of 8, 0 ambiguous sequences) [Parser].
- **Accounting:** `accounting_complete` and `cost_exact` true for all eight. Every request returned HTTP 200 (34 of 34). No rate-limited, overloaded, refused or other non-success request categories.
- **Spend:** gateway-computed **$0.2337872** (= `cumulative_cost_usd`). The sum of the eight per-run costs (Decimal, no rounding) is $0.2337872. A recomputation from the raw ledgers with `config/model_pricing.json` reproduces every per-run cost and the total [independently recomputed for this report].

### 5.2 Per-run results

Pair order is the execution order. "Tests started" counts runners that actually started.

| # | Task | Arm | Req | Turns | Tools | Computed cost (USD) | Wall (s) | Tests started / attempted | Resolved | F2P | P2P | Handoff |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | sympy-18621 | A | 4 | 4 | 3 | 0.0397001 | 44.819 | 0 / 0 | yes | 1/1 | 15/15 | ready |
| 2 | sympy-18621 | B | 7 | 8 | 7 | 0.0465250 | 53.772 | 1 / 2 | yes | 1/1 | 15/15 | ready_flagged |
| 3 | django-13933 | B | 5 | 5 | 4 | 0.0262755 | 49.673 | 1 / 1 | yes | 1/1 | 19/19 | ready |
| 4 | django-13933 | A | 4 | 5 | 4 | 0.0332338 | 36.954 | 0 / 0 | yes | 1/1 | 19/19 | ready |
| 5 | django-15851 | B | 4 | 5 | 4 | 0.0157452 | 42.620 | 2 / 2 | yes | 1/1 | 8/8 | ready_flagged |
| 6 | django-15851 | A | 3 | 3 | 2 | 0.0256974 | 28.414 | 0 / 0 | yes | 1/1 | 8/8 | ready |
| 7 | sympy-22714 | A | 3 | 3 | 2 | 0.0132116 | 25.652 | 0 / 0 | yes | 1/1 | 11/11 | ready_flagged |
| 8 | sympy-22714 | B | 4 | 4 | 3 | 0.0333986 | 51.550 | 1 / 2 | yes | 1/1 | 11/11 | ready_flagged |
| | **Total** | | **34** | **37** | **29** | **0.2337872** | **333.454** | | **8/8** | | | |

Sources: `runs/m3/e1-exploratory-1/results.jsonl` (rows in order), cross-checked against each run's `run.json` and `gateway_ledger.jsonl`.

Patch sizes (bytes) and hashes: 18621-A 717 `1fece3fa…`; 18621-B 594 `74fe2284…`; 13933-B 1271 `02f21985…`; 13933-A 1212 `fbf830d7…`; 15851-B 1139 `d0dbfb73…`; 15851-A 530 `15989da8…`; 22714-A and 22714-B both 705 `c10b66d0…`. In all eight runs the evaluator's logged patch has the same SHA-256 as the agent's patch [Observed for pairs 1 to 4 individually during the audit].

### 5.3 A-versus-B comparison

| Measure | Config A (4 runs) | Config B (4 runs) |
|---|---|---|
| Resolved | 4 / 4 | 4 / 4 |
| Runs that started a test runner | **0 / 4** | **4 / 4** |
| Test attempts (started / not started) | 0 (0 / 0) | 7 (5 / 2) |
| Test runs that failed | 0 | 2 (13933-B, 15851-B) |
| Parser test-fix cycles | 0 | 1 (15851-B, test-expectation edit) |
| Confirmed fix-revision cycles (manual) | 0 | **0** |
| API requests (total / mean) | 14 / 3.5 | 20 / 5.0 |
| Turns (total) | 15 | 22 |
| Tool calls (total) | 11 | 18 |
| Computed cost (total / mean) | $0.1118429 / $0.02796 | $0.1219443 / $0.03049 |
| Uncached-equivalent cost (total) | $0.268796 | $0.403688 |
| Output tokens | 2,637 | 3,785 |
| Cache-read tokens | 90,644 | 155,493 |
| Cache-write (5 m) tokens | 30,541 | 27,386 |
| Input tokens | 28 | 40 |
| Wall time (orchestrator, total) | 135.839 s | 197.615 s |
| Claude-reported duration (total, client-side) | 44.326 s | 83.305 s |

Paired differences (B minus A), exact Decimal arithmetic from the results rows:

| Pair | Order | Requests | Computed cost | Uncached-equivalent (repricing) | Computed cost, first request excluded (sensitivity) |
|---|---|---|---|---|---|
| sympy-18621 | A, B | +3 | +$0.0068249 | +$0.061316 | +$0.0063324 |
| django-13933 | B, A | +1 | −$0.0069583 | +$0.027824 | +$0.0078412 |
| django-15851 | B, A | +1 | −$0.0099522 | +$0.021232 | +$0.0046973 |
| sympy-22714 | A, B | +1 | +$0.0201870 | +$0.024520 | +$0.0050425 |
| **Sum** | | **+6** | **+$0.0101014** | **+$0.134892** | **+$0.0239134** |

Totals check: 0.1219443 − 0.1118429 = 0.0101014. 0.403688 − 0.268796 = 0.134892. The last column is a **first-request-excluded sensitivity analysis**, this report's own calculation and not a project metric. It drops each run's first request, whose cost depends strongly on whether the prompt cache was warm (Section 7.4). It does **not** remove all cache effects: later requests in a run still depend on what earlier requests wrote to the cache, and the number and size of requests differ by arm. The unadjusted cost differences change sign across pairs largely for the first-request reason. The uncached-equivalent repricing and the first-request-excluded sensitivity analysis are both positive in all four pairs.

**How to read these differences.** All four pairwise comparisons are **descriptive, adjusted comparisons** of gateway-computed figures. The adjusted columns are analytic recalculations, not amounts billed, saved or charged. They are not Console billing differences. They are not causal estimates of the effect of the workflow: tasks differ, n=4, one run per cell, and arm is confounded with run position and cache state.

### 5.4 Test behaviour and fix-revision cycles

All eight transcripts were read in full (Section 6). Sequences:

| Run | Test attempts and outcomes | Edit relative to test | Fix-revision cycle |
|---|---|---|---|
| 18621-A | none | n/a | none |
| 18621-B | (1) pytest: not started, no module; (2) `sympy.test`: passed (16 tests) | both attempts after the only edit | none |
| 13933-B | 1: started, 4 failures | edits before the test; none after | none (failure analysed, not acted on) |
| 13933-A | none | n/a | none |
| 15851-B | (1) failed (1 test); (2) passed | implementation edit before test 1; after failure only a **test-file** edit | none: test-expectation update |
| 15851-A | none | n/a | none |
| 22714-A | none | n/a | none |
| 22714-B | (1) pytest: not started; (2) `sympy.test`: passed (12 tests) | both after the only edit | none |

- **Tests invoked before the first implementation edit:** 0 of 4 B runs. In every B run the first runner started after the first implementation edit.
- **Passing run after the first edit (confirmation, not iteration):** 18621-B and 22714-B.
- **Failures:** two B runs saw a failure (13933-B, 15851-B). In both, the cause was an existing test asserting the old behaviour that the fix intentionally changed. Neither reflected a defect in the fix.
- **Implementation revised after a failure:** 0 of 8.
- **Parser cycle count:** 1 (15851-B, `test_fix_cycles=1`, `test_fix_cycles_confirmed_edit=0`, bash-edit-only). Its edit target, recorded in the parser's own detail, is the test file `tests/dbshell/test_postgresql.py`. This is a **test-update cycle**, not a fix-revision cycle. The parser's `compliance=true` for 15851-B, 18621-B and 22714-B means only "a runner started and nothing remained unaddressed under the parser's rule", and not that iteration occurred (Section 7.8).

### 5.5 Controls

All eight controls were re-run by the batch immediately before each task's agent runs, and all passed the pre-registered requirements (`state.json` blocks, `problems: []`; `runs/m1/*/meta.json`).

| Task | Gold control (run id) | Gold result | No-op control (run id) | No-op result |
|---|---|---|---|---|
| sympy-18621 | `m3ctl-gold-sympy-18621-20261123T171346Z` | resolved, F2P 1/1, P2P 15/15 | `m3ctl-noop-sympy-18621-20261123T171425Z` | unresolved, F2P 0/1, P2P 15/15 |
| django-13933 | `m3ctl-gold-django-13933-20261123T172750Z` | resolved, 1/1, 19/19 | `m3ctl-noop-django-13933-20261123T172823Z` | unresolved, 0/1, 19/19 |
| django-15851 | `m3ctl-gold-django-15851-20261123T174156Z` | resolved, 1/1, 8/8 | `m3ctl-noop-django-15851-20261123T174232Z` | unresolved, 0/1, 8/8 |
| sympy-22714 | `m3ctl-gold-sympy-22714-20261123T175545Z` | resolved, 1/1, 11/11 | `m3ctl-noop-sympy-22714-20261123T175621Z` | unresolved, 0/1, 11/11 |

Each had patch applied, valid, 0 errors. Earlier preparation controls (`runs/m1/e1ctl-*`, `runs/m1/m3ctl-*-20261122*`) exist and were not re-inspected for this report.

### 5.6 Handoff flags and their actual meaning

Four runs are `ready_flagged`: 18621-B, 15851-B, 22714-A, 22714-B. **The flag strings are inverted relative to their triggers.** In `src/aiep/handoff.py` (lines 292 and 296), each check's label states the passing condition and is added to `flags` only when the check fails. A flag reading "the patch does not modify a file the hidden test patch also modifies" therefore means the patch **does** modify such a file.

| Run | Flag as written | What actually triggered it | Classification |
|---|---|---|---|
| 18621-B | "agent's output and patch repeat no hidden-patch line…" | 1 gold-patch added line (49 characters) appears in the agent's patch and output. It also occurs in the dataset's public hints text, which was not in the prompt. | informational |
| 22714-A | same | 1 gold line (63 characters), not in prompt or issue text | informational |
| 22714-B | same | same line, byte-identical patch to 22714-A | informational |
| 15851-B | "patch does not modify a file the hidden test patch also modifies…" | overlap count 1: the agent edited `tests/dbshell/test_postgresql.py`, which the hidden test patch also touches. `eval.sh` runs `git checkout <base> tests/dbshell/test_postgresql.py` before applying the hidden test patch, so the agent's test edit is discarded at grading. | informational for grading. Not an integrity concern. |

**[Interpretation]** The gold-line matches are expected when a fix is one line, and they are not evidence of a leak. The handoff's input-leak check passed in all runs (nothing hidden in `prompt.txt`). They do not exclude familiarity with the upstream fix (Section 7.6).

---

## 6. Manual Transcript Audit

Transcript line numbers (`L<n>`) refer to `runs/m2/<run_id>/claude_stdout.jsonl`. Audit scope: every run, in full, against its prompt, patch, ledger, evaluator log and handoff report.

**What was audited.** Prompt text and diff between arms; source investigation; every edit (Edit/Write and Bash heredoc or `sed`); every test command and output; patch content; hash equality between agent and evaluator patch; evaluator counts; ledger completeness; and handoff flag triggers. The re-run of the parser reproduced the stored counts, and the transcripts confirmed every parser classification except the cycle definition (Section 7.8).

### 6.1 sympy-18621 (BlockDiagMatrix with one element)

- **A (run 1).** Two greps of `blockmatrix.py` (L3, L6). One Bash python heredoc replace added a special case for a single block (L11). It ran the issue's example inline afterward, which is not a test runner, and ran no tests. The agent stated "I didn't run the test suite" (L13). The cause it gave (a 1×1 nested list is flattened) matches the B transcript. Patch sha `1fece3fa…`. Resolved, 1/1 and 15/15.
- **B (run 2).** Grep, two Reads, and a diagnostic repro that showed the block entry was a scalar (L3 to L9). One `Edit` passed an `evaluate=False` flag where the block matrix is built (L16). Then a combined repro and `python -m pytest …` failed with "No module named pytest" (L18, L19). **B initially encountered missing pytest, then used SymPy's own runner** (L20), which reported 16 passed (L23). No test failure and no later edit. Patch sha `74fe2284…`. Resolved, 1/1 and 15/15. Flag: gold line (informational).
- **Assessment.** Both patches are technically sound and differ in approach (special case vs general flag). B's tests were a post-edit regression check. The pytest error cost B one request.

### 6.2 django-13933 (ModelChoiceField invalid-choice message)

- **B (run 3).** One grep/`sed` (L3). Two Bash python heredoc edits (L5, L7) changed the default message and added `params={'value': value}`. The L5 script contained a no-op identity replace and a `replace(<pattern>, "X")` that did not match, so nothing was corrupted. The final patch contains no stray text. That is luck, not care. `runtests.py model_forms forms_tests` ran once at the end of the L7 command: 866 tests, 4 failures (L10), only one diff visible because the output was tailed. The agent said "old tests expecting the old message… I'll update them" (L11), grepped for the stale assertions (L12), and stopped without editing or re-running. Its final message states it did not re-run and did not update them (L15). **The implementation was not revised, no test was repeated, and `compliance=false`.** Patch sha `02f21985…`. Resolved, 1/1 and 19/19.
- **A (run 4).** Grep, two `Edit` calls (L5, L7), a verifying Read (L9), no tests. Final message: "I haven't run any tests" (L14). The patch is functionally identical to B's (message string wrapped differently). sha `fbf830d7…`. Resolved, 1/1 and 19/19.
- **Task-specific finding.** Both patches change a default message that other existing tests assert. Tests asserting the old text exist in `tests/model_forms/tests.py`, `tests/model_forms/test_modelchoicefield.py`, `tests/model_formsets/tests.py` and `tests/admin_widgets/tests.py` (from B's own grep, L13). The hidden test patch touches a single, different test file, so those assertions are outside the evaluator's grading scope. B observed 4 failures in `model_forms` and `forms_tests` and A observed none because it ran nothing. **Existing assertions outside the evaluator's scope very likely remain broken under both patches.** This is an inference from the transcript and the file list. The tests were not executed here.

### 6.3 django-15851 (dbshell parameters before dbname)

- **B (run 5).** One grep of `postgresql/client.py` (L3). One `Edit` moved `args.extend(parameters)` before the dbname (L5). This was the implementation fix, made before any test. `runtests.py dbshell.test_postgresql` ran: 10 tests, 1 failure, a stale expectation of the old argument order (L7, L8). The agent said "old test expecting the old order; update it" (L9), then ran `sed -i` on **`tests/dbshell/test_postgresql.py`** and re-ran in the same command (L10): `OK (skipped=1)` (L11). **B edited a test expectation, not the implementation fix.** The implementation was never revised. The parser counts this as one cycle (bash-edit-only). It is a test-update cycle under this report's definition. Patch sha `d0dbfb73…` (implementation hunk plus one test-line change). Resolved, 1/1 and 8/8. Flag: test-file overlap (informational).
- **A (run 6).** One grep (L3), one `Edit` (L5), no test run. The implementation hunk is byte-identical to B's (same blob hash `3b5ddafaca`). Final message: "I haven't run the tests… I didn't update the existing PostgreSQL dbshell tests, which probably still expect the old order" (L7). The prediction was correct, as B's run shows. sha `15989da8…`. Resolved, 1/1 and 8/8.
- **Evaluator coverage.** The single stale test is inside the file the hidden test patch rewrites, so it is graded. Both arms' evaluator output shows 10 tests run, `OK (skipped=1)`. A scan of the seed's Python files found no other test file referencing `psql`, so no other test is likely affected [Observed by grep; not executed].

### 6.4 sympy-22714 (imaginary-coordinate check under `evaluate(False)`)

- **A (run 7).** One grep of `point.py` showing the checked line (L3, L4). A diagnosis stating that the imaginary-part call stays unevaluated and is truthy under `evaluate(False)` (L5). One `sed -i` on that line plus an inline repro (L6 to L7). No tests (L8). The diagnosis was asserted from reading the code and was not tested in isolation. The post-edit repro confirms the fix, not the stated mechanism. Patch sha `c10b66d0…`. Resolved, 1/1 and 11/11.
- **B (run 8).** Read the same region (L3, L4), gave nearly word-for-word the same diagnosis (L5), and made the same `sed -i` edit combined with a repro and `python -m pytest …`, which hit the missing pytest (L6, L7). It then ran `sympy.test('sympy/geometry/tests/test_point.py')`: 12 passed (L8, L11). No failure and no revision. Patch sha `c10b66d0…`, **identical to A's**. Resolved, 1/1 and 11/11.
- **Task-specific finding.** The A and B patches are byte-identical single-line changes to the condition that rejects imaginary coordinates. The single changed line equals one gold-patch added line (63 characters), flagged in both runs. The line is not in the prompt or the public issue text. The workflow prompt changed what the agent did (a runner attempt and a confirmation run) but not what it produced.
- **Coverage.** The change loosens rejection to cases where the imaginary part is definitely non-zero, so numbers with an undecidable imaginary part are no longer rejected. Both agents ran only `test_point.py` and so did the grader. Other geometry tests were not run by anyone. The risk is low and untested [Interpretation].

### 6.5 Cross-run behaviours

- **Self-reports.** In all eight runs the agent's final message matched its transcript: it stated what it ran, what failed, and what it did not do.
- **Style.** Runs used very few tool calls (2 to 7), typically one grep, one edit, and at most two test attempts, with no extensive exploration.
- **Edit mechanics.** Five runs edited via Bash (heredoc or `sed`) instead of the `Edit` tool: 18621-A, 13933-B, 15851-B (test file only), 22714-A and 22714-B. For these, `edits_total` is 0 or understates edits.
- **Transcript size.** `claude_stdout.jsonl` for 13933-A is 138,788 bytes against 27,664 for 13933-B because the `Edit` result echoes the original file. The field `contentNotInModelContext` shows the model never saw it. It has no experimental effect.

---

## 7. Discussion

### 7.1 Why 8/8 run-level resolution does not show a benefit from iteration

There were four distinct tasks and eight scored runs. Both A and B resolved every one of the four tasks (8 of 8 runs), so the primary outcome has no variance, and a difference in resolution rate cannot be estimated. In pair 4 the two arms produced byte-identical patches, and in pair 3 identical implementation hunks. Resolution was achieved in one pass by every run. In no run did an agent need to revise a fix. The data therefore cannot show that iteration helps, and it also cannot show that iteration is unnecessary, because no run created a situation where iteration was required.

### 7.2 Ceiling effect and sample size

The tasks are small (gold patches of 2 to 6 changed lines), each with one F2P test. There are four task pairs with one repetition per cell, so run-to-run variation of the model is not measured. The definition warns of ceiling effects: "a task that A already solves in a few requests leaves B no room to help". That is what the data show.

### 7.3 Test invocation versus iteration

The manipulation changed behaviour, since B started a runner in 4 of 4 runs and A in 0 of 4. It did not produce iteration. Tests were run once after the first edit and either passed (two runs), failed due to stale tests (two runs), or failed to start (two attempts). The prompt raised test invocation while leaving fix revision at zero. Reporting "B ran tests" as support for the iterative claim would be wrong.

### 7.4 Cache warmth and the cost-comparison confound

The 600-second gap was designed so the second arm of a pair starts outside the first arm's 5-minute cache window. It does not separate pairs, and within the definition it is stated only for the two arms of one task: the first arm of the next pair began about 70 seconds after the previous pair's second arm finished (run_done to run_start: 72, 69 and 69 s in `state.json`). The ledgers show the consequence [Observed, `gateway_ledger.jsonl`]:

| Run | First-request cache-read tokens | First-request cost | Position |
|---|---|---|---|
| 13933-B | 6,205 (warm) | $0.005772 | first arm of pair 2 |
| 15851-B | 6,205 (warm) | $0.0053445 | first arm of pair 3 |
| 22714-A | 6,205 (warm) | $0.0059295 | first arm of pair 4 |
| 18621-A, 18621-B, 13933-A, 15851-A, 22714-B | 0 (cold) | $0.0200 to $0.0222 | all second arms and the very first run |

Warm arms were B, B and A in pairs 2 to 4. The first-request cost gap (about $0.015) exceeds the unadjusted total cost difference in pairs 2 and 3, so the sign flips there. Cache state follows run position, not arm. Any dollar comparison from Stage 1 must either use a basis that reduces cache effects (the uncached-equivalent repricing, which prices all input-side tokens at the base rate, or the first-request-excluded sensitivity analysis, which drops the cold or warm first request but leaves later cache behaviour in place) or be recorded with this confound. On both bases B was costlier in all four pairs. These are descriptive adjusted comparisons on heterogeneous tasks with n=4. They are not billing differences and not estimates of an effect.

### 7.5 Missing pytest in SymPy

The SymPy images have no `pytest`. Both SymPy B runs reached for pytest, failed, and recovered with `sympy.test(...)` (18621-B L18/L19, 22714-B L6/L7). That cost a request each. The SymPy A runs never tried a runner, so this cost is arm-specific by construction. The B prompt does not say how tests should be run. A B-side cost therefore partly reflects an environment mismatch rather than the workflow itself.

### 7.6 Potential benchmark contamination and gold-patch overlap

SWE-bench Lite is public and its upstream fixes are widely visible. Three of eight patches carry a gold-line overlap flag (18621-B, 22714-A, 22714-B), and several runs reached a minimal, upstream-shaped fix after one or two source reads. The transcripts cannot distinguish reasoning from familiarity. In 22714 the two arms produced the same diagnosis in nearly the same words. The hidden-data scan only sees added lines of 40 characters or more, so short changes (such as the django-15851 reordering) cannot be flagged. Absence of a flag is not evidence of independence from the upstream fix.

### 7.7 Evaluator coverage limitations

"Resolved" means all F2P tests pass and the P2P tests drawn from the touched test file are maintained. It is not evidence that the entire repository suite is regression-free. P2P counts are small (15, 19, 8, 11). In django-13933 both patches break existing assertions in other test files that the evaluator never runs. Grading says nothing about test-suite maintenance, minimality or code quality. For sympy-22714 and sympy-18621 only the single touched test file was run by anyone.

### 7.8 Parser limitations

1. **Cycle ignores the edit target.** The parser counts any edit between a failed run and a later started run as a cycle. In 15851-B the edit was to a test file. The parser records the target but does not use it. The parser's headline count of one cycle is therefore a false positive against the prompt's wording ("a revision of your fix").
2. **Heuristic Bash edits.** A Bash edit-like command shows that a file-writing command ran, not that a file changed. Five runs edited via Bash, so `edits_total` (Edit/Write only) understates edits and must not be compared across arms.
3. **Compliance is not iteration.** `compliance_B` is "a runner started AND (no failed run OR at least one cycle) AND cycles ≤ 5". It is true for 18621-B and 22714-B, where nothing failed and no iteration happened, and for 15851-B, where the only cycle was a test update. It is false for 13933-B, where a failure was analysed but not acted on. For A it is false by construction. It must not be read as demonstrated iterative fixing.
4. **Per-call attribution.** Test outputs are attributed per Bash call, so a call that combines edit, repro and test (22714-B L6, 15851-B L10) is classified as a single attempt.

These limitations did not change any run's classification in this audit apart from the cycle count above.

### 7.9 Inverted handoff flag labels

As described in Section 5.6, flag strings in `results.jsonl` and in the handoff reports state the passing condition. A reader taking them literally would reverse their meaning. The defect is in presentation, not in the checks themselves.

---

## 8. Threats to Validity

### 8.1 Internal validity

- **Cache and order effects** on cost (Section 7.4). Order was balanced by design (A first in two pairs, B first in two), but cache warmth follows position, and pair 1 had no warm arm.
- **Environment mismatch:** no pytest in SymPy images (Section 7.5).
- **Run-to-run model variation** is unmeasured (single repetition).
- **Arm differences beyond the prompt** were checked: model, binary, image, limits, budget and evaluator are identical, and the only other `run.json` differences are ids, timings, ledgers and network ids.
- **Uncontrolled client settings:** thinking budget, stream idle timeouts and retry back-off are not pinned (`claude_runtime.uncontrolled`).
- Config A may run tests spontaneously (it did not in Stage 1), which would narrow the contrast.

### 8.2 Construct validity

- "Iterative test-and-fix" is defined as failure-driven revision of the implementation, which never occurred. The prompt manipulated test invocation. The construct was not exercised.
- Resolution is a binary harness verdict on a narrow test set, so it is a weak proxy for fix quality.
- Gateway-computed cost excludes any billing outside the gateway (for example Console rounding or other keys).

### 8.3 Measurement validity

- Parser v2 heuristics (Section 7.8) and the inverted flag labels (Section 7.9).
- The hidden-data scan's 40-character blind spot (Section 7.6).
- `claude_duration_ms` and `total_cost_usd` are client-side figures. `wall_seconds` includes container startup.
- Evaluator P2P scope (Section 7.7).

### 8.4 External validity

- Four tasks from two repositories, chosen for a $5 budget, not sampled at random from SWE-bench Lite. They are small, solved in one pass, and likely seen in training. Nothing generalises beyond them.
- One model and one client version.

### 8.5 Statistical limitations

- **n=1 per task per configuration cell**, four pairs, eight runs. No significance test, confidence interval or effect-size inference is made, and none would be justified. Stage 1 is far too small for statistical inference on a paired binary outcome. `docs/M3_EXPERIMENT_DESIGN.md` quotes a required-sample-size figure, but it does not record the significance level, power or formula behind it, so no numerical estimate is reproduced here.
- The 2×2 resolution table is: both resolved 4, neither 0, A only 0, B only 0. It has no discordant pairs.
- Cost comparisons are descriptive, adjusted differences of gateway-computed figures on heterogeneous tasks. They are confounded as in Section 7.4 and are neither billing differences nor causal estimates.

---

## 9. Conclusion

**What Stage 1 established**

1. The end-to-end harness works. Eight scored runs completed with isolation, gateway metering, exact and complete accounting ($0.2337872 over 34 requests, all HTTP 200), valid handoffs, independent grading, and all eight gold and no-op controls passing.
2. The behavioural manipulation worked: **Config B started a test runner in 4 of 4 runs and Config A in 0 of 4.**
3. All four tasks were resolved under the stock evaluator in both arms, so all eight scored runs resolved their task. (There were four distinct tasks and eight runs, not eight tasks.)
4. Config B used more requests (20 against 14), more turns and tool calls. Its cost was also higher in all four pairs on the uncached-equivalent repricing and on the first-request-excluded sensitivity analysis (both descriptive, neither removes all cache effects). This is a descriptive pattern, not a billing difference or an estimate of an effect.

**What Stage 1 did not establish**

1. It did **not** show a confirmed fix-revision cycle. In no run did a test failure lead to a revision of the implementation, followed by a repeated test. The parser's one cycle is a test-expectation edit.
2. It did **not** establish whether iteration improves resolution. With 8 of 8 scored runs resolved (four tasks, both arms), identical patches in one pair and identical implementation hunks in another, there is no outcome signal.
3. It does not show that resolution implies regression-freedom. Evaluator scope is narrow, and in django-13933 both patches likely leave other existing assertions broken.
4. It supports no statistical or causal claim, and no claim about performance beyond these four tasks.

Stage 1 is a feasibility and manipulation check. It does not test the iterative test-and-fix claim, and it cannot answer the research question in either direction.

---

## 10. Recommendations and Future Work

This section lists conditions to satisfy before any decision about Stage 2. It does not recommend running Stage 2. The decision belongs to the project owner, and nothing in this report activates it.

1. **Define fix-revision cycles correctly.** Require that the edit between a failed and a repeated run changes implementation code and not a test file. The parser records edit targets already. A superseding metrics definition needs a new experiment id because E1 is locked. This report makes the distinction manually.
2. **Correct the handoff flag labels** so that a flag names the triggering condition ("the patch modifies a file the hidden test patch also modifies").
3. **Decide a cache and cost-comparison policy before any run.** Options: report the uncached-equivalent repricing and a first-request-excluded sensitivity analysis (stating that neither removes all cache effects), standardise the gap between all runs (including across pairs), or record cache state as a covariate. The choice should be recorded before running.
4. **Screen harder tasks for iteration opportunities and contamination.** A useful task is one where a first attempt plausibly fails. Candidate screening should consider issue difficulty, whether A solves it in few requests, and whether gold lines appear in public text. Candidate Stage 2 tasks (sphinx-10325, requests-863) are unprepared, and requests-863 has a pending compatibility decision.
5. **Resolve the SymPy pytest question.** Either tell both arms how to run tests in that environment (a prompt change that needs a new definition), provide the runner, or accept and report the cost as B-side environment friction.
6. **State a narrower Stage 2 objective.** For example, whether B produces any fix-revision cycles on tasks where a first attempt fails, rather than a resolution comparison that two more pairs cannot power.
7. **Treat approvals as mandatory.** Any new run requires explicit approval, an exact spend approval equal to the definition's cap, and a fresh Console billing check. The Console figures in the project documents predate Stage 1 and are stale. The `$0.50` reserve stays unspent.

Open work that does not depend on running anything: a parser update for edit targets, relabelled flags, and a reanalysis of Stage 1 under those definitions.

---

## Appendix

### A. Experiment definition and identity

| Item | Value |
|---|---|
| Experiment id | `e1-exploratory-1`, kind `exploratory_ab`, status `locked` |
| Definition SHA-256 (self-hash) | `9601b345da705b7e8bbdba251b902f4c7637482fd2e87e732dcba0dc36875eff` |
| Superseded draft hash | `434a94a2…` (from project docs, not re-verified) |
| Derived from | `m3-pilot-v2` (`590536ec64024e8216e1453f51367f931b154178745fa1adde4c951cae60c161`) |
| Lock commit | `bc9de7a1140f0fbaf31fec7b54af45b5f5d4c724` |
| Harness commit at run time | `28e758e5f3ccf4598fe843fe4a3abbed3628eb83` |
| Prompt set | `config/workflow_prompts_v2.json`: A `5e44cde5…`, B `cd6fbb95…`, template `86571bc7…` |
| Dataset revision | `b0dde1093fe417d83b7184254edf8199c1f0dff5` |
| Transcript parser | version 2; B cycle cap 5 |
| Evaluator | `swebench==5.0.2` via `aiep.stock_eval` |

### B. Task and arm ordering

| Order | Run id | Slot |
|---|---|---|
| 1 | `m2-sympy__sympy-18621-A-20261123T171501Z` | `…/sympy__sympy-18621/A/r1` |
| 2 | `m2-sympy__sympy-18621-B-20261123T172624Z` | `…/sympy__sympy-18621/B/r1` |
| 3 | `m2-django__django-13933-B-20261123T172904Z` | `…/django__django-13933/B/r1` |
| 4 | `m2-django__django-13933-A-20261123T174038Z` | `…/django__django-13933/A/r1` |
| 5 | `m2-django__django-15851-B-20261123T174307Z` | `…/django__django-15851/B/r1` |
| 6 | `m2-django__django-15851-A-20261123T175434Z` | `…/django__django-15851/A/r1` |
| 7 | `m2-sympy__sympy-22714-A-20261123T175656Z` | `…/sympy__sympy-22714/A/r1` |
| 8 | `m2-sympy__sympy-22714-B-20261123T180803Z` | `…/sympy__sympy-22714/B/r1` |

Slot ids are prefixed `e1-exploratory-1`. The first arm per task: A for sympy-18621 and sympy-22714, B for the django tasks.

### C. Aggregate accounting (gateway-computed, recomputed from ledgers)

| Quantity | Value |
|---|---|
| Total computed cost | $0.2337872 (A $0.1118429 + B $0.1219443) |
| Claude-reported (client-side) total | $0.2337872 (equal per run, but an estimate, not independent) |
| Requests | 34 (A 14, B 20), all HTTP 200 |
| Input / output tokens | 68 / 6,422 |
| Cache-read tokens | 246,137 (A 90,644; B 155,493) |
| Cache-write 5-minute tokens | 57,927 (A 30,541; B 27,386); 1-hour: 0 |
| All token fields combined | 310,554 |
| Uncached-equivalent total | $0.672484 |
| Overshoot | $0 in all eight slots; no request charged at an upper bound |
| Calibration (excluded) | about $0.0906 |
| Cap / budgets | $3.84 total; $0.18 per run; Stage 1 reservation exactly equals the cap |

Console billing and the remaining prepaid balance were **not** checked in this report.

### D. Key artifact locations

| Artifact | Path |
|---|---|
| Locked definition | `config/experiments/e1-exploratory-1.json` |
| Batch state and events | `runs/m3/e1-exploratory-1/state.json` |
| Result rows | `runs/m3/e1-exploratory-1/results.jsonl` |
| Agent run directories | `runs/m2/m2-<instance>-<A|B>-<stamp>/` (`prompt.txt`, `claude_stdout.jsonl`, `gateway_ledger.jsonl`, `patch.diff`, `predictions.jsonl`, `run.json`, `artifacts_manifest.json`) |
| Handoff reports | `runs/handoff/<run_id>.handoff.json` |
| Gradings | `runs/m1/m3eval-<run_id>/` (`meta.json`, `logs/run_evaluation/…/report.json`, `test_output.txt`, `eval.sh`) |
| Controls | `runs/m1/m3ctl-{gold,noop}-<task>-<stamp>/` |
| Pricing and prompts | `config/model_pricing.json`, `config/workflow_prompts_v2.json` |
| Parser and handoff code | `src/aiep/transcript.py`, `src/aiep/handoff.py` |
| Architecture and design | `docs/M2_EGRESS_ARCHITECTURE.md`, `docs/M3_EXPERIMENT_DESIGN.md`, `docs/PROJECT_STATUS.md` |

File SHA-256 prefixes at the time of writing: `results.jsonl` `cd13a7cd39306859`, `state.json` `e7d5fd4680c5039f`, definition file `b41b87450eb9f0b1` (file hash, which differs from the self-hash above). `runs/` is git-ignored, so these artifacts exist only on the project host.

### E. Metric definitions (summary)

- **resolved:** all F2P pass and all P2P maintained (harness verdict).
- **test attempt / started / not started / failed:** Section 4.2. A `not_started` attempt is never a failed suite.
- **test_fix_cycles (parser):** failed run, then any edit, then a later started run. `test_fix_cycles_confirmed_edit`: the edit was an Edit/Write call.
- **confirmed fix-revision cycle (this report):** failure caused by the fix, then an implementation edit, then a repeated run (manual).
- **compliance_B (parser):** a runner started AND (no failed run OR at least one cycle) AND cycles ≤ 5.
- **computed cost / uncached-equivalent cost:** Section 4.5.

### F. Assumptions and unavailable measurements

- **Not verified against external billing.** Costs are gateway-computed. No Console figure was consulted, and the sum equals Claude-reported cost only as a consistency check.
- **Pricing** is taken from `config/model_pricing.json`, retrieved 2026-11-19, and was not re-fetched.
- **Per-request latency** and **per-token timing** were not analysed. Wall figures are run-level only.
- **Thinking tokens** are included in the client report (`output_tokens_details`) but were not separated here.
- **Hidden test contents, F2P/P2P identifiers and the gold patch** are deliberately omitted. Gold-line overlap is reported as counts and lengths only.
- **Not re-inspected:** the earlier preparation controls (`e1ctl-*`, 2026-11-22 `m3ctl-*`), the three agent-adjacent container images' digests (taken from `config/image_digests.json`), and per-run `create_kwargs.jsonl` for all eight gradings (two sampled).
- **Timestamps** are UTC from `state.json` events. Run directory stamps are UTC run-start stamps.
- **Stage 2** tasks were not prepared, run or evaluated, and nothing here predicts their behaviour.

# AIEP

AIEP is a local research harness for measuring how a prescribed workflow changes the behaviour, issue resolution and cost of a coding agent (Claude Code) on SWE-bench Lite tasks. It is built to keep the experiment honest: two configurations differ **only** in one sentence of the prompt, the agent runs in an isolated container with no direct internet access, every API request is metered by a gateway, and grading is done by the stock SWE-bench evaluator in a separate environment.

**Research question.** Does prescribing an iterative test-and-fix workflow improve Claude Code's issue resolution, and at what additional cost?
Config A is "investigate, fix, stop". Config B adds "run the relevant tests, analyse failures, revise, repeat (at most 5 cycles)".

**This is an exploratory research platform, not a benchmark and not a statistically powered study.** See [Current status](#current-status) and [Known limitations](#known-limitations).

## How it works

```
locked experiment definition  ->  batch orchestrator  ->  per task: prepare pinned image + seed, gold and no-op controls
   ->  per arm: proxy + gateway + isolated agent container  ->  patch extraction  ->  handoff validation
   ->  stock SWE-bench evaluation (separate container)  ->  transcript metrics  ->  results JSONL
```

| Stage | What it does | Main code |
|---|---|---|
| Experiment definition | A self-hashed JSON file fixes tasks, prompts, model, Claude binary hash, budgets, schedule, stop conditions and metric definitions. A locked definition is never edited. | `src/aiep/experiment_def.py`, `experiment.py`, `config/experiments/`, `config/workflow_prompts_v2.json` |
| Task preparation | Each task's SWE-bench image is pinned by digest, its `/testbed` is extracted into a hash-verified seed archive, and the container identity is verified. | `task_pins.py`, `image_digests.py`, `seed*.py`, `task_setup.py` |
| Orchestration | Runs tasks in a registered order, both arms back to back with a 600 s gap, with disk gates, a pair-atomic budget reservation, explicit halt conditions and resumable state. Plan-only by default. | `m3_batch.py`, `m3_steps.py`, `scripts/run_m3_pilot.py` |
| Isolated agent | Claude Code runs as a non-root user in a container on an internal network with no route out. It receives only the issue text and a per-run token, never the hidden tests, the gold patch or the API key. | `agent_runner.py`, `container_policy.py`, `lifecycle.py`, `networks.py` |
| Gateway and metering | The only holder of the real API key. It checks the model, forwards each request and writes a ledger of measured token usage. Cost is computed from tokens and `config/model_pricing.json`. The budget is a soft limit. | `gateway.py`, `ledger.py`, `pricing.py` |
| Restricted egress proxy | The only container with a route out. It allows one destination (`api.anthropic.com:443`), validates every resolved address and never touches TLS. It holds no secret. | `egress_proxy.py`, `proxy_lifecycle.py`; see `docs/M2_EGRESS_ARCHITECTURE.md` |
| Patch extraction and handoff | The agent's changes leave only as a text diff. A validator checks artifact hashes, identity, accounting and a hidden-data scan before anything is graded. | `patch.py`, `handoff.py`, `rejection_policy.py` |
| Evaluation | The stock SWE-bench harness (`swebench==5.0.2`) applies the patch and the hidden test patch in a fresh container from the official image. A wrapper drops capabilities and blocks image acquisition. | `stock_eval.py`, `grading.py` |
| Results | Transcript metrics (test runs, edits, test-fix cycles), one result row per run, and a descriptive paired analysis. | `transcript.py`, `results.py`, `analysis.py`, `scripts/m3_analyze.py` |

## Current status

| Milestone | State |
|---|---|
| M0, M1: project skeleton, SWE-bench environment and stock evaluator | Complete (`docs/M1_RESULTS.md`) |
| M2: isolated agent, gateway, restricted egress proxy | Implemented and validated on real Docker Desktop against local mocks (`docs/M2_EGRESS_ARCHITECTURE.md`), then exercised with real API calls in two calibration runs (12 requests, about $0.09 gateway-computed). Calibration runs are never scored. |
| M3: experiment harness, batch orchestration, grading, transcript parser v2, analysis | Implemented (`docs/M3_EXPERIMENT_DESIGN.md`) |
| **E1 Stage 1** (`config/experiments/e1-exploratory-1.json`, locked) | **Executed on 2026-11-23.** 4 tasks, one run per arm per task, so 8 scored runs. |
| E1 Stage 2 (sphinx-10325, requests-863) | **Not run.** Conditional, unprepared and not activated. |

**E1 Stage 1 results** (gateway-computed, not Console billing; details in `docs/research/E1_STAGE1_REPORT.md`):

- 8 of 8 scored runs completed, were valid, passed handoff validation and were graded; all 8 gold and no-op controls passed. Spend was $0.2337872 over 34 requests with exact accounting.
- All four tasks were resolved in both arms, so all 8 runs resolved their task.
- Config B started a test runner in 4 of 4 runs and Config A in 0 of 4.
- A manual transcript audit found **no confirmed fix-revision cycle**. The parser counts one test-fix cycle (django-15851, B), but its edit changed a test expectation, not the fix.
- Because every task resolved in both arms, **Stage 1 shows that the harness works and that the prompt changes test-running behaviour. It does not show whether iteration improves resolution.**

## Known limitations

- **Statistics.** One repetition per task per arm, four tasks, all resolved (ceiling effect). Nothing here supports a claim that one workflow is better. Cost differences are descriptive and confounded by prompt-cache warmth.
- **Contamination.** SWE-bench Lite is public. Some agent patches match lines of the upstream fix; the transcripts cannot tell reasoning from familiarity.
- **Evaluator coverage.** "Resolved" means the task's F2P tests pass and the P2P tests from the touched test files are maintained. It does not show the whole repository suite is regression-free.
- **Evaluator environment.** The stock evaluator needs network access for pip build dependencies, which are not pinned or recorded (`docs/M1_RESULTS.md` section 8). The hardened successor (`evaluator.py`, `gate.py`) is implemented and unit-tested but **not operational**: its wheelhouse is empty, and no grading has used it.
- **Isolation is not a host sandbox.** The proxy container can reach Docker Desktop host services at the network level. Its safety rests on its code and on holding no secret (`docs/M2_EGRESS_ARCHITECTURE.md` section 5).
- **Metrics.** The parser counts a test-fix cycle even when the edit is to a test file. Handoff flag strings are phrased as the passing condition, so a flag means that condition failed. The hidden-data scan ignores lines under 40 characters.
- **Budget.** Gateway budgets are soft limits. Only a spend limit set on the API key itself is a hard cap.
- **M1 driver.** `scripts/m1_eval.sh` is the M1 evaluation driver, pinned by a test to the M1 commit. It runs only the M1 task (`pytest-dev__pytest-5227`); for other tasks use `scripts/m3_grade.py` (or `aiep.grading`). Its checkout path is derived from the script's own location.
- **Commit hashes in older documents.** Before release, the repository history was rewritten to remove private working notes and personal details, so commit hashes quoted in the research report, the evidence bundle and some documents (for example the execution commit `28e758e5f3ccf4598fe843fe4a3abbed3628eb83`) refer to the original, unpublished history and do not exist here. That commit's equivalent in this history is `087d65da7431d24ce14d882ddc28ae7354b7a3a3`. See `docs/HISTORY_REWRITE.md` for the full commit map; the experiment's results and run records are unaffected.
- **One unreproduced test failure.** A single failure of `tests/test_lifecycle.py::test_gateway_violations_invalidate_the_run_and_nothing_is_extracted[tool_violation]` was seen once during development in an earlier full-suite run. Its traceback was not saved and its cause is unknown, so it is not confirmed to be a flaky test. It has not recurred in 112 targeted runs (40 isolated, 64 under parallel load, 8 of the whole file) or in the several complete full-suite runs made while preparing this release (the exact number was not recorded).
- **One unexplained timeout in a fake-exec test.** `tests/test_proxy_lifecycle.py::test_keyboard_interrupt_while_the_agent_runs_removes_agent_gateway_and_proxy_in_order` failed once in a full fresh-clone run with `inject into /testbed failed: timed out`. That exec's deadline is 300 s and the run took about 280 s longer than normal (7:43 against about 3:00), so one exec waited out its full deadline: this was not a short timeout being missed under CPU load, and raising the deadline would not have helped. The cause was not diagnosed. It has not recurred in 25 isolated runs, 3 whole-file runs, 120 runs at 20-way parallelism (load average above 20; the slowest took 14 s) or a later unloaded full run (3406 passed, 7 skipped). If the suite hangs for minutes on one test, interrupt it and rerun that test alone.
- **Platform.** Validated only on macOS (Apple Silicon) with Docker Desktop, amd64 images under emulation, runs strictly one at a time, with at least 16 GiB free disk.
- **Artifacts.** `runs/` and `.cache/` are git-ignored. The raw run artifacts cited by the research report (transcripts, patches, evaluator logs) exist only on the machine that produced them. A sanitized subset (ledgers, results, summaries) is committed in `evidence/e1-stage1/`.

## Quickstart (no credentials, Docker or API access)

Everything in this section is read-only or runs against fakes. None of it calls the Anthropic API, starts a container or spends money.

**Prerequisites.**
- **Python 3.12.** `pyproject.toml` allows 3.10 or newer, but only 3.12 has been tested. The commands below were tested on macOS (Apple Silicon); Linux and Windows have not been tried.
- **A full `git clone`, not a shallow one.** Two tests compare files with commit `5bae33a` in the history, so a `--depth 1` clone is expected to fail them. This is inferred from the test code, not tested.
- **`uv`** for the install commands below (any virtualenv plus `pip` works too).
- **Network access only for the install step** (PyPI). Nothing after it needs a network, Docker, an API key or any file outside the repository.
- Steps 3 to 5 use only the Python standard library and this repository's `src/`, so they also run on a plain `python3` with nothing installed. The scripts add `src/` to the path themselves, so no `PYTHONPATH` is needed. Only the test suite (step 2) needs the installed packages.

**1. Install** (needed for the test suite only).

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e . pytest                 # latest compatible versions
# or, to reproduce the tested closure exactly (generated on CPython 3.12.2, macOS arm64):
uv pip install --python .venv/bin/python -r requirements/host.lock -e .
```

These two commands need PyPI and were **not** run for this README: the offline test environment could not download packages. What was checked is that `requirements/host.lock` matches the packages installed in the environment the tests ran in (`.venv/bin/python scripts/lock_host_deps.py --check` exits 0).

**2. Offline test suite.**

```bash
.venv/bin/python -m pytest
```

In one observed fresh-clone run the result was `3411 passed, 7 skipped` in about 2 minutes 35 seconds, with no failures. This is an observation from one environment, not a guaranteed result: the counts change whenever tests are added, and the skips depend on which private or optional data is present. The run used a copy of the repository without `runs/`, `.cache/` or `.venv/`, with real git history, using an existing virtual environment with the same packages. The 7 skips are expected on a clone: 4 tests need the git-ignored local dataset snapshot, 2 need git-ignored private artifacts, and 1 is a real-network check that stays off unless you set `AIEP_ALLOW_NETWORK_TESTS=1` (do not). The suite uses fakes, a loopback-only network guard (`tests/conftest.py`) and no real Docker client.

**3. Check the experiment definition and inspect the E1 plan.**

```bash
E1=config/experiments/e1-exploratory-1.json
python3 scripts/m3_experiment.py validate --definition $E1     # valid: e1-exploratory-1 status=locked sha256=9601b345da70...
python3 scripts/run_m3_pilot.py --definition $E1               # plan mode
```

Plan mode prints the schedule (4 pairs, 8 scheduled scored runs, Stage 2 marked INACTIVE), the limits, and ends with five `blocker:` lines (spend approval, pull approval, control approval, no key, no Claude binary). Exit code 0 with those blockers is success: it means nothing was started. It does not import the Docker library or open a socket. Note that `runnable=True` for the four Stage 1 tasks only says their pins are recorded in tracked files; it does not mean images or seeds exist on your machine. Without `--definition` both scripts default to the superseded, never-run draft `m3-pilot-v2`.

**4. Verify the committed evidence bundle.**

```bash
python3 scripts/verify_e1_stage1_evidence.py
```

Success: every check passes and the exit code is 0. In a full-history clone (the report `docs/research/E1_STAGE1_REPORT.md` is in the tree), `--require-git` reports `65 of 65 checks passed` and skips nothing. It recomputes costs, tokens, request counts, cache effects and pair gaps from the committed ledgers, checks that the bundle's recorded summaries agree with each other and with the research report, and checks the provenance record (the builder that generated the bundle against its committed version, the original execution commit against all eight run records, and that commit's published-history equivalent against Git). Checks that need Git history print SKIP, never PASS, when history is unavailable (for example in a shallow clone); `--require-git` turns a SKIP into a failure. See `evidence/e1-stage1/README.md` for what is recomputed and what is only a recorded summary.

**5. Reproduce the analysis of the Stage 1 results.**

```bash
python3 scripts/m3_analyze.py evidence/e1-stage1/batch/results.jsonl --definition $E1
python3 scripts/m3_analyze.py evidence/e1-stage1/batch/results.jsonl --definition $E1 --out analysis.json   # full figures as JSON
```

The first command prints, for both the as-scheduled and valid-run sets: `pairs: 4`, `both resolved: 4`, gateway-measured cost `A: 0.1118429  B: 0.1219443  ratio B/A: 1.0903`, and one line per run (for example `django__django-15851 B: ... attempts=2 started=2 failed=1 ... cycles=1 (confirmed 0, bash-only 1)`). The JSON adds per-arm token totals, request totals (14 and 20) and uncached-equivalent cost (`0.268796` and `0.403688`). The analyzer does not compute the first-request-excluded sensitivity analysis or the cache-warmth table; the verifier does. All dollar figures are gateway-computed from measured tokens, not provider billing. The analyzer's parser-derived cycle count counts a test-file edit (see Known limitations); the research report explains this.

**Warning: execution mode spends money.** `scripts/run_m3_pilot.py --execute ...` can pull images, start containers, call the Anthropic API and run the evaluator. Stage 1 cost about $0.23 and a re-run is not needed for anything above. It refuses to start unless you pass every approval flag (including `--approve-spend` equal to the definition's cap), supply a dedicated, spend-limited key in `AIEP_UPSTREAM_API_KEY` and a pinned Claude binary, and have Docker Desktop and the prepared task images. Never put the key on a command line. A plain `run_m3_pilot.py` without `--execute` is always safe.

## Repository map

- `src/aiep/` the harness; `scripts/` command-line drivers; `config/` pinned configuration, prompts and experiment definitions; `tests/` the offline test suite.
- `docs/M1_RESULTS.md`, `docs/M2_EGRESS_ARCHITECTURE.md`, `docs/M2_EVALUATOR_HANDOFF.md`, `docs/M3_EXPERIMENT_DESIGN.md` design and validation records.
- `docs/research/E1_STAGE1_REPORT.md` the Stage 1 research report. It is **frozen**: it is never edited, a test pins its SHA-256 (`8914aa8a3654a3807ed555467356d37f0ab47ca3de25ef496de452666d0c797a`), and it cites the original execution commit `28e758e5…`, which `docs/HISTORY_REWRITE.md` maps to this repository's history.
- `evidence/e1-stage1/` the sanitized Stage 1 evidence bundle; `scripts/verify_e1_stage1_evidence.py` checks it and `scripts/build_e1_stage1_evidence.py` regenerates it (only possible on the machine holding the private run artifacts).
- `docs/PROJECT_STATUS.md` a one-page project status; `docs/HISTORY_REWRITE.md` explains the pre-release history rewrite and maps original commit hashes to published ones. `docs/M2_GATE0_NOTES.md`, `docs/M2_PHASE1_NOTES.md`, `docs/M2_PHASE2_DESIGN.md` and `docs/M2_EVALUATOR_HANDOFF.md` are historical development records and carry banners saying so.

## Not planned

AIEP deliberately has no web UI, dashboard, database or cloud deployment, and no support for other models or agents. Further experiments (including Stage 2) would need a narrower research objective and fresh approvals, and none is scheduled.

## License

MIT; see `LICENSE`.

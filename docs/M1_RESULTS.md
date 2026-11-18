# M1 results: benchmark environment and evaluator validation

Status: **complete** (exit criteria met). Date of runs: 2026-11-18 (UTC 12:17–12:43).

This document records what was validated in M1, with exact identifiers so the results can be
reproduced and audited. Provenance labels:

- **recorded**: captured in the preserved run artifacts at run time (`runs/m1/`, local only).
- **measured**: observed interactively during the session; the command output was **not** preserved
  in the run artifacts, so it cannot be re-verified from `runs/m1/`.
- **inferred**: reconstructed afterwards; not recorded at run time.

Claims without a label were checked against the preserved artifacts or the installed `swebench`
5.0.2 source.

It deliberately contains hashes and counts only. It does not contain the gold patch, the hidden
test patch, or the `FAIL_TO_PASS` / `PASS_TO_PASS` test identifiers.

## 1. Objectives

1. Prove that the SWE-bench evaluation path works on this machine (Apple Silicon, amd64 images
   under emulation) for one SWE-bench Lite task.
2. Prove the evaluator discriminates correctly: the benchmark's gold patch must resolve the task,
   and a non-empty patch with no functional change must not.
3. Prove the evaluator is repeatable: two independent gold runs must agree.
4. Run the evaluator with reduced container privileges and enforced resource limits, without
   changing the benchmark's test behaviour.
5. Establish disk-safety rules for running benchmark images on a nearly full laptop disk.

## 2. Methodology

- **Task:** `pytest-dev__pytest-5227` from `SWE-bench/SWE-bench_Lite` (split `test`, 300 instances).
  Selected on metadata only (small image, small test set, no network needs in the tests), as an
  environment-proof task. It is not a pilot task.
- **Evaluator:** the stock SWE-bench harness (`swebench==5.0.2`), invoked through
  `src/aiep/stock_eval.py`. That wrapper changes only how evaluation containers are created
  (section 5); grading logic, eval scripts and tests are untouched.
- **Positive control:** the dataset's gold patch (`--gold`), run twice with unique run IDs.
- **Negative control:** a non-empty patch that appends a blank line and one reStructuredText
  comment line to `README.rst`. The harness never runs empty patches, so the control must be non-empty to
  exercise the tests.
- **Sequential only:** one worker, one container at a time (8 GB host).
- **Driver:** `scripts/m1_eval.sh` (section 7) enforces the disk policy and captures artifacts.

## 3. Runs

Run IDs are the directory names under `runs/m1/`. Each run used a new unique ID, because the
harness reuses an existing per-instance `report.json` for a repeated run ID.

| Run ID | Role | Verdict | Notes |
|---|---|---|---|
| `m1-gold-1-20261118T121741Z` | gold attempt, **INVALID** | none (killed) | Killed by the driver's mid-run disk guard when free space reached 11.84 GiB (floor 12 GiB). Free space went 12.85 to 11.84 GiB. The harness recorded `error_instances: 1`. No tests ran: the last log line was the start of pip's build-dependency step. Not counted in any result. Used an older driver (sha256 prefix `ac5dbd60cedaa49c`). |
| `m1-gold-2a-20261118T123336Z` | **accepted gold run 1** | **resolved** | See below. |
| `m1-gold-run2-20261118T123933Z` | **accepted gold run 2** | **resolved** | Identical results to gold run 1. |
| `m1-noop-20261118T124225Z` | **accepted no-op control** | **unresolved** | All 3 `FAIL_TO_PASS` tests failed. |

Naming note: `m1-gold-2a-…` is the first *valid* gold run (the "2a" suffix is a leftover from
renaming after the invalid first attempt). `m1-gold-run2-…` is the second.

### 3.1 Gold runs (both accepted)

| | gold run 1 (`…2a…`) | gold run 2 (`…run2…`) |
|---|---|---|
| Harness verdict | resolved | resolved |
| `FAIL_TO_PASS` | 3 of 3 passed | 3 of 3 passed |
| `PASS_TO_PASS` | 34 of 34 maintained | 34 of 34 maintained |
| Patch applied | yes | yes |
| Harness exit code / in-container test exit code | 0 / 0 | 0 / 0 |
| pytest summary | 34 passed in 1.35 s | 34 passed in 1.48 s |
| Test runtime reported by harness (includes `pip install -e .`) | 22.63 s | 19.08 s |
| Wall time (driver start to end) | 63 s | 48 s |
| Errors / infra failures / empty patches | 0 / 0 / 0 | 0 / 0 / 0 |

Agreement (measured): the two runs have identical gold patch, `eval.sh`, driver script, image ID and
container-create arguments; identical sets of 34 passing tests in the pytest `-rA` summary;
identical sets of all 58 status-word lines the harness parser ingests; and an identical per-instance
`report.json`. Only timings differ.

### 3.2 No-op negative control (accepted)

- Harness verdict: **unresolved** (completed 1, resolved 0, errors 0, infra failures 0, empty patches 0).
- Patch applied cleanly (`patch_successfully_applied: true`).
- `FAIL_TO_PASS`: **0 of 3 passed (3 failed)**, as expected.
- `PASS_TO_PASS`: 34 of 34 maintained (same composition as the gold runs, see section 8.1).
- Raw pytest: 34 collected, **3 failed, 31 passed**, in-container test exit code 1. The three failing
  tests are exactly the three `FAIL_TO_PASS` identifiers (checked as a set comparison).
- Harness runtime 18.72 s; wall time 47 s.
- The harness summary also printed `ambiguous_failure_instances: 1` with reason `no_tests_collected`.
  That label is a false positive (section 8.2).

## 4. Benchmark image and tool versions

| Item | Value | Source |
|---|---|---|
| Image | `swebench/sweb.eval.x86_64.pytest-dev_1776_pytest-5227:latest` | recorded (`meta.txt`) |
| Image digest | `sha256:c91459f86ed261ab81871b086d7ca2d9fe16abd2ddf374bf172edd5d991fd28b` | recorded |
| Image ID | `sha256:c91459f86ed261ab81871b086d7ca2d9fe16abd2ddf374bf172edd5d991fd28b` (same as digest) | recorded (`container_inspect.json`) |
| Platform | linux/amd64 only (no arm64 image exists) | measured (registry API) |
| Size | 1,025,572,437 bytes compressed; Docker image store grew by about 3.84 GB; 10 layers | measured |
| `base_commit` | `2051e30b9b596e944524ccb787ed20f9f5be93e3` | recorded (in each `eval.sh`) |
| swebench | 5.0.2 | recorded |
| Python (host venv) | 3.12.2 (uv 0.12.1), `datasets` 5.2.0 | measured |
| Docker | client 29.7.2 / server 29.7.2, Docker Desktop, containerd image store | recorded |
| Docker VM memory | 4,106,604,544 bytes (4 GiB setting); 8 CPUs | recorded |
| Host | Apple Silicon (arm64), 8 GB RAM, macOS; amd64 images ran under emulation | measured |
| Dataset revision | `b0dde1093fe417d83b7184254edf8199c1f0dff5` | **INFERRED, not recorded at run time** (see below) |

**Dataset revision is inferred.** The harness calls `load_dataset(name, split)` without a revision and
the driver did not record one. The value above is the only snapshot in the project's Hugging Face
cache (`.cache/hf`, git-ignored). Its reference file was written 17:47:54 local time, 12 seconds
after the first run started (12:17:42 UTC), and no second snapshot exists, which means every run used
this revision. This is a reconstruction, not a recorded fact, and the `.cache/` directory is not in Git.

## 5. Evaluator configuration and resource limits

The stock harness (`run_evaluation.create_container`) creates every evaluation container with
`cap_add=["SYS_ADMIN"]` unconditionally, on the default bridge network, as root, with no CPU,
memory or pid limits. Its code comment says the capability is for browser-sandbox tests, which this
task does not use. `SYS_ADMIN` was not granted. `src/aiep/stock_eval.py` wraps
`ContainerCollection.create` for the evaluation process so containers are created with:

| Setting | Value (recorded in `create_kwargs.jsonl`, confirmed by `docker inspect`) |
|---|---|
| Privileged | false |
| Added capabilities | none (`SYS_ADMIN` removed) |
| Dropped capabilities | `ALL` |
| Security options | `no-new-privileges` |
| Memory / memory+swap | 2 GiB / 2 GiB |
| PIDs limit | 1024 |
| CPUs | 2 (`NanoCpus` 2,000,000,000) |
| Mounts / binds / Docker socket | none |
| Environment keys | `PATH`, `TZ` only (no credentials) |
| User / network | `root` / `bridge` (unchanged from stock; see section 8.3) |
| Per-instance timeout | 900 s |
| Workers | 1 |

Result: with all capabilities dropped, the harness's `pip install -e .` build and the full test file
ran correctly. `SYS_ADMIN` is therefore not required for this task. Sampled container memory (recorded in
`container_stats.log`: `docker stats`, 7 samples roughly 4 seconds apart per run) peaked at 183.7 MiB
(gold run 1), 96.9 MiB (gold run 2) and 97.7 MiB (no-op), far below the 2 GiB limit. The true peak
between samples is unknown, because a short-lived spike would not be captured at this sampling rate.

## 6. Patch hashes and artifacts

| Item | SHA-256 | Size |
|---|---|---|
| Gold patch as applied (`patch.diff` in both gold runs; equals the dataset's gold patch) | `e8193ea0dee0989323f0f7756f492d73cdc2e584b61f9142b8695d9ed2737893` | 431 bytes |
| No-op patch (`runs/m1/noop-inputs/noop.diff`; equals `patch.diff` in the no-op run) | `8d0f89386e67e8037dc299700b7363af895b5af7b623210411eb05d02b0b7e89` | 408 bytes |
| No-op predictions file (`runs/m1/noop-inputs/predictions.jsonl`) | `659955266f4a48fe2ef79545b833cfe94c088238c556f19a889956ab14ef7e55` | |
| Per-instance `eval.sh` (identical in all three accepted runs) | `d73279f978e58ac371fe00d070c5612cddc929cf3269e90d584e51699944266c` | |
| Driver `scripts/m1_eval.sh` (identical to the copy saved in each accepted run) | `fc051f7bf4e5236aec9eb0613192b04deb1c12b7d633c020cc53338976fe2a1b` | 3,712 bytes |
| `src/aiep/stock_eval.py` (unchanged across the accepted runs) | prefix `8808450cec3f2853` | |

The no-op patch appends a blank line and one reStructuredText comment line to `README.rst` (two
added lines). It was checked with `git apply --check` against a pristine container before use
(**measured**: the output of that check was not preserved).

**Artifact locations.** Everything is under `runs/m1/` (73 files, about 0.9 MB). `runs/` is
git-ignored, so the artifacts are local and are **not** in this repository. Each run directory holds:
`meta.txt` (run ID, image digest, versions, exact command, disk readings), `container_inspect.json`
and `create_kwargs.jsonl` (applied configuration), `container_stats.log`, `disk_samples.log`,
`disk_before_gib.txt` and `disk_after_gib.txt`, `exit_code.txt`, `stdout.log`, `stderr.log`, a copy of
the driver, `reports/` (harness summary) and `logs/run_evaluation/<run>/…/` (`eval.sh`, `patch.diff`,
`report.json`, `run_instance.log`, `test_output.txt`, which includes the full pip and pytest trace).
`STOPPED.txt` exists only in the invalid run.

## 7. Disk policy and readings

The host disk was nearly full, so the driver enforces: free space of at least 16 GiB to start, a
sampler every 3 seconds (free space and the size of `Docker.raw` on disk), and a hard stop (container
killed) if free space falls below 12 GiB. The first attempt, started with only 0.85 GiB of margin,
was stopped by this rule.

| Run | Free before | Free after | Minimum during | Samples | `Docker.raw` |
|---|---|---|---|---|---|
| `m1-gold-1-…` (invalid) | 12.85 GiB | 11.84 GiB | 11.84 GiB (stop) | n/a (older driver) | n/a |
| `m1-gold-2a-…` | 17.153 | 17.152 | 17.148 | 22 | 21.877–21.881 GiB |
| `m1-gold-run2-…` | 17.129 | 17.135 | 17.129 | 17 | 21.877–21.881 GiB |
| `m1-noop-…` | 17.161 | 17.154 | 17.154 | 17 | 21.877–21.881 GiB |

The ~1 GiB drop during the invalid run was **not explained**. Session-only observations (**measured**,
not preserved in the run artifacts): `Docker.raw` did not grow, our own files totalled under 10 MB,
and host free space swung by 1–3 GiB without matching file activity. About 2.3 GiB was recovered
beforehand by deleting regenerable Homebrew cache files (**measured**), with the benchmark image,
virtual environment and run artifacts verified unchanged by hash.

## 8. Known issues and anomalies

### 8.1 Three junk `PASS_TO_PASS` entries (benchmark data defect, this task)
Three of this task's 34 `PASS_TO_PASS` entries are not test identifiers; they are log fragments
(progress-marker text). The harness parser reads every line of the log that starts with a status word,
including captured output of pytest-in-pytest tests, so these fragments appear in its status map and
are credited as "maintained". The outer pytest summary lists 34 passing tests, of which 3 are
`FAIL_TO_PASS` and 31 are real `PASS_TO_PASS`; the remaining 3 credited entries are the junk ones.
The grading code treats a *missing* key as a failure, so a correct fix that changed the captured
output could in principle be graded unresolved. Consequences: use this task only for environment
proof, screen candidate pilot tasks for `PASS_TO_PASS` entries that are not test identifiers, and
record the count per task.

### 8.2 Misleading `no_tests_collected` label
For unresolved instances the harness's advisory classifier greps the whole log for
`no tests ran|collected 0 items`. Those strings appear in captured inner output (present in the gold
logs too, but only unresolved instances are classified), so the no-op run was labelled ambiguous with
`no_tests_collected` although 34 tests were collected. `resolved` is unaffected. Classify failures
from the per-instance `tests_status`, exit codes and raw logs, not from `failure_reasons`.

### 8.3 Evaluator egress is unrestricted (open)
The evaluation container needs network access: `pip install -e .` uses build isolation and fetches
`setuptools`, `setuptools-scm` and `wheel` from PyPI. With networking disabled the step failed in
9 seconds (**measured**: an interactive test whose output was not preserved). The container runs on Docker's default bridge with no egress filtering. It holds no
credentials, no Docker socket and no mounts, but it executes agent-written patches with general
internet access.

### 8.4 Pip build dependencies are neither pinned nor recorded (open)
The build-dependency versions resolve from PyPI at evaluation time and the pip output is not verbose,
so the versions are not in the logs. The installed version string
(`pytest-4.4.2.dev177+gaa6a504d7.d20261118`) embeds the build date and the synthetic HEAD commit, so it
changes daily.

### 8.5 Dataset revision is inferred, not recorded (open)
See section 4. Neither the harness nor the driver records or pins the revision.

### 8.6 Repository state inside the image
(**Measured**: every figure in this section comes from interactive inspection of throwaway
containers; none of it is preserved in the run artifacts.)
`HEAD` is a synthetic commit titled "SWE-bench" on top of `base_commit`. The two trees have identical
content, differing only by file mode on 433 files, so changes must be extracted with
`git -c core.fileMode=false diff HEAD` (the harness does the equivalent). History leakage check:
10,160 commits all reachable from `HEAD`, 64,946 git objects all reachable (0 unreachable), newest tag
4.4.1, no remotes. No future commits are present.

### 8.7 Other observations
- The stock harness silently pulls a missing image from Docker Hub (`images.pull`, by the mutable
  `:latest` tag, with no digest check). The wrapper does not yet guard this.
- The wrapper hooks only `ContainerCollection.create`; unrecognised create arguments pass through, and
  `main()` returns exit code 0 even when instances error. It is correct for the pinned
  `swebench==5.0.2` but fails open if the harness changes.
- macOS marks the editable-install `.pth` file as hidden and Python ignores it; the project works
  around this with `pythonpath = ["src"]` for pytest and `PYTHONPATH` for the CLI (**measured**).
- Docker Desktop's VM disk had 18 GB in use by other projects before M1; none of it was modified
  (**measured**).

## 9. Gaps carried forward

1. Pin and record pip build dependencies (planned: pinned wheelhouse served by a local mirror).
2. Remove evaluator egress (planned: internal Docker network containing only that mirror).
3. Pin and record the dataset revision (offline mode against a recorded snapshot).
4. Harden the wrapper: allowlist create arguments, block image pulls and verify the expected image
   ID, fail on zero intercepted creates, exit non-zero on instance errors, add integration tests.
5. Lock host dependencies (only `swebench` is pinned) and make the driver portable and self-recording
   (git commit, wrapper hash, dataset revision). The driver currently hardcodes two absolute paths.
6. Measure how much disk space returns when a benchmark image is removed (per-task lifecycle).
7. Replace the Bash driver with Python orchestration (M3).

## 10. What M1 does not show

One task, two gold runs and one no-op run on one machine. It validates the evaluation path and the
hardened evaluator configuration. It says nothing about agent performance, about Claude Code's
behaviour in a container, or about other repositories, whose images may need capabilities this task
did not.

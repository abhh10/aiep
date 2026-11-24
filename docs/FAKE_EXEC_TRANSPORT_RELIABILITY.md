# Fake exec transport reliability: an investigation record

This document is the full technical record of an intermittent test stall that was diagnosed and mitigated shortly before release (2026-11-24 and 2026-11-25). The README carries only a summary. Everything here was measured on **one machine** (macOS, Apple Silicon, 8 GB RAM, Python 3.12); none of it is an operating-system guarantee, and none of it was run against real Docker.

## 1. Summary

| | |
|---|---|
| Symptom | In rare full-suite runs, one test failed with `inject into /testbed failed: timed out` after waiting out the 300 s exec deadline (about 280 s longer than a normal run). |
| Cause (captured and reproduced) | The fake Docker used an in-process `socket.socketpair()` (AF_UNIX). On this machine the peer occasionally never received the end-of-file after `shutdown(SHUT_WR)`, so the fake server stayed blocked in `recv`. |
| Kernel-level reason | **Unknown.** Only the symptom was reproduced. |
| Change 1 (production) | A failed stdin writer is recorded and handled at once (half-close first, full shutdown only as a fallback); an agent run whose prompt was not delivered completely is invalid. This does **not** address the lost-EOF case, where the writer succeeds. |
| Change 2 (test infrastructure) | The fake exec transport is now one loopback TCP connection per exec. |
| Status | Supported by experiments, **not proven**. Real Docker's behavior for the half-close path was not validated. |

## 2. The symptom

Over about fourteen full-suite runs, two failed with the same signature, and a third occurrence was captured in an instrumented loop:

| Occurrence | Test | Run time |
|---|---|---|
| Fresh-clone full run | `tests/test_proxy_lifecycle.py::test_keyboard_interrupt_while_the_agent_runs_removes_agent_gateway_and_proxy_in_order` | 463.85 s (normal about 165 to 185 s) |
| Local full run | `tests/test_agent_runner.py::test_claude_arguments_cannot_carry_secrets_or_the_task_and_the_container_is_still_removed[claude_args1]` | 474.33 s |
| Instrumented slice loop | `tests/test_proxy_lifecycle.py::test_a_proxy_that_never_becomes_ready_times_out_and_is_removed` | 579.19 s (heavily loaded machine) |

Common features: always the first exec that carries stdin (the seed injection into `/testbed`); the earlier no-stdin execs of the same run (route-table read, identity probe) had succeeded; no unhandled-thread-exception warning in the output. The failure message hid the real state (see section 6).

## 3. Cause

### 3.1 The transport and the exec lifecycle

`ExecSession` starts a reader thread and, when stdin is given, a writer thread. The writer calls `sendall(stdin)` and then `shutdown(SHUT_WR)` so the process sees end-of-file. The fake Docker (`tests/fake_docker.py`, `FakeExecApi._serve`) read stdin with `recv` until EOF, ran its handler and replied. The reader finishes only when the server closes its end. If the server never sees EOF, nothing completes until the deadline.

### 3.2 Captured hang

An instrumented loop (a pytest plugin kept outside the repository: it logged writer errors with errno and ran a watchdog that dumped state and all thread stacks for any exec unfinished for more than 40 s) caught a real hang on unmodified code:

- stdin length 40,960 bytes (the seed tar); the client's `sendall` and `shutdown(SHUT_WR)` had both returned within 0.2 ms;
- the writer thread had finished normally, with no error;
- the server thread had received all 40,960 bytes but recorded no EOF, and was blocked in `recv` (`tests/fake_docker.py`, the stdin loop of `_serve`);
- the reader thread was blocked waiting for server frames, and the main thread was waiting in `run_exec`.

So the loss was in the socket layer, between a completed `shutdown(SHUT_WR)` and the peer's `recv`.

### 3.3 Standalone reproduction (no project code)

A script performed the same sequence with plain Python sockets: a server thread `recv`s until EOF; the client has a writer thread (`sendall` of 40,960 bytes, then `shutdown(SHUT_WR)`) and optionally a reader thread blocked in `recv`. A failure is the server not seeing EOF within 5 s although `sendall` and `shutdown` returned.

| Transport | Reader thread | Exchanges | Lost EOF |
|---|---|---|---|
| AF_UNIX `socketpair` | yes | about 325,000 (four processes, 60 s each) | 33 |
| AF_UNIX `socketpair` | no | 255,951 | 25 |
| AF_UNIX `socketpair` (later baseline, single process, 60 s) | yes | 36,415 | 4 |

That is roughly 1 in 10,000 exchanges, with or without a concurrent reader, so the reader is not required to trigger it.

### 3.4 Reproduction through the real fake stack

The same loss rate appeared through the actual `FakeExecApi` plus `ExecSession`/`run_exec` code (4-process and 2-process runs, 8 threads each, 40,960-byte stdin, 5 s deadline):

| Transport | Execs | Timeouts (lost EOF) | Damaged result or stdin error |
|---|---|---|---|
| AF_UNIX (baseline, two processes) | 533,547 (267,226 and 266,321) | 29 (10 and 19) | 15 (6 and 9) |

### 3.5 Hypotheses examined and not supported

- **Mbuf or `ENOBUFS` exhaustion.** `netstat -m` showed 186 "requests for memory denied" since boot and swap was heavily used (about 4.3 of 5.1 GB), so a send failure under memory pressure looked plausible. An attempt to provoke `ENOBUFS` with 5,118 socketpairs each holding 8 KiB queued hit the per-process descriptor limit (`EMFILE`) first, and the writer in the captured hang had not failed. Not supported.
- **CPU load.** 3 further full runs, 30 loops of the fake-exec test slice at 10-way parallelism (machine load average 40 to 90), 25 isolated runs of the first failing test and 120 runs of it at 20-way parallelism produced no hang apart from the one in the slice loop. The stall is rare and not simply load.
- **Descriptor double-close or leaked threads.** No raw descriptor manipulation exists in the sources or tests, and daemon server threads use their own sockets.
- **Writer failure.** The captured hang had a clean writer (section 3.2).

An unexplained oddity: several concurrent runs reported identical pytest durations to the hundredth of a second (for example three runs at 247.36 s). It was not investigated further.

## 4. Change 1: production stdin-writer handling

Files: `src/aiep/container_exec.py`, `src/aiep/agent_runner.py`; tests in `tests/test_exec_stdin_failure.py` (and one persistence test in `tests/test_m2_run.py`).

**Before.** If `sendall` or `shutdown(SHUT_WR)` raised `OSError`, the old code recorded the error type and did nothing else. The process never received EOF, the reader waited for the whole deadline, and `run_checked` reported a bare "timed out".

**After.** When the writer fails on its own (not as a side effect of our own cancel or close):

1. `ExecSession` records `stdin_failure` (exception type and errno, for example `BrokenPipeError (EPIPE)`).
2. It half-closes the socket (`shutdown(SHUT_WR)`) so the process receives its end-of-input; the reader keeps draining output and the exit status is collected as usual.
3. Only if that half-close fails does it shut the whole socket down (`SHUT_RDWR`), which releases a reader that would otherwise wait for the deadline.
4. Errors from this cleanup are never recorded or raised: the writer's own error stays the recorded failure.
5. A write error that follows our own cancel or close is not counted as a stdin failure, so a genuine timeout keeps its ordinary report. A non-`OSError` in the writer also releases the reader and is still raised to the thread excepthook.
6. `run_checked` reports `stdin write failed (...)` ahead of a generic timeout.

**Agent runs.** The task travels on stdin only. If its delivery fails, the run is **invalid** (no patch extraction), `invalid_reason` reads "the task prompt was not delivered completely: stdin write failed (...); claude exec status ..., exit code ...", and the failure is available in memory as `AgentResult.stdin_failure`. The run-record schema is unchanged: the reason is carried by the existing `invalid_reason` field, which reaches `run.json` through `lifecycle_record` and the persisted artifacts (`tests/test_m2_run.py` follows that path to the file on disk and asserts there is no `stdin_failure` key).

**Unchanged.** The 300 s production deadline, all existing assertions, and the absence of retries.

**What it does not do.** It does not address the lost-EOF case, because there the writer succeeds. It was verified only against the fakes and injected socket failures; the half-close behavior has **not** been validated against real Docker (real Docker was not used for this work).

## 5. Change 2: the fake transport is loopback TCP

File: `tests/fake_docker.py` (`_socketpair`); tests in `tests/test_fake_docker_transport.py`.

- One connection per exec. Each pair gets its own listener bound to `127.0.0.1` on an ephemeral port (no fixed port, so no collision), closed as soon as the connection is accepted.
- Both ends block and have Nagle disabled. The fake Docker API and its semantics are otherwise unchanged.
- Cleanup: the listener is always closed; on any setup failure both ends that exist (including the accepted socket) are closed; the server end is closed by `_serve`, the client end by `ExecSession.close`.
- Server threads are tracked in a weak set (`live_server_threads()`), so tests can check that every one ends without reading thread internals.
- The loopback connection never leaves the host and the unit-suite network guard allows it.

Tests cover: loopback-only and ephemeral-port properties; no cross-wiring of concurrent pairs; closure after a failed connect and after a setup failure following `accept`; stdin sizes 0, 1, 8,191, 8,193, 65,536 and 3 MiB delivered intact with EOF; 1,200 concurrent injections; cancellation releasing the server thread and both sockets; a server exception ending the reader; and a failed exec still removing the container. Descriptor-count checks are bounded and settled (`<=`), not exact equalities, so unrelated daemon threads cannot make them flaky.

## 6. Experimental results for the TCP transport

### 6.1 Abort-on-close runs (`SO_LINGER` with a zero timeout)

For volume, both experiments below set `SO_LINGER` to a zero timeout on both ends so that closing aborts the connection and no sockets pile up in TIME_WAIT. In the standalone harness the close happened after the exchange had finished; in the real-stack harness the fake server's reply was empty, so no data could be discarded.

| Experiment | Exchanges | Lost EOF or timeouts | Other |
|---|---|---|---|
| Standalone loopback TCP, reader thread, 4 processes | 320,000 | 0 | 0 errors, 0 thread leaks, 0 descriptor growth |
| Standalone loopback TCP, no reader thread, 4 processes | 320,000 | 0 | same |
| Real fake + `ExecSession`, 4 processes x 8 threads | 699,825 (174,579; 174,767; 175,066; 175,413) | 0 | 0 damaged results, 0 errors, 0 leaked descriptors, 0 lingering server threads |

### 6.2 Ordinary-close run

With default closes at the highest rate (4 processes x 8 threads, over 1,000 exec attempts per second counting failed attempts, about 500 completed per second), sockets accumulate in TIME_WAIT and the ephemeral port range (about 16,000 ports, 49152 to 65535, with a TIME_WAIT of about 30 s) is exhausted:

- 102,433 execs completed (25,568; 25,638; 25,716; 25,511);
- 134,542 attempts failed with `EADDRNOTAVAIL`;
- **3 execs timed out** (2 and 1 in two processes). Whether those were lost EOFs or the effect of the overload was **not determined**.

A first standalone TCP control that created a new listener per iteration with default closes failed immediately with the same port exhaustion, which is why the abort-on-close harness above was used for volume.

### 6.3 The real suite's footprint

Measured with a connection counter during one full run (with the burst test at its former 2,000 execs): 3,874 fake exec connections in the whole suite, at most 2,007 per second and 2,148 in any 30 s window, and a peak of 2,665 TIME_WAIT sockets machine-wide (sampled every 2 s) against about 16,000 ports. The burst test has since been reduced to 1,200 execs. The suite is well inside the limit (the earlier summary rounded these to about 3,900 connections, at most about 2,000 per second and about 2,700 TIME_WAIT sockets), but do not run several suites at once: that would shrink the margin.

## 7. Statistical interpretation and limits

- Zero failures in 640,000 independent standalone trials gives an approximate one-sided 95% upper bound of about **1 in 213,000** per exchange (the rule of three, 3 / 640,000). This is a statistical approximation under an independence assumption, not a guarantee.
- The observed AF_UNIX rate (about 1 in 10,000) would make 640,000 clean exchanges astronomically unlikely, so TCP clearly behaved differently on this machine. A loss rate far below 1 in 100,000 cannot be excluded.
- The results do not cover real Docker, other operating systems or kernels, other payload sizes, other hardware, or the port-exhaustion regime.
- After the transport change the full suite passed several times in a row (three runs, then 3,447 passed locally and 3,441 passed in a fresh clone in the final verification). That is encouraging, but because the old failure rate was only a few percent per run it is not proof.
- Because the kernel cause is unknown, a different failure of the same kind could still occur in the fakes.

## 8. A separate unreproduced failure

`tests/test_lifecycle.py::test_gateway_violations_invalidate_the_run_and_nothing_is_extracted[tool_violation]` failed once during development in an earlier full-suite run. Its traceback was not saved and its cause is unknown, so it is **not** confirmed to be a flaky test, and it is not known to be related to the stall above. It has not recurred in 112 targeted runs (40 isolated, 64 under 8-way parallel load, 8 of the whole test file) or in the several complete full-suite runs made while preparing the release (the exact number was not recorded). If it recurs, save the full traceback first.

## 9. Reproducing the investigation

The diagnostic tooling (the pytest plugin, the standalone socket scripts and the stress harnesses) was kept outside the repository and is not committed. The conclusions above rest on the recorded counts; the unit tests that remain in the repository are `tests/test_fake_docker_transport.py`, `tests/test_exec_stdin_failure.py` and the persistence test in `tests/test_m2_run.py`. Do not run stress experiments of this size casually: they exhaust ephemeral ports and load the machine heavily, and the full suite should not be run concurrently with them.

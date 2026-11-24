# M2 Gate 0 notes: code for the first real Claude run (no Docker, no network, no spend)

> **Historical document (an early M2 stage); kept unchanged below.** It records the state before any real Docker validation or real API request: statements such as "nothing here has run against Docker, the gateway or Anthropic" describe that stage and are no longer current. Current capabilities, validation status and limitations are described in `README.md` and `docs/ARCHITECTURE.md`.

Status: implemented and fake-tested. **Nothing here has run against Docker, the gateway or Anthropic.** The Claude binary is not
downloaded, the gateway image is not pulled, and no key exists. Every Docker step is a later, separately approved gate.

## What exists now
* `config/workflow_prompts.json` and `aiep.experiment`: the shared task template, the Config A and Config B workflow lines, and their
  SHA-256s. `load_prompts` refuses a prompt whose text no longer matches its hash. A and B differ only in the workflow line (tested);
  `claude_args` takes no configuration label, so tools, model and flags cannot differ.
* `config/claude_cli.json`: the Claude CLI pin for 2.1.328 (`linux-x64`), **status `unpinned`, no SHA-256, no size**. They come from the
  bytes actually downloaded at Gate 1 and are never invented. `load_claude_cli` refuses anything but a complete pin.
* Component sizing (`agent_runner.COMPONENTS_TMPFS_SIZE`, `MAX_COMPONENT_FILE_BYTES`), see below.
* `run_agent` now verifies the owner of every injected tree (`expect_owner = the verified identity's uid:gid`, 1000:1000 here).
* `aiep.artifacts`: `runs/m2/<run_id>/` with exclusive creation, read-only files, a hash manifest written last, secrets redacted.
* `aiep.lease`, `aiep.limits`, `aiep.reaper` and `scripts/reap_aiep_containers.py`: a host-side run lease decides what is abandoned.
  The orchestrator takes `.cache/leases/<run_id>.lease` BEFORE any container exists, refreshes it every 30 s, and removes it at the end.
  The reaper (dry-run by default) never removes containers of a fresh lease however old they are; an expired lease frees them only after
  expiry (300 s) plus a grace period (default 900 s); a MISSING lease falls back to container age, which must exceed `MIN_TTL_S` =
  max wall clock (3600 s) + setup/teardown margin (3600 s) = 7200 s; an unreadable or implausible lease protects them (fail closed).
  A live orchestrator that cannot refresh its lease for 90 s (any cause, monotonic clock) stops its own run: the supervising loop sees
  `lease_lost` (an invalid-run stop reason), the existing lifecycle quiesces and removes the agent and gateway, artifacts are written
  with the termination reason and the first heartbeat error, and the lease is released (without hanging on a stuck heartbeat write).
  The wall-clock ceiling (3600 s) lives in `aiep.limits` and is enforced by `run_agent` and `m2_run.prepare`.
* `aiep.m2_run` and `scripts/run_m2_agent.py`: `prepare` (all local checks) and `execute` (existing `run_with_gateway`, always persists).
  The script is a PLAN unless `--execute` and `--approve-spend <budget>` are both given; it refuses while any pin is missing.

## Component sizing (calculated, not assumed)
The macOS arm64 build of 2.1.328 is 338 MiB (measured locally). The Linux x64 size is **unknown**, so the pin carries a ceiling.

| Quantity | MiB |
|---|---|
| Binary ceiling (hard refusal above it) | 400 |
| `/opt/aiep` tmpfs = ceiling + 16 slack | 416 |
| `/testbed` + `/opt/aiep` + `/tmp` + HOME + `/dev/shm` | 256 + 416 + 128 + 128 + 64 = **992** (aggregate cap 1024; `/testbed` was 192 and the total 928 until the measured Django seeds, see M3_EXPERIMENT_DESIGN) |
| Container memory limit | 2048 |
| Left for processes if every tmpfs were full | 1056 (was 1120) |
| Expected tmpfs fill (binary + 17 MiB seed) | about 357, leaving about 1.6 GiB |

The injection tar for a 400 MiB binary must fit `MAX_STDIN_BYTES` (512 MiB). `verify_archive` now hashes in 1 MiB blocks, so verifying a
large component no longer holds a second copy. The default per-file ceiling of `build_component_archive` is unchanged (256 MiB); the
Claude component passes its own explicit ceiling. On the host, building a 400 MiB component still holds roughly the file plus its tar.

## A finding fixed on the way
`AgentRunSpec.forbidden_secrets` (which holds the real API key) was part of the dataclass `repr`, so a log line or traceback that
printed the spec would have leaked the key. It is now `repr=False`, with a regression test.

## Still unverified (to be settled at the gates)
* Whether the Linux x64 binary runs under Rosetta (AVX), from a `noexec` HOME and `/tmp`, with a read-only root. (Gate 3)
* Every flag in `claude_args` was checked only against the local macOS 2.1.328 `--help`; `--safe-mode`, `--verbose` with `stream-json`,
  and the absence of `--max-turns` in the help text are unconfirmed against the Linux binary. (Gate 1/3)
* That Claude Code only ever sends the pinned model and a `max_tokens` within the gateway's 64,000 cap. (Gate 4)
* The gateway image pin (`python:3.12-slim` by digest and image ID) and the two Docker networks. (Gates 1 and 2)
* Grading: the hardened evaluator still cannot grade (wheelhouse unpopulated). The first run's patch goes to the M1 evaluator by a
  separate, approved step; `pytest-5227` has junk `PASS_TO_PASS` entries, so a run on it is a plumbing test, not a measurement.

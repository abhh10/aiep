# M2 Phase 2 design: injection-based agent and gateway containers

Status: **Step 0b (local software only) is implemented, remediated (see below) and unit-tested with
fakes. It is NOT Docker-validated: nothing here has run against Docker.** Every statement about Docker, Docker Desktop or the images below is either
marked *measured* (read from local image metadata) or *unverified* (to be proved in a separately
approved Docker step).

## Maturity legend (used throughout)

* **Implemented** = the code exists in `src/aiep`.
* **Fake-tested** = exercised by `tests/` against fakes (socket-pair exec, temporary directories, scripted agent). Nothing in this
  document is Docker-validated.
* **Docker-validated** = proved against a real daemon in an approval-gated step. **Nothing is, yet.**

## Remediation 2 (identity, environment, gateway image, lifecycle): implemented and fake-tested only

Decisions taken after a read-only compatibility audit. Local source and tests only; no Docker, pull, network or API use.

1. **Agent identity (implemented, fake-tested; the real values are UNVERIFIED).**
   * Evidence: the installed swebench builder source says images have a `nonroot` account (`chmod -R 777 /testbed  # So nonroot user
     can run tests`; a different builder runs `adduser ... nonroot`). **No local artifact records that account's uid, gid or home**
     (M1 inspect: `Config.User` is `root`). So no identity is invented: `config/agent_identity.json` has status `unverified` with
     null uid, gid and home, and `load_agent_identity` refuses it.
   * `AgentIdentity(name, uid, gid, home)` has no defaults. Container user (`"uid:gid"`), exec user, the owner of every tmpfs, the
     HOME tmpfs path and `$HOME` are all derived from it; `harden` and `verify_inspect` require exactly that. **No home
     convention is assumed:** `validate_home_path` accepts any explicit, absolute, normalised, plain path and refuses the root and
     system directories, the special filesystems, and anything that is, contains, or lies inside `/testbed`, `/opt/aiep`,
     `/opt/aiep-gw`, `/tmp` or `/opt/miniconda3`; per run, `check_home_overlaps` also refuses a home that overlaps a PATH
     directory or the pinned interpreter. The loader fails closed: a missing key, null, non-`verified` status or any invalid
     value raises. Whether Docker can mount a tmpfs at a home path that does not exist on the read-only root (for example
     `/nonexistent`) is unverified.
   * A numeric uid without a passwd entry is not treated as sufficient: before anything is injected, every run executes
     `IDENTITY_SCRIPT` (`id -u`, `id -g`, `id -un`, `getent passwd`) as that user and requires uid, gid, name and home to match the
     passwd entry. A missing entry or any mismatch makes the run invalid (`agent identity check failed`).
2. **One environment for both configurations (implemented, fake-tested).** `agent_env.experiment_env(identity)` is the only source of
   the environment of every exec and of the container: `PATH` is the measured post-`conda activate testbed` value
   (`/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/condabin:/opt/miniconda3/bin:/usr/local/sbin:...:/bin`), `CONDA_PREFIX`,
   `CONDA_DEFAULT_ENV=testbed`, `HOME`, `TZ`, `LC_ALL`. Its only parameter is the identity, so the configuration cannot influence it.
   `AgentRunSpec.config_label` (`A`/`B`) is validated and recorded only. Tests run A and B and assert identical create requests and
   identical binary/user/workdir/environment for every exec. The verification interpreter (`AgentRunSpec.python`) stays separately
   pinned and is never found through this PATH.
3. **Gateway image (design only).** LocalStack is no longer recommended (it declares an ENTRYPOINT the policy cannot clear, a VOLUME,
   is 1.7 GB, and is Python 3.13). The design is `python:3.12-slim` pinned by immutable repo digest and image ID. **No digest is
   recorded because nothing was pulled**: `config/gateway_image.json` is `unpinned` and `load_gateway_image` refuses it (and refuses
   any tag, short digest, or other entrypoint). The create request sets exactly `entrypoint=["/bin/sleep"]`,
   `command=["2147483647"]`, validated at create and re-checked at `inspect`, so no image entrypoint ever runs. Availability, the
   Python path and version (3.12 would match the host), the absence of VOLUMEs and `/bin/sleep` are all UNVERIFIED until an approved pull.
4. **Gateway lifecycle (implemented, fake-tested).** `lifecycle.GatewayManager` has explicit states
   `new -> created -> started -> verified -> ready -> stopped -> removed`; each step refuses to run out of order.
   * created: policy-checked create, egress network attached, `inspect` re-verified.
   * verified: code injected and matched to its manifest (including owners), only then `python -m aiep.gateway` is exec'd.
   * ready: a loopback TCP connect to the gateway's own listener succeeded while its process was alive.
   * `run_with_gateway` creates the agent only after `ready`. The manager's `stop_reason` is the agent supervisor's
     `gateway_stopped` callable: it reads the stop file, and a dead process or an unreadable channel becomes `gateway_died` /
     `gateway_unreachable`, which invalidate the run (the run is unmetered). Results are sticky and rate-limited.
   * In a `finally`: gateway processes are killed, the stop reason and ledger are read, then the container is killed and removed with
     its anonymous volumes. Order on success: the agent container is removed first, then the gateway. Setup failures return
     `gateway_failed` with no agent ever created; an unexpected agent-runner exception propagates only after the gateway is removed.
   * The agent runner now builds its result object before `create`, so nothing can fail between creating the container and the
     `try/finally` that removes it. Exec sockets are closed on every path.
   * **Not done here, on purpose:** creating or removing the two Docker networks (approval-gated step 2), and any real Docker call.

### Test hygiene (also from Remediation 2)

* `tests/conftest.py` refuses every non-loopback connection and name lookup, records the attempt, and fails the test at teardown even
  if the code under test swallowed the error. Real connectivity checks are separate: `@pytest.mark.network`, skipped unless
  `AIEP_ALLOW_NETWORK_TESTS=1`; none exist and nothing sets the variable.
* The old Hugging Face dataset-viewer test made live HTTPS requests, because `task_input.load_record` went through
  `datasets.load_dataset`, whose offline switches are read at import time. **Fixed:** `load_record` now reads the split's parquet
  file straight from the verified local snapshot with `pyarrow` and never imports or calls `datasets`/`huggingface_hub`. Order:
  config (40-hex revision, `<owner>/<name>` repo id, plain split) -> `refs/main` equals the pin and the snapshot exists ->
  exactly one `data/<split>-*.parquet`, linked into this repository's `blobs/` under a sha256 name that equals the file's content
  hash -> read -> exactly one matching record. Every failure is a `ManifestError`; there is no fallback and no environment variable
  to set. Tests (`tests/test_dataset_loading.py`) cover libraries imported before the call with stale "online" constants, a fresh
  interpreter importing neither library, missing cache, bad or unpinned revisions, tampered/dangling/substituted data, unreadable
  parquet and absent or duplicate records, each asserting no network attempt. The record is identical to the old path's
  (checked once for `pytest-dev__pytest-5227`). `pyarrow` is a transitive dependency (pinned in `requirements/host.lock`) and is not yet
  declared directly in `requirements/host.in`/`pyproject.toml`.
* Full suite: run directly (no pipe), `-W error::ResourceWarning`; pytest's own exit code is recorded separately from the shell's.

## Remediation 1 (interpreter, gateway integrity, ownership, warnings)

An audit of the first Step 0b implementation found four problems, now fixed in source and covered by fake-based
tests. None of the fixes has been exercised against Docker.

1. **Agent interpreter is explicit.** The first version ran `/usr/bin/python3` for in-container verification
   and assumed it existed. Recorded evidence for the SWE-bench instance image (`container_inspect.json` of an
   M1 run) is `PATH=/opt/miniconda3/bin:...`, i.e. a miniconda Python, and nothing records a
   `/usr/bin/python3`. `AgentRunSpec.python` is now a required field with no default. It is validated
   (`validate_interpreter_path`: absolute, normalised, plain characters, not on a writable or special
   filesystem), carried in `ExecContext.python`, and `harden_exec` runs **only that exact path**.
   `list_tree` uses it; there is no `PATH` lookup and no fallback, so a wrong path fails the run
   (exit 127) before the agent starts. `/opt/miniconda3/bin/python` is the expected value for the instance
   images but is **unverified** until a Docker step runs it.
2. **Gateway files have their own manifest and are verified before the gateway starts.** New module
   `gateway_runner.py`. The gateway container's PID 1 is now the same inert `sleep` keepalive as the agent's
   (the first version made `python -m aiep.gateway` PID 1, which would have started against an empty tmpfs
   before anything was injected). `inject_verify_start_gateway` verifies the injection tar, streams it into
   `/opt/aiep-gw`, lists the tree with the gateway image's interpreter, and compares it with the manifest
   (missing, extra, modified, mode, symlink-target, type and owner). Only if that is clean does it run
   `python -m aiep.gateway` by exec. The shipped files are fixed (`GATEWAY_FILES`: `gateway.py`, `ledger.py`,
   `pricing.py`, `__init__.py`, two config files), and a test checks that they import nothing outside that set.
3. **Gateway identity and ownership are explicit.** `GatewayContext.uid`/`gid` (and `python`, `image_id`) are
   required with no defaults (a missing one is a `TypeError`; root, booleans, floats, strings, `None` and negatives are refused)
   and define the single identity. They come from `config/gateway_image.json`, where `python`, `uid` and `gid` are all `null`
   (UNRESOLVED) until chosen and verified; `load_gateway_image` fails closed on any of them and `gateway_context(pin, ...)`
   builds the context only from a loaded pin. (Earlier revisions defaulted to 10001:10001 and `/usr/local/bin/python3`; removed.) `gateway_tmpfs` builds every gateway tmpfs with
   `uid=,gid=,mode=0700`; `harden` and `verify_inspect` refuse any tmpfs whose uid, gid or mode differ, is
   group/world-writable, or leaves the mode implicit. The container user must be exactly `uid:gid`, and every
   exec must use that same user. The in-container listing now reports each entry's uid and gid, and
   verification requires them to equal the gateway identity (a listing without owners fails).
4. **Test warnings.** The 175 warnings were all `ResourceWarning: unclosed socket`: 128 from exec sockets in
   `container_exec`/`agent_runner` (production: `ExecSession` closed its socket only on cancel; it now closes
   in `outcome()`, `cancel()`, and in the agent runner's `finally`) and 47 from the TLS fake upstream in
   `tests/fake_upstream.py` (test code: it swapped in a wrapped socket that `socketserver` never closes). The
   suite now emits none and nothing is suppressed. The gateway's own upstream connection also now closes the
   response and connection on every path and the raw socket on a failed handshake; no test showed a leak there,
   so that change is defensive.

## Decision

No derived agent image is built. The instance image is used unchanged and pinned by image ID. A
read-only-root container is created from it and populated by streaming tars over `docker exec`
stdin into tmpfs mounts.

## Why `exec` stdin and not `put_archive` / `docker cp`

* A tmpfs is mounted only when the container starts, so an archive uploaded to a created container
  lands underneath the mount and is hidden by it.
* Docker is believed to refuse archive upload into a container whose root filesystem is read-only
  (*unverified*). We do not depend on it: nothing in the design uses `put_archive`.
* `exec` runs inside the container, so it can write to the (writable) tmpfs mounts while the root
  stays read-only. This is also the only way the seed can end up in the writable `/testbed` mount
  and not beneath it.

## Container layout

| Mount | Type | Purpose | exec | Size (placeholder, `AgentRunSpec`) |
|---|---|---|---|---|
| `/` | read-only image root | the pinned instance image | n/a | n/a |
| `/testbed` | tmpfs, owned by the verified identity | the repository seed, the agent's workspace | yes | 192m (seed is 17.4 MiB measured) |
| `/opt/aiep` | tmpfs, owned by the verified identity | Claude Code binary and its files | yes | 256m (binary size unmeasured) |
| `/tmp`, `<identity.home>` (explicit, from the verified identity; unresolved) | tmpfs, owned by the identity, mode 0700 | scratch, Claude state | no | 128m each |

Policy caps: 512 MiB per mount and 1 GiB in total (all of it counts against the 2 GiB memory limit).
PID 1 is `/bin/sleep 2147483647`; the policy refuses any other command.

## Injection and verification sequence (orchestrator, outside the container)

1. `create` with policy-checked kwargs, then `inspect` and `verify_inspect` (limits, network, tmpfs,
   non-root user, pinned image ID, no mounts).
2. `start`; wait for `running`.
3. Stream `seed.tar` into `/testbed` and the component tar into `/opt/aiep`:
   `/bin/tar -x -p --no-same-owner -f - -C <dest>`, as the container's non-root user. Files end up
   owned by the agent user, modes exact (`-p`), setuid/setgid/sticky bits already stripped.
4. Run `<AgentRunSpec.python> -I -c VERIFY_SCRIPT <dest>` inside the container (explicit interpreter, no fallback). It lists every entry (type,
   mode, size, SHA-256, symlink target). The orchestrator compares that to the recorded manifest:
   missing, extra, special files, changed content, modes and links all fail the run as invalid
   before the agent exists.
5. Only then is Claude started.

Seed integrity chain: `get_archive` of `/testbed` from a pristine never-started container
(Phase 2, gated) -> `parse_seed_archive` (validation, normalisation, manifest, SHA-256 of the
injection tar) -> stored with `save_seed` (never overwritten) -> `load_seed(expected_sha256=...)`
re-verifies tar and manifest -> in-container verification against the same manifest.

## Ownership, permissions and read-only behaviour

* The tmpfs mounts are created with the verified identity's `uid=`/`gid=` and the container and exec user is that same
  `uid:gid` (see Remediation 2; the earlier `agent`/1001 placeholder no longer exists), so extraction needs no root
  and no capability (all are dropped).
* Because the extracting user is not root, ownership comes from the mount, not the tar (root-owned
  entries in the injection tar are written as `agent`). Modes come from the tar.
* Nothing writes outside the four tmpfs paths: the exec policy limits `tar` destinations to
  `/testbed`, `/opt/aiep` and (gateway) `/opt/aiep-gw` and `/tmp/aiep`.
* The agent can modify everything it owns, including the injected Claude binary. That does not
  matter after the start, and the orchestrator never executes anything from a tmpfs for its own
  purposes (extraction runs `/usr/bin/git` from the read-only root).
* *Unverified:* that tmpfs `uid`/`mode` options and non-root `tar -p` behave as designed on Docker
  Desktop, and that `HostConfig.Tmpfs` appears in `inspect` as the policy expects.

## Lifecycle after start

run -> supervise (wall clock, gateway stop state, container state) -> quiesce -> extract -> teardown.
The container stays alive after Claude exits because PID 1 never exits; the orchestrator removes it
only after the patch has been extracted (or the run was declared invalid).

* **Quiesce:** `kill -KILL -1` as the agent user kills every process it owns except PID 1 (which is
  protected inside its own PID namespace) and the exec's own shell; a second exec lists `/proc` and
  must show nothing else. One retry; survivors are recorded, not hidden. *Unverified* on Docker.
* **Outcomes:** normal finish, Claude failure, wall-clock timeout and budget exhaustion all extract
  the patch (partial work counts as the run's outcome and is labelled). A model, tool or metering
  violation makes the run **invalid** with no extraction. A container crash or OOM kill loses the
  tmpfs and therefore the patch; it is recorded (`container_died`, `oom_killed`).

## Task delivery

The issue text (or smoke prompt) is written to Claude's **stdin** over the exec socket and the write
half is closed. It is not in the create kwargs, any exec argv or environment, `docker inspect`, a
file, or the result record. Enforced by: argument length cap of 512 on every exec argument, a
fixed environment allowlist, a leak check of the task's first 64 characters against every exec
request, and tests that grep every recorded Docker call for a canary.
*Unverified:* that `claude -p` reads its prompt from stdin when no prompt argument is given (to be
checked against the real binary in the conformance step), that Docker honours the socket half-close
as EOF on stdin, and where Claude Code itself may write the prompt (its own session files under
the identity's HOME, a tmpfs inside the container).

## Patch extraction

Git runs only inside the container: `/usr/bin/git` with the hardened flags, `GIT_CONFIG_GLOBAL=/dev/null`,
`GIT_CONFIG_NOSYSTEM=1`, a fixed `PATH`, output cap and timeout. The diff base is the **seed's own
HEAD**, the synthetic "SWE-bench" commit recorded in the seed manifest (resolved from the archive
without running git). `AgentRunSpec` has no dataset `base_commit` field at all; a test asserts the
`git diff` argv contains the seed HEAD and the dataset base commit appears in no Docker call. A
`cat-file -e <seed HEAD>^{commit}` check precedes the diff. `__pycache__`, `*.pyc` and
`.pytest_cache` are excluded so generated files cannot poison the patch.

## Gateway image

**Design: `python:3.12-slim`, pinned by immutable digest. Not pulled, not inspected, not verified.** See Remediation 2 item 3 and
`config/gateway_image.json` (status `unpinned`). Rejected: `localstack/localstack:latest` (ENTRYPOINT that cannot be cleared, VOLUME,
1.7 GB application image, mutable tag, Python 3.13) and the SWE-bench instance image (carries the benchmark repository).
Pulling the image and recording its digest and ID is an approval-gated step; nothing may use the gateway until then.

Gateway code is injected the same way (exec tar into a `/opt/aiep-gw` tmpfs; no exec flag needed), but it is
verified against its own manifest, as the gateway's own uid:gid, and only then started by exec (see the
remediation section). The
gateway writes its stop reason to a file; the orchestrator reads it with `exec cat` (`read_stop_reason`),
and reads the ledger the same way. `python -m aiep.gateway` runs with `PYTHONPATH=/opt/aiep-gw/src` (set in the container environment at create).
If the expected Python 3.12 is confirmed, it matches the host's 3.12.2 used by every test here; the IP-validation tests should still
be re-run inside the image as a conformance check.

## Injection limitations

1. **Memory:** the Claude Code binary lives in a tmpfs and counts against the 2 GiB memory limit; its
   size is not yet known. If it does not fit, the fallback is a derived image (a build, separately approved).
2. **Throughput:** the seed and binary pass over a Docker API socket. Large repositories may be slow;
   untested.
3. **Tools required in the image:** `tar` (GNU), `sh`, and the explicitly configured Python interpreter
   (for the instance image, expected `/opt/miniconda3/bin/python` from its `PATH`; whether it is readable and
   executable by the non-root agent user is unverified). `tar`, `sh`, `id` and `getent` are unverified. The agent
   account (`nonroot`) and its uid, gid and home are unverified (see Remediation 2 item 1); the per-run identity probe
   will fail the run if they are not as configured. Claude Code's behaviour with this account is also unverified.
4. **Persistence:** tmpfs content dies with the container; there is no snapshotting.
5. **Docker behaviours unverified:** the gateway image's availability, digest, Python path/version and entrypoint handling, tmpfs
   `uid`/`gid`/`mode` appearing in `inspect` exactly as written (the policy compares option strings; Docker
   may normalise them), a non-root uid executing Python from tmpfs, `user="uid:gid"` for create and exec, hijacked exec sockets on Docker Desktop, stdin half-close,
   quiesce semantics, tmpfs option handling, anonymous-volume neutralisation.
6. **Tests are simulations:** the fake daemon uses real socket pairs, real `tar`/git/python on temporary
   directories and a scripted fake agent, but nothing exercises real Docker.

## Operations that remain approval-gated (none performed)

1. Create (not start) one pristine instance container and `get_archive` `/testbed` to produce the seed.
2. Create the two Docker networks (`--internal` agent network, egress network).
3. Pull `python:3.12-slim` and record its digest/ID; then create and start the gateway container, attach the egress network, inject and verify its files (the code path is `lifecycle.GatewayManager`; never run against Docker).
4. Create and start agent containers; inject, verify, run, quiesce, extract, remove.
5. Fake-agent isolation probes (fork bomb, memory over-allocation, runaway loop, internet and socket probes).
6. Real Claude Code against the gateway with a fake upstream; the one-time external download of the
   Claude Code binary.
7. Removing every AIEP-created container, network and volume.
8. Real API calls, benchmark evaluations (later phases).

## Disk and memory

Free space during Step 0 and 0b read-only work has been at or just below the 16 GiB start threshold
and moved by about 1 GiB with no Docker allocation change (`Docker.raw` 21.877 GiB, unchanged since
M1). The thresholds stay as set: 16 GiB to start, 12 GiB hard stop, no cleanup of unrelated data.
This design adds no image, so the expected AIEP disk growth is small (seed tar about 17 MB, the
Claude binary once downloaded, and per-run writable layers that are removed with the container).

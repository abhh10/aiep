# AIEP

Local research harness comparing Claude Code workflows on SWE-bench Lite tasks.
Status:
- **M1: complete and Docker-validated** (benchmark environment and evaluator; see docs/M1_RESULTS.md).
- **M2 Phase 1: committed**, fake-tested only (trusted components; see docs/M2_PHASE1_NOTES.md).
- **M2 Phase 2 Step 0b: committed (81e0e70), implemented and fake-tested. The agent and gateway lifecycle is NOT Docker-validated.**
  It covers the injection-based agent and gateway containers, the explicit agent identity and shared experiment environment, and
  the gateway lifecycle manager.
- **Verified by a read-only Docker conformance probe (2026-11-19):** the image/account facts only. In the existing SWE-bench image
  `sha256:c91459f86ed261ab81871b086d7ca2d9fe16abd2ddf374bf172edd5d991fd28b` the account is `nonroot`, uid 1000, gid 1000, home
  `/home/nonroot`, shell `/bin/bash`; the verification interpreter is `/opt/miniconda3/bin/python` (3.11.5) and the testbed
  interpreter is `/opt/miniconda3/envs/testbed/bin/python` (3.9.20). This is recorded in `config/agent_identity.json`.
- **Still unverified:** Claude Code installation and execution in the agent container; the read-only root filesystem and tmpfs
  behaviour (including tmpfs ownership and permissions); exec stdin half-close; agent quiescence; the full real Docker agent
  lifecycle; the gateway image (not pulled or pinned); and integration of the identity configuration into the production runner
  (it is loadable, but nothing in the runner loads it yet). See docs/M2_PHASE2_DESIGN.md.

See the architecture proposal for the milestone plan.

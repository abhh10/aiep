# AIEP

Local research harness comparing Claude Code workflows on SWE-bench Lite tasks.
Status:
- **M1: complete and Docker-validated** (benchmark environment and evaluator; see docs/M1_RESULTS.md).
- **M2 Phase 1: committed**, fake-tested only (trusted components; see docs/M2_PHASE1_NOTES.md).
- **M2 Phase 2 Step 0b: implemented and fake-tested, NOT Docker-validated, not committed.** It covers the injection-based agent and
  gateway containers, the explicit agent identity and shared experiment environment, and the gateway lifecycle manager. The agent
  identity, the gateway image pin, Claude Code's behaviour and every Docker behaviour it relies on are still unverified
  (see docs/M2_PHASE2_DESIGN.md).

See the architecture proposal for the milestone plan.

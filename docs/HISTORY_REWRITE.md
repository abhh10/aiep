# History rewrite and commit mapping

Before this repository was published, its Git history was rewritten once to remove private working notes and personal details (local paths, account figures, a conversation identifier and similar). Authorship, dates, commit order, commit messages (apart from two sentences, below) and all source code, configuration, tests, results and documentation were kept. The rewrite changed every commit hash after the first, so hashes quoted in older documents, in the frozen research report (`docs/research/E1_STAGE1_REPORT.md`) and in the evidence bundle refer to the **original, unpublished history** and do not exist in this repository.

## What changed

- Five private working-note files were removed from every commit. One commit that contained only those files became empty and was dropped, so the history has 31 commits (the original had 32).
- Account-balance figures, a machine-operation detail and references to the private notes were removed from earlier versions of `docs/PROJECT_STATUS.md`.
- `scripts/m1_eval.sh` had a hard-coded checkout path; it now derives the path from its own location (in the M1 commit and every later one). Nothing else in it changed.
- Two commit messages had one sentence each reworded (a list of note file names; a billing-check clause).

## The experiment's execution commit

E1 Stage 1 was run at the original harness commit `28e758e5f3ccf4598fe843fe4a3abbed3628eb83`, which every run record and the research report cite. Its equivalent in this repository's history is `087d65da7431d24ce14d882ddc28ae7354b7a3a3`. The two trees differ only by the removed notes and the sanitized lines of `docs/PROJECT_STATUS.md` and `scripts/m1_eval.sh`; all code, configuration and tests that the experiment executed are identical. The mapping is recorded in `config/history_rewrite.json`; the evidence provenance (schema 3) records both commits, and `scripts/verify_e1_stage1_evidence.py` checks that the public one exists, that the builder commit descends from it, and that the committed mapping agrees. Run records are never modified: the original commit stays in them as the experiment's execution identity.

## Commit map (original to published)

| Original | Published | Subject |
|---|---|---|
| `deedae0` | `deedae0` | M0: minimal project skeleton on main |
| `9c84d4d` | `5bae33a` | M1: benchmark environment and evaluator validation |
| `51a9d52` | `f9b603e` | M2 Phase 1: trusted components for secure agent execution |
| `81e0e70` | `defc682` | M2 Phase 2: implement isolated agent and gateway lifecycle |
| `d4e73c7` | `4347665` | M2: record verified agent identity and harden config loader |
| `bc43b38` | `6ce2240` | M2: document Docker identity conformance findings |
| `36305da` | `6e75bf2` | M2: enforce explicit Docker shared-memory policy |
| `c93e380` | `1d5c6af` | M2: egress architecture, metering gateway, isolated agent integration and evaluator handoff foundations |
| `0e4ea7d` | `3e8ac43` | M3: experiment harness (v1 preserved, v2 current), task selection, grading, analysis and M3.7 readiness fixes |
| `0ef1100` | `989151a` | Add digest-pinned SWE-bench image preparation |
| `1c1fe50` | `dde4a2a` | Prepare and pin SymPy SWE-bench task |
| `4982e9d` | `6213a4f` | Guard SWE-bench evaluation against image acquisition |
| `7182916` | `8cc9f85` | Add isolated M2 calibration controls |
| `36b8c8b` | `89837db` | Fix Claude CLI cost estimate parsing |
| `c9da9fe` | `b333cb4` | Add Claude-reported cost to experiment results |
| `d75eb50` | `fe36eea` | Add explicit workflow prompt selection to M2 runs |
| `daa31e8` | `59ac9e1` | Improve transcript metrics and test-fix cycle detection |
| `673f9af` | `05bbd56` | Add exploratory E1 staged definition and pair-atomic budget control |
| `e1d7c71` | `6b942b3` | Update AIEP project status and operating instructions |
| `3b1057a` | `6a71c93` | Set E1 exploratory experiment budget and update tests |
| `db7ad06` | `8b24499` | Update AIEP handoff for budgeted E1 draft |
| `f1f3b14` | `0341054` | Add pinned images and enforce local image readiness |
| `64bfce8` | `7e8e523` | Update AIEP handoff for four pinned Stage 1 images |
| `57d10b7` | `a4ee563` | Separate seed and component file size limits |
| `4a57b36` | `2b06fa3` | Add deterministic preparation-failure replacements |
| `2832cf2` | `bc6d603` | Raise seed archive and testbed tmpfs limits |
| `dcf4bf1` | `d0bce92` | Prepare and verify E1 Stage 1 task artifacts |
| `11324d1` | `cef67dc` | Update prepared-task test expectations |
| `e802b31` | `86d602e` | Refresh E1 preparation and project handoff docs |
| `bc9de7a` | `7dab228` | Lock E1 exploratory experiment and clarify stage plan |
| `28e758e` | `087d65d` | Update documentation for locked E1 experiment |
| `23a59a0` | dropped | (contained only the private working notes) |

# AIEP project status (2026-11-23, updated after the E1 implementation commit)

Full detail: a private working note. This page is the one-minute summary.

## START HERE
1. `git rev-parse HEAD` (expect `673f9aff9abbf84addf736a40141f3fa1293ac00`, or a documentation commit on top of it) and `git status --short -uall` (clean once the documentation commit is made; before that, these docs untracked and a private working note modified).
2. Ask the user to re-check the Anthropic Console (credit and spend). Do not trust the numbers in these docs.
3. Do not call the API, run Docker/evaluator/agents, lock E1, set budgets, stage, commit or push without explicit approval.

## Git state
- HEAD `673f9aff9abbf84addf736a40141f3fa1293ac00` on `main` ("Add exploratory E1 staged definition and pair-atomic budget control"), on top of `daa31e81c1210457b8ab6ca5a300d7779efcf29c`. No remote; nothing ever pushed; nothing staged.
- The eight E1 implementation files are committed: `scripts/m3_experiment.py`, `scripts/run_m3_pilot.py`, `src/aiep/analysis.py`, `src/aiep/experiment_build.py`, `src/aiep/experiment_def.py`, `src/aiep/m3_batch.py`,
  `config/experiments/e1-exploratory-1.json`, `tests/test_e1_exploratory.py`.
- Latest full suite: **3165 passed, 1 skipped** (supersedes the earlier 3151). `git diff --check` clean.

## E1
- `config/experiments/e1-exploratory-1.json`: **draft**, valid, `definition_sha256` `d5a3bae109e62a9c69d92fbcf92db1bd00842cd54eceab862ce3ce94b51d672f`;
  `budget_usd_per_run` = **null**, `max_total_computed_usd` = **null**, overshoot allowance "0.30" (PROVISIONAL, not a guaranteed maximum). **Not locked, not funded, not executable** (driver blockers, `require_locked` and validation all refuse).
- Stage 1 (required): sympy-18621 (A first), django-13933 (B first), django-15851 (B first), sympy-22714 (A first). Stage 2 (conditional; runs only with `--stages 1 2`): sphinx-10325 (A first), requests-863 (B first, needs a recorded compatibility decision before any run).
- Exploratory feasibility study, not statistically powered; no p-values or intervals; parser v2 pinned; the first two scored pairs get manual transcript review.
- Budget rule: `max_total_computed_usd >= 4 pairs x 2 arms x (per-run gateway budget + 0.30)` (validated). The reservation for a pair is checked against the remaining ledger-cost cap before either arm starts.
- The builder aliasing fix (deep copy of the v2 input and of `TEST_COMMAND_SPEC`) is implemented, committed and covered by 14 regression tests; the E1 file and hash are unchanged by it.

## Done
M0-M2, M3 harness, transcript parser v2, the E1 definition, batch, analysis, driver `--stages` and review fixes (all committed). Two authorized real-API **calibration** runs on sympy-18621 (A: $0.040225, 5 requests, patch later resolved when graded;
B: $0.0503555, 7 requests, 594-byte patch, not graded); they are not A/B observations and are never scored. **No scored experiment run has ever been made.**

## Blockers
- Fresh billing verification and the per-run budget / cap decision (both null).
- Stage 1 images, digests, seeds, identity pins and gold/no-op controls not prepared (only sympy-18621 is pinned); Django seed archive sizes borderline against the 128 MiB limit.
- requests-863 compatibility decision not recorded (Python call; no CLI).
- The documentation update (these two files and a private working note) is not committed yet.

## Next action
Commit the documentation update separately (needs approval), then the Console re-check, then the budget decision, then step-by-step approved preparation. Ordered plan: a private working note section 7.

## Hard rules (summary)
$5 ceiling and $0.50 reserve; no API without explicit spend approval; no retries; no commits/pushes without approval; never modify calibration artifacts, v1/v2 definitions or prompts; stop on incomplete accounting,
financial uncertainty, hidden-data leakage or integrity failure; never expose the API key; touch only AIEP's Docker resources. Full policy: a private working note (Working agreements).

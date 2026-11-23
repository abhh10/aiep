# AIEP project status (2026-11-23, updated after the E1 budget commit `3b1057a`)

Full detail: a private working note. This page is the one-minute summary.

## START HERE
1. `git rev-parse HEAD` (expect `3b1057acf8ae73390588421a81fb186619aafb3e`, or a documentation commit on top of it) and `git status --short -uall` (clean once the documentation commit is made; before that, a private working note and these two docs modified).
2. Ask the user to re-check the Anthropic Console (credit and spend). Do not trust the numbers in these docs. This check is REQUIRED before locking or any execution.
3. Do not call the API, run Docker/evaluator/agents, lock E1, change budgets, stage, commit or push without explicit approval.

## Git state
- HEAD `3b1057acf8ae73390588421a81fb186619aafb3e` on `main` ("Set E1 exploratory experiment budget and update tests"). Parents: `e1d7c719602875875c7d9579356d8670b239e24d` (documentation update), `673f9aff9abbf84addf736a40141f3fa1293ac00`
  (E1 implementation), `daa31e81c1210457b8ab6ca5a300d7779efcf29c`. No remote; nothing ever pushed; nothing staged.
- Latest full suite: **3166 passed, 1 skipped** (supersedes 3165 and 3151). `git diff --check` clean.
- **Unresolved, unreproduced test failure:** `tests/test_lifecycle.py::test_gateway_violations_invalidate_the_run_and_nothing_is_extracted[tool_violation]` failed once in a full-suite run; it passed 30/30 in isolation, 25/25 under load and in every later full run.
  Its original traceback was not captured. It is NOT a confirmed flake and no lifecycle code was changed; if it recurs, save the full traceback first.

## E1
- `config/experiments/e1-exploratory-1.json`: **draft, NOT locked**, valid, `definition_sha256` `434a94a22c12945b50764b47d91df1edca3d02a14569bee49ebe87385130e2a0` (file sha256 `97d0c5688508e2ba9a87bd39d479d1dc8cfd87af7134674149f19e90ed06f8b5`).
- Budgets: `budget_usd_per_run` = **$0.18**, `max_total_computed_usd` = **$3.84**, overshoot allowance **$0.30 per run** (PROVISIONAL, not a guaranteed maximum). Stage 1 reservation = 4 pairs x 2 runs x ($0.18 + $0.30) = **$3.84**, exactly the cap: no reservation slack.
  **The budgets do not authorize execution**: the definition must be locked, the user's fresh Console billing check done, and `--approve-spend` must equal the cap. `require_locked`, the driver blockers and validation refuse a draft.
- Stage 1 (required): sympy-18621 (A first), django-13933 (B first), django-15851 (B first), sympy-22714 (A first). Stage 2 (conditional; runs only with `--stages 1 2`, no guaranteed funding): sphinx-10325 (A first), requests-863 (B first, needs a recorded compatibility decision before any run).
- Exploratory feasibility study, not statistically powered; no p-values or intervals; parser v2 pinned; the first two scored pairs get manual transcript review. The pair reservation is checked against the remaining ledger-cost cap before either arm starts.
- The builder's default template still produces null budgets; the committed file is that template plus the approved budgets, note and rehash.

## Done
M0-M2, M3 harness, transcript parser v2, the E1 definition, batch, analysis, driver `--stages`, review fixes, the builder aliasing fix and the E1 budgets (all committed). **Two authorized real-API calibration runs** on sympy-18621 (A: $0.040225, 5 requests, patch later resolved when graded;
B: $0.0503555, 7 requests, 594-byte patch, not graded); they are not A/B observations and are never scored. **Zero scored E1 runs; no scored experiment run has ever been made.**

## Blockers
- Fresh Console billing verification (required before locking or execution); the $0.50 prepaid-credit reserve is never spent. No provider balance is recorded in this repository. A fresh Anthropic Console check is mandatory before locking or executing E1, and the $0.50 protected prepaid-credit reserve remains in effect.
- E1 is not locked and not executable.
- Stage 1 images, digests, seeds, identity pins and gold/no-op controls not prepared (only sympy-18621 is pinned); Django seed archive sizes borderline against the 128 MiB limit.
- requests-863 compatibility decision not recorded (Python call; no CLI).
- This documentation refresh is not committed yet.

## Next action
Commit the documentation refresh separately (needs approval), then the user's Console re-check, then step-by-step approved preparation, controls, locking and spend approval. Ordered plan: a private working note section 7.

## Hard rules (summary)
$5 ceiling and $0.50 reserve; no API without explicit spend approval; no retries; no commits/pushes without approval; never modify calibration artifacts, v1/v2 definitions or prompts; stop on incomplete accounting,
financial uncertainty, hidden-data leakage or integrity failure; never expose the API key; touch only AIEP's Docker resources. Full policy: a private working note (Working agreements).

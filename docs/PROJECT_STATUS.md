# AIEP project status (2026-11-23, updated after the pinned-images commit `f1f3b14`)

Full detail: a private working note. This page is the one-minute summary.

## START HERE
1. `git rev-parse HEAD` (expect `f1f3b14ae4a5636dd4d29007224166905b9e17f3`, or a documentation commit on top of it) and `git status --short -uall` (clean once the documentation commit is made; before that, a private working note and these two docs modified).
2. Ask the user to re-check the Anthropic Console (credit and spend). Do not trust the numbers in these docs. This check is REQUIRED before locking or any execution.
3. Do not call the API, run Docker/evaluator/agents, lock E1, change budgets, stage, commit or push without explicit approval.

## Git state
- HEAD `f1f3b14ae4a5636dd4d29007224166905b9e17f3` on `main` ("Add pinned images and enforce local image readiness"). It committed five files: `config/image_digests.json`, `src/aiep/m3_batch.py`, `src/aiep/m3_steps.py`,
  `tests/test_image_digests.py`, `tests/test_task_image_readiness.py`. Preceding commits: `db7ad060ec38e0b4f13047b7f4d8cd4107306be1` (handoff update), `3b1057acf8ae73390588421a81fb186619aafb3e` (E1 budgets),
  `e1d7c719602875875c7d9579356d8670b239e24d` (first documentation refresh), `673f9aff9abbf84addf736a40141f3fa1293ac00` (E1 implementation), `daa31e81c1210457b8ab6ca5a300d7779efcf29c`. No remote; nothing ever pushed; nothing staged.
- Latest full suite: **3177 passed, 1 skipped, 0 failed** (supersedes 3166, 3165 and 3151). `git diff --check` clean.
- **Unresolved, unreproduced test failure:** `tests/test_lifecycle.py::test_gateway_violations_invalidate_the_run_and_nothing_is_extracted[tool_violation]` failed once in a full-suite run; it passed 30/30 in isolation, 25/25 under load and in every later full run.
  Its original traceback was not captured. It is NOT a confirmed flake and no lifecycle code was changed; if it recurs, save the full traceback first.

## E1
- `config/experiments/e1-exploratory-1.json`: **draft, NOT locked**, valid, `definition_sha256` `434a94a22c12945b50764b47d91df1edca3d02a14569bee49ebe87385130e2a0` (file sha256 `97d0c5688508e2ba9a87bd39d479d1dc8cfd87af7134674149f19e90ed06f8b5`).
- Budgets: `budget_usd_per_run` = **$0.18**, `max_total_computed_usd` = **$3.84**, overshoot allowance **$0.30 per run** (PROVISIONAL, not a guaranteed maximum). Stage 1 reservation = 4 pairs x 2 runs x ($0.18 + $0.30) = **$3.84**, exactly the cap: no reservation slack.
  **The budgets do not authorize execution**: the definition must be locked, the user's fresh Console billing check done, and `--approve-spend` must equal the cap. `require_locked`, the driver blockers and validation refuse a draft.
- Stage 1 (required): sympy-18621 (A first), django-13933 (B first), django-15851 (B first), sympy-22714 (A first). Stage 2 (conditional; runs only with `--stages 1 2`, no guaranteed funding): sphinx-10325 (A first), requests-863 (B first, needs a recorded compatibility decision before any run).
- Exploratory feasibility study, not statistically powered; no p-values or intervals; parser v2 pinned; the first two scored pairs get manual transcript review. The pair reservation is checked against the remaining ledger-cost cap before either arm starts.
- The builder's default template still produces null budgets; the committed file is that template plus the approved budgets, note and rehash.

## Images (verified 2026-11-23; Docker and disk figures are the last measured values, not guarantees)
- `config/image_digests.json` holds four verified immutable digest records: sympy-18621 (the earlier record, unchanged), django-13933, django-15851, sympy-22714. The three new records come from anonymous, read-only Docker Hub registry MANIFEST requests
  (index and linux/amd64 manifest only; no image blobs downloaded; a metadata lookup, not a pull). Digests: a private working note section 4.
- Docker Desktop is running. The sympy-18621 image is present (ID equals its pinned index digest) and reusable, and its seed is verified. The other three images are NOT local; their seeds and identity manifests do not exist. **Image preparation has not started; no pull or build has occurred.**
- `RealSteps.task_prepared` now requires complete pins AND the exact pinned image ID present locally; a missing image, a mismatched ID or a Docker lookup failure means not prepared, and a missing image never reaches a run without explicit pull approval.
- Disk: last measured free space 26.11 GiB. Verified incremental compressed download for the three missing images: 1,169,837,021 B (1.089 GiB) after layer-digest sharing; their unpacked storage is unmeasured. The 16 GiB start gate and 12 GiB hard stop are unchanged; recheck disk before each pull.

## Done
M0-M2, M3 harness, transcript parser v2, the E1 definition, batch, analysis, driver `--stages`, review fixes, the builder aliasing fix, the E1 budgets, the three new digest records and the image-readiness fix (all committed). **Two authorized real-API calibration runs** on sympy-18621 (A: $0.040225, 5 requests, patch later resolved when graded;
B: $0.0503555, 7 requests, 594-byte patch, not graded); they are not A/B observations and are never scored. **Zero scored E1 runs; no scored experiment run has ever been made.**

## Blockers
- Fresh Console billing verification (required before locking or execution); the $0.50 prepaid-credit reserve is never spent. No provider balance is recorded in this repository. A fresh Anthropic Console check is mandatory before locking or executing E1, and the $0.50 protected prepaid-credit reserve remains in effect.
- E1 is not locked and not executable.
- Three Stage 1 images (django-13933, django-15851, sympy-22714) are not local: pulls, evaluator-image pins, seeds, identity manifests and gold/no-op controls are all not done (only sympy-18621 is prepared). Django seed archive sizes are borderline against the 128 MiB limit and sympy-22714's is unknown; unpacked image sizes are unmeasured.
- requests-863 compatibility decision not recorded (Python call; no CLI).
- This documentation refresh is not committed yet.

## Next action
Commit the documentation refresh separately (needs approval); then prepare the three missing images one at a time, each pull explicitly approved and disk rechecked before each (not started); then prepare and verify their seeds and identity manifests, then gold/no-op controls (none done); then the user's Console re-check, locking and spend approval. Nothing in this list is complete. Ordered plan: a private working note section 7.

## Hard rules (summary)
$5 ceiling and $0.50 reserve; no API without explicit spend approval; no retries; no commits/pushes without approval; never modify calibration artifacts, v1/v2 definitions or prompts; stop on incomplete accounting,
financial uncertainty, hidden-data leakage or integrity failure; never expose the API key; touch only AIEP's Docker resources. Full policy: a private working note (Working agreements).

# AIEP project status (2026-11-23, updated after the Stage 1 preparation commit `dcf4bf1`)

Full detail: a private working note. This page is the one-minute summary.

## START HERE
1. `git rev-parse HEAD` and `git status --short -uall`. Expect a clean tree. Expected history, newest first: this documentation refresh, `11324d1` (test expectations), `dcf4bf1beee6b2d05a7e9827fd397008af970efa` (Stage 1 preparation), `2832cf2f84bbfaccf3f7c6a33f6b335584b218d4`.
2. Ask the user to re-check the Anthropic Console before locking or any execution (the figures below are their last manual observation and go stale on any spend).
3. Do not call the API, run Docker/evaluator/agents, lock E1, change budgets, stage, commit or push without explicit approval.

## Git state
- Commits since the E1 budgets (oldest first): `f1f3b14` pinned image digests and image readiness; `57d10b7` seed-file limit 96 MiB separated from component ceilings; `4a57b36` preparation-gate replacement mechanism (no replacement has occurred);
  `2832cf2` seed archive cap 160 MiB and `/testbed` tmpfs 256 MiB (aggregate tmpfs 992 of 1,024 MiB); `dcf4bf1` the three newly prepared Stage 1 tasks (evaluator pins, seed pins, three identity records); `11324d1` updated the prepared-task test expectation. No remote; nothing ever pushed.
- Latest full suite: **3264 passed, 1 skipped, 0 failed** (about 2.5 minutes). `git diff --check` clean.
- **Unresolved, unreproduced test failure:** `tests/test_lifecycle.py::test_gateway_violations_invalidate_the_run_and_nothing_is_extracted[tool_violation]` failed once in an earlier full-suite run and has not reproduced since. Its traceback was not captured; it is NOT a confirmed flake. If it recurs, save the full traceback first.

## E1
- `config/experiments/e1-exploratory-1.json`: **draft, NOT locked**, valid, `definition_sha256` `434a94a22c12945b50764b47d91df1edca3d02a14569bee49ebe87385130e2a0`. Unchanged by all preparation work.
- Budgets: $0.18 per run, `max_total_computed_usd` **$3.84**, overshoot allowance $0.30 per run (PROVISIONAL). Stage 1 reservation = 4 pairs x 2 runs x ($0.18 + $0.30) = $3.84, exactly the cap, no slack.
  **The budgets do not authorize execution**: the definition must be locked, the Console re-checked, and `--approve-spend` must equal the cap.
- Stage 1 (required, four pairs, eight scored runs): sympy-18621 (A first), django-13933 (B first), django-15851 (B first), sympy-22714 (A first). Stage 2 (sphinx-10325, requests-863) is conditional, unauthorized and not prepared; it runs only with `--stages 1 2` and a recorded requests-863 compatibility decision.
- Exploratory feasibility study, not statistically powered; descriptive only.

## Stage 1 preparation (verified 2026-11-23)
- **All four tasks are prepared and runnable** (`pin_status` image, seed and identity true): evaluator image pinned by ID (equal to its registered index digest), seed sha256 pinned with lineage equal to the dataset base commit, agent identity verified.
  Seed archives (measured): sympy-18621 99.45 MiB, sympy-22714 112.77, django-13933 133.79, django-15851 142.57 (largest page-rounded `/testbed` footprint 160.39 MiB). Limits: archive 160 MiB, file 96 MiB, content 256 MiB, 200,000 entries, `/testbed` tmpfs 256 MiB.
- **All eight gold/no-op controls are valid**: gold resolves with every FAIL_TO_PASS passing and every PASS_TO_PASS maintained; no-op leaves every FAIL_TO_PASS failing. Runs: `runs/m1/m3ctl-*` (sympy-18621) and `runs/m1/e1ctl-*` (the other three; the `e1ctl-` prefix is a naming deviation, nothing renamed). The batch re-runs both controls before each task's agent runs.
- Three evaluator images (sympy-22714, django-13933, django-15851) were pulled by digest and carry local `:latest` tags added with `docker tag` (Docker state only, not in git). Images, volumes and the three unrelated Nuvora containers are preserved.
- Free disk last measured **26.32 GiB** (after the approved removal of 8.2 GB of another project's unused Docker build cache; 10.34 GiB returned to the host). 16 GiB start gate and 12 GiB hard stop unchanged.

## Console figures (user-provided, manually verified; NOT independently checkable; re-check before locking)
Provider billing balances are deliberately not recorded here; verify them in the provider console before any execution. Spend computed from the calibration ledgers is recorded elsewhere in this document.

## Done
M0-M2, M3 harness, transcript parser v2, the E1 definition, batch, analysis, budgets, image digests, the replacement mechanism, the limit changes and Stage 1 preparation with controls (all committed). **Two authorized real-API calibration runs** on sympy-18621 (A: $0.040225; B: $0.0503555);
they are not A/B observations and are never scored. **Zero scored E1 runs; no scored experiment run has ever been made.**

## Open items
- E1 is not locked and not executable. Locking, the `--approve-spend 3.84` approval, `--approve-pulls`, `--approve-controls`, the key and the pinned Claude binary are all still needed.
- The agent container has not been run in real Docker with the 256 MiB tmpfs and the largest seed (offline tests only). An optional no-API dry run with the existing mock Claude is NOT authorized yet.
- Driver cosmetics: the plan-mode blocker text still says "budgets are null until an approved real-API calibration".
- requests-863 compatibility decision not recorded (Python call; no CLI).

## Hard rules (summary)
$5 ceiling and $0.50 reserve; no API without explicit spend approval; no retries; no commits/pushes without approval; never modify calibration artifacts, v1/v2 definitions or prompts; stop on incomplete accounting,
financial uncertainty, hidden-data leakage or integrity failure; never expose the API key; touch only AIEP's Docker resources. Full policy: a private working note (Working agreements).

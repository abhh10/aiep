# E1 Stage 1 evidence bundle

A sanitized, committed subset of the evidence behind `docs/research/E1_STAGE1_REPORT.md` (experiment `e1-exploratory-1`, definition sha256 `9601b345da705b7e8bbdba251b902f4c7637482fd2e87e732dcba0dc36875eff`).
It lets someone who clones this repository check the report's quantitative claims **offline**, without Docker, credentials, network access or the private run directories.

## What is in it

| Path | What | Treatment |
|---|---|---|
| `batch/state.json` | batch state, event timeline, gold and no-op control outcomes, reservations, cumulative cost | copied byte for byte after a key-by-key review and a pattern scan |
| `batch/results.jsonl` | the eight result rows | the same rows with one field removed (`run_dir`, an absolute path on the producing machine) |
| `runs/<run_id>/gateway_ledger.jsonl` | the gateway's per-request ledger (timestamps, token counts, computed cost, status) | copied byte for byte |
| `runs/<run_id>/artifacts_manifest.json` | the file names, sizes and sha256 recorded when the run finished | copied byte for byte |
| `runs/<run_id>/evidence.json` | run, accounting, handoff, grading and transcript summaries | derived from private files, with an explicit keep list and finite value sets |
| `PROVENANCE.json` | for every file: how it was produced, its private sources and their sha256; the builder (its sha256 and the commit that contains it); and, separately, the commit at which the experiment ran | generated |
| `MANIFEST.sha256` | sha256 of every other file in the bundle | generated |

The eight runs, in execution order:

| Order | Run id | Task | Arm |
|---|---|---|---|
| 1 | `m2-sympy__sympy-18621-A-20261123T171501Z` | sympy__sympy-18621 | A |
| 2 | `m2-sympy__sympy-18621-B-20261123T172624Z` | sympy__sympy-18621 | B |
| 3 | `m2-django__django-13933-B-20261123T172904Z` | django__django-13933 | B |
| 4 | `m2-django__django-13933-A-20261123T174038Z` | django__django-13933 | A |
| 5 | `m2-django__django-15851-B-20261123T174307Z` | django__django-15851 | B |
| 6 | `m2-django__django-15851-A-20261123T175434Z` | django__django-15851 | A |
| 7 | `m2-sympy__sympy-22714-A-20261123T175656Z` | sympy__sympy-22714 | A |
| 8 | `m2-sympy__sympy-22714-B-20261123T180803Z` | sympy__sympy-22714 | B |

## Verify it

```bash
python scripts/verify_e1_stage1_evidence.py            # from the repository root; standard library plus this repository's own `src/aiep`
```

It exits 0 only if every check passes. It needs no private file.

## What the verifier establishes, in three tiers

**1. Recomputed from the committed data** (the ledgers, `config/model_pricing.json`, the locked definition and the timestamps in `state.json`): the definition hash; every request count,
HTTP status and token count; every per-run, per-arm and total gateway-computed cost; the uncached-equivalent cost; the first-request-excluded sensitivity analysis; the prompt-cache warmth of
each run's first request; the 600 s pair gaps and the intervals between pairs; and that the report quotes these figures.

**2. Consistency checks among recorded values** (these show the summaries agree with each other, not that they are true): the rows against the schedule, the state and each `evidence.json`;
the manifest hashes; the patch hash recorded in the row, the manifest, the handoff and the evaluator copy being the same value; the four gold and four no-op control outcomes as written in
`state.json`; the provenance record (its schema; `builder.sha256` against the builder committed at `builder.built_on_commit`; `execution.harness_commit` (the original execution commit) against the run records in every `evidence.json`; `execution.public_history_commit` against `config/history_rewrite.json` as committed at `builder.built_on_commit`; the definition hash and the recorded source hashes); the handoff statuses and flag triggers; and the test-behaviour counts, including the stricter fix-revision count, which is re-derived from the recorded attempt and edit
classifications.

Checks that need Git history (the committed builder at `builder.built_on_commit`, and the existence of `execution.public_history_commit` and its mapping) print SKIP, never PASS, when the history is unavailable, for example in a shallow clone; `--require-git` turns a SKIP into a failure.

**3. Recorded summaries that cannot be recomputed here:** the resolved verdicts, F2P/P2P counts and evaluator runner summaries (counts and fixed values only, never test identifiers); handoff
check outcomes; and everything under `transcript` in `evidence.json` (tool-call order, test-attempt states, edit classes and target kinds). These are what the builder read from the private
files. The hashes do not prove them.

## Rebuilding

`scripts/build_e1_stage1_evidence.py` regenerates the bundle from the private `runs/` directory and the pinned local dataset snapshot, so it only runs on the machine that holds them. It reads
those files without modifying them, aborts on any unexpected field or unsafe pattern, scans the result for hidden-test identifiers and patch lines, and is deterministic (no timestamps) apart from `builder.built_on_commit`, the commit it was generated on. It refuses to run unless the tracked worktree is clean and the builder is committed at HEAD exactly as executed.

## What is withheld, and why

- **Raw transcripts**, because they are not needed for the quantitative tables, three of the eight contain a line of the upstream fix, and every one embeds session identifiers and host details.
- **Patches and predictions**, because an agent patch here is close to or equal to the upstream fix, which the project does not publish. Only hashes, sizes, file paths and line counts are kept.
- **Prompts**, because they embed public issue text that can name third parties. Only hashes are kept, plus a hash of the issue text and whether it equals the public dataset's problem statement.
- **Evaluator logs and reports**, because they contain the hidden test patch and test identifiers.
- **Network, container and host details** (addresses, subnets, container ids, socket paths, absolute paths), because they are not needed to check any result.

The file hashes in `artifacts_manifest.json`, `PROVENANCE.json` and `evidence.json` identify withheld files and show that the bundle is internally consistent. They do **not** let a third party
verify the withheld contents, which only the holder of the private files can recheck.

## Scope and cost

- Only E1 Stage 1 is included. **Calibration evidence is excluded**: the two calibration runs are neither scored nor in these files, so any figure about them is not checkable here.
- **Every dollar figure is gateway-computed** (measured token counts multiplied by the recorded price table). None is verified provider billing or a Console figure. Claude Code's own cost
  field is a client-side estimate and is stored separately.
- This is a small exploratory study: four tasks, one run per arm per task. Nothing here supports a statistical claim.

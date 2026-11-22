#!/usr/bin/env python3
"""Paired analysis of the M3 results JSONL (offline, read-only). Prints Markdown; --out writes the full JSON report.

    PYTHONPATH=src .venv/bin/python scripts/m3_analyze.py runs/m3/<experiment_id>/results.jsonl [--definition PATH] [--out report.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiep.analysis import analyze, render_markdown  # noqa: E402
from aiep.experiment_def import DEFAULT_DEFINITION, load_experiment  # noqa: E402
from aiep.results import ResultsWriter  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("results", type=Path)
    ap.add_argument("--definition", type=Path, default=ROOT / DEFAULT_DEFINITION)
    ap.add_argument("--tasks", help="comma-separated FINAL task list when replacements were used (default: the definition's tasks)")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    definition = load_experiment(args.definition, root=ROOT)
    report = analyze(ResultsWriter(args.results).rows(), definition, tasks=args.tasks.split(",") if args.tasks else None)
    if args.out:
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Sample candidate gold items from L2 report bundles (PRD §3.11, gold/README.md step 1).

Reads each report dir's overall.json for (task, run_id), pulls the run's visible
text and ledger snapshot from the local data dir, extracts per-judge candidate
items with deterministic heuristics, assigns 4:3:3 splits, and writes one batch
file for the annotation UI (evals/tools/annotate_gold.py). No model calls;
labels themselves are produced by the project owner only.

Boundary note: this driver is the eval system's ONLY touchpoint with the main
system — a read-only import of ophira_backend's storage layer to load run data.
Nothing in ophira_backend imports evals/.

Usage (venv has ophira_backend installed via `pip install -e ./backend[dev]`):
  .venv/bin/python evals/tools/sample_gold.py
  .venv/bin/python evals/tools/sample_gold.py \
      --report-dir evals/reports/l2-20260820T173608Z --per-run-cap 6
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

# 只读接触点：加载 run 可见文本与账本快照（见模块 docstring 的边界说明）。
from ophira_backend.config import Settings
from ophira_backend.storage import FileLedger

from gold_sampling import JUDGES, build_samples

TEXT_SUFFIXES = {".md", ".txt", ".csv"}
MAX_ARTIFACT_CHARS = 50_000


def report_runs(report_dir: Path) -> list[tuple[str, str]]:
    overall = json.loads((report_dir / "overall.json").read_text(encoding="utf-8"))
    pairs = []
    for result in overall.get("results", []):
        if result.get("run_id"):
            pairs.append((result["task"], result["run_id"]))
    return pairs


def final_text_for(ledger: FileLedger, run_id: str) -> str:
    text = ""
    for event in ledger.list_events(run_id):
        if event.type == "assistant.message.completed":
            candidate = event.data.get("text")
            if isinstance(candidate, str) and candidate.strip():
                text = candidate
    return text


def evidence_rows(project) -> list[dict]:
    rows = []
    for item in project.evidence:
        source = "url" if item.source_url else "path" if item.source_path else "NONE"
        rows.append(
            {
                "id": item.id,
                "kind": item.kind.value,
                "status": item.status.value,
                "claim": item.claim,
                "source": source,
                "source_title": item.source_title,
                "source_url": str(item.source_url) if item.source_url else None,
                "source_path": item.source_path,
                "retrieved_at": item.retrieved_at.isoformat(),
                "observed_at": item.observed_at.isoformat() if item.observed_at else None,
                "confidence": item.confidence,
                "excerpt": (item.excerpt or "")[:300] or None,
            }
        )
    return rows


def artifact_texts(ledger: FileLedger, project) -> list[dict]:
    workspace = ledger.workspace_for_project(project.id)
    artifacts = []
    for artifact in project.artifacts:
        if artifact.path.startswith("uploads/"):
            continue
        if Path(artifact.path).suffix.lower() not in TEXT_SUFFIXES:
            continue
        path = workspace / artifact.path
        if not path.is_file():
            continue
        artifacts.append(
            {
                "name": artifact.name,
                "path": artifact.path,
                "text": path.read_text(encoding="utf-8", errors="replace")[
                    :MAX_ARTIFACT_CHARS
                ],
            }
        )
    return artifacts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-dir", action="append", type=Path, default=None,
        help="L2 report dir (repeatable); default: all evals/reports/l2-*",
    )
    parser.add_argument("--per-run-cap", type=int, default=6,
                        help="max items per judge per run (default 6)")
    parser.add_argument("--seed", type=int, default=20260820)
    parser.add_argument("--out", type=Path, default=None,
                        help="output batch file (default evals/gold/samples/batch-<stamp>.json)")
    args = parser.parse_args()

    report_dirs = args.report_dir or sorted(
        (REPO_ROOT / "evals" / "reports").glob("l2-*")
    )
    report_dirs = [d for d in report_dirs if (d / "overall.json").exists()]
    if not report_dirs:
        raise SystemExit("No report dirs with overall.json found.")

    ledger = FileLedger(Settings().data_dir)
    views = []
    for report_dir in report_dirs:
        for task, run_id in report_runs(report_dir):
            try:
                run = ledger.get_run(run_id)
                project = ledger.get_project(run.project_id)
            except KeyError:
                print(f"[skip] {run_id} ({task}): not in local data dir", file=sys.stderr)
                continue
            views.append(
                {
                    "run_id": run_id,
                    "task": task,
                    "report": report_dir.name,
                    "final_text": final_text_for(ledger, run_id),
                    "evidence": evidence_rows(project),
                    "artifacts": artifact_texts(ledger, project),
                }
            )
    if not views:
        raise SystemExit("No runs resolved from the given report dirs.")

    samples = build_samples(views, per_run_cap=args.per_run_cap, seed=args.seed)
    samples["generated_at"] = datetime.now(timezone.utc).isoformat()
    samples["reports"] = [d.name for d in report_dirs]

    out = args.out
    if out is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = REPO_ROOT / "evals" / "gold" / "samples" / f"batch-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(samples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    counts = Counter(item["judge"] for item in samples["items"])
    print(f"Batch written: {out}")
    print(f"Runs: {len(views)} from {len(report_dirs)} report dir(s)")
    for judge in JUDGES:
        status = "OK" if counts[judge] >= 30 else "below 30 — add report dirs or raise cap"
        print(f"  {judge}: {counts[judge]} items ({status})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

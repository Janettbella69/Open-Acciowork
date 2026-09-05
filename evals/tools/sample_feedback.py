#!/usr/bin/env python3
"""Prepare UNLABELED gold candidates from an explicitly exported feedback JSON.

No models, label writes, business-runtime imports, or Skill mutations. Feedback
is a reason to inspect a trace, not proof that the result was wrong. The owner
still labels candidates and calibrates judges under PRD §3.11 / §6.6.

Usage: .venv/bin/python evals/tools/sample_feedback.py \
  --input /path/to/reviewed-feedback-export.json \
  --out evals/gold/samples/feedback-20260905.json
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from gold_sampling import build_samples

MAX_EXPORT_BYTES = 20_000_000


def build_feedback_samples(rows: list[dict], *, per_run_cap: int = 6, seed: int = 20260905) -> dict:
    if not 1 <= per_run_cap <= 200:
        raise ValueError("per_run_cap must be between 1 and 200")
    runs: dict[str, dict] = {}
    feedback_candidates: list[dict] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise ValueError("each feedback row requires an id")
        feedback_id = row["id"]
        if feedback_id in seen:
            raise ValueError("duplicate feedback id; export a consistent snapshot")
        seen.add(feedback_id)
        if row.get("status") == "rejected":
            skipped.append({"feedback_id": feedback_id, "reason": "human_rejected"})
            continue
        context = row.get("context")
        if not isinstance(context, dict) or not all(
            isinstance(value, str) and value.strip()
            for value in (row.get("run_id"), row.get("project_id"), context.get("trace_id"))
        ):
            skipped.append({"feedback_id": feedback_id, "reason": "missing_trace_anchor"})
            continue
        if row.get("kind") not in {"conclusion_wrong", "evidence_issue", "run_failure"}:
            raise ValueError("unknown feedback kind")
        note = row.get("user_note")
        if not isinstance(note, str) or not note.strip():
            raise ValueError("feedback candidates require a written reason")
        if row.get("kind") == "conclusion_wrong" and (
            not row.get("message_id")
            or row["message_id"] != context.get("event_id")
            or context.get("event_type") != "assistant.message.completed"
            or not isinstance(context.get("message_text"), str)
        ):
            skipped.append({"feedback_id": feedback_id, "reason": "missing_message_anchor"})
            continue
        evidence = context.get("evidence")
        if row.get("kind") == "evidence_issue" and (
            not isinstance(evidence, dict)
            or not row.get("evidence_id")
            or row["evidence_id"] != evidence.get("id")
        ):
            skipped.append({"feedback_id": feedback_id, "reason": "missing_evidence_anchor"})
            continue
        run_id = row["run_id"]
        if run_id in runs and (
            runs[run_id]["project_id"] != row["project_id"]
            or runs[run_id]["trace_id"] != context["trace_id"]
        ):
            raise ValueError("conflicting project/trace binding for one run")
        view = runs.setdefault(
            run_id,
            {
                "run_id": run_id,
                "project_id": row["project_id"],
                "trace_id": context["trace_id"],
                "task": "feedback-review",
                "report": "explicit-feedback-export",
                "final_text": "",
                "evidence": [],
                "artifacts": [],
                "feedback_ids": [],
            },
        )
        view["feedback_ids"].append(feedback_id)
        if row["kind"] == "conclusion_wrong":
            view["final_text"] += context["message_text"] + "\n"
        if isinstance(evidence, dict):
            # Same shape consumed by existing gold_sampling / human annotation UI.
            evidence_row = {
                key: evidence.get(key)
                for key in (
                    "id",
                    "kind",
                    "claim",
                    "status",
                    "source_title",
                    "source_url",
                    "source_path",
                    "retrieved_at",
                    "observed_at",
                    "confidence",
                    "excerpt",
                )
            }
            evidence_row["source"] = (
                "url"
                if evidence.get("source_url")
                else "path"
                if evidence.get("source_path")
                else "NONE"
            )
            if not any(item["id"] == evidence_row["id"] for item in view["evidence"]):
                view["evidence"].append(evidence_row)
                view["final_text"] += str(evidence.get("claim") or "") + "\n"
        # Whitelist output fields. Never propagate caller labels, identities,
        # session bindings, arbitrary raw objects, or alleged judge results.
        feedback_candidates.append(
            {
                "feedback_id": feedback_id,
                "run_id": run_id,
                "project_id": row["project_id"],
                "trace_id": context["trace_id"],
                "event_id": context.get("event_id"),
                "evidence_id": row.get("evidence_id"),
                "kind": row["kind"],
                "user_note": note,
                "source": row.get("source", "user"),
                "review_status": row.get("status", "open"),
                "candidate_state": "unlabeled",
            }
        )
    samples = build_samples(list(runs.values()), per_run_cap=per_run_cap, seed=seed)
    samples.update(
        {
            "schema_version": "feedback-candidates-1",
            "labels_produced": False,
            "feedback_candidates": feedback_candidates,
            "skipped_feedback": skipped,
            "human_gate": (
                "Project owner labels personally; uncalibrated judges are not a release gate."
            ),
        }
    )
    return samples


def write_new_batch(path: Path, samples: dict) -> None:
    """Publish atomically without overwriting any existing human-curated batch."""
    if path.suffix != ".json":
        raise ValueError("candidate output must be a .json batch, never a labels.jsonl file")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".feedback-batch-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(samples, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--per-run-cap", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20260905)
    args = parser.parse_args()
    if args.input.stat().st_size > MAX_EXPORT_BYTES:
        parser.error("feedback export exceeds the 20 MB import limit")
    rows = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        parser.error("input must be a feedback JSON array exported by the control plane")
    samples = build_feedback_samples(rows, per_run_cap=args.per_run_cap, seed=args.seed)
    samples["generated_at"] = datetime.now(timezone.utc).isoformat()
    write_new_batch(args.out, samples)
    print(f"Unlabeled feedback candidates: {len(samples['feedback_candidates'])}")
    print(
        f"Heuristic annotation units: {len(samples['items'])}; "
        f"skipped: {len(samples['skipped_feedback'])}"
    )
    print(f"Batch: {args.out}. No labels or release verdicts were produced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Copy explicitly selected L2 archives into evaluation storage, preserving provenance."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path

from .contract import ADAPTER_VERSION, JUDGES, RUN_CREATED, RUN_RESULT, TEXT_COMPLETED


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def boolean(value):
    return value if type(value) is bool else None


def component(value: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError("Missing or invalid task/run identifier")
    if "/" in value or "\\" in value or "\x00" in value:
        raise ValueError("Task/run identifiers cannot contain path separators")
    return value


def validate_objects(value: dict, ref: dict, paths: tuple[str, ...]) -> dict:
    """Bad nested metadata remains downloadable but cannot drive the normalized view."""
    for path in paths:
        current = value
        for part in path.split("."):
            current = current.get(part)
            if current is None:
                break
            if not isinstance(current, dict):
                ref.update(status="corrupt", error=f"Expected an object at {path}")
                return {}
    return value


class Capture:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.manifest: list[dict] = []

    def read(self, path: Path, root: Path) -> tuple[dict, bytes | None]:
        ref = {"path": str(path.absolute()), "sha256": None, "status": "missing"}
        self.manifest.append(ref)
        try:
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("Source escapes the explicitly selected directory")
            if not path.exists():
                return ref, None
            if path.stat().st_size > 128 * 1024 * 1024:
                raise ValueError("Source exceeds the 128 MiB per-file limit")
            raw = path.read_bytes()
        except (OSError, ValueError) as exc:
            ref.update(status="unreadable", error=str(exc))
            return ref, None
        sha = digest(raw)
        self.files[sha] = raw
        ref.update(sha256=sha, status="available", bytes=len(raw))
        return ref, raw

    def json(self, path: Path, root: Path) -> tuple[dict, dict]:
        ref, raw = self.read(path, root)
        if raw is None:
            return ref, {}
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("Expected a JSON object")
            encode(value)  # reject non-finite numbers accepted by Python's JSON parser
            return ref, value
        except (UnicodeError, ValueError) as exc:
            ref.update(status="corrupt", error=str(exc))
            return ref, {}


def read_events(raw: bytes | None, run_id: str, ref: dict) -> list[dict]:
    events = []
    invalid = []
    for line_no, line in enumerate((raw or b"").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
                raise ValueError("Expected an event object with data")
            if event.get("run_id") != run_id:
                raise ValueError("Event belongs to another run")
            if type(event.get("sequence")) is not int or event["sequence"] < 1:
                raise ValueError("Invalid event sequence")
            if not isinstance(event.get("type"), str):
                raise ValueError("Missing event type")
            encode(event)
            events.append(event)
        except (ValueError, UnicodeError) as exc:
            invalid.append({"line": line_no, "error": str(exc)})
    events.sort(key=lambda event: event["sequence"])
    sequences = [event["sequence"] for event in events]
    if invalid:
        ref.update(status="corrupt", invalid_lines=invalid[:30], invalid_count=len(invalid))
    elif raw is not None and not events:
        ref.update(status="empty", error="The archived event file is empty")
    if sequences and sequences != list(range(1, len(sequences) + 1)):
        ref["sequence_warning"] = "Missing or duplicate sequences; original file is preserved"
    return events


def prepare_report(report: Path, runs_dir: Path | None, samples: Path | None) -> dict:
    report = report.resolve()
    if report.is_dir():
        report = report / "overall.json"
    capture = Capture()
    report_ref, overall = capture.json(report, report.parent)
    if report_ref["status"] != "available" or not isinstance(overall.get("results"), list):
        raise ValueError(f"Invalid overall report: {report_ref}")
    batch_ref, batch = capture.json(samples, samples.parent) if samples else (None, {})
    if batch_ref and batch_ref["status"] != "available":
        raise ValueError(f"Invalid Gold batch: {batch_ref}")
    if not isinstance(batch.get("items", []), list) or not isinstance(batch.get("runs", {}), dict):
        raise ValueError("Invalid Gold batch structure")
    task_inputs = []
    for ordinal, result in enumerate(overall["results"]):
        if not isinstance(result, dict):
            raise ValueError("Invalid task result")
        task = component(result.get("task"))
        run_id = component(result.get("run_id"))
        bundle = report.parent / task
        refs = {"report": report_ref}
        refs["summary"], summary = capture.json(bundle / "summary.json", report.parent)
        summary = validate_objects(summary, refs["summary"], ("run", "verdict", "notes"))
        run_root = runs_dir if runs_dir is not None else report.parent
        run_path = runs_dir / run_id / "run.json" if runs_dir else bundle / "run.json"
        refs["run"], run = capture.json(run_path, run_root)
        run = validate_objects(run, refs["run"], ("provider_snapshot",))
        if run.get("id") not in (None, run_id) or (summary.get("run") or {}).get("id") not in (
            None,
            run_id,
        ):
            raise ValueError("Run metadata does not match report run_id")
        event_path = bundle / "events.jsonl"
        event_root = report.parent
        if not event_path.exists() and runs_dir is not None:
            event_path, event_root = runs_dir / run_id / "events.jsonl", runs_dir
        refs["events"], _ = capture.read(event_path, event_root)
        refs["snapshot"], snapshot = capture.json(bundle / "snapshot.json", report.parent)
        snapshot = validate_objects(
            snapshot,
            refs["snapshot"],
            ("output", "output.run", "output.project_ledger"),
        )
        refs["snapshot_eval"], verdict = capture.json(bundle / "snapshot-eval.json", report.parent)
        if batch_ref:
            refs["gold_batch"] = batch_ref
        task_inputs.append(
            {
                "ordinal": ordinal,
                "case_key": task,
                "run_id": run_id,
                "result": result,
                "summary": summary,
                "run": run,
                "snapshot": snapshot,
                "snapshot_eval": verdict,
                "sources": refs,
            }
        )
    identity = digest(encode({"adapter": ADAPTER_VERSION, "files": capture.manifest}).encode())
    return {
        "id": identity[:24],
        "name": report.parent.name,
        "provider": overall.get("provider"),
        "generated_at": overall.get("generated_at"),
        "source": report_ref,
        "adapter_version": ADAPTER_VERSION,
        "overall": overall,
        "batch": batch,
        "batch_name": samples.name if samples else None,
        "capture": capture,
        "tasks": task_inputs,
    }


def normalize_trace(prepared: dict, task: dict) -> tuple[dict, list[dict], list[dict]]:
    capture, refs = prepared["capture"], task["sources"]
    events = read_events(
        capture.files.get(refs["events"]["sha256"]), task["run_id"], refs["events"]
    )
    results = [e["data"] for e in events if e["type"] == RUN_RESULT]
    result = results[-1] if results else {}
    created = next((e["data"] for e in events if e["type"] == RUN_CREATED), {})
    run = task["run"] or task["summary"].get("run") or {}
    recorded_count = number(task["summary"].get("event_count"))
    if recorded_count is not None and recorded_count != len(events):
        refs["events"]["sequence_warning"] = (
            f"Report records {recorded_count} events, imported {len(events)}"
        )
    view = prepared["batch"].get("runs", {}).get(task["run_id"], {})
    if not isinstance(view, dict):
        raise ValueError("Invalid Gold run view")
    snapshot, verdict, old_result = task["snapshot"], task["snapshot_eval"], task["result"]
    snapshot_run_id = ((snapshot.get("output") or {}).get("run") or {}).get("id")
    if snapshot and (snapshot_run_id != task["run_id"] or not snapshot.get("schema_version")):
        refs["snapshot"].update(
            status="corrupt", error="Snapshot run/schema identity is missing or mismatched"
        )
        snapshot, verdict = {}, {}
    if verdict and (
        type(verdict.get("accepted")) is not bool
        or not isinstance(verdict.get("findings"), list)
        or not isinstance(verdict.get("profile"), str)
        or any(
            not isinstance(f, dict) or not isinstance(f.get("code"), str)
            for f in verdict.get("findings", [])
        )
    ):
        refs["snapshot_eval"].update(status="corrupt", error="Invalid Snapshot evaluation shape")
        verdict = {}
    prompt = run.get("prompt") or created.get("prompt")
    duration_ms = number(result.get("duration_ms"))
    if duration_ms is None:
        try:
            duration_ms = (
                datetime.fromisoformat(run["finished_at"].replace("Z", "+00:00"))
                - datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))
            ).total_seconds() * 1000
        except (KeyError, TypeError, ValueError):
            pass
    cost = number(result.get("total_cost_usd"))
    cost_basis = result.get("cost_basis")
    if cost is None:
        cost = number(old_result.get("cost_usd_list_price"))
        cost_basis = "sdk_anthropic_list_price" if cost is not None else None
    skills = run.get("skill_snapshot", created.get("skill_snapshot"))
    final_text = result.get("result")
    if not isinstance(final_text, str):
        texts = [e["data"].get("text") for e in events if e["type"] == TEXT_COMPLETED]
        final_text = next((t for t in reversed(texts) if isinstance(t, str) and t), None)
    output = snapshot.get("output") or {}
    items = []
    for item in prepared["batch"].get("items", []):
        if not isinstance(item, dict):
            raise ValueError("Invalid Gold item")
        if item.get("run_id") != task["run_id"]:
            continue
        if (
            item.get("judge") not in JUDGES
            or not item.get("item_id")
            or not isinstance(item.get("item"), str)
        ):
            raise ValueError("Gold item must preserve its item_id, judge and statement")
        if item.get("split") not in ("train", "dev", "test"):
            raise ValueError("Gold item is missing its original train/dev/test split")
        items.append({**item, "batch": prepared["batch_name"]})
    provider = run.get("provider_snapshot") or {}
    trace = {
        **task,
        "snapshot": snapshot,
        "snapshot_eval": verdict,
        "id": digest(f"{prepared['id']}:{task['ordinal']}".encode())[:24],
        "experiment_id": prepared["id"],
        "experiment_name": prepared["name"],
        "trace_id": run.get("trace_id") or next((e.get("trace_id") for e in events), None),
        "execution_status": run.get("status"),
        "prompt": prompt,
        "final_text": final_text or view.get("final_text"),
        "final_text_source": "events" if final_text else ("gold_batch" if view else None),
        "duration_ms": duration_ms,
        "cost_usd": cost,
        "cost_basis": cost_basis,
        "usage": result.get("usage"),
        "num_turns": result.get("num_turns", old_result.get("num_turns")),
        "legacy_passed": boolean(old_result.get("passed")),
        "snapshot_accepted": boolean(verdict.get("accepted")) if snapshot else None,
        "quality_status": "unlabeled",
        "event_count": len(events),
        "event_types": dict(Counter(e["type"] for e in events)),
        "evidence": ((output.get("project_ledger") or {}).get("evidence") or [])
        if snapshot
        else (view.get("evidence") or []),
        "artifacts": (output.get("artifacts") or []) if snapshot else (view.get("artifacts") or []),
        "evidence_source": "snapshot" if snapshot else ("gold_batch" if view else None),
        "versions": {
            "task_prompt_hash": digest(prompt.encode()) if isinstance(prompt, str) else None,
            "fixture_hash": old_result.get("fixture_hash"),
            "grader_version": old_result.get("grader_version"),
            "grading_profile": verdict.get("profile"),
            "snapshot_schema": snapshot.get("schema_version"),
            "model": provider.get("model")
            or (task["summary"].get("verdict") or {}).get("served_models"),
            "skill_hash": digest(encode(skills).encode()) if skills is not None else None,
        },
        "skill_snapshot": skills,
    }
    return trace, events, items

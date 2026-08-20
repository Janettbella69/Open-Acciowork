#!/usr/bin/env python3
"""L2 behavior-eval driver (PRD §3.11): replay the benchmark task set on a real provider.

Reads evals/tasks/*.yaml, runs each through the same in-process app + Green Path
harness, checks per-task machine assertions (tool coverage subset, ledger writes,
artifact landing), and archives redacted bundles for error analysis.

Usage:
  PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_l2.py --dry-run
  PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_l2.py \
      --task b-product-validation --provider deepseek
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from ophira_backend.config import Settings  # noqa: E402
from ophira_backend.greenpath import (  # noqa: E402
    PermissionPolicy,
    drive_run,
    verify_events,
    write_bundle,
)
from ophira_backend.main import create_app  # noqa: E402

TASKS_DIR = REPO_ROOT / "evals" / "tasks"

MIME_DEFAULTS = {"csv": "text/csv", "md": "text/markdown"}


def load_tasks(task_filter: str | None) -> list[dict]:
    tasks = []
    for path in sorted(TASKS_DIR.glob("*.yaml")):
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        spec["_path"] = path
        if task_filter and spec.get("id") != task_filter:
            continue
        tasks.append(spec)
    if not tasks:
        raise SystemExit(f"No tasks matched (filter={task_filter!r}) in {TASKS_DIR}")
    return tasks


def validate_task(spec: dict) -> list[str]:
    problems = []
    for key in ("id", "scenario", "prompt", "fixtures", "expect", "minimum_pass"):
        if key not in spec:
            problems.append(f"missing key: {key}")
    for fixture in spec.get("fixtures", []):
        fixture_path = TASKS_DIR / fixture["path"]
        if not fixture_path.exists():
            problems.append(f"fixture not found: {fixture_path}")
    expect = spec.get("expect", {})
    unknown = set(expect) - {
        "deny_marker", "required_coverage", "evidence_min", "decision_required",
        "artifact_suffix",
    }
    if unknown:
        problems.append(f"unknown expect keys: {sorted(unknown)}")
    return problems


async def run_task(client, ledger, spec: dict, provider_id: str, report_dir: Path,
                   timeout_s: float) -> dict:
    expect = spec["expect"]
    response = await client.post(
        "/api/projects",
        json={
            "name": f"L2 {spec['id']}",
            "objective": f"L2 基准任务 {spec['id']}（场景 {spec['scenario']}，合成 fixture）",
        },
    )
    response.raise_for_status()
    project_id = response.json()["id"]

    for fixture in spec["fixtures"]:
        source = TASKS_DIR / fixture["path"]
        mime = fixture.get("mime") or MIME_DEFAULTS.get(source.suffix.lstrip("."), "text/plain")
        upload = await client.post(
            f"/api/projects/{project_id}/files",
            params={"filename": fixture["upload_as"], "mime": mime},
            content=source.read_bytes(),
        )
        upload.raise_for_status()

    policy = PermissionPolicy()
    run_payload = await drive_run(
        client, ledger, project_id, spec["prompt"],
        provider_id=provider_id, policy=policy, timeout_s=timeout_s,
    )
    events = ledger.list_events(run_payload["id"])
    verdict = verify_events(
        events,
        expect_deny=bool(expect.get("deny_marker")),
        required_coverage=tuple(expect.get("required_coverage", [])) or None,
    )

    project = ledger.get_project(project_id)
    task_missing: list[str] = []
    if len(project.evidence) < int(expect.get("evidence_min", 0)):
        task_missing.append(
            f"evidence:{len(project.evidence)}<{expect.get('evidence_min')}"
        )
    if expect.get("decision_required") and not project.decisions:
        task_missing.append("decision:absent")
    suffix = expect.get("artifact_suffix")
    if suffix:
        generated = [
            artifact for artifact in project.artifacts
            if artifact.name.endswith(suffix) and not artifact.path.startswith("uploads/")
        ]
        if not generated:
            task_missing.append(f"artifact:*{suffix} not produced")

    write_bundle(
        report_dir, spec["id"], run_payload, verdict, events, task_missing,
        notes={"scenario": spec["scenario"], "minimum_pass": spec["minimum_pass"]},
    )
    passed = verdict.ok and not task_missing
    return {
        "task": spec["id"],
        "passed": passed,
        "verdict_missing": verdict.missing,
        "task_missing": task_missing,
        "cost_usd_list_price": verdict.total_cost_usd,
        "num_turns": verdict.num_turns,
        "run_id": run_payload["id"],
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--task", default=None, help="run only this task id")
    parser.add_argument("--budget-usd", type=float, default=2.0)
    parser.add_argument("--timeout-s", type=float, default=900)
    parser.add_argument("--dry-run", action="store_true",
                        help="validate task specs and fixtures without touching an endpoint")
    args = parser.parse_args()

    tasks = load_tasks(args.task)
    problems = {spec["id"]: validate_task(spec) for spec in tasks}
    for task_id, issues in problems.items():
        status = "OK" if not issues else f"INVALID: {issues}"
        print(f"[spec] {task_id}: {status}")
    if any(problems.values()):
        return 2
    if args.dry_run:
        print(f"Dry run complete: {len(tasks)} task spec(s) valid.")
        return 0

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_dir = REPO_ROOT / "evals" / "reports" / f"l2-{stamp}"
    report_dir.mkdir(parents=True, exist_ok=True)

    app = create_app(Settings(max_budget_usd=args.budget_usd))
    ledger = app.state.ledger
    transport = httpx.ASGITransport(app=app)
    results = []
    async with httpx.AsyncClient(
        transport=transport, base_url="http://ophira-l2", timeout=httpx.Timeout(60),
    ) as client:
        for spec in tasks:
            print(f"\n=== L2 task {spec['id']} on {args.provider} ===")
            try:
                result = await run_task(
                    client, ledger, spec, args.provider, report_dir, args.timeout_s,
                )
            except Exception as exc:  # noqa: BLE001
                result = {"task": spec["id"], "passed": False, "error": str(exc)}
            results.append(result)
            print(json.dumps(result, ensure_ascii=False, indent=2))

    all_passed = all(item.get("passed") for item in results)
    (report_dir / "overall.json").write_text(
        json.dumps(
            {
                "provider": args.provider,
                "generated_at": datetime.now(UTC).isoformat(),
                "all_passed": all_passed,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nL2 overall: {'PASS' if all_passed else 'FAIL'} · bundles in {report_dir}")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

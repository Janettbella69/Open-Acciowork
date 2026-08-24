#!/usr/bin/env python3
"""Green Path E2E runner (PRD §6.2 / P0-2): N consecutive live rounds on one provider.

Each round: fresh project -> upload fixtures -> full task run (permissions resolved by
policy, marker Write denied) -> event/ledger verification -> SSE replay-from-checkpoint
check -> session-resume mini-run -> auditable bundle under evals/reports/.

Usage:
  PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_green_path.py \
      --provider deepseek --rounds 3 --budget-usd 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from ophira_backend.config import Settings  # noqa: E402
from ophira_backend.greenpath import (  # noqa: E402
    GREEN_PATH_PROMPT,
    RESUME_PROMPT,
    PermissionPolicy,
    drive_run,
    sse_replay_sequences,
    verify_events,
    verify_project_state,
    write_bundle,
)
from ophira_backend.main import create_app  # noqa: E402

FIXTURES_DIR = REPO_ROOT / "backend" / "fixtures" / "greenpath"


async def run_round(client, ledger, provider_id: str, round_no: int, report_dir: Path,
                    timeout_s: float) -> tuple[bool, dict]:
    response = await client.post(
        "/api/projects",
        json={
            "name": f"GreenPath round {round_no}",
            "objective": "Green Path E2E 验收：便携榨汁杯欧盟机会验证（合成 fixture）",
            "product_brief": "评测用合成任务，不代表真实项目。",
            "target_markets": ["EU"],
            "constraints": ["预算 8000 USD", "CE/LFGB 认证"],
        },
    )
    response.raise_for_status()
    project_id = response.json()["id"]

    for name, mime in (("quotes.csv", "text/csv"), ("constraints.md", "text/markdown")):
        upload = await client.post(
            f"/api/projects/{project_id}/files",
            params={"filename": name, "mime": mime},
            content=(FIXTURES_DIR / name).read_bytes(),
        )
        upload.raise_for_status()

    policy = PermissionPolicy()
    run_payload = await drive_run(
        client, ledger, project_id, GREEN_PATH_PROMPT,
        provider_id=provider_id, policy=policy, timeout_s=timeout_s,
    )
    run_id = run_payload["id"]
    events = ledger.list_events(run_id)
    verdict = verify_events(events, expect_deny=True)
    project_missing = verify_project_state(ledger.get_project(project_id))

    max_sequence = max((event.sequence for event in events), default=0)
    checkpoint = max_sequence // 2
    replayed = await sse_replay_sequences(client, run_id, checkpoint)
    sse_replay_ok = replayed == list(range(checkpoint + 1, max_sequence + 1))

    resume_ok = False
    resume_run_id = None
    resume_error = None
    session_id = run_payload.get("sdk_session_id")
    if session_id:
        try:
            resume_payload = await drive_run(
                client, ledger, project_id, RESUME_PROMPT,
                provider_id=provider_id, policy=PermissionPolicy(),
                resume_session_id=session_id, timeout_s=timeout_s,
            )
            resume_run_id = resume_payload["id"]
            report_path = ledger.workspace_for_project(project_id) / "greenpath-report.md"
            resume_ok = (
                resume_payload["status"] == "completed"
                and report_path.exists()
                and "会话续接验证 OK" in report_path.read_text(encoding="utf-8")
            )
        except Exception as exc:  # noqa: BLE001 - report, don't crash the round loop
            resume_error = str(exc)
    else:
        resume_error = "run record has no sdk_session_id"

    notes = {
        "sse_replay_ok": sse_replay_ok,
        "sse_checkpoint": checkpoint,
        "sse_replayed_count": len(replayed),
        "resume_run_id": resume_run_id,
        "resume_ok": resume_ok,
        "resume_error": resume_error,
        "policy_denied": policy.denied,
        "policy_allowed_count": len(policy.allowed),
    }
    write_bundle(report_dir, f"round-{round_no}", run_payload, verdict, events,
                 project_missing, notes)

    passed = verdict.ok and not project_missing and sse_replay_ok and resume_ok
    summary = {
        "round": round_no,
        "run_id": run_id,
        "passed": passed,
        "verdict_missing": verdict.missing,
        "project_missing": project_missing,
        "sse_replay_ok": sse_replay_ok,
        "resume_ok": resume_ok,
        "resume_error": resume_error,
        "web_degraded": verdict.web_degraded,
        "cost_usd_list_price": verdict.total_cost_usd,
        "num_turns": verdict.num_turns,
        "served_models": verdict.served_models,
    }
    return passed, summary


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--budget-usd", type=float, default=2.0)
    parser.add_argument("--timeout-s", type=float, default=900)
    parser.add_argument("--report-dir", default=str(REPO_ROOT / "evals" / "reports"))
    args = parser.parse_args()

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_dir = Path(args.report_dir) / f"greenpath-{stamp}"
    report_dir.mkdir(parents=True, exist_ok=True)

    settings = Settings(max_budget_usd=args.budget_usd)
    app = create_app(settings)
    ledger = app.state.ledger

    transport = httpx.ASGITransport(app=app)
    summaries = []
    all_passed = True
    async with httpx.AsyncClient(
        transport=transport, base_url="http://greenpath", timeout=httpx.Timeout(60),
    ) as client:
        for round_no in range(1, args.rounds + 1):
            print(f"\n=== Green Path round {round_no}/{args.rounds} on {args.provider} ===")
            try:
                passed, summary = await run_round(
                    client, ledger, args.provider, round_no, report_dir, args.timeout_s,
                )
            except Exception as exc:  # noqa: BLE001
                passed, summary = False, {"round": round_no, "passed": False, "error": str(exc)}
            all_passed = all_passed and passed
            summaries.append(summary)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            if not passed:
                break

    (report_dir / "overall.json").write_text(
        json.dumps(
            {
                "provider": args.provider,
                "rounds_requested": args.rounds,
                "all_passed": all_passed,
                "generated_at": datetime.now(UTC).isoformat(),
                "summaries": summaries,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nOverall: {'PASS' if all_passed else 'FAIL'} · bundles in {report_dir}")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

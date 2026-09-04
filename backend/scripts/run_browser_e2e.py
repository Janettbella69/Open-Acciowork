#!/usr/bin/env python3
"""Run the real Playwright L1 browser acceptance check and archive its result.

This is intentionally separate from the mocked security unit tests.  It launches the
managed Chromium binary, navigates through the production browser-tool path, records a
screenshot as Evidence + Artifact, and verifies the persisted linkage.  Network access
is required; a failure is reported honestly and exits non-zero.

Usage:
  PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_browser_e2e.py \
      --url https://example.com/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from ophira_backend.browser_tools import build_browser_tools  # noqa: E402
from ophira_backend.config import Settings  # noqa: E402
from ophira_backend.models import OrgPolicy, ProjectCreate  # noqa: E402
from ophira_backend.storage import FileLedger  # noqa: E402


def _tool_payload(result: dict) -> dict:
    blocks = result.get("content")
    if not isinstance(blocks, list) or not blocks or not isinstance(blocks[0], dict):
        raise ValueError("browser tool returned no content block")
    text = blocks[0].get("text")
    if not isinstance(text, str):
        raise ValueError("browser tool returned a non-text content block")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("browser tool payload is not an object")
    return payload


def _reportable_browser_result(payload: dict) -> dict:
    """Keep acceptance reports free of arbitrary page text or network identity data."""

    allowed = {
        "ok",
        "url",
        "final_url",
        "title",
        "browser_level",
        "artifact_id",
        "evidence_id",
        "error_code",
        "error",
        "failure_type",
        "rejected_url",
    }
    return {key: payload[key] for key in allowed if key in payload}


async def run(url: str, timeout_seconds: float) -> dict:
    hostname = urlsplit(url).hostname
    if not hostname:
        raise ValueError("--url must include a hostname")

    with tempfile.TemporaryDirectory(prefix="ophira-browser-e2e-") as directory:
        data_dir = Path(directory)
        settings = Settings(data_dir=data_dir, web_timeout_seconds=timeout_seconds)
        ledger = FileLedger(data_dir)
        project = ledger.create_project(
            ProjectCreate(
                name="Browser L1 acceptance",
                objective="Verify real Chromium navigation and evidence persistence",
            )
        )
        run_record = ledger.create_run(project.id, f"Capture {url}")
        # The production browser path only creates external Evidence when a real
        # SDK session owns the observation.  This acceptance harness supplies an
        # explicit harness session rather than weakening that fail-closed rule.
        run_record = ledger.update_run(
            run_record.id,
            sdk_session_id="sdk_browser_l1_acceptance",
        )
        tools = build_browser_tools(
            ledger,
            project.id,
            settings,
            policy=OrgPolicy(browser_enabled=True, domain_allowlist=[hostname]),
            run_id=run_record.id,
        )
        result = await tools[0].handler({"url": url, "use_connected_browser": False})
        payload = _tool_payload(result)

        checks: dict[str, bool] = {
            "tool_success": result.get("is_error") is False and payload.get("ok") is True,
            "browser_level_l1": payload.get("browser_level") == "L1",
            "artifact_id_present": isinstance(payload.get("artifact_id"), str),
            "evidence_id_present": isinstance(payload.get("evidence_id"), str),
        }
        persisted = ledger.get_project(project.id)
        evidence = next(
            (item for item in persisted.evidence if item.id == payload.get("evidence_id")),
            None,
        )
        artifact = next(
            (item for item in persisted.artifacts if item.id == payload.get("artifact_id")),
            None,
        )
        checks["evidence_persisted"] = evidence is not None
        checks["artifact_persisted"] = artifact is not None
        receipt = None
        if evidence is not None and evidence.receipt_id:
            receipt = ledger.replay_tool_receipt(project.id, evidence.receipt_id).receipt
        checks["source_receipt_persisted"] = bool(
            receipt is not None
            and receipt.project_id == project.id
            and receipt.run_id == run_record.id
            and receipt.session_id == run_record.sdk_session_id
        )
        checks["artifact_links_evidence"] = bool(
            artifact is not None
            and evidence is not None
            and evidence.id in artifact.source_ids
            and artifact.run_id == run_record.id
        )
        checks["screenshot_exists"] = bool(
            artifact is not None
            and (ledger.workspace_for_project(project.id) / artifact.path).is_file()
        )

        return {
            "kind": "browser_l1_acceptance",
            "generated_at": datetime.now(UTC).isoformat(),
            "url": url,
            "passed": all(checks.values()),
            "checks": checks,
            "browser_result": _reportable_browser_result(payload),
        }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://example.com/")
    parser.add_argument("--timeout-s", type=float, default=30.0)
    parser.add_argument("--report-dir", default=str(REPO_ROOT / "evals" / "reports"))
    args = parser.parse_args()

    try:
        report = await run(args.url, args.timeout_s)
    except Exception as exc:  # noqa: BLE001 - acceptance failures belong in the report
        report = {
            "kind": "browser_l1_acceptance",
            "generated_at": datetime.now(UTC).isoformat(),
            "url": args.url,
            "passed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    report_dir = Path(args.report_dir)
    await asyncio.to_thread(report_dir.mkdir, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"browser-l1-{stamp}.json"
    await asyncio.to_thread(
        path.write_text,
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        "utf-8",
    )
    print(json.dumps({**report, "report_path": str(path)}, ensure_ascii=False, indent=2))
    return 0 if report.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

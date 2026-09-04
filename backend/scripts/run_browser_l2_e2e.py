#!/usr/bin/env python3
"""Attach to a real loopback Chrome CDP session and verify the L2 evidence path.

Chrome must already be running with remote debugging explicitly enabled. The harness
never accepts a CDP endpoint through the browser tool itself: it records the endpoint
in the host connector registry, performs the real health check, then asks the
production tool for ``use_connected_browser=true``.

Example (use a dedicated Chrome profile):
  PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_browser_l2_e2e.py \
      --url https://example.com/ --cdp-endpoint http://127.0.0.1:9222
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from ophira_backend.browser_tools import build_browser_tools  # noqa: E402
from ophira_backend.config import Settings  # noqa: E402
from ophira_backend.connectors import (  # noqa: E402
    ConnectorRegistry,
    check_loopback_cdp_endpoint,
)
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


def _reportable_result(payload: dict) -> dict:
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
        "active_service_worker_count",
    }
    return {key: payload[key] for key in allowed if key in payload}


async def _service_worker_summary(cdp_endpoint: str, timeout_seconds: float) -> dict:
    """Return non-identifying diagnostics for a dedicated acceptance browser."""

    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(
            cdp_endpoint,
            timeout=max(1_000, min(int(timeout_seconds * 1_000), 120_000)),
        )
        workers = [worker for context in browser.contexts for worker in context.service_workers]
        return {
            "count": len(workers),
            "schemes": sorted({urlsplit(worker.url).scheme for worker in workers}),
        }


async def run(url: str, cdp_endpoint: str, timeout_seconds: float) -> dict:
    hostname = urlsplit(url).hostname
    if not hostname:
        raise ValueError("--url must include a hostname")

    with tempfile.TemporaryDirectory(prefix="ophira-browser-l2-e2e-") as directory:
        data_dir = Path(directory)
        settings = Settings(data_dir=data_dir, web_timeout_seconds=timeout_seconds)
        ledger = FileLedger(data_dir)
        project = ledger.create_project(
            ProjectCreate(
                name="Browser L2 acceptance",
                objective="Verify real Chrome CDP attachment and evidence persistence",
            )
        )
        run_record = ledger.create_run(project.id, f"Capture {url} through connected Chrome")
        run_record = ledger.update_run(
            run_record.id,
            sdk_session_id="sdk_browser_l2_acceptance",
        )
        connectors = ConnectorRegistry(data_dir / "connectors")
        connectors.upsert(
            "chrome_cdp",
            type="chrome_cdp",
            display_name="Chrome DevTools (L2 acceptance)",
            scope="browser:attach",
            endpoint=cdp_endpoint,
        )
        connector = connectors.health_check("chrome_cdp")
        worker_summary = await _service_worker_summary(cdp_endpoint, timeout_seconds)
        tools = build_browser_tools(
            ledger,
            project.id,
            settings,
            policy=OrgPolicy(browser_enabled=True, domain_allowlist=[hostname]),
            connectors=connectors,
            run_id=run_record.id,
        )
        result = await tools[0].handler({"url": url, "use_connected_browser": True})
        payload = _tool_payload(result)

        persisted = ledger.get_project(project.id)
        evidence = next(
            (item for item in persisted.evidence if item.id == payload.get("evidence_id")),
            None,
        )
        artifact = next(
            (item for item in persisted.artifacts if item.id == payload.get("artifact_id")),
            None,
        )
        receipt = None
        if evidence is not None and evidence.receipt_id:
            receipt = ledger.replay_tool_receipt(project.id, evidence.receipt_id).receipt
        checks = {
            "connector_health_ok": connector.health == "ok",
            "tool_success": result.get("is_error") is False and payload.get("ok") is True,
            "browser_level_l2": payload.get("browser_level") == "L2",
            "evidence_persisted": evidence is not None,
            "artifact_persisted": artifact is not None,
            "source_receipt_persisted": bool(
                receipt is not None
                and receipt.project_id == project.id
                and receipt.run_id == run_record.id
                and receipt.session_id == run_record.sdk_session_id
            ),
            "artifact_links_evidence": bool(
                artifact is not None
                and evidence is not None
                and evidence.id in artifact.source_ids
                and artifact.run_id == run_record.id
            ),
            "screenshot_exists": bool(
                artifact is not None
                and (ledger.workspace_for_project(project.id) / artifact.path).is_file()
            ),
        }
        return {
            "kind": "browser_l2_acceptance",
            "generated_at": datetime.now(UTC).isoformat(),
            "url": url,
            "passed": all(checks.values()),
            "checks": checks,
            "browser_result": _reportable_result(payload),
            "preexisting_service_workers": worker_summary,
        }


async def _wait_for_cdp(cdp_endpoint: str, *, timeout_seconds: float = 10) -> None:
    parts = urlsplit(cdp_endpoint)
    if check_loopback_cdp_endpoint(cdp_endpoint):
        raise ValueError("--cdp-endpoint must be a safe loopback endpoint")
    assert parts.hostname is not None and parts.port is not None
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        try:
            _reader, writer = await asyncio.open_connection(parts.hostname, parts.port)
        except OSError:
            await asyncio.sleep(0.1)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise TimeoutError("Chrome CDP endpoint did not become ready")


async def run_with_dedicated_chrome(
    url: str,
    cdp_endpoint: str,
    timeout_seconds: float,
    chrome_binary: str,
) -> dict:
    """Launch real Google Chrome only for acceptance, then attach through L2."""

    parts = urlsplit(cdp_endpoint)
    if check_loopback_cdp_endpoint(cdp_endpoint):
        raise ValueError("--cdp-endpoint must be a safe loopback endpoint")
    assert parts.port is not None
    binary = await asyncio.to_thread(Path(chrome_binary).resolve)
    if not await asyncio.to_thread(binary.is_file):
        raise FileNotFoundError(f"Chrome binary not found: {binary}")
    with tempfile.TemporaryDirectory(prefix="ophira-browser-l2-chrome-") as profile:
        process = await asyncio.create_subprocess_exec(
            str(binary),
            "--headless=new",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={parts.port}",
            f"--user-data-dir={profile}",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-component-extensions-with-background-pages",
            "--disable-default-apps",
            "--disable-extensions",
            # The acceptance target may be a literal public HTTP address when
            # the host DNS resolver is intentionally proxied. Keep Chrome from
            # silently rewriting that explicit test URL before OPHIRA's relay
            # can validate and fetch it.
            "--disable-features=HttpsUpgrades",
            "--disable-sync",
            "--metrics-recording-only",
            "--no-service-autorun",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            await _wait_for_cdp(cdp_endpoint)
            return await run(url, cdp_endpoint, timeout_seconds)
        finally:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://example.com/")
    parser.add_argument("--cdp-endpoint", default="http://127.0.0.1:9222")
    parser.add_argument("--timeout-s", type=float, default=30.0)
    parser.add_argument("--launch-chrome", action="store_true")
    parser.add_argument(
        "--chrome-binary",
        default="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
    parser.add_argument("--report-dir", default=str(REPO_ROOT / "evals" / "reports"))
    args = parser.parse_args()

    try:
        if args.launch_chrome:
            report = await run_with_dedicated_chrome(
                args.url,
                args.cdp_endpoint,
                args.timeout_s,
                args.chrome_binary,
            )
        else:
            report = await run(args.url, args.cdp_endpoint, args.timeout_s)
    except Exception as exc:  # noqa: BLE001 - acceptance failures belong in the report
        report = {
            "kind": "browser_l2_acceptance",
            "generated_at": datetime.now(UTC).isoformat(),
            "url": args.url,
            "passed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    report_dir = Path(args.report_dir)
    await asyncio.to_thread(report_dir.mkdir, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"browser-l2-{stamp}.json"
    await asyncio.to_thread(
        path.write_text,
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        "utf-8",
    )
    print(json.dumps({**report, "report_path": str(path)}, ensure_ascii=False, indent=2))
    return 0 if report.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

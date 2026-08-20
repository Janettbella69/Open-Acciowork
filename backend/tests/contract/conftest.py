"""Provider contract suite plumbing (PRD §4.9 matrix).

Live-gated: set OPHIRA_CONTRACT_PROVIDER=<profile_id> to run against that configured
profile. Without it every test skips cleanly, so the suite is CI-safe. Each test appends
one structured outcome row; a session finalizer writes the machine-readable report to
evals/reports/ for the release archive.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ophira_backend.config import Settings
from ophira_backend.providers import ProviderRegistry

REPO_ROOT = Path(__file__).resolve().parents[3]
PROVIDER_ID = os.environ.get("OPHIRA_CONTRACT_PROVIDER", "").strip()

RESULTS: list[dict] = []


def record(case: str, status: str, detail: str = "", **extra) -> None:
    RESULTS.append(
        {
            "case": case,
            "status": status,  # PASS | FAIL | DEGRADED | NOT_TESTED
            "detail": detail,
            **extra,
        }
    )


@pytest.fixture(scope="session")
def provider_id() -> str:
    if not PROVIDER_ID:
        pytest.skip("OPHIRA_CONTRACT_PROVIDER is not set; contract suite is live-only")
    return PROVIDER_ID


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


@pytest.fixture(scope="session")
def registry(settings: Settings) -> ProviderRegistry:
    return ProviderRegistry(settings)


@pytest.fixture(scope="session")
def profile(registry: ProviderRegistry, provider_id: str):
    try:
        loaded = registry.get(provider_id)
    except KeyError:
        pytest.skip(f"provider profile {provider_id!r} is not configured")
    if not loaded.secret_configured:
        pytest.skip(f"provider profile {provider_id!r} has no stored credential")
    return loaded


@pytest.fixture(scope="session", autouse=True)
def write_report():
    yield
    if not PROVIDER_ID or not RESULTS:
        return
    report_dir = REPO_ROOT / "evals" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    payload = {
        "provider": PROVIDER_ID,
        "generated_at": datetime.now(UTC).isoformat(),
        "sdk_version": "0.2.82",
        "results": RESULTS,
    }
    json_path = report_dir / f"provider-contract-{PROVIDER_ID}-{stamp}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    lines = [
        f"# Provider 合约测试报告 · {PROVIDER_ID} · {stamp}",
        "",
        "SDK: claude-agent-sdk 0.2.82 · 依据 PRD v2.1 §4.9 合约矩阵",
        "",
        "| 用例 | 结果 | 说明 |",
        "|---|---|---|",
    ]
    for row in RESULTS:
        icon = {"PASS": "✅", "FAIL": "❌", "DEGRADED": "⚠️", "NOT_TESTED": "⏭️"}.get(
            row["status"], "?"
        )
        lines.append(f"| {row['case']} | {icon} {row['status']} | {row['detail']} |")
    md_path = report_dir / f"provider-contract-{PROVIDER_ID}-{stamp}.md"
    md_path.write_text("\n".join(lines) + "\n", "utf-8")

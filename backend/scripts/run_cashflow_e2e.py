#!/usr/bin/env python3
"""Run one real-provider Cashflow/Skill acceptance and preserve success or failure.

Uses the normal ASGI control plane and Green Path approval driver. No custom
model client, synthetic tool results or alternative Agent loop is involved.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from ophira_backend.cashflow import CashflowInput, calculate_cashflow  # noqa: E402
from ophira_backend.config import Settings  # noqa: E402
from ophira_backend.constants import (  # noqa: E402
    TOOL_CASHFLOW_PEAK,
    TOOL_GET_PROJECT_CONTEXT,
    TOOL_REGISTER_ARTIFACT,
    EventType,
    RunStatus,
)
from ophira_backend.events import redact  # noqa: E402
from ophira_backend.greenpath import PermissionPolicy, drive_run  # noqa: E402
from ophira_backend.main import create_app  # noqa: E402
from ophira_backend.models import RunRecord  # noqa: E402
from ophira_backend.providers import provider_readiness_failures  # noqa: E402

SKILL_ID = "ophira-core:landed-cost-analysis"
JSON_FILE = "cashflow-result.json"
MARKDOWN_FILE = "cashflow-report.md"
RESOURCE_PATHS = ("references/cashflow-inputs.md", "templates/cashflow-report.md")
FIXTURE = {
    "currency": "USD",
    "start_date": "2026-01-01",
    "end_date": "2026-01-02",
    "opening_cash": "600",
    "minimum_reserve": "200",
    "movements": [
        {
            "id": "day1-payment",
            "date": "2026-01-01",
            "direction": "outflow",
            "amount": "1000",
            "activity": "operating",
            "source_ref": "synthetic:payment",
        },
        {
            "id": "day1-receipt",
            "date": "2026-01-01",
            "direction": "inflow",
            "amount": "800",
            "activity": "operating",
            "source_ref": "synthetic:receipt",
        },
        {
            "id": "day2-payment",
            "date": "2026-01-02",
            "direction": "outflow",
            "amount": "600",
            "activity": "operating",
            "source_ref": "synthetic:second-payment",
        },
    ],
}
PROMPT = f"""请完成一个明确标为合成 fixture 的现金流验收任务，不代表真实业务、报价或回款。
使用 Plugin Skill `{SKILL_ID}`。Skill 加载后，用真实 Read 完整读取该 Skill 自己目录中的
references/cashflow-inputs.md 和 templates/cashflow-report.md，不要只凭摘要或猜测内容。
再用 Read 读取工作区 uploads/cashflow-fixture.json，将其中全部显式输入原样用于
`{TOOL_CASHFLOW_PEAK}`，此计算工具只调用一次，不增补假设或其他收付款。

用 Write 将该计算工具返回的完整 JSON 对象原样保存为工作区根目录 {JSON_FILE}，不是摘要，
不要省略 inputs、timeline、note 或其他字段。再依据已读取模板用 Write 写 {MARKDOWN_FILE}。
Markdown 必须说明这是“合成 fixture”、来源“未验证”、reserve 不列支出、先付后收仅是保守假设。
报告末尾增加以下审计表（值来自真实工具结果，保留四位小数，无单位后缀）：
| 指标 | 工具值 |
| --- | --- |
| funding_for_opening_and_daily_closes | 对应 funding 字段 |
| funding_if_outflows_settle_first | 对应 funding 字段 |
| closing_cash | 对应 totals 字段 |
| input_sha256 | 完整输入指纹 |

最后分别调用 `{TOOL_REGISTER_ARTIFACT}` 注册上述两个文件，source_ids=[]，
mime 分别 application/json 和 text/markdown。不要调用 Bash、外部搜索、外发、调度或子 Agent，
不要修改 Skill 原件。其他不相关工作无需执行，完成后简短确认。
"""


class CashflowPermissionPolicy(PermissionPolicy):
    def __init__(self, workspace: Path):
        super().__init__()
        self.workspace = workspace.resolve()

    def decide(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        key = "path" if tool_name == TOOL_REGISTER_ARTIFACT else "file_path"
        raw = tool_input.get(key)
        allowed = False
        if tool_name in {"Write", "Edit", TOOL_REGISTER_ARTIFACT} and isinstance(raw, str):
            path = (self.workspace / raw).resolve()
            allowed = path in {self.workspace / JSON_FILE, self.workspace / MARKDOWN_FILE}
        (self.allowed if allowed else self.denied).append(tool_name)
        return "allow_once" if allowed else "deny"


def tool_payload(response: Any) -> dict[str, Any] | None:
    if not isinstance(response, dict) or response.get("is_error") or response.get("isError"):
        return None
    if response.get("method") == "explicit_dated_cash_movements":
        return response
    for block in response.get("content", []):
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        try:
            parsed = json.loads(block.get("text", ""))
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and parsed.get("method") == "explicit_dated_cash_movements":
            return parsed
    return None


def _path(raw: Any, workspace: Path) -> Path | None:
    return (workspace / raw).resolve() if isinstance(raw, str) else None


def _successful(response: Any) -> bool:
    return response is not None and not (
        isinstance(response, dict) and (response.get("is_error") or response.get("isError"))
    )


def verify_run(ledger, run: RunRecord, skill_files: dict[str, Path], budget_usd: float) -> dict:
    events = ledger.list_events(run.id)
    workspace = ledger.workspace_for_project(run.project_id)
    project = ledger.get_project(run.project_id)
    starts = [item for item in events if item.type == EventType.TOOL_STARTED.value]
    completed = [item for item in events if item.type == EventType.TOOL_COMPLETED.value]
    valid_completions = [
        item
        for item in completed
        if _successful(item.data.get("tool_response"))
        and item.run_id == run.id
        and item.project_id == run.project_id
        and item.trace_id == run.trace_id
        and item.sdk_session_id == run.sdk_session_id
        and item.actor.type == "agent"
        and not item.actor.agent_id
    ]
    calls = [item for item in starts if item.data.get("tool_name") == TOOL_CASHFLOW_PEAK]
    finishes = [item for item in completed if item.data.get("tool_name") == TOOL_CASHFLOW_PEAK]
    payload = tool_payload(finishes[0].data.get("tool_response")) if len(finishes) == 1 else None
    expected = calculate_cashflow(FIXTURE)
    checks = {
        "run_completed": run.status == RunStatus.COMPLETED,
        "one_cashflow_call": len(calls) == len(finishes) == 1,
        "cashflow_result_bound_to_call": bool(
            len(calls) == len(finishes) == 1
            and calls[0].tool_use_id
            and calls[0].tool_use_id == finishes[0].tool_use_id
            and calls[0].sequence < finishes[0].sequence
            and calls[0].run_id == run.id
            and calls[0].project_id == run.project_id
            and calls[0].trace_id == run.trace_id
            and calls[0].sdk_session_id == run.sdk_session_id
            and calls[0].actor.type == "agent"
            and not calls[0].actor.agent_id
            and finishes[0] in valid_completions
            and finishes[0].sdk_session_id == run.sdk_session_id
            and finishes[0].actor.type == "agent"
            and not finishes[0].actor.agent_id
        ),
        "cashflow_matches_deterministic_fixture": bool(
            payload is not None
            and all(payload.get(key) == value for key, value in expected.items())
        ),
        "skill_invoked": any(
            item.data.get("tool_name") == "Skill"
            and item.data.get("tool_input", {}).get("skill") == SKILL_ID
            for item in valid_completions
        ),
        "no_skill_approval": not any(
            item.type == EventType.PERMISSION_REQUESTED.value
            and item.data.get("tool_name") == "Skill"
            for item in events
        ),
        "no_subagents": not any(item.type.startswith("subagent.") for item in events),
        "no_unrelated_tools": all(
            item.data.get("tool_name")
            in {
                "Skill",
                "Read",
                "Write",
                "Edit",
                "ToolSearch",
                "TodoWrite",
                TOOL_CASHFLOW_PEAK,
                TOOL_REGISTER_ARTIFACT,
                TOOL_GET_PROJECT_CONTEXT,
            }
            for item in starts
        ),
        "events_contiguous": [item.sequence for item in events] == list(range(1, len(events) + 1)),
    }
    try:
        checks["fixture_inputs_unchanged"] = len(calls) == 1 and CashflowInput.model_validate(
            calls[0].data.get("tool_input")
        ) == CashflowInput.model_validate(FIXTURE)
    except ValueError:
        checks["fixture_inputs_unchanged"] = False
    for relative in RESOURCE_PATHS:
        target = skill_files.get(relative)
        checks[f"read_skill:{relative}"] = bool(
            target
            and any(
                item.data.get("tool_name") == "Read"
                and _path(item.data.get("tool_input", {}).get("file_path"), workspace)
                == target.resolve()
                and item.data.get("tool_input", {}).get("offset", 1) == 1
                and item.data.get("tool_input", {}).get("limit", 10000)
                >= len(target.read_text(encoding="utf-8").splitlines())
                and "cashflow_peak"
                in json.dumps(item.data.get("tool_response"), ensure_ascii=False)
                for item in valid_completions
            )
        )
    checks["read_uploaded_fixture"] = any(
        item.data.get("tool_name") == "Read"
        and _path(item.data.get("tool_input", {}).get("file_path"), workspace)
        == (workspace / "uploads/cashflow-fixture.json").resolve()
        for item in valid_completions
    )
    saved = None
    try:
        saved = json.loads((workspace / JSON_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    checks["json_exactly_matches_server_tool_result"] = payload is not None and saved == payload
    try:
        markdown = (workspace / MARKDOWN_FILE).read_text(encoding="utf-8")
    except OSError:
        markdown = ""
    rows = {}
    for line in markdown.splitlines():
        cells = [value.strip().strip("`") for value in line.strip().strip("|").split("|")]
        if len(cells) == 2:
            rows[cells[0]] = cells[1]
    checks["markdown_numbers_match_tool"] = rows == rows | {
        "funding_for_opening_and_daily_closes": "400.0000",
        "funding_if_outflows_settle_first": "600.0000",
        "closing_cash": "-200.0000",
        "input_sha256": expected["input_sha256"],
    }
    checks["markdown_states_synthetic_unverified_scope"] = (
        "合成 fixture" in markdown and "未验证" in markdown
    )
    for filename in (JSON_FILE, MARKDOWN_FILE):
        path = workspace / filename
        checks[f"sdk_wrote:{filename}"] = any(
            item.data.get("tool_name") == "Write"
            and _path(item.data.get("tool_input", {}).get("file_path"), workspace) == path.resolve()
            for item in valid_completions
        )
        artifacts = [
            item for item in project.artifacts if item.path == filename and item.run_id == run.id
        ]
        checks[f"registered:{filename}"] = bool(
            len(artifacts) == 1
            and path.is_file()
            and not path.is_symlink()
            and artifacts[0].checksum == hashlib.sha256(path.read_bytes()).hexdigest()
            and any(
                item.data.get("tool_name") == TOOL_REGISTER_ARTIFACT
                and item.data.get("tool_input", {}).get("path") == filename
                for item in valid_completions
            )
        )
    results = [item for item in events if item.type == EventType.RUN_RESULT.value]
    latest = results[-1].data if results else {}
    cost = latest.get("total_cost_usd")
    checks["sdk_result_success"] = latest.get("is_error") is False
    checks["sdk_budget_bounded"] = isinstance(run.sdk_budget_usd, int | float) and (
        0 < run.sdk_budget_usd <= budget_usd + 1e-9
    )
    checks["reported_list_price_cost_bounded"] = (
        isinstance(cost, int | float)
        and not isinstance(cost, bool)
        and math.isfinite(cost)
        and 0 <= cost <= budget_usd
    )
    models = sorted(
        {
            item.data["model"]
            for item in events
            if item.type == EventType.ASSISTANT_MESSAGE_COMPLETED.value
            and isinstance(item.data.get("model"), str)
        }
    )
    checks["real_deepseek_model_observed"] = bool(models) and all(
        value.lower().startswith("deepseek") for value in models
    )
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "failed_checks": [key for key, value in checks.items() if not value],
        "run_id": run.id,
        "project_id": run.project_id,
        "served_models": models,
        "cost_usd_sdk_list_price": cost,
        "sdk_budget_usd": run.sdk_budget_usd,
        "expected_input_sha256": expected["input_sha256"],
        "event_count": len(events),
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(redact(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def prepare_report(report_dir: Path) -> None:
    report_dir.mkdir(parents=True, exist_ok=False)
    write_json(report_dir / "fixture.json", FIXTURE)
    (report_dir / "prompt.md").write_text(PROMPT, encoding="utf-8")


def archive_project(application, project_id: str, report_dir: Path, budget_usd: float) -> dict:
    """Archive each independent evidence source even when another source fails."""
    summary: dict[str, Any] = {"passed": False, "archive_errors": []}
    ledger = application.state.ledger

    def attempt(label, action):
        try:
            return action()
        except Exception as exc:
            summary["archive_errors"].append(f"{label}: {type(exc).__name__}: {redact(str(exc))}")
            return None

    project = attempt("read_project", lambda: ledger.get_project(project_id))
    if project is not None:
        attempt(
            "write_project",
            lambda: write_json(
                report_dir / "project.json",
                project.model_dump(mode="json"),
            ),
        )
    attempt(
        "copy_workspace",
        lambda: shutil.copytree(
            ledger.workspace_for_project(project_id),
            report_dir / "workspace",
            symlinks=True,
        ),
    )
    runs = attempt("read_runs", lambda: ledger.list_runs(project_id))
    if runs:
        run = runs[0]
        attempt(
            "write_run", lambda: write_json(report_dir / "run.json", run.model_dump(mode="json"))
        )
        events = attempt("read_events", lambda: ledger.list_events(run.id))
        if events is not None:
            # Ledger events already passed the production redaction boundary.
            attempt(
                "write_events",
                lambda: (report_dir / "events.jsonl").write_text(
                    "".join(
                        json.dumps(item.model_dump(mode="json"), ensure_ascii=False) + "\n"
                        for item in events
                    ),
                    encoding="utf-8",
                ),
            )
        try:
            with application.state.skills.runtime_snapshot_lease(run.skill_snapshot) as view:
                roots = [
                    Path(item["path"]) / "skills/landed-cost-analysis"
                    for item in view.plugin_paths
                    if Path(item["path"]).name == "ophira-core"
                ]
                files = {
                    relative: root / relative
                    for root in roots
                    for relative in RESOURCE_PATHS
                    if (root / relative).is_file()
                }
                summary["skill_resource_sha256"] = {
                    relative: hashlib.sha256(path.read_bytes()).hexdigest()
                    for relative, path in files.items()
                }
                for relative, path in files.items():
                    destination = report_dir / "skill-resources" / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, destination)
                summary.update(verify_run(ledger, run, files, budget_usd))
        except Exception as exc:
            summary["verification_error"] = f"{type(exc).__name__}: {redact(str(exc))}"
    if summary["archive_errors"] or summary.get("verification_error"):
        summary["passed"] = False
    return summary


async def run_acceptance(
    report_dir: Path, *, provider_id: str, budget_usd: float, timeout_s: float
):
    prepare_report(report_dir)
    summary: dict[str, Any] = {
        "passed": False,
        "provider": provider_id,
        "budget_usd_sdk_list_price": budget_usd,
        "timeout_seconds": timeout_s,
    }
    application = None
    project_id = None
    policy = None
    try:
        settings = Settings(max_budget_usd=budget_usd)
        application = create_app(settings)
        ledger = application.state.ledger
        profile = application.state.providers.get(provider_id)
        failures = provider_readiness_failures(profile)
        if failures:
            raise RuntimeError("Provider is not preflight-ready: " + ", ".join(failures))
        # The runtime multiplies operator units by this profile's scale. Freeze
        # a list-price cap without changing the durable Provider configuration.
        settings.max_budget_usd = min(1000, budget_usd / profile.budget_scale)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://localhost",
            timeout=httpx.Timeout(60),
        ) as client:
            created = await client.post(
                "/api/projects",
                json={
                    "name": "Cashflow Skill real-provider acceptance",
                    "objective": "Synthetic cash timing fixture; verify Skill and editable outputs",
                },
            )
            created.raise_for_status()
            project_id = created.json()["id"]
            upload = await client.post(
                f"/api/projects/{project_id}/files",
                params={"filename": "cashflow-fixture.json", "mime": "application/json"},
                content=json.dumps(FIXTURE, ensure_ascii=False).encode(),
            )
            upload.raise_for_status()
            policy = CashflowPermissionPolicy(ledger.workspace_for_project(project_id))
            await drive_run(
                client,
                ledger,
                project_id,
                PROMPT,
                provider_id=provider_id,
                policy=policy,
                timeout_s=timeout_s,
            )
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {redact(str(exc))}"
    finally:
        if application is not None:
            try:
                await application.state.runs.stop()
            except Exception as exc:
                summary["cleanup_error"] = f"{type(exc).__name__}: {redact(str(exc))}"
            if project_id is not None:
                try:
                    summary.update(archive_project(application, project_id, report_dir, budget_usd))
                except Exception as exc:
                    summary["archive_error"] = f"{type(exc).__name__}: {redact(str(exc))}"
        if policy is not None:
            summary["policy_allowed"] = policy.allowed
            summary["policy_denied"] = policy.denied
        if any(key.endswith("error") and value for key, value in summary.items()):
            summary["passed"] = False
        summary["generated_at"] = datetime.now(UTC).isoformat()
        write_json(report_dir / "summary.json", summary)
    return summary


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["deepseek"], default="deepseek")
    parser.add_argument("--budget-usd", type=float, default=2)
    parser.add_argument("--timeout-s", type=float, default=900)
    parser.add_argument("--report-dir", type=Path, default=REPO_ROOT / "evals/reports")
    args = parser.parse_args()
    if not 0 < args.budget_usd <= 2 or not 0 < args.timeout_s <= 900:
        parser.error("budget must be within (0, 2] SDK list-price USD and timeout within (0, 900]s")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    report_dir = args.report_dir / f"cashflow-e2e-{stamp}"
    summary = await run_acceptance(
        report_dir, provider_id=args.provider, budget_usd=args.budget_usd, timeout_s=args.timeout_s
    )
    print(json.dumps({**summary, "report_dir": str(report_dir)}, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

#!/usr/bin/env python3
"""Bounded real-SDK Deep Research acceptance using only isolated synthetic files.

Run through the normal control plane and existing approval driver. Archive the
actual event stream, resources, project and immutable report on success or failure.
No alternate model client, fabricated receipts or runtime loop is used.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import math
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend/src"))

from ophira_backend.config import Settings  # noqa: E402
from ophira_backend.constants import (  # noqa: E402
    DELEGATION_TOOL,
    READ_ONLY_BUILTIN_TOOLS,
    RESEARCH_UNITS,
    TOOL_GET_PROJECT_CONTEXT,
    TOOL_RECORD_DECISION,
    TOOL_RECORD_EVIDENCE_BATCH,
    TOOL_REGISTER_ARTIFACT,
    TOOL_SEARCH_EVIDENCE,
    EventType,
    RunMode,
    RunStatus,
)
from ophira_backend.events import redact  # noqa: E402
from ophira_backend.greenpath import PermissionPolicy, drive_run, sse_replay_sequences  # noqa: E402
from ophira_backend.providers import ProviderRegistry, provider_readiness_failures  # noqa: E402

REPORT = "deep-research-report.md"
FIXTURES = {
    "quote_en.md": """# Synthetic supplier quote A — unverified user-supplied material
Original issuer: Fixture Supplier. SKU: TRACE-CUP. Quote date: 2026-09-01.
Unit price: USD 6.80. Minimum order quantity: 500 units.
Freight, certification and validity are not specified. This is not a real quotation.
""",
    "quote_zh.md": """# 合成供应商报价 B — 未验证的用户材料
原始出具方：Fixture Supplier。SKU：TRACE-CUP。报价日期：2026-09-03。
单价：USD 7.20。最低起订量：300 件。
运费、认证及有效期未说明。本材料不是真实报价，也未说明是否替代旧报价。
""",
    "repost_fr.md": """# Reprise française — document synthétique, non vérifié
Traduction et republication de uploads/quote_en.md, PAS une source indépendante.
Fixture Supplier, TRACE-CUP, devis du 2026-09-01 : USD 6.80, minimum 500 unités.
Les frais de transport, la certification et la validité ne sont pas précisés.
""",
}
PROMPT = f"""执行 Deep Research 验收：以下均是合成 fixture、未验证的用户材料，不能当作真实商业报价。
仅使用 uploads/quote_en.md、uploads/quote_zh.md、uploads/repost_fr.md。不要搜索互联网、
使用 Bash、发消息、创建自动化、调用其他外部工具或修改输入文件。

在同一条调用消息中并行发起且仅发起一次三个原生 Agent：{', '.join(RESEARCH_UNITS)}。
每次传 run_in_background=false，主 Agent 必须等三个返回后再综合。每个研究单元都要实际
Read 三个文件，分别着重映射原始来源、多语言重复来源、日期/价格/MOQ 冲突。
每次委派的 prompt 简述该单元重点即可，限160字；不要复制材料数值或重复整段来源包协议。
要求各单元遵循来源包协议：只返回 JSON，protocol="ophira-research-v1"、claims 数组、
unknowns 数组；每条 claim 有 claim/evidence_kind/source_locator/observed_at/excerpt/
language/conflicts/limitations。不写任何文件或账本，也不创建其他 Agent。

等三份结果返回后，主 Agent 亲自用 Read 重新核对三个原文件，然后：
1. 用 `{TOOL_RECORD_EVIDENCE_BATCH}` 一次提交两条原始报价的证据，各自包含价格、MOQ、
日期、短摘录、行定位，kind=user_statement，source_type=file，source_path 为对应 uploads
路径，source_title 自拟，confidence=0.5，supports_hypothesis_ids=[]，recorded_by=research-main。
在同一个 entries 数组末尾原样重复第一条，验证现有接口幂等返回相同 id。法语转载不得另建证据。
2. 用 `{TOOL_RECORD_DECISION}` 记录一个 plan：先澄清报价差异与缺失条件，暂不选择报价。
引用以上真实返回的两个不同 evidence_ids，说明价格/MOQ/日期差异，保留未知条件与后续动作。
3. Write {REPORT}，中文说明合成 fixture、未验证、两份报价数据、日期、冲突、去重依据、
三个研究单元的工作、未知条件，以及两个实际 evidence IDs 和决策 ID。
4. `{TOOL_REGISTER_ARTIFACT}` 注册此文件，mime=text/markdown，source_ids 引用两个证据 ID。
不要伪造 source_receipt，不要把子 Agent 总结当成已经入账的事实。完成后简短确认。
"""


class ResearchPermissionPolicy(PermissionPolicy):
    def __init__(self, workspace: Path):
        super().__init__()
        self.workspace = workspace.resolve()

    def decide(self, tool_name, tool_input):
        allowed = tool_name in {TOOL_RECORD_EVIDENCE_BATCH, TOOL_RECORD_DECISION}
        if tool_name in {"Write", "Edit", TOOL_REGISTER_ARTIFACT}:
            raw = tool_input.get("path" if tool_name == TOOL_REGISTER_ARTIFACT else "file_path")
            allowed = isinstance(raw, str) and (self.workspace / raw).resolve() == (
                self.workspace / REPORT
            )
        (self.allowed if allowed else self.denied).append(tool_name)
        return "allow_once" if allowed else "deny"


def source_packet(response):
    """Inspect the native tool's returned content, never its echoed input prompt."""
    if isinstance(response, dict):
        if response.get("is_error") or response.get("isError"):
            return None
        if response.get("protocol") == "ophira-research-v1":
            return response
        return source_packet(response.get("content", response.get("text")))
    if isinstance(response, list):
        return next((packet for part in response if (packet := source_packet(part))), None)
    if isinstance(response, str) and len(response) <= 200_000:
        decoder = json.JSONDecoder()
        for index, char in enumerate(response):
            if char != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(response[index:])
            except ValueError:
                continue
            if isinstance(parsed, dict) and parsed.get("protocol") == "ophira-research-v1":
                return parsed
    return None


def verify_run(run, project, events, resources, markdown, *, budget_usd):
    successful = [event for event in events if event.type == EventType.TOOL_COMPLETED
                  and event.data.get("tool_response") is not None
                  and not (isinstance(event.data["tool_response"], dict)
                           and (event.data["tool_response"].get("is_error")
                                or event.data["tool_response"].get("isError")))]
    starts = [event for event in events if event.type == EventType.SUBAGENT_STARTED]
    stops = [event for event in events if event.type == EventType.SUBAGENT_STOPPED]
    observed = {event.data.get("agent_id"): event.data.get("agent_type") for event in starts}
    main = [event for event in successful if event.actor.type == "agent"
            and not event.actor.agent_id]
    readonly = {*READ_ONLY_BUILTIN_TOOLS, TOOL_GET_PROJECT_CONTEXT, TOOL_SEARCH_EVIDENCE}
    evidence = [entry for entry in project.evidence if entry.source_run_id == run.id]
    ids = {entry.id for entry in evidence}
    decisions = [entry for entry in project.decisions if entry.source_run_id == run.id]
    results = [event.data for event in events if event.type == EventType.RUN_RESULT]
    result = results[-1] if results else {}
    cost = result.get("total_cost_usd")
    models = sorted({event.data["model"] for event in events
                     if event.type == EventType.ASSISTANT_MESSAGE_COMPLETED
                     and isinstance(event.data.get("model"), str)})
    checks = {
        "completed_with_successful_sdk_result": run.status == RunStatus.COMPLETED
        and result.get("is_error") is False,
        "deep_research_mode": run.mode == RunMode.DEEP_RESEARCH,
        "event_identity_and_sequence": all(
            event.run_id == run.id and event.project_id == project.id
            and event.trace_id == run.trace_id and event.sequence == index
            and event.id == f"{run.id}:{index}"
            for index, event in enumerate(events, 1)
        ),
        "exactly_three_native_research_units": len(starts) == len(observed) == 3
        and None not in observed and set(observed.values()) == set(RESEARCH_UNITS),
        "all_three_stopped": len(stops) == 3
        and {event.data.get("agent_id") for event in stops} == set(observed),
        "native_execution_overlapped": bool(starts and stops)
        and max(event.sequence for event in starts) < min(event.sequence for event in stops),
        "children_remained_readonly": all(event.data.get("tool_name") in readonly
                                         for event in successful if event.actor.agent_id),
        "no_unrelated_completed_tools": all(event.data.get("tool_name") in {
            "Read", "Glob", "Grep", "Write", "Edit", DELEGATION_TOOL, "ToolSearch", "TodoWrite",
            "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", TOOL_GET_PROJECT_CONTEXT,
            TOOL_SEARCH_EVIDENCE, TOOL_RECORD_EVIDENCE_BATCH, TOOL_RECORD_DECISION,
            TOOL_REGISTER_ARTIFACT,
        } for event in successful),
        "two_original_sources_only": len(evidence) == len(ids) == 2
        and {entry.source_path for entry in evidence}
        == {"uploads/quote_en.md", "uploads/quote_zh.md"}
        and all(entry.kind == "user_statement" and entry.source_type == "file"
                for entry in evidence),
        "conflicting_quote_values_preserved": any("6.80" in entry.claim for entry in evidence)
        and any("7.20" in entry.claim for entry in evidence),
        "main_wrote_batch_after_three_returns": any(
            event.data.get("tool_name") == TOOL_RECORD_EVIDENCE_BATCH
            and stops and event.sequence > max(stop.sequence for stop in stops)
            and len(event.data.get("tool_input", {}).get("entries", [])) == 3
            for event in main
        ),
        "decision_links_both_sources": len(decisions) == 1
        and set(decisions[0].evidence_ids) == ids and decisions[0].decision_type == "plan",
        "report_cites_ledger_and_scope": len(ids) == 2 and len(decisions) == 1
        and all(identifier in markdown for identifier in [*ids, decisions[0].id])
        and all(term in markdown for term in
                ("合成 fixture", "未验证", "6.80", "7.20", "500", "300", "冲突", "去重",
                 "2026-09-01", "2026-09-03", *RESEARCH_UNITS)),
        "sdk_budget_frozen_and_cost_reported": run.sdk_budget_usd is not None
        and 0 < run.sdk_budget_usd <= budget_usd + 1e-9
        and isinstance(cost, int | float) and not isinstance(cost, bool)
        and math.isfinite(cost) and 0 <= cost <= budget_usd,
        "no_turn_limit_and_original_deadline": run.sdk_turn_limit is None
        and run.sdk_wall_clock_deadline is not None and run.finished_at is not None
        and run.finished_at <= run.sdk_wall_clock_deadline,
        "real_deepseek_model_observed": bool(models)
        and all(model.lower().startswith("deepseek") for model in models),
        "resources_match_native_observations": resources.get("observations_available") is True
        and resources.get("run_id") == run.id and resources.get("trace_id") == run.trace_id
        and resources.get("project_id") == project.id and resources.get("status") == run.status
        and len(resources.get("agents", [])) == len(observed)
        and {item["agent_id"] for item in resources.get("agents", [])} == set(observed),
    }
    for identifier, unit in observed.items():
        checks[f"child_read:{unit}"] = all(any(
            event.actor.agent_id == identifier and event.data.get("tool_name") == "Read"
            and str(event.data.get("tool_input", {}).get("file_path", "")).removeprefix(
                "./").endswith(f"uploads/{name}") for event in successful
        ) for name in FIXTURES)
    required_fields = {"claim", "evidence_kind", "source_locator", "observed_at", "excerpt",
                       "language", "conflicts", "limitations"}
    for unit in RESEARCH_UNITS:
        returns = [event for event in main if event.data.get("tool_name") == DELEGATION_TOOL
                   and event.data.get("tool_input", {}).get("subagent_type") == unit]
        packet = source_packet(returns[0].data.get("tool_response")) if len(returns) == 1 else None
        checks[f"source_packet:{unit}"] = bool(
            packet and isinstance(packet.get("unknowns"), list)
            and isinstance(packet.get("claims"), list) and packet["claims"]
            and all(isinstance(claim, dict) and required_fields <= claim.keys()
                    for claim in packet["claims"])
        )
    for name in FIXTURES:
        checks[f"main_rechecked:{name}"] = any(
            event.data.get("tool_name") == "Read"
            and str(event.data.get("tool_input", {}).get("file_path", "")).removeprefix(
                "./").endswith(f"uploads/{name}")
            and any(batch.data.get("tool_name") == TOOL_RECORD_EVIDENCE_BATCH
                    and event.sequence < batch.sequence for batch in main)
            for event in main if stops and event.sequence > max(stop.sequence for stop in stops)
        )
    return {"passed": all(checks.values()), "checks": checks,
            "failed_checks": [key for key, value in checks.items() if not value],
            "run_id": run.id, "project_id": project.id, "served_models": models,
            "cost_usd_sdk_estimate": cost, "event_count": len(events)}


def write_json(path, payload):
    path.write_text(json.dumps(redact(payload), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def write_event_archive(report_dir, events):
    raw = "".join(json.dumps(redact(event), ensure_ascii=False) + "\n"
                  for event in events).encode("utf-8")
    compressed = gzip.compress(raw, mtime=0)
    (report_dir / "events.jsonl.gz").write_bytes(compressed)
    write_json(report_dir / "event-archive.json", {
        "file": "events.jsonl.gz", "encoding": "utf-8", "compression": "gzip",
        "uncompressed_bytes": len(raw), "compressed_bytes": len(compressed),
        "uncompressed_sha256": hashlib.sha256(raw).hexdigest(),
        "compressed_sha256": hashlib.sha256(compressed).hexdigest(),
    })


async def run_acceptance(report_dir, *, provider_id, budget_usd, timeout_s):
    report_dir.mkdir(parents=True, exist_ok=False)
    (report_dir / "prompt.md").write_text(PROMPT, encoding="utf-8")
    write_json(report_dir / "fixtures.json", FIXTURES)
    summary = {"passed": False, "provider": provider_id, "sdk_budget_usd": budget_usd,
               "timeout_seconds": timeout_s, "scope": "synthetic uploaded sources only"}
    application, project_id, watcher = None, None, None
    resource_samples = []
    stop_watching = asyncio.Event()
    with tempfile.TemporaryDirectory(prefix="ophira-research-") as temporary:
        try:
            original = ProviderRegistry(Settings())
            profile = original.get(provider_id)
            failures = provider_readiness_failures(profile)
            if failures:
                raise RuntimeError("Provider is not preflight-ready: " + ", ".join(failures))
            secret = original.vault.get(provider_id)
            settings = Settings(data_dir=Path(temporary), max_turns=None,
                                max_budget_usd=min(1000, budget_usd / profile.budget_scale),
                                deep_research_timeout_seconds=timeout_s, auth_mode="local_only",
                                anthropic_auth_token=None, anthropic_api_key=None)
            isolated = ProviderRegistry(settings)
            # Preserve the existing verified profile, never fake preflight.
            isolated._write(profile)
            isolated.vault.set(provider_id, secret)
            # The module-level ASGI app also initializes under this isolated root.
            previous = os.environ.get("OPHIRA_DATA_DIR")
            os.environ["OPHIRA_DATA_DIR"] = temporary
            try:
                from ophira_backend.main import create_app
            finally:
                if previous is None:
                    os.environ.pop("OPHIRA_DATA_DIR", None)
                else:
                    os.environ["OPHIRA_DATA_DIR"] = previous
            application = create_app(settings)
            ledger = application.state.ledger
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                         base_url="http://localhost", timeout=60) as client:
                response = await client.post("/api/projects", json={
                    "name": "Deep Research real-SDK acceptance",
                    "objective": "Synthetic bilingual quote conflict and source deduplication",
                })
                response.raise_for_status()
                project_id = response.json()["id"]
                for name, contents in FIXTURES.items():
                    uploaded = await client.post(f"/api/projects/{project_id}/files",
                                                 params={"filename": name, "mime": "text/markdown"},
                                                 content=contents.encode())
                    uploaded.raise_for_status()

                async def observe():
                    while not stop_watching.is_set():
                        runs = ledger.list_runs(project_id)
                        if runs:
                            response = await client.get(f"/api/runs/{runs[0].id}/resources")
                            response.raise_for_status()
                            sample = response.json()
                            if not resource_samples or sample["computed_through_sequence"] != (
                                resource_samples[-1]["computed_through_sequence"]
                            ) or sample["status"] != resource_samples[-1]["status"]:
                                resource_samples.append(sample)
                        try:
                            await asyncio.wait_for(stop_watching.wait(), timeout=2)
                        except TimeoutError:
                            pass

                watcher = asyncio.create_task(observe())
                await drive_run(client, ledger, project_id, PROMPT, provider_id=provider_id,
                                mode=RunMode.DEEP_RESEARCH, timeout_s=timeout_s + 30,
                                policy=ResearchPermissionPolicy(
                                    ledger.workspace_for_project(project_id)))
                run = ledger.list_runs(project_id)[0]
                task = application.state.runs._tasks.get(run.id)
                if task is not None:
                    await asyncio.wait_for(asyncio.shield(task), timeout=30)
                checkpoint = len(ledger.list_events(run.id)) // 2
                replay = await asyncio.wait_for(sse_replay_sequences(
                    client, run.id, checkpoint, project_id=project_id), timeout=30)
                summary["sse_replay_complete"] = replay == list(
                    range(checkpoint + 1, len(ledger.list_events(run.id)) + 1))
                response = await client.get(f"/api/runs/{run.id}/resources")
                response.raise_for_status()
                resource_samples.append(response.json())
        except Exception as exc:
            summary["error"] = f"{type(exc).__name__}: {redact(str(exc))}"
        finally:
            stop_watching.set()
            if watcher is not None:
                try:
                    await watcher
                except Exception as exc:
                    summary["observation_error"] = str(redact(str(exc)))
            if application is not None:
                await application.state.runs.stop()
            if application is not None and project_id is not None:
                try:
                    ledger = application.state.ledger
                    project = ledger.get_project(project_id)
                    write_json(report_dir / "project.json", project)
                    run = ledger.list_runs(project_id)[0]
                    write_json(report_dir / "run.json", run)
                    events = ledger.list_events(run.id)
                    write_event_archive(report_dir, events)
                    artifacts = [artifact for artifact in project.artifacts
                                 if artifact.path == REPORT and artifact.run_id == run.id]
                    markdown = ""
                    if artifacts:
                        _, raw = ledger.capture_artifact(project_id, artifacts[-1].id)
                        (report_dir / REPORT).write_bytes(raw)
                        markdown = raw.decode("utf-8")
                    summary.update(verify_run(run, project, events,
                                              resource_samples[-1] if resource_samples else {},
                                              markdown, budget_usd=budget_usd))
                    summary["checks"]["immutable_artifact_captured"] = bool(artifacts)
                    summary["checks"]["sse_replay_complete"] = summary.get(
                        "sse_replay_complete", False)
                    summary["checks"]["live_resources_observed"] = any(
                        sample["status"] == "running" and sample["agents"]
                        for sample in resource_samples)
                    summary["failed_checks"] = [key for key, value in summary["checks"].items()
                                                 if not value]
                except Exception as exc:
                    summary["archive_error"] = f"{type(exc).__name__}: {redact(str(exc))}"
            write_json(report_dir / "resource-samples.json", resource_samples)
            summary["passed"] = bool(summary.get("checks")) and not summary.get("failed_checks")
            if any(key.endswith("error") for key in summary):
                summary["passed"] = False
            summary["generated_at"] = datetime.now(UTC).isoformat()
            write_json(report_dir / "summary.json", summary)
    return summary


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["deepseek"], default="deepseek")
    parser.add_argument("--budget-usd", type=float, default=2)
    parser.add_argument("--timeout-s", type=int, default=600)
    parser.add_argument("--report-dir", type=Path, default=REPO_ROOT / "evals/reports")
    args = parser.parse_args()
    if not 0 < args.budget_usd <= 2 or not 60 <= args.timeout_s <= 900:
        parser.error("budget must be in (0, 2] SDK estimate USD; timeout must be 60–900s")
    report_dir = args.report_dir / f"research-e2e-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}"
    summary = await run_acceptance(report_dir, provider_id=args.provider,
                                   budget_usd=args.budget_usd, timeout_s=args.timeout_s)
    print(json.dumps({**summary, "report_dir": str(report_dir)}, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

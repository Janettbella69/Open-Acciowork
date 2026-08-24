"""Green Path E2E harness (PRD §6.2, backlog P0-E2E-01; reused as the eval L2 driver).

One fixed cross-border task must exercise, on a REAL provider endpoint, the full chain:
Read -> Web -> Skill -> domain MCP -> Bash (sandbox auto-approved) -> Write -> Edit ->
evidence/decision ledger writes -> a denied marker Write probe followed by graceful
continuation -> session resume (a context-dependent follow-up, not a self-contained
instruction) -> SSE replay from an arbitrary checkpoint.
`verify_events` is pure and unit-testable offline.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .constants import EventType
from .events import redact
from .models import EventEnvelope, OpportunityProject

DENY_MARKER = "OPHIRA_DENY_MARKER"

GREEN_PATH_PROMPT = f"""这是 OPHIRA Green Path 端到端验收任务。请严格按顺序完成每一步，不要合并、不要跳步、不要创建子 Agent：

1. 触发 Skill `ophira-core:landed-cost-analysis`，按它的方法执行本任务的成本分析部分。
2. 用 Read 依次读取工作区 `uploads/quotes.csv` 和 `uploads/constraints.md`。
3. 用 WebSearch 搜索一次 "portable blender EU market"。如果该工具在当前 Provider 不可用或调用失败：不要重试，记下失败原因，继续下一步。
4. 调用 `mcp__ophira_commerce_math__calculate_landed_cost`，参数：quantity=500, unit_price=6.8, freight=420, insurance=35, duty_rate=0.045, tax_rate=0.19, other_costs=120, currency="USD"。
5. 用 Bash 运行 `wc -l uploads/quotes.csv` 统计报价行数。
6. 用 Write 在工作区根目录创建 `greenpath-report.md`：包含到岸成本计算结果、报价行数、两个主要风险。
7. 用 Edit 在 `greenpath-report.md` 末尾追加一段 "## 修订记录" 并写一行修订说明。
8. 调用 `mcp__ophira_ledger__record_evidence` 记录一条证据：kind="user_statement"，claim 引用 constraints.md 中预算约束的原文，source_type="file"，source_path="uploads/constraints.md"，source_title="项目约束文件"，confidence=0.9，supports_hypothesis_ids=[]，recorded_by="greenpath-agent"。
9. 再调用一次 `mcp__ophira_ledger__record_evidence`：kind="community_signal"，claim 概述欧盟小家电市场存在公开统计口径，source_type="web"，source_url 使用 constraints.md 中列出的参考 URL，source_title="Eurostat 参考页（来自用户材料，未实时抓取）"，freshness_note="URL 来自用户提供材料，本次未实时抓取"，confidence=0.4，supports_hypothesis_ids=[]，recorded_by="greenpath-agent"。若第 3 步 WebSearch 成功，则改用真实搜索结果的 URL 并如实描述。
10. 调用 `mcp__ophira_ledger__record_decision` 记录 decision_type="test" 的决策：question 为"该便携榨汁杯品类是否值得小规模测试"，outcome 给出你的判断，rationale 简述，evidence_ids 引用第 8、9 步返回的证据 id，hypothesis_ids=[]，assumptions 至少 1 条，conditions 至少 1 条，next_actions 至少 2 条，made_by="greenpath-agent"。
11. 最后用 Write 在工作区创建文件 `{DENY_MARKER}.md`（内容写一行即可）。这个请求会被用户拒绝——被拒绝后不要重试、不要换写法，直接输出一段简短的中文总结并结束。
"""

# 教材第 10 章："真实用户不能这么说话……问这种前后有相互关联的语义。"
# 刻意不重复文件名、不复述上一轮做过什么——只有真的续上了上下文才答得出来，
# 从而把"session 续接通了"和"上下文真的延续了"两件事分开验证。
RESUME_PROMPT = (
    "刚才那个决策，你依据的证据我想再确认一下：把你引用的那几条证据的 claim 逐条列出来，"
    "并说明每条属于哪个 kind。然后把这段确认追加到你刚才写的那份报告末尾，"
    "并在最后单独加一行：\n\n`会话续接验证 OK`\n\n完成后回复一句确认即可。"
)

READ_TOOLS = {"Read"}
WEB_TOOLS = {"WebSearch", "WebFetch"}
FALLBACK_WEB_TOOL_PREFIX = "mcp__ophira_web__"
SKILL_TOOLS = {"Skill"}
BASH_TOOLS = {"Bash"}
WRITE_TOOLS = {"Write"}
EDIT_TOOLS = {"Edit"}

COVERAGE_KEYS = ("read", "web", "skill", "mcp", "bash", "write", "edit")


class PermissionPolicy:
    """Deny the marker write once and forever; allow everything else once.

    2026-08-24 起沙箱内 Bash 走 SDK 原生自动批准、不再产生权限请求，
    deny→continue 探针因此落在仍走 ask 的 Write 层（marker 文件名）。
    """

    def __init__(self) -> None:
        self.denied: list[str] = []
        self.allowed: list[str] = []

    def decide(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        serialized = json.dumps(tool_input, ensure_ascii=False, default=str)
        if tool_name in WRITE_TOOLS and DENY_MARKER in serialized:
            self.denied.append(tool_name)
            return "deny"
        self.allowed.append(tool_name)
        return "allow_once"


@dataclass
class GreenPathVerdict:
    tool_coverage: dict[str, bool] = field(default_factory=dict)
    web_degraded: bool = False
    subagent_events: int = 0
    permission_requests: int = 0
    permission_flow_ok: bool = False
    deny_then_continue_ok: bool = False
    sequence_contiguous: bool = False
    run_completed: bool = False
    result_is_error: bool | None = None
    served_models: list[str] = field(default_factory=list)
    total_cost_usd: float | None = None
    num_turns: int | None = None
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing


def _tool_name(event: EventEnvelope) -> str:
    name = event.data.get("tool_name")
    return name if isinstance(name, str) else ""


def _is_skill_use(event: EventEnvelope) -> bool:
    if _tool_name(event) in SKILL_TOOLS:
        return True
    tool_input = event.data.get("tool_input")
    return isinstance(tool_input, dict) and "ophira-core:" in json.dumps(
        tool_input, ensure_ascii=False, default=str
    )


def verify_events(
    events: list[EventEnvelope],
    *,
    expect_deny: bool = True,
    required_coverage: tuple[str, ...] | None = None,
) -> GreenPathVerdict:
    """Pure assertion pass over one run's full ordered event list.

    `required_coverage` narrows which tool chains must appear (eval L2 tasks assert a
    subset); the Green Path gate itself keeps the full seven-link default.
    """
    verdict = GreenPathVerdict()
    coverage = dict.fromkeys(COVERAGE_KEYS, False)
    required = COVERAGE_KEYS if required_coverage is None else tuple(required_coverage)

    tool_event_types = {
        EventType.TOOL_PLANNED.value,
        EventType.TOOL_STARTED.value,
        EventType.TOOL_COMPLETED.value,
    }
    requested: dict[str, int] = {}
    resolved: dict[str, str] = {}
    timed_out: set[str] = set()
    deny_sequence: int | None = None
    last_sequence = 0
    web_failures = 0
    native_web_seen = False
    fallback_web_seen = False

    for event in events:
        last_sequence = max(last_sequence, event.sequence)
        if event.type == EventType.SUBAGENT_STARTED.value or event.actor.type == "subagent":
            verdict.subagent_events += 1
        if event.type in tool_event_types:
            name = _tool_name(event)
            if name in READ_TOOLS:
                coverage["read"] = True
            if name in WEB_TOOLS:
                coverage["web"] = True
                native_web_seen = True
            if name.startswith(FALLBACK_WEB_TOOL_PREFIX):
                # FR-23 sanctioned fallback counts as web coverage, flagged degraded.
                coverage["web"] = True
                fallback_web_seen = True
            if _is_skill_use(event):
                coverage["skill"] = True
            if name.startswith("mcp__ophira_"):
                coverage["mcp"] = True
            if name in BASH_TOOLS:
                coverage["bash"] = True
            if name in WRITE_TOOLS:
                coverage["write"] = True
            if name in EDIT_TOOLS:
                coverage["edit"] = True
        if event.type == EventType.TOOL_FAILED.value and _tool_name(event) in WEB_TOOLS:
            web_failures += 1
        if event.type == EventType.PERMISSION_REQUESTED.value:
            request_id = event.data.get("request_id")
            if isinstance(request_id, str):
                requested[request_id] = event.sequence
        if event.type == EventType.PERMISSION_RESOLVED.value:
            request_id = event.data.get("request_id")
            decision = event.data.get("decision")
            if isinstance(request_id, str):
                resolved[request_id] = str(decision)
                if decision == "deny" and deny_sequence is None:
                    deny_sequence = event.sequence
        if event.type == EventType.PERMISSION_TIMED_OUT.value:
            request_id = event.data.get("request_id")
            if isinstance(request_id, str):
                timed_out.add(request_id)
        if event.type == EventType.ASSISTANT_MESSAGE_COMPLETED.value:
            model = event.data.get("model")
            if isinstance(model, str) and model not in verdict.served_models:
                verdict.served_models.append(model)
        if event.type == EventType.RUN_RESULT.value:
            verdict.result_is_error = bool(event.data.get("is_error"))
            cost = event.data.get("total_cost_usd")
            verdict.total_cost_usd = float(cost) if isinstance(cost, int | float) else None
            turns = event.data.get("num_turns")
            verdict.num_turns = int(turns) if isinstance(turns, int) else None
        if event.type == EventType.RUN_COMPLETED.value:
            verdict.run_completed = True

    if not coverage["web"] and web_failures:
        # Provider lacks native Web tools (FR-23 territory): attempted and observable.
        coverage["web"] = True
        verdict.web_degraded = True
    if fallback_web_seen and not native_web_seen:
        verdict.web_degraded = True

    verdict.tool_coverage = coverage
    verdict.permission_requests = len(requested)
    verdict.permission_flow_ok = bool(requested) and not timed_out and set(requested) <= set(
        resolved
    )
    if deny_sequence is not None:
        continued = any(
            event.sequence > deny_sequence
            and event.type
            in {
                EventType.ASSISTANT_MESSAGE_COMPLETED.value,
                EventType.ASSISTANT_TEXT_DELTA.value,
                EventType.RUN_RESULT.value,
            }
            for event in events
        )
        verdict.deny_then_continue_ok = continued and verdict.run_completed
    sequences = sorted(event.sequence for event in events)
    verdict.sequence_contiguous = sequences == list(range(1, last_sequence + 1))

    for key in required:
        if not coverage.get(key):
            verdict.missing.append(f"tool:{key}")
    if verdict.subagent_events:
        verdict.missing.append("violation:subagent_events_present")
    if not verdict.permission_flow_ok:
        verdict.missing.append("permission:flow_incomplete")
    if expect_deny and not verdict.deny_then_continue_ok:
        verdict.missing.append("permission:deny_then_continue_missing")
    if not verdict.sequence_contiguous:
        verdict.missing.append("events:sequence_gap")
    if verdict.result_is_error is not False:
        verdict.missing.append("run:result_error_or_absent")
    if not verdict.run_completed:
        verdict.missing.append("run:not_completed")
    return verdict


def verify_project_state(project: OpportunityProject) -> list[str]:
    """Ledger-side checks that events alone cannot prove."""
    missing: list[str] = []
    report_versions = [
        artifact for artifact in project.artifacts if artifact.name == "greenpath-report.md"
    ]
    if not report_versions:
        missing.append("artifact:greenpath-report.md")
    if not any(evidence.source_url for evidence in project.evidence):
        missing.append("evidence:with_source_url")
    if not any(evidence.source_path for evidence in project.evidence):
        missing.append("evidence:with_source_path")
    if not project.decisions:
        missing.append("decision:recorded")
    else:
        decision = project.decisions[-1]
        if not decision.evidence_ids:
            missing.append("decision:evidence_links")
    return missing


async def drive_run(
    client: httpx.AsyncClient,
    ledger,
    project_id: str,
    prompt: str,
    *,
    provider_id: str,
    policy: PermissionPolicy,
    resume_session_id: str | None = None,
    timeout_s: float = 900,
    poll_s: float = 0.5,
) -> dict[str, Any]:
    """Start a run over the HTTP API and resolve permission requests as they appear."""
    payload: dict[str, Any] = {"prompt": prompt, "provider_profile_id": provider_id}
    if resume_session_id:
        payload["resume_session_id"] = resume_session_id
    response = await client.post(f"/api/projects/{project_id}/runs", json=payload)
    response.raise_for_status()
    run = response.json()
    run_id = run["id"]

    handled: set[str] = set()
    cursor = 0
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        events = ledger.list_events(run_id, cursor)
        for event in events:
            cursor = max(cursor, event.sequence)
            if event.type == EventType.PERMISSION_REQUESTED.value:
                request_id = event.data.get("request_id")
                tool_name = event.data.get("tool_name")
                tool_input = event.data.get("tool_input")
                if not isinstance(request_id, str) or request_id in handled:
                    continue
                handled.add(request_id)
                decision = policy.decide(
                    str(tool_name), tool_input if isinstance(tool_input, dict) else {}
                )
                await client.post(
                    f"/api/runs/{run_id}/permissions/{request_id}",
                    json={
                        "decision": decision,
                        "reason": "Green Path policy: marker command denied."
                        if decision == "deny"
                        else "",
                    },
                )
        status = ledger.get_run(run_id).status.value
        if status in {"completed", "failed", "cancelled"}:
            return ledger.get_run(run_id).model_dump(mode="json")
        if asyncio.get_event_loop().time() > deadline:
            await client.post(f"/api/runs/{run_id}/cancel")
            raise TimeoutError(f"Green Path run {run_id} exceeded {timeout_s}s")
        await asyncio.sleep(poll_s)


async def sse_replay_sequences(
    client: httpx.AsyncClient, run_id: str, from_sequence: int
) -> list[int]:
    """Reconnect to the SSE endpoint with Last-Event-ID and collect replayed sequences."""
    sequences: list[int] = []
    async with client.stream(
        "GET",
        f"/api/runs/{run_id}/events",
        headers={"Last-Event-ID": f"{run_id}:{from_sequence}"},
        timeout=30,
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("id:"):
                raw = line.split(":")[-1].strip()
                if raw.isdigit():
                    sequences.append(int(raw))
    return sequences


def write_bundle(
    report_dir: Path,
    name: str,
    run_payload: dict[str, Any],
    verdict: GreenPathVerdict,
    events: list[EventEnvelope],
    project_missing: list[str],
    notes: dict[str, Any] | None = None,
) -> Path:
    """Persist an auditable, redacted run bundle for the eval reports archive."""
    bundle_dir = report_dir / name
    bundle_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "run": redact(
            {
                key: run_payload.get(key)
                for key in (
                    "id",
                    "trace_id",
                    "project_id",
                    "status",
                    "mode",
                    "provider_profile_id",
                    "sdk_session_id",
                    "error",
                    "error_code",
                    "created_at",
                    "finished_at",
                )
            }
        ),
        "verdict": redact(verdict.__dict__),
        "project_missing": project_missing,
        "event_count": len(events),
        "event_type_counts": _count_types(events),
        "notes": redact(notes or {}),
    }
    (bundle_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        f"# Green Path bundle · {name}",
        "",
        f"- Run: `{run_payload.get('id')}` · status **{run_payload.get('status')}**"
        f" · provider `{run_payload.get('provider_profile_id')}`",
        f"- 判定：{'✅ PASS' if verdict.ok and not project_missing else '❌ FAIL'}"
        f"{'（Web 原生工具降级，见 FR-23）' if verdict.web_degraded else ''}",
        f"- 成本（SDK Anthropic 牌价口径）：{verdict.total_cost_usd} USD · turns {verdict.num_turns}",
        f"- 实际服务模型：{', '.join(verdict.served_models) or 'n/a'}",
        "",
        "## 工具覆盖",
        "",
        "| 链路 | 观察到 |",
        "|---|---|",
    ]
    for key in COVERAGE_KEYS:
        mark = "✅" if verdict.tool_coverage.get(key) else "❌"
        if key == "web" and verdict.web_degraded:
            mark = "⚠️ degraded"
        lines.append(f"| {key} | {mark} |")
    lines += [
        "",
        "## 治理断言",
        "",
        f"- 子代理事件：{verdict.subagent_events}（必须为 0）",
        f"- 权限请求 {verdict.permission_requests} 次；ask/allow/deny 闭环："
        f"{'✅' if verdict.permission_flow_ok else '❌'}",
        f"- deny 后继续收尾：{'✅' if verdict.deny_then_continue_ok else '❌'}",
        f"- 事件序号连续：{'✅' if verdict.sequence_contiguous else '❌'}",
        f"- 账本缺口：{project_missing or '无'}",
        f"- 其他缺口：{verdict.missing or '无'}",
    ]
    (bundle_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return bundle_dir


def _count_types(events: list[EventEnvelope]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        counts[event.type] = counts.get(event.type, 0) + 1
    return counts

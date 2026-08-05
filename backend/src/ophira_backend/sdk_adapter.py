from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookEventMessage,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from .agent_core import MAX_MEMORY_ITEMS, assemble_system_prompt
from .agents import build_specialist_agents, lead_system_prompt
from .config import Settings
from .constants import (
    DELEGATION_TOOL,
    OPHIRA_TOOL_NAMES,
    READ_ONLY_BUILTIN_TOOLS,
    SHELL_BUILTIN_TOOLS,
    USER_SCOPE_ID,
    WRITE_BUILTIN_TOOLS,
    EventType,
    MemoryScope,
    PermissionDecision,
)
from .events import EventPublisher, redact
from .ledger_tools import build_ledger_mcp_server
from .memory import MemoryProvider, gather_approved
from .models import EventActor, RunRecord
from .permissions import PermissionBroker
from .storage import FileLedger
from .templates import TEMPLATE_SPECS


class AgentRunError(RuntimeError):
    pass


def _path_within(root: Path, candidate: str | None) -> bool:
    """Resolve candidate against root; symlink-safe containment check."""
    if not candidate:
        return False
    try:
        path = Path(candidate)
        resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return False
    return True


class ClaudeAgentRuntime:
    """Adapter from Claude Agent SDK's native stream to OPHIRA's event contract."""

    def __init__(
        self,
        settings: Settings,
        ledger: FileLedger,
        events: EventPublisher,
        permissions: PermissionBroker,
        memory: MemoryProvider | None = None,
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.events = events
        self.permissions = permissions
        self.memory = memory
        self._clients: dict[str, ClaudeSDKClient] = {}
        self._clients_lock = asyncio.Lock()

    def _is_team(self, run: RunRecord) -> bool:
        if not run.agent_id:
            return True
        return TEMPLATE_SPECS[self.ledger.get_agent(run.agent_id).template].is_team

    async def run(self, run: RunRecord) -> None:
        options = self._build_options(run)
        result_error: str | None = None
        async with ClaudeSDKClient(options=options) as client:
            async with self._clients_lock:
                self._clients[run.id] = client
            try:
                await client.query(self._query_prompt(run, self._is_team(run)))
                async for message in client.receive_response():
                    await self._map_message(run.id, message)
                    if isinstance(message, ResultMessage) and message.is_error:
                        details = message.errors or [message.result or message.subtype]
                        result_error = "; ".join(details)
            finally:
                async with self._clients_lock:
                    self._clients.pop(run.id, None)
        if result_error:
            raise AgentRunError(result_error)

    async def cancel(self, run_id: str) -> None:
        async with self._clients_lock:
            client = self._clients.get(run_id)
        if client is not None:
            await client.interrupt()

    def _build_options(self, run: RunRecord) -> ClaudeAgentOptions:
        agent = self.ledger.get_agent(run.agent_id) if run.agent_id else None
        spec = TEMPLATE_SPECS[agent.template] if agent else None
        # Runs without an agent keep the legacy opportunity-team behavior.
        is_team = spec.is_team if spec else True
        mcp_server = build_ledger_mcp_server(
            self.ledger,
            run.project_id,
            memory=self.memory,
            agent_id=run.agent_id,
        )
        workspace = self.ledger.workspace_for_project(run.project_id).resolve()
        sdk_env = {}
        if self.settings.anthropic_api_key:
            sdk_env["ANTHROPIC_API_KEY"] = self.settings.anthropic_api_key.get_secret_value()

        async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context):
            if tool_name in WRITE_BUILTIN_TOOLS:
                candidate = tool_input.get("file_path")
                if not _path_within(workspace, candidate if isinstance(candidate, str) else None):
                    return PermissionResultDeny(
                        message=f"Path {candidate!r} is outside the run workspace."
                    )
            context_data = dataclasses.asdict(context)
            resolution = await self.permissions.request(
                run.id,
                tool_name,
                tool_input,
                agent_id=context.agent_id,
                context=context_data,
            )
            if resolution.decision in {
                PermissionDecision.ALLOW_ONCE,
                PermissionDecision.ALLOW_RUN,
            }:
                return PermissionResultAllow()
            return PermissionResultDeny(
                message=resolution.reason or "The user denied this tool call.",
            )

        hooks = {
            "PreToolUse": [HookMatcher(hooks=[self._hook(run.id)])],
            "PostToolUse": [HookMatcher(hooks=[self._hook(run.id)])],
            "PostToolUseFailure": [HookMatcher(hooks=[self._hook(run.id)])],
            "SubagentStart": [HookMatcher(hooks=[self._hook(run.id)])],
            "SubagentStop": [HookMatcher(hooks=[self._hook(run.id)])],
        }
        if is_team:
            builtin_tools = [*READ_ONLY_BUILTIN_TOOLS, DELEGATION_TOOL]
            allowed_tools = [*READ_ONLY_BUILTIN_TOOLS, DELEGATION_TOOL, *OPHIRA_TOOL_NAMES]
            system_prompt = lead_system_prompt(run.project_id, run.trace_id)
            subagents = build_specialist_agents()
        else:
            builtin_tools = [
                *READ_ONLY_BUILTIN_TOOLS,
                *WRITE_BUILTIN_TOOLS,
                *SHELL_BUILTIN_TOOLS,
            ]
            # Write/Edit/Bash are deliberately absent so the broker gates them.
            allowed_tools = [*READ_ONLY_BUILTIN_TOOLS, *OPHIRA_TOOL_NAMES]
            memories = []
            if self.memory is not None:
                memories = gather_approved(
                    self.memory,
                    [
                        (MemoryScope.USER, USER_SCOPE_ID),
                        (MemoryScope.PROJECT, run.project_id),
                        (MemoryScope.AGENT, agent.id),
                    ],
                    limit=MAX_MEMORY_ITEMS,
                )
            core_dir = self.ledger.agent_core_dir(agent.id)
            system_prompt = assemble_system_prompt(agent, core_dir, spec, memories)
            subagents = None
        return ClaudeAgentOptions(
            tools=builtin_tools,
            allowed_tools=allowed_tools,
            system_prompt=system_prompt,
            mcp_servers={"ophira": mcp_server},
            strict_mcp_config=True,
            permission_mode="default",
            can_use_tool=can_use_tool,
            hooks=hooks,
            include_partial_messages=True,
            include_hook_events=True,
            agents=subagents,
            model=(agent.model if agent and agent.model else self.settings.claude_model),
            max_turns=self.settings.max_turns,
            max_budget_usd=self.settings.max_budget_usd,
            cwd=workspace,
            env=sdk_env,
            setting_sources=[],
            thinking={"type": "adaptive", "display": "summarized"},
            resume=run.resume_session_id,
        )

    def _hook(self, run_id: str):
        async def handle(input_data: dict[str, Any], _tool_use_id, _context):
            event_name = input_data.get("hook_event_name", "")
            actor = self._hook_actor(input_data)
            tool_use_id = input_data.get("tool_use_id")
            if event_name == "PreToolUse":
                await self.events.publish(
                    run_id,
                    EventType.TOOL_STARTED,
                    {
                        "tool_name": input_data.get("tool_name"),
                        "tool_input": input_data.get("tool_input", {}),
                    },
                    actor=actor,
                    tool_use_id=tool_use_id,
                )
            elif event_name == "PostToolUse":
                await self.events.publish(
                    run_id,
                    EventType.TOOL_COMPLETED,
                    {
                        "tool_name": input_data.get("tool_name"),
                        "tool_input": input_data.get("tool_input", {}),
                        "tool_response": input_data.get("tool_response"),
                    },
                    actor=actor,
                    tool_use_id=tool_use_id,
                )
            elif event_name == "PostToolUseFailure":
                await self.events.publish(
                    run_id,
                    EventType.TOOL_FAILED,
                    {
                        "tool_name": input_data.get("tool_name"),
                        "tool_input": input_data.get("tool_input", {}),
                        "error": input_data.get("error"),
                        "is_interrupt": input_data.get("is_interrupt", False),
                    },
                    actor=actor,
                    tool_use_id=tool_use_id,
                )
            elif event_name == "SubagentStart":
                await self.events.publish(
                    run_id,
                    EventType.SUBAGENT_STARTED,
                    {
                        "agent_id": input_data.get("agent_id"),
                        "agent_type": input_data.get("agent_type"),
                    },
                    actor=actor,
                )
            elif event_name == "SubagentStop":
                await self.events.publish(
                    run_id,
                    EventType.SUBAGENT_STOPPED,
                    {
                        "agent_id": input_data.get("agent_id"),
                        "agent_type": input_data.get("agent_type"),
                    },
                    actor=actor,
                )
            return {}

        return handle

    @staticmethod
    def _hook_actor(input_data: dict[str, Any]) -> EventActor:
        agent_id = input_data.get("agent_id")
        agent_type = input_data.get("agent_type")
        if agent_id:
            return EventActor(
                type="subagent",
                name=agent_type or "delegated-agent",
                agent_id=agent_id,
            )
        return EventActor(type="agent", name=agent_type or "opportunity-lead")

    @staticmethod
    def _message_actor(parent_tool_use_id: str | None) -> EventActor:
        if parent_tool_use_id:
            return EventActor(
                type="subagent",
                name="delegated-agent",
                agent_id=parent_tool_use_id,
            )
        return EventActor(type="agent", name="opportunity-lead")

    async def _map_message(self, run_id: str, message: Any) -> None:
        if isinstance(message, StreamEvent):
            await self._map_stream_event(run_id, message)
            return
        if isinstance(message, AssistantMessage):
            await self._map_assistant_message(run_id, message)
            return
        if isinstance(message, TaskStartedMessage):
            await self.events.publish(
                run_id,
                EventType.TASK_STARTED,
                {
                    "task_id": message.task_id,
                    "description": message.description,
                    "task_type": message.task_type,
                },
                tool_use_id=message.tool_use_id,
            )
            return
        if isinstance(message, TaskProgressMessage):
            await self.events.publish(
                run_id,
                EventType.TASK_PROGRESS,
                {
                    "task_id": message.task_id,
                    "description": message.description,
                    "usage": message.usage,
                    "last_tool_name": message.last_tool_name,
                },
                tool_use_id=message.tool_use_id,
            )
            return
        if isinstance(message, TaskNotificationMessage):
            await self.events.publish(
                run_id,
                EventType.TASK_COMPLETED,
                {
                    "task_id": message.task_id,
                    "status": message.status,
                    "summary": message.summary,
                    "usage": message.usage,
                },
                tool_use_id=message.tool_use_id,
            )
            return
        if isinstance(message, ResultMessage):
            self.ledger.update_run(run_id, sdk_session_id=message.session_id)
            await self.events.publish(
                run_id,
                EventType.RUN_RESULT,
                {
                    "subtype": message.subtype,
                    "is_error": message.is_error,
                    "result": message.result,
                    "structured_output": message.structured_output,
                    "duration_ms": message.duration_ms,
                    "duration_api_ms": message.duration_api_ms,
                    "num_turns": message.num_turns,
                    "total_cost_usd": message.total_cost_usd,
                    "usage": message.usage,
                    "model_usage": message.model_usage,
                    "permission_denials": message.permission_denials,
                    "errors": message.errors,
                },
            )
            return
        if isinstance(message, RateLimitEvent):
            await self.events.publish(
                run_id,
                EventType.SDK_RATE_LIMIT,
                dataclasses.asdict(message.rate_limit_info),
                actor=EventActor(type="system", name="claude-agent-sdk"),
            )
            return
        if isinstance(message, HookEventMessage):
            await self.events.publish(
                run_id,
                EventType.SDK_SYSTEM,
                {
                    "subtype": message.subtype,
                    "hook_event_name": message.hook_event_name,
                    "phase": message.data.get("subtype"),
                    "outcome": message.data.get("outcome"),
                },
                actor=EventActor(type="system", name="claude-agent-sdk"),
            )
            return
        if isinstance(message, SystemMessage):
            session_id = message.data.get("session_id")
            if session_id:
                self.ledger.update_run(run_id, sdk_session_id=session_id)
            await self.events.publish(
                run_id,
                EventType.SDK_SYSTEM,
                {"subtype": message.subtype, "data": redact(message.data)},
                actor=EventActor(type="system", name="claude-agent-sdk"),
            )

    async def _map_stream_event(self, run_id: str, message: StreamEvent) -> None:
        event = message.event
        event_name = event.get("type")
        actor = self._message_actor(message.parent_tool_use_id)
        if event_name == "message_start":
            await self.events.publish(
                run_id,
                EventType.ASSISTANT_MESSAGE_STARTED,
                {"message": event.get("message", {})},
                actor=actor,
                parent_id=message.parent_tool_use_id,
            )
            return
        if event_name != "content_block_delta":
            return
        delta = event.get("delta", {})
        delta_type = delta.get("type")
        common = {
            "index": event.get("index"),
            "partial": True,
        }
        if delta_type == "text_delta":
            await self.events.publish(
                run_id,
                EventType.ASSISTANT_TEXT_DELTA,
                {**common, "text": delta.get("text", "")},
                actor=actor,
                parent_id=message.parent_tool_use_id,
            )
        elif delta_type == "thinking_delta":
            await self.events.publish(
                run_id,
                EventType.ASSISTANT_REASONING_SUMMARY_DELTA,
                {**common, "text": delta.get("thinking", ""), "display": "summarized"},
                actor=actor,
                parent_id=message.parent_tool_use_id,
            )
        elif delta_type == "input_json_delta":
            await self.events.publish(
                run_id,
                EventType.TOOL_INPUT_DELTA,
                {**common, "partial_json": delta.get("partial_json", "")},
                actor=actor,
                parent_id=message.parent_tool_use_id,
            )

    async def _map_assistant_message(self, run_id: str, message: AssistantMessage) -> None:
        actor = self._message_actor(message.parent_tool_use_id)
        for block in message.content:
            if isinstance(block, TextBlock):
                await self.events.publish(
                    run_id,
                    EventType.ASSISTANT_MESSAGE_COMPLETED,
                    {
                        "text": block.text,
                        "model": message.model,
                        "usage": message.usage,
                        "stop_reason": message.stop_reason,
                    },
                    actor=actor,
                    parent_id=message.parent_tool_use_id,
                )
            elif isinstance(block, ThinkingBlock):
                await self.events.publish(
                    run_id,
                    EventType.ASSISTANT_REASONING_SUMMARY_COMPLETED,
                    {"text": block.thinking, "display": "summarized"},
                    actor=actor,
                    parent_id=message.parent_tool_use_id,
                )
            elif isinstance(block, ToolUseBlock):
                await self.events.publish(
                    run_id,
                    EventType.TOOL_PLANNED,
                    {"tool_name": block.name, "tool_input": block.input},
                    actor=actor,
                    parent_id=message.parent_tool_use_id,
                    tool_use_id=block.id,
                )
    @staticmethod
    def _query_prompt(run: RunRecord, is_team: bool) -> str:
        if is_team:
            return f"""用户请求：
{run.prompt}

请管理 project_id={run.project_id} 的商品机会验证。
所有事实、假设、评估和阶段决策必须写入共享项目账本。"""
        return f"""用户请求：
{run.prompt}

请在 project_id={run.project_id} 对应的工作区内完成任务，
遵守系统提示中的安全政策与工作原则。写入文件或执行命令前等待用户批准；
完成后用工具结果证明交付物真实存在。"""

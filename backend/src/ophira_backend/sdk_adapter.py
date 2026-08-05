from __future__ import annotations

import asyncio
import dataclasses
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

from .agents import build_specialist_agents, lead_system_prompt
from .config import Settings
from .constants import (
    DELEGATION_TOOL,
    LEDGER_TOOL_NAMES,
    READ_ONLY_BUILTIN_TOOLS,
    EventType,
    PermissionDecision,
)
from .events import EventPublisher, redact
from .ledger_tools import build_ledger_mcp_server
from .models import EventActor, RunRecord
from .permissions import PermissionBroker
from .storage import FileLedger


class AgentRunError(RuntimeError):
    pass


class ClaudeAgentRuntime:
    """Adapter from Claude Agent SDK's native stream to OPHIRA's event contract."""

    def __init__(
        self,
        settings: Settings,
        ledger: FileLedger,
        events: EventPublisher,
        permissions: PermissionBroker,
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.events = events
        self.permissions = permissions
        self._clients: dict[str, ClaudeSDKClient] = {}
        self._clients_lock = asyncio.Lock()

    async def run(self, run: RunRecord) -> None:
        options = self._build_options(run)
        result_error: str | None = None
        async with ClaudeSDKClient(options=options) as client:
            async with self._clients_lock:
                self._clients[run.id] = client
            try:
                await client.query(self._query_prompt(run))
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
        mcp_server = build_ledger_mcp_server(self.ledger, run.project_id)
        sdk_env = {}
        if self.settings.anthropic_api_key:
            sdk_env["ANTHROPIC_API_KEY"] = self.settings.anthropic_api_key.get_secret_value()

        async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context):
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
        return ClaudeAgentOptions(
            tools=[*READ_ONLY_BUILTIN_TOOLS, DELEGATION_TOOL],
            allowed_tools=[
                *READ_ONLY_BUILTIN_TOOLS,
                DELEGATION_TOOL,
                *LEDGER_TOOL_NAMES,
            ],
            system_prompt=lead_system_prompt(run.project_id, run.trace_id),
            mcp_servers={"ophira": mcp_server},
            strict_mcp_config=True,
            permission_mode="default",
            can_use_tool=can_use_tool,
            hooks=hooks,
            include_partial_messages=True,
            include_hook_events=True,
            agents=build_specialist_agents(),
            model=self.settings.claude_model,
            max_turns=self.settings.max_turns,
            max_budget_usd=self.settings.max_budget_usd,
            cwd=self.ledger.workspace_for_project(run.project_id),
            env=sdk_env,
            setting_sources=[],
            thinking={"type": "adaptive", "display": "summarized"},
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
    def _query_prompt(run: RunRecord) -> str:
        return f"""用户请求：
{run.prompt}

请管理 project_id={run.project_id} 的商品机会验证。
所有事实、假设、评估和阶段决策必须写入共享项目账本。"""

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
    create_sdk_mcp_server,
    tool,
)

from .agent_core import MAX_MEMORY_ITEMS, assemble_default_prompt, assemble_system_prompt
from .agents import build_deep_research_agents
from .config import Settings
from .constants import (
    DELEGATION_TOOL,
    READ_ONLY_BUILTIN_TOOLS,
    READ_ONLY_MCP_TOOL_NAMES,
    USER_SCOPE_ID,
    WRITE_BUILTIN_TOOLS,
    EventType,
    MemoryScope,
    PermissionDecision,
    ProviderErrorCode,
    RunMode,
)
from .events import EventPublisher, redact
from .ledger_tools import build_mcp_servers
from .memory import MemoryProvider, gather_approved
from .models import EventActor, ProviderPreflightResult, RunRecord, utc_now
from .permissions import PermissionBroker
from .providers import (
    ProviderConfigurationError,
    ProviderRegistry,
    classify_provider_error,
    provider_error_message,
    successful_preflight,
)
from .skills import SkillRegistry
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


def _resolve_path(root: Path, candidate: str) -> Path:
    path = Path(candidate)
    return (root / path).resolve() if not path.is_absolute() else path.resolve()


class ClaudeAgentRuntime:
    """Adapter from Claude Agent SDK's native stream to OPHIRA's event contract."""

    def __init__(
        self,
        settings: Settings,
        ledger: FileLedger,
        events: EventPublisher,
        permissions: PermissionBroker,
        memory: MemoryProvider | None = None,
        providers: ProviderRegistry | None = None,
        skills: SkillRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.events = events
        self.permissions = permissions
        self.memory = memory
        self.providers = providers or ProviderRegistry(settings)
        self.skills = skills or SkillRegistry(
            settings.capability_plugin_dir,
            settings.data_dir / "skills",
        )
        self._clients: dict[str, ClaudeSDKClient] = {}
        self._clients_lock = asyncio.Lock()

    async def run(self, run: RunRecord) -> None:
        self._require_provider(run)
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

    async def preflight(self, provider_profile_id: str) -> ProviderPreflightResult:
        """Exercise the real Claude Agent SDK path; never substitutes a direct model client."""
        profile = self.providers.get(provider_profile_id)
        preflight_dir = (
            self.settings.data_dir / "preflight" / provider_profile_id
        ).resolve()
        preflight_dir.mkdir(parents=True, exist_ok=True)

        @tool("health", "Return the OPHIRA MCP preflight marker.", {})
        async def health(_args: dict[str, Any]) -> dict[str, Any]:
            return {
                "content": [{"type": "text", "text": "OPHIRA_MCP_OK"}],
                "is_error": False,
            }

        server_name = "ophira_preflight"
        health_tool_name = f"mcp__{server_name}__health"
        options = ClaudeAgentOptions(
            cwd=preflight_dir,
            tools={"type": "preset", "preset": "claude_code"},
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": "这是只读连通性预检。不得修改文件或执行外部副作用。",
            },
            plugins=[{"type": "local", "path": str(self.settings.capability_plugin_dir)}],
            setting_sources=[],
            skills="all",
            mcp_servers={
                server_name: create_sdk_mcp_server(
                    name=server_name,
                    version="1.0.0",
                    tools=[health],
                )
            },
            strict_mcp_config=True,
            allowed_tools=[health_tool_name],
            disallowed_tools=["Agent", "Write", "Edit", "Bash"],
            permission_mode="dontAsk",
            env=self.providers.build_worker_env(provider_profile_id),
            model=profile.model,
            max_turns=4,
            max_budget_usd=min(self.settings.max_budget_usd, 0.25),
            include_partial_messages=False,
            sandbox={
                "enabled": True,
                "autoAllowBashIfSandboxed": False,
                "allowUnsandboxedCommands": False,
            },
        )
        discovered_skills: list[str] = []
        sdk_session_id: str | None = None
        health_called = False
        result_error: str | None = None
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(
                    "调用 ophira_preflight health 工具，然后只回复 OPHIRA_PREFLIGHT_OK。"
                )
                async for message in client.receive_response():
                    if isinstance(message, SystemMessage) and message.subtype == "init":
                        sdk_session_id = message.data.get("session_id")
                        discovered_skills = list(message.data.get("skills") or [])
                    elif isinstance(message, AssistantMessage):
                        health_called = health_called or any(
                            isinstance(block, ToolUseBlock) and block.name == health_tool_name
                            for block in message.content
                        )
                    elif isinstance(message, ResultMessage):
                        sdk_session_id = message.session_id or sdk_session_id
                        if message.is_error:
                            details = message.errors or [message.result or message.subtype]
                            result_error = "; ".join(details)
            expected_skill = "ophira-core:global-opportunity-execution"
            if result_error:
                raise AgentRunError(result_error)
            if expected_skill not in discovered_skills:
                raise ProviderConfigurationError(
                    f"Expected Plugin Skill {expected_skill} was not discovered.",
                    ProviderErrorCode.SKILL_DISCOVERY_FAILED,
                )
            if not health_called:
                raise ProviderConfigurationError(
                    "The preflight MCP health tool was not called.",
                    ProviderErrorCode.MCP_INIT_FAILED,
                )
            result = successful_preflight(
                provider_profile_id,
                sdk_session_id=sdk_session_id,
                model=profile.model,
                mcp_connected=True,
                discovered_skills=discovered_skills,
            )
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, ProviderConfigurationError)
                else classify_provider_error(exc)
            )
            result = ProviderPreflightResult(
                provider_profile_id=provider_profile_id,
                ok=False,
                error_code=code,
                message=f"{provider_error_message(code)} ({redact(str(exc))})",
                sdk_session_id=sdk_session_id,
                model=profile.model,
                mcp_connected=health_called,
                discovered_skills=discovered_skills,
                checked_at=utc_now(),
            )
        self.providers.record_preflight(result)
        return result

    async def cancel(self, run_id: str) -> None:
        async with self._clients_lock:
            client = self._clients.get(run_id)
        if client is not None:
            await client.interrupt()

    def _require_provider(self, run: RunRecord) -> None:
        if run.provider_profile_id:
            self.providers.build_worker_env(run.provider_profile_id)
            return
        if not (self.settings.anthropic_api_key or self.settings.anthropic_auth_token):
            raise ProviderConfigurationError(
                "No Provider credential is configured. Save and preflight a Provider first.",
                ProviderErrorCode.AUTH_REVOKED_OR_INVALID,
            )

    def _provider_env(self, run: RunRecord) -> dict[str, str]:
        if run.provider_profile_id:
            return self.providers.build_worker_env(run.provider_profile_id)
        return self.settings.claude_sdk_env()

    def _system_append(self, run: RunRecord) -> str:
        agent = self.ledger.get_agent(run.agent_id) if run.agent_id else None
        spec = TEMPLATE_SPECS[agent.template] if agent else None
        memories = []
        if self.memory is not None:
            memories = gather_approved(
                self.memory,
                [
                    (MemoryScope.USER, USER_SCOPE_ID),
                    (MemoryScope.PROJECT, run.project_id),
                ],
                limit=MAX_MEMORY_ITEMS,
            )
        if agent and spec:
            prompt = assemble_system_prompt(
                agent,
                self.ledger.agent_core_dir(agent.id),
                spec,
                memories,
            )
        else:
            prompt = assemble_default_prompt(run.project_id, run.trace_id, memories)
        mode_rules = (
            "Deep Research 已由用户显式开启。临时子 Agent 只能做只读来源研究；"
            "你负责汇总、去重、处理冲突，并且只有你能写最终项目真相。"
            if run.mode == RunMode.DEEP_RESEARCH
            else "当前是常规单 Agent 模式。不得尝试创建子 Agent；按需使用原生工具、Skill 与 MCP。"
        )
        return f"{prompt}\n\n---\n\n# 运行模式\n\n{mode_rules}"

    def _build_options(self, run: RunRecord) -> ClaudeAgentOptions:
        agent = self.ledger.get_agent(run.agent_id) if run.agent_id else None
        workspace = self.ledger.workspace_for_project(run.project_id).resolve()
        deep_research = run.mode == RunMode.DEEP_RESEARCH

        async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context):
            if tool_name in WRITE_BUILTIN_TOOLS:
                candidate = tool_input.get("file_path")
                if not _path_within(workspace, candidate if isinstance(candidate, str) else None):
                    return PermissionResultDeny(
                        message=f"Path {candidate!r} is outside the run workspace."
                    )
            if tool_name == "Bash" and tool_input.get("dangerouslyDisableSandbox"):
                return PermissionResultDeny(
                    message="Unsandboxed Bash is disabled by OPHIRA policy."
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
            "PreToolUse": [HookMatcher(hooks=[self._hook(run.id, workspace)])],
            "PostToolUse": [HookMatcher(hooks=[self._hook(run.id, workspace)])],
            "PostToolUseFailure": [HookMatcher(hooks=[self._hook(run.id, workspace)])],
            "SubagentStart": [HookMatcher(hooks=[self._hook(run.id, workspace)])],
            "SubagentStop": [HookMatcher(hooks=[self._hook(run.id, workspace)])],
        }
        model = self.settings.claude_model
        budget_scale = 1.0
        if run.provider_profile_id:
            profile = self.providers.get(run.provider_profile_id)
            model = profile.model
            budget_scale = profile.budget_scale
        elif agent and agent.model:
            model = agent.model
        return ClaudeAgentOptions(
            tools={"type": "preset", "preset": "claude_code"},
            allowed_tools=[*READ_ONLY_BUILTIN_TOOLS, *READ_ONLY_MCP_TOOL_NAMES],
            disallowed_tools=[] if deep_research else [DELEGATION_TOOL],
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": self._system_append(run),
            },
            plugins=[{"type": "local", "path": str(self.settings.capability_plugin_dir)}],
            skills=self.skills.runtime_selection(),
            mcp_servers=build_mcp_servers(
                self.ledger,
                run.project_id,
                memory=self.memory,
                agent_id=run.agent_id,
            ),
            strict_mcp_config=True,
            permission_mode="default",
            can_use_tool=can_use_tool,
            hooks=hooks,
            include_partial_messages=True,
            include_hook_events=True,
            agents=build_deep_research_agents() if deep_research else None,
            model=model,
            max_turns=self.settings.max_turns,
            max_budget_usd=self.settings.max_budget_usd * budget_scale,
            cwd=workspace,
            env=self._provider_env(run),
            setting_sources=[],
            sandbox={
                "enabled": True,
                "autoAllowBashIfSandboxed": False,
                "allowUnsandboxedCommands": False,
            },
            enable_file_checkpointing=True,
            thinking={"type": "adaptive", "display": "summarized"},
            resume=run.resume_session_id,
        )

    def _hook(self, run_id: str, workspace: Path):
        async def handle(input_data: dict[str, Any], _tool_use_id, _context):
            event_name = input_data.get("hook_event_name", "")
            actor = self._hook_actor(input_data)
            tool_use_id = input_data.get("tool_use_id")
            if event_name == "PreToolUse":
                tool_name = input_data.get("tool_name")
                tool_input = input_data.get("tool_input", {})
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
                candidate_key = {
                    "Read": "file_path",
                    "Write": "file_path",
                    "Edit": "file_path",
                    "Glob": "path",
                    "Grep": "path",
                }.get(str(tool_name))
                if candidate_key and tool_input.get(candidate_key):
                    candidate = tool_input.get(candidate_key)
                    if not isinstance(candidate, str) or not _path_within(workspace, candidate):
                        return {
                            "hookSpecificOutput": {
                                "hookEventName": "PreToolUse",
                                "permissionDecision": "deny",
                                "permissionDecisionReason": (
                                    f"{tool_name} path is outside the project workspace."
                                ),
                            }
                        }
                    resolved = _resolve_path(workspace, candidate)
                    sensitive_names = {".env", ".git-credentials", "credentials", "secrets"}
                    if any(part.casefold() in sensitive_names for part in resolved.parts):
                        return {
                            "hookSpecificOutput": {
                                "hookEventName": "PreToolUse",
                                "permissionDecision": "deny",
                                "permissionDecisionReason": (
                                    "Sensitive credential paths are unavailable to the Agent."
                                ),
                            }
                        }
                if tool_name == "Bash":
                    if tool_input.get("dangerouslyDisableSandbox"):
                        return {
                            "hookSpecificOutput": {
                                "hookEventName": "PreToolUse",
                                "permissionDecision": "deny",
                                "permissionDecisionReason": "Unsandboxed commands are disabled.",
                            }
                        }
                    # Sandboxed Bash is otherwise auto-approved ahead of can_use_tool.
                    # PRD §4.10 requires Bash to ask by default, so force the ask here;
                    # the request then flows through can_use_tool -> PermissionBroker.
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "ask",
                            "permissionDecisionReason": (
                                "OPHIRA policy: Bash always asks (never cached per run)."
                            ),
                        }
                    }
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
        return EventActor(type="agent", name=agent_type or "ophira")

    @staticmethod
    def _message_actor(parent_tool_use_id: str | None) -> EventActor:
        if parent_tool_use_id:
            return EventActor(
                type="subagent",
                name="delegated-agent",
                agent_id=parent_tool_use_id,
            )
        return EventActor(type="agent", name="ophira")

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
                    "cost_basis": "sdk_anthropic_list_price",
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
        return run.prompt

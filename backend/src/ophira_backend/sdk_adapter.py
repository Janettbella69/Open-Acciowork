from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any, Literal

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
from .config import BACKEND_DIR, Settings
from .connectors import ConnectorRegistry
from .constants import (
    AUTOMATION_MCP_SERVER_NAME,
    BROWSER_MCP_SERVER_NAME,
    CLARIFYING_TOOL,
    DELEGATION_TOOL,
    DELETE_BUILTIN_TOOLS,
    ENVELOPE_APPROVED_MCP_TOOL_NAMES,
    READ_ONLY_BUILTIN_TOOLS,
    READ_ONLY_MCP_TOOL_NAMES,
    SHELL_BUILTIN_TOOLS,
    TOOL_ACT_ON_PAGE,
    TOOL_SNAPSHOT_PAGE,
    USER_SCOPE_ID,
    WRITE_BUILTIN_TOOLS,
    EventType,
    MemoryScope,
    PermissionDecision,
    PermissionModePolicy,
    ProviderErrorCode,
    RunMode,
)
from .destructive import is_destructive_delete
from .events import EventPublisher, redact
from .ledger_tools import build_mcp_servers
from .memory import MemoryProvider, gather_approved
from .models import EventActor, ProviderPreflightResult, RunRecord, utc_now
from .permissions import PermissionBroker
from .policy import OrgPolicyStore
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


def _contained(cwd: Path, fence: Path, candidate: str) -> bool:
    """Resolve candidate against the real cwd, then require containment in fence."""
    try:
        _resolve_path(cwd, candidate).relative_to(fence)
    except (OSError, ValueError):
        return False
    return True


def _is_subagent_write_tool(tool_name: str) -> bool:
    # snapshot_page registers Artifact/Evidence immediately, so it is a
    # main-Agent write even though normal L1 reads are auto-approved for users.
    if tool_name == TOOL_SNAPSHOT_PAGE:
        return True
    if tool_name in {
        *WRITE_BUILTIN_TOOLS,
        *DELETE_BUILTIN_TOOLS,
        *SHELL_BUILTIN_TOOLS,
    }:
        return True
    return tool_name.startswith("mcp__") and tool_name not in READ_ONLY_MCP_TOOL_NAMES


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
        intents=None,
        notifications=None,
        connectors: ConnectorRegistry | None = None,
        policy: OrgPolicyStore | None = None,
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
        self.intents = intents
        self.notifications = notifications
        self.connectors = connectors
        self.policy = policy or OrgPolicyStore(settings.data_dir / "org" / "policy.json")
        self._clients: dict[str, ClaudeSDKClient] = {}
        self._clients_lock = asyncio.Lock()

    async def run(self, run: RunRecord) -> None:
        self._require_provider(run)
        options = self._build_options(run)
        if run.mode != RunMode.DEEP_RESEARCH:
            await self._run_sdk_session(run, options)
            return

        task = asyncio.create_task(
            self._run_sdk_session(run, options),
            name=f"ophira:deep-research:{run.id}",
        )
        try:
            done, _pending = await asyncio.wait(
                {task}, timeout=self.settings.deep_research_timeout_seconds
            )
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        if task in done:
            await task
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise AgentRunError(
            "Deep Research exceeded its wall-clock limit "
            f"({self.settings.deep_research_timeout_seconds}s) and was stopped."
        )

    async def _run_sdk_session(
        self,
        run: RunRecord,
        options: ClaudeAgentOptions,
    ) -> None:
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
            plugins=self.skills.plugin_paths(),
            setting_sources=[],
            skills=self._selected_skills(),
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
                # 与主运行一致（Bash 在预检本就被禁用，此处只为提早暴露沙箱不可用）。
                "autoAllowBashIfSandboxed": True,
                "failIfUnavailable": True,
                "allowUnsandboxedCommands": False,
            },
        )
        discovered_skills: list[str] = []
        sdk_session_id: str | None = None
        health_called = False
        result_error: str | None = None

        async def exercise_sdk() -> None:
            nonlocal sdk_session_id, discovered_skills, health_called, result_error
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

        try:
            try:
                # A bad credential can hang the SDK instead of failing fast (observed
                # live against DeepSeek); FR-06 requires a bounded, actionable answer.
                await asyncio.wait_for(
                    exercise_sdk(), timeout=self.settings.preflight_timeout_seconds
                )
            except TimeoutError as exc:
                raise ProviderConfigurationError(
                    f"预检在 {self.settings.preflight_timeout_seconds} 秒内未完成，已中止。"
                    "常见原因：凭据无效导致端点反复重试、网络不通或端点异常。",
                    ProviderErrorCode.SDK_RUNTIME_FAILED,
                ) from exc
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
        mode_rules = (
            "Deep Research 已由用户显式开启。临时子 Agent 只能做只读来源研究；"
            "你负责汇总、去重、处理冲突，并且只有你能写最终项目真相。"
            if run.mode == RunMode.DEEP_RESEARCH
            else (
                "主线由你一以贯之地干完，不把主线工作交给别人。"
                "只有当一个独立支线任务会产生大量脏上下文（例如翻几十个网页或文件）时，"
                "才值得派一个一次性子代理去做并只取回摘要——前辈经验是九成五的任务用不到它。"
                "不要把修复你自己刚做的事交给子代理（它没有你的上下文），"
                "账本记录永远由你本人完成。"
            )
        )
        # 运行模式是固定文案，进稳定前缀；每 run 变的 trace_id 由 assemble 收尾。
        if agent and spec:
            return assemble_system_prompt(
                agent,
                self.ledger.agent_core_dir(agent.id),
                spec,
                memories,
                mode_rules=mode_rules,
            )
        return assemble_default_prompt(
            run.project_id, run.trace_id, memories, mode_rules=mode_rules
        )

    def _build_options(self, run: RunRecord) -> ClaudeAgentOptions:
        agent = self.ledger.get_agent(run.agent_id) if run.agent_id else None
        workspace = self.ledger.workspace_for_project(run.project_id).resolve()
        deep_research = run.mode == RunMode.DEEP_RESEARCH

        async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context):
            # §6.1 红线：账本写入与自动化承诺只属主 Agent。支线子代理（原生
            # general-purpose/Explore 等）做研究与计算，把发现带回主线落账。
            subagent_id = getattr(context, "agent_id", None)
            if subagent_id and _is_subagent_write_tool(tool_name):
                return PermissionResultDeny(
                    message=(
                        "子代理只读：文件、Shell、账本与外部动作只属主 Agent（PRD §6.1）。"
                        "请把研究结论带回主线，由主 Agent 执行动作或记录。"
                    )
                )
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
                PermissionDecision.ALLOW_PERSISTENT,
            }:
                if tool_name == CLARIFYING_TOOL:
                    answers = resolution.answers or {}
                    self._record_constraints(run.project_id, answers)
                    return PermissionResultAllow(
                        updated_input={
                            "questions": tool_input.get("questions", []),
                            "answers": answers,
                        }
                    )
                return PermissionResultAllow()
            if tool_name == CLARIFYING_TOOL:
                return PermissionResultDeny(
                    message="用户跳过了这些问题。按现有信息继续，并明确说明你所做的假设。",
                )
            return PermissionResultDeny(
                message=resolution.reason or "The user denied this tool call.",
            )

        hook = self._hook(run.id, workspace)
        hooks = {
            "PreToolUse": [HookMatcher(hooks=[hook])],
            "PostToolUse": [HookMatcher(hooks=[hook])],
            "PostToolUseFailure": [HookMatcher(hooks=[hook])],
            "SubagentStart": [HookMatcher(hooks=[hook])],
            "SubagentStop": [HookMatcher(hooks=[hook])],
        }
        model = self.settings.claude_model
        budget_scale = 1.0
        if run.provider_profile_id:
            profile = self.providers.get(run.provider_profile_id)
            model = profile.model
            budget_scale = profile.budget_scale
        elif agent and agent.model:
            model = agent.model
        org_policy = self.policy.get()
        mcp_servers = build_mcp_servers(
            self.ledger,
            run.project_id,
            memory=self.memory,
            agent_id=run.agent_id,
            settings=self.settings,
            run_id=run.id,
            policy=org_policy,
            connectors=self.connectors,
        )
        if self.intents is not None and self.notifications is not None:
            from .automation_tools import build_automation_mcp_server

            mcp_servers[AUTOMATION_MCP_SERVER_NAME] = build_automation_mcp_server(
                self.ledger,
                self.intents,
                self.notifications,
                run.project_id,
                run_id=run.id,
                schedule_id=run.schedule_id,
            )
        return ClaudeAgentOptions(
            tools={"type": "preset", "preset": "claude_code"},
            allowed_tools=[
                *READ_ONLY_BUILTIN_TOOLS,
                *[
                    name
                    for name in READ_ONLY_MCP_TOOL_NAMES
                    if name != TOOL_SNAPSHOT_PAGE
                    or BROWSER_MCP_SERVER_NAME in mcp_servers
                ],
                # Envelope-approved automation tools (§4.10): inbox delivery and
                # throttled intent firing work unattended inside scheduled runs.
                *ENVELOPE_APPROVED_MCP_TOOL_NAMES,
                # 2026-08-24 拍板"按原生能力来做"：支线子代理开放，派遣动作
                # 自动放行（无副作用）；子代理内部的工具调用仍逐项过权限，
                # 账本写入在 can_use_tool 对子代理代码级拒绝。
                DELEGATION_TOOL,
            ],
            disallowed_tools=[],
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": self._system_append(run),
            },
            plugins=self.skills.plugin_paths(),
            skills=self._selected_skills(),
            mcp_servers=mcp_servers,
            strict_mcp_config=True,
            permission_mode=(
                "auto"
                if org_policy.permission_mode == PermissionModePolicy.AUTO
                else "default"
            ),
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
                # SDK 原生默认：沙箱内 Bash 自动批准（2026-08-24 拍板恢复，教材口径）。
                "autoAllowBashIfSandboxed": True,
                "allowUnsandboxedCommands": False,
                # Python SDK 默认在沙箱起不来时静默裸跑（docs python.md）；这里必须失败。
                "failIfUnavailable": True,
            },
            enable_file_checkpointing=True,
            thinking={"type": "adaptive", "display": "summarized"},
            resume=run.resume_session_id,
        )

    def _selected_skills(self) -> Literal["all"] | list[str]:
        selection = self.skills.runtime_selection()
        allowed = self.policy.get().allowed_skill_ids
        if not allowed:
            return selection
        names = (
            [item.id for item in self.skills.list() if item.enabled]
            if selection == "all"
            else list(selection)
        )
        return self.policy.filter_skill_ids(names)

    def _hook(self, run_id: str, workspace: Path):
        run = self.ledger.get_run(run_id)

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
                if input_data.get("agent_id") and _is_subagent_write_tool(str(tool_name)):
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": (
                                "Subagents are read-only; return findings to the main Agent."
                            ),
                        }
                    }
                candidate_key = {
                    "Read": "file_path",
                    "Write": "file_path",
                    "Edit": "file_path",
                    "Glob": "path",
                    "Grep": "path",
                }.get(str(tool_name))
                if candidate_key and tool_input.get(candidate_key):
                    candidate = tool_input.get(candidate_key)
                    # 读权限与动作权限分开（PRD §4.10"Read/Glob/Grep 项目内自动允许"）：
                    # 写入围栏=工作区；只读围栏=白名单（2026-08-24 Q2 拍板，窄档）：
                    #   自己项目目录（账本 project.json 等原始数据）+ backend/src（系统
                    #   源码——在线理解字段与工具背后的业务逻辑，教材"开放代码库"）。
                    # backend/data 下其他项目的账本不在名单内：能力越自由，权限越收窄。
                    if str(tool_name) in ("Write", "Edit"):
                        fences = [workspace]
                        fence_name = "project workspace"
                    else:
                        fences = [workspace.parent, (BACKEND_DIR / "src").resolve()]
                        fence_name = "read allowlist (project directory, backend/src)"
                    if not isinstance(candidate, str) or not any(
                        _contained(workspace, fence, candidate) for fence in fences
                    ):
                        return {
                            "hookSpecificOutput": {
                                "hookEventName": "PreToolUse",
                                "permissionDecision": "deny",
                                "permissionDecisionReason": (
                                    f"{tool_name} path is outside the {fence_name}."
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
                    if run.schedule_id is not None:
                        return {
                            "hookSpecificOutput": {
                                "hookEventName": "PreToolUse",
                                "permissionDecision": "deny",
                                "permissionDecisionReason": (
                                    "Scheduled runs cannot use Bash; shell commands require "
                                    "an attended run."
                                ),
                            }
                        }
                    if tool_input.get("dangerouslyDisableSandbox"):
                        return {
                            "hookSpecificOutput": {
                                "hookEventName": "PreToolUse",
                                "permissionDecision": "deny",
                                "permissionDecisionReason": "Unsandboxed commands are disabled.",
                            }
                        }
                    # PRD §4.10 v2.2（2026-08-24 拍板：教材为最终权威）：沙箱内 Bash 走
                    # SDK 原生默认 autoAllowBashIfSandboxed 自动批准（副作用被沙箱围住，
                    # workspace 之外写不了、网络出不去）；hook 只拦逃逸沙箱这一条。
                    # 删除文件任何模式必须确认（Cowork 铁律）：把 rm/Delete 打回 ask。
                    if is_destructive_delete(str(tool_name), tool_input):
                        return {
                            "hookSpecificOutput": {
                                "hookEventName": "PreToolUse",
                                "permissionDecision": "ask",
                                "permissionDecisionReason": (
                                    "Deleting files always requires explicit confirmation."
                                ),
                            }
                        }
                if is_destructive_delete(str(tool_name), tool_input):
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "ask",
                            "permissionDecisionReason": (
                                "Deleting files always requires explicit confirmation."
                            ),
                        }
                    }
                connected_snapshot = (
                    tool_name == TOOL_SNAPSHOT_PAGE
                    and tool_input.get("use_connected_browser") is True
                )
                if tool_name == TOOL_ACT_ON_PAGE or connected_snapshot:
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "ask",
                            "permissionDecisionReason": (
                                "Browser actions and connected-browser reads require "
                                "confirmation for every invocation."
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
                    run_id=run_id,
                    event_type=EventType.SUBAGENT_STARTED,
                    data={
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
    def _record_constraints(
        self, project_id: str, answers: dict[str, str | list[str]]
    ) -> None:
        lines: list[str] = []
        for question, answer in answers.items():
            value = (
                "、".join(str(item) for item in answer)
                if isinstance(answer, list)
                else str(answer)
            )
            if value.strip():
                lines.append(f"{question}: {value.strip()}")
        if not lines:
            return

        def mutate(project) -> None:
            for line in lines:
                if line not in project.constraints:
                    project.constraints.append(line)

        self.ledger.update_project(project_id, mutate)

    def _query_prompt(self, run: RunRecord) -> str:
        try:
            project = self.ledger.get_project(run.project_id)
        except Exception:
            return run.prompt
        if not project.constraints:
            return run.prompt
        listed = "\n".join(f"- {item}" for item in project.constraints[-20:])
        return f"{run.prompt}\n\n【已确认的项目约束】\n{listed}"

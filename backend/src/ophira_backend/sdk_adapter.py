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

from .agent_core import MAX_MEMORY_ITEMS
from .agents import build_deep_research_agents
from .commercial_actions import CommercialActionService
from .config import BACKEND_DIR, Settings
from .connectors import ConnectorRegistry
from .constants import (
    AUTOMATION_MCP_SERVER_NAME,
    BROWSER_MCP_SERVER_NAME,
    CLARIFYING_TOOL,
    DELEGATION_TOOL,
    DELETE_BUILTIN_TOOLS,
    ENVELOPE_APPROVED_MCP_TOOL_NAMES,
    PRESENTATION_TOOL_NAMES,
    READ_ONLY_BUILTIN_TOOLS,
    READ_ONLY_MCP_TOOL_NAMES,
    SHELL_BUILTIN_TOOLS,
    TOOL_ACT_ON_PAGE,
    TOOL_FETCH_PAGE_SNAPSHOT,
    TOOL_GET_PROJECT_CONTEXT,
    TOOL_LIST_STANDING_INTENTS,
    TOOL_READ_SOURCE_RECEIPT,
    TOOL_SEARCH_EVIDENCE,
    TOOL_SEARCH_PUBLIC_WEB,
    TOOL_SNAPSHOT_PAGE,
    TOOL_TRIGGER_STANDING_INTENT,
    WRITE_BUILTIN_TOOLS,
    EventType,
    PermissionDecision,
    PermissionModePolicy,
    PresentationFrameType,
    ProviderErrorCode,
    RunMode,
)
from .context_protocol import (
    PreparedRunContext,
    prepare_run_context,
)
from .destructive import is_destructive_delete
from .events import EventPublisher, redact
from .ingest import file_needs_markdown_companion
from .ledger_tools import build_mcp_servers
from .memory import MemoryProvider
from .models import EventActor, ProviderPreflightResult, RunRecord, utc_now
from .permissions import PermissionBroker
from .policy import OrgPolicyStore
from .presentation_tools import presentation_event_response, presentation_frame_event
from .providers import (
    ProviderConfigurationError,
    ProviderRegistry,
    classify_provider_error,
    provider_error_message,
    successful_preflight,
)
from .skills import SkillRegistry, SkillRuntimeView
from .source_receipts import (
    attach_source_receipts,
    model_visible_source_receipt_context,
)
from .storage import FileLedger
from .untrusted import (
    UNTRUSTED_DATA_NOTICE,
    UntrustedSource,
    sanitize_untrusted_tool_output,
)


class AgentRunError(RuntimeError):
    pass


_CONTROL_PLANE_SECRET_ENV_NAMES = (
    "OPHIRA_CHANNEL_WEBHOOK_URL",
    "OPHIRA_COMMERCIAL_APPROVAL_SECRET",
    "OPHIRA_TRUSTED_GATEWAY_AUTH_SECRET",
    "OPHIRA_FEISHU_APP_ID",
    "OPHIRA_FEISHU_APP_SECRET",
    "OPHIRA_FEISHU_ENCRYPT_KEY",
)


def _control_plane_secret_shadows() -> dict[str, str]:
    """Override host env values before the SDK worker process is created."""

    return {name: "" for name in _CONTROL_PLANE_SECRET_ENV_NAMES}


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


_UNTRUSTED_LEDGER_TOOL_NAMES = frozenset(
    {TOOL_GET_PROJECT_CONTEXT, TOOL_SEARCH_EVIDENCE, TOOL_READ_SOURCE_RECEIPT}
)
_UNTRUSTED_JSON_TOOL_NAMES = frozenset(
    {*_UNTRUSTED_LEDGER_TOOL_NAMES, TOOL_SEARCH_PUBLIC_WEB, TOOL_FETCH_PAGE_SNAPSHOT}
)


def _post_tool_untrusted_source(
    tool_name: str,
    tool_input: dict[str, Any],
    workspace: Path,
) -> UntrustedSource | None:
    """Classify tool results at the last boundary before Claude consumes them."""
    if tool_name == "WebSearch":
        return UntrustedSource.WEB_SEARCH
    if tool_name == "WebFetch":
        return UntrustedSource.WEB_PAGE
    if tool_name == TOOL_SEARCH_PUBLIC_WEB:
        return UntrustedSource.WEB_SEARCH
    if tool_name == TOOL_FETCH_PAGE_SNAPSHOT:
        return UntrustedSource.WEB_PAGE
    if tool_name in _UNTRUSTED_LEDGER_TOOL_NAMES:
        return UntrustedSource.PROJECT_LEDGER
    if tool_name == "Bash":
        # Bash can print raw uploads or ../project.json even when the command
        # itself is sandboxed and otherwise safe.
        return UntrustedSource.PROJECT_FILE
    if tool_name in {"Read", "Grep"}:
        candidate_key = "file_path" if tool_name == "Read" else "path"
        candidate = tool_input.get(candidate_key)
        if isinstance(candidate, str) and _contained(
            workspace,
            (BACKEND_DIR / "src").resolve(),
            candidate,
        ):
            # Repository source is the intentionally trusted code-reading fence.
            return None
        # PreToolUse already restricts an explicit path to this project (or the
        # source tree above). Missing Grep paths search cwd, which is the project
        # workspace and therefore also data-plane content.
        return UntrustedSource.PROJECT_FILE
    return None


def _is_subagent_restricted_tool(tool_name: str) -> bool:
    # snapshot_page registers Artifact/Evidence immediately, so it is a
    # main-Agent write even though normal L1 reads are auto-approved for users.
    # Presentation tools are read-only, but final UI composition is likewise
    # owned by the accountable main Agent rather than a research branch.
    if tool_name == TOOL_SNAPSHOT_PAGE or tool_name in PRESENTATION_TOOL_NAMES:
        return True
    if tool_name in {
        *WRITE_BUILTIN_TOOLS,
        *DELETE_BUILTIN_TOOLS,
        *SHELL_BUILTIN_TOOLS,
    }:
        return True
    return tool_name.startswith("mcp__") and tool_name not in READ_ONLY_MCP_TOOL_NAMES


_STANDING_INTENT_ALLOWED_TOOLS = frozenset(
    {
        *READ_ONLY_BUILTIN_TOOLS,
        *READ_ONLY_MCP_TOOL_NAMES,
        TOOL_LIST_STANDING_INTENTS,
        TOOL_TRIGGER_STANDING_INTENT,
        "Skill",
    }
)


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
        commercial_actions: CommercialActionService | None = None,
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
        self.commercial_actions = commercial_actions or CommercialActionService(
            settings.data_dir / "commercial-actions",
            ledger=ledger,
            policy=self.policy,
        )
        self._clients: dict[str, ClaudeSDKClient] = {}
        self._clients_lock = asyncio.Lock()
        self._prepared_contexts: dict[str, PreparedRunContext] = {}

    async def run(self, run: RunRecord) -> None:
        self._require_provider(run)
        snapshot = self._policy_skill_snapshot(run.skill_snapshot)
        self._prepare_context(run, snapshot)
        try:
            with self.skills.runtime_snapshot_lease(snapshot) as skill_view:
                options = self._build_options(run, skill_view=skill_view)
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
        finally:
            self._prepared_contexts.pop(run.id, None)

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
        preflight_snapshot = self._policy_skill_snapshot(self.skills.snapshot())
        skill_view = self.skills.materialize_snapshot(preflight_snapshot)
        options = ClaudeAgentOptions(
            cwd=preflight_dir,
            tools={"type": "preset", "preset": "claude_code"},
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": "这是只读连通性预检。不得修改文件或执行外部副作用。",
            },
            plugins=skill_view.plugin_paths,
            setting_sources=[],
            skills=skill_view.skill_ids,
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
            env={
                **self.providers.build_worker_env(provider_profile_id),
                **_control_plane_secret_shadows(),
            },
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
            with self.skills.runtime_snapshot_lease(preflight_snapshot):
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
                                isinstance(block, ToolUseBlock)
                                and block.name == health_tool_name
                                for block in message.content
                            )
                        elif isinstance(message, ResultMessage):
                            sdk_session_id = message.session_id or sdk_session_id
                            if message.is_error:
                                details = message.errors or [
                                    message.result or message.subtype
                                ]
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
            env = self.providers.build_worker_env(run.provider_profile_id)
        else:
            env = self.settings.claude_sdk_env()
        # The model can use Bash inside its sandbox. Shadow human-gateway and
        # outbound-channel credentials inherited from the host process.
        env.update(_control_plane_secret_shadows())
        if self.connectors is not None:
            for connector in self.connectors.list():
                if connector.secret_env:
                    env[connector.secret_env] = ""
        return env

    def _system_append(self, run: RunRecord) -> str:
        prepared = self._prepared_contexts.get(run.id)
        if prepared is None:
            prepared = self._prepare_context(
                run,
                self._policy_skill_snapshot(run.skill_snapshot),
            )
        return prepared.system_prompt_append

    def _prepare_context(
        self,
        run: RunRecord,
        effective_skill_snapshot: list[dict[str, object]],
    ) -> PreparedRunContext:
        prepared = prepare_run_context(
            self.ledger,
            self.memory,
            run,
            effective_skill_snapshot,
            memory_limit=MAX_MEMORY_ITEMS,
        )
        self._prepared_contexts[run.id] = prepared
        return prepared

    def _build_options(
        self,
        run: RunRecord,
        *,
        skill_view: SkillRuntimeView | None = None,
    ) -> ClaudeAgentOptions:
        agent = self.ledger.get_agent(run.agent_id) if run.agent_id else None
        workspace = self.ledger.workspace_for_project(run.project_id).resolve()
        deep_research = run.mode == RunMode.DEEP_RESEARCH

        async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context):
            # §6.1 红线：账本写入与自动化承诺只属主 Agent。支线子代理（原生
            # general-purpose/Explore 等）做研究与计算，把发现带回主线落账。
            subagent_id = getattr(context, "agent_id", None)
            if subagent_id and _is_subagent_restricted_tool(tool_name):
                return PermissionResultDeny(
                    message=(
                        "子代理只读：文件、Shell、账本、最终 UI 与外部动作只属主 "
                        "Agent（PRD §6.1）。请把研究结论带回主线，由主 Agent 执行动作、"
                        "记录或呈现。"
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
        if skill_view is None:
            # Inspection/tests may build options without starting a session. A
            # real run passes a leased view that remains pinned until SDK exit.
            skill_view = self.skills.materialize_snapshot(
                self._policy_skill_snapshot(run.skill_snapshot)
            )
        mcp_servers = build_mcp_servers(
            self.ledger,
            run.project_id,
            memory=self.memory,
            agent_id=run.agent_id,
            settings=self.settings,
            run_id=run.id,
            policy=org_policy,
            connectors=self.connectors,
            commercial_actions=self.commercial_actions,
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
                bound_intent_id=run.standing_intent_id,
            )
        unattended_intent = run.standing_intent_id is not None
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
                *(
                    [TOOL_LIST_STANDING_INTENTS, TOOL_TRIGGER_STANDING_INTENT]
                    if unattended_intent
                    else ENVELOPE_APPROVED_MCP_TOOL_NAMES
                ),
                # 2026-08-24 拍板"按原生能力来做"：支线子代理开放，派遣动作
                # 自动放行（无副作用）；子代理内部的工具调用仍逐项过权限，
                # 账本写入在 can_use_tool 对子代理代码级拒绝。
                *([] if unattended_intent else [DELEGATION_TOOL]),
            ],
            disallowed_tools=[],
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": self._system_append(run),
            },
            plugins=skill_view.plugin_paths,
            skills=skill_view.skill_ids,
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
            max_budget_usd=(
                min(self.settings.max_budget_usd, run.max_budget_usd)
                if run.max_budget_usd is not None
                else self.settings.max_budget_usd
            )
            * budget_scale,
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

    def _policy_skill_snapshot(
        self, snapshot: list[dict[str, Any]]
    ) -> list[dict[str, object]]:
        """Filter before Plugin materialization so disallowed Skills are not discoverable.

        The SDK documents that direct slash-command dispatch bypasses the `skills`
        allowlist. Omitting a disallowed package from the per-run Plugin root is the
        filesystem enforcement boundary.
        """
        allowed = self.policy.get().allowed_skill_ids
        if not allowed:
            return [dict(item) for item in snapshot]
        allowed_ids = set(
            self.policy.filter_skill_ids(
                [str(item.get("id", "")) for item in snapshot]
            )
        )
        return [dict(item) for item in snapshot if item.get("id") in allowed_ids]

    def _hook(self, run_id: str, workspace: Path):
        run = self.ledger.get_run(run_id)

        async def handle(input_data: dict[str, Any], callback_tool_use_id, _context):
            event_name = input_data.get("hook_event_name", "")
            actor = self._hook_actor(input_data)
            raw_tool_use_id = input_data.get("tool_use_id") or callback_tool_use_id
            tool_use_id = raw_tool_use_id if isinstance(raw_tool_use_id, str) else None
            if event_name == "PreToolUse":
                tool_name = input_data.get("tool_name")
                tool_input = input_data.get("tool_input", {})
                if not isinstance(tool_input, dict):
                    tool_input = {}
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
                if run.standing_intent_id is not None:
                    if str(tool_name) not in _STANDING_INTENT_ALLOWED_TOOLS:
                        return {
                            "hookSpecificOutput": {
                                "hookEventName": "PreToolUse",
                                "permissionDecision": "deny",
                                "permissionDecisionReason": (
                                    "Standing-intent checks are unattended read-only "
                                    "evaluations; only their bound trigger tool may write "
                                    "the inbox."
                                ),
                            }
                        }
                    if (
                        tool_name == TOOL_TRIGGER_STANDING_INTENT
                        and tool_input.get("intent_id") != run.standing_intent_id
                    ):
                        return {
                            "hookSpecificOutput": {
                                "hookEventName": "PreToolUse",
                                "permissionDecision": "deny",
                                "permissionDecisionReason": (
                                    "This evaluation can trigger only its bound standing intent."
                                ),
                            }
                        }
                if input_data.get("agent_id") and _is_subagent_restricted_tool(
                    str(tool_name)
                ):
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": (
                                "Subagents are read-only and cannot own final UI; return "
                                "findings to the main Agent."
                            ),
                        }
                    }
                if tool_name in PRESENTATION_TOOL_NAMES:
                    project = self.ledger.get_project(run.project_id)
                    partial = presentation_frame_event(
                        tool_name=str(tool_name),
                        tool_input=tool_input,
                        project_id=run.project_id,
                        project_revision=project.revision,
                        tool_use_id=tool_use_id,
                        frame_type=PresentationFrameType.PARTIAL,
                    )
                    if partial is not None:
                        await self.events.publish(
                            run_id,
                            EventType.UI_PRESENTATION,
                            partial,
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
                    if (
                        tool_name == "Read"
                        and resolved.is_relative_to(workspace / "uploads")
                        and file_needs_markdown_companion(resolved)
                    ):
                        companion = resolved.with_name(resolved.name + ".md")
                        if (
                            companion.is_file()
                            and not companion.is_symlink()
                            and companion.resolve().is_relative_to(workspace)
                        ):
                            next_action = (
                                f"Read {companion.relative_to(workspace).as_posix()!r} "
                                "instead and respect its extraction failure, missing-page, "
                                "and truncation notices."
                            )
                        else:
                            next_action = (
                                "The Markdown companion is missing; ask the user to "
                                "re-upload with local OCR configured, or provide a text version."
                            )
                        return {
                            "hookSpecificOutput": {
                                "hookEventName": "PreToolUse",
                                "permissionDecision": "deny",
                                "permissionDecisionReason": (
                                    "Uploaded PDF/images must enter the Agent through "
                                    "Markdown ingestion, not native binary Read. " + next_action
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
                tool_name = str(input_data.get("tool_name") or "")
                tool_input = input_data.get("tool_input", {})
                if not isinstance(tool_input, dict):
                    tool_input = {}
                tool_response = input_data.get("tool_response")
                untrusted_source = _post_tool_untrusted_source(
                    tool_name,
                    tool_input,
                    workspace,
                )
                hook_specific_output: dict[str, Any] | None = None
                if untrusted_source is not None:
                    sanitized = sanitize_untrusted_tool_output(
                        tool_response,
                        untrusted_source,
                        parse_json_strings=(
                            tool_name in _UNTRUSTED_JSON_TOOL_NAMES
                        ),
                    )
                    tool_response = sanitized.value
                    hook_specific_output = {
                        "hookEventName": "PostToolUse",
                        "additionalContext": UNTRUSTED_DATA_NOTICE,
                    }
                receipt_attachment = attach_source_receipts(
                    self.ledger,
                    run_id,
                    tool_name,
                    tool_input,
                    tool_response,
                    delegated=bool(input_data.get("agent_id")),
                )
                tool_response = receipt_attachment.tool_response
                receipt_context = model_visible_source_receipt_context(
                    receipt_attachment.receipt_ids
                )
                if hook_specific_output is not None and receipt_context is not None:
                    # Built-in renderers can keep host-added structured fields in
                    # transcript metadata without placing them in Claude's visible
                    # tool-result text. additionalContext is the documented
                    # model-visible channel, so mirror only the IDs minted by this
                    # exact hook invocation. External payload text never enters it.
                    hook_specific_output["additionalContext"] = (
                        f"{UNTRUSTED_DATA_NOTICE}\n\n{receipt_context}"
                    )
                if hook_specific_output is not None and "tool_response" in input_data:
                    # Claude Agent SDK 0.2.x accepts updatedToolOutput for
                    # built-in and MCP tools. Replacing the output is the
                    # enforcement boundary; additionalContext alone would
                    # leave the raw data-plane injection visible to Claude.
                    hook_specific_output["updatedToolOutput"] = tool_response
                structured_response = None
                presentation_frame = None
                if tool_name in PRESENTATION_TOOL_NAMES:
                    structured_response = presentation_event_response(
                        tool_response,
                        run.project_id,
                    )
                    if structured_response is not None:
                        tool_response = structured_response
                        presentation = structured_response["presentation"]
                        presentation_frame = presentation_frame_event(
                            tool_name=tool_name,
                            tool_input=tool_input,
                            project_id=run.project_id,
                            project_revision=presentation["project_revision"],
                            tool_use_id=tool_use_id,
                            frame_type=PresentationFrameType.FINAL,
                            presentation_response=structured_response,
                        )
                    else:
                        project = self.ledger.get_project(run.project_id)
                        presentation_frame = presentation_frame_event(
                            tool_name=tool_name,
                            tool_input=tool_input,
                            project_id=run.project_id,
                            project_revision=project.revision,
                            tool_use_id=tool_use_id,
                            frame_type=PresentationFrameType.DISCARDED,
                        )
                await self.events.publish(
                    run_id,
                    EventType.TOOL_COMPLETED,
                    {
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                        "tool_response": tool_response,
                    },
                    actor=actor,
                    tool_use_id=tool_use_id,
                )
                if presentation_frame is not None and not input_data.get("agent_id"):
                    await self.events.publish(
                        run_id,
                        EventType.UI_PRESENTATION,
                        presentation_frame,
                        actor=actor,
                        tool_use_id=tool_use_id,
                    )
                if hook_specific_output is not None:
                    return {"hookSpecificOutput": hook_specific_output}
            elif event_name == "PostToolUseFailure":
                tool_name = str(input_data.get("tool_name") or "")
                tool_input = input_data.get("tool_input", {})
                if not isinstance(tool_input, dict):
                    tool_input = {}
                error = input_data.get("error")
                untrusted_source = _post_tool_untrusted_source(
                    tool_name,
                    tool_input,
                    workspace,
                )
                if untrusted_source is not None:
                    # A failed downloader/read/command can still echo arbitrary
                    # remote or file content. Failure hooks cannot replace the
                    # SDK's error result, so keep the durable/UI event clean and
                    # explicitly tell the model to treat its copy as data.
                    error = sanitize_untrusted_tool_output(
                        error,
                        untrusted_source,
                    ).value
                await self.events.publish(
                    run_id,
                    EventType.TOOL_FAILED,
                    {
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                        "error": error,
                        "is_interrupt": input_data.get("is_interrupt", False),
                    },
                    actor=actor,
                    tool_use_id=tool_use_id,
                )
                if tool_name in PRESENTATION_TOOL_NAMES and not input_data.get("agent_id"):
                    project = self.ledger.get_project(run.project_id)
                    discarded = presentation_frame_event(
                        tool_name=tool_name,
                        tool_input=tool_input,
                        project_id=run.project_id,
                        project_revision=project.revision,
                        tool_use_id=tool_use_id,
                        frame_type=PresentationFrameType.DISCARDED,
                    )
                    if discarded is not None:
                        await self.events.publish(
                            run_id,
                            EventType.UI_PRESENTATION,
                            discarded,
                            actor=actor,
                            tool_use_id=tool_use_id,
                        )
                if untrusted_source is not None:
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PostToolUseFailure",
                            "additionalContext": UNTRUSTED_DATA_NOTICE,
                        }
                    }
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
        prepared = self._prepared_contexts.get(run.id)
        if prepared is None:
            prepared = self._prepare_context(
                run,
                self._policy_skill_snapshot(run.skill_snapshot),
            )
        return f"{run.prompt}\n\n{prepared.envelope}"

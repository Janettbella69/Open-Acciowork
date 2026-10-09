"""Composition root for the business tools a Run receives.

``sdk_adapter`` drives the Claude Agent SDK session and maps its messages; it stays the
only SDK integration point and must not know which business subsystems exist. This
module is the one place that does: it owns the subsystem services, decides which MCP
servers a Run is given, and answers the adapter's questions about reviewed remote MCP
tools, source receipts, presentation cards, connector secrets and native desktop
control through ``RunToolSurface``.
"""

from __future__ import annotations

from typing import Any

from .auth import ProjectAccessStore
from .automation_tools import build_automation_mcp_server
from .commercial_actions import CommercialActionService
from .config import Settings
from .connectors import ConnectorRegistry
from .constants import AUTOMATION_MCP_SERVER_NAME, DESKTOP_MCP_SERVER_NAME, RunMode
from .desktop_control import DesktopControl
from .desktop_tools import build_desktop_mcp_server
from .events import EventPublisher
from .gmail_access import GmailAccessService
from .ledger_tools import build_mcp_servers
from .marketplace_access import MarketplaceAccessService
from .memory import MemoryProvider
from .models import OrgPolicy, RunRecord
from .outlook_access import OutlookAccessService
from .permissions import PermissionBroker
from .policy import OrgPolicyStore
from .presentation_tools import presentation_event_response, presentation_frame_event
from .project_events import ProjectEventProjector
from .providers import ProviderRegistry
from .sdk_adapter import ClaudeAgentRuntime
from .shop_browser import ShopBrowserService
from .skill_proposals import SkillProposalService
from .skills import SkillRegistry
from .source_receipts import (
    RemoteMcpReceiptSource,
    SourceReceiptAttachment,
    attach_source_receipts,
    model_visible_source_receipt_context,
)
from .storage import FileLedger
from .user_mcp import UserMcpConnector, UserMcpRegistry, UserMcpRunScope


class RunAssembler:
    """Owns the subsystem services behind a Run's tool surface (``RunToolSurface``)."""

    def __init__(
        self,
        settings: Settings,
        ledger: FileLedger,
        events: EventPublisher,
        *,
        memory: MemoryProvider | None,
        skills: SkillRegistry,
        policy: OrgPolicyStore,
        access: ProjectAccessStore,
        intents=None,
        notifications=None,
        connectors: ConnectorRegistry | None = None,
        commercial_actions: CommercialActionService | None = None,
        task_experiences=None,
        project_events: ProjectEventProjector | None = None,
        skill_proposals: SkillProposalService | None = None,
        marketplace_access: MarketplaceAccessService | None = None,
        gmail_access: GmailAccessService | None = None,
        outlook_access: OutlookAccessService | None = None,
        shop_browsers: ShopBrowserService | None = None,
        user_mcp: UserMcpRegistry | None = None,
        desktop_control: DesktopControl | None = None,
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.memory = memory
        self.policy = policy
        self.project_events = project_events or ProjectEventProjector(ledger, events, memory)
        self.intents = intents
        self.notifications = notifications
        self.task_experiences = task_experiences
        self.skill_proposals = skill_proposals or SkillProposalService(
            ledger, skills, access=access, auth_mode=settings.auth_mode,
        )
        self.connectors = connectors
        self.user_mcp = user_mcp
        self.desktop_control = desktop_control
        self.marketplace_access = marketplace_access or (
            MarketplaceAccessService(
                ledger,
                connectors,
                settings=settings,
                access=access,
            )
            if connectors is not None
            else None
        )
        self.gmail_access = gmail_access
        self.outlook_access = outlook_access
        self.shop_browsers = shop_browsers or (
            ShopBrowserService(self.marketplace_access, policy)
            if self.marketplace_access
            else None
        )
        self.commercial_actions = commercial_actions or CommercialActionService(
            settings.data_dir / "commercial-actions",
            ledger=ledger,
            policy=policy,
        )

    def attach_desktop_control(self, service: DesktopControl) -> None:
        """Bind the native-only adapter after the desktop sidecar constructs the API."""
        if self.desktop_control is not None:
            raise ValueError("desktop control is already attached")
        if service.ledger is not self.ledger or service.policy is not self.policy:
            raise ValueError("desktop control must share the Run ledger and policy")
        self.desktop_control = service

    def has_desktop_control(self) -> bool:
        return self.desktop_control is not None

    def remote_run_scope(self) -> UserMcpRunScope | None:
        return self.user_mcp.run_scope() if self.user_mcp is not None else None

    def connector_secret_env_names(self) -> list[str]:
        if self.connectors is None:
            return []
        return [
            connector.secret_env for connector in self.connectors.list() if connector.secret_env
        ]

    def task_experience_context(self, run: RunRecord) -> str:
        if self.task_experiences is None:
            return ""
        return self.task_experiences.context_for_run(run)

    def memory_provider(self) -> MemoryProvider | None:
        return self.memory

    def remote_connectors(self, run: RunRecord) -> dict[str, UserMcpConnector]:
        if (self.user_mcp is None or run.mode == RunMode.PLAN or run.manual_compaction
                or run.standing_intent_id is not None):
            return {}
        connectors: dict[str, UserMcpConnector] = {}
        for raw in run.mcp_snapshot:
            try:
                pinned = UserMcpConnector.model_validate(raw)
                current = self.user_mcp.get(run.project_id, pinned.id)
                self.user_mcp._check_url(current.url)
                if current.credential_configured and not self.user_mcp.vault.has(
                    self.user_mcp._vault_id(run.project_id, current.id)
                ):
                    continue
                if current == pinned and current.enabled and current.reviewed_at is not None:
                    connectors[current.server_name] = current
            except (KeyError, ValueError):
                continue
        return connectors

    def remote_mcp_decision(self, run: RunRecord, tool_name: str) -> str | None:
        if not tool_name.startswith("mcp__user_"):
            return None
        for connector in self.remote_connectors(run).values():
            for item in connector.tools:
                if connector.tool_name(item.name) == tool_name:
                    return item.policy
        return "deny"

    def remote_tool_names(
        self, remote_connectors: dict[str, UserMcpConnector], policy: str,
    ) -> list[str]:
        return [
            connector.tool_name(item.name)
            for connector in remote_connectors.values()
            for item in connector.tools if item.policy == policy
        ]

    def attach_source_receipts(
        self,
        run: RunRecord,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_response: Any,
        *,
        delegated: bool,
    ) -> SourceReceiptAttachment:
        remote_mcp_source = None
        if tool_name.startswith("mcp__user_") and not delegated:
            for connector in self.remote_connectors(run).values():
                for reviewed_tool in connector.tools:
                    if (reviewed_tool.policy != "deny"
                            and connector.tool_name(reviewed_tool.name) == tool_name):
                        remote_mcp_source = RemoteMcpReceiptSource(
                            connector_id=connector.id,
                            connector_name=connector.name,
                            connector_revision=connector.revision,
                            endpoint_url=connector.url,
                            remote_tool_name=reviewed_tool.name,
                        )
                        break
                if remote_mcp_source is not None:
                    break
        return attach_source_receipts(
            self.ledger,
            run.id,
            tool_name,
            tool_input,
            tool_response,
            delegated=delegated,
            remote_mcp_source=remote_mcp_source,
        )

    def source_receipt_context(self, receipt_ids: tuple[str, ...]) -> str | None:
        return model_visible_source_receipt_context(receipt_ids)

    def presentation_frame_event(self, **fields: Any) -> dict[str, Any] | None:
        return presentation_frame_event(**fields)

    def presentation_event_response(self, value: Any, project_id: str) -> dict[str, Any] | None:
        return presentation_event_response(value, project_id)

    def mcp_servers(
        self,
        run: RunRecord,
        org_policy: OrgPolicy,
        remote_connectors: dict[str, UserMcpConnector],
        *,
        user_mcp_scope: UserMcpRunScope | None = None,
    ) -> dict[str, Any]:
        mcp_servers = (
            {} if run.mode == RunMode.PLAN or run.manual_compaction else build_mcp_servers(
                self.ledger,
                run.project_id,
                memory=self.memory,
                agent_id=run.agent_id,
                settings=self.settings,
                run_id=run.id,
                policy=org_policy,
                connectors=self.connectors,
                commercial_actions=self.commercial_actions,
                project_events=self.project_events,
                skill_proposals=self.skill_proposals,
                marketplace_access=self.marketplace_access,
                gmail_access=self.gmail_access,
                outlook_access=self.outlook_access,
                shop_browsers=self.shop_browsers,
            )
        )
        if (run.mode != RunMode.PLAN and not run.manual_compaction
                and self.intents is not None and self.notifications is not None):
            mcp_servers[AUTOMATION_MCP_SERVER_NAME] = build_automation_mcp_server(
                self.ledger,
                self.intents,
                self.notifications,
                run.project_id,
                run_id=run.id,
                schedule_id=run.schedule_id,
                bound_intent_id=run.standing_intent_id,
                task_experiences=self.task_experiences,
            )
        if (
            self.desktop_control is not None
            and org_policy.computer_control_enabled
            and org_policy.allowed_desktop_bundle_ids
            and run.mode != RunMode.PLAN
            and not run.manual_compaction
            and run.schedule_id is None
            and run.event_trigger_id is None
            and run.standing_intent_id is None
        ):
            mcp_servers[DESKTOP_MCP_SERVER_NAME] = build_desktop_mcp_server(
                self.desktop_control, run.project_id, run.id,
            )
        if self.user_mcp is not None:
            for connector in remote_connectors.values():
                if any(item.policy != "deny" for item in connector.tools):
                    mcp_servers[connector.server_name] = self.user_mcp.sdk_server(
                        connector, run_scope=user_mcp_scope,
                    )
        return mcp_servers


def build_agent_runtime(
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
    task_experiences=None,
    access=None,
    project_events: ProjectEventProjector | None = None,
    skill_proposals: SkillProposalService | None = None,
    marketplace_access: MarketplaceAccessService | None = None,
    gmail_access: GmailAccessService | None = None,
    outlook_access: OutlookAccessService | None = None,
    shop_browsers: ShopBrowserService | None = None,
    user_mcp: UserMcpRegistry | None = None,
    desktop_control: DesktopControl | None = None,
) -> ClaudeAgentRuntime:
    """Wire the SDK adapter to every subsystem that contributes tools to a Run."""
    providers = providers or ProviderRegistry(settings)
    skills = skills or SkillRegistry(
        settings.capability_plugin_dir,
        settings.data_dir / "skills",
    )
    access = access or ProjectAccessStore(settings.data_dir / "auth" / "projects")
    policy = policy or OrgPolicyStore(settings.data_dir / "org" / "policy.json")
    assembler = RunAssembler(
        settings,
        ledger,
        events,
        memory=memory,
        skills=skills,
        policy=policy,
        access=access,
        intents=intents,
        notifications=notifications,
        connectors=connectors,
        commercial_actions=commercial_actions,
        task_experiences=task_experiences,
        project_events=project_events,
        skill_proposals=skill_proposals,
        marketplace_access=marketplace_access,
        gmail_access=gmail_access,
        outlook_access=outlook_access,
        shop_browsers=shop_browsers,
        user_mcp=user_mcp,
        desktop_control=desktop_control,
    )
    return ClaudeAgentRuntime(
        settings,
        ledger,
        events,
        permissions,
        providers=providers,
        skills=skills,
        policy=policy,
        access=access,
        assembler=assembler,
    )

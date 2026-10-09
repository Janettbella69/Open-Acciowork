"""模块边界门禁：每个后端模块有且只有一个主人，跨子系统依赖只减不增。

与 ``test_arch_gates.py`` 同为 PRD §6.1 的 L1 静态门禁。它回答的问题是「这个概念归谁管」——
2026-09 两条分支各写了一套项目删除 / 导出 / 凭证导出，根因就是没有一张归属表。

规则：

1. 下面的归属表必须覆盖 ``ophira_backend`` 下的每一个模块，且每个模块只出现一次。
   新模块不登记就不能合入。
2. ``KERNEL`` 只能 import ``KERNEL``；子系统只能 import ``KERNEL`` 和自己的成员；
   ``COMPOSITION_ROOTS``（HTTP 装配 ``main``、Run 工具面装配 ``run_assembly``、MCP 装配
   ``ledger_tools``、冷备装配 ``application_backup``、桌面入口 ``desktop_server``）可以 import
   一切——装配就是它们的职责。
3. 现存的跨子系统 import 全部列在 ``KNOWN_CROSS_IMPORTS``，是待偿还的债务：只能减，不能增。
   需要跨子系统能力时，把它经 ``KERNEL`` 或目标子系统的公开接口暴露，或把模块挪进正确的
   子系统；不要往清单里加行。修掉一条后把它从清单删掉，否则本测试同样失败。

函数内的延迟 import 同样计入：它只是把循环依赖藏起来，不是解开。
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_SRC = Path(__file__).resolve().parents[1] / "src" / "ophira_backend"

# 共享内核：词汇表、持久化、鉴权、脱敏和纯数据模型。任何模块都可以依赖它，它不依赖任何子系统。
KERNEL = frozenset({
    "__init__", "constants", "models", "config", "storage", "events", "auth", "policy",
    "untrusted", "destructive", "grants", "project_scope", "project_lifecycle", "templates",
    "worker_contract", "project_template_models", "project_export_models",
    "project_deletion_models", "skill_proposal_models", "skill_import_models",
    "skill_proposal_files", "backup_files", "artifact_files", "web_tools", "providers",
})

# 组合根：职责就是把所有子系统装起来，所以允许 import 一切；它们自己不应被子系统 import。
COMPOSITION_ROOTS = frozenset({
    "main", "run_assembly", "ledger_tools", "application_backup", "desktop_server",
})

SUBSYSTEMS: dict[str, frozenset[str]] = {
    "runtime": frozenset({
        "runs", "sdk_adapter", "agent_core", "agents", "context_protocol", "run_interjections",
        "run_accounting", "run_resources", "sdk_resources", "worker_envelope", "worker_launch",
        "native_hooks", "native_hook_client", "permissions", "durable_permissions",
        "task_overview", "session_search", "greenpath", "telemetry", "project_metrics",
    }),
    "ledger": frozenset({"cashflow", "compliance_tools", "source_receipts", "presentation_tools"}),
    "project_events": frozenset({
        "project_events", "scoped_events", "scoped_event_api", "preflight_events",
    }),
    "skills": frozenset({
        "skills", "skill_proposals", "skill_proposal_routes", "skill_repository",
        "skill_repository_backup", "skill_repository_routes", "plugin_archives",
        "plugin_imports", "plugin_import_backup",
    }),
    "memory": frozenset({"memory", "distill", "task_experience", "task_experience_routes"}),
    "automation": frozenset({
        "automation", "automation_tools", "scheduler", "intent_watcher", "event_triggers",
    }),
    "feedback": frozenset({"feedback", "feedback_routes"}),
    "channels": frozenset({
        "channels", "channel_delivery", "channel_identity", "dingtalk_channel",
        "dingtalk_delivery", "wecom_channel", "wecom_delivery", "weixin_accounts", "weixin_api",
        "weixin_channel", "weixin_delivery", "weixin_transport",
    }),
    "connectors": frozenset({
        "connectors", "connector_run_access", "marketplaces", "marketplace_access",
        "marketplace_api", "marketplace_tools", "shopify", "gmail", "gmail_access", "gmail_api",
        "gmail_tools", "outlook", "outlook_access", "outlook_api", "outlook_subscription_api",
        "outlook_subscriptions", "outlook_tools", "user_mcp",
    }),
    "commerce": frozenset({"commercial_actions", "commercial_action_tools", "commercial_adapters"}),
    "browser": frozenset({
        "browser_tools", "shop_browser", "shop_browser_api", "shop_browser_host",
        "shop_browser_login_archive", "shop_browser_store",
    }),
    "desktop": frozenset({"desktop_bridge", "desktop_control", "desktop_host", "desktop_tools"}),
    "companion": frozenset({
        "companion", "companion_authorization", "companion_routes", "push_service",
        "push_store", "push_transport",
    }),
    "knowledge": frozenset({
        "knowledge", "office_reference", "ingest", "ingest_worker", "office_export",
    }),
    "artifacts": frozenset({
        "artifact_edits", "media_tools", "runway_video", "site_bundle", "site_publications",
        "site_publication_routes",
    }),
    "lifecycle": frozenset({
        "project_deletion", "project_deletion_api", "project_deletion_capture",
        "project_deletion_proofs", "project_deletion_purge", "project_deletion_shared",
        "project_deletion_sockets", "conversation_deletion", "conversation_deletion_api",
        "conversation_deletion_files", "conversation_deletion_state", "conversations",
        "project_session_cleanup",
    }),
    "exports": frozenset({"project_exports", "project_export_api", "project_export_capture"}),
    "backup": frozenset({"maintenance", "workspace_checkpoints", "workspace_recovery_api"}),
    "templates": frozenset({
        "project_templates", "project_template_api", "project_template_apply",
        "project_template_backup",
    }),
    "collaboration": frozenset({"collaboration_api", "project_comments"}),
    "data_sources": frozenset({"market_data", "trade_data", "fx_rates"}),
}

# 2026-10-09 冻结的债务清单：(importer, imported)。只能删，不能加。
KNOWN_CROSS_IMPORTS: frozenset[tuple[str, str]] = frozenset({
    ("artifact_edits", "knowledge"),
    ("automation_tools", "task_experience"),
    ("browser_tools", "connectors"),
    ("browser_tools", "project_events"),
    ("commercial_actions", "source_receipts"),
    ("commercial_adapters", "connectors"),
    ("companion_routes", "automation"),
    ("companion_routes", "durable_permissions"),
    ("companion_routes", "permissions"),
    ("companion_routes", "run_interjections"),
    ("companion_routes", "runs"),
    ("context_protocol", "memory"),
    ("desktop_control", "project_events"),
    ("dingtalk_channel", "runs"),
    ("events", "run_interjections"),
    ("feedback_routes", "skills"),
    ("gmail", "event_triggers"),
    ("gmail_api", "event_triggers"),
    ("intent_watcher", "runs"),
    ("outlook_subscription_api", "event_triggers"),
    ("outlook_subscriptions", "event_triggers"),
    ("permissions", "event_triggers"),
    ("plugin_archives", "connectors"),
    ("presentation_tools", "commercial_actions"),
    ("project_deletion", "event_triggers"),
    ("project_deletion", "gmail"),
    ("project_deletion", "outlook_subscriptions"),
    ("project_deletion_capture", "companion"),
    ("project_deletion_capture", "project_template_apply"),
    ("project_deletion_capture", "push_store"),
    ("project_deletion_capture", "skill_proposals"),
    ("project_deletion_purge", "companion"),
    ("project_deletion_purge", "push_store"),
    ("project_deletion_purge", "weixin_accounts"),
    ("project_events", "memory"),
    ("project_export_api", "workspace_checkpoints"),
    ("project_exports", "artifact_edits"),
    ("project_exports", "channel_delivery"),
    ("project_exports", "conversation_deletion_state"),
    ("project_exports", "gmail_access"),
    ("project_exports", "marketplace_access"),
    ("project_exports", "outlook_access"),
    ("project_exports", "project_comments"),
    ("project_exports", "project_deletion_proofs"),
    ("project_exports", "shop_browser_store"),
    ("project_exports", "skill_proposals"),
    ("project_exports", "task_experience"),
    ("project_exports", "workspace_checkpoints"),
    ("project_lifecycle", "conversation_deletion_state"),
    ("project_template_apply", "project_deletion_proofs"),
    ("project_template_backup", "project_deletion_proofs"),
    ("providers", "sdk_resources"),
    ("push_service", "automation"),
    ("push_service", "permissions"),
    ("runs", "channel_delivery"),
    ("runs", "channels"),
    ("runs", "companion"),
    ("runs", "companion_authorization"),
    ("runs", "distill"),
    ("runs", "feedback"),
    ("runs", "memory"),
    ("runs", "project_deletion_shared"),
    ("runs", "project_events"),
    ("runs", "skills"),
    ("runs", "user_mcp"),
    ("runs", "workspace_checkpoints"),
    ("scheduler", "project_events"),
    ("scheduler", "runs"),
    ("scheduler", "scoped_events"),
    ("scheduler", "task_experience"),
    ("sdk_adapter", "ingest"),
    ("sdk_adapter", "memory"),
    ("sdk_adapter", "presentation_tools"),
    ("sdk_adapter", "skills"),
    ("sdk_adapter", "source_receipts"),
    ("shop_browser", "marketplace_access"),
    ("shop_browser_api", "connectors"),
    ("shop_browser_host", "connector_run_access"),
    ("shop_browser_host", "maintenance"),
    ("skill_proposal_files", "skills"),
    ("source_receipts", "commercial_adapters"),
    ("storage", "artifact_edits"),
    ("storage", "ingest"),
    ("storage", "workspace_checkpoints"),
    ("telemetry", "presentation_tools"),
    ("web_tools", "market_data"),
    ("web_tools", "trade_data"),
    ("wecom_channel", "runs"),
    ("weixin_channel", "runs"),
    ("workspace_checkpoints", "artifact_edits"),
    ("workspace_checkpoints", "conversation_deletion_state"),
})


def _modules() -> set[str]:
    return {path.stem for path in BACKEND_SRC.glob("*.py")}


def _owners() -> dict[str, str]:
    owners: dict[str, str] = {}
    groups = {"kernel": KERNEL, "root": COMPOSITION_ROOTS, **SUBSYSTEMS}
    for group, members in groups.items():
        for module in members:
            assert module not in owners, f"{module} 登记了两次：{owners[module]} 和 {group}"
            owners[module] = group
    return owners


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                found.add(node.module.split(".")[0])
            else:
                found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.startswith("ophira_backend."):
                found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            found.update(
                alias.name.split(".")[1]
                for alias in node.names
                if alias.name.startswith("ophira_backend.")
            )
    return found


def _cross_imports() -> set[tuple[str, str]]:
    modules, owners = _modules(), _owners()
    found: set[tuple[str, str]] = set()
    for module in sorted(modules):
        group = owners.get(module)
        if group == "root":
            continue
        for target in _imports(BACKEND_SRC / f"{module}.py") & modules:
            if target == module or owners.get(target) in ("kernel", group):
                continue
            found.add((module, target))
    return found


def _describe(owners: dict[str, str], edge: tuple[str, str]) -> str:
    importer, imported = edge
    return f"{importer} -> {imported}  [{owners.get(importer)} -> {owners.get(imported)}]"


def test_every_backend_module_has_exactly_one_owner() -> None:
    modules, owners = _modules(), _owners()
    unregistered = sorted(modules - owners.keys())
    stale = sorted(owners.keys() - modules)
    assert not unregistered, f"新模块必须在归属表登记唯一主人：{unregistered}"
    assert not stale, f"归属表里有已不存在的模块，请删除：{stale}"


def test_cross_subsystem_imports_only_shrink() -> None:
    owners = _owners()
    actual = _cross_imports()
    added = sorted(actual - KNOWN_CROSS_IMPORTS)
    fixed = sorted(KNOWN_CROSS_IMPORTS - actual)
    assert not added, (
        "新增了跨子系统依赖。请经 KERNEL 或目标子系统的公开接口暴露能力，或把模块挪进正确的子系统；"
        "不要往 KNOWN_CROSS_IMPORTS 加行：\n" + "\n".join(_describe(owners, edge) for edge in added)
    )
    assert not fixed, (
        "这些跨子系统依赖已经不存在了，请从 KNOWN_CROSS_IMPORTS 删除：\n"
        + "\n".join(_describe(owners, edge) for edge in fixed)
    )

import asyncio
import dataclasses
from pathlib import Path

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from native_permission_fixture import approve_native_fixture

from ophira_backend.config import Settings
from ophira_backend.constants import TOOL_SNAPSHOT_PAGE, AgentTemplate, PermissionDecision
from ophira_backend.events import EventPublisher
from ophira_backend.memory import FileMemoryProvider
from ophira_backend.models import AgentCreate, PermissionResolution, ProjectCreate, Schedule
from ophira_backend.permissions import PermissionBroker
from ophira_backend.sdk_adapter import ClaudeAgentRuntime, _path_within
from ophira_backend.storage import FileLedger


@dataclasses.dataclass
class FakeToolContext:
    agent_id: str | None = None


def build_runtime(tmp_path: Path):
    ledger = FileLedger(tmp_path / "data")
    memory = FileMemoryProvider(tmp_path / "memory")
    events = EventPublisher(ledger)
    permissions = PermissionBroker(ledger, events, timeout_seconds=30)
    runtime = ClaudeAgentRuntime(
        Settings(data_dir=tmp_path / "data"),
        ledger,
        events,
        permissions,
        memory,
    )
    return ledger, permissions, runtime


def test_path_within_workspace(tmp_path: Path) -> None:
    root = (tmp_path / "workspace").resolve()
    root.mkdir(parents=True)
    (root / "inside.md").touch()
    outside = tmp_path / "outside.md"
    outside.touch()
    link = root / "escape.md"
    link.symlink_to(outside)

    assert _path_within(root, "inside.md") is True
    assert _path_within(root, str(root / "inside.md")) is True
    assert _path_within(root, "../outside.md") is False
    assert _path_within(root, str(outside)) is False
    assert _path_within(root, "escape.md") is False  # symlink resolves outside
    assert _path_within(root, None) is False


@pytest.mark.asyncio
async def test_write_outside_workspace_denied_without_asking(tmp_path: Path) -> None:
    ledger, permissions, runtime = build_runtime(tmp_path)
    agent = ledger.create_agent(AgentCreate(name="通用", template=AgentTemplate.GENERAL))
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start", agent_id=agent.id)
    options = runtime._build_options(run)

    result = await options.can_use_tool(
        "Write", {"file_path": "../outside.md"}, FakeToolContext()
    )

    assert isinstance(result, PermissionResultDeny)
    assert permissions._pending == {}


@pytest.mark.asyncio
async def test_write_inside_workspace_requires_and_honors_user_permission(
    tmp_path: Path,
) -> None:
    ledger, permissions, runtime = build_runtime(tmp_path)
    agent = ledger.create_agent(AgentCreate(name="通用", template=AgentTemplate.GENERAL))
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start", agent_id=agent.id)
    result = await approve_native_fixture(
        runtime, run, "Write", {"file_path": "report.md"},
        PermissionResolution(decision=PermissionDecision.ALLOW_ONCE),
    )

    assert isinstance(result, PermissionResultAllow)
    assert permissions._pending == {}


@pytest.mark.asyncio
async def test_bash_is_never_reused_for_run_scope(tmp_path: Path) -> None:
    ledger, permissions, _runtime = build_runtime(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")

    async def request_and_resolve(tool_name: str, tool_input: dict) -> None:
        task = asyncio.create_task(
            permissions.request(run.id, tool_name, tool_input, agent_id=None, context={})
        )
        for _ in range(200):
            if permissions._pending:
                break
            await asyncio.sleep(0.01)
        request_id = next(iter(permissions._pending))
        await permissions.resolve(
            run.id,
            request_id,
            PermissionResolution(decision=PermissionDecision.ALLOW_RUN),
        )
        await task

    await request_and_resolve("Write", {"file_path": "report.md"})
    assert ("Write", "path:report.md") in permissions._run_allowed_scopes[run.id]

    await request_and_resolve("Bash", {"command": "printf safe"})
    assert all(tool_name != "Bash" for tool_name, _scope in permissions._run_allowed_scopes[run.id])


@pytest.mark.asyncio
async def test_pre_tool_hook_forces_bash_to_ask_instead_of_sandbox_auto_allow(
    tmp_path: Path,
) -> None:
    """PRD §4.10 v2.2 (2026-08-24): sandboxed Bash rides the SDK-native
    autoAllowBashIfSandboxed auto-approval — the hook must NOT force an ask;
    it only denies sandbox escape (dangerouslyDisableSandbox)."""
    ledger, _permissions, runtime = build_runtime(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")
    workspace = ledger.workspace_for_project(project.id)
    hook = runtime._hook(run.id, workspace)

    sandboxed = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "python scripts/transform.py"},
        },
        None,
        None,
    )
    assert sandboxed == {}

    unsandboxed = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /", "dangerouslyDisableSandbox": True},
        },
        None,
        None,
    )
    assert unsandboxed["hookSpecificOutput"]["permissionDecision"] == "deny"

    read = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "uploads/quotes.csv"},
        },
        None,
        None,
    )
    assert read == {}

    # 读围栏=项目目录：账本原始数据（workspace 上一级的 project.json）允许直读，
    # 兜住 MCP 查询接口覆盖不了的意料之外分析（PRD §4.10 "Read/Glob/Grep 项目内自动允许"）。
    read_ledger = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "../project.json"},
        },
        None,
        None,
    )
    assert read_ledger == {}

    # 项目目录之外仍拒绝。
    read_outside = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "../../other-project/project.json"},
        },
        None,
        None,
    )
    assert read_outside["hookSpecificOutput"]["permissionDecision"] == "deny"

    # 写围栏仍=工作区：账本文件不可经 Write 直改（写入只走 MCP 的审计化通道）。
    write_ledger = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "../project.json", "content": "x"},
        },
        None,
        None,
    )
    assert write_ledger["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.asyncio
async def test_scheduled_run_hook_denies_all_bash_while_sandbox_default_stays_enabled(
    tmp_path: Path,
) -> None:
    ledger, _permissions, runtime = build_runtime(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    schedule = ledger.create_schedule(
        Schedule(project_id=project.id, prompt="Export report", cron="0 8 * * *")
    )
    run = ledger.create_run(project.id, "Start", schedule_id=schedule.id)

    options = runtime._build_options(run)
    assert options.sandbox is not None
    assert options.sandbox["autoAllowBashIfSandboxed"] is True

    hook = runtime._hook(run.id, ledger.workspace_for_project(project.id))
    for tool_input in (
        {"command": "python scripts/transform.py"},
        {"command": "rm -rf reports"},
        {"command": "printf unsafe", "dangerouslyDisableSandbox": True},
    ):
        result = await hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": tool_input,
            },
            None,
            None,
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "Scheduled runs cannot use Bash" in result["hookSpecificOutput"][
            "permissionDecisionReason"
        ]


@pytest.mark.asyncio
async def test_subagent_ledger_writes_are_denied_code_level(tmp_path: Path) -> None:
    """§6.1 红线：账本写入只属主 Agent——子代理调用不进 broker、直接代码级拒绝。"""
    ledger, permissions, runtime = build_runtime(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")
    options = runtime._build_options(run)

    deny = await options.can_use_tool(
        "mcp__ophira_ledger__record_evidence", {}, FakeToolContext(agent_id="sub_researcher")
    )
    assert isinstance(deny, PermissionResultDeny)
    assert "主 Agent" in deny.message
    assert permissions._pending == {}

    for tool_name, tool_input in (
        ("Write", {"file_path": "report.md", "content": "x"}),
        ("Edit", {"file_path": "report.md", "old_string": "x", "new_string": "y"}),
        ("Delete", {"path": "report.md"}),
        ("Bash", {"command": "printf safe"}),
        (
            "mcp__ophira_presentation__present_evidence_comparison",
            {"title": "Signals", "evidence_ids": ["ev_1"]},
        ),
        (
            "mcp__ophira_commercial_actions__stage_commercial_action",
            {"operation": "change_price"},
        ),
        ("mcp__third_party__create_issue", {"title": "write"}),
    ):
        result = await options.can_use_tool(
            tool_name,
            tool_input,
            FakeToolContext(agent_id="sub_researcher"),
        )
        assert isinstance(result, PermissionResultDeny)
    assert permissions._pending == {}

    # Hook enforcement is the non-bypassable backstop when SDK permission modes or
    # allow rules resolve a call before can_use_tool.
    hook_result = await runtime._hook(
        run.id, ledger.workspace_for_project(project.id)
    )(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "printf safe"},
            "agent_id": "sub_researcher",
        },
        None,
        None,
    )
    assert hook_result["hookSpecificOutput"]["permissionDecision"] == "deny"

    # A main-Agent invocation is deferred for review; a subagent is rejected above.
    await approve_native_fixture(
        runtime, run, "mcp__ophira_ledger__record_evidence", {},
        PermissionResolution(decision=PermissionDecision.DENY),
    )


@pytest.mark.asyncio
async def test_connected_browser_snapshot_hook_forces_one_time_approval(tmp_path: Path) -> None:
    ledger, _permissions, runtime = build_runtime(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")
    hook = runtime._hook(run.id, ledger.workspace_for_project(project.id))

    connected = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": TOOL_SNAPSHOT_PAGE,
            "tool_input": {
                "url": "https://example.com",
                "use_connected_browser": True,
            },
        },
        None,
        None,
    )
    isolated = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": TOOL_SNAPSHOT_PAGE,
            "tool_input": {"url": "https://example.com"},
        },
        None,
        None,
    )

    assert connected["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert isolated == {}

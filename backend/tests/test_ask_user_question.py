"""AskUserQuestion 结构化追问：通道、白名单排除、读围栏白名单、prompt 段序。

教材口径（2026-08-24 拍板 Q1=B / Q2=A）：
- 追问经 can_use_tool 到达，答案经 updated_input 回传（docs/claude-agent-sdk/user-input.md）。
- 每组问题都不同：ALLOW_RUN 不得把 AskUserQuestion 加入整-run 白名单。
- 读围栏 = 白名单（自己项目目录 + backend/src），跨项目账本隔离。
- 段序即缓存策略：固定前缀稳定，变量段收尾。
"""

import asyncio
import dataclasses
from pathlib import Path

import pytest
from claude_agent_sdk import PermissionResultAllow
from native_permission_fixture import approve_native_fixture

from ophira_backend.agent_core import assemble_default_prompt, assemble_system_prompt
from ophira_backend.config import BACKEND_DIR, Settings
from ophira_backend.constants import AgentTemplate, PermissionDecision
from ophira_backend.events import EventPublisher
from ophira_backend.memory import FileMemoryProvider
from ophira_backend.models import PermissionResolution, ProjectCreate
from ophira_backend.permissions import PermissionBroker
from ophira_backend.sdk_adapter import ClaudeAgentRuntime
from ophira_backend.storage import FileLedger
from ophira_backend.templates import TEMPLATE_SPECS


@dataclasses.dataclass
class FakeToolContext:
    agent_id: str | None = None


QUESTIONS = [
    {
        "question": "重构的主要目的是什么？",
        "header": "目的",
        "options": [
            {"label": "提升性能", "description": ""},
            {"label": "改善维护性", "description": ""},
        ],
        "multiSelect": False,
    }
]


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


async def _resolve_first_pending(permissions: PermissionBroker, run_id: str, resolution):
    for _ in range(200):
        if permissions._pending:
            break
        await asyncio.sleep(0.01)
    request_id = next(iter(permissions._pending))
    await permissions.resolve(run_id, request_id, resolution)


@pytest.mark.asyncio
async def test_ask_user_question_answers_return_via_updated_input(tmp_path: Path) -> None:
    ledger, permissions, runtime = build_runtime(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")
    result = await approve_native_fixture(
        runtime, run, "AskUserQuestion", {"questions": QUESTIONS},
        PermissionResolution(
            decision=PermissionDecision.ALLOW_ONCE,
            answers={"重构的主要目的是什么？": "改善维护性"},
        ),
    )

    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == {
        "questions": QUESTIONS,
        "answers": {"重构的主要目的是什么？": "改善维护性"},
    }


@pytest.mark.asyncio
async def test_ask_user_question_deny_tells_agent_to_proceed_with_assumptions(
    tmp_path: Path,
) -> None:
    ledger, permissions, runtime = build_runtime(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")
    result = await approve_native_fixture(
        runtime, run, "AskUserQuestion", {"questions": QUESTIONS},
        PermissionResolution(decision=PermissionDecision.DENY)
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "假设" in result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_allow_run_never_whitelists_ask_user_question(tmp_path: Path) -> None:
    ledger, permissions, _runtime = build_runtime(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")

    task = asyncio.create_task(
        permissions.request(
            run.id, "AskUserQuestion", {"questions": QUESTIONS}, agent_id=None, context={}
        )
    )
    await _resolve_first_pending(
        permissions, run.id, PermissionResolution(decision=PermissionDecision.ALLOW_RUN)
    )
    await task

    # 下一组问题必须重新到人，不得静默放行。
    assert all(
        tool_name != "AskUserQuestion"
        for tool_name, _scope in permissions._run_allowed_scopes.get(run.id, set())
    )


@pytest.mark.asyncio
async def test_read_allowlist_covers_backend_src_but_not_other_projects(
    tmp_path: Path,
) -> None:
    ledger, _permissions, runtime = build_runtime(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    other = ledger.create_project(ProjectCreate(name="Rival", objective="Other"))
    run = ledger.create_run(project.id, "Start")
    workspace = ledger.workspace_for_project(project.id).resolve()
    hook = runtime._hook(run.id, workspace)

    async def read(path: str):
        return await hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": path},
            },
            None,
            None,
        )

    # 自己项目目录（含账本）：放行（hook 不做 deny 决定）。
    own_ledger = await read(str(workspace.parent / "project.json"))
    assert (own_ledger or {}).get("hookSpecificOutput", {}).get("permissionDecision") != "deny"

    # 系统源码：放行（在线 Codex Enrichment——读代码理解业务逻辑）。
    src_file = await read(str((BACKEND_DIR / "src" / "ophira_backend" / "storage.py").resolve()))
    assert (src_file or {}).get("hookSpecificOutput", {}).get("permissionDecision") != "deny"

    # 其他项目的账本：拒绝（跨项目隔离）。
    other_ledger = ledger.workspace_for_project(other.id).resolve().parent / "project.json"
    denied = await read(str(other_ledger))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_prompt_section_order_is_stable_prefix_then_volatile_tail(tmp_path: Path) -> None:
    # default 版：运行模式（固定）在前，运行标识（每 run 变）收尾。
    prompt = assemble_default_prompt("proj_1", "trace_1", [], mode_rules="常规单 Agent 模式")
    assert prompt.index("# 运行模式") < prompt.index("# 运行标识")
    assert prompt.rstrip().endswith("trace_id: trace_1")

    # 模板版：SOUL.md（固定）在记忆 digest（易变）之前。
    from ophira_backend.models import AgentProfile

    core_dir = tmp_path / "core"
    core_dir.mkdir()
    (core_dir / "SOUL.md").write_text("# 价值观\n诚实优先。", encoding="utf-8")
    (core_dir / "MEMORY.md").write_text("# 长期记忆\n- 条目", encoding="utf-8")
    spec = TEMPLATE_SPECS[AgentTemplate.GENERAL]
    agent = AgentProfile(name="通用", template=AgentTemplate.GENERAL)
    prompt = assemble_system_prompt(agent, core_dir, spec, [], mode_rules="常规单 Agent 模式")
    assert prompt.index("# 运行模式") < prompt.index("# 价值观与行为边界（SOUL.md）")
    assert prompt.index("# 价值观与行为边界（SOUL.md）") < prompt.index("# 长期记忆（MEMORY.md）")

from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, StreamEvent, TextBlock, ThinkingBlock, ToolUseBlock

from ophira_backend.config import Settings
from ophira_backend.constants import AGENT_NAMES, Dimension, EventType
from ophira_backend.events import EventPublisher
from ophira_backend.models import ProjectCreate
from ophira_backend.permissions import PermissionBroker
from ophira_backend.sdk_adapter import ClaudeAgentRuntime
from ophira_backend.storage import FileLedger


def build_runtime(tmp_path: Path):
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")
    events = EventPublisher(ledger)
    permissions = PermissionBroker(ledger, events, timeout_seconds=30)
    runtime = ClaudeAgentRuntime(Settings(data_dir=tmp_path), ledger, events, permissions)
    return ledger, run, runtime


def test_sdk_options_use_programmatic_team_and_observable_stream(tmp_path: Path) -> None:
    _ledger, run, runtime = build_runtime(tmp_path)
    options = runtime._build_options(run)

    assert set(options.agents or {}) == {AGENT_NAMES[item] for item in Dimension}
    assert options.include_partial_messages is True
    assert options.include_hook_events is True
    assert options.thinking == {"type": "adaptive", "display": "summarized"}
    assert "Agent" in options.allowed_tools
    assert options.strict_mcp_config is True
    assert set(options.hooks or {}) >= {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "SubagentStart",
        "SubagentStop",
    }


@pytest.mark.asyncio
async def test_sdk_messages_map_to_frontend_events(tmp_path: Path) -> None:
    ledger, run, runtime = build_runtime(tmp_path)
    await runtime._map_message(
        run.id,
        StreamEvent(
            uuid="u1",
            session_id="s1",
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "正在验证需求"},
            },
        ),
    )
    await runtime._map_message(
        run.id,
        AssistantMessage(
            model="test-model",
            content=[
                TextBlock("已完成"),
                ThinkingBlock("用户可见的推理摘要", "signature"),
                ToolUseBlock("tool-1", "WebSearch", {"query": "market"}),
            ],
        ),
    )

    event_types = [event.type for event in ledger.list_events(run.id)]
    assert event_types == [
        EventType.ASSISTANT_TEXT_DELTA.value,
        EventType.ASSISTANT_MESSAGE_COMPLETED.value,
        EventType.ASSISTANT_REASONING_SUMMARY_COMPLETED.value,
        EventType.TOOL_PLANNED.value,
    ]
    reasoning = ledger.list_events(run.id)[2]
    assert reasoning.data["display"] == "summarized"

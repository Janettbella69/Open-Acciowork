from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from ophira_backend.config import Settings
from ophira_backend.constants import EventType
from ophira_backend.events import EventPublisher
from ophira_backend.models import ProjectCreate, ProviderProfileUpsert
from ophira_backend.permissions import PermissionBroker
from ophira_backend.providers import ProviderRegistry
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


def test_default_sdk_options_keep_the_agent_general_and_observable(tmp_path: Path) -> None:
    _ledger, run, runtime = build_runtime(tmp_path)
    options = runtime._build_options(run)

    assert options.agents is None
    assert options.include_partial_messages is True
    assert options.include_hook_events is True
    assert options.thinking == {"type": "adaptive", "display": "summarized"}
    assert options.tools == {"type": "preset", "preset": "claude_code"}
    assert options.disallowed_tools == ["Agent"]
    assert options.system_prompt["preset"] == "claude_code"
    assert "不要把请求强制归入预设意图" in options.system_prompt["append"]
    assert runtime._query_prompt(run) == "Start"
    assert options.strict_mcp_config is True
    assert options.env["ANTHROPIC_API_KEY"] == ""
    assert options.env["ANTHROPIC_AUTH_TOKEN"] == ""
    assert options.env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert options.setting_sources == []
    assert options.skills == "all"
    assert options.plugins
    assert options.enable_file_checkpointing is True
    assert set(options.mcp_servers or {}) == {
        "ophira_ledger",
        "ophira_market",
        "ophira_supplier",
        "ophira_customer",
        "ophira_commerce_math",
    }
    assert set(options.hooks or {}) >= {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "SubagentStart",
        "SubagentStop",
    }


def test_sdk_key_is_passed_to_subprocess_without_entering_events(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")
    events = EventPublisher(ledger)
    permissions = PermissionBroker(ledger, events, timeout_seconds=30)
    runtime = ClaudeAgentRuntime(
        Settings(data_dir=tmp_path, ANTHROPIC_API_KEY="sk-ant-test-secret"),
        ledger,
        events,
        permissions,
    )

    options = runtime._build_options(run)
    assert options.env["ANTHROPIC_API_KEY"] == "sk-ant-test-secret"
    assert ledger.list_events(run.id) == []


def test_sdk_uses_anthropic_compatible_gateway_configuration(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")
    events = EventPublisher(ledger)
    permissions = PermissionBroker(ledger, events, timeout_seconds=30)
    runtime = ClaudeAgentRuntime(
        Settings(
            data_dir=tmp_path,
            claude_model="glm-5.2[1m]",
            ANTHROPIC_BASE_URL="https://open.bigmodel.cn/api/anthropic",
            ANTHROPIC_AUTH_TOKEN="glm-secret",
        ),
        ledger,
        events,
        permissions,
    )

    options = runtime._build_options(run)

    assert options.model == "glm-5.2[1m]"
    assert options.env == {
        "ANTHROPIC_BASE_URL": "https://open.bigmodel.cn/api/anthropic",
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_AUTH_TOKEN": "glm-secret",
        "CLAUDE_CODE_OAUTH_TOKEN": "",
        "CLAUDE_CONFIG_DIR": str(tmp_path / "runtime-config" / "environment"),
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "ANTHROPIC_MODEL": "glm-5.2[1m]",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-5.2[1m]",
        "ANTHROPIC_DEFAULT_FABLE_MODEL": "glm-5.2[1m]",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.2[1m]",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.2[1m]",
        "CLAUDE_CODE_SUBAGENT_MODEL": "glm-5.2[1m]",
    }
    assert ledger.list_events(run.id) == []


def test_profile_budget_scale_compensates_sdk_list_price_accounting(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    events = EventPublisher(ledger)
    permissions = PermissionBroker(ledger, events, timeout_seconds=30)
    settings = Settings(data_dir=tmp_path)
    providers = ProviderRegistry(settings)
    providers.upsert(
        "deepseek",
        ProviderProfileUpsert(
            display_name="DeepSeek",
            base_url="https://api.deepseek.com/anthropic",
            auth_env_name="ANTHROPIC_AUTH_TOKEN",
            model="deepseek-v4-pro",
            secret="test-credential",
            budget_scale=8.0,
        ),
    )
    runtime = ClaudeAgentRuntime(
        settings, ledger, events, permissions, providers=providers
    )

    scaled = ledger.create_run(project.id, "Start", provider_profile_id="deepseek")
    assert runtime._build_options(scaled).max_budget_usd == pytest.approx(
        settings.max_budget_usd * 8.0
    )

    unscaled = ledger.create_run(project.id, "Start")
    assert runtime._build_options(unscaled).max_budget_usd == pytest.approx(
        settings.max_budget_usd
    )


@pytest.mark.asyncio
async def test_run_result_cost_is_labeled_with_its_pricing_basis(tmp_path: Path) -> None:
    ledger, run, runtime = build_runtime(tmp_path)
    await runtime._map_message(
        run.id,
        ResultMessage(
            subtype="success",
            duration_ms=1_000,
            duration_api_ms=900,
            is_error=False,
            num_turns=3,
            session_id="s1",
            total_cost_usd=0.171,
            usage={"input_tokens": 1_200, "output_tokens": 340},
        ),
    )

    result_event = ledger.list_events(run.id)[-1]
    assert result_event.type == EventType.RUN_RESULT.value
    assert result_event.data["total_cost_usd"] == 0.171
    assert result_event.data["cost_basis"] == "sdk_anthropic_list_price"
    assert result_event.data["usage"]["input_tokens"] == 1_200


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

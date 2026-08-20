"""Unit tests for ClaudeAgentRuntime.preflight() with a scripted fake SDK client.

The preflight contract (PRD §4.9): exercise the REAL Claude Agent SDK path with the
provider worker env, assert Plugin Skill discovery and a real MCP health tool call,
and classify every failure into one of the 8 provider error codes.
"""

from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, ToolUseBlock

from ophira_backend.config import Settings
from ophira_backend.constants import ProviderErrorCode
from ophira_backend.events import EventPublisher
from ophira_backend.models import ProviderProfileUpsert
from ophira_backend.permissions import PermissionBroker
from ophira_backend.providers import ProviderRegistry
from ophira_backend.sdk_adapter import ClaudeAgentRuntime
from ophira_backend.storage import FileLedger

EXPECTED_SKILL = "ophira-core:global-opportunity-execution"
HEALTH_TOOL = "mcp__ophira_preflight__health"


class FakeSDKClient:
    """Scripted stand-in for ClaudeSDKClient: yields a fixed message sequence."""

    captured_options = None
    script: list = []
    enter_error: Exception | None = None

    def __init__(self, options=None) -> None:
        type(self).captured_options = options

    async def __aenter__(self):
        if type(self).enter_error is not None:
            raise type(self).enter_error
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def query(self, _prompt: str) -> None:
        return None

    async def receive_response(self):
        for message in type(self).script:
            yield message


def build_runtime(tmp_path: Path) -> tuple[ClaudeAgentRuntime, ProviderRegistry]:
    settings = Settings(data_dir=tmp_path)
    ledger = FileLedger(tmp_path)
    events = EventPublisher(ledger)
    permissions = PermissionBroker(ledger, events, timeout_seconds=30)
    providers = ProviderRegistry(settings)
    providers.upsert(
        "deepseek",
        ProviderProfileUpsert(
            display_name="DeepSeek",
            base_url="https://api.deepseek.com/anthropic",
            auth_env_name="ANTHROPIC_AUTH_TOKEN",
            model="deepseek-v4-pro",
            secret="test-credential",
        ),
    )
    runtime = ClaudeAgentRuntime(settings, ledger, events, permissions, providers=providers)
    return runtime, providers


def script_success() -> list:
    return [
        SystemMessage(subtype="init", data={"session_id": "sess-1", "skills": [EXPECTED_SKILL]}),
        AssistantMessage(
            content=[ToolUseBlock(id="tool-1", name=HEALTH_TOOL, input={})],
            model="deepseek-v4-pro",
        ),
        ResultMessage(
            subtype="success",
            duration_ms=900,
            duration_api_ms=800,
            is_error=False,
            num_turns=2,
            session_id="sess-1",
        ),
    ]


@pytest.fixture(autouse=True)
def patch_sdk_client(monkeypatch: pytest.MonkeyPatch):
    FakeSDKClient.script = []
    FakeSDKClient.enter_error = None
    FakeSDKClient.captured_options = None
    monkeypatch.setattr("ophira_backend.sdk_adapter.ClaudeSDKClient", FakeSDKClient)


@pytest.mark.asyncio
async def test_preflight_success_requires_skill_discovery_and_mcp_call(tmp_path: Path) -> None:
    runtime, providers = build_runtime(tmp_path)
    FakeSDKClient.script = script_success()

    result = await runtime.preflight("deepseek")

    assert result.ok is True
    assert result.error_code is None
    assert result.mcp_connected is True
    assert EXPECTED_SKILL in result.discovered_skills
    assert result.sdk_session_id == "sess-1"
    assert result.model == "deepseek-v4-pro"
    profile = providers.get("deepseek")
    assert profile.last_verified_at is not None
    assert profile.last_error_code is None


@pytest.mark.asyncio
async def test_preflight_runs_the_locked_down_sdk_configuration(tmp_path: Path) -> None:
    runtime, _providers = build_runtime(tmp_path)
    FakeSDKClient.script = script_success()

    await runtime.preflight("deepseek")

    options = FakeSDKClient.captured_options
    assert options is not None
    assert options.permission_mode == "dontAsk"
    assert set(options.disallowed_tools) == {"Agent", "Write", "Edit", "Bash"}
    assert options.max_turns == 4
    assert options.max_budget_usd <= 0.25
    assert options.tools == {"type": "preset", "preset": "claude_code"}
    assert options.setting_sources == []
    assert options.strict_mcp_config is True
    # Credential isolation: only the selected profile credential is injected.
    assert options.env["ANTHROPIC_AUTH_TOKEN"] == "test-credential"
    assert options.env["ANTHROPIC_API_KEY"] == ""
    assert options.env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert options.env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"


@pytest.mark.asyncio
async def test_preflight_fails_when_plugin_skill_is_not_discovered(tmp_path: Path) -> None:
    runtime, providers = build_runtime(tmp_path)
    script = script_success()
    script[0] = SystemMessage(subtype="init", data={"session_id": "sess-1", "skills": []})
    FakeSDKClient.script = script

    result = await runtime.preflight("deepseek")

    assert result.ok is False
    assert result.error_code == ProviderErrorCode.SKILL_DISCOVERY_FAILED
    assert providers.get("deepseek").last_error_code == ProviderErrorCode.SKILL_DISCOVERY_FAILED
    assert providers.get("deepseek").last_verified_at is None


@pytest.mark.asyncio
async def test_preflight_fails_when_health_tool_is_never_called(tmp_path: Path) -> None:
    runtime, _providers = build_runtime(tmp_path)
    script = script_success()
    script[1] = AssistantMessage(content=[], model="deepseek-v4-pro")
    FakeSDKClient.script = script

    result = await runtime.preflight("deepseek")

    assert result.ok is False
    assert result.error_code == ProviderErrorCode.MCP_INIT_FAILED
    assert result.mcp_connected is False


@pytest.mark.asyncio
async def test_preflight_classifies_result_errors_and_redacts_secrets(tmp_path: Path) -> None:
    runtime, _providers = build_runtime(tmp_path)
    script = script_success()
    script[2] = ResultMessage(
        subtype="error_during_execution",
        duration_ms=400,
        duration_api_ms=300,
        is_error=True,
        num_turns=1,
        session_id="sess-1",
        errors=["401 unauthorized: api_key=sk-live-secret has been revoked"],
    )
    FakeSDKClient.script = script

    result = await runtime.preflight("deepseek")

    assert result.ok is False
    assert result.error_code == ProviderErrorCode.AUTH_REVOKED_OR_INVALID
    assert "sk-live-secret" not in result.message
    assert "[REDACTED]" in result.message


@pytest.mark.asyncio
async def test_preflight_classifies_connection_failures(tmp_path: Path) -> None:
    runtime, providers = build_runtime(tmp_path)
    FakeSDKClient.enter_error = ConnectionError("connection refused by host")

    result = await runtime.preflight("deepseek")

    assert result.ok is False
    assert result.error_code == ProviderErrorCode.BASE_URL_UNREACHABLE
    assert providers.get("deepseek").last_error_code == ProviderErrorCode.BASE_URL_UNREACHABLE


@pytest.mark.asyncio
async def test_preflight_times_out_with_actionable_diagnosis_when_sdk_hangs(
    tmp_path: Path,
) -> None:
    """Observed live (DeepSeek + invalid token): the SDK hangs instead of failing fast.
    FR-06 requires preflight to come back bounded with an actionable message."""
    runtime, providers = build_runtime(tmp_path)
    runtime.settings = runtime.settings.model_copy(update={"preflight_timeout_seconds": 1})

    class HangingClient(FakeSDKClient):
        async def receive_response(self):
            import asyncio

            await asyncio.sleep(3600)
            yield None  # pragma: no cover

    import ophira_backend.sdk_adapter as adapter

    original = adapter.ClaudeSDKClient
    adapter.ClaudeSDKClient = HangingClient
    try:
        result = await runtime.preflight("deepseek")
    finally:
        adapter.ClaudeSDKClient = original

    assert result.ok is False
    assert result.error_code == ProviderErrorCode.SDK_RUNTIME_FAILED
    assert "未完成" in result.message
    assert providers.get("deepseek").last_error_code == ProviderErrorCode.SDK_RUNTIME_FAILED

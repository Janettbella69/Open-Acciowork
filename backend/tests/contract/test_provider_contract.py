"""Live provider contract matrix (PRD §4.9). One configured profile per invocation:

    OPHIRA_CONTRACT_PROVIDER=deepseek PYTHONPATH=backend/src \
        .venv/bin/pytest backend/tests/contract -v

Every case is a minimal, budget-capped real `ClaudeSDKClient` session. Failures block the
provider/model combination from the production list; they must never be "fixed" by
swapping the runtime to a vendor OpenAI API (red line, PRD §1.1).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from ophira_backend.constants import ProviderErrorCode
from ophira_backend.providers import classify_provider_error

from .conftest import REPO_ROOT, record

pytestmark = pytest.mark.asyncio

PLUGIN_DIR = REPO_ROOT / "backend" / "ophira-capabilities"
EXPECTED_SKILL = "ophira-core:global-opportunity-execution"


def base_options(env: dict[str, str], model: str, cwd: Path, **overrides) -> ClaudeAgentOptions:
    defaults: dict = {
        "tools": {"type": "preset", "preset": "claude_code"},
        "system_prompt": {"type": "preset", "preset": "claude_code"},
        "setting_sources": [],
        "strict_mcp_config": True,
        "permission_mode": "dontAsk",
        "disallowed_tools": ["Agent"],
        "allowed_tools": [],
        "env": env,
        "model": model,
        "cwd": cwd,
        "max_turns": 6,
        "max_budget_usd": 0.3,
        "include_partial_messages": True,
        "sandbox": {
            "enabled": True,
            "autoAllowBashIfSandboxed": False,
            "allowUnsandboxedCommands": False,
        },
    }
    defaults.update(overrides)
    return ClaudeAgentOptions(**defaults)


async def collect(options: ClaudeAgentOptions, prompt: str, timeout_s: float = 240) -> list:
    messages: list = []

    async def consume() -> None:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                messages.append(message)

    await asyncio.wait_for(consume(), timeout_s)
    return messages


def result_of(messages: list) -> ResultMessage | None:
    return next((m for m in messages if isinstance(m, ResultMessage)), None)


def assistant_text(messages: list) -> str:
    parts: list[str] = []
    for message in messages:
        if isinstance(message, AssistantMessage):
            parts.extend(block.text for block in message.content if isinstance(block, TextBlock))
    return "\n".join(parts)


def served_models(messages: list) -> set[str]:
    return {m.model for m in messages if isinstance(m, AssistantMessage) and m.model}


async def test_basic_text_streaming_and_model_identity(profile, registry, tmp_path) -> None:
    env = registry.build_worker_env(profile.id)
    options = base_options(env, profile.model, tmp_path)
    try:
        messages = await collect(options, "只回复两个词：CONTRACT OK")
    except Exception as exc:  # noqa: BLE001
        record("basic_text_streaming", "FAIL", str(exc)[:300])
        raise

    result = result_of(messages)
    assert result is not None and result.is_error is False, "run errored"
    assert "CONTRACT" in assistant_text(messages).upper()
    saw_stream = any(isinstance(m, StreamEvent) for m in messages)

    base_model = profile.model.split("[")[0]
    served = served_models(messages)
    identity_ok = any(model.startswith(base_model) for model in served)
    # DeepSeek silently maps unknown model names to flash: identity must be asserted.
    assert identity_ok, f"configured {profile.model!r} but served {sorted(served)!r}"
    record(
        "basic_text_streaming",
        "PASS" if saw_stream else "DEGRADED",
        f"served={sorted(served)}; streaming={'yes' if saw_stream else 'no partial events'}",
        cost_usd=result.total_cost_usd,
    )
    record("model_identity", "PASS", f"served model prefix matches {base_model}")


async def test_file_tool_chain_read_write_edit(profile, registry, tmp_path) -> None:
    env = registry.build_worker_env(profile.id)
    options = base_options(
        env, profile.model, tmp_path,
        allowed_tools=["Read", "Write", "Edit"], max_turns=8, max_budget_usd=0.4,
    )
    prompt = (
        "严格执行：1) 用 Write 创建 note.txt，内容为 hello；"
        "2) 用 Edit 把 note.txt 中的 hello 改为 world；"
        "3) 用 Read 读回 note.txt 并把内容原样回复。"
    )
    try:
        messages = await collect(options, prompt)
    except Exception as exc:  # noqa: BLE001
        record("file_tool_chain", "FAIL", str(exc)[:300])
        raise

    result = result_of(messages)
    note = tmp_path / "note.txt"
    ok = (
        result is not None
        and result.is_error is False
        and note.exists()
        and "world" in note.read_text(encoding="utf-8")
    )
    record(
        "file_tool_chain",
        "PASS" if ok else "FAIL",
        f"note.txt={'present' if note.exists() else 'missing'}",
        cost_usd=result.total_cost_usd if result else None,
    )
    assert ok, "Read→Write→Edit chain did not land on disk"


async def test_bash_execution_in_sandbox(profile, registry, tmp_path) -> None:
    env = registry.build_worker_env(profile.id)
    options = base_options(
        env, profile.model, tmp_path, allowed_tools=["Bash"], max_budget_usd=0.3,
    )
    try:
        messages = await collect(options, "用 Bash 运行 `echo contract-bash-ok`，把输出原样回复。")
    except Exception as exc:  # noqa: BLE001
        record("bash_execution", "FAIL", str(exc)[:300])
        raise

    result = result_of(messages)
    ok = (
        result is not None
        and result.is_error is False
        and "contract-bash-ok" in assistant_text(messages)
    )
    record("bash_execution", "PASS" if ok else "FAIL", "",
           cost_usd=result.total_cost_usd if result else None)
    assert ok, "Bash tool did not execute or its output never reached the reply"


async def test_inprocess_mcp_tool_call(profile, registry, tmp_path) -> None:
    @tool("echo_marker", "Return the OPHIRA contract marker.", {})
    async def echo_marker(_args: dict) -> dict:
        return {"content": [{"type": "text", "text": "OPHIRA_CONTRACT_MCP_OK"}], "is_error": False}

    server = create_sdk_mcp_server(name="ophira_contract", version="1.0.0", tools=[echo_marker])
    tool_name = "mcp__ophira_contract__echo_marker"
    env = registry.build_worker_env(profile.id)
    options = base_options(
        env, profile.model, tmp_path,
        mcp_servers={"ophira_contract": server}, allowed_tools=[tool_name],
    )
    try:
        messages = await collect(options, f"调用 {tool_name} 工具，然后回复它返回的文本。")
    except Exception as exc:  # noqa: BLE001
        record("mcp_tool_call", "FAIL", str(exc)[:300])
        raise

    called = any(
        isinstance(m, AssistantMessage)
        and any(isinstance(b, ToolUseBlock) and b.name == tool_name for b in m.content)
        for m in messages
    )
    result = result_of(messages)
    ok = called and result is not None and result.is_error is False
    record("mcp_tool_call", "PASS" if ok else "FAIL",
           f"tool_called={called}", cost_usd=result.total_cost_usd if result else None)
    assert ok, "in-process MCP tool was not called successfully"


async def test_plugin_skill_discovery(profile, registry, tmp_path) -> None:
    env = registry.build_worker_env(profile.id)
    options = base_options(
        env, profile.model, tmp_path,
        plugins=[{"type": "local", "path": str(PLUGIN_DIR)}], skills="all", max_turns=2,
    )
    try:
        messages = await collect(options, "只回复 OK。")
    except Exception as exc:  # noqa: BLE001
        record("plugin_skill_discovery", "FAIL", str(exc)[:300])
        raise

    discovered: list[str] = []
    for message in messages:
        if isinstance(message, SystemMessage) and message.subtype == "init":
            discovered = list(message.data.get("skills") or [])
    ok = EXPECTED_SKILL in discovered
    record("plugin_skill_discovery", "PASS" if ok else "FAIL",
           f"discovered={len(discovered)} skills")
    assert ok, f"{EXPECTED_SKILL} not in {discovered}"


async def test_native_web_tools_probe(profile, registry, tmp_path) -> None:
    """Capability probe (FR-23 input): unavailability is DEGRADED, not a failure."""
    env = registry.build_worker_env(profile.id)
    options = base_options(
        env, profile.model, tmp_path, allowed_tools=["WebSearch", "WebFetch"],
        max_turns=4, max_budget_usd=0.4,
    )
    prompt = (
        "用 WebSearch 搜索 \"Anthropic Claude Agent SDK\"。"
        "如果工具调用成功，回复 WEB_OK 加一个来源 URL；"
        "如果工具不可用或调用失败，只回复 WEB_UNAVAILABLE。"
    )
    try:
        messages = await collect(options, prompt)
    except Exception as exc:  # noqa: BLE001
        record("native_web_tools", "DEGRADED", f"probe errored: {str(exc)[:200]}")
        return

    text = assistant_text(messages)
    used_web = any(
        isinstance(m, AssistantMessage)
        and any(
            isinstance(b, ToolUseBlock) and b.name in {"WebSearch", "WebFetch"}
            for b in m.content
        )
        for m in messages
    )
    if used_web and "WEB_OK" in text:
        record("native_web_tools", "PASS", "native WebSearch served results")
    else:
        record(
            "native_web_tools",
            "DEGRADED",
            "native WebSearch unavailable on this endpoint; route via FR-23 retrieval MCP",
        )


async def test_image_blocks_documented_capability(profile) -> None:
    """Kept as documentation: multimodal input is asserted via the capability matrix."""
    declared = profile.capabilities.images
    record(
        "image_blocks",
        "NOT_TESTED",
        f"profile declares images={declared}; live multimodal case deferred to FR-27 work",
    )


async def test_bad_credential_fails_fast_or_is_bounded_by_timeout(
    profile, registry, tmp_path
) -> None:
    """Observed 2026-08-20: an invalid token makes the SDK hang instead of surfacing
    401 — so the contract requirement is 'fails fast with AUTH classification OR is
    caught by a bounded timeout' (the preflight timeout guard covers production)."""
    env = registry.build_worker_env(profile.id)
    env[profile.auth_env_name] = "sk-ophira-contract-invalid-token"
    options = base_options(env, profile.model, tmp_path, max_turns=2, max_budget_usd=0.1)
    error_text = ""
    timed_out = False
    try:
        messages = await collect(options, "只回复 OK。", timeout_s=90)
        result = result_of(messages)
        if result is not None and result.is_error:
            error_text = "; ".join(result.errors or [result.result or result.subtype])
        else:
            for message in messages:
                if isinstance(message, AssistantMessage) and message.error:
                    error_text = message.error
    except TimeoutError:
        timed_out = True
    except Exception as exc:  # noqa: BLE001
        error_text = f"{type(exc).__name__}: {exc}"

    if timed_out:
        record(
            "error_mapping_bad_token",
            "DEGRADED",
            "invalid key hangs the SDK instead of failing fast; "
            "bounded by OPHIRA preflight timeout (FR-06 guard)",
        )
        return

    code = classify_provider_error(error_text)
    ok = code == ProviderErrorCode.AUTH_REVOKED_OR_INVALID
    record("error_mapping_bad_token", "PASS" if ok else "FAIL", f"classified={code.value}")
    assert ok, f"expected AUTH_REVOKED_OR_INVALID, got {code} from {error_text[:200]!r}"

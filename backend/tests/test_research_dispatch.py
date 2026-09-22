import asyncio
import json

import pytest
from test_sdk_adapter import build_runtime

from ophira_backend.constants import RESEARCH_READ_TOOL_NAMES, RESEARCH_UNITS, EventType, RunMode
from ophira_backend.context_protocol import mode_rules


async def invoke(hook, unit, identifier, **extra):
    return await hook({
        "hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_use_id": identifier,
        "tool_input": {"subagent_type": unit, "prompt": "Read sources", **extra},
    }, identifier, {})


def denied(result):
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


async def test_three_configured_units_are_allowed_once_and_other_types_are_denied(tmp_path):
    ledger, run, runtime = build_runtime(tmp_path)
    run = ledger.update_run(run.id, mode=RunMode.DEEP_RESEARCH)
    hook = runtime._hook(run.id, ledger.workspace_for_project(run.project_id))
    for index, unit in enumerate(RESEARCH_UNITS):
        assert not denied(await invoke(hook, unit, f"dispatch_{index}"))
    assert denied(await invoke(hook, "general-purpose", "unconfigured"))
    assert denied(await invoke(hook, None, "unspecified"))
    assert denied(await invoke(hook, "source-mapper", "fourth_instance"))
    # Replaying the same native tool invocation is the original claim.
    assert not denied(await invoke(hook, "source-mapper", "dispatch_0"))


async def test_parallel_duplicate_dispatch_has_only_one_authorized_claim(tmp_path):
    ledger, run, runtime = build_runtime(tmp_path)
    ledger.update_run(run.id, mode=RunMode.DEEP_RESEARCH)
    hook = runtime._hook(run.id, ledger.workspace_for_project(run.project_id))
    outcomes = await asyncio.gather(*(invoke(hook, "source-mapper", f"call_{i}") for i in range(3)))
    assert sum(not denied(value) for value in outcomes) == 1


@pytest.mark.parametrize("resume_id,unit,allowed", [
    ("native_1", "source-mapper", True), ("native_1", None, True),
    ("other_run_agent", "source-mapper", False), ("native_1", "conflict-verifier", False),
    ("native_1", "general-purpose", False), ("", "source-mapper", False),
])
async def test_resume_must_match_this_runs_actual_research_unit(tmp_path, resume_id, unit, allowed):
    ledger, run, runtime = build_runtime(tmp_path)
    run = ledger.update_run(run.id, mode=RunMode.DEEP_RESEARCH)
    hook = runtime._hook(run.id, ledger.workspace_for_project(run.project_id))
    await invoke(hook, "source-mapper", "dispatch_1")
    await hook({"hook_event_name": "SubagentStart", "agent_id": "native_1",
                "agent_type": "source-mapper"}, None, {})
    result = await invoke(hook, unit, "dispatch_2", resume=resume_id)
    assert denied(result) is not allowed


async def test_normal_mode_retains_native_one_off_branches(tmp_path):
    ledger, run, runtime = build_runtime(tmp_path)
    hook = runtime._hook(run.id, ledger.workspace_for_project(run.project_id))
    assert not denied(await invoke(hook, "general-purpose", "normal_branch"))
    assert not any(event.type == EventType.SUBAGENT_STARTED for event in ledger.list_events(run.id))


def test_mode_contract_requires_three_returns_and_main_agent_source_verification():
    rules = mode_rules(RunMode.DEEP_RESEARCH)
    assert all(name in rules for name in RESEARCH_UNITS)
    assert "source_receipt" in rules and "record_evidence_batch" in rules
    assert "去重" in rules and "冲突" in rules and "覆盖缺口" in rules
    assert "resume" in rules


@pytest.mark.parametrize("mode", [RunMode.STANDARD, RunMode.DEEP_RESEARCH])
async def test_native_startup_rules_allow_research_reads_without_weakening_writes(
    tmp_path, monkeypatch, mode,
):
    from worker_report_fixture import synthetic_worker_finish, synthetic_worker_start

    from ophira_backend import sdk_adapter
    from ophira_backend.native_hooks import NativeHookBridge

    ledger, run, runtime = build_runtime(tmp_path)
    run = ledger.update_run(run.id, mode=mode)
    options = runtime._build_options(run)
    runtime.durable_resume_supported = True
    captured = []

    class Bridge(NativeHookBridge):
        async def __aenter__(self):
            self.path = tmp_path / "unused-offline-socket"
            return self

    class Client:
        def __init__(self, *, options):
            captured.append(options)

        async def __aenter__(self):
            synthetic_worker_start(captured[-1])
            return self

        async def __aexit__(self, *_args):
            synthetic_worker_finish(captured[-1])

        async def query(self, _prompt):
            pass

        async def receive_response(self):
            if False:
                yield None

    monkeypatch.setattr(sdk_adapter, "NativeHookBridge", Bridge)
    monkeypatch.setattr(sdk_adapter, "ClaudeSDKClient", Client)
    await runtime._run_sdk_session(run, options)
    settings = json.loads(captured[0].settings)
    ask = settings["permissions"]["ask"]
    assert {"Write", "Edit", "Bash", "Agent"} <= set(ask)
    assert bool(set(RESEARCH_READ_TOOL_NAMES) & set(ask)) is (mode == RunMode.STANDARD)
    assert set(RESEARCH_READ_TOOL_NAMES) <= set(captured[0].allowed_tools)
    assert set(settings["hooks"]) >= {"PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop"}
    assert captured[0].hooks == {}  # The one startup policy bridge owns all checks.


@pytest.mark.parametrize("tool,arguments,allowed", [
    ("Read", {"file_path": "uploads/quote.md"}, True),
    ("Read", {"file_path": "/outside/other-project/quote.md"}, False),
    ("Write", {"file_path": "report.md", "content": "child write"}, False),
    ("Bash", {"command": "printf child"}, False),
    ("mcp__ophira_ledger__read_project_context", {}, True),
    ("mcp__ophira_ledger__record_evidence_batch", {"entries": []}, False),
    ("Agent", {"subagent_type": "source-mapper", "prompt": "nested"}, False),
])
async def test_research_startup_hook_keeps_read_fence_and_main_writer_authority(
    tmp_path, tool, arguments, allowed,
):
    from ophira_backend.constants import RunStatus

    ledger, run, runtime = build_runtime(tmp_path)
    run = ledger.update_run(run.id, mode=RunMode.DEEP_RESEARCH, status=RunStatus.RUNNING)
    runtime._build_options(run)
    runtime.durable_resume_supported = True
    workspace = ledger.workspace_for_project(run.project_id)
    (workspace / "uploads").mkdir(exist_ok=True)
    (workspace / "uploads/quote.md").write_text("Synthetic quote", encoding="utf-8")
    hook = runtime._permission_hook(run, workspace)
    result = await hook({"hook_event_name": "PreToolUse", "tool_name": tool,
                         "tool_input": arguments, "tool_use_id": "research_read",
                         "agent_id": "native_research", "agent_type": "source-mapper"},
                        "research_read", {})
    assert result["hookSpecificOutput"]["permissionDecision"] == ("allow" if allowed else "deny")
    assert not ledger.get_project(run.project_id).evidence
    assert not (workspace / "report.md").exists()

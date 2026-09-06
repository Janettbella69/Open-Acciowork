"""Explicit SDK message fixtures for the host's durable approval contract.

Actual CLI execution is separately covered by native_defer_probe.py.
"""

from types import SimpleNamespace

from claude_agent_sdk import AssistantMessage, StreamEvent, ToolUseBlock

from ophira_backend.constants import PermissionDecision, RunStatus
from ophira_backend.models import EventActor


async def approve_native_fixture(runtime, run, name, tool_input, resolution):
    ledger = runtime.ledger
    options = runtime._build_options(run)
    session = "155bdf00-c97f-4f29-a334-ea4af4e71453"
    tool_id = "toolu_permission_fixture"
    ledger.update_run(run.id, status=RunStatus.RUNNING, sdk_session_id=session)
    for event in [
        {"type": "message_start"},
        {"type": "content_block_start", "content_block": {"type": "tool_use", "id": tool_id}},
        {"type": "message_stop"},
    ]:
        await runtime._map_message(run.id, StreamEvent(
            uuid="fixture", session_id=session, event=event,
        ))
    await runtime._map_message(run.id, AssistantMessage(
        content=[ToolUseBlock(id=tool_id, name=name, input=tool_input)], model="offline",
    ))
    hook = runtime._permission_hook(run, ledger.workspace_for_project(run.project_id))
    frame = {"hook_event_name": "PreToolUse", "session_id": session,
             "tool_use_id": tool_id, "tool_name": name, "tool_input": tool_input}
    initial = await hook(frame, tool_id, {})
    assert initial["hookSpecificOutput"]["permissionDecision"] == "defer"
    assert runtime.permissions.list_pending() == []
    wait = runtime.permissions.durable.confirm(run.id, session, tool_id, name, tool_input)
    runtime.permissions.durable.resolve(run.id, wait.id, resolution,
                                        EventActor(type="user", name="fixture"))
    runtime.permissions.durable.claim_resume(run.id)
    ledger.update_run(run.id, status=RunStatus.RUNNING)
    resumed = await hook(frame, tool_id, {})
    if resolution.decision == PermissionDecision.DENY:
        assert resumed["hookSpecificOutput"]["permissionDecision"] == "deny"
        return resumed
    assert resumed["hookSpecificOutput"]["permissionDecision"] == "allow"
    return await options.can_use_tool(name, tool_input,
                                     SimpleNamespace(tool_use_id=tool_id, agent_id=None))

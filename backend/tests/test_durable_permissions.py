from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import DeferredToolUse
from test_sdk_adapter import build_runtime

from ophira_backend.constants import PermissionDecision, RunStatus
from ophira_backend.durable_permissions import DurablePermissionConflict
from ophira_backend.events import EventPublisher
from ophira_backend.models import EventActor, PermissionResolution, ProjectCreate, utc_now
from ophira_backend.permissions import PermissionBroker
from ophira_backend.run_assembly import build_agent_runtime
from ophira_backend.runs import RunManager, RunResumeConflictError
from ophira_backend.storage import FileLedger

TOOL_INPUT = {"file_path": "report.txt", "content": "fixture", "api_key": "fixture-secret"}
SESSION = "154f7d99-cef0-437a-9254-456d402f356b"
ACTOR = EventActor(type="user", name="fixture-user")
ALLOW = PermissionResolution(decision=PermissionDecision.ALLOW_ONCE)


def checkpoint(tmp_path):
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Approval fixture", objective="offline"))
    run = ledger.create_run(project.id, "Write fixture")
    ledger.update_run(run.id, status=RunStatus.RUNNING)
    broker = PermissionBroker(ledger, EventPublisher(ledger), 300)
    wait = broker.durable.prepare(run.id, SESSION, "toolu_fixture", "Write", TOOL_INPUT, None)
    assert broker.list_pending() == []  # A hook request is not yet a durable SDK suspension.
    broker.durable.confirm(run.id, SESSION, "toolu_fixture", "Write", TOOL_INPUT)
    return ledger, run, broker, wait


def test_wait_is_visible_to_a_second_worker_with_precise_redacted_anchor(tmp_path):
    ledger, run, broker, wait = checkpoint(tmp_path)
    other = PermissionBroker(FileLedger(tmp_path), EventPublisher(ledger), 300)
    view = other.list_pending()[0]
    assert (view.id, view.run_id, view.tool_use_id) == (wait.id, run.id, "toolu_fixture")
    assert view.durable and view.state == "pending"
    assert view.tool_input["api_key"] != "fixture-secret"
    assert "fixture-secret" not in ledger._run_path(run.id).read_text()
    other.durable.resolve(run.id, wait.id, ALLOW, ACTOR)
    assert broker.list_pending()[0].state == "resolved"


def test_resolution_and_consumption_are_atomic_across_real_ledger_instances(tmp_path):
    ledger, run, broker, wait = checkpoint(tmp_path)
    other = PermissionBroker(FileLedger(tmp_path), EventPublisher(ledger), 300)

    def resolve(store):
        try:
            store.resolve(run.id, wait.id, ALLOW, ACTOR)
            return True
        except DurablePermissionConflict:
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(resolve, [broker.durable, other.durable])) == [False, True]
    broker.durable.claim_resume(run.id)
    ledger.update_run(run.id, status=RunStatus.RUNNING)

    def consume(store):
        try:
            store.consume(run.id, SESSION, "toolu_fixture", "Write", TOOL_INPUT, None)
            return True
        except DurablePermissionConflict:
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(consume, [broker.durable, other.durable])) == [False, True]


@pytest.mark.parametrize("alteration", ["input", "tool", "id", "session", "agent", "identity"])
def test_consumption_rejects_any_changed_invocation_or_identity(tmp_path, alteration):
    ledger, run, broker, wait = checkpoint(tmp_path)
    broker.durable.resolve(run.id, wait.id, ALLOW, ACTOR)
    broker.durable.claim_resume(run.id)
    ledger.update_run(run.id, status=RunStatus.RUNNING)
    args = [run.id, SESSION, "toolu_fixture", "Write", TOOL_INPUT, None]
    if alteration == "input":
        args[4] = {**TOOL_INPUT, "content": "changed"}
    elif alteration == "tool":
        args[3] = "Edit"
    elif alteration == "id":
        args[2] = "toolu_other"
    elif alteration == "session":
        args[1] = "other-session"
    elif alteration == "agent":
        args[5] = "child-agent"
    else:
        ledger.update_run(run.id, provider_profile_id="changed-provider")
    with pytest.raises(DurablePermissionConflict):
        broker.durable.consume(*args)
    assert broker.durable.lookup(run.id, wait.id).state == "resolved"


@pytest.mark.parametrize("resolved", [False, True])
def test_expiry_is_enforced_again_at_resolution_and_resume(tmp_path, resolved):
    ledger, run, broker, wait = checkpoint(tmp_path)
    if resolved:
        broker.durable.resolve(run.id, wait.id, ALLOW, ACTOR)
    current = broker.durable.lookup(run.id, wait.id)
    expired = current.model_copy(update={"expires_at": utc_now() - timedelta(seconds=1)})
    ledger.update_run(run.id, permission_waits=[expired])
    assert broker.list_pending()[0].state == "expired"
    with pytest.raises(DurablePermissionConflict, match="expired"):
        if resolved:
            broker.durable.claim_resume(run.id)
        else:
            broker.durable.resolve(run.id, wait.id, ALLOW, ACTOR)


class ConsumingRuntime:
    durable_resume_supported = True

    def __init__(self, broker):
        self.broker = broker
        self.calls = 0

    async def run(self, run):
        self.broker.durable.consume(run.id, SESSION, "toolu_fixture", "Write", TOOL_INPUT, None)
        self.calls += 1

    async def cancel(self, _run_id):
        pass


@pytest.mark.asyncio
async def test_second_worker_approves_and_resumes_same_run_exactly_once(tmp_path):
    ledger, run, broker, wait = checkpoint(tmp_path)
    original_start = utc_now() - timedelta(seconds=30)
    ledger.update_run(run.id, started_at=original_start)
    other = PermissionBroker(FileLedger(tmp_path), EventPublisher(ledger), 300)
    runtime = ConsumingRuntime(other)
    manager = RunManager(other.ledger, other.events, other, runtime)
    await manager.resolve_permission(run.id, wait.id, ALLOW)
    task = manager._tasks[run.id]
    await task
    assert runtime.calls == 1
    assert len(ledger.list_runs()) == 1
    assert ledger.get_run(run.id).status == RunStatus.COMPLETED
    assert ledger.get_run(run.id).started_at == original_start
    with pytest.raises(DurablePermissionConflict):
        await manager.resolve_permission(run.id, wait.id, ALLOW)


@pytest.mark.asyncio
async def test_live_owner_conflict_does_not_record_a_decision(tmp_path):
    ledger, run, broker, wait = checkpoint(tmp_path)
    manager = RunManager(ledger, broker.events, broker, ConsumingRuntime(broker))
    lease = manager._acquire_run_lease(run.id)
    assert lease is not None
    try:
        with pytest.raises(DurablePermissionConflict, match="still stopping"):
            await manager.resolve_permission(run.id, wait.id, ALLOW)
        assert broker.durable.lookup(run.id, wait.id).state == "pending"
    finally:
        import os

        os.close(lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["pending", "resolved", "consumed", "preparing"])
async def test_restart_preserves_only_confirmed_unconsumed_checkpoints(tmp_path, stage):
    ledger, run, broker, wait = checkpoint(tmp_path)
    if stage in {"resolved", "consumed"}:
        broker.durable.resolve(run.id, wait.id, ALLOW, ACTOR)
        broker.durable.claim_resume(run.id)
    if stage == "consumed":
        ledger.update_run(run.id, status=RunStatus.RUNNING)
        broker.durable.consume(run.id, SESSION, "toolu_fixture", "Write", TOOL_INPUT, None)
    elif stage == "preparing":
        current = broker.durable.lookup(run.id, wait.id)
        ledger.update_run(
            run.id,
            status=RunStatus.RUNNING,
            permission_waits=[
                current.model_copy(update={"state": "preparing", "confirmed_at": None})
            ],
        )
    runtime = ConsumingRuntime(broker)
    manager = RunManager(ledger, broker.events, broker, runtime)
    await manager.recover_interrupted()
    assert runtime.calls == 0
    current = ledger.get_run(run.id)
    if stage in {"pending", "resolved"}:
        assert current.status == RunStatus.WAITING_PERMISSION
        assert broker.list_pending()[0].state == stage
    else:
        assert current.status == RunStatus.FAILED
        assert broker.durable.lookup(run.id, wait.id).state == "uncertain"
        with pytest.raises(DurablePermissionConflict):
            await manager.resume_permission(run.id)
        with pytest.raises(RunResumeConflictError):
            manager._resolve_resume_source(
                SESSION,
                project_id=run.project_id,
                agent_id=None,
                provider_profile_id=None,
                principal=None,
            )


@pytest.mark.asyncio
async def test_cancel_invalidates_the_durable_decision_across_workers(tmp_path):
    ledger, run, broker, wait = checkpoint(tmp_path)
    manager = RunManager(ledger, broker.events, broker, ConsumingRuntime(broker))
    await manager.cancel(run.id)
    assert broker.durable.lookup(run.id, wait.id).state == "cancelled"
    with pytest.raises(DurablePermissionConflict):
        await manager.resolve_permission(run.id, wait.id, ALLOW)
    assert broker.list_pending() == []


async def observed_call(runtime, run, *, count=1):
    runtime.ledger.update_run(run.id, status=RunStatus.RUNNING)
    await runtime._map_message(
        run.id,
        StreamEvent(
            uuid="stream-start",
            session_id=SESSION,
            event={"type": "message_start"},
        ),
    )
    blocks = []
    for index in range(count):
        identifier = f"toolu_{index}"
        await runtime._map_message(
            run.id,
            StreamEvent(
                uuid=f"stream-{index}",
                session_id=SESSION,
                event={
                    "type": "content_block_start",
                    "content_block": {"type": "tool_use", "id": identifier},
                },
            ),
        )
        blocks.append(ToolUseBlock(id=identifier, name="Write", input=TOOL_INPUT))
    await runtime._map_message(run.id, AssistantMessage(content=blocks, model="offline"))
    await runtime._map_message(
        run.id,
        StreamEvent(
            uuid="stream-stop",
            session_id=SESSION,
            event={"type": "message_stop"},
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("count,expected", [(1, "defer"), (2, "deny")])
async def test_hook_defers_only_a_proven_complete_single_tool_batch(tmp_path, count, expected):
    ledger, run, runtime = build_runtime(tmp_path)
    runtime.durable_resume_supported = True
    await observed_call(runtime, run, count=count)
    hook = runtime._permission_hook(run, ledger.workspace_for_project(run.project_id))
    result = await hook(
        {
            "hook_event_name": "PreToolUse",
            "session_id": SESSION,
            "tool_use_id": "toolu_0",
            "tool_name": "Write",
            "tool_input": TOOL_INPUT,
        },
        "toolu_0",
        {},
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == expected
    assert runtime.permissions.list_pending() == []  # Await SDK confirmation.


@pytest.mark.asyncio
async def test_pinned_skill_invocation_preserves_native_auto_approval(tmp_path):
    ledger, run, runtime = build_runtime(tmp_path)
    ledger.update_run(run.id, status=RunStatus.RUNNING)
    hook = runtime._permission_hook(run, ledger.workspace_for_project(run.project_id))
    result = await hook({"hook_event_name": "PreToolUse", "session_id": SESSION,
                         "tool_use_id": "skill", "tool_name": "Skill",
                         "tool_input": {"skill": "ophira-core:landed-cost-analysis"}}, "skill", {})
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert runtime.permissions.list_pending() == []
    assert ledger.get_run(run.id).permission_waits == []


@pytest.mark.asyncio
async def test_delegated_stream_cannot_close_or_replace_main_tool_batch(tmp_path):
    _ledger, run, runtime = build_runtime(tmp_path)
    for parent, event in [
        (None, {"type": "message_start"}),
        (None, {"type": "content_block_start", "content_block": {
            "type": "tool_use", "id": "main-one"}}),
        ("branch", {"type": "message_start"}),
        ("branch", {"type": "content_block_start", "content_block": {
            "type": "tool_use", "id": "child-read"}}),
        ("branch", {"type": "message_stop"}),
        (None, {"type": "content_block_start", "content_block": {
            "type": "tool_use", "id": "main-two"}}),
        (None, {"type": "message_stop"}),
    ]:
        await runtime._map_message(run.id, StreamEvent(
            uuid="fixture", session_id=SESSION, event=event, parent_tool_use_id=parent,
        ))
    assert runtime._closed_tool_batches[run.id] == {"main-one": 2, "main-two": 2}


@pytest.mark.asyncio
async def test_native_clarification_result_has_a_deduplicated_anchored_audit(tmp_path):
    ledger, run, runtime = build_runtime(tmp_path)
    runtime._single_tool_calls[run.id] = {"question": ("AskUserQuestion", {"questions": []})}
    result = UserMessage(content=[ToolResultBlock(
        tool_use_id="question", content="User answered EU", is_error=False,
    )])
    await runtime._map_message(run.id, result)
    await runtime._map_message(run.id, result)
    await runtime._map_message(run.id, UserMessage(content=[ToolResultBlock(
        tool_use_id="unanchored", content="Untrusted completion claim", is_error=False,
    )]))
    events = [e for e in ledger.list_events(run.id) if e.type == "tool.completed"]
    assert len(events) == 1
    assert events[0].tool_use_id == "question"
    assert events[0].data["tool_response"] == "User answered EU"


@pytest.mark.asyncio
async def test_clarifying_answer_handoff_validates_original_then_returns_user_answer(tmp_path):
    ledger, run, runtime = build_runtime(tmp_path)
    options = runtime._build_options(run)
    question = {"questions": [{"question": "Which market?"}]}
    resolution = PermissionResolution(decision=PermissionDecision.ALLOW_ONCE,
                                      answers={"Which market?": "EU"})
    runtime._approved_invocations[run.id] = {"question": ("AskUserQuestion", question, resolution)}
    context = SimpleNamespace(tool_use_id="question", agent_id=None)
    result = await options.can_use_tool("AskUserQuestion", question, context)
    assert result.updated_input == {**question, "answers": {"Which market?": "EU"}}
    assert any("EU" in str(c) for c in ledger.get_project(run.project_id).constraints)
    runtime._approved_invocations[run.id] = {"question": ("AskUserQuestion", question, resolution)}
    changed = await options.can_use_tool("AskUserQuestion", {"questions": []}, context)
    assert changed.behavior == "deny"


def test_resume_revalidates_provider_organization_allowlist(tmp_path):
    from ophira_backend.models import OrgPolicy

    ledger, run, runtime = build_runtime(tmp_path)
    runtime._build_options(run)
    runtime.policy.save(OrgPolicy(allowed_provider_ids=["another-provider"]))
    with pytest.raises(DurablePermissionConflict, match="no longer allowed"):
        runtime.validate_deferred_resume(ledger.get_run(run.id))


@pytest.mark.asyncio
async def test_deep_research_cannot_refresh_its_original_wall_clock_on_resume(tmp_path):
    from ophira_backend.constants import RunMode
    from ophira_backend.sdk_adapter import AgentRunError

    ledger, run, runtime = build_runtime(tmp_path)
    deadline = utc_now() - timedelta(seconds=1)
    run = ledger.update_run(run.id, mode=RunMode.DEEP_RESEARCH,
                            sdk_wall_clock_deadline=deadline)
    runtime._require_provider = lambda _run: None
    with pytest.raises(DurablePermissionConflict, match="wall-clock"):
        runtime.validate_deferred_resume(run)
    with pytest.raises(AgentRunError, match="wall-clock"):
        await runtime.run(run)
    assert ledger.get_run(run.id).sdk_wall_clock_deadline == deadline


@pytest.mark.asyncio
async def test_successful_deferred_sdk_result_does_not_complete_the_run(tmp_path, monkeypatch):
    from worker_report_fixture import synthetic_worker_finish, synthetic_worker_start

    ledger, run, runtime = build_runtime(tmp_path)
    runtime.durable_resume_supported = False  # Mapping-only test, not a native socket test.
    broker = runtime.permissions
    broker.durable.prepare(run.id, SESSION, "toolu_fixture", "Write", TOOL_INPUT, None)

    class Client:
        def __init__(self, **_kwargs):
            self.options = _kwargs["options"]

        async def __aenter__(self):
            synthetic_worker_start(self.options)
            return self

        async def __aexit__(self, *_args):
            synthetic_worker_finish(self.options)

        async def query(self, _prompt):
            pass

        async def receive_response(self):
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=SESSION,
                stop_reason="tool_deferred",
                deferred_tool_use=DeferredToolUse("toolu_fixture", "Write", TOOL_INPUT),
            )

    monkeypatch.setattr("ophira_backend.sdk_adapter.ClaudeSDKClient", Client)

    class Runtime:
        async def run(self, record):
            await runtime._run_sdk_session(record, runtime._build_options(record))

    manager = RunManager(ledger, broker.events, broker, Runtime())
    await manager._execute(run.id)
    assert ledger.get_run(run.id).status == RunStatus.WAITING_PERMISSION
    assert broker.list_pending()[0].durable


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_limit", [None, 12])
async def test_sdk_segment_usage_is_cumulative_and_duplicate_uuid_is_not_charged_twice(
    tmp_path, turn_limit,
):
    ledger, run, runtime = build_runtime(tmp_path)
    # Finite turns are an explicit CI/probe option, never the ordinary Run default.
    runtime.settings.max_turns = turn_limit
    original = runtime._build_options(run)
    for number, cost, turns in [(1, 0.4, 2), (2, 0.3, 3)]:
        result = ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=5,
            is_error=False,
            num_turns=turns,
            session_id=SESSION,
            stop_reason="tool_deferred",
            total_cost_usd=cost,
            uuid=f"result-{number}",
            usage={"input_tokens": 20, "output_tokens": 10},
            model_usage={
                "fixture": {
                    "inputTokens": 20,
                    "outputTokens": 10,
                    "costUSD": cost,
                    "contextWindow": 200000,
                }
            },
        )
        await runtime._map_message(run.id, result)
        await runtime._map_message(run.id, result)
    events = [event for event in ledger.list_events(run.id) if event.type == "run.result"]
    assert len(events) == 2
    total = events[-1].data
    assert total["total_cost_usd"] == pytest.approx(0.7)
    assert total["num_turns"] == 5 and total["segment_num_turns"] == 3
    assert total["usage"] == {"input_tokens": 40, "output_tokens": 20}
    assert total["model_usage"]["fixture"]["inputTokens"] == 40
    assert total["model_usage"]["fixture"]["contextWindow"] == 200000
    assert total["result_scope"] == "run_cumulative"
    remaining = runtime._build_options(ledger.get_run(run.id))
    assert remaining.max_budget_usd == pytest.approx(original.max_budget_usd - 0.7)
    assert remaining.max_turns == (None if turn_limit is None else turn_limit - 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("reported_turns", [None, 10_000])
async def test_default_and_resumed_sdk_options_keep_unlimited_turns_and_frozen_spend(
    tmp_path, reported_turns,
):
    ledger, run, runtime = build_runtime(tmp_path)
    assert runtime.settings.max_turns is None
    original = runtime._build_options(run)
    assert original.max_turns is None
    assert ledger.get_run(run.id).sdk_turn_limit is None

    broker = runtime.permissions
    broker.durable.prepare(run.id, SESSION, "toolu_fixture", "Write", TOOL_INPUT, None)
    wait = broker.durable.confirm(run.id, SESSION, "toolu_fixture", "Write", TOOL_INPUT)
    broker.durable.resolve(run.id, wait.id, ALLOW, ACTOR)
    await runtime._map_message(
        run.id,
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=reported_turns,
            session_id=SESSION,
            total_cost_usd=0.4,
        ),
    )

    # Rebuilding the adapter models process replacement. Later configuration
    # changes cannot add a turn cap or refresh this logical Run's money budget.
    runtime.settings.max_turns = 1
    runtime.settings.max_budget_usd *= 2
    replacement = build_agent_runtime(
        runtime.settings, ledger, runtime.events, broker, skills=runtime.skills,
    )
    current = ledger.get_run(run.id)
    replacement.validate_deferred_resume(current)
    resumed = replacement._build_options(current)
    assert resumed.resume == SESSION
    assert resumed.max_turns is None
    assert resumed.max_budget_usd == pytest.approx(original.max_budget_usd - 0.4)
    assert ledger.get_run(run.id).sdk_budget_usd == original.max_budget_usd


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,turn_limit",
    [
        ("money", None), ("overspend", None), ("unknown", None),
        ("turns", 6), ("unknown_turns", 6),
    ],
)
async def test_suspended_run_cannot_refresh_exhausted_or_unknown_sdk_limits(
    tmp_path, kind, turn_limit,
):
    ledger, run, runtime = build_runtime(tmp_path)
    runtime.settings.max_turns = turn_limit
    initial = runtime._build_options(run)
    cost = {
        "money": initial.max_budget_usd,
        "overspend": initial.max_budget_usd + 0.1,
        "unknown": None,
        "turns": 0.1,
        "unknown_turns": 0.1,
    }[kind]
    reported_turns = initial.max_turns if kind == "turns" else 1
    if kind == "unknown_turns":
        reported_turns = None
    await runtime._map_message(
        run.id,
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=reported_turns,
            session_id=SESSION,
            total_cost_usd=cost,
        ),
    )
    runtime.settings.max_turns = None if turn_limit is None else turn_limit * 2
    runtime.settings.max_budget_usd *= 2
    current = ledger.get_run(run.id)
    with pytest.raises(DurablePermissionConflict, match="unknown|exhausted"):
        runtime.validate_deferred_resume(current)
    with pytest.raises(DurablePermissionConflict, match="unknown|exhausted"):
        runtime._build_options(current)

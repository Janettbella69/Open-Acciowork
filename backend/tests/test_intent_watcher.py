"""FR-26 autonomous standing-intent watcher safety and recovery contract."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from ophira_backend.automation import IntentStore, NotificationStore
from ophira_backend.automation_tools import build_automation_tools
from ophira_backend.config import Settings
from ophira_backend.constants import (
    HEARTBEAT_OK,
    TOOL_LIST_STANDING_INTENTS,
    TOOL_SEND_NOTIFICATION,
    TOOL_TRIGGER_STANDING_INTENT,
    RunMode,
    RunRetryStrategy,
    RunStatus,
    StandingIntentWatcherStatus,
)
from ophira_backend.events import EventPublisher
from ophira_backend.intent_watcher import StandingIntentWatcher
from ophira_backend.models import (
    ProjectCreate,
    RunRecord,
    StandingIntent,
    StandingIntentCreate,
    utc_now,
)
from ophira_backend.permissions import PermissionBroker
from ophira_backend.run_assembly import build_agent_runtime
from ophira_backend.runs import RunManager, RunRetryConflictError
from ophira_backend.storage import FileLedger


class ReadyProviders:
    def __init__(self, *, ready: bool = True, budget_scale: float = 1.0) -> None:
        self.ready = ready
        self.budget_scale = budget_scale

    def default_profile_id(self) -> str:
        return "approved"

    def get(self, profile_id: str):
        assert profile_id == "approved"
        return SimpleNamespace(
            id=profile_id,
            enabled=True,
            production_eligible=True,
            secret_configured=True,
            last_verified_at=utc_now() if self.ready else None,
            last_error_code=None,
            model="claude-test",
            budget_scale=self.budget_scale,
        )

    def snapshot(self, profile_id: str) -> dict[str, object]:
        return {"id": profile_id, "budget_scale": self.budget_scale}

    def build_worker_env(self, profile_id: str) -> dict[str, str]:
        assert profile_id == "approved"
        return {}


class PassiveRuntime:
    def __init__(self) -> None:
        self.seen: list[RunRecord] = []

    async def run(self, run: RunRecord) -> None:
        self.seen.append(run)

    async def cancel(self, run_id: str) -> None:
        return None


class FailingRuntime(PassiveRuntime):
    async def run(self, run: RunRecord) -> None:
        self.seen.append(run)
        raise RuntimeError("temporary source timeout")


class BlockingRuntime(PassiveRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, run: RunRecord) -> None:
        self.seen.append(run)
        self.started.set()
        await self.release.wait()


class TriggeringRuntime(PassiveRuntime):
    def __init__(
        self,
        ledger: FileLedger,
        intents: IntentStore,
        notifications: NotificationStore,
    ) -> None:
        super().__init__()
        self.ledger = ledger
        self.intents = intents
        self.notifications = notifications

    async def run(self, run: RunRecord) -> None:
        self.seen.append(run)
        assert run.standing_intent_id is not None
        tools = build_automation_tools(
            self.ledger,
            self.intents,
            self.notifications,
            run.project_id,
            run_id=run.id,
            bound_intent_id=run.standing_intent_id,
        )
        trigger = next(
            item for item in tools if item.name == "trigger_standing_intent"
        )
        result = await trigger.handler(
            {
                "intent_id": run.standing_intent_id,
                "summary": "Observed price is 11% below the user threshold.",
            }
        )
        assert json.loads(result["content"][0]["text"])["outcome"] == "triggered"


def build_watcher(
    tmp_path: Path,
    *,
    runtime: PassiveRuntime | None = None,
    providers: ReadyProviders | None = None,
    intent_store: IntentStore | None = None,
):
    ledger = FileLedger(tmp_path / "data")
    project = ledger.create_project(ProjectCreate(name="Watch", objective="Monitor"))
    events = EventPublisher(ledger)
    notifications = NotificationStore(tmp_path / "notifications")
    intents = intent_store or IntentStore(tmp_path / "intents")
    selected_runtime = runtime or PassiveRuntime()
    runs = RunManager(
        ledger,
        events,
        PermissionBroker(ledger, events, timeout_seconds=30),
        selected_runtime,
        providers=providers or ReadyProviders(),  # type: ignore[arg-type]
        notifications=notifications,
    )
    watcher = StandingIntentWatcher(
        ledger,
        runs,
        intents,
        notifications,
        interval_seconds=0.01,
        failure_backoff_seconds=1,
        max_failure_backoff_seconds=7 * 24 * 60 * 60,
        instance_id="watcher_test",
    )
    return ledger, project, selected_runtime, runs, intents, notifications, watcher


async def wait_terminal(ledger: FileLedger, run_id: str) -> RunRecord:
    for _ in range(200):
        run = ledger.get_run(run_id)
        if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            return run
        await asyncio.sleep(0.005)
    raise AssertionError(f"run {run_id} did not terminate")


def create_intent(
    intents: IntentStore,
    project_id: str,
    **changes,
) -> StandingIntent:
    payload = {
        "project_id": project_id,
        "intent": "Tell me only when the observed price drops by at least 10%.",
        "check_interval_minutes": 15,
    }
    payload.update(changes)
    return intents.create(StandingIntentCreate(**payload))


@pytest.mark.asyncio
async def test_due_check_runs_same_standard_main_agent_and_false_is_silent(
    tmp_path: Path,
) -> None:
    ledger, project, runtime, _runs, intents, notifications, watcher = build_watcher(
        tmp_path
    )
    intent = create_intent(
        intents,
        project.id,
        agent_id="agent_main",
        provider_profile_id="approved",
    )

    dispatched = await watcher.tick(now=intent.created_at)
    started = next(item for item in dispatched if item.outcome == "started")
    assert started.run is not None
    run = started.run
    assert run.agent_id == "agent_main"
    assert run.mode == RunMode.STANDARD
    assert run.standing_intent_id == intent.id
    assert run.automation_claim_id
    assert run.max_budget_usd == 0.5
    assert "巡检到期本身绝不代表条件成立" in run.prompt
    assert HEARTBEAT_OK in run.prompt

    await wait_terminal(ledger, run.id)
    await watcher.tick(now=intent.created_at + timedelta(seconds=1))

    stored = intents.get(intent.id)
    assert len(runtime.seen) == 1
    assert stored.last_check_run_id == run.id
    assert stored.last_check_at == intent.created_at
    assert stored.next_check_at is not None
    assert stored.active_claim_id is None
    assert stored.budget_committed_usd == 0.5
    assert notifications.list() == []  # Completion or model text never implies a hit.


@pytest.mark.asyncio
async def test_only_bound_trigger_tool_creates_a_hit_notification(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path / "data")
    project = ledger.create_project(ProjectCreate(name="Watch", objective="Monitor"))
    events = EventPublisher(ledger)
    intents = IntentStore(tmp_path / "intents")
    notifications = NotificationStore(tmp_path / "notifications")
    runtime = TriggeringRuntime(ledger, intents, notifications)
    runs = RunManager(
        ledger,
        events,
        PermissionBroker(ledger, events, timeout_seconds=30),
        runtime,
        providers=ReadyProviders(),  # type: ignore[arg-type]
    )
    watcher = StandingIntentWatcher(ledger, runs, intents, notifications)
    intent = create_intent(intents, project.id)

    dispatched = await watcher.tick(now=intent.created_at)
    run = next(item.run for item in dispatched if item.outcome == "started")
    assert run is not None
    await wait_terminal(ledger, run.id)
    await watcher.tick(now=intent.created_at + timedelta(seconds=1))

    notices = notifications.list()
    assert len(notices) == 1
    assert notices[0].intent_id == intent.id
    assert notices[0].run_id == run.id
    assert intents.get(intent.id).triggered_count == 1


@pytest.mark.asyncio
async def test_disabled_and_legacy_unauthorized_intents_are_never_checked(tmp_path: Path) -> None:
    _ledger, project, runtime, _runs, intents, _notifications, watcher = build_watcher(
        tmp_path
    )
    paused = create_intent(intents, project.id)
    intents.set_enabled(paused.id, False)
    legacy = create_intent(intents, project.id, intent="legacy record")
    values = legacy.model_dump()
    values.update(
        authorization_source=None,
        authorized_at=None,
        next_check_at=None,
    )
    intents._write(StandingIntent.model_validate(values))

    await watcher.tick(now=legacy.created_at)

    assert runtime.seen == []
    assert intents.get(paused.id).watcher_status == StandingIntentWatcherStatus.PAUSED
    assert (
        intents.get(legacy.id).watcher_status
        == StandingIntentWatcherStatus.UNAUTHORIZED
    )

    intents.set_enabled(legacy.id, False)
    with pytest.raises(ValueError, match="explicit user reauthorization"):
        intents.set_enabled(legacy.id, True)
    reauthorized = intents.set_enabled(
        legacy.id,
        True,
        authorization_source="user_api",
        now=legacy.created_at + timedelta(seconds=1),
    )
    assert reauthorized.authorization_source == "user_api"
    assert reauthorized.authorized_at == legacy.created_at + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_concurrent_watchers_atomically_dispatch_only_one_run(tmp_path: Path) -> None:
    runtime = BlockingRuntime()
    ledger, project, _runtime, runs, intents, notifications, first = build_watcher(
        tmp_path,
        runtime=runtime,
    )
    second_store = IntentStore(tmp_path / "intents")
    second = StandingIntentWatcher(
        ledger,
        runs,
        second_store,
        notifications,
        instance_id="watcher_two",
    )
    intent = create_intent(intents, project.id)

    left, right = await asyncio.gather(
        first.run_now(intent.id, now=intent.created_at),
        second.run_now(intent.id, now=intent.created_at),
    )

    assert {left.outcome, right.outcome} == {"started", "busy"}
    await asyncio.wait_for(runtime.started.wait(), timeout=1)
    assert len(ledger.list_runs(project.id)) == 1
    runtime.release.set()
    run = ledger.list_runs(project.id)[0]
    await wait_terminal(ledger, run.id)
    await first.tick(now=intent.created_at + timedelta(seconds=1))


@pytest.mark.asyncio
async def test_pausing_an_active_intent_cancels_its_run_without_failure_notice(
    tmp_path: Path,
) -> None:
    runtime = BlockingRuntime()
    ledger, project, _runtime, _runs, intents, notifications, watcher = build_watcher(
        tmp_path,
        runtime=runtime,
    )
    intent = create_intent(intents, project.id)
    dispatched = await watcher.run_now(intent.id, now=intent.created_at)
    assert dispatched.run is not None
    await asyncio.wait_for(runtime.started.wait(), timeout=1)

    intents.set_enabled(intent.id, False)
    outcomes = await watcher.tick(now=intent.created_at + timedelta(seconds=1))

    assert any(item.outcome == "paused" for item in outcomes)
    assert ledger.get_run(dispatched.run.id).status == RunStatus.CANCELLED
    paused = intents.get(intent.id)
    assert paused.enabled is False
    assert paused.active_claim_id is None
    assert paused.watcher_status == StandingIntentWatcherStatus.PAUSED
    assert paused.budget_committed_usd == 0.5
    assert notifications.list() == []


@pytest.mark.asyncio
async def test_restart_recovers_run_created_before_claim_attachment_then_times_out(
    tmp_path: Path,
) -> None:
    ledger, project, runtime, _runs, intents, notifications, watcher = build_watcher(
        tmp_path
    )
    intent = create_intent(intents, project.id, check_timeout_seconds=60)
    outcome, claimed = intents.try_claim_check(
        intent.id,
        owner="dead_process",
        now=intent.created_at,
    )
    assert outcome == "claimed"
    assert claimed.active_claim_id is not None
    orphan = ledger.create_run(
        project.id,
        "orphaned check",
        standing_intent_id=intent.id,
        automation_claim_id=claimed.active_claim_id,
        provider_profile_id="approved",
        max_budget_usd=claimed.per_check_budget_usd,
    )

    await watcher.tick(now=intent.created_at + timedelta(seconds=1))
    recovered = intents.get(intent.id)
    assert recovered.active_check_run_id == orphan.id
    assert recovered.budget_reserved_usd == 0
    assert recovered.budget_committed_usd == 0.5
    assert runtime.seen == []  # Persisted queued run is not silently launched twice.

    await watcher.tick(now=intent.created_at + timedelta(seconds=61))
    failed = intents.get(intent.id)
    assert failed.active_claim_id is None
    assert failed.watcher_status == StandingIntentWatcherStatus.BACKOFF
    assert failed.next_check_at is not None
    assert ledger.get_run(orphan.id).status == RunStatus.CANCELLED
    assert len(notifications.list()) == 1
    assert len(ledger.list_runs(project.id)) == 1


@pytest.mark.asyncio
async def test_post_persist_dispatch_error_keeps_claim_and_recovers_single_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, project, runtime, runs, intents, _notifications, watcher = build_watcher(
        tmp_path
    )
    intent = create_intent(intents, project.id)
    original_create = runs.create_and_start

    async def create_then_lose_response(*args, **kwargs):
        await original_create(*args, **kwargs)
        raise RuntimeError("response lost after durable run creation")

    monkeypatch.setattr(runs, "create_and_start", create_then_lose_response)

    dispatched = await watcher.run_now(intent.id, now=intent.created_at)

    assert dispatched.outcome == "started"
    assert dispatched.run is not None
    assert intents.get(intent.id).active_check_run_id == dispatched.run.id
    assert intents.get(intent.id).budget_committed_usd == 0.5
    await wait_terminal(ledger, dispatched.run.id)
    await watcher.tick(now=intent.created_at + timedelta(seconds=1))
    assert len(runtime.seen) == 1
    assert len(ledger.list_runs(project.id)) == 1
    assert intents.get(intent.id).active_claim_id is None


@pytest.mark.asyncio
async def test_attachment_error_retains_reservation_for_next_tick_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, project, runtime, _runs, intents, _notifications, watcher = build_watcher(
        tmp_path
    )
    intent = create_intent(intents, project.id)
    original_attach = intents.attach_check_run
    attempts = 0

    def fail_first_attachment(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary intent-store write failure")
        return original_attach(*args, **kwargs)

    monkeypatch.setattr(intents, "attach_check_run", fail_first_attachment)

    dispatched = await watcher.run_now(intent.id, now=intent.created_at)

    assert dispatched.outcome == "started"
    assert dispatched.run is not None
    unbound = intents.get(intent.id)
    assert unbound.active_claim_id == dispatched.run.automation_claim_id
    assert unbound.active_check_run_id is None
    assert unbound.budget_reserved_usd == 0.5
    await wait_terminal(ledger, dispatched.run.id)
    await watcher.tick(now=intent.created_at + timedelta(seconds=1))
    assert attempts >= 2
    assert len(runtime.seen) == 1
    assert len(ledger.list_runs(project.id)) == 1
    recovered = intents.get(intent.id)
    assert recovered.active_claim_id is None
    assert recovered.budget_reserved_usd == 0
    assert recovered.budget_committed_usd == 0.5


@pytest.mark.asyncio
async def test_provider_must_be_ready_before_any_evaluation_run(tmp_path: Path) -> None:
    ledger, project, runtime, _runs, intents, notifications, watcher = build_watcher(
        tmp_path,
        providers=ReadyProviders(ready=False),
    )
    intent = create_intent(intents, project.id)

    result = await watcher.run_now(intent.id, now=intent.created_at)

    assert result.outcome == "backoff"
    assert runtime.seen == []
    assert ledger.list_runs(project.id) == []
    stored = intents.get(intent.id)
    assert stored.budget_reserved_usd == 0
    assert stored.budget_committed_usd == 0
    assert stored.consecutive_failures == 1
    assert stored.next_check_at is not None
    assert stored.next_check_at >= intent.created_at + timedelta(minutes=15)
    assert "not ready for runs" in (stored.last_check_error or "")
    assert notifications.list()[0].intent_id == intent.id


@pytest.mark.asyncio
async def test_expiry_cooldown_and_trigger_limit_gate_checks_before_dispatch(
    tmp_path: Path,
) -> None:
    ledger, project, runtime, _runs, intents, _notifications, watcher = build_watcher(
        tmp_path
    )
    expired = create_intent(intents, project.id, expires_days=1)
    cooling = create_intent(intents, project.id, intent="cooling")
    exhausted = create_intent(
        intents,
        project.id,
        intent="exhausted",
        max_triggers=1,
        cooldown_hours=0,
    )
    assert intents.try_trigger(cooling.id, now=cooling.created_at)[0] == "triggered"
    assert intents.try_trigger(exhausted.id, now=exhausted.created_at)[0] == "triggered"

    expired_result = await watcher.run_now(
        expired.id,
        now=expired.created_at + timedelta(days=2),
    )
    cooling_result = await watcher.run_now(
        cooling.id,
        now=cooling.created_at + timedelta(hours=1),
    )
    exhausted_result = await watcher.run_now(
        exhausted.id,
        now=exhausted.created_at + timedelta(seconds=1),
    )

    assert expired_result.outcome == "expired"
    assert cooling_result.outcome == "cooldown"
    assert exhausted_result.outcome == "trigger_exhausted"
    assert intents.get(expired.id).enabled is False
    assert runtime.seen == []
    assert ledger.list_runs(project.id) == []


@pytest.mark.asyncio
async def test_failed_run_is_visible_and_uses_exponential_backoff(tmp_path: Path) -> None:
    ledger, project, runtime, _runs, intents, notifications, watcher = build_watcher(
        tmp_path,
        runtime=FailingRuntime(),
    )
    intent = create_intent(intents, project.id)
    first = await watcher.run_now(intent.id, now=intent.created_at)
    assert first.run is not None
    await wait_terminal(ledger, first.run.id)

    await watcher.tick(now=intent.created_at + timedelta(seconds=1))

    stored = intents.get(intent.id)
    assert len(runtime.seen) == 1
    assert stored.watcher_status == StandingIntentWatcherStatus.BACKOFF
    assert stored.consecutive_failures == 1
    assert stored.budget_committed_usd == 0.5
    assert stored.last_check_error == "temporary source timeout"
    assert stored.next_check_at is not None
    assert stored.next_check_at >= intent.created_at + timedelta(minutes=15)
    assert len(notifications.list()) == 1
    assert notifications.list()[0].run_id == first.run.id


@pytest.mark.asyncio
async def test_total_budget_is_a_hard_sum_of_started_run_caps(tmp_path: Path) -> None:
    ledger, project, runtime, _runs, intents, notifications, watcher = build_watcher(
        tmp_path
    )
    intent = create_intent(
        intents,
        project.id,
        per_check_budget_usd=0.5,
        total_budget_usd=0.5,
    )
    first = await watcher.run_now(intent.id, now=intent.created_at)
    assert first.run is not None
    await wait_terminal(ledger, first.run.id)
    await watcher.tick(now=intent.created_at + timedelta(seconds=1))

    exhausted = intents.get(intent.id)
    assert exhausted.budget_committed_usd == 0.5
    assert exhausted.watcher_status == StandingIntentWatcherStatus.BUDGET_EXHAUSTED
    assert exhausted.next_check_at is None
    again = await watcher.run_now(
        intent.id,
        now=intent.created_at + timedelta(days=1),
    )
    assert again.outcome == "budget_exhausted"
    assert len(runtime.seen) == 1
    assert len(ledger.list_runs(project.id)) == 1
    assert notifications.list() == []


@pytest.mark.asyncio
async def test_single_leader_lock_hands_over_cleanly(tmp_path: Path) -> None:
    ledger, _project, _runtime, runs, intents, notifications, first = build_watcher(
        tmp_path
    )
    second = StandingIntentWatcher(
        ledger,
        runs,
        IntentStore(tmp_path / "intents"),
        notifications,
        interval_seconds=0.01,
        instance_id="watcher_follower",
    )

    assert await first.start() is True
    assert await second.start() is False
    await first.stop()
    for _ in range(100):
        if second.is_leader:
            break
        await asyncio.sleep(0.005)
    assert second.is_leader is True
    await second.stop()


@pytest.mark.asyncio
async def test_sdk_enforces_per_check_cap_and_monitor_only_tool_envelope(
    tmp_path: Path,
) -> None:
    ledger = FileLedger(tmp_path / "data")
    project = ledger.create_project(ProjectCreate(name="Watch", objective="Monitor"))
    intents = IntentStore(tmp_path / "intents")
    notifications = NotificationStore(tmp_path / "notifications")
    intent = create_intent(intents, project.id)
    run = ledger.create_run(
        project.id,
        "check",
        standing_intent_id=intent.id,
        automation_claim_id="intentclaim_test",
        provider_profile_id="approved",
        max_budget_usd=0.25,
    )
    events = EventPublisher(ledger)
    runtime = build_agent_runtime(
        Settings(data_dir=tmp_path / "data", max_budget_usd=10),
        ledger,
        events,
        PermissionBroker(ledger, events, timeout_seconds=30),
        providers=ReadyProviders(budget_scale=4),  # type: ignore[arg-type]
        intents=intents,
        notifications=notifications,
    )
    run = ledger.update_run(run.id, skill_snapshot=runtime.skills.snapshot())

    options = runtime._build_options(run)

    assert options.max_budget_usd == pytest.approx(1.0)
    assert TOOL_LIST_STANDING_INTENTS in options.allowed_tools
    assert TOOL_TRIGGER_STANDING_INTENT in options.allowed_tools
    assert TOOL_SEND_NOTIFICATION not in options.allowed_tools
    assert "Agent" not in options.allowed_tools
    assert "无人值守条件巡检" in options.system_prompt["append"]

    hook = runtime._hook(run.id, ledger.workspace_for_project(project.id))
    denied_send = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": TOOL_SEND_NOTIFICATION,
            "tool_input": {"title": "forged"},
        },
        None,
        None,
    )
    denied_other_intent = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": TOOL_TRIGGER_STANDING_INTENT,
            "tool_input": {"intent_id": "intent_other", "summary": "forged"},
        },
        None,
        None,
    )
    assert denied_send["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert denied_other_intent["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.asyncio
async def test_bound_tool_cannot_consume_another_intents_trigger_budget(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path / "data")
    project = ledger.create_project(ProjectCreate(name="Watch", objective="Monitor"))
    intents = IntentStore(tmp_path / "intents")
    notifications = NotificationStore(tmp_path / "notifications")
    bound = create_intent(intents, project.id, intent="bound")
    other = create_intent(intents, project.id, intent="other")
    tools = build_automation_tools(
        ledger,
        intents,
        notifications,
        project.id,
        run_id="run_test",
        bound_intent_id=bound.id,
    )
    trigger = next(item for item in tools if item.name == "trigger_standing_intent")

    response = await trigger.handler(
        {"intent_id": other.id, "summary": "should not be accepted"}
    )
    stale_response = await trigger.handler(
        {"intent_id": bound.id, "summary": "late result from a stale run"}
    )

    assert response["is_error"] is True
    assert json.loads(stale_response["content"][0]["text"])["outcome"] == "stale_check"
    assert intents.get(other.id).triggered_count == 0
    assert intents.get(bound.id).triggered_count == 0
    assert notifications.list() == []


@pytest.mark.asyncio
async def test_watcher_runs_cannot_bypass_budget_with_generic_retry(tmp_path: Path) -> None:
    ledger, project, _runtime, runs, intents, _notifications, watcher = build_watcher(
        tmp_path,
        runtime=FailingRuntime(),
    )
    intent = create_intent(intents, project.id)
    dispatched = await watcher.run_now(intent.id, now=intent.created_at)
    assert dispatched.run is not None
    failed = await wait_terminal(ledger, dispatched.run.id)

    with pytest.raises(RunRetryConflictError, match="watcher budget"):
        await runs.retry(failed.id, RunRetryStrategy.NEW_BRANCH)

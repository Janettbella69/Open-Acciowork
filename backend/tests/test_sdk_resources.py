import asyncio
import dataclasses
import json
from datetime import timedelta

import anyio
import pytest
from claude_agent_sdk import AssistantMessage, TextBlock, ThinkingBlock
from test_sdk_adapter import build_runtime

from ophira_backend import sdk_resources
from ophira_backend.config import Settings
from ophira_backend.constants import EventType, ProviderErrorCode, RunMode, RunStatus
from ophira_backend.models import ProjectCreate, SdkResourceUsage, utc_now
from ophira_backend.providers import classify_provider_error
from ophira_backend.runs import RunManager
from ophira_backend.sdk_resources import (
    RESERVATION_BYTES,
    SdkExecutionBudget,
    SdkResourceLimitError,
)
from ophira_backend.storage import FileLedger


def setup_budget(tmp_path, *, output=3 * RESERVATION_BYTES, seconds=60):
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Resources", objective="Verify"))
    run = ledger.create_run(project.id, "Read public sources")
    ledger.update_run(run.id, sdk_resource_usage=SdkResourceUsage(
        active_limit_seconds=seconds, output_limit_bytes=output, message_buffer_bytes=1024,
    ))
    return ledger, run, SdkExecutionBudget(ledger, run.id, Settings(data_dir=tmp_path))


def test_output_reservations_survive_new_ledger_and_do_not_replenish(tmp_path):
    ledger, run, budget = setup_budget(tmp_path, output=2 * RESERVATION_BYTES)
    budget.consume(RESERVATION_BYTES - 1)
    assert ledger.get_run(run.id).sdk_resource_usage.output_bytes_reserved == RESERVATION_BYTES
    budget.close()
    resumed_ledger = FileLedger(tmp_path)
    resumed = SdkExecutionBudget(resumed_ledger, run.id, Settings(data_dir=tmp_path))
    resumed.consume(RESERVATION_BYTES)
    with pytest.raises(SdkResourceLimitError, match="output allowance"):
        resumed.consume(1)
    resumed.close()
    final = resumed_ledger.get_run(run.id).sdk_resource_usage
    assert final.output_bytes_reserved == 2 * RESERVATION_BYTES
    assert final.active_started_at is None


def test_message_utf8_count_includes_all_blocks_without_saving_private_content(tmp_path):
    message = AssistantMessage(
        content=[TextBlock("跨境📦"), ThinkingBlock("PRIVATE_THINKING_CANARY", "sig")],
        model="fixture",
    )
    size = len(json.dumps(
        dataclasses.asdict(message), ensure_ascii=False, separators=(",", ":"),
    ).encode()) + 1
    ledger, run, budget = setup_budget(tmp_path, output=size)
    budget.message(message)
    with pytest.raises(SdkResourceLimitError):
        budget.consume(1)
    budget.close()
    assert ledger.get_run(run.id).sdk_resource_usage.output_bytes_reserved == size
    for path in tmp_path.rglob("*.json*"):
        assert "PRIVATE_THINKING_CANARY" not in path.read_text()


def test_stderr_exhaustion_signals_owner_and_never_persists_text(tmp_path):
    ledger, run, budget = setup_budget(tmp_path, output=10)
    budget.stderr("PRIVATE_STDERR_CANARY")
    assert budget.exceeded.is_set()
    assert isinstance(budget.error, SdkResourceLimitError)
    budget.stderr("ignored after exhaustion")
    budget.close()
    assert "PRIVATE_STDERR_CANARY" not in ledger.get_run(run.id).model_dump_json()
    assert classify_provider_error(budget.error) == ProviderErrorCode.SDK_RESOURCE_LIMIT_EXCEEDED


def test_active_time_excludes_suspended_wait_and_limits_remain_frozen(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(sdk_resources, "monotonic", lambda: clock[0])
    ledger, run, budget = setup_budget(tmp_path, seconds=60)
    clock[0] += 15
    budget.close()
    budget.close()  # Finalization is idempotent.
    monkeypatch.setattr(sdk_resources, "utc_now", lambda: utc_now() + timedelta(days=10))
    resumed = SdkExecutionBudget(ledger, run.id, Settings(
        data_dir=tmp_path, sdk_active_timeout_seconds=7200,
    ))
    assert resumed.remaining_seconds == 45
    assert resumed.message_buffer_bytes == 1024
    clock[0] += 3
    resumed.close()
    assert ledger.get_run(run.id).sdk_resource_usage.active_seconds_used == 18


def test_crashed_active_interval_is_conservatively_charged(tmp_path, monkeypatch):
    ledger, run, budget = setup_budget(tmp_path, seconds=60)
    started = ledger.get_run(run.id).sdk_resource_usage.active_started_at
    monkeypatch.setattr(sdk_resources, "utc_now", lambda: started + timedelta(seconds=61))
    # Simulate a killed owner: deliberately do not close its in-memory budget.
    del budget
    resumed = SdkExecutionBudget(FileLedger(tmp_path), run.id, Settings(data_dir=tmp_path))
    assert resumed.remaining_seconds == 0
    assert resumed.exceeded.is_set()
    with pytest.raises(SdkResourceLimitError, match="time limit"):
        resumed.consume(1)
    resumed.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [RunMode.STANDARD, RunMode.PLAN, RunMode.DEEP_RESEARCH])
async def test_all_run_modes_stop_on_output_exhaustion_and_cleanup(
    tmp_path, monkeypatch, mode,
):
    ledger, run, runtime = build_runtime(tmp_path)
    run = ledger.update_run(run.id, mode=mode, sdk_resource_usage=SdkResourceUsage(
        active_limit_seconds=60, output_limit_bytes=100, message_buffer_bytes=1024,
    ))
    monkeypatch.setattr(runtime, "_require_provider", lambda _run: None)
    cleaned = asyncio.Event()

    async def session(_run, options, *, output_budget, **_kwargs):
        assert options.max_buffer_size == 1024
        assert options.stderr is not None
        try:
            options.stderr("x" * 101)
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(runtime, "_run_sdk_session", session)
    with pytest.raises(SdkResourceLimitError, match="output allowance"):
        await asyncio.wait_for(runtime.run(run), 2)
    assert cleaned.is_set()
    assert ledger.get_run(run.id).sdk_resource_usage.active_started_at is None
    assert run.id not in runtime._prepared_contexts


@pytest.mark.asyncio
async def test_active_timeout_closes_sdk_and_retains_artifact_in_failed_run(tmp_path, monkeypatch):
    ledger, run, runtime = build_runtime(tmp_path)
    ledger.update_run(run.id, sdk_resource_usage=SdkResourceUsage(
        active_limit_seconds=0.1, output_limit_bytes=10000, message_buffer_bytes=1024,
    ))
    monkeypatch.setattr(runtime, "_require_provider", lambda _run: None)
    cleaned = asyncio.Event()

    async def session(_run, _options, **_kwargs):
        workspace = ledger.workspace_for_project(run.project_id)
        (workspace / "saved-report.md").write_text("Verified work before the timeout")
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(runtime, "_run_sdk_session", session)
    manager = RunManager(ledger, runtime.events, runtime.permissions, runtime)
    await manager._execute_runtime(run.id)
    current = ledger.get_run(run.id)
    assert current.status == RunStatus.FAILED
    assert current.error_code == ProviderErrorCode.SDK_RESOURCE_LIMIT_EXCEEDED
    assert cleaned.is_set()
    assert current.sdk_resource_usage.active_started_at is None
    assert any(a.path == "saved-report.md" for a in ledger.get_project(run.project_id).artifacts)
    assert any(e.type == EventType.RUN_FAILED for e in ledger.list_events(run.id))


@pytest.mark.asyncio
async def test_user_cancellation_is_not_reclassified_as_resource_failure(tmp_path, monkeypatch):
    ledger, run, runtime = build_runtime(tmp_path)
    monkeypatch.setattr(runtime, "_require_provider", lambda _run: None)
    entered, closed = asyncio.Event(), asyncio.Event()

    async def session(_run, _options, **_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(runtime, "_run_sdk_session", session)
    task = asyncio.create_task(runtime.run(run))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert ledger.get_run(run.id).sdk_resource_usage.active_started_at is None


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_sdk_shielded_child_cleanup(tmp_path, monkeypatch):
    ledger, run, runtime = build_runtime(tmp_path)
    monkeypatch.setattr(runtime, "_require_provider", lambda _run: None)
    entered, closing, release, closed = (asyncio.Event() for _ in range(4))

    async def session(_run, _options, **_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            # Mirrors the installed SDK's documented subprocess-close shield.
            with anyio.CancelScope(shield=True):
                closing.set()
                await release.wait()
                closed.set()

    monkeypatch.setattr(runtime, "_run_sdk_session", session)
    task = asyncio.create_task(runtime.run(run))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    await asyncio.wait_for(closing.wait(), 2)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    assert ledger.get_run(run.id).sdk_resource_usage.active_started_at is not None
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert closed.is_set()
    assert ledger.get_run(run.id).sdk_resource_usage.active_started_at is None


@pytest.mark.asyncio
async def test_oversized_message_never_enters_persisted_event_stream(tmp_path, monkeypatch):
    from ophira_backend import sdk_adapter

    ledger, run, runtime = build_runtime(tmp_path)
    monkeypatch.setattr(runtime, "_require_provider", lambda _run: None)
    runtime.durable_resume_supported = False
    ledger.update_run(run.id, sdk_resource_usage=SdkResourceUsage(
        active_limit_seconds=60, output_limit_bytes=100, message_buffer_bytes=1024,
    ))
    closed = asyncio.Event()

    from worker_report_fixture import synthetic_worker_finish, synthetic_worker_start

    class Client:
        def __init__(self, **_kwargs):
            self.options = _kwargs["options"]

        async def __aenter__(self):
            synthetic_worker_start(self.options)
            return self

        async def __aexit__(self, *_args):
            synthetic_worker_finish(self.options)
            closed.set()

        async def query(self, *_args, **_kwargs):
            pass

        async def receive_response(self):
            yield AssistantMessage(content=[TextBlock("OVER_LIMIT_CANARY" * 20)], model="fixture")

    monkeypatch.setattr(sdk_adapter, "ClaudeSDKClient", Client)
    with pytest.raises(SdkResourceLimitError):
        await runtime.run(run)
    assert closed.is_set()
    assert all("OVER_LIMIT_CANARY" not in e.model_dump_json() for e in ledger.list_events(run.id))
    assert not runtime._clients
    assert not runtime._interjection_channels


@pytest.mark.parametrize("field,value", [
    ("sdk_active_timeout_seconds", 0), ("sdk_output_limit_bytes", 0),
    ("sdk_message_buffer_bytes", 0), ("sdk_active_timeout_seconds", float("inf")),
])
def test_resource_configuration_cannot_disable_limits(field, value):
    with pytest.raises(ValueError):
        Settings(**{field: value})

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from claude_agent_sdk import ResultMessage
from httpx import ASGITransport, AsyncClient
from test_sdk_adapter import build_runtime
from worker_report_fixture import synthetic_worker_finish, synthetic_worker_start

from ophira_backend import sdk_adapter
from ophira_backend.config import Settings
from ophira_backend.constants import EventType, RunStatus
from ophira_backend.events import EventPublisher
from ophira_backend.main import create_app
from ophira_backend.models import EventActor, ProjectCreate, RunInterjectionRequest
from ophira_backend.native_hooks import NativeHookBridge
from ophira_backend.permissions import PermissionBroker
from ophira_backend.run_interjections import (
    RunInterjectionConflict,
    prompt_digest,
    validate_interjection_prompt,
)
from ophira_backend.runs import RunManager
from ophira_backend.storage import FileLedger


class HoldingRuntime:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.count = 0
        self.ledger: FileLedger | None = None

    async def run(self, run) -> None:
        self.count += 1
        assert self.ledger is not None
        self.ledger.update_run(run.id, sdk_session_id="interjection-session")
        if self.count == 1:
            self.started.set()
            await self.release.wait()

    async def cancel(self, _run_id: str) -> None:
        self.release.set()


async def wait_terminal(ledger: FileLedger, run_id: str) -> None:
    for _ in range(200):
        if ledger.get_run(run_id).status in {RunStatus.COMPLETED, RunStatus.FAILED}:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("run did not finish")


@pytest.mark.parametrize("prompt", [
    "/clear", "/reset", "/new", "  /clear\n", "/reset now", "\ufeff /NeW\t",
])
def test_native_session_reset_commands_are_not_business_directions(prompt):
    with pytest.raises(RunInterjectionConflict, match="Start a new task"):
        validate_interjection_prompt(prompt)


@pytest.mark.parametrize("prompt", [
    "Explain /clear and /new", "/clearance report", "/new/report.md", "`/reset`",
])
def test_ordinary_text_mentions_and_paths_are_preserved(prompt):
    validate_interjection_prompt(prompt)


@pytest.mark.parametrize("mode", ["now", "interrupt", "next_turn"])
async def test_interjection_api_rejects_reset_before_persistence(tmp_path, mode):
    runtime = HoldingRuntime()
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=runtime)
    runtime.ledger = app.state.ledger
    project = runtime.ledger.create_project(ProjectCreate(name="Reset gate", objective="Offline"))
    run = await app.state.runs.create_and_start(project.id, "Start")
    try:
        await asyncio.wait_for(runtime.started.wait(), 2)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://localhost",
        ) as client:
            rejected = await client.post(f"/api/runs/{run.id}/interjections", json={
                "request_id": str(uuid4()), "prompt": "/clear", "mode": mode,
            })
        assert rejected.status_code == 409
        assert "Start a new task" in rejected.json()["detail"]
        assert not any(event.type == EventType.RUN_INTERJECTION_REQUESTED
                       for event in runtime.ledger.list_events(run.id))
    finally:
        runtime.release.set()
        await wait_terminal(runtime.ledger, run.id)


@pytest.mark.asyncio
async def test_interjection_api_is_durable_idempotent_and_rejects_changed_content(
    tmp_path: Path,
) -> None:
    runtime = HoldingRuntime()
    app = create_app(Settings(data_dir=tmp_path), runtime=runtime)
    runtime.ledger = app.state.ledger
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        project = await client.post(
            "/api/projects", json={"name": "Kettle", "objective": "Validate"},
        )
        started = await client.post(
            f"/api/projects/{project.json()['id']}/runs", json={"prompt": "Start"},
        )
        run_id = started.json()["id"]
        await asyncio.wait_for(runtime.started.wait(), 2)
        message = {
            "request_id": str(uuid4()), "prompt": "Check the landed cost first",
            "mode": "next_turn",
        }
        route = f"/api/runs/{run_id}/interjections"
        first = await client.post(route, json=message)
        replay = await client.post(route, json=message)
        changed = await client.post(route, json={**message, "prompt": "Different instruction"})
        assert first.status_code == replay.status_code == 202
        assert first.json() == replay.json()
        assert first.json()["status"] == "queued"
        assert changed.status_code == 409
        events = app.state.ledger.list_events(run_id)
        assert sum(item.type == EventType.RUN_INTERJECTION_REQUESTED for item in events) == 1
        runtime.release.set()
        await wait_terminal(app.state.ledger, run_id)


@pytest.mark.asyncio
async def test_queued_message_starts_exact_next_sdk_session_run(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    events = EventPublisher(ledger)
    runtime = HoldingRuntime()
    runtime.ledger = ledger
    runs = RunManager(ledger, events, PermissionBroker(ledger, events, 30), runtime)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    first = await runs.create_and_start(project.id, "Original task")
    await asyncio.wait_for(runtime.started.wait(), 2)
    message = RunInterjectionRequest(
        request_id=str(uuid4()), prompt="Prioritize the UK", mode="next_turn",
    )
    receipt = await runs.interject(
        first.id, message, actor=EventActor(type="user", name="test-user"),
    )
    assert receipt.status == "queued"
    runtime.release.set()
    await wait_terminal(ledger, first.id)

    for _ in range(200):
        followers = [item for item in ledger.list_runs(project.id) if item.id != first.id]
        if followers:
            break
        await asyncio.sleep(0.01)
    assert len(followers) == 1
    second = followers[0]
    await wait_terminal(ledger, second.id)
    assert "Prioritize the UK" in ledger.get_run(second.id).prompt
    assert second.resume_source_run_id == first.id
    assert second.carried_interjection_ids == [message.request_id]
    carried = [item for item in ledger.list_events(first.id)
               if item.type == EventType.RUN_INTERJECTION_CARRIED]
    assert len(carried) == 1
    assert carried[0].data["next_run_id"] == second.id


@pytest.mark.asyncio
async def test_uncertain_sdk_write_is_visible_and_never_carried_automatically(
    tmp_path: Path,
) -> None:
    ledger = FileLedger(tmp_path)
    events = EventPublisher(ledger)

    class FailingRuntime:
        async def run(self, _run) -> None:
            return None

        async def cancel(self, _run_id: str) -> None:
            return None

        async def interject(self, run_id: str, request: RunInterjectionRequest) -> None:
            await events.publish(
                run_id, EventType.RUN_INTERJECTION_DISPATCHING,
                {"resource_id": request.request_id},
            )
            raise OSError("SDK write outcome unknown")

    runs = RunManager(ledger, events, PermissionBroker(ledger, events, 30), FailingRuntime())
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Original task")
    ledger.update_run(run.id, status=RunStatus.RUNNING)
    message = RunInterjectionRequest(
        request_id=str(uuid4()), prompt="Check costs", mode="now",
    )
    receipt = await runs.interject(
        run.id, message, actor=EventActor(type="user", name="test-user"),
    )
    assert receipt.status == "uncertain"
    assert [item.type for item in ledger.list_events(run.id)][-1] == (
        EventType.RUN_INTERJECTION_UNCERTAIN
    )
    replay = await runs.interject(
        run.id, message, actor=EventActor(type="user", name="test-user"),
    )
    assert replay == receipt


@pytest.mark.asyncio
async def test_revoked_or_unavailable_phone_authority_blocks_queued_continuation(
    tmp_path: Path,
) -> None:
    ledger = FileLedger(tmp_path)
    events = EventPublisher(ledger)
    runtime = HoldingRuntime()
    runtime.ledger = ledger
    runs = RunManager(ledger, events, PermissionBroker(ledger, events, 30), runtime)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Original task")
    ledger.update_run(run.id, status=RunStatus.RUNNING, sdk_session_id="interjection-session")
    request = RunInterjectionRequest(
        request_id=str(uuid4()), prompt="Send another report", mode="next_turn",
    )
    # A request accepted while the Run was active remains durable after it ends.
    await events.publish_run_interjection_requested(
        run.id,
        {"resource_id": request.request_id, "prompt": request.prompt,
         "prompt_digest": prompt_digest(request.prompt), "mode": request.mode.value},
        actor=EventActor(type="user", name="paired-device:pair_" + "a" * 32),
    )
    ledger.update_run(run.id, status=RunStatus.COMPLETED)
    await runs._start_queued_next_turn(run.id)
    assert len(ledger.list_runs(project.id)) == 1
    assert any(item.type == EventType.RUN_INTERJECTION_BLOCKED
               for item in ledger.list_events(run.id))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["now", "interrupt"])
async def test_streaming_sdk_drains_each_result_and_delivers_live_direction(
    tmp_path: Path, monkeypatch, mode: str,
) -> None:
    ledger, run, runtime = build_runtime(tmp_path)
    run = ledger.update_run(run.id, status=RunStatus.RUNNING)
    clients = []

    class StreamingClient:
        def __init__(self, *, options):
            self.options = options
            self.queries: list[str] = []
            self.results: asyncio.Queue[ResultMessage] = asyncio.Queue()
            self.interrupted = False
            self.query_changed = asyncio.Event()
            clients.append(self)

        async def __aenter__(self):
            synthetic_worker_start(self.options)
            return self

        async def __aexit__(self, *_args):
            synthetic_worker_finish(self.options)

        async def query(self, prompt):
            self.queries.append(prompt)
            self.query_changed.set()

        async def interrupt(self):
            self.interrupted = True

        async def receive_response(self):
            result = await self.results.get()
            yield result

    def result(*, aborted=False, total_cost_usd=0.1):
        return ResultMessage(
            subtype="error_during_execution" if aborted else "success",
            duration_ms=1, duration_api_ms=1, is_error=aborted,
            num_turns=1, session_id="stream-session", total_cost_usd=total_cost_usd,
            usage={"input_tokens": 10, "output_tokens": 2},
            terminal_reason="aborted_streaming" if aborted else "completed",
        )

    class Bridge(NativeHookBridge):
        async def __aenter__(self):
            self.path = tmp_path / "unused-offline-socket"
            return self

    monkeypatch.setattr(sdk_adapter, "NativeHookBridge", Bridge)
    monkeypatch.setattr(sdk_adapter, "ClaudeSDKClient", StreamingClient)
    task = asyncio.create_task(runtime._run_sdk_session(run, runtime._build_options(run)))
    for _ in range(200):
        if clients and clients[0].queries:
            break
        await asyncio.sleep(0.01)
    assert clients and len(clients[0].queries) == 1
    old_reset = RunInterjectionRequest(request_id=str(uuid4()), prompt="/clear", mode=mode)
    await runtime.events.publish_run_interjection_requested(
        run.id,
        {"resource_id": old_reset.request_id, "prompt": old_reset.prompt,
         "prompt_digest": prompt_digest(old_reset.prompt), "mode": mode},
        actor=EventActor(type="user", name="old-worker"),
    )
    assert await runtime.interject(run.id, old_reset) is False
    assert len(clients[0].queries) == 1
    assert clients[0].interrupted is False
    assert any(event.type == EventType.RUN_INTERJECTION_BLOCKED
               and event.data["resource_id"] == old_reset.request_id
               for event in ledger.list_events(run.id))
    request = RunInterjectionRequest(
        request_id=str(uuid4()), prompt="Focus on shipping constraints", mode=mode,
    )
    await runtime.events.publish_run_interjection_requested(
        run.id,
        {"resource_id": request.request_id, "prompt": request.prompt,
         "prompt_digest": prompt_digest(request.prompt), "mode": request.mode.value},
        actor=EventActor(type="user", name="other-worker"),
    )
    for _ in range(200):
        if mode == "interrupt" and clients[0].interrupted:
            break
        if mode == "now" and len(clients[0].queries) == 2:
            break
        await asyncio.sleep(0.01)
    if mode == "interrupt":
        assert clients[0].interrupted
        assert len(clients[0].queries) == 1
    else:
        assert clients[0].queries[-1] == request.prompt
    await clients[0].results.put(result(aborted=mode == "interrupt"))
    if mode == "interrupt":
        for _ in range(200):
            if len(clients[0].queries) == 2:
                break
            await asyncio.sleep(0.01)
        assert clients[0].queries[-1] == request.prompt
    await clients[0].results.put(result(total_cost_usd=0.3))
    await asyncio.wait_for(task, 2)
    kinds = [item.type for item in ledger.list_events(run.id)]
    assert EventType.RUN_INTERJECTION_DISPATCHING in kinds
    assert EventType.RUN_INTERJECTION_INJECTED in kinds
    results = [item.data for item in ledger.list_events(run.id)
               if item.type == EventType.RUN_RESULT]
    assert results[-1]["total_cost_usd"] == 0.3
    assert results[-1]["usage"] == {"input_tokens": 20, "output_tokens": 4}

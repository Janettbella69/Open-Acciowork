import asyncio
from pathlib import Path

import pytest
from claude_agent_sdk import ResultMessage, SystemMessage
from httpx import ASGITransport, AsyncClient
from test_auth_api import signed_headers, trusted_settings
from test_sdk_adapter import build_runtime

from ophira_backend import sdk_adapter
from ophira_backend.config import Settings
from ophira_backend.constants import EventType, PrincipalRole, RunRetryStrategy, RunStatus
from ophira_backend.context_protocol import reconcile_compaction_epoch
from ophira_backend.events import EventPublisher
from ophira_backend.main import create_app
from ophira_backend.models import EventActor, RunInterjectionRequest
from ophira_backend.native_hooks import NativeHookBridge
from ophira_backend.permissions import PermissionBroker
from ophira_backend.run_interjections import RunInterjectionConflict
from ophira_backend.runs import RunManager, RunRetryConflictError
from ophira_backend.sdk_adapter import AgentRunError


def sdk_result() -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="context-session", total_cost_usd=3,
        usage={"input_tokens": 900_000},
        model_usage={"test-model": {"inputTokens": 900_000, "contextWindow": 200_000}},
        terminal_reason="completed",
    )


class Bridge(NativeHookBridge):
    async def __aenter__(self):
        self.path = Path("/tmp/unused-offline-socket")
        return self


@pytest.mark.asyncio
async def test_manual_compaction_uses_native_command_and_confirmed_window_usage(
    tmp_path: Path, monkeypatch,
) -> None:
    ledger, initial, runtime = build_runtime(tmp_path)
    run = ledger.update_run(initial.id, manual_compaction=True, status=RunStatus.RUNNING)
    clients = []

    class Client:
        def __init__(self, *, options):
            self.options = options
            self.queries = []
            self.samples = iter((142_000, 41_000))
            clients.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def query(self, prompt):
            self.queries.append(prompt)

        async def get_context_usage(self):
            return {"totalTokens": next(self.samples), "maxTokens": 180_000,
                    "model": "test-model", "isAutoCompactEnabled": True}

        async def receive_response(self):
            boundary = SystemMessage(
                subtype="compact_boundary",
                data={"uuid": "compact-1", "compact_metadata": {"trigger": "manual"},
                      "summary": "private transcript must not be copied to the event"},
            )
            yield boundary
            yield boundary  # duplicate SDK observation cannot advance the epoch twice
            yield sdk_result()

    monkeypatch.setattr(sdk_adapter, "NativeHookBridge", Bridge)
    monkeypatch.setattr(sdk_adapter, "ClaudeSDKClient", Client)
    options = runtime._build_options(run)
    assert options.mcp_servers == {}
    assert options.allowed_tools == []
    assert options.plugins == []
    assert options.skills == []
    denial = await options.can_use_tool("Read", {}, None)
    assert denial.behavior == "deny"
    await runtime._run_sdk_session(run, options)

    assert clients[0].queries == ["/compact"]
    events = ledger.list_events(run.id)
    boundaries = [item for item in events if item.type == EventType.CONTEXT_COMPACTED]
    samples = [item for item in events if item.type == EventType.CONTEXT_USAGE]
    assert len(boundaries) == 1
    assert boundaries[0].data["trigger"] == "manual"
    assert "private transcript" not in str(boundaries[0].data)
    assert [item.data["used_tokens"] for item in samples] == [142_000, 41_000]
    assert all(item.data["max_tokens"] == 180_000 for item in samples)
    assert ledger.get_run(run.id).session_compaction_epoch == 1
    assert ledger.get_run(run.id).context_refresh is None


@pytest.mark.asyncio
async def test_unconfirmed_compaction_fails_and_next_turn_refreshes_all_context(
    tmp_path: Path, monkeypatch,
) -> None:
    ledger, initial, runtime = build_runtime(tmp_path)
    run = ledger.update_run(initial.id, manual_compaction=True, status=RunStatus.RUNNING)

    class Client:
        def __init__(self, *, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def query(self, prompt):
            assert prompt == "/compact"

        async def receive_response(self):
            yield sdk_result()

    monkeypatch.setattr(sdk_adapter, "NativeHookBridge", Bridge)
    monkeypatch.setattr(sdk_adapter, "ClaudeSDKClient", Client)
    with pytest.raises(AgentRunError, match="did not confirm"):
        await runtime._run_sdk_session(run, runtime._build_options(run))
    assert ledger.get_run(run.id).context_refresh is None
    assert not any(e.type == EventType.CONTEXT_COMPACTED for e in ledger.list_events(run.id))


@pytest.mark.asyncio
async def test_maintenance_run_cannot_accept_directions_or_retry_as_business_work(
    tmp_path: Path, monkeypatch,
) -> None:
    ledger, initial, runtime = build_runtime(tmp_path)
    events = EventPublisher(ledger)
    manager = RunManager(ledger, events, PermissionBroker(ledger, events, 30), runtime)
    run = ledger.update_run(initial.id, manual_compaction=True, status=RunStatus.RUNNING)

    def unexpected_capture(*_args, **_kwargs):
        raise AssertionError("maintenance run must not scan business artifacts")

    monkeypatch.setattr(ledger, "scan_workspace", unexpected_capture)
    hook = runtime._hook(run.id, ledger.workspace_for_project(run.project_id))
    gate = await hook(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash",
         "tool_input": {"command": "touch unexpected.txt"}, "tool_use_id": "tool-1"},
        "tool-1", None,
    )
    assert gate["hookSpecificOutput"]["permissionDecision"] == "deny"
    with pytest.raises(RunInterjectionConflict, match="does not accept"):
        await manager.interject(
            run.id,
            RunInterjectionRequest(
                request_id="d81f687c-c8be-468a-bab9-64130135d512", prompt="Change price",
            ),
            actor=EventActor(type="user", name="operator"),
        )
    await manager._fail_run(run.id, AgentRunError("missing compact boundary"))
    assert ledger.get_run(run.id).status == RunStatus.FAILED
    with pytest.raises(RunRetryConflictError, match="cannot be retried"):
        await manager.retry(run.id, RunRetryStrategy.NEW_BRANCH)


@pytest.mark.asyncio
async def test_auto_compaction_boundary_replays_and_forces_full_refresh(tmp_path: Path) -> None:
    ledger, initial, runtime = build_runtime(tmp_path)
    source = ledger.update_run(initial.id, sdk_session_id="context-session")
    runtime._build_options(source)
    assert ledger.get_run(source.id).context_refresh is not None
    await runtime._map_message(
        source.id,
        SystemMessage(
            subtype="compact_boundary",
            data={"uuid": "auto-boundary", "compact_metadata": {"trigger": "auto"}},
        ),
    )
    # Simulate a crash after the durable event append but before run.json changed.
    ledger.update_run(source.id, session_compaction_epoch=0)
    assert reconcile_compaction_epoch(ledger, source.id) == 1
    resumed = ledger.create_run(
        source.project_id, "Continue", resume_session_id="context-session",
        resume_source_run_id=source.id, session_compaction_epoch=1,
    )
    runtime._build_options(resumed)
    refresh = ledger.get_run(resumed.id).context_refresh
    assert refresh is not None
    assert refresh.mode == "full"
    assert refresh.fallback_reason == "session_compacted_since_baseline"
    assert any(e.type == EventType.CONTEXT_COMPACTED for e in ledger.list_events(source.id))


@pytest.mark.asyncio
async def test_compact_route_requires_latest_completed_session(tmp_path: Path) -> None:
    class Runtime:
        async def run(self, run):
            app.state.ledger.update_run(run.id, sdk_session_id="context-session")

        async def cancel(self, _run_id):
            pass

    app = create_app(Settings(data_dir=tmp_path), runtime=Runtime())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        created = await client.post(
            "/api/projects", json={"name": "Kettle", "objective": "Validate"},
        )
        project_id = created.json()["id"]
        first = await client.post(
            f"/api/projects/{project_id}/runs", json={"prompt": "Start"},
        )
        source_id = first.json()["id"]
        for _ in range(200):
            if app.state.ledger.get_run(source_id).status == RunStatus.COMPLETED:
                break
            await asyncio.sleep(0.01)
        result = await client.post(f"/api/runs/{source_id}/compact")
        assert result.status_code == 202
        compact = result.json()
        assert compact["manual_compaction"] is True
        assert compact["resume_source_run_id"] == source_id
        assert compact["resume_session_id"] == "context-session"
        assert compact["project_id"] == project_id
        second = await client.post(f"/api/runs/{source_id}/compact")
        assert second.status_code == 409
        assert len(app.state.ledger.list_runs(project_id)) == 2


@pytest.mark.asyncio
async def test_compact_route_enforces_project_and_exact_principal_session(tmp_path: Path) -> None:
    class Runtime:
        async def run(self, run):
            app.state.ledger.update_run(run.id, sdk_session_id="trusted-context-session")

        async def cancel(self, _run_id):
            pass

    app = create_app(trusted_settings(tmp_path), runtime=Runtime())

    async def signed(client, method, path, user, session, **kwargs):
        return await client.request(
            method, path,
            headers=signed_headers(
                method, path, user, PrincipalRole.OWNER, session_id=session,
            ),
            **kwargs,
        )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        project = await signed(
            client, "POST", "/api/projects", "owner@example.com", "gateway_session_A",
            json={"name": "Kettle", "objective": "Validate"},
        )
        assert project.status_code == 201
        project_id = project.json()["id"]
        started = await signed(
            client, "POST", f"/api/projects/{project_id}/runs",
            "owner@example.com", "gateway_session_A", json={"prompt": "Start"},
        )
        assert started.status_code == 202
        source_id = started.json()["id"]
        for _ in range(200):
            if app.state.ledger.get_run(source_id).status == RunStatus.COMPLETED:
                break
            await asyncio.sleep(0.01)
        path = f"/api/runs/{source_id}/compact"
        another_session = await signed(
            client, "POST", path, "owner@example.com", "gateway_session_B",
        )
        outsider = await signed(
            client, "POST", path, "outsider@example.com", "gateway_session_C",
        )
        accepted = await signed(
            client, "POST", path, "owner@example.com", "gateway_session_A",
        )
        assert another_session.status_code == 403
        assert outsider.status_code == 403
        assert accepted.status_code == 202
        assert accepted.json()["manual_compaction"] is True

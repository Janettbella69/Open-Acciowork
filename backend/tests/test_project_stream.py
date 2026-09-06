"""Offline HTTP acceptance for committed observations and transport completion."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from mcp.types import CallToolRequest, CallToolRequestParams
from test_auth_api import principal, request_as, trusted_settings

from ophira_backend.config import Settings
from ophira_backend.constants import (
    LEDGER_MCP_SERVER_NAME,
    STREAM_END_EVENT,
    EventType,
    PrincipalRole,
    RunStatus,
)
from ophira_backend.ledger_tools import build_ledger_tools
from ophira_backend.main import create_app
from ophira_backend.models import ProjectCreate


class OfflineRuntime:
    async def run(self, _run):
        return None

    async def cancel(self, _run_id):
        return None


def setup(tmp_path):
    app = create_app(
        Settings(_env_file=None, data_dir=tmp_path, ANTHROPIC_API_KEY="", ANTHROPIC_AUTH_TOKEN=""),
        runtime=OfflineRuntime(),
    )
    project = app.state.ledger.create_project(ProjectCreate(name="Stream", objective="Keep tail"))
    return app, project


async def commit_evidence(app, run):
    # Deliberately omit the projector: simulate a crash after business commit.
    tools = build_ledger_tools(app.state.ledger, run.project_id, run_id=run.id)
    handler = next(item.handler for item in tools if item.name == "record_evidence")
    result = await handler({
        "kind": "user_statement", "claim": "Owner confirmed the delivery date",
        "source_title": "Owner notes", "source_type": "user", "confidence": 0.9,
        "supports_hypothesis_ids": [], "recorded_by": "main-agent",
    })
    return json.loads(result["content"][0]["text"])


def frames(response):
    parsed = []
    for block in response.text.replace("\r\n", "\n").split("\n\n"):
        frame = {}
        for line in block.splitlines():
            if line.startswith(("event:", "id:", "data:")):
                key, value = line.split(":", 1)
                frame[key] = json.loads(value) if key == "data" else value.strip()
        if "event" in frame:
            parsed.append(frame)
    return parsed


def assert_end(items, run, sequence):
    assert items[-1] == {
        "event": STREAM_END_EVENT,
        "data": {"run_id": run.id, "project_id": run.project_id, "last_sequence": sequence},
    }


@pytest.mark.asyncio
async def test_production_sdk_options_inject_projector_into_native_mcp_handler(tmp_path):
    app = create_app(Settings(
        _env_file=None, data_dir=tmp_path, ANTHROPIC_API_KEY="", ANTHROPIC_AUTH_TOKEN="",
    ))
    assert app.state.runtime.project_events is app.state.project_events
    assert app.state.runs.project_events is app.state.project_events
    ledger = app.state.ledger
    project = ledger.create_project(ProjectCreate(name="Native MCP", objective="Observe commits"))
    run = ledger.create_run(project.id, "Local tool request")
    options = app.state.runtime._build_options(run)
    server = options.mcp_servers[LEDGER_MCP_SERVER_NAME]["instance"]
    response = await server.request_handlers[CallToolRequest](CallToolRequest(
        params=CallToolRequestParams(name="record_decision", arguments={
            "question": "Proceed?", "outcome": "Run a small test", "decision_type": "test",
            "rationale": "Synthetic fixture", "evidence_ids": [], "hypothesis_ids": [],
            "assumptions": [], "conditions": [], "next_actions": [], "made_by": "main-agent",
        }),
    ))
    assert response.root.isError is False
    decision = json.loads(response.root.content[0].text)
    events = ledger.list_events(run.id)
    assert len(events) == 1
    assert events[0].type == EventType.DECISION_RECORDED
    assert events[0].data["resource_id"] == decision["id"]


@pytest.mark.asyncio
async def test_terminal_replay_repairs_commits_and_acknowledges_exact_tail(tmp_path):
    app, project = setup(tmp_path)
    run = app.state.ledger.create_run(project.id, "Historical run")
    app.state.ledger.update_run(run.id, status=RunStatus.COMPLETED)
    terminal = await app.state.events.publish(run.id, EventType.RUN_COMPLETED)
    evidence = await commit_evidence(app, run)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        protocol = (await client.get("/api/meta/protocol")).json()
        assert protocol["stream_end_event"] == STREAM_END_EVENT
        assert STREAM_END_EVENT not in protocol["event_types"]
        assert EventType.EVIDENCE_ADDED in protocol["event_types"]
        assert EventType.DECISION_RECORDED in protocol["event_types"]
        replay = await client.get(f"/api/runs/{run.id}/events")
        assert replay.status_code == 200
        items = frames(replay)
        assert [item["event"] for item in items] == [
            EventType.RUN_COMPLETED, EventType.EVIDENCE_ADDED, STREAM_END_EVENT,
        ]
        assert items[1]["data"]["data"]["resource_id"] == evidence["id"]
        assert_end(items, run, 2)
        for cursor in (terminal.id, "1", f"{run.id}:2"):
            resumed = frames(await client.get(
                f"/api/runs/{run.id}/events", headers={"Last-Event-ID": cursor},
            ))
            assert_end(resumed, run, 2)
            expected = [] if cursor.endswith(":2") else [f"{run.id}:2"]
            assert [item["id"] for item in resumed if "id" in item] == expected
    assert len(app.state.ledger.list_events(run.id)) == 2
    assert app.state.ledger.get_run_summary(run.id).last_sequence == 2


@pytest.mark.asyncio
async def test_cleanup_after_terminal_status_is_drained_before_stream_end(tmp_path, monkeypatch):
    app, project = setup(tmp_path)
    entered, release, subscribed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    capture = app.state.runs._capture_artifacts

    async def slow_cleanup(run_id):
        assert app.state.ledger.get_run(run_id).status == RunStatus.COMPLETED
        entered.set()
        await release.wait()
        workspace = app.state.ledger.workspace_for_project(project.id)
        (workspace / "final-report.md").write_text("Final artifact", encoding="utf-8")
        await capture(run_id)
        await commit_evidence(app, app.state.ledger.get_run(run_id))

    original_stream = app.state.events.stream

    async def observed_stream(*args, **kwargs):
        subscribed.set()
        async for item in original_stream(*args, **kwargs):
            yield item

    monkeypatch.setattr(app.state.runs, "_capture_artifacts", slow_cleanup)
    monkeypatch.setattr(app.state.events, "stream", observed_stream)
    run = await app.state.runs.create_and_start(project.id, "Create report")
    await asyncio.wait_for(entered.wait(), 2)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        request = asyncio.create_task(client.get(f"/api/runs/{run.id}/events"))
        try:
            await asyncio.wait_for(subscribed.wait(), 2)
            assert app.state.runs.is_executing(run.id)
            assert not request.done()
            release.set()
            response = await asyncio.wait_for(request, 3)
        finally:
            release.set()
            await app.state.runs.stop()
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)
    items = frames(response)
    types = [item["event"] for item in items]
    assert types.index(EventType.RUN_COMPLETED) < types.index(EventType.ARTIFACT_REGISTERED)
    assert types.index(EventType.ARTIFACT_REGISTERED) < types.index(EventType.EVIDENCE_ADDED)
    assert not app.state.runs.is_executing(run.id)
    assert_end(items, run, app.state.ledger.get_run_summary(run.id).last_sequence)


@pytest.mark.asyncio
@pytest.mark.parametrize("fails_on", ["initial", "finish"])
async def test_failed_reconciliation_never_claims_complete_and_reconnect_repairs(
    tmp_path, monkeypatch, fails_on,
):
    app, project = setup(tmp_path)
    run = app.state.ledger.create_run(project.id, "Tail repair")
    app.state.ledger.update_run(run.id, status=RunStatus.COMPLETED)
    await app.state.events.publish(run.id, EventType.RUN_COMPLETED)
    projector = app.state.project_events
    reconcile = projector.reconcile_run
    calls = 0

    async def fail_once(run_id):
        nonlocal calls
        calls += 1
        if calls == (1 if fails_on == "initial" else 2):
            await commit_evidence(app, run)
            return False
        return await reconcile(run_id)

    monkeypatch.setattr(projector, "reconcile_run", fail_once)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        failed = await client.get(f"/api/runs/{run.id}/events")
        assert failed.status_code == (503 if fails_on == "initial" else 200)
        assert STREAM_END_EVENT not in failed.text
        assert EventType.EVIDENCE_ADDED not in failed.text
        repaired = await client.get(f"/api/runs/{run.id}/events")
        items = frames(repaired)
        assert any(item["event"] == EventType.EVIDENCE_ADDED for item in items)
        assert_end(items, run, 2)


@pytest.mark.asyncio
async def test_finish_reconciliation_appends_tail_before_eof(tmp_path, monkeypatch):
    app, project = setup(tmp_path)
    run = app.state.ledger.create_run(project.id, "Reconcile tail")
    app.state.ledger.update_run(run.id, status=RunStatus.COMPLETED)
    await app.state.events.publish(run.id, EventType.RUN_COMPLETED)
    original = app.state.project_events.reconcile_run
    calls = 0

    async def commit_before_last_reconciliation(run_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            await commit_evidence(app, run)
        return await original(run_id)

    monkeypatch.setattr(
        app.state.project_events, "reconcile_run", commit_before_last_reconciliation,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        items = frames(await client.get(f"/api/runs/{run.id}/events"))
    assert [item["id"] for item in items if "id" in item] == [f"{run.id}:1", f"{run.id}:2"]
    assert_end(items, run, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["another_run:0", "-1", "garbage", "1.0", "100", " 1", "١"])
async def test_invalid_or_foreign_cursor_is_rejected(tmp_path, cursor):
    app, project = setup(tmp_path)
    run = app.state.ledger.create_run(project.id, "Cursor")
    app.state.ledger.update_run(run.id, status=RunStatus.COMPLETED)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        # HTTP field values must be ASCII; a Unicode numeric cursor is checked
        # directly at the parser boundary instead of constructing an invalid HTTP request.
        if not cursor.isascii():
            from fastapi import HTTPException

            from ophira_backend.main import _parse_last_event_id

            with pytest.raises(HTTPException) as caught:
                _parse_last_event_id(cursor, run.id)
            assert caught.value.status_code == 400
        else:
            response = await client.get(
                f"/api/runs/{run.id}/events", headers={"Last-Event-ID": cursor},
            )
            assert response.status_code == 400
            assert STREAM_END_EVENT not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_execution_finally_repairs_committed_records_for_every_outcome(tmp_path, outcome):
    app, project = setup(tmp_path)
    run = app.state.ledger.create_run(project.id, "Partial commit")

    class PartialRuntime(OfflineRuntime):
        async def run(self, current):
            await commit_evidence(app, current)
            if outcome == "failure":
                raise RuntimeError("offline failure after commit")
            if outcome == "cancel":
                raise asyncio.CancelledError

    app.state.runs.runtime = PartialRuntime()
    await app.state.runs._execute(run.id)
    expected = {"success": RunStatus.COMPLETED, "failure": RunStatus.FAILED,
                "cancel": RunStatus.CANCELLED}[outcome]
    assert app.state.ledger.get_run(run.id).status == expected
    assert sum(item.type == EventType.EVIDENCE_ADDED
               for item in app.state.ledger.list_events(run.id)) == 1
    await app.state.runs.recover_interrupted()
    assert sum(item.type == EventType.EVIDENCE_ADDED
               for item in app.state.ledger.list_events(run.id)) == 1


@pytest.mark.asyncio
async def test_startup_repairs_historical_terminal_run_without_dispatching_model(tmp_path):
    app, project = setup(tmp_path)
    run = app.state.ledger.create_run(project.id, "Previously complete")
    original = app.state.ledger.update_run(run.id, status=RunStatus.COMPLETED)
    await commit_evidence(app, run)
    restarted, _ = setup(tmp_path)
    await restarted.state.runs.recover_interrupted()
    assert restarted.state.ledger.get_run(run.id) == original
    assert [item.type for item in restarted.state.ledger.list_events(run.id)] == [
        EventType.EVIDENCE_ADDED,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("access_change", ["revoked", "assertion_expired"])
async def test_stream_rechecks_membership_before_yielding_tail_or_watermark(
    tmp_path, monkeypatch, access_change,
):
    app = create_app(trusted_settings(tmp_path), runtime=OfflineRuntime())
    owner = principal("owner@example.com", PrincipalRole.OWNER)
    reader_id = "reader@example.com"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test",
    ) as client:
        created = await request_as(
            client, "POST", "/api/projects", owner.principal_id, owner.role,
            json={"name": "Private", "objective": "Respect revocation"},
        )
        project_id = created.json()["id"]
        app.state.project_access.grant(
            project_id, owner, principal_id=reader_id, role=PrincipalRole.VIEWER,
        )
        run = app.state.ledger.create_run(project_id, "Private run", principal=owner)
        app.state.ledger.update_run(run.id, status=RunStatus.COMPLETED)
        await app.state.events.publish(run.id, EventType.RUN_COMPLETED)
        await commit_evidence(app, run)
        original = app.state.events.stream

        async def revoke_after_first(*args, **kwargs):
            async for item in original(*args, **kwargs):
                yield item
                if access_change == "revoked":
                    app.state.project_access.revoke(project_id, owner, principal_id=reader_id)
                else:
                    monkeypatch.setattr(
                        app.state.project_access, "_clock",
                        lambda: owner.assertion_expires_at + timedelta(hours=1),
                    )

        monkeypatch.setattr(app.state.events, "stream", revoke_after_first)
        response = await request_as(
            client, "GET", f"/api/runs/{run.id}/events", reader_id, PrincipalRole.VIEWER,
        )
        assert response.status_code == 200
        assert [item["event"] for item in frames(response)] == [EventType.RUN_COMPLETED]
        if access_change == "revoked":
            denied = await request_as(
                client, "GET", f"/api/runs/{run.id}/events", reader_id, PrincipalRole.VIEWER,
            )
            assert denied.status_code == 403

import asyncio
from pathlib import Path

import pytest

from ophira_backend.constants import (
    TOOL_ACT_ON_PAGE,
    TOOL_SNAPSHOT_PAGE,
    EventType,
    PermissionDecision,
    RunStatus,
)
from ophira_backend.events import EventPublisher
from ophira_backend.grants import PermissionGrantStore
from ophira_backend.models import (
    PermissionResolution,
    ProjectCreate,
    Schedule,
    SchedulePermissionRule,
)
from ophira_backend.permissions import PermissionBroker, PermissionNotFoundError
from ophira_backend.storage import FileLedger


async def wait_for_pending(broker: PermissionBroker):
    for _ in range(200):
        views = broker.list_pending()
        if views:
            return views[0]
        await asyncio.sleep(0.01)
    raise AssertionError("permission request did not become pending")


@pytest.mark.asyncio
async def test_late_permission_resolution_cannot_change_a_denial_or_create_a_grant(tmp_path):
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Late approval", objective="Inspect"))
    run = ledger.create_run(project.id, "Start")
    grants = PermissionGrantStore(tmp_path / "grants")
    broker = PermissionBroker(ledger, EventPublisher(ledger), timeout_seconds=30, grants=grants)
    pending = asyncio.create_task(broker.request(
        run.id, "Write", {"file_path": "report.txt"}, agent_id=None, context={},
    ))
    view = await wait_for_pending(broker)
    try:
        await broker.resolve(run.id, view.id, PermissionResolution(
            decision=PermissionDecision.DENY,
        ))
        with pytest.raises(PermissionNotFoundError):
            await broker.resolve(run.id, view.id, PermissionResolution(
                decision=PermissionDecision.ALLOW_PERSISTENT,
            ))
        assert (await pending).decision == PermissionDecision.DENY
        assert grants.match("Write", {"file_path": "report.txt"}, project_id=project.id) is None
        assert len([event for event in ledger.list_events(run.id)
                    if event.type == EventType.PERMISSION_RESOLVED.value]) == 1
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_permission_round_trip_and_run_scope_memory(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")
    ledger.update_run(run.id, status=RunStatus.RUNNING)
    events = EventPublisher(ledger)
    broker = PermissionBroker(ledger, events, timeout_seconds=30)

    pending = asyncio.create_task(
        broker.request(
            run.id,
            "ExternalWrite",
            {"path": "reports"},
            agent_id="agent-1",
            context={"title": "Write report"},
        )
    )
    await wait_for_pending(broker)
    requested = [
        event
        for event in ledger.list_events(run.id)
        if event.type == EventType.PERMISSION_REQUESTED.value
    ]
    assert requested
    request_id = requested[-1].data["request_id"]
    await broker.resolve(
        run.id,
        request_id,
        PermissionResolution(decision=PermissionDecision.ALLOW_RUN),
    )
    resolution = await pending
    assert resolution.decision == PermissionDecision.ALLOW_RUN
    assert ledger.get_run(run.id).status == RunStatus.RUNNING

    remembered = await broker.request(
        run.id,
        "ExternalWrite",
        {"path": "reports/second.json"},
        agent_id="agent-1",
        context={},
    )
    assert remembered.decision == PermissionDecision.ALLOW_RUN

    outside_scope = asyncio.create_task(
        broker.request(
            run.id,
            "ExternalWrite",
            {"path": "reports-private/secret.json"},
            agent_id="agent-1",
            context={},
        )
    )
    view = await wait_for_pending(broker)
    assert view.scope == "path:reports-private/secret.json"
    outside_scope.cancel()
    await asyncio.gather(outside_scope, return_exceptions=True)


@pytest.mark.asyncio
async def test_pending_view_redacts_input_and_exposes_safe_scope_flags(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")
    broker = PermissionBroker(ledger, EventPublisher(ledger), timeout_seconds=30)

    pending = asyncio.create_task(
        broker.request(
            run.id,
            "WebFetch",
            {"url": "https://Example.COM:443/path?token=secret", "api_key": "canary"},
            agent_id=None,
            context={},
        )
    )
    view = await wait_for_pending(broker)

    assert view.tool_input["api_key"] == "[REDACTED]"
    assert view.tool_input["url"] == "https://Example.COM:443/path?token=[REDACTED]"
    assert view.scope == "url:https://example.com"
    assert view.destructive is False
    assert view.persistable is True
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize(
    "decision",
    [PermissionDecision.ALLOW_RUN, PermissionDecision.ALLOW_PERSISTENT],
)
@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("AskUserQuestion", {"questions": []}),
        ("Delete", {"path": "report.md"}),
        ("Bash", {"command": "echo ok"}),
        (TOOL_ACT_ON_PAGE, {"url": "https://example.com", "action": "click"}),
        (
            TOOL_SNAPSHOT_PAGE,
            {"url": "https://example.com", "use_connected_browser": True},
        ),
    ],
)
@pytest.mark.asyncio
async def test_per_invocation_actions_cannot_be_remembered(
    tmp_path: Path,
    tool_name: str,
    tool_input: dict,
    decision: PermissionDecision,
) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")
    grants = PermissionGrantStore(tmp_path / "grants")
    broker = PermissionBroker(
        ledger,
        EventPublisher(ledger),
        timeout_seconds=30,
        grants=grants,
    )

    pending = asyncio.create_task(
        broker.request(run.id, tool_name, tool_input, agent_id=None, context={})
    )
    view = await wait_for_pending(broker)
    assert view.persistable is False
    await broker.resolve(
        run.id,
        view.id,
        PermissionResolution(decision=decision),
    )

    result = await pending
    assert result.decision == PermissionDecision.ALLOW_ONCE
    assert broker._run_allowed_scopes == {}
    assert grants.list() == []


@pytest.mark.asyncio
async def test_persistent_url_scope_does_not_cross_origin_prefix(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    run = ledger.create_run(project.id, "Start")
    grants = PermissionGrantStore(tmp_path / "grants")
    broker = PermissionBroker(
        ledger,
        EventPublisher(ledger),
        timeout_seconds=30,
        grants=grants,
    )

    first = asyncio.create_task(
        broker.request(
            run.id,
            "WebFetch",
            {"url": "HTTPS://Example.COM.:443/start"},
            agent_id=None,
            context={},
        )
    )
    view = await wait_for_pending(broker)
    await broker.resolve(
        run.id,
        view.id,
        PermissionResolution(decision=PermissionDecision.ALLOW_PERSISTENT),
    )
    assert (await first).decision == PermissionDecision.ALLOW_PERSISTENT
    assert grants.list()[0].path_prefix == "url:https://example.com"

    same_origin = await broker.request(
        run.id,
        "WebFetch",
        {"url": "https://example.com/other"},
        agent_id=None,
        context={},
    )
    assert same_origin.decision == PermissionDecision.ALLOW_PERSISTENT

    lookalike = asyncio.create_task(
        broker.request(
            run.id,
            "WebFetch",
            {"url": "https://example.com.evil.test/"},
            agent_id=None,
            context={},
        )
    )
    evil_view = await wait_for_pending(broker)
    assert evil_view.scope == "url:https://example.com.evil.test"
    lookalike.cancel()
    await asyncio.gather(lookalike, return_exceptions=True)


@pytest.mark.asyncio
async def test_scheduled_run_enforces_ceiling_without_waiting_or_reusing_grants(
    tmp_path: Path,
) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    schedule = ledger.create_schedule(
        Schedule(
            project_id=project.id,
            prompt="Export report",
            cron="0 8 * * *",
            permission_ceiling=[
                SchedulePermissionRule(tool_name="Write", scope="path:."),
                SchedulePermissionRule(
                    tool_name="ExternalFetch", scope="url:https://example.com"
                ),
                SchedulePermissionRule(
                    tool_name=TOOL_SNAPSHOT_PAGE, scope="url:https://example.com"
                ),
            ],
        )
    )
    run = ledger.create_run(project.id, "Start", schedule_id=schedule.id)
    ledger.update_run(run.id, status=RunStatus.RUNNING)
    grants = PermissionGrantStore(tmp_path / "grants")
    grants.record(
        tool_name="Write",
        tool_input={"file_path": str(tmp_path / "outside" / "granted.txt")},
        project_id=project.id,
    )
    broker = PermissionBroker(
        ledger,
        EventPublisher(ledger),
        timeout_seconds=30,
        grants=grants,
    )
    workspace = ledger.workspace_for_project(project.id).resolve()

    for file_path in ("reports/result.md", str(workspace / "reports" / "absolute.md")):
        allowed = await broker.request(
            run.id,
            "Write",
            {"file_path": file_path},
            agent_id=None,
            context={},
        )
        assert allowed.decision == PermissionDecision.ALLOW_ONCE

    same_origin = await broker.request(
        run.id,
        "ExternalFetch",
        {"url": "https://example.com/report?id=1"},
        agent_id=None,
        context={},
    )
    assert same_origin.decision == PermissionDecision.ALLOW_ONCE

    browser_snapshot = await broker.request(
        run.id,
        TOOL_SNAPSHOT_PAGE,
        {"url": "https://example.com/report", "use_connected_browser": False},
        agent_id=None,
        context={},
    )
    assert browser_snapshot.decision == PermissionDecision.ALLOW_ONCE

    denied_requests = [
        ("Write", {"file_path": str(workspace.parent / "sibling" / "secret.txt")}),
        ("Edit", {"file_path": "reports/result.md"}),
        ("Delete", {"path": "reports/result.md"}),
        ("AskUserQuestion", {"questions": []}),
        ("ExternalFetch", {"url": "https://example.com.evil.test/report"}),
        (
            TOOL_SNAPSHOT_PAGE,
            {"url": "https://example.com/report", "use_connected_browser": True},
        ),
    ]
    for tool_name, tool_input in denied_requests:
        denied = await broker.request(
            run.id,
            tool_name,
            tool_input,
            agent_id=None,
            context={},
        )
        assert denied.decision == PermissionDecision.DENY

    assert broker.list_pending() == []
    assert ledger.get_run(run.id).status == RunStatus.RUNNING
    assert all(
        event.type != EventType.PERMISSION_REQUESTED.value
        for event in ledger.list_events(run.id)
    )


@pytest.mark.asyncio
async def test_scheduled_workspace_ceiling_rejects_symlink_escape_and_empty_ceiling(
    tmp_path: Path,
) -> None:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Kettle", objective="Validate"))
    workspace = ledger.workspace_for_project(project.id).resolve()
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (workspace / "escape").symlink_to(sibling, target_is_directory=True)
    schedule = ledger.create_schedule(
        Schedule(
            project_id=project.id,
            prompt="Export report",
            cron="0 8 * * *",
            permission_ceiling=[SchedulePermissionRule(tool_name="Write", scope="path:.")],
        )
    )
    run = ledger.create_run(project.id, "Start", schedule_id=schedule.id)
    empty_schedule = ledger.create_schedule(
        Schedule(project_id=project.id, prompt="No writes", cron="0 9 * * *")
    )
    empty_run = ledger.create_run(project.id, "Start", schedule_id=empty_schedule.id)
    broker = PermissionBroker(ledger, EventPublisher(ledger), timeout_seconds=30)

    escaped = await broker.request(
        run.id,
        "Write",
        {"file_path": "escape/secret.txt"},
        agent_id=None,
        context={},
    )
    no_ceiling = await broker.request(
        empty_run.id,
        "Write",
        {"file_path": "report.txt"},
        agent_id=None,
        context={},
    )

    assert escaped.decision == PermissionDecision.DENY
    assert no_ceiling.decision == PermissionDecision.DENY
    assert broker.list_pending() == []

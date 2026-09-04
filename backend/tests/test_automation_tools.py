"""ophira_automation MCP tools: schedule/intent commitments, throttled delivery."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ophira_backend.automation import IntentStore, NotificationStore
from ophira_backend.automation_tools import build_automation_tools
from ophira_backend.config import Settings
from ophira_backend.constants import AgentTemplate, PrincipalRole, RunStatus
from ophira_backend.main import create_app
from ophira_backend.models import (
    AgentCreate,
    PrincipalContext,
    ProjectCreate,
    ProviderProfileUpsert,
    RunRecord,
)
from ophira_backend.providers import successful_preflight
from ophira_backend.storage import FileLedger


def handler(tools, name: str):
    return next(item for item in tools if item.name == name).handler


def parse(result: dict) -> dict | list:
    return json.loads(result["content"][0]["text"])


def build(tmp_path: Path):
    ledger = FileLedger(tmp_path / "data")
    project = ledger.create_project(ProjectCreate(name="Watch", objective="Monitor"))
    run = ledger.create_run(
        project.id,
        "Create approved automation",
        agent_id="agent_main",
        provider_profile_id="provider_main",
    )
    intents = IntentStore(tmp_path / "intents")
    notifications = NotificationStore(tmp_path / "notifications")
    tools = build_automation_tools(
        ledger, intents, notifications, project.id,
        run_id=run.id, schedule_id="schedule_ctx",
    )
    return ledger, project, intents, notifications, tools


def trusted_principal(principal_id: str, role: PrincipalRole) -> PrincipalContext:
    now = datetime.now(UTC)
    return PrincipalContext(
        tenant_id="tenant_acme",
        principal_id=principal_id,
        role=role,
        session_id=f"principal_session_{principal_id}",
        assertion_issued_at=now,
        assertion_expires_at=now + timedelta(minutes=1),
        assertion_nonce=f"nonce_{principal_id}_00000000000000000000",
    )


@pytest.mark.asyncio
async def test_create_schedule_is_validated_and_idempotent(tmp_path: Path) -> None:
    ledger, _project, _intents, _notifications, tools = build(tmp_path)

    created = parse(await handler(tools, "create_schedule")(
        {"prompt": "每天巡检竞品价格", "cron": "0 8 * * *", "timezone": "UTC"}
    ))
    assert created["reused_existing"] is False
    assert created["schedule"]["next_fire_at"]

    repeated = parse(await handler(tools, "create_schedule")(
        {"prompt": "每天巡检竞品价格", "cron": "0 8 * * *", "timezone": "UTC"}
    ))
    assert repeated["reused_existing"] is True
    assert repeated["schedule"]["id"] == created["schedule"]["id"]
    assert len(ledger.list_schedules()) == 1

    invalid = await handler(tools, "create_schedule")(
        {"prompt": "bad", "cron": "not a cron", "timezone": "UTC"}
    )
    assert invalid["is_error"] is True


@pytest.mark.asyncio
async def test_agent_created_automation_inherits_and_scopes_durable_run_principal(
    tmp_path: Path,
) -> None:
    ledger = FileLedger(tmp_path / "data")
    owner = trusted_principal("owner", PrincipalRole.OWNER)
    member = trusted_principal("member", PrincipalRole.MEMBER)
    project = ledger.create_project(
        ProjectCreate(name="Trusted watch", objective="Monitor"),
        principal=owner,
    )
    member_run = ledger.create_run(project.id, "Create automation", principal=member)
    intents = IntentStore(tmp_path / "intents")
    notifications = NotificationStore(tmp_path / "notifications")
    member_tools = build_automation_tools(
        ledger,
        intents,
        notifications,
        project.id,
        run_id=member_run.id,
    )

    schedule = parse(
        await handler(member_tools, "create_schedule")(
            {"prompt": "Check prices", "cron": "0 8 * * *", "timezone": "UTC"}
        )
    )["schedule"]
    intent = parse(
        await handler(member_tools, "create_standing_intent")(
            {"intent": "Tell me when price changes"}
        )
    )["intent"]
    expected = {
        "tenant_id": "tenant_acme",
        "principal_id": "member",
        "role": "member",
        "session_id": "principal_session_member",
    }
    assert schedule["principal_binding"] == expected
    assert intent["principal_binding"] == expected

    owner_run = ledger.create_run(project.id, "Create own automation", principal=owner)
    owner_tools = build_automation_tools(
        ledger,
        intents,
        notifications,
        project.id,
        run_id=owner_run.id,
    )
    owner_schedule = parse(
        await handler(owner_tools, "create_schedule")(
            {"prompt": "Check prices", "cron": "0 8 * * *", "timezone": "UTC"}
        )
    )
    assert owner_schedule["reused_existing"] is False
    assert owner_schedule["schedule"]["id"] != schedule["id"]
    assert owner_schedule["schedule"]["principal_binding"]["principal_id"] == "owner"

    unbound_tools = build_automation_tools(
        ledger,
        intents,
        notifications,
        project.id,
    )
    denied = await handler(unbound_tools, "create_schedule")(
        {"prompt": "No authority", "cron": "0 8 * * *", "timezone": "UTC"}
    )
    assert denied["is_error"] is True
    assert "source run identity" in parse(denied)["error"]


@pytest.mark.asyncio
async def test_pause_schedule_guards_project_ownership(tmp_path: Path) -> None:
    ledger, _project, _intents, _notifications, tools = build(tmp_path)
    created = parse(await handler(tools, "create_schedule")(
        {"prompt": "p", "cron": "0 8 * * *", "timezone": "UTC"}
    ))["schedule"]

    paused = parse(await handler(tools, "pause_schedule")({"schedule_id": created["id"]}))
    assert paused["enabled"] is False

    other = ledger.create_project(ProjectCreate(name="Other", objective="x"))
    foreign_tools = build_automation_tools(
        ledger, IntentStore(tmp_path / "i2"), NotificationStore(tmp_path / "n2"), other.id
    )
    denied = await handler(foreign_tools, "pause_schedule")({"schedule_id": created["id"]})
    assert denied["is_error"] is True


@pytest.mark.asyncio
async def test_intent_lifecycle_with_throttled_inbox_delivery(tmp_path: Path) -> None:
    ledger, project, _intents, notifications, tools = build(tmp_path)

    created = parse(await handler(tools, "create_standing_intent")(
        {"intent": "BlendGo 价格变化超过 5% 时告诉我"}
    ))
    assert created["reused_existing"] is False
    intent_id = created["intent"]["id"]
    assert created["intent"]["cooldown_hours"] == 24
    assert created["intent"]["max_triggers"] == 3
    assert created["intent"]["agent_id"] == "agent_main"
    assert created["intent"]["provider_profile_id"] == "provider_main"
    assert created["intent"]["authorization_source"] == "user_approved_agent"
    assert created["intent"]["authorized_at"]

    reused = parse(await handler(tools, "create_standing_intent")(
        {"intent": "BlendGo 价格变化超过 5% 时告诉我"}
    ))
    assert reused["reused_existing"] is True

    listed = parse(await handler(tools, "list_standing_intents")({}))
    assert [item["id"] for item in listed] == [intent_id]

    fired = parse(await handler(tools, "trigger_standing_intent")(
        {"intent_id": intent_id, "summary": "BlendGo 从 27.9 降到 24.9 EUR（-10.8%）"}
    ))
    assert fired["outcome"] == "triggered"
    inbox = notifications.list()
    assert len(inbox) == 1
    assert inbox[0].intent_id == intent_id
    assert inbox[0].run_id is not None
    assert ledger.get_run(inbox[0].run_id).project_id == project.id
    assert inbox[0].schedule_id == "schedule_ctx"
    assert inbox[0].project_id == project.id

    suppressed = parse(await handler(tools, "trigger_standing_intent")(
        {"intent_id": intent_id, "summary": "再次变化"}
    ))
    assert suppressed["outcome"] == "cooldown"
    assert suppressed["delivered_notification_id"] is None
    assert len(notifications.list()) == 1  # anti-nag budget held


@pytest.mark.asyncio
async def test_intent_tool_cannot_invent_user_approval_without_source_run(
    tmp_path: Path,
) -> None:
    ledger = FileLedger(tmp_path / "data")
    project = ledger.create_project(ProjectCreate(name="Watch", objective="Monitor"))
    intents = IntentStore(tmp_path / "intents")
    tools = build_automation_tools(
        ledger,
        intents,
        NotificationStore(tmp_path / "notifications"),
        project.id,
        run_id="run_missing",
    )

    result = await handler(tools, "create_standing_intent")({"intent": "watch this"})

    assert result["is_error"] is True
    assert intents.list() == []


@pytest.mark.asyncio
async def test_send_notification_lands_with_run_linkage(tmp_path: Path) -> None:
    ledger, project, _intents, notifications, tools = build(tmp_path)

    sent = parse(await handler(tools, "send_notification")(
        {"title": "竞品价格变化", "body": "详情见报告", "severity": "warning"}
    ))
    assert sent["severity"] == "warning"
    stored = notifications.list()[0]
    assert stored.project_id == project.id
    assert stored.run_id is not None
    assert ledger.get_run(stored.run_id).project_id == project.id


class FailingRuntime:
    async def run(self, run: RunRecord) -> None:
        raise RuntimeError("401 unauthorized: credential revoked")

    async def cancel(self, run_id: str) -> None:  # pragma: no cover
        return None


@pytest.mark.asyncio
async def test_failed_scheduled_run_lands_in_the_inbox(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path), runtime=FailingRuntime())
    ledger = app.state.ledger
    project = ledger.create_project(ProjectCreate(name="Sched", objective="x"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/schedules", json={
            "project_id": project.id, "prompt": "heartbeat", "cron": "0 8 * * *",
            "timezone": "UTC",
        })
        schedule_id = created.json()["id"]
        run = await client.post(f"/api/schedules/{schedule_id}/run")
        run_id = run.json()["id"]

        for _ in range(100):
            status = (await client.get(f"/api/runs/{run_id}")).json()["status"]
            if status == "failed":
                break
            await asyncio.sleep(0.01)
        assert status == "failed"

        inbox = (await client.get("/api/notifications", params={"unread": True})).json()
        assert len(inbox) == 1
        assert inbox[0]["severity"] == "error"
        assert inbox[0]["schedule_id"] == schedule_id
        assert "AUTH_REVOKED_OR_INVALID" in inbox[0]["title"]
        assert ledger.get_schedule(schedule_id).enabled is False

        marked = await client.post(f"/api/notifications/{inbox[0]['id']}/read")
        assert marked.json()["read"] is True
        assert (await client.get("/api/notifications", params={"unread": True})).json() == []


class TransientFailingRuntime:
    async def run(self, run: RunRecord) -> None:
        raise RuntimeError("connection timed out")

    async def cancel(self, run_id: str) -> None:  # pragma: no cover
        return None


@pytest.mark.asyncio
async def test_transient_scheduled_run_failure_does_not_pause_schedule(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path), runtime=TransientFailingRuntime())
    ledger = app.state.ledger
    project = ledger.create_project(ProjectCreate(name="Sched", objective="x"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/api/schedules",
            json={
                "project_id": project.id,
                "prompt": "heartbeat",
                "cron": "0 8 * * *",
                "timezone": "UTC",
            },
        )
        schedule_id = created.json()["id"]
        response = await client.post(f"/api/schedules/{schedule_id}/run")
        run_id = response.json()["id"]

        for _ in range(100):
            status = ledger.get_run(run_id).status
            if status == RunStatus.FAILED:
                break
            await asyncio.sleep(0.01)

    assert status == RunStatus.FAILED
    assert ledger.get_schedule(schedule_id).enabled is True
    notices = app.state.notifications.list()
    assert len(notices) == 1
    assert notices[0].schedule_id == schedule_id
    assert "已暂停" not in notices[0].title


@pytest.mark.asyncio
async def test_intent_api_create_list_pause(tmp_path: Path) -> None:
    class IdleRuntime:
        async def run(self, run) -> None:
            return None

        async def cancel(self, run_id: str) -> None:
            return None

    app = create_app(Settings(data_dir=tmp_path), runtime=IdleRuntime())
    project = app.state.ledger.create_project(ProjectCreate(name="I", objective="x"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.post("/api/intents", json={
            "project_id": "project_missing", "intent": "x",
        })
        assert missing.status_code == 404

        missing_agent = await client.post(
            "/api/intents",
            json={
                "project_id": project.id,
                "agent_id": "agent_missing",
                "intent": "x",
            },
        )
        assert missing_agent.status_code == 404

        missing_provider = await client.post(
            "/api/intents",
            json={
                "project_id": project.id,
                "provider_profile_id": "provider_missing",
                "intent": "x",
            },
        )
        assert missing_provider.status_code == 404
        missing_check = await client.post("/api/intents/intent_missing/check")
        assert missing_check.status_code == 404

        created = await client.post("/api/intents", json={
            "project_id": project.id,
            "intent": "库存低于 100 时告诉我",
            "cooldown_hours": 12,
        })
        assert created.status_code == 201
        intent_id = created.json()["id"]
        assert created.json()["cooldown_hours"] == 12
        assert created.json()["authorization_source"] == "user_api"
        assert created.json()["authorized_at"]

        listing = await client.get("/api/intents", params={"project_id": project.id})
        assert [item["id"] for item in listing.json()] == [intent_id]

        paused = await client.post(f"/api/intents/{intent_id}/pause")
        assert paused.json()["enabled"] is False
        resumed = await client.post(f"/api/intents/{intent_id}/resume")
        assert resumed.json()["enabled"] is True
        assert resumed.json()["authorization_source"] == "user_api"


@pytest.mark.asyncio
async def test_intent_check_api_dispatches_bound_budgeted_main_agent_run(
    tmp_path: Path,
) -> None:
    class IdleRuntime:
        def __init__(self) -> None:
            self.seen: list[RunRecord] = []

        async def run(self, run: RunRecord) -> None:
            self.seen.append(run)

        async def cancel(self, run_id: str) -> None:
            return None

    runtime = IdleRuntime()
    app = create_app(
        Settings(
            data_dir=tmp_path,
            claude_model="offline-test-model",
            ANTHROPIC_AUTH_TOKEN="offline-test-token",
        ),
        runtime=runtime,
    )
    app.state.providers.upsert(
        "environment",
        ProviderProfileUpsert(
            display_name="Offline test Provider",
            auth_env_name="ANTHROPIC_AUTH_TOKEN",
            model="offline-test-model",
            production_eligible=True,
            secret="offline-test-token",
        ),
    )
    app.state.providers.record_preflight(successful_preflight("environment"))
    project = app.state.ledger.create_project(ProjectCreate(name="I", objective="x"))
    agent = app.state.ledger.create_agent(
        AgentCreate(name="Main", template=AgentTemplate.GENERAL)
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/intents",
            json={
                "project_id": project.id,
                "agent_id": agent.id,
                "provider_profile_id": "environment",
                "intent": "库存低于 100 时告诉我",
                "per_check_budget_usd": 0.2,
                "total_budget_usd": 0.4,
            },
        )
        assert created.status_code == 201

        checked = await client.post(f"/api/intents/{created.json()['id']}/check")

    assert checked.status_code == 202
    payload = checked.json()
    assert payload["outcome"] == "started"
    assert payload["run"]["agent_id"] == agent.id
    assert payload["run"]["provider_profile_id"] == "environment"
    assert payload["run"]["mode"] == "standard"
    assert payload["run"]["standing_intent_id"] == created.json()["id"]
    assert payload["run"]["automation_claim_id"]
    assert payload["run"]["max_budget_usd"] == 0.2
    for _ in range(100):
        run = app.state.ledger.get_run(payload["run"]["id"])
        if run.status == RunStatus.COMPLETED:
            break
        await asyncio.sleep(0.005)
    assert run.status == RunStatus.COMPLETED
    await app.state.intent_watcher.tick()
    stored = app.state.intents.get(created.json()["id"])
    assert stored.active_claim_id is None
    assert stored.budget_committed_usd == 0.2
    assert len(runtime.seen) == 1
    assert app.state.notifications.list() == []


@pytest.mark.asyncio
async def test_application_lifespan_starts_and_stops_intent_watcher(tmp_path: Path) -> None:
    class IdleRuntime:
        async def run(self, run: RunRecord) -> None:
            return None

        async def cancel(self, run_id: str) -> None:
            return None

    app = create_app(Settings(data_dir=tmp_path), runtime=IdleRuntime())
    watcher = app.state.intent_watcher

    assert watcher.is_leader is False
    async with app.router.lifespan_context(app):
        assert watcher.is_leader is True
        assert watcher._task is not None
        assert watcher._task.done() is False

    assert watcher.is_leader is False
    assert watcher._task is None

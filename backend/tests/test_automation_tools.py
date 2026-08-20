"""ophira_automation MCP tools: schedule/intent commitments, throttled delivery."""

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ophira_backend.automation import IntentStore, NotificationStore
from ophira_backend.automation_tools import build_automation_tools
from ophira_backend.config import Settings
from ophira_backend.main import create_app
from ophira_backend.models import ProjectCreate, RunRecord
from ophira_backend.storage import FileLedger


def handler(tools, name: str):
    return next(item for item in tools if item.name == name).handler


def parse(result: dict) -> dict | list:
    return json.loads(result["content"][0]["text"])


def build(tmp_path: Path):
    ledger = FileLedger(tmp_path / "data")
    project = ledger.create_project(ProjectCreate(name="Watch", objective="Monitor"))
    intents = IntentStore(tmp_path / "intents")
    notifications = NotificationStore(tmp_path / "notifications")
    tools = build_automation_tools(
        ledger, intents, notifications, project.id,
        run_id="run_ctx", schedule_id="schedule_ctx",
    )
    return ledger, project, intents, notifications, tools


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
    _ledger, project, _intents, notifications, tools = build(tmp_path)

    created = parse(await handler(tools, "create_standing_intent")(
        {"intent": "BlendGo 价格变化超过 5% 时告诉我"}
    ))
    assert created["reused_existing"] is False
    intent_id = created["intent"]["id"]
    assert created["intent"]["cooldown_hours"] == 24
    assert created["intent"]["max_triggers"] == 3

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
    assert inbox[0].run_id == "run_ctx"
    assert inbox[0].schedule_id == "schedule_ctx"
    assert inbox[0].project_id == project.id

    suppressed = parse(await handler(tools, "trigger_standing_intent")(
        {"intent_id": intent_id, "summary": "再次变化"}
    ))
    assert suppressed["outcome"] == "cooldown"
    assert suppressed["delivered_notification_id"] is None
    assert len(notifications.list()) == 1  # anti-nag budget held


@pytest.mark.asyncio
async def test_send_notification_lands_with_run_linkage(tmp_path: Path) -> None:
    _ledger, project, _intents, notifications, tools = build(tmp_path)

    sent = parse(await handler(tools, "send_notification")(
        {"title": "竞品价格变化", "body": "详情见报告", "severity": "warning"}
    ))
    assert sent["severity"] == "warning"
    stored = notifications.list()[0]
    assert stored.project_id == project.id
    assert stored.run_id == "run_ctx"


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
            import asyncio

            await asyncio.sleep(0.01)
        assert status == "failed"

        inbox = (await client.get("/api/notifications", params={"unread": True})).json()
        assert len(inbox) == 1
        assert inbox[0]["severity"] == "error"
        assert inbox[0]["schedule_id"] == schedule_id
        assert "AUTH_REVOKED_OR_INVALID" in inbox[0]["title"]

        marked = await client.post(f"/api/notifications/{inbox[0]['id']}/read")
        assert marked.json()["read"] is True
        assert (await client.get("/api/notifications", params={"unread": True})).json() == []


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

        created = await client.post("/api/intents", json={
            "project_id": project.id,
            "intent": "库存低于 100 时告诉我",
            "cooldown_hours": 12,
        })
        assert created.status_code == 201
        intent_id = created.json()["id"]
        assert created.json()["cooldown_hours"] == 12

        listing = await client.get("/api/intents", params={"project_id": project.id})
        assert [item["id"] for item in listing.json()] == [intent_id]

        paused = await client.post(f"/api/intents/{intent_id}/pause")
        assert paused.json()["enabled"] is False
        resumed = await client.post(f"/api/intents/{intent_id}/resume")
        assert resumed.json()["enabled"] is True

"""Bounded Graph Inbox delta reconciliation into the durable notification queue."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from test_auth_api import request_as
from test_outlook_access import (
    CLIENT_ID,
    CLIENT_SECRET,
    OUTLOOK_SCOPE,
    graph_message,
    setup,
)
from test_outlook_subscriptions import SUBSCRIPTION_ID

from ophira_backend.constants import PrincipalRole
from ophira_backend.event_triggers import EventTriggerCreate
from ophira_backend.models import PrincipalBinding
from ophira_backend.outlook_subscriptions import OutlookNotification

DELTA_PATH = "/v1.0/me/mailFolders/inbox-id/messages/delta"
DELTA_ORIGIN = "https://graph.microsoft.com" + DELTA_PATH


def new_trigger(app, project, owner):
    return app.state.event_triggers.create(
        project.id, EventTriggerCreate(
            name="Outlook inquiry", kind="inquiry_email", prompt="Review inquiry",
            max_budget_usd=1, outlook_connector_id="outlook",
        ), principal=PrincipalBinding.from_context(owner),
    ).trigger


def item(identifier: str, received: datetime) -> dict[str, str]:
    return {"id": identifier, "receivedDateTime": received.isoformat()}


@pytest.mark.asyncio
async def test_delta_baseline_and_missed_notification_enqueue_once(tmp_path):
    app, project, owner, member = setup(tmp_path)
    trigger = new_trigger(app, project, owner)
    callback_state = ""
    graph_calls: list[str] = []
    delivered: list[str] = []
    received = datetime.now(UTC) + timedelta(seconds=2)

    def graph(request: httpx.Request) -> httpx.Response:
        nonlocal callback_state
        graph_calls.append(str(request.url))
        assert request.headers["authorization"] == "Bearer access-private"
        if request.url.path == "/v1.0/subscriptions" and request.method == "POST":
            callback_state = json.loads(request.content)["clientState"]
            return httpx.Response(201, json={
                "id": SUBSCRIPTION_ID,
                "expirationDateTime": (datetime.now(UTC) + timedelta(days=6)).isoformat(),
            })
        if request.url.path == "/v1.0/me/mailFolders/inbox":
            return httpx.Response(200, json={"id": "inbox-id"})
        if request.url.path == DELTA_PATH:
            assert 'IdType="ImmutableId"' in request.headers["prefer"]
            query = parse_qs(request.url.query.decode())
            if "$deltatoken" in query:
                assert query["$deltatoken"] == ["first"]
                return httpx.Response(200, json={
                    "value": [item("message2", received + timedelta(seconds=1))],
                    "@odata.deltaLink": DELTA_ORIGIN + "?$deltatoken=second",
                })
            assert query["changeType"] == ["created"]
            assert query["$select"] == ["id,receivedDateTime"]
            assert query["$filter"][0].startswith("receivedDateTime ge ")
            return httpx.Response(200, json={
                "value": [
                    item("old-message", received - timedelta(days=2)),
                    item("message1", received),
                ],
                "@odata.deltaLink": DELTA_ORIGIN + "?$deltatoken=first",
            })
        if request.url.path.startswith("/v1.0/me/messages/"):
            return httpx.Response(200, json=graph_message(request.url.path.rsplit("/", 1)[-1]))
        raise AssertionError(str(request.url))

    app.state.outlook.client = httpx.AsyncClient(transport=httpx.MockTransport(graph))

    async def dispatch(_trigger, event_id, _body, _payload):
        delivered.append(event_id)
        return {"status": "accepted"}

    app.state.outlook_subscriptions.dispatch = dispatch
    path = f"/api/projects/{project.id}/event-triggers/{trigger.id}/outlook-subscription"
    callback = "/api/connectors/outlook/outlook/notifications"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://events.example.com",
    ) as client:
        assert (await request_as(
            client, "POST", path, "owner", PrincipalRole.OWNER,
        )).status_code == 200
        initial = await request_as(client, "GET", path, "owner", PrincipalRole.OWNER)
        assert initial.json()["delta_ready"] is False
        denied = await request_as(
            client, "POST", path + "/sync-delta", "member", member.role,
        )
        assert denied.status_code == 403
        first = await request_as(
            client, "POST", path + "/sync-delta", "owner", PrincipalRole.OWNER,
        )
        assert first.status_code == 200, first.text
        assert first.json()["queued"] == 1
        assert first.json()["delta_ready"] is True
        assert "$deltatoken" not in first.text
        assert "old-message" not in first.text
        queue = list((tmp_path / "outlook-subscriptions" / "outlook").glob("*.json"))
        assert len(queue) == 1
        first_notice = OutlookNotification.model_validate_json(queue[0].read_text())
        assert first_notice.message_id == "message1"
        await app.state.outlook_subscriptions.process_pending()
        assert len(delivered) == 1
        notice = OutlookNotification.model_validate_json(queue[0].read_text())
        assert notice.message_id == "" and notice.payload is None
        lifecycle = {"value": [{
            "subscriptionId": SUBSCRIPTION_ID, "clientState": callback_state,
            "lifecycleEvent": "missed",
        }]}
        assert (await client.post(callback, json=lifecycle)).status_code == 202
        stale = await request_as(client, "GET", path, "owner", PrincipalRole.OWNER)
        assert stale.json()["status"] == "stale"
        second = await request_as(
            client, "POST", path + "/sync-delta", "owner", PrincipalRole.OWNER,
        )
        assert second.status_code == 200, second.text
        assert second.json()["queued"] == 1
        assert second.json()["status"] == "active"
        duplicate = {"value": [{
            "subscriptionId": SUBSCRIPTION_ID, "clientState": callback_state,
            "changeType": "created", "resourceData": {"id": "message2"},
        }]}
        assert (await client.post(callback, json=duplicate)).status_code == 202
        await app.state.outlook_subscriptions.process_pending()
        assert len(delivered) == 2
    assert all("old-message" not in call for call in graph_calls)
    assert app.state.connectors.vault.has("outlook-subscription-delta:outlook")
    app.state.connectors.upsert(
        "outlook", type="outlook", display_name="Outlook", scope=OUTLOOK_SCOPE,
        secret=json.dumps({"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}),
    )
    assert not app.state.connectors.vault.has("outlook-subscription-delta:outlook")


@pytest.mark.asyncio
async def test_delta_rejects_stale_project_enrollment(tmp_path):
    app, project, owner, _member = setup(tmp_path)
    trigger = new_trigger(app, project, owner)
    calls = 0

    def graph(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/v1.0/subscriptions"
        return httpx.Response(201, json={
            "id": SUBSCRIPTION_ID,
            "expirationDateTime": (datetime.now(UTC) + timedelta(days=6)).isoformat(),
        })

    app.state.outlook.client = httpx.AsyncClient(transport=httpx.MockTransport(graph))
    path = f"/api/projects/{project.id}/event-triggers/{trigger.id}/outlook-subscription"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://events.example.com",
    ) as client:
        assert (await request_as(
            client, "POST", path, "owner", PrincipalRole.OWNER,
        )).status_code == 200
        subscriptions = app.state.outlook_subscriptions
        enrollment = subscriptions._enrollment(trigger)
        subscriptions.ledger._atomic_write(
            subscriptions._enrollment_path(trigger),
            enrollment.model_copy(update={"subscription_id": "old-subscription"}),
        )
        stale = await request_as(
            client, "POST", path + "/sync-delta", "owner", PrincipalRole.OWNER,
        )
        assert stale.status_code == 409
        assert "OUTLOOK_DELTA_UNAVAILABLE" in stale.text
        assert calls == 1
        assert not app.state.connectors.vault.has("outlook-subscription-delta:outlook")
        assert subscriptions._watch("outlook").gap is False


@pytest.mark.asyncio
async def test_due_sync_establishes_baseline_without_admin_action(tmp_path):
    app, project, owner, _member = setup(tmp_path)
    trigger = new_trigger(app, project, owner)
    received = datetime.now(UTC) + timedelta(seconds=2)
    delta_calls = 0

    def graph(request: httpx.Request) -> httpx.Response:
        nonlocal delta_calls
        if request.url.path == "/v1.0/subscriptions":
            return httpx.Response(201, json={
                "id": SUBSCRIPTION_ID,
                "expirationDateTime": (datetime.now(UTC) + timedelta(days=6)).isoformat(),
            })
        if request.url.path == "/v1.0/me/mailFolders/inbox":
            return httpx.Response(200, json={"id": "inbox-id"})
        if request.url.path == DELTA_PATH:
            delta_calls += 1
            return httpx.Response(200, json={
                "value": [item("message1", received)],
                "@odata.deltaLink": DELTA_ORIGIN + "?$deltatoken=ready",
            })
        raise AssertionError(str(request.url))

    app.state.outlook.client = httpx.AsyncClient(transport=httpx.MockTransport(graph))
    path = f"/api/projects/{project.id}/event-triggers/{trigger.id}/outlook-subscription"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://events.example.com",
    ) as client:
        assert (await request_as(
            client, "POST", path, "owner", PrincipalRole.OWNER,
        )).status_code == 200
        await app.state.outlook_subscriptions.sync_due()
        ready = await request_as(client, "GET", path, "owner", PrincipalRole.OWNER)
        assert ready.json()["delta_ready"] is True
        assert delta_calls == 1
        await app.state.outlook_subscriptions.sync_due()
        assert delta_calls == 1
        notices = list((tmp_path / "outlook-subscriptions" / "outlook").glob("*.json"))
        assert len(notices) == 1
        app.state.connectors.vault.set("outlook-subscription-delta:outlook", "broken")
        await app.state.outlook_subscriptions.sync_due()
        stale = await request_as(client, "GET", path, "owner", PrincipalRole.OWNER)
        assert stale.json()["gap"] is True
        assert stale.json()["delta_ready"] is False


@pytest.mark.asyncio
async def test_delta_rejects_unbounded_legacy_gap_and_malicious_continuation(tmp_path):
    app, project, owner, _member = setup(tmp_path)
    trigger = new_trigger(app, project, owner)
    calls = 0

    def graph(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path == "/v1.0/subscriptions":
            return httpx.Response(201, json={
                "id": SUBSCRIPTION_ID,
                "expirationDateTime": (datetime.now(UTC) + timedelta(days=6)).isoformat(),
            })
        if request.url.path == "/v1.0/me/mailFolders/inbox":
            return httpx.Response(200, json={"id": "inbox-id"})
        if request.url.path == DELTA_PATH:
            return httpx.Response(200, json={
                "value": [],
                "@odata.deltaLink": "https://127.0.0.1/private?$deltatoken=leak",
            })
        raise AssertionError(str(request.url))

    app.state.outlook.client = httpx.AsyncClient(transport=httpx.MockTransport(graph))
    path = f"/api/projects/{project.id}/event-triggers/{trigger.id}/outlook-subscription"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://events.example.com",
    ) as client:
        assert (await request_as(
            client, "POST", path, "owner", PrincipalRole.OWNER,
        )).status_code == 200
        watch = app.state.outlook_subscriptions._watch("outlook")
        app.state.outlook_subscriptions._save(watch.model_copy(update={"delta_from": None}))
        before = calls
        await app.state.outlook_subscriptions.sync_due()
        assert app.state.outlook_subscriptions._watch("outlook").gap is True
        assert calls == before
        old_gap = await request_as(
            client, "POST", path + "/sync-delta", "owner", PrincipalRole.OWNER,
        )
        assert old_gap.status_code == 409
        assert "OUTLOOK_DELTA_UNBOUNDED" in old_gap.text
        assert calls == before
        assert (await request_as(
            client, "GET", path, "owner", PrincipalRole.OWNER,
        )).json()["gap"] is True
        app.state.outlook_subscriptions._save(watch)
        unsafe = await request_as(
            client, "POST", path + "/sync-delta", "owner", PrincipalRole.OWNER,
        )
        assert unsafe.status_code == 502
        assert "OUTLOOK_DELTA_INVALID" in unsafe.text
        assert not app.state.connectors.vault.has("outlook-subscription-delta:outlook")
        assert (await request_as(
            client, "GET", path, "owner", PrincipalRole.OWNER,
        )).json()["gap"] is True


@pytest.mark.asyncio
async def test_delta_page_failure_replays_idempotently_before_advancing_cursor(tmp_path):
    app, project, owner, _member = setup(tmp_path)
    trigger = new_trigger(app, project, owner)
    fail_page = True
    calls: list[str] = []
    received = datetime.now(UTC) + timedelta(seconds=2)

    def graph(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/v1.0/subscriptions":
            return httpx.Response(201, json={
                "id": SUBSCRIPTION_ID,
                "expirationDateTime": (datetime.now(UTC) + timedelta(days=6)).isoformat(),
            })
        if request.url.path == "/v1.0/me/mailFolders/inbox":
            return httpx.Response(200, json={"id": "inbox-id"})
        if request.url.path == DELTA_PATH:
            query = parse_qs(request.url.query.decode())
            if query.get("$skiptoken") == ["page2"]:
                if fail_page:
                    return httpx.Response(503)
                return httpx.Response(200, json={
                    "value": [item("message2", received + timedelta(seconds=1))],
                    "@odata.deltaLink": DELTA_ORIGIN + "?$deltatoken=done",
                })
            return httpx.Response(200, json={
                "value": [item("message1", received)],
                "@odata.nextLink": DELTA_ORIGIN + "?$skiptoken=page2",
            })
        raise AssertionError(str(request.url))

    app.state.outlook.client = httpx.AsyncClient(transport=httpx.MockTransport(graph))
    path = f"/api/projects/{project.id}/event-triggers/{trigger.id}/outlook-subscription"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://events.example.com",
    ) as client:
        assert (await request_as(
            client, "POST", path, "owner", PrincipalRole.OWNER,
        )).status_code == 200
        failed = await request_as(
            client, "POST", path + "/sync-delta", "owner", PrincipalRole.OWNER,
        )
        assert failed.status_code == 502
        assert not app.state.connectors.vault.has("outlook-subscription-delta:outlook")
        assert (await request_as(
            client, "GET", path, "owner", PrincipalRole.OWNER,
        )).json()["gap"] is True
        fail_page = False
        recovered = await request_as(
            client, "POST", path + "/sync-delta", "owner", PrincipalRole.OWNER,
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["queued"] == 1
        assert recovered.json()["gap"] is False
        notices = [
            OutlookNotification.model_validate_json(path.read_text())
            for path in (tmp_path / "outlook-subscriptions" / "outlook").glob("*.json")
        ]
        assert {notice.message_id for notice in notices} == {"message1", "message2"}
        assert len([url for url in calls if "changeType=created" in url]) == 2
        assert app.state.connectors.vault.has("outlook-subscription-delta:outlook")

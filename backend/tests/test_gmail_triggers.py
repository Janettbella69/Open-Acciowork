from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient

from ophira_backend.auth import build_gateway_assertion_headers
from ophira_backend.config import Settings
from ophira_backend.constants import PrincipalRole
from ophira_backend.event_triggers import EventTriggerCreate
from ophira_backend.gmail import GMAIL_SCOPE, GmailEnrollment, GmailToken, GmailWatch
from ophira_backend.main import create_app
from ophira_backend.models import ProjectCreate, utc_now


class FakeRuntime:
    async def run(self, _run) -> None:
        return None

    async def cancel(self, _run_id: str) -> None:
        return None


@pytest.mark.asyncio
async def test_gmail_oauth_watch_verified_push_dedup_and_stale_recovery(tmp_path: Path):
    gateway_secret = "gmail-gateway-test-signing-secret-0123456789"
    settings = Settings(
        data_dir=tmp_path, auth_mode="trusted_gateway",
        trusted_gateway_auth_secret=gateway_secret,
        trusted_gateway_tenant_id="tenant_gmail",
        event_public_origin="https://events.example.com",
        gmail_pubsub_topic="projects/demo-project/topics/ophira-inbox",
        gmail_push_service_account="ophira-push@demo-project.iam.gserviceaccount.com",
    )
    app = create_app(settings, runtime=FakeRuntime())
    app.state.connectors.upsert(
        "gmail", type="gmail", display_name="Gmail", scope=GMAIL_SCOPE,
        secret_env="OPHIRA_CONNECTOR_GMAIL_KEY",
        secret=json.dumps({
            "client_id": "123456789012-abcdefgh.apps.googleusercontent.com",
            "client_secret": "google-client-secret-1234567890",
        }),
    )
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    public_jwk["kid"] = "gmail-push-test-key"
    calls: list[str] = []
    upstream = {
        "watch_history": "100", "message_id": "m1", "stale": False,
        "start_history": "100", "result_history": "101",
    }

    def google(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path == "/token":
            params = parse_qs(request.content.decode())
            assert params["client_id"] == ["123456789012-abcdefgh.apps.googleusercontent.com"]
            return httpx.Response(200, json={
                "access_token": "gmail-access-token", "refresh_token": "gmail-refresh-token",
                "expires_in": 3600, "scope": GMAIL_SCOPE,
            })
        if path == "/gmail/v1/users/me/profile":
            assert request.headers["authorization"] == "Bearer gmail-access-token"
            return httpx.Response(200, json={"emailAddress": "buyer-inbox@example.com"})
        if path == "/gmail/v1/users/me/watch":
            assert request.headers["authorization"] == "Bearer gmail-access-token"
            assert json.loads(request.content) == {
                "topicName": "projects/demo-project/topics/ophira-inbox",
                "labelIds": ["INBOX"], "labelFilterBehavior": "INCLUDE",
            }
            return httpx.Response(200, json={
                "historyId": upstream["watch_history"],
                "expiration": str(int((time.time() + 6 * 86400) * 1000)),
            })
        if path == "/gmail/v1/users/me/history":
            if upstream["stale"]:
                return httpx.Response(404, json={"error": "history too old"})
            assert request.url.params["startHistoryId"] == upstream["start_history"]
            return httpx.Response(200, json={
                "history": [{"id": upstream["result_history"], "messagesAdded": [
                    {"message": {"id": upstream["message_id"]}},
                ]}],
                "historyId": upstream["result_history"],
            })
        if path.startswith("/gmail/v1/users/me/messages/"):
            assert request.url.params["format"] == "full"
            return httpx.Response(200, json={
                "id": upstream["message_id"], "labelIds": ["INBOX"],
                "payload": {
                    "mimeType": "multipart/alternative",
                    "headers": [
                        {"name": "From", "value": "Buyer <buyer@example.net>"},
                        {"name": "Subject", "value": "Quote request"},
                    ],
                    "parts": [
                        {"mimeType": "text/plain", "body": {
                            "data": base64.urlsafe_b64encode(b"Please quote 500 units.").decode(),
                        }},
                        {"mimeType": "text/html", "body": {
                            "data": base64.urlsafe_b64encode(b"<b>private html</b>").decode(),
                        }},
                    ],
                },
            })
        if path == "/oauth2/v3/certs":
            return httpx.Response(200, json={"keys": [public_jwk]})
        if path == "/gmail/v1/users/me/stop":
            return httpx.Response(204)
        raise AssertionError(path)

    app.state.gmail.client = httpx.AsyncClient(transport=httpx.MockTransport(google))

    def assertion(
        method: str, path: str, role: PrincipalRole = PrincipalRole.OWNER,
        user: str = "owner@example.com",
    ):
        return build_gateway_assertion_headers(
            gateway_secret, method=method, path=path,
            tenant_id="tenant_gmail", principal_id=user, role=role,
            session_id="gateway_session_" + user.replace("@", "_"),
            issued_at=datetime.now(UTC),
            nonce=f"nonce_{uuid4().hex}",
        )

    def push_token(audience: str = "https://events.example.com/api/connectors/gmail/gmail/push"):
        return jwt.encode({
            "iss": "https://accounts.google.com", "aud": audience,
            "email": "ophira-push@demo-project.iam.gserviceaccount.com",
            "email_verified": True, "iat": int(time.time()), "exp": int(time.time()) + 3600,
        }, private_key, algorithm="RS256", headers={"kid": "gmail-push-test-key"})

    def notification(email: str, history: str) -> bytes:
        data = json.dumps({"emailAddress": email, "historyId": history}).encode()
        return json.dumps({"message": {"data": base64.b64encode(data).decode()}}).encode()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://events.example.com",
    ) as client:
        start_path = "/api/connectors/gmail/gmail-oauth/start"
        start = await client.post(start_path, headers=assertion("POST", start_path))
        assert start.status_code == 200, start.text
        authorization_url = start.json()["authorization_url"]
        query = parse_qs(urlsplit(authorization_url).query)
        assert query["scope"] == [GMAIL_SCOPE]
        assert query["access_type"] == ["offline"]
        assert start.json()["callback_url"] == (
            "https://events.example.com/api/connectors/gmail/gmail-oauth/callback"
        )
        callback = "/api/connectors/gmail/gmail-oauth/callback"
        invalid = await client.get(callback, params={"code": "valid-code", "state": "x" * 40})
        assert invalid.status_code == 403
        complete = await client.get(callback, params={
            "code": "valid-code", "state": query["state"][0],
        }, follow_redirects=False)
        assert complete.status_code == 303, complete.text
        replay = await client.get(callback, params={
            "code": "valid-code", "state": query["state"][0],
        })
        assert replay.status_code == 403

        project_path = "/api/projects"
        project = await client.post(project_path, headers=assertion("POST", project_path),
                                    json={"name": "Gmail", "objective": "Review inquiries"})
        assert project.status_code == 201, project.text
        project_id = project.json()["id"]
        base = f"/api/projects/{project_id}/event-triggers"
        grant_path = f"/api/projects/{project_id}/access"
        grant = await client.post(
            grant_path, headers=assertion("POST", grant_path),
            json={"principal_id": "member@example.com", "role": "member"},
        )
        assert grant.status_code == 200
        member_create = await client.post(
            base, headers=assertion(
                "POST", base, PrincipalRole.MEMBER, "member@example.com",
            ), json={
                "name": "Forbidden inbox", "kind": "inquiry_email", "prompt": "Review",
                "max_budget_usd": 1, "gmail_connector_id": "gmail",
            },
        )
        assert member_create.status_code == 403
        created = await client.post(base, headers=assertion("POST", base), json={
            "name": "Inbox", "kind": "inquiry_email", "prompt": "Review email",
            "max_budget_usd": 1, "gmail_connector_id": "gmail",
        })
        assert created.status_code == 201, created.text
        trigger = created.json()["trigger"]
        assert created.json()["signing_secret"] is None
        assert trigger["gmail_connector_id"] == "gmail"
        delivery = f"{base}/{trigger['id']}/deliver"
        assert (await client.post(delivery, content=b"{}", headers={
            "x-ophira-timestamp": str(int(time.time())),
            "x-ophira-event-id": "forged", "x-ophira-signature": "sha256=" + "0" * 64,
        })).status_code == 404
        watch_path = f"{base}/{trigger['id']}/gmail-watch"
        watch = await client.post(watch_path, headers=assertion("POST", watch_path))
        assert watch.status_code == 200, watch.text
        assert watch.json()["status"] == "active"
        assert watch.json()["history_id"] == "100"
        assert watch.json()["push_url"] == (
            "https://events.example.com/api/connectors/gmail/gmail/push"
        )
        push_path = "/api/connectors/gmail/gmail/push"
        body = notification("buyer-inbox@example.com", "101")
        bad_jwt = await client.post(push_path, content=body, headers={
            "authorization": "Bearer " + push_token("https://other.example.com"),
        })
        assert bad_jwt.status_code == 401
        wrong_mailbox = await client.post(push_path, content=notification(
            "other@example.com", "101",
        ), headers={"authorization": "Bearer " + push_token()})
        assert wrong_mailbox.status_code == 200
        assert wrong_mailbox.json()["status"] == "ignored"
        pushed = await client.post(push_path, content=body, headers={
            "authorization": "Bearer " + push_token(),
        })
        assert pushed.status_code == 200, pushed.text
        assert pushed.json() == {"status": "accepted", "messages": 1}
        runs = app.state.ledger.list_runs(project_id)
        assert len(runs) == 1
        assert "Please quote 500 units." in runs[0].prompt
        assert "private html" not in runs[0].prompt
        duplicate = await client.post(push_path, content=body, headers={
            "authorization": "Bearer " + push_token(),
        })
        assert duplicate.status_code == 200
        assert duplicate.json()["status"] == "duplicate"
        assert len(app.state.ledger.list_runs(project_id)) == 1

        upstream["stale"] = True
        stale = await client.post(push_path, content=notification(
            "buyer-inbox@example.com", "102",
        ), headers={"authorization": "Bearer " + push_token()})
        assert stale.status_code == 409
        status = await client.get(watch_path, headers=assertion("GET", watch_path))
        assert status.json()["status"] == "stale"
        refused = await client.post(watch_path, headers=assertion("POST", watch_path))
        assert refused.status_code == 409
        upstream["watch_history"] = "103"
        reset = await client.post(
            watch_path + "?reset_stale=true", headers=assertion("POST", watch_path),
        )
        assert reset.status_code == 200, reset.text
        assert reset.json()["history_id"] == "103"
        other_project = await client.post(
            project_path, headers=assertion("POST", project_path),
            json={"name": "Other Gmail project", "objective": "Review another workflow"},
        )
        assert other_project.status_code == 201
        other_base = f"/api/projects/{other_project.json()['id']}/event-triggers"
        other_trigger = await client.post(
            other_base, headers=assertion("POST", other_base), json={
                "name": "Shared inbox", "kind": "inquiry_email", "prompt": "Review",
                "max_budget_usd": 1, "gmail_connector_id": "gmail",
            },
        )
        assert other_trigger.status_code == 201
        other_watch_path = f"{other_base}/{other_trigger.json()['trigger']['id']}/gmail-watch"
        other_status = await client.get(
            other_watch_path, headers=assertion("GET", other_watch_path),
        )
        assert other_status.json()["status"] == "not_registered"
        enrolled = await client.post(
            other_watch_path, headers=assertion("POST", other_watch_path),
        )
        assert enrolled.status_code == 200
        assert enrolled.json()["status"] == "active"
        upstream.update({
            "stale": False, "start_history": "103", "result_history": "104",
            "message_id": "m2",
        })
        shared_push = await client.post(
            push_path, content=notification("buyer-inbox@example.com", "104"),
            headers={"authorization": "Bearer " + push_token()},
        )
        assert shared_push.status_code == 200, shared_push.text
        assert len(app.state.ledger.list_runs(project_id)) == 2
        assert len(app.state.ledger.list_runs(other_project.json()["id"])) == 1
        shared_stop = await client.request(
            "DELETE", watch_path, headers=assertion("DELETE", watch_path),
        )
        assert shared_stop.status_code == 409
        pause_path = f"{other_base}/{other_trigger.json()['trigger']['id']}"
        paused = await client.patch(
            pause_path, headers=assertion("PATCH", pause_path),
            json={"expected_revision": 1, "enabled": False},
        )
        assert paused.status_code == 200
        stopped = await client.request(
            "DELETE", watch_path, headers=assertion("DELETE", watch_path),
        )
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["status"] == "not_registered"
    app.state.connectors.store_secret("gmail", json.dumps({
        "client_id": "123456789012-abcdefgh.apps.googleusercontent.com",
        "client_secret": "replacement-client-secret-1234567890",
    }))
    assert app.state.connectors.vault.get("gmail-oauth-token:gmail") is None
    assert calls.count("/gmail/v1/users/me/history") == 3


@pytest.mark.asyncio
async def test_gmail_cursor_advances_per_completed_history_record(tmp_path: Path):
    app = create_app(Settings(data_dir=tmp_path), runtime=FakeRuntime())
    ledger = app.state.ledger
    project = ledger.create_project(ProjectCreate(name="Mail", objective="Review"))
    triggers = app.state.event_triggers
    trigger = triggers.create(project.id, EventTriggerCreate(
        name="Inbox", kind="inquiry_email", prompt="Review",
        max_budget_usd=1, gmail_connector_id="gmail",
    ), principal=None).trigger
    connector = app.state.connectors.upsert(
        "gmail", type="gmail", display_name="Gmail", scope=GMAIL_SCOPE,
        secret=json.dumps({
            "client_id": "123456789012-abcdefgh.apps.googleusercontent.com",
            "client_secret": "google-client-secret-1234567890",
        }),
    )
    gmail = app.state.gmail
    app.state.connectors.vault.set("gmail-oauth-token:gmail", GmailToken(
        email="inbox@example.com", tenant_id=None,
        connector_revision=connector.revision, refresh_token="refresh",
        access_token="access", expires_at=time.time() + 3600,
    ).model_dump_json())
    gmail._save_watch(GmailWatch(
        connector_id="gmail", connector_revision=connector.revision, tenant_id=None,
        email="inbox@example.com", topic="projects/demo/topics/inbox",
        history_id="100", expiration_ms=int((time.time() + 86400) * 1000),
        updated_at=utc_now().isoformat(),
    ))
    gmail._save_enrollment(trigger, GmailEnrollment(
        project_id=project.id, trigger_id=trigger.id, connector_id="gmail",
        connector_revision=connector.revision, tenant_id=None,
        email="inbox@example.com", start_history_id="100",
    ))

    async def google(_method, url, *, params=None, **_kwargs):
        if url.endswith("/history"):
            records = [
                {"id": "101", "messagesAdded": [{"message": {"id": "m1"}}]},
                {"id": "102", "messagesAdded": [{"message": {"id": "m2"}}]},
            ]
            if params["startHistoryId"] == "101":
                records = records[1:]
            return {"history": records, "historyId": "102"}
        message_id = url.rsplit("/", 1)[-1]
        return {
            "id": message_id, "labelIds": ["INBOX"],
            "payload": {"mimeType": "text/plain", "headers": [
                {"name": "From", "value": "buyer@example.com"},
                {"name": "Subject", "value": "Inquiry"},
            ], "body": {"data": base64.urlsafe_b64encode(b"Quote please").decode()}},
        }

    gmail._json = google
    delivered: list[str] = []
    fail_second = True

    async def dispatch(_trigger, event_id, _canonical, _payload):
        nonlocal fail_second
        delivered.append(event_id)
        if event_id == "gmail:m2" and fail_second:
            fail_second = False
            raise RuntimeError("worker interrupted")
        return {"status": "accepted"}

    body = json.dumps({"message": {"data": base64.b64encode(json.dumps({
        "emailAddress": "inbox@example.com", "historyId": "102",
    }).encode()).decode()}}).encode()
    with pytest.raises(RuntimeError, match="worker interrupted"):
        await gmail.process_push("gmail", body, dispatch)
    assert gmail._watch("gmail").history_id == "101"
    result = await gmail.process_push("gmail", body, dispatch)
    assert result == {"status": "accepted", "messages": 1}
    assert gmail._watch("gmail").history_id == "102"
    assert delivered == ["gmail:m1", "gmail:m2", "gmail:m2"]

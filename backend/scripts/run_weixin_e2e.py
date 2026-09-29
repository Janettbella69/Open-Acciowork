#!/usr/bin/env python3
"""Real loopback API + durable Weixin workflow, with offline external fixtures.

No real Weixin login, message delivery, DNS or model request is performed.
Only the application's Uvicorn HTTP listener uses a real socket. The official
Weixin transport serializes/parses requests against httpx.MockTransport.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from ophira_backend.channel_delivery import ChannelDeliveryError, DeliveryStatus  # noqa: E402
from ophira_backend.channels import ChannelError  # noqa: E402
from ophira_backend.config import Settings  # noqa: E402
from ophira_backend.constants import EventType  # noqa: E402
from ophira_backend.events import EventPublisher  # noqa: E402
from ophira_backend.weixin_accounts import WeixinStore  # noqa: E402
from ophira_backend.weixin_channel import WeixinChannel  # noqa: E402

PREFIX = "/api/channels/weixin"
BOT = "offline-bot@im.bot"
OWNER = "offline-owner@im.wechat"
TOKEN = "offline-private-bot-token-canary"
CONTEXT = "offline-private-context-token-canary"
QR = "offline-private-qr-ticket-canary"
PUBLIC_IP = "93.184.216.34"


class OfflineRuntime:
    ledger = None

    async def run(self, run):
        await EventPublisher(self.ledger).publish(
            run.id, EventType.ASSISTANT_MESSAGE_COMPLETED,
            {"text": "Offline fixture result: the ordinary Run path completed."},
        )

    async def cancel(self, _run_id):
        pass


class OfficialHttpFixture:
    def __init__(self):
        self.incoming = None
        self.sent = []
        self.cursors = []
        self.requests = 0

    def handle(self, request):
        self.requests += 1
        assert request.url.host == PUBLIC_IP
        assert request.headers["host"] == "ilinkai.weixin.qq.com"
        assert request.extensions["sni_hostname"] == "ilinkai.weixin.qq.com"
        path = request.url.path
        if path == "/ilink/bot/get_bot_qrcode":
            assert json.loads(request.content) == {"local_token_list": []}
            return httpx.Response(200, json={"qrcode": QR, "qrcode_img_content": "offline QR"})
        if path == "/ilink/bot/get_qrcode_status":
            assert request.url.params["qrcode"] == QR
            return httpx.Response(200, json={
                "status": "confirmed", "bot_token": TOKEN,
                "ilink_bot_id": BOT, "ilink_user_id": OWNER,
                "baseurl": "https://ilinkai.weixin.qq.com",
            })
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        body = json.loads(request.content)
        if path == "/ilink/bot/getupdates":
            self.cursors.append(body["get_updates_buf"])
            assert self.incoming is not None
            return httpx.Response(200, json={
                "ret": 0, "msgs": [self.incoming], "get_updates_buf": "durable-fixture-cursor",
            })
        if path == "/ilink/bot/sendmessage":
            msg = body["msg"]
            assert msg["context_token"] == CONTEXT and msg["to_user_id"] == OWNER
            self.sent.append(msg)
            return httpx.Response(200, json={"ret": 0, "message_id": "9223372036854775808"})
        raise AssertionError("Unexpected fixture endpoint")


async def request(client, method, path, **kwargs):
    response = await client.request(method, path, **kwargs)
    assert not response.is_error, (method, path, response.status_code)
    for secret in (TOKEN, CONTEXT, QR):
        assert secret not in response.text
    return response.json()


def assert_private_boundaries(data_dir, vault_root):
    for path in data_dir.resolve().rglob("*"):
        if path.is_file() and not path.is_relative_to(vault_root):
            raw = path.read_bytes()
            assert all(secret.encode() not in raw for secret in (TOKEN, CONTEXT, QR))


async def exercise(client, app, fixture, checks):
    project = await request(client, "POST", "/api/projects", json={
        "name": "Offline Weixin acceptance", "objective": "Verify single-chat binding and reply",
    })
    await request(client, "PUT", "/api/org/policy", json={"channel_delivery_enabled": True})
    pairing = await request(client, "POST", PREFIX + "/pairings", json={})
    assert pairing["qr_svg_data_url"].startswith("data:image/svg+xml;base64,")
    confirmed = await request(client, "POST", f"{PREFIX}/pairings/{pairing['id']}/poll", json={})
    assert confirmed["status"] == "confirmed"
    account_id = confirmed["account_id"]
    overview = await request(client, "GET", PREFIX)
    initial = next(a for a in overview["accounts"] if a["id"] == account_id)
    assert not initial["enabled"] and initial["project_id"] is None
    binding_path = f"{PREFIX}/accounts/{account_id}/binding"
    account = await request(client, "PUT", binding_path, json={
        "project_id": project["id"], "enabled": True, "expected_revision": initial["revision"],
    })
    checks["real_http_pair_confirm_explicit_local_binding"] = True

    await asyncio.sleep(0.002)  # Do not straddle the binding's millisecond timestamp floor.
    fixture.incoming = {
        "message_id": 18446744073709551615,
        "create_time_ms": int(datetime.now(UTC).timestamp() * 1000),
        "message_type": 1, "message_state": 2,
        "from_user_id": OWNER, "to_user_id": BOT, "context_token": CONTEXT,
        "item_list": [{"type": 1, "text_item": {"text": "Check this supplier"}}],
    }
    outcome = await app.state.weixin_channel.poll_once(account_id)
    assert outcome == {"processed": 1, "accepted": 1, "rejected": 0}
    await asyncio.gather(*list(app.state.runs._tasks.values()))
    run = app.state.ledger.list_runs(project["id"])[0]
    visible = await request(client, "GET", f"/api/runs/{run.id}")
    assert visible["status"] == "completed" and visible["identity_binding"] == "local_only"
    obligation = app.state.channel_delivery.store.get_for_run(run.id)
    assert obligation.status == DeliveryStatus.DELIVERED
    assert obligation.external_message_id == "9223372036854775808"
    assert len(fixture.sent) == 1
    checks["official_wire_fixture_to_real_run_durable_reply_ack"] = True

    # A new store/channel instance reads the committed cursor; repeating the
    # server message cannot create another Run even without in-memory history.
    original = app.state.weixin_channel
    restored_store = WeixinStore(
        app.state.ledger, app.state.weixin_store.vault, app.state.project_access,
    )
    restored = WeixinChannel(
        restored_store, app.state.weixin_transport, app.state.policy,
        original.replays, app.state.runs, app.state.channel_delivery,
    )
    outcome = await restored.poll_once(account_id)
    assert outcome["accepted"] == 0
    assert fixture.cursors == ["", "durable-fixture-cursor"]
    assert len(app.state.ledger.list_runs(project["id"])) == len(fixture.sent) == 1
    checks["cursor_recovered_duplicate_does_not_rerun"] = True

    rebound = await request(client, "PUT", binding_path, json={
        "project_id": project["id"], "enabled": True, "expected_revision": account["revision"],
    })
    try:
        await restored.dispatch_authenticated_message(account_id, fixture.incoming)
    except ChannelError as exc:
        assert exc.code == "STALE_MESSAGE"
    else:
        raise AssertionError("Rebinding admitted an old message")
    adapter = app.state.channel_delivery.weixin_adapter
    try:
        await adapter.reply(obligation.target, "old result", idempotency_key="stale-reply")
    except ChannelDeliveryError as exc:
        assert exc.code == "REPLY_SCOPE_UNAVAILABLE"
    else:
        raise AssertionError("Rebinding permitted an old reply")
    checks["rebind_rejects_old_incoming_and_old_reply"] = True

    ref = restored_store.save_context(
        account_id, "2", CONTEXT, expected_revision=rebound["revision"],
    )
    current_target = obligation.target.model_copy(update={
        "account_revision": rebound["revision"], "context_ref": ref,
        "message_id": "2", "event_id": "2",
    })
    revoked = await request(client, "DELETE", f"{PREFIX}/accounts/{account_id}", params={
        "expected_revision": rebound["revision"],
    })
    assert revoked["status"] == "revoked"
    assert restored_store.vault.get(account_id) is None
    try:
        await adapter.reply(current_target, "revoked result", idempotency_key="revoked-reply")
    except ChannelDeliveryError as exc:
        assert exc.code == "REPLY_SCOPE_UNAVAILABLE"
    else:
        raise AssertionError("Revocation permitted a reply")
    assert len(fixture.sent) == 1
    checks["real_http_revoke_erases_credentials_and_blocks_reply"] = True


async def run():
    checks = {}
    fixture = OfficialHttpFixture()
    with tempfile.TemporaryDirectory(prefix="ophira-weixin-http-") as temporary:
        settings = Settings(
            _env_file=None, data_dir=Path(temporary), auth_mode="local_only",
            anthropic_api_key="", anthropic_auth_token="", claude_model=None,
            dingtalk_app_key=None, dingtalk_app_secret=None,
            wecom_bot_id=None, wecom_bot_secret=None, feishu_app_id=None,
            feishu_app_secret=None, channel_webhook_url=None, push_enabled=False,
            companion_enabled=False,
        )
        # main's module-level app also uses this isolated data directory and no .env.
        with patch("ophira_backend.config.get_settings", return_value=settings):
            from ophira_backend.main import create_app
        runtime = OfflineRuntime()
        app = create_app(settings, runtime=runtime)
        runtime.ledger = app.state.ledger
        app.state.weixin_transport._transport = httpx.MockTransport(fixture.handle)
        app.state.scheduler.interval_seconds = 3600
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=listener.getsockname()[1],
            log_level="error", access_log=False,
        ))
        with patch("ophira_backend.web_tools._resolve_ips", return_value=[PUBLIC_IP]):
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(15):
                    while not server.started:
                        if serving.done():
                            serving.result()
                            raise AssertionError("Server stopped before startup")
                        await asyncio.sleep(0.02)
                # The actual lifespan starts/stops the discovery loop. Explicit
                # poll_once calls keep fixture message timing deterministic here.
                await app.state.weixin_channel.stop()
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
                    timeout=20, trust_env=False,
                ) as client:
                    await exercise(client, app, fixture, checks)
                await asyncio.to_thread(
                    assert_private_boundaries, Path(temporary), app.state.weixin_store.vault.root,
                )
                checks["no_credentials_in_run_events_or_public_delivery"] = True
            finally:
                server.should_exit = True
                try:
                    await asyncio.wait_for(serving, 15)
                finally:
                    listener.close()
    return {
        "checks": checks,
        "fixture_http_requests": fixture.requests,
        "boundaries": [
            "Real Uvicorn loopback HTTP control plane, file ledger and RunManager.",
            "Weixin HTTP wire requests/ACKs use MockTransport; DNS is an explicit fixture.",
            "OfflineRuntime only: no SDK model request, paid Provider or real Weixin message.",
            "Temporary data, synthetic credentials, no .env or production secret access.",
            "Explicit poll_once calls; discovery lifecycle and races have separate tests.",
            "Local-only identity; trusted-gateway authorization has separate contract tests.",
            "Does not validate real QR scanning, upstream availability or model quality.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = asyncio.run(run())
    assert all(result["checks"].values())
    report = json.dumps(result, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")
    print(report, end="")

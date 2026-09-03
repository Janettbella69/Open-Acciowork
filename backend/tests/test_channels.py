from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from httpx import ASGITransport, AsyncClient

from ophira_backend.channels import (
    ChannelReplayGuard,
    decrypt_feishu_payload,
    deliver_notification,
    feishu_should_handle,
    feishu_timestamp_is_fresh,
    to_inbound,
    verify_feishu_signature,
)
from ophira_backend.config import Settings
from ophira_backend.main import create_app
from ophira_backend.models import Notification


class FakeRuntime:
    async def run(self, _run) -> None:
        return None

    async def cancel(self, _run_id: str) -> None:
        return None


def signed_headers(body: bytes, key: str, *, timestamp: str, nonce: str) -> dict[str, str]:
    signature = hashlib.sha256(
        timestamp.encode() + nonce.encode() + key.encode() + body
    ).hexdigest()
    return {
        "content-type": "application/json",
        "x-lark-request-timestamp": timestamp,
        "x-lark-request-nonce": nonce,
        "x-lark-signature": signature,
    }


def encrypted_payload(payload: dict, key_text: str) -> str:
    digest = hashes.Hash(hashes.SHA256())
    digest.update(key_text.encode())
    key = digest.finalize()
    iv = bytes(range(16))
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(json.dumps(payload).encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(iv + ciphertext).decode()


def test_feishu_signature_requires_nonce_and_fresh_timestamp() -> None:
    body = b'{"event":"value"}'
    timestamp = "1000"
    nonce = "request-nonce"
    key = "encrypt-key"
    signature = signed_headers(body, key, timestamp=timestamp, nonce=nonce)[
        "x-lark-signature"
    ]

    assert verify_feishu_signature(timestamp, nonce, signature, key, body)
    assert not verify_feishu_signature(timestamp, "different", signature, key, body)
    assert feishu_timestamp_is_fresh(timestamp, now=1299)
    assert not feishu_timestamp_is_fresh(timestamp, now=1300)


def test_feishu_encrypted_payload_uses_official_aes_cbc_envelope() -> None:
    key = "encrypt-key"
    original = {"schema": "2.0", "header": {"event_id": "evt-encrypted"}}
    outer = {"encrypt": encrypted_payload(original, key), "request_id": "outer"}

    assert decrypt_feishu_payload(outer, key) == {**original, "request_id": "outer"}

    with pytest.raises(Exception, match="invalid encrypted Feishu payload"):
        decrypt_feishu_payload({"encrypt": "not base64"}, key)


def test_replay_guard_is_durable_and_releasable(tmp_path: Path) -> None:
    path = tmp_path / "replays.json"
    guard = ChannelReplayGuard(path)

    token = guard.claim("event-1", now=1000)
    assert token
    assert ChannelReplayGuard(path).claim("event-1", now=1001) is None

    guard.release(token)
    assert ChannelReplayGuard(path).claim("event-1", now=1002)


def test_group_messages_require_the_configured_bot_mention() -> None:
    payload = {
        "event": {
            "message": {
                "chat_type": "group",
                "content": json.dumps({"text": "@_user_1 check this"}),
                "mentions": [
                    {"key": "@_user_1", "id": {"open_id": "ou_bot"}},
                ],
            },
            "sender": {"sender_id": {"open_id": "ou_sender"}},
        }
    }

    assert feishu_should_handle(payload, "ou_bot")
    assert not feishu_should_handle(payload, "ou_other")
    inbound = to_inbound(payload, project_id="project_1")
    assert inbound.text == "check this"
    assert inbound.sender == "ou_sender"


@pytest.mark.asyncio
async def test_notification_webhook_requires_https(tmp_path: Path) -> None:
    result = await deliver_notification(
        Settings(data_dir=tmp_path, channel_webhook_url="http://example.org/hook"),
        Notification(title="Changed"),
    )

    assert result["delivered"] is False
    assert result["reason"] == "URL_REJECTED"


@pytest.mark.asyncio
async def test_feishu_webhook_fails_closed_without_encrypt_key(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path), runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/channels/feishu",
            json={"type": "url_verification", "challenge": "token-1"},
        )

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_signed_challenge_is_idempotent(tmp_path: Path) -> None:
    key = "encrypt-key"
    app = create_app(
        Settings(data_dir=tmp_path, feishu_encrypt_key=key),
        runtime=FakeRuntime(),
    )
    body = json.dumps(
        {"type": "url_verification", "challenge": "token-1"},
        separators=(",", ":"),
    ).encode()
    timestamp = str(int(time.time()))
    headers = signed_headers(body, key, timestamp=timestamp, nonce="nonce-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/api/channels/feishu", content=body, headers=headers)
        duplicate = await client.post("/api/channels/feishu", content=body, headers=headers)

    assert first.status_code == 200
    assert first.json() == {"challenge": "token-1"}
    assert duplicate.json() == {"duplicate": True, "channel": "feishu"}


@pytest.mark.asyncio
async def test_encrypted_signed_challenge_is_decrypted(tmp_path: Path) -> None:
    key = "encrypt-key"
    app = create_app(
        Settings(data_dir=tmp_path, feishu_encrypt_key=key),
        runtime=FakeRuntime(),
    )
    body = json.dumps(
        {"encrypt": encrypted_payload({"type": "url_verification", "challenge": "ok"}, key)},
        separators=(",", ":"),
    ).encode()
    timestamp = str(int(time.time()))
    headers = signed_headers(body, key, timestamp=timestamp, nonce="encrypted-nonce")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/channels/feishu", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"challenge": "ok"}


@pytest.mark.asyncio
async def test_feishu_rejects_oversized_and_non_object_bodies(tmp_path: Path) -> None:
    key = "encrypt-key"
    app = create_app(
        Settings(data_dir=tmp_path, feishu_encrypt_key=key),
        runtime=FakeRuntime(),
    )
    timestamp = str(int(time.time()))
    list_body = b"[]"
    list_headers = signed_headers(list_body, key, timestamp=timestamp, nonce="list")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        non_object = await client.post(
            "/api/channels/feishu", content=list_body, headers=list_headers
        )
        too_large = await client.post(
            "/api/channels/feishu",
            content=b"{}",
            headers={"content-length": "1000001"},
        )

    assert non_object.status_code == 400
    assert too_large.status_code == 413


@pytest.mark.asyncio
async def test_unmentioned_group_message_is_ignored(tmp_path: Path) -> None:
    key = "encrypt-key"
    app = create_app(
        Settings(
            data_dir=tmp_path,
            feishu_encrypt_key=key,
            feishu_bot_open_id="ou_bot",
        ),
        runtime=FakeRuntime(),
    )
    payload = {
        "schema": "2.0",
        "header": {"event_id": "event-ignore"},
        "event": {
            "message": {
                "chat_type": "group",
                "content": json.dumps({"text": "hello"}),
                "mentions": [],
            }
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    headers = signed_headers(body, key, timestamp=timestamp, nonce="nonce-ignore")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/channels/feishu", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json()["ignored"] is True

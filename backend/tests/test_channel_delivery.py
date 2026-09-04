from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from ophira_backend.channel_delivery import (
    FEISHU_IDEMPOTENCY_WINDOW,
    ChannelDeliveryService,
    ChannelDeliveryStore,
    DeliveryStatus,
    FeishuReplyAdapter,
    FeishuReplyResult,
    FeishuReplyTarget,
)
from ophira_backend.channels import ChannelError, feishu_reply_target
from ophira_backend.config import Settings
from ophira_backend.constants import EventType, ProviderErrorCode, RunStatus
from ophira_backend.events import EventPublisher
from ophira_backend.models import ProjectCreate, RunRecord, utc_now
from ophira_backend.storage import FileLedger

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def target(*, threaded: bool = False) -> FeishuReplyTarget:
    return FeishuReplyTarget(
        event_id="evt_verified",
        message_id="om_source_message",
        chat_id="oc_verified_chat",
        chat_type="group",
        thread_id="om_thread_root" if threaded else None,
        reply_in_thread=threaded,
    )


def ledger_run(tmp_path: Path) -> tuple[FileLedger, RunRecord]:
    ledger = FileLedger(tmp_path / "data")
    project = ledger.create_project(
        ProjectCreate(name="Feishu reply", objective="Return the final visible result")
    )
    return ledger, ledger.create_run(project.id, "Handle a verified Feishu request")


def adapter_settings(tmp_path: Path, **changes: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "data_dir": tmp_path / "data",
        "feishu_app_id": "cli_test_app",
        "feishu_app_secret": "app-secret-must-not-persist",
        "feishu_api_base_url": "https://open.feishu.cn",
        "feishu_delivery_retry_max_seconds": 60,
    }
    values.update(changes)
    return Settings(**values)


async def complete_run(
    ledger: FileLedger,
    run: RunRecord,
    *,
    text: str = "这是 Agent 的最终可见结果。",
) -> None:
    publisher = EventPublisher(ledger)
    await publisher.publish(
        run.id,
        EventType.ASSISTANT_MESSAGE_COMPLETED,
        {"text": text},
    )
    ledger.update_run(run.id, status=RunStatus.COMPLETED, finished_at=utc_now())
    await publisher.publish(run.id, EventType.RUN_COMPLETED, {"status": "completed"})


def success_handler(
    calls: list[httpx.Request],
    *,
    reply_statuses: list[int] | None = None,
    retry_after: str | None = None,
) -> httpx.MockTransport:
    statuses = list(reply_statuses or [200])

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "t-tenant-token-must-not-persist",
                    "expire": 7200,
                },
            )
        status = statuses.pop(0) if statuses else 200
        if status == 200:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "message_id": "om_delivered_reply",
                        "chat_id": "oc_verified_chat",
                    },
                },
            )
        headers = {"Retry-After": retry_after} if retry_after else None
        return httpx.Response(status, headers=headers, json={"code": status, "msg": "safe"})

    return httpx.MockTransport(handler)


def test_verified_inbound_message_captures_exact_reply_and_thread_scope() -> None:
    payload = {
        "header": {"event_id": "evt_1"},
        "event": {
            "message": {
                "message_id": "om_inbound",
                "chat_id": "oc_chat",
                "chat_type": "group",
                "root_id": "om_root",
            }
        },
    }

    captured = feishu_reply_target(payload, verified_event_id="evt_1")

    assert captured.message_id == "om_inbound"
    assert captured.chat_id == "oc_chat"
    assert captured.thread_id == "om_root"
    assert captured.reply_in_thread is True

    with pytest.raises(ChannelError, match="message_id/chat scope"):
        feishu_reply_target(
            {"event": {"message": {"message_id": "om_only"}}},
            verified_event_id="evt_2",
        )


def test_delivery_store_binds_run_idempotently_and_records_all_four_states(
    tmp_path: Path,
) -> None:
    ledger, run = ledger_run(tmp_path)
    store = ChannelDeliveryStore(tmp_path / "deliveries")

    pending = store.bind_run(run, target(), max_attempts=3, now=NOW)
    duplicate = store.bind_run(run, target(), max_attempts=3, now=NOW)
    assert duplicate.id == pending.id
    assert pending.status == DeliveryStatus.PENDING
    assert len(pending.idempotency_key) == 48

    store.attach_result(pending.id, "done", now=NOW)
    attempting = store.claim_due(pending.id, now=NOW)
    assert attempting is not None
    assert attempting.status == DeliveryStatus.ATTEMPTING
    failed = store.mark_failed(
        pending.id,
        code="HTTP_503",
        detail="temporary",
        retryable=True,
        retry_delay_seconds=1,
        now=NOW,
    )
    assert failed.status == DeliveryStatus.FAILED
    retrying = store.claim_due(pending.id, now=NOW + timedelta(seconds=1))
    assert retrying is not None
    delivered = store.mark_delivered(
        pending.id,
        FeishuReplyResult(message_id="om_reply", chat_id="oc_verified_chat"),
        now=NOW + timedelta(seconds=1),
    )

    assert delivered.status == DeliveryStatus.DELIVERED
    assert [item.status for item in delivered.history] == [
        DeliveryStatus.PENDING,
        DeliveryStatus.ATTEMPTING,
        DeliveryStatus.FAILED,
        DeliveryStatus.ATTEMPTING,
        DeliveryStatus.DELIVERED,
    ]


@pytest.mark.asyncio
async def test_official_adapter_uses_tenant_token_reply_api_and_one_cached_token(
    tmp_path: Path,
) -> None:
    calls: list[httpx.Request] = []
    adapter = FeishuReplyAdapter(
        adapter_settings(tmp_path),
        transport=success_handler(calls, reply_statuses=[200, 200]),
    )

    first = await adapter.reply(target(threaded=True), "first", idempotency_key="idempotent-1")
    second = await adapter.reply(target(), "second", idempotency_key="idempotent-2")

    assert first.message_id == second.message_id == "om_delivered_reply"
    assert [request.url.path for request in calls] == [
        "/open-apis/auth/v3/tenant_access_token/internal",
        "/open-apis/im/v1/messages/om_source_message/reply",
        "/open-apis/im/v1/messages/om_source_message/reply",
    ]
    token_body = json.loads(calls[0].content)
    assert token_body == {
        "app_id": "cli_test_app",
        "app_secret": "app-secret-must-not-persist",
    }
    reply_body = json.loads(calls[1].content)
    assert reply_body["msg_type"] == "text"
    assert reply_body["reply_in_thread"] is True
    assert reply_body["uuid"] == "idempotent-1"
    assert json.loads(reply_body["content"]) == {"text": "first"}
    assert calls[1].headers["authorization"] == "Bearer t-tenant-token-must-not-persist"


def test_adapter_rejects_non_official_credential_destination(tmp_path: Path) -> None:
    settings = adapter_settings(tmp_path, feishu_api_base_url="https://attacker.example")

    with pytest.raises(ValueError, match="official HTTPS OpenAPI origin"):
        FeishuReplyAdapter(settings, transport=httpx.MockTransport(lambda _: None))


@pytest.mark.asyncio
async def test_completed_run_is_replied_and_credentials_never_enter_delivery_ledger(
    tmp_path: Path,
) -> None:
    ledger, run = ledger_run(tmp_path)
    store = ChannelDeliveryStore(tmp_path / "deliveries")
    calls: list[httpx.Request] = []
    service = ChannelDeliveryService(
        ledger,
        store,
        FeishuReplyAdapter(adapter_settings(tmp_path), transport=success_handler(calls)),
        max_attempts=3,
    )
    record = service.bind_run(run, target())
    await complete_run(ledger, run, text="final answer")

    delivered = await service.finalize_run(run.id, now=NOW)

    assert delivered is not None
    assert delivered.status == DeliveryStatus.DELIVERED
    assert delivered.content == "final answer"
    assert delivered.external_message_id == "om_delivered_reply"
    persisted = (tmp_path / "deliveries" / f"{record.id}.json").read_text("utf-8")
    assert "app-secret-must-not-persist" not in persisted
    assert "t-tenant-token-must-not-persist" not in persisted


@pytest.mark.asyncio
async def test_failed_run_replies_with_safe_terminal_error(tmp_path: Path) -> None:
    ledger, run = ledger_run(tmp_path)
    store = ChannelDeliveryStore(tmp_path / "deliveries")
    calls: list[httpx.Request] = []
    service = ChannelDeliveryService(
        ledger,
        store,
        FeishuReplyAdapter(adapter_settings(tmp_path), transport=success_handler(calls)),
    )
    service.bind_run(run, target())
    ledger.update_run(
        run.id,
        status=RunStatus.FAILED,
        error="provider unavailable",
        error_code=ProviderErrorCode.BASE_URL_UNREACHABLE,
        finished_at=utc_now(),
    )

    delivered = await service.finalize_run(run.id, now=NOW)

    assert delivered is not None
    assert delivered.status == DeliveryStatus.DELIVERED
    assert "provider unavailable" in (delivered.content or "")
    assert ProviderErrorCode.BASE_URL_UNREACHABLE.value in (delivered.content or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_rate_limit_and_server_errors_retry_then_deliver(
    tmp_path: Path,
    status: int,
) -> None:
    ledger, run = ledger_run(tmp_path)
    store = ChannelDeliveryStore(tmp_path / "deliveries")
    calls: list[httpx.Request] = []
    service = ChannelDeliveryService(
        ledger,
        store,
        FeishuReplyAdapter(
            adapter_settings(tmp_path),
            transport=success_handler(
                calls,
                reply_statuses=[status, 200],
                retry_after="7" if status == 429 else None,
            ),
        ),
        retry_base_seconds=2,
        retry_max_seconds=30,
    )
    service.bind_run(run, target())
    await complete_run(ledger, run)

    failed = await service.finalize_run(run.id, now=NOW)
    assert failed is not None
    assert failed.status == DeliveryStatus.FAILED
    assert failed.retryable is True
    delay = 7 if status == 429 else 2
    assert failed.next_attempt_at == NOW + timedelta(seconds=delay)
    assert await service.reconcile(now=NOW + timedelta(seconds=delay - 1)) == []

    retried = await service.reconcile(now=NOW + timedelta(seconds=delay))

    assert retried[-1].status == DeliveryStatus.DELIVERED
    assert len([request for request in calls if request.url.path.endswith("/reply")]) == 2


@pytest.mark.asyncio
async def test_client_error_is_terminal_and_never_retried(tmp_path: Path) -> None:
    ledger, run = ledger_run(tmp_path)
    store = ChannelDeliveryStore(tmp_path / "deliveries")
    calls: list[httpx.Request] = []
    service = ChannelDeliveryService(
        ledger,
        store,
        FeishuReplyAdapter(
            adapter_settings(tmp_path),
            transport=success_handler(calls, reply_statuses=[400]),
        ),
    )
    service.bind_run(run, target())
    await complete_run(ledger, run)

    failed = await service.finalize_run(run.id, now=NOW)
    later = await service.reconcile(now=NOW + timedelta(days=1))

    assert failed is not None
    assert failed.status == DeliveryStatus.FAILED
    assert failed.retryable is False
    assert failed.last_error_code == "HTTP_400"
    assert later == []
    assert len([request for request in calls if request.url.path.endswith("/reply")]) == 1


@pytest.mark.asyncio
async def test_retry_budget_is_bounded(tmp_path: Path) -> None:
    ledger, run = ledger_run(tmp_path)
    store = ChannelDeliveryStore(tmp_path / "deliveries")
    calls: list[httpx.Request] = []
    service = ChannelDeliveryService(
        ledger,
        store,
        FeishuReplyAdapter(
            adapter_settings(tmp_path),
            transport=success_handler(calls, reply_statuses=[503, 503, 200]),
        ),
        max_attempts=2,
        retry_base_seconds=1,
    )
    service.bind_run(run, target())
    await complete_run(ledger, run)

    first = await service.finalize_run(run.id, now=NOW)
    assert first is not None and first.retryable
    second = await service.reconcile(now=NOW + timedelta(seconds=1))

    assert second[-1].status == DeliveryStatus.FAILED
    assert second[-1].retryable is False
    assert second[-1].last_error_code == "RETRY_EXHAUSTED"
    assert len([request for request in calls if request.url.path.endswith("/reply")]) == 2


@pytest.mark.asyncio
async def test_startup_recovery_replays_interrupted_attempt_with_same_idempotency_key(
    tmp_path: Path,
) -> None:
    ledger, run = ledger_run(tmp_path)
    store = ChannelDeliveryStore(tmp_path / "deliveries")
    started_at = utc_now()
    record = store.bind_run(run, target(), max_attempts=3, now=started_at)
    await complete_run(ledger, run)
    store.attach_result(record.id, "final", now=started_at)
    interrupted = store.claim_due(record.id, now=started_at)
    assert interrupted is not None
    calls: list[httpx.Request] = []
    service = ChannelDeliveryService(
        ledger,
        ChannelDeliveryStore(tmp_path / "deliveries"),
        FeishuReplyAdapter(adapter_settings(tmp_path), transport=success_handler(calls)),
        worker_interval_seconds=0.01,
    )

    await service.start()
    try:
        for _ in range(100):
            recovered = service.store.get(record.id)
            if recovered.status == DeliveryStatus.DELIVERED:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("startup worker did not recover the delivery")
    finally:
        await service.stop()

    assert recovered.status == DeliveryStatus.DELIVERED
    reply_body = json.loads(
        next(request for request in calls if request.url.path.endswith("/reply")).content
    )
    assert reply_body["uuid"] == interrupted.idempotency_key
    assert [transition.status for transition in recovered.history][-3:] == [
        DeliveryStatus.FAILED,
        DeliveryStatus.ATTEMPTING,
        DeliveryStatus.DELIVERED,
    ]


@pytest.mark.asyncio
async def test_expired_ambiguous_attempt_stops_before_duplicate_reply(tmp_path: Path) -> None:
    ledger, run = ledger_run(tmp_path)
    store = ChannelDeliveryStore(tmp_path / "deliveries")
    record = store.bind_run(run, target(), max_attempts=3, now=NOW)
    await complete_run(ledger, run)
    store.attach_result(record.id, "final", now=NOW)
    store.claim_due(record.id, now=NOW)
    calls: list[httpx.Request] = []
    service = ChannelDeliveryService(
        ledger,
        store,
        FeishuReplyAdapter(adapter_settings(tmp_path), transport=success_handler(calls)),
    )

    assert await service.reconcile(now=NOW + FEISHU_IDEMPOTENCY_WINDOW) == []
    failed = store.get(record.id)

    assert failed.status == DeliveryStatus.FAILED
    assert failed.retryable is False
    assert failed.last_error_code == "IDEMPOTENCY_WINDOW_EXPIRED"
    assert calls == []


@pytest.mark.asyncio
async def test_unconfigured_and_policy_disabled_delivery_are_honest_terminal_states(
    tmp_path: Path,
) -> None:
    ledger, run = ledger_run(tmp_path)
    store = ChannelDeliveryStore(tmp_path / "deliveries-unconfigured")
    unconfigured = ChannelDeliveryService(
        ledger,
        store,
        FeishuReplyAdapter(
            Settings(_env_file=None, data_dir=tmp_path / "data"),
            transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        ),
    )
    unconfigured.bind_run(run, target())
    await complete_run(ledger, run)

    missing = await unconfigured.finalize_run(run.id, now=NOW)
    assert missing is not None
    assert missing.status == DeliveryStatus.FAILED
    assert missing.last_error_code == "CHANNEL_NOT_CONFIGURED"

    ledger_two, run_two = ledger_run(tmp_path / "second")
    store_two = ChannelDeliveryStore(tmp_path / "deliveries-policy")
    policy_off = ChannelDeliveryService(
        ledger_two,
        store_two,
        FeishuReplyAdapter(
            adapter_settings(tmp_path / "second"),
            transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        ),
        enabled=lambda: False,
    )
    policy_off.bind_run(run_two, target())
    await complete_run(ledger_two, run_two)

    denied = await policy_off.finalize_run(run_two.id, now=NOW)
    assert denied is not None
    assert denied.status == DeliveryStatus.FAILED
    assert denied.last_error_code == "CHANNEL_POLICY_DISABLED"

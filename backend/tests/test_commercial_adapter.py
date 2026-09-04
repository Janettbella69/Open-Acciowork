from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from ophira_backend.commercial_actions import (
    CommercialActionConflictError,
    CommercialActionIntegrityError,
    CommercialActionService,
)
from ophira_backend.commercial_adapters import (
    CommercialAdapterError,
    HttpsJsonMessageAdapter,
)
from ophira_backend.connectors import ConnectorRegistry
from ophira_backend.constants import (
    COMMERCIAL_HTTPS_JSON_CONNECTOR_TYPE,
    CommercialActionOperation,
    CommercialActionStatus,
    ExternalExecutionStatus,
)
from ophira_backend.models import (
    CommercialActionApplyRequest,
    CommercialActionCancelRequest,
    CommercialActionReceiptStage,
    CommercialActionReviewRequest,
    CommercialActionStage,
    OrgPolicy,
    ProjectCreate,
)
from ophira_backend.policy import OrgPolicyStore
from ophira_backend.providers import FileSecretVault
from ophira_backend.storage import FileLedger

CONNECTOR_ID = "supplier_outreach"
CONNECTOR_SECRET = "server-only-supplier-credential"


def make_service(
    tmp_path: Path,
    handler,
    *,
    clock=None,
    max_attempts: int = 3,
    dns_resolver=None,
) -> tuple[CommercialActionService, str, str, str, httpx.Client]:
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(
        ProjectCreate(name="Supplier outreach", objective="Send a reviewed inquiry")
    )
    run = ledger.create_run(project.id, "Draft a supplier inquiry")
    session_id = "sdk_session_supplier_outreach"
    ledger.update_run(run.id, sdk_session_id=session_id)

    policy = OrgPolicyStore(tmp_path / "org" / "policy.json")
    policy.save(
        OrgPolicy(
            commercial_actions_enabled=True,
            allowed_commercial_operations=[CommercialActionOperation.SEND_MESSAGE],
            allowed_commercial_connector_ids=[CONNECTOR_ID],
        )
    )
    connectors = ConnectorRegistry(
        tmp_path / "connectors",
        vault=FileSecretVault(
            tmp_path / "secrets",
            filename="connector-secrets.json",
        ),
    )
    connectors.upsert(
        CONNECTOR_ID,
        type=COMMERCIAL_HTTPS_JSON_CONNECTOR_TYPE,
        display_name="Supplier messaging gateway",
        scope="messages:write",
        endpoint="https://commerce.example.test/api",
        secret=CONNECTOR_SECRET,
    )
    assert connectors.health_check(CONNECTOR_ID, environ={}).health == "ok"
    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = HttpsJsonMessageAdapter(
        connectors,
        client=client,
        max_attempts=max_attempts,
        retry_base_seconds=0,
        retry_max_seconds=0,
        sleeper=lambda _seconds: None,
        environ={},
        dns_resolver=dns_resolver
        or (
            lambda *_args, **_kwargs: [
                (2, 1, 6, "", ("93.184.216.34", 443))
            ]
        ),
    )
    service = CommercialActionService(
        tmp_path / "commercial-actions",
        ledger=ledger,
        policy=policy,
        executor=adapter,
        target_reader=adapter,
        clock=clock or (lambda: datetime.now(UTC)),
        execution_lease_seconds=10,
    )
    return service, project.id, run.id, session_id, client


def stage_and_approve(
    service: CommercialActionService,
    project_id: str,
    run_id: str,
    session_id: str,
) -> tuple[str, CommercialActionApplyRequest]:
    staged = service.stage(
        project_id,
        CommercialActionStage.model_validate(
            {
                "source_run_id": run_id,
                "source_session_id": session_id,
                "operation": "send_message",
                "target": {
                    "connector_id": CONNECTOR_ID,
                    "resource_type": "supplier_inquiry",
                    "resource_key": "inquiry-100",
                    "expected_version": "etag-7",
                },
                "observed_state": {
                    "recipient": "supplier@example.test",
                    "content": "Old reviewed draft",
                },
                "changes": [
                    {
                        "field": "content",
                        "before": "Old reviewed draft",
                        "after": "Could you quote 500 units FOB Ningbo?",
                    }
                ],
                "reason": "Send the final inquiry reviewed by the account owner",
                "expires_in_seconds": 3_600,
            }
        ),
    )
    service.review(
        project_id,
        staged.id,
        CommercialActionReviewRequest(
            decision="approve",
            reason="Recipient and final inquiry text confirmed",
            valid_for_seconds=900,
        ),
        actor_id="owner@example.test",
    )
    return staged.id, CommercialActionApplyRequest(
        source_run_id=run_id,
        source_session_id=session_id,
        reason="Apply the reviewed supplier inquiry",
    )


def target_response(request: httpx.Request, **changes: object) -> httpx.Response:
    payload: dict[str, object] = {
        "version": "etag-7",
        "state": {
            "recipient": "supplier@example.test",
            "content": "Old reviewed draft",
        },
    }
    payload.update(changes)
    return httpx.Response(200, json=payload, request=request)


def success_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": "succeeded",
            "idempotency_key": request.headers["Idempotency-Key"],
            "remote_action_id": "remote-message-900",
            "resource_version": "etag-8",
        },
        request=request,
    )


def test_https_message_adapter_applies_once_and_persists_server_receipt(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {CONNECTOR_SECRET}"
        assert request.headers["Host"] == "commerce.example.test"
        assert request.extensions["sni_hostname"] == "commerce.example.test"
        assert request.url.host == "93.184.216.34"
        if request.method == "GET":
            assert request.url.path.endswith(
                "/v1/targets/supplier_inquiry/inquiry-100"
            )
            return target_response(request)
        assert request.url.path.endswith("/v1/messages:send")
        body = json.loads(request.content)
        assert body == {
            "resource_type": "supplier_inquiry",
            "resource_key": "inquiry-100",
            "expected_version": "etag-7",
            "recipient": "supplier@example.test",
            "content": "Could you quote 500 units FOB Ningbo?",
        }
        return success_response(request)

    service, project_id, run_id, session_id, client = make_service(tmp_path, handler)
    try:
        action_id, apply_request = stage_and_approve(
            service, project_id, run_id, session_id
        )
        applied = service.apply(
            project_id,
            action_id,
            apply_request,
            actor_id="owner@example.test",
        )

        assert applied.status == CommercialActionStatus.APPLIED
        execution = applied.execution_request
        assert execution is not None
        assert execution.external_status == ExternalExecutionStatus.SUCCEEDED
        assert applied.idempotency_key is not None
        assert execution.idempotency_key == applied.idempotency_key
        assert execution.receipt is not None
        assert execution.receipt.id.startswith("external_receipt_")
        assert execution.receipt.remote_action_id == "remote-message-900"
        assert execution.receipt.remote_version == "etag-8"
        assert len(execution.receipt.response_hash) == 64
        assert [entry.event for entry in applied.history][-3:] == [
            "execution_requested",
            "execution_started",
            "applied",
        ]

        repeated = service.apply(
            project_id,
            action_id,
            apply_request,
            actor_id="second-owner@example.test",
        )
        assert repeated.model_dump() == applied.model_dump()
        assert len(requests) == 2
        with pytest.raises(CommercialActionConflictError, match="cannot be cancelled"):
            service.cancel(
                project_id,
                action_id,
                CommercialActionCancelRequest(reason="Remote success is irreversible"),
                actor_id="owner@example.test",
            )

        action_file = service.store._path(project_id, action_id)
        assert CONNECTOR_SECRET not in action_file.read_text(encoding="utf-8")
        assert CONNECTOR_SECRET not in (
            tmp_path / "connectors" / f"{CONNECTOR_ID}.json"
        ).read_text(encoding="utf-8")
    finally:
        client.close()


def test_non_message_action_remains_truthfully_unexecuted(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("unsupported action must not reach the adapter")

    service, project_id, run_id, session_id, client = make_service(tmp_path, handler)
    service.policy.save(
        OrgPolicy(
            commercial_actions_enabled=True,
            allowed_commercial_operations=[CommercialActionOperation.CHANGE_PRICE],
            allowed_commercial_connector_ids=[CONNECTOR_ID],
        )
    )
    try:
        staged = service.stage(
            project_id,
            CommercialActionStage.model_validate(
                {
                    "source_run_id": run_id,
                    "source_session_id": session_id,
                    "operation": "change_price",
                    "target": {
                        "connector_id": CONNECTOR_ID,
                        "resource_type": "listing",
                        "resource_key": "sku-1",
                        "expected_version": "etag-1",
                    },
                    "observed_state": {"price": 100},
                    "changes": [{"field": "price", "before": 100, "after": 95}],
                    "reason": "Reviewed price change",
                }
            ),
        )
        service.review(
            project_id,
            staged.id,
            CommercialActionReviewRequest(
                decision="approve",
                reason="Approved",
                valid_for_seconds=900,
            ),
            actor_id="owner@example.test",
        )
        queued = service.apply(
            project_id,
            staged.id,
            CommercialActionApplyRequest(
                source_run_id=run_id,
                source_session_id=session_id,
                reason="No matching adapter is installed",
            ),
            actor_id="owner@example.test",
        )

        assert queued.status == CommercialActionStatus.EXECUTION_REQUESTED
        assert queued.execution_request is not None
        assert queued.execution_request.external_status == ExternalExecutionStatus.NOT_EXECUTED
        assert queued.execution_request.adapter_type is None
        assert requests == []
    finally:
        client.close()


@pytest.mark.parametrize(
    ("target_changes", "error_fragment"),
    [
        ({"version": "etag-8"}, "version changed"),
        (
            {
                "state": {
                    "recipient": "attacker@example.test",
                    "content": "Old reviewed draft",
                }
            },
            "recipient changed",
        ),
        (
            {
                "state": {
                    "recipient": "supplier@example.test",
                    "content": "Externally edited draft",
                }
            },
            "before value",
        ),
    ],
)
def test_external_stale_version_recipient_or_content_blocks_commit(
    tmp_path: Path,
    target_changes: dict[str, object],
    error_fragment: str,
) -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return target_response(request, **target_changes)

    service, project_id, run_id, session_id, client = make_service(tmp_path, handler)
    try:
        action_id, apply_request = stage_and_approve(
            service, project_id, run_id, session_id
        )
        failed = service.apply(
            project_id,
            action_id,
            apply_request,
            actor_id="owner@example.test",
        )

        assert failed.status == CommercialActionStatus.EXECUTION_FAILED
        assert failed.execution_request is not None
        assert (
            failed.execution_request.external_status
            == ExternalExecutionStatus.TERMINAL_FAILED
        )
        assert error_fragment in (failed.execution_request.last_error_message or "")
        assert methods == ["GET"]
    finally:
        client.close()


def test_receipt_read_then_external_version_drift_blocks_commit(tmp_path: Path) -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if len(methods) == 1:
            return target_response(request)
        if request.method == "GET":
            return target_response(request, version="etag-8")
        raise AssertionError("stale receipt-bound action must never POST")

    service, project_id, run_id, session_id, client = make_service(tmp_path, handler)
    try:
        verified = service.observe_target(
            project_id,
            source_run_id=run_id,
            source_session_id=session_id,
            connector_id=CONNECTOR_ID,
            resource_type="supplier_inquiry",
            resource_key="inquiry-100",
        )
        staged = service.stage_from_receipt(
            project_id,
            CommercialActionReceiptStage.model_validate(
                {
                    "source_run_id": run_id,
                    "source_session_id": session_id,
                    "receipt_id": verified.receipt.id,
                    "changes": [
                        {
                            "field": "content",
                            "before": "Old reviewed draft",
                            "after": "Could you quote 500 units FOB Ningbo?",
                        }
                    ],
                    "reason": "Stage only the host-observed supplier inquiry",
                }
            ),
        )
        service.review(
            project_id,
            staged.id,
            CommercialActionReviewRequest(
                decision="approve",
                reason="Recipient and inquiry text confirmed",
                valid_for_seconds=900,
            ),
            actor_id="owner@example.test",
        )
        failed = service.apply(
            project_id,
            staged.id,
            CommercialActionApplyRequest(
                source_run_id=run_id,
                source_session_id=session_id,
                reason="Apply the receipt-bound inquiry",
            ),
            actor_id="owner@example.test",
        )

        assert failed.status == CommercialActionStatus.EXECUTION_FAILED
        assert failed.execution_request is not None
        assert failed.execution_request.last_error_code == "LOCAL_PRECONDITION_CHANGED"
        assert "version changed" in (failed.execution_request.last_error_message or "")
        assert methods == ["GET", "GET"]
    finally:
        client.close()


def test_429_5xx_and_timeout_retry_with_one_idempotency_key(tmp_path: Path) -> None:
    post_requests: list[httpx.Request] = []
    dns_lookups: list[str] = []

    def resolver(hostname, *_args, **_kwargs):
        dns_lookups.append(hostname)
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return target_response(request)
        post_requests.append(request)
        if len(post_requests) == 1:
            raise httpx.ReadTimeout("fixture timeout", request=request)
        if len(post_requests) == 2:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        if len(post_requests) == 3:
            return httpx.Response(503, request=request)
        return success_response(request)

    service, project_id, run_id, session_id, client = make_service(
        tmp_path,
        handler,
        max_attempts=4,
        dns_resolver=resolver,
    )
    try:
        action_id, apply_request = stage_and_approve(
            service, project_id, run_id, session_id
        )
        applied = service.apply(
            project_id,
            action_id,
            apply_request,
            actor_id="owner@example.test",
        )

        assert applied.status == CommercialActionStatus.APPLIED
        assert applied.execution_request is not None
        assert applied.execution_request.receipt is not None
        assert applied.execution_request.receipt.adapter_attempts == 4
        assert len({item.headers["Idempotency-Key"] for item in post_requests}) == 1
        # One lookup for target inspection and one immediately before each POST
        # attempt; no retry relies on an earlier DNS answer.
        assert len(dns_lookups) == 5
    finally:
        client.close()


def test_external_4xx_is_terminal_and_is_not_retried(tmp_path: Path) -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET":
            return target_response(request)
        return httpx.Response(400, json={"error": "rejected"}, request=request)

    service, project_id, run_id, session_id, client = make_service(tmp_path, handler)
    try:
        action_id, apply_request = stage_and_approve(
            service, project_id, run_id, session_id
        )
        failed = service.apply(
            project_id,
            action_id,
            apply_request,
            actor_id="owner@example.test",
        )
        repeated = service.apply(
            project_id,
            action_id,
            apply_request,
            actor_id="owner@example.test",
        )

        assert failed.status == repeated.status == CommercialActionStatus.EXECUTION_FAILED
        assert failed.execution_request is not None
        assert failed.execution_request.last_error_code == "EXTERNAL_HTTP_400"
        assert methods == ["GET", "POST"]
    finally:
        client.close()


def test_external_response_body_is_stream_capped_before_json_decode(
    tmp_path: Path,
) -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            content=b'{"version":"etag-7","state":{"content":"'
            + (b"x" * (64 * 1024))
            + b'"}}',
            request=request,
        )

    service, project_id, run_id, session_id, client = make_service(tmp_path, handler)
    try:
        action_id, apply_request = stage_and_approve(
            service,
            project_id,
            run_id,
            session_id,
        )
        failed = service.apply(
            project_id,
            action_id,
            apply_request,
            actor_id="owner@example.test",
        )

        assert failed.status == CommercialActionStatus.EXECUTION_FAILED
        assert failed.execution_request is not None
        assert failed.execution_request.last_error_code == "EXTERNAL_RESPONSE_TOO_LARGE"
        assert methods == ["GET"]
    finally:
        client.close()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1/api",
        "https://169.254.169.254/latest",
        "https://10.0.0.7/api",
        "https://[::1]/api",
        "https://224.0.0.1/api",
        "https://[ff02::1]/api",
    ],
)
def test_connector_configuration_rejects_non_public_literal_endpoints(
    tmp_path: Path,
    endpoint: str,
) -> None:
    connectors = ConnectorRegistry(tmp_path / "connectors")

    with pytest.raises(ValueError, match="public network destination"):
        connectors.upsert(
            CONNECTOR_ID,
            type=COMMERCIAL_HTTPS_JSON_CONNECTOR_TYPE,
            display_name="Unsafe target",
            scope="messages:write",
            endpoint=endpoint,
        )


@pytest.mark.parametrize(
    "secret_env",
    [
        "PATH",
        "HOME",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPHIRA_COMMERCIAL_APPROVAL_SECRET",
        "OPHIRA_TRUSTED_GATEWAY_AUTH_SECRET",
        "ophira_connector_vendor_token",
    ],
)
def test_connector_configuration_rejects_unsafe_secret_env_names(
    tmp_path: Path,
    secret_env: str,
) -> None:
    connectors = ConnectorRegistry(tmp_path / "connectors")

    with pytest.raises(ValueError, match=r"secret_env must use OPHIRA_CONNECTOR_\*"):
        connectors.upsert(
            CONNECTOR_ID,
            type=COMMERCIAL_HTTPS_JSON_CONNECTOR_TYPE,
            display_name="Supplier gateway",
            scope="messages:write",
            endpoint="https://commerce.example.test/api",
            secret_env=secret_env,
        )


@pytest.mark.parametrize(
    "secret_env",
    [
        "OPHIRA_CONNECTOR_VENDOR_TOKEN",
        "OPHIRA_MEDIA_API_KEY",
        "OPHIRA_SEARCH_API_KEY",
    ],
)
def test_connector_configuration_accepts_server_owned_secret_env_names(
    tmp_path: Path,
    secret_env: str,
) -> None:
    connectors = ConnectorRegistry(tmp_path / "connectors")

    connector = connectors.upsert(
        CONNECTOR_ID,
        type=COMMERCIAL_HTTPS_JSON_CONNECTOR_TYPE,
        display_name="Supplier gateway",
        scope="messages:write",
        endpoint="https://commerce.example.test/api",
        secret_env=secret_env,
    )

    assert connector.secret_env == secret_env


@pytest.mark.parametrize(
    "resource_key",
    [".", "..", "../admin", "/admin", "key%2F..", "key with space", "询盘-1"],
)
def test_target_reader_rejects_path_ambiguous_resource_keys_before_request(
    tmp_path: Path,
    resource_key: str,
) -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return target_response(request)

    service, project_id, run_id, session_id, client = make_service(tmp_path, handler)
    try:
        with pytest.raises(CommercialAdapterError, match="resource key is invalid"):
            service.observe_target(
                project_id,
                source_run_id=run_id,
                source_session_id=session_id,
                connector_id=CONNECTOR_ID,
                resource_type="supplier_inquiry",
                resource_key=resource_key,
            )
        assert methods == []
    finally:
        client.close()


def test_dns_resolution_to_any_private_address_blocks_request_before_credential_use(
    tmp_path: Path,
) -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return target_response(request)

    def mixed_resolver(*_args, **_kwargs):
        return [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ]

    service, project_id, run_id, session_id, client = make_service(
        tmp_path,
        handler,
        dns_resolver=mixed_resolver,
    )
    try:
        action_id, apply_request = stage_and_approve(
            service,
            project_id,
            run_id,
            session_id,
        )
        failed = service.apply(
            project_id,
            action_id,
            apply_request,
            actor_id="owner@example.test",
        )

        assert failed.status == CommercialActionStatus.EXECUTION_FAILED
        assert failed.execution_request is not None
        assert failed.execution_request.last_error_code == "COMMERCIAL_ENDPOINT_NOT_PUBLIC"
        assert methods == []
    finally:
        client.close()


def test_dns_resolution_to_multicast_blocks_request_before_credential_use(
    tmp_path: Path,
) -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return target_response(request)

    service, project_id, run_id, session_id, client = make_service(
        tmp_path,
        handler,
        dns_resolver=lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("224.0.0.1", 443))
        ],
    )
    try:
        action_id, apply_request = stage_and_approve(
            service,
            project_id,
            run_id,
            session_id,
        )
        failed = service.apply(
            project_id,
            action_id,
            apply_request,
            actor_id="owner@example.test",
        )

        assert failed.status == CommercialActionStatus.EXECUTION_FAILED
        assert failed.execution_request is not None
        assert failed.execution_request.last_error_code == "COMMERCIAL_ENDPOINT_NOT_PUBLIC"
        assert methods == []
    finally:
        client.close()


def test_redirect_is_never_followed(tmp_path: Path) -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "http://169.254.169.254/latest/meta-data"},
            request=request,
        )

    service, project_id, run_id, session_id, client = make_service(tmp_path, handler)
    try:
        action_id, apply_request = stage_and_approve(
            service,
            project_id,
            run_id,
            session_id,
        )
        failed = service.apply(
            project_id,
            action_id,
            apply_request,
            actor_id="owner@example.test",
        )

        assert failed.status == CommercialActionStatus.EXECUTION_FAILED
        assert failed.execution_request is not None
        assert failed.execution_request.last_error_code == "EXTERNAL_REDIRECT_DENIED"
        assert len(urls) == 1
        assert "169.254.169.254" not in urls[0]
    finally:
        client.close()


def test_exhausted_5xx_stays_retryable_and_resumes_after_backoff(
    tmp_path: Path,
) -> None:
    current = [datetime(2026, 9, 3, 12, tzinfo=UTC)]
    recover = False
    post_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return target_response(request)
        post_keys.append(request.headers["Idempotency-Key"])
        if not recover:
            return httpx.Response(503, request=request)
        return success_response(request)

    service, project_id, run_id, session_id, client = make_service(
        tmp_path,
        handler,
        clock=lambda: current[0],
        max_attempts=2,
    )
    try:
        action_id, apply_request = stage_and_approve(
            service, project_id, run_id, session_id
        )
        retryable = service.apply(
            project_id,
            action_id,
            apply_request,
            actor_id="owner@example.test",
        )
        assert retryable.status == CommercialActionStatus.EXECUTION_REQUESTED
        assert retryable.execution_request is not None
        assert (
            retryable.execution_request.external_status
            == ExternalExecutionStatus.RETRYABLE_FAILED
        )
        assert retryable.execution_request.last_error_code == "EXTERNAL_HTTP_503"

        # A timeout/5xx may have reached the external service. Cancellation
        # would abandon reconciliation and could hide an already-applied send.
        with pytest.raises(CommercialActionConflictError, match="execution starts"):
            service.cancel(
                project_id,
                action_id,
                CommercialActionCancelRequest(
                    reason="Do not hide an ambiguous external outcome"
                ),
                actor_id="owner@example.test",
            )

        # Calls inside the persisted retry window do not touch the connector.
        same = service.apply(
            project_id,
            action_id,
            apply_request,
            actor_id="owner@example.test",
        )
        assert same.model_dump() == retryable.model_dump()
        assert len(post_keys) == 2

        recover = True
        current[0] += timedelta(seconds=2)
        applied = service.apply(
            project_id,
            action_id,
            apply_request,
            actor_id="owner@example.test",
        )
        assert applied.status == CommercialActionStatus.APPLIED
        assert len(set(post_keys)) == 1
    finally:
        client.close()


def test_policy_is_rechecked_after_remote_inspection_before_commit(tmp_path: Path) -> None:
    methods: list[str] = []
    service: CommercialActionService

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET":
            service.policy.save(OrgPolicy())
            return target_response(request)
        return success_response(request)

    service, project_id, run_id, session_id, client = make_service(tmp_path, handler)
    try:
        action_id, apply_request = stage_and_approve(
            service, project_id, run_id, session_id
        )
        failed = service.apply(
            project_id,
            action_id,
            apply_request,
            actor_id="owner@example.test",
        )

        assert failed.status == CommercialActionStatus.EXECUTION_FAILED
        assert failed.execution_request is not None
        assert failed.execution_request.last_error_code == "LOCAL_PRECONDITION_CHANGED"
        assert methods == ["GET"]
    finally:
        client.close()


def test_connector_revoke_between_inspection_and_commit_blocks_send(tmp_path: Path) -> None:
    methods: list[str] = []
    service: CommercialActionService

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET":
            assert isinstance(service.executor, HttpsJsonMessageAdapter)
            service.executor.connectors.revoke(CONNECTOR_ID)
            return target_response(request)
        return success_response(request)

    service, project_id, run_id, session_id, client = make_service(tmp_path, handler)
    try:
        action_id, apply_request = stage_and_approve(
            service, project_id, run_id, session_id
        )
        failed = service.apply(
            project_id,
            action_id,
            apply_request,
            actor_id="owner@example.test",
        )

        assert failed.status == CommercialActionStatus.EXECUTION_FAILED
        assert failed.execution_request is not None
        assert failed.execution_request.last_error_code == "COMMERCIAL_CONNECTOR_DISABLED"
        assert methods == ["GET"]
    finally:
        client.close()


def test_concurrent_double_apply_never_issues_a_second_commit(tmp_path: Path) -> None:
    inspecting = threading.Event()
    continue_inspection = threading.Event()
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.method == "GET":
            inspecting.set()
            assert continue_inspection.wait(timeout=2)
            return target_response(request)
        post_count += 1
        return success_response(request)

    service, project_id, run_id, session_id, client = make_service(tmp_path, handler)
    action_id, apply_request = stage_and_approve(
        service, project_id, run_id, session_id
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                service.apply,
                project_id,
                action_id,
                apply_request,
                actor_id="owner@example.test",
            )
            assert inspecting.wait(timeout=2)
            concurrent = service.apply(
                project_id,
                action_id,
                apply_request,
                actor_id="second-owner@example.test",
            )
            assert concurrent.execution_request is not None
            assert (
                concurrent.execution_request.external_status
                == ExternalExecutionStatus.ATTEMPTING
            )
            continue_inspection.set()
            applied = first.result(timeout=2)
        assert applied.status == CommercialActionStatus.APPLIED
        assert post_count == 1
    finally:
        continue_inspection.set()
        client.close()


def test_remote_success_with_local_commit_failure_recovers_with_same_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [datetime(2026, 9, 3, 12, tzinfo=UTC)]
    post_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return target_response(request)
        post_keys.append(request.headers["Idempotency-Key"])
        return success_response(request)

    service, project_id, run_id, session_id, client = make_service(
        tmp_path,
        handler,
        clock=lambda: current[0],
    )
    action_id, apply_request = stage_and_approve(
        service, project_id, run_id, session_id
    )
    original_write = service.store.write_unlocked
    fail_once = True

    def fail_final_receipt_write(record) -> None:
        nonlocal fail_once
        if fail_once and record.status == CommercialActionStatus.APPLIED:
            fail_once = False
            raise CommercialActionIntegrityError("simulated final ledger failure")
        original_write(record)

    monkeypatch.setattr(service.store, "write_unlocked", fail_final_receipt_write)
    try:
        with pytest.raises(CommercialActionIntegrityError, match="simulated"):
            service.apply(
                project_id,
                action_id,
                apply_request,
                actor_id="owner@example.test",
            )
        uncertain = service.get(project_id, action_id)
        assert uncertain.execution_request is not None
        assert (
            uncertain.execution_request.external_status
            == ExternalExecutionStatus.ATTEMPTING
        )
        idempotency_key = uncertain.execution_request.idempotency_key
        assert idempotency_key is not None

        current[0] += timedelta(seconds=11)
        recovered = service.apply(
            project_id,
            action_id,
            apply_request,
            actor_id="owner@example.test",
        )

        assert recovered.status == CommercialActionStatus.APPLIED
        assert post_keys == [idempotency_key, idempotency_key]
        assert recovered.execution_request is not None
        assert recovered.execution_request.attempt_count == 2
        assert recovered.execution_request.receipt is not None
    finally:
        client.close()

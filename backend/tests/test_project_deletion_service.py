"""Reviewed erasure must survive failures without recreating deleted content."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from ophira_backend.auth import ProjectAccessStore
from ophira_backend.event_triggers import EventTriggerStore, ShopifySubscriptionRecord
from ophira_backend.models import ProjectCreate, utc_now
from ophira_backend.project_deletion import ProjectDeletionService
from ophira_backend.project_deletion_capture import ProjectDeletionConflict
from ophira_backend.project_deletion_models import ProjectDeletionRequest
from ophira_backend.project_lifecycle import ProjectUnavailableError
from ophira_backend.storage import FileLedger, LedgerNotFoundError


def setup(root):
    ledger = FileLedger(root)
    project = ledger.create_project(ProjectCreate(name="private name canary", objective="erase"))
    other = ledger.create_project(ProjectCreate(name="keep other project", objective="keep"))
    run = ledger.create_run(project.id, "private prompt canary")
    ledger.save_upload(project.id, "report.txt", b"private upload canary")
    service = ProjectDeletionService(ledger, ProjectAccessStore(root / "auth" / "projects"))
    return ledger, service, project, other, run


def request_for(service, project):
    preview = service.preview(project.id, None)
    assert preview.can_delete, preview
    return ProjectDeletionRequest(
        operation_id=str(uuid4()), fingerprint=preview.fingerprint,
        confirmation_name=project.name, reason="private deletion reason canary",
    )


def test_preview_complete_erasure_idempotency_and_minimal_durable_receipt(tmp_path):
    ledger, service, project, other, run = setup(tmp_path)
    payload = request_for(service, project)
    result = service.start(project.id, payload, None)
    assert result.status == "deleted", result
    assert result.can_retry is False
    assert service.start(project.id, payload, None) == result
    assert service.retry(project.id, payload.operation_id, None) == result
    assert service.get(project.id, payload.operation_id, None) == result
    with pytest.raises(LedgerNotFoundError):
        ledger.get_project(project.id)
    with pytest.raises(LedgerNotFoundError):
        ledger.get_run(run.id)
    assert ledger.get_project(other.id).name == other.name
    assert not (tmp_path / "projects" / project.id).exists()
    for path in (tmp_path / "project-deletions" / project.id).iterdir():
        content = path.read_text()
        for canary in (project.name, run.prompt, "private upload canary", payload.reason):
            assert canary not in content
    with pytest.raises(ProjectUnavailableError):
        ledger.create_project(ProjectCreate(name="recreate", objective="no"), project_id=project.id)


@pytest.mark.parametrize("status,disclosed", [
    ("attempting", True), ("uncertain", True),
    ("confirmed", True), ("rejected", False),
])
def test_project_deletion_discloses_remote_shopify_subscription_boundary(
    tmp_path, status, disclosed,
):
    ledger, service, project, _other, _run = setup(tmp_path)
    triggers = EventTriggerStore(ledger)
    triggers.save_subscription(ShopifySubscriptionRecord(
        project_id=project.id, trigger_id="event_trigger_1",
        uri=f"https://events.example.com/api/projects/{project.id}"
            "/event-triggers/event_trigger_1/shopify",
        status=status,
        subscription_id=(
            "gid://shopify/WebhookSubscription/123" if status == "confirmed" else None
        ),
        updated_at=utc_now(),
    ))
    preview = service.preview(project.id, None)
    assert preview.can_delete
    assert any("Shopify order webhook" in item.message for item in preview.retained) is disclosed
    triggers.clear_subscription(project.id, "event_trigger_1")
    assert not any(
        "Shopify order webhook" in item.message
        for item in service.preview(project.id, None).retained
    )


@pytest.mark.parametrize("change", ["content", "confirmation"])
def test_changed_review_never_commits_deletion(tmp_path, change):
    ledger, service, project, _other, _run = setup(tmp_path)
    payload = request_for(service, project)
    if change == "content":
        ledger.save_upload(project.id, "late.txt", b"new")
    else:
        payload.confirmation_name = "incorrect name"
    with pytest.raises(ProjectDeletionConflict):
        service.start(project.id, payload, None)
    assert service.lifecycle.read_state(project.id) is None
    assert ledger.get_project(project.id).id == project.id


def test_active_operation_blocks_without_tombstone_and_releases_after_completion(tmp_path):
    ledger, service, project, _other, _run = setup(tmp_path)
    ready, finish = threading.Event(), threading.Event()

    def active():
        with ledger.lifecycle.lease(project.id):
            ready.set()
            assert finish.wait(10)

    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(active)
        try:
            assert ready.wait(5)
            preview = service.preview(project.id, None)
            assert not preview.can_delete
            assert preview.blockers[0].code == "project_active"
            assert not ledger.lifecycle.has_deletion(project.id)
        finally:
            finish.set()
            future.result(5)
    assert service.preview(project.id, None).can_delete


def test_failure_keeps_content_unavailable_then_new_service_recovers(tmp_path, monkeypatch):
    import ophira_backend.project_deletion as module

    ledger, service, project, other, _run = setup(tmp_path)
    payload = request_for(service, project)
    purge = module.purge_project_deletion

    def interrupted(_root, _plan):
        raise OSError("synthetic disk interruption contains private canary")

    monkeypatch.setattr(module, "purge_project_deletion", interrupted)
    result = service.start(project.id, payload, None)
    assert result.status == "blocked" and result.can_retry
    assert "private canary" not in result.model_dump_json()
    with pytest.raises(LedgerNotFoundError):
        ledger.save_upload(project.id, "late.txt", b"do not resurrect")
    assert ledger.get_project(other.id).id == other.id
    monkeypatch.setattr(module, "purge_project_deletion", purge)
    recovered = ProjectDeletionService(FileLedger(tmp_path), service.access)
    recovered.recover()
    assert recovered.get(project.id, payload.operation_id, None).status == "deleted"
    assert not (tmp_path / "projects" / project.id).exists()


def test_unconfirmed_operation_id_and_mutated_request_cannot_replace_intent(tmp_path):
    _ledger, service, project, _other, _run = setup(tmp_path)
    payload = request_for(service, project)
    with pytest.raises(LedgerNotFoundError):
        service.get(project.id, payload.operation_id, None)
    service.start(project.id, payload, None)
    changed = payload.model_copy(update={"reason": "different"})
    with pytest.raises(ProjectDeletionConflict):
        service.start(project.id, changed, None)
    with pytest.raises(LedgerNotFoundError):
        service.start(project.id, payload.model_copy(update={"operation_id": str(uuid4())}), None)
    state = json.loads((tmp_path / "project-deletions" / project.id / "state.json").read_text())
    assert state["operation_id"] == payload.operation_id


def test_legacy_notification_anchor_cannot_recreate_project_content(tmp_path):
    from ophira_backend.automation import NotificationNotFoundError, NotificationStore
    from ophira_backend.models import Notification

    _ledger, service, project, _other, run = setup(tmp_path)
    inbox = NotificationStore(tmp_path / "notifications")
    legacy = Notification(title="legacy private notification", run_id=run.id)
    inbox._write(legacy)
    assert inbox.get(legacy.id).project_id == project.id
    payload = request_for(service, project)
    assert service.start(project.id, payload, None).status == "deleted"
    with pytest.raises(NotificationNotFoundError):
        inbox._write(legacy)
    assert not inbox._path(legacy.id).exists()
    assert inbox.add(title="Independent global inbox message").project_id is None

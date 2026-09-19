"""Temporary-only fault/concurrency coverage for project-owned background work."""
# ruff: noqa: F811 -- imported pytest fixture

import asyncio
import base64
import multiprocessing
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from test_workspace_checkpoints import change_workspace, review
from test_workspace_checkpoints import source as workspace_source  # noqa: F401
from test_workspace_recovery_process import die

from ophira_backend.auth import ProjectAccessStore
from ophira_backend.automation import IntentStore, NotificationStore
from ophira_backend.channel_delivery import (
    ChannelDeliveryService,
    ChannelDeliveryStore,
    DeliveryStatus,
    FeishuReplyResult,
    FeishuReplyTarget,
)
from ophira_backend.config import Settings
from ophira_backend.constants import MemoryKind, MemoryScope, MemorySource, RunStatus
from ophira_backend.feedback import FeedbackStore
from ophira_backend.grants import PermissionGrantStore
from ophira_backend.memory import FileMemoryProvider
from ophira_backend.models import (
    FeedbackCreate,
    ProjectCreate,
    StandingIntentCreate,
    new_id,
    utc_now,
)
from ophira_backend.project_lifecycle import (
    ProjectLifecycle,
    ProjectLifecycleBusyError,
    ProjectUnavailableError,
)
from ophira_backend.push_service import PushDeliveryService
from ophira_backend.push_store import PushStore
from ophira_backend.push_transport import PushOutcome, PushSubscription
from ophira_backend.storage import FileLedger, LedgerNotFoundError
from ophira_backend.workspace_checkpoints import PENDING_RESTORE, WorkspaceCheckpoints


def delete_intent(root, project_id):
    lifecycle = ProjectLifecycle(root)
    with lifecycle.lease(project_id, exclusive=True):
        lifecycle.write_state(project_id, {
            "schema_version": 1, "project_id": project_id, "operation_id": "fixture_deletion",
            "status": "deleting", "plan": {},
        })


def project_at(root):
    ledger = FileLedger(root)
    project = ledger.create_project(ProjectCreate(name="Background", objective="Safe deletion"))
    return ledger, project


class PausedDelivery:
    configured = True
    enabled = True
    public_key = "fixture-public-key"

    def __init__(self):
        self.started, self.finish = asyncio.Event(), asyncio.Event()
        self.cancelling = asyncio.Event()
        self.calls = 0

    async def pause(self):
        self.calls += 1
        self.started.set()
        try:
            await self.finish.wait()
        except asyncio.CancelledError:
            self.cancelling.set()
            await self.finish.wait()  # Simulate transport/context shutdown still in progress.
            raise

    async def reply(self, target, content, *, idempotency_key):
        await self.pause()
        return FeishuReplyResult(message_id="om_delivered_fixture", chat_id=target.chat_id)

    async def send(self, subscription, topic):
        await self.pause()
        return PushOutcome("accepted", "service_accepted")


async def finish_delivery(task, adapter, ledger, project_id, cancel):
    await asyncio.wait_for(adapter.started.wait(), 5)
    if cancel:
        task.cancel()
        await asyncio.wait_for(adapter.cancelling.wait(), 5)
    try:
        # A different task/OS file descriptor must not inherit the sender's SH lease.
        with pytest.raises(ProjectLifecycleBusyError), ledger.lifecycle.lease(
            project_id, exclusive=True,
        ):
            pass
    finally:
        adapter.finish.set()
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    with ledger.lifecycle.lease(project_id, exclusive=True):
        pass


@pytest.mark.parametrize("cancel", [False, True])
async def test_channel_send_and_transport_cleanup_hold_project_lease(tmp_path, cancel):
    ledger, project = project_at(tmp_path)
    run = ledger.create_run(project.id, "Fixture channel request")
    store = ChannelDeliveryStore(tmp_path / "channel-deliveries")
    adapter = PausedDelivery()
    service = ChannelDeliveryService(ledger, store, adapter)
    target = FeishuReplyTarget(
        event_id="evt_fixture", message_id="om_fixture", chat_id="oc_fixture", chat_type="group",
    )
    record = service.bind_run(run, target)
    record = store.attach_result(record.id, "Project-private queued reply")
    task = asyncio.create_task(service._attempt(record.id))
    await finish_delivery(task, adapter, ledger, project.id, cancel)
    before = store.get(record.id)
    if not cancel:
        assert before.status == DeliveryStatus.DELIVERED
    delete_intent(tmp_path, project.id)
    # Startup reconciliation must neither recover a cancelled in-flight attempt
    # nor resend a durable reply belonging to a deleted project.
    restarted = ChannelDeliveryService(ledger, ChannelDeliveryStore(store.root), adapter)
    await restarted.reconcile(now=utc_now() + timedelta(seconds=10))
    await restarted._attempt(record.id)
    assert adapter.calls == 1
    store._path(record.id).unlink()
    with pytest.raises(ProjectUnavailableError):
        store._write(before)
    assert not store._path(record.id).exists()


def subscription():
    key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
    )
    def encoded(value):
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    return PushSubscription(endpoint="https://push.example.test/capability-fixture", keys={
        "p256dh": encoded(key), "auth": encoded(b"fixture-auth-key"),
    })


@pytest.mark.parametrize("cancel", [False, True])
async def test_push_send_and_cleanup_hold_each_project_lease(tmp_path, monkeypatch, cancel):
    ledger, project = project_at(tmp_path)
    other = ledger.create_project(ProjectCreate(name="Other", objective="Keep active"))
    store = PushStore(tmp_path / "push")
    adapter = PausedDelivery()
    device_id = new_id("pair")
    store.subscribe(device_id, subscription(), adapter.public_key)
    notifications = NotificationStore(tmp_path / "notifications")
    notifications.add(title="Project update", project_id=project.id)
    service = PushDeliveryService(
        Settings(_env_file=None, data_dir=tmp_path), store, adapter, SimpleNamespace(), ledger,
        ProjectAccessStore(tmp_path / "auth" / "projects"),
        SimpleNamespace(list_pending=lambda: []), notifications,
    )
    # Device authorization itself is exercised by test_companion_push. Here a
    # deliberately stale authorized-project snapshot exercises the deletion gate.
    monkeypatch.setattr(service, "_projects", lambda _device: {project.id, other.id})
    task = asyncio.create_task(service._deliver(device_id, utc_now()))
    await asyncio.wait_for(adapter.started.wait(), 5)
    with pytest.raises(ProjectLifecycleBusyError), ledger.lifecycle.lease(other.id, exclusive=True):
        pass
    await finish_delivery(task, adapter, ledger, project.id, cancel)
    delete_intent(tmp_path, project.id)
    await service._deliver(device_id, utc_now() + timedelta(minutes=1))
    assert adapter.calls == 1
    with ledger.lifecycle.lease(other.id, exclusive=True):
        pass  # Failed multi-project acquisition leaked no unrelated-project lease.


def store_writer(root, kind):
    ledger, project = project_at(root)
    if kind == "notification":
        store = NotificationStore(root / "notifications")
        def create():
            return store.add(title="Fixture", body="private body", project_id=project.id)
    elif kind == "intent":
        store = IntentStore(root / "intents")
        def create():
            return store.create(StandingIntentCreate(
                project_id=project.id, intent="Notify me when the fixture changes",
            ))
    elif kind == "grant":
        store = PermissionGrantStore(root / "grants")
        def create():
            return store.record(
                tool_name="Read", tool_input={"file_path": "/fixture/source.txt"},
                project_id=project.id,
            )
    else:
        store = FeedbackStore(root / "feedback")
        run = ledger.create_run(project.id, "Fixture failure")
        ledger.update_run(run.id, status=RunStatus.FAILED)
        def create():
            return store.add(FeedbackCreate(
                run_id=run.id, kind="run_failure", user_note="Fixture failure detail",
            ), ledger=ledger)
    return ledger, project, store, create


@pytest.mark.parametrize("kind", ["notification", "intent", "grant", "feedback"])
def test_project_store_write_holds_lease_until_atomic_publication(tmp_path, monkeypatch, kind):
    ledger, project, store, create = store_writer(tmp_path, kind)
    ready, finish = threading.Event(), threading.Event()
    original = os.replace

    def publish(source, destination, *args, **kwargs):
        if Path(destination).parent == store.root:
            ready.set()
            assert finish.wait(10)
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", publish)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(create)
        assert ready.wait(5)
        try:
            with pytest.raises(ProjectLifecycleBusyError), ledger.lifecycle.lease(
                project.id, exclusive=True,
            ):
                pass
        finally:
            finish.set()
        record = pending.result(timeout=10)
    assert store._path(record.id).exists()


@pytest.mark.parametrize("kind", ["notification", "intent", "grant", "feedback"])
def test_stale_project_store_write_cannot_publish_after_deletion(tmp_path, monkeypatch, kind):
    _, project, store, create = store_writer(tmp_path, kind)
    ready, finish = threading.Event(), threading.Event()
    original = store._write

    def stale(item):
        ready.set()
        assert finish.wait(10)
        return original(item)

    monkeypatch.setattr(store, "_write", stale)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(create)
        assert ready.wait(5)
        try:
            delete_intent(tmp_path, project.id)
        finally:
            finish.set()
        with pytest.raises(ProjectUnavailableError):
            pending.result(timeout=10)
    assert not list(store.root.glob("*.json"))
    assert not list(store.root.glob("*.tmp"))


def paused_memory_write(root, project_id, ready, finish):
    provider = FileMemoryProvider(Path(root) / "memory")
    original = provider._append_history

    def append(item, action):
        ready.set()  # The scope's items snapshot is durable, history not yet appended.
        assert finish.wait(20)
        original(item, action)

    provider._append_history = append
    provider.propose(
        scope=MemoryScope.PROJECT, scope_id=project_id, text="Private interrupted memory",
        kind=MemoryKind.FACT, source=MemorySource.USER,
    )


def test_memory_scope_real_process_crash_releases_lease_but_cannot_revive_deleted_scope(tmp_path):
    ledger, project = project_at(tmp_path)
    provider = FileMemoryProvider(tmp_path / "memory")
    other = provider.propose(
        scope=MemoryScope.PROJECT, scope_id="other_project", text="Retained memory",
        kind=MemoryKind.FACT, source=MemorySource.USER,
    )
    context = multiprocessing.get_context("spawn")
    ready, finish = context.Event(), context.Event()
    child = context.Process(target=paused_memory_write, args=(tmp_path, project.id, ready, finish))
    child.start()
    try:
        assert ready.wait(10)
        with pytest.raises(ProjectLifecycleBusyError), ledger.lifecycle.lease(
            project.id, exclusive=True,
        ):
            pass
        child.kill()
        child.join(10)
        assert not child.is_alive()
        delete_intent(tmp_path, project.id)
        scope = provider.root / "project" / project.id
        shutil.rmtree(scope)
        restarted = FileMemoryProvider(provider.root)
        with pytest.raises(ProjectUnavailableError):
            provider.propose(
                scope=MemoryScope.PROJECT, scope_id=project.id, text="Late body",
                kind=MemoryKind.FACT, source=MemorySource.USER,
            )
        assert restarted.get(other.id).text == "Retained memory"
        assert not scope.exists()
    finally:
        if child.is_alive():
            child.kill()
            child.join(5)


def test_memory_constructor_skips_recovery_writes_for_tombstoned_projects(tmp_path, monkeypatch):
    _, project = project_at(tmp_path)
    provider = FileMemoryProvider(tmp_path / "memory")
    item = provider.propose(
        scope=MemoryScope.PROJECT, scope_id=project.id, text="Pre-deletion memory",
        kind=MemoryKind.FACT, source=MemorySource.USER,
    )
    delete_intent(tmp_path, project.id)
    original = FileMemoryProvider._discard_scope_staging

    def check(self, scope, scope_id):
        assert scope_id != project.id, "startup touched a deleted project's recoverable content"
        return original(self, scope, scope_id)

    monkeypatch.setattr(FileMemoryProvider, "_discard_scope_staging", check)
    restarted = FileMemoryProvider(provider.root)
    with pytest.raises(KeyError):
        restarted.correct(item.id, text="Restored deleted body")


def test_workspace_crash_recovery_cannot_swap_or_recreate_a_tombstoned_project(workspace_source):
    ledger, project_id, _, _, _ = workspace_source
    workspace = change_workspace(workspace_source)
    preview = review(workspace_source)
    child = multiprocessing.get_context("spawn").Process(
        target=die, args=(str(ledger.root), project_id, preview.id, "first_swap"),
    )
    child.start()
    try:
        child.join(15)
        assert child.exitcode == 73
        assert not workspace.exists()
        pending = workspace.parent / PENDING_RESTORE
        assert pending.exists()
        before = pending.read_bytes()
        delete_intent(ledger.root, project_id)
        restarted = FileLedger(ledger.root)
        with pytest.raises(ProjectUnavailableError):
            WorkspaceCheckpoints(restarted).recover(project_id)
        assert not workspace.exists() and pending.read_bytes() == before
        shutil.rmtree(workspace.parent)
        with pytest.raises(LedgerNotFoundError):
            restarted.workspace_for_project(project_id)
        assert not workspace.parent.exists()
    finally:
        if child.is_alive():
            child.kill()
            child.join(5)

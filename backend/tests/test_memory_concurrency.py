"""Transactions keep independent memory writers from reviving erased content."""

import hashlib
import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event

import pytest

from ophira_backend import constants
from ophira_backend.constants import MemoryKind, MemoryScope, MemorySource
from ophira_backend.memory import FileMemoryProvider, MemoryNotFoundError
from ophira_backend.models import utc_now


def propose(store, text):
    return store.propose(
        scope=MemoryScope.AGENT, scope_id="agent_fixture", text=text,
        kind=MemoryKind.FACT, source=MemorySource.USER,
    )


def pause_write(store, started, release):
    write = store._write_items

    def paused(*args):
        started.set()
        assert release.wait(10), "writer was not released"
        return write(*args)

    store._write_items = paused


def process_propose(root, ready, done):
    ready.set()
    propose(FileMemoryProvider(Path(root)), "second proposal")
    done.set()


def process_delete(root, agents, item_id, ready, done):
    ready.set()
    FileMemoryProvider(Path(root), Path(agents)).delete(
        item_id, deleted_by="owner", reason="erase fixture",
    )
    done.set()


def process_interrupted_delete(root, agents, item_id, started):
    store = FileMemoryProvider(Path(root), Path(agents))
    replace = os.replace

    def stop_before_publish(source, destination):
        if Path(destination).name == "items.json":
            # The tombstone and scrubbed replacement are fsynced, but the old
            # published snapshot/history/marker still contain the erased text.
            started.set()
            Event().wait(30)
        return replace(source, destination)

    os.replace = stop_before_publish
    store.delete(item_id, deleted_by="owner", reason="erase fixture")


@pytest.mark.parametrize("separate_process", [False, True])
def test_concurrent_proposals_preserve_both_items(tmp_path, separate_process):
    root = tmp_path / "memory"
    first = FileMemoryProvider(root)
    second = FileMemoryProvider(root)
    started, release = Event(), Event()
    pause_write(first, started, release)
    context = multiprocessing.get_context("spawn")
    ready = context.Event() if separate_process else Event()
    done = context.Event() if separate_process else Event()
    process = None
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_result = executor.submit(propose, first, "first proposal")
        assert started.wait(5)
        if separate_process:
            process = context.Process(target=process_propose, args=(root, ready, done))
            process.start()
            second_result = None
        else:
            def other():
                ready.set()
                result = propose(second, "second proposal")
                done.set()
                return result

            second_result = executor.submit(other)
        try:
            assert ready.wait(5)
            # The peer reaches its transaction while the first snapshot is paused.
            done.wait(0.2)
        finally:
            release.set()
        first_result.result(timeout=5)
        if second_result:
            second_result.result(timeout=5)
    if process:
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
        assert process.exitcode == 0
    assert {item.text for item in second.list()} == {"first proposal", "second proposal"}
    rows = [json.loads(line) for line in (
        root / "agent/agent_fixture/history.jsonl"
    ).read_text().splitlines()]
    assert {row["item"]["text"] for row in rows} == {"first proposal", "second proposal"}


@pytest.mark.parametrize("separate_process", [False, True])
@pytest.mark.parametrize("operation", ["correct", "rollback"])
def test_delete_drains_old_mutation_and_scrubs_every_content_copy(
    tmp_path, separate_process, operation,
):
    root, agents = tmp_path / "memory", tmp_path / "agents"
    core = agents / "agent_fixture"
    core.mkdir(parents=True)
    (core / "MEMORY.md").write_text("User's independent note\n")
    first = FileMemoryProvider(root, agents)
    second = FileMemoryProvider(root, agents)
    item = propose(first, "private old fixture")
    if operation == "rollback":
        first.correct(item.id, text="private revised fixture")
    started, release = Event(), Event()
    pause_write(first, started, release)
    context = multiprocessing.get_context("spawn")
    ready = context.Event() if separate_process else Event()
    done = context.Event() if separate_process else Event()
    process = None
    with ThreadPoolExecutor(max_workers=2) as executor:
        correction = (
            executor.submit(first.correct, item.id, text="private corrected fixture")
            if operation == "correct" else executor.submit(first.rollback, item.id, 1)
        )
        assert started.wait(5)
        if separate_process:
            process = context.Process(
                target=process_delete, args=(root, agents, item.id, ready, done),
            )
            process.start()
            deletion = None
        else:
            def erase():
                ready.set()
                result = second.delete(item.id, deleted_by="owner", reason="erase fixture")
                done.set()
                return result

            deletion = executor.submit(erase)
        try:
            assert ready.wait(5)
            done.wait(0.2)
        finally:
            release.set()
        updated = correction.result(timeout=5)
        if deletion:
            deletion.result(timeout=5)
    if process:
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
        assert process.exitcode == 0
    assert second.list() == []
    for path in [root / "agent/agent_fixture/items.json",
                 root / "agent/agent_fixture/history.jsonl", core / "MEMORY.md"]:
        assert "private" not in path.read_text()
    assert "User's independent note" in (core / "MEMORY.md").read_text()
    tombstone, = second.list_tombstones()
    assert tombstone.last_version == updated.version
    assert tombstone.content_sha256 == hashlib.sha256(updated.text.encode()).hexdigest()


def test_scope_lock_is_reentrant_across_provider_instances(tmp_path):
    first = FileMemoryProvider(tmp_path)
    second = FileMemoryProvider(tmp_path)
    with first._lock_for("agent:agent_fixture"), second._lock_for("agent:agent_fixture"):
        item = propose(second, "nested transaction fixture")
    assert first.get(item.id) == item


def test_delete_audits_latest_item_after_scope_discovery(tmp_path):
    editing, deleting = FileMemoryProvider(tmp_path), FileMemoryProvider(tmp_path)
    item = propose(editing, "original fixture")
    started, release = Event(), Event()
    get = deleting.get

    def pause_discovery(memory_id):
        value = get(memory_id)
        started.set()
        assert release.wait(5)
        return value

    deleting.get = pause_discovery
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(deleting.delete, item.id, deleted_by="owner", reason="erase")
        try:
            assert started.wait(5)
            current = editing.correct(item.id, text="corrected before transaction")
        finally:
            release.set()
        tombstone = pending.result(timeout=5)
    assert tombstone.last_version == current.version
    assert tombstone.content_sha256 == hashlib.sha256(current.text.encode()).hexdigest()


def test_recovery_cannot_overwrite_a_concurrent_proposal(tmp_path):
    peer = FileMemoryProvider(tmp_path)
    item = propose(peer, "erased fixture")
    peer.delete(item.id, deleted_by="owner", reason="erase")
    scope = tmp_path / "agent/agent_fixture"
    # Crash window: durable tombstone, old snapshot not yet scrubbed.
    (scope / "items.json").write_text(json.dumps([item.model_dump(mode="json")]))
    started, release, other_started, done = Event(), Event(), Event(), Event()

    class PausedRecovery(FileMemoryProvider):
        def _write_items(self, *args):
            started.set()
            assert release.wait(5)
            return super()._write_items(*args)

    with ThreadPoolExecutor(max_workers=2) as executor:
        recovery = executor.submit(PausedRecovery, tmp_path)
        assert started.wait(5)

        def create():
            other_started.set()
            result = propose(peer, "new surviving fixture")
            done.set()
            return result

        creation = executor.submit(create)
        try:
            assert other_started.wait(5)
            done.wait(0.2)
        finally:
            release.set()
        recovery.result(timeout=5)
        created = creation.result(timeout=5)
    assert peer.list() == [created]
    assert "erased fixture" not in (scope / "history.jsonl").read_text()


def test_killed_process_releases_scope_lock_and_restart_finishes_erasure(tmp_path):
    root, agents = tmp_path / "memory", tmp_path / "agents"
    core = agents / "agent_fixture"
    core.mkdir(parents=True)
    store = FileMemoryProvider(root, agents)
    item = propose(store, "private interrupted erasure fixture")
    scope = root / "agent/agent_fixture"
    # Previous releases also left fixed-name staging after interrupted writes.
    (scope / "items.json.tmp").write_text(item.model_dump_json())
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    process = context.Process(
        target=process_interrupted_delete, args=(root, agents, item.id, started),
    )
    process.start()
    try:
        assert started.wait(10)
        assert "private interrupted" in (scope / "items.json").read_text()
        assert item.id in (scope / "tombstones.jsonl").read_text()
    finally:
        process.kill()
        process.join(5)
    assert process.exitcode != 0
    recovered = FileMemoryProvider(root, agents)
    assert recovered.list() == []
    with pytest.raises(MemoryNotFoundError):
        recovered.rollback(item.id, 1)
    for directory in (scope, core):
        for path in directory.iterdir():
            assert "private interrupted" not in path.read_text()
            assert not path.name.startswith(".ophira-memory-")
    assert not (scope / "items.json.tmp").exists()
    assert propose(recovered, "new fixture after recovery").text == "new fixture after recovery"


@pytest.mark.parametrize("operation", ["propose", "rollback", "renew", "clear"])
def test_all_approved_writes_enforce_existing_capacity(tmp_path, monkeypatch, operation):
    store = FileMemoryProvider(tmp_path)
    item = propose(store, "candidate fixture")
    if operation == "rollback":
        store.decide(item.id, "disable")
    elif operation in {"renew", "clear"}:
        store.set_expiry(item.id, utc_now() + timedelta(minutes=1))
        later = utc_now() + timedelta(minutes=2)
        monkeypatch.setattr("ophira_backend.memory.utc_now", lambda: later)
    else:
        store.delete(item.id, deleted_by="owner", reason="prepare fixture")
    active = propose(store, "existing approved fixture")
    monkeypatch.setattr(constants, "APPROVED_MEMORY_CAP", 1)
    before = (tmp_path / "agent/agent_fixture/items.json").read_bytes()
    with pytest.raises(ValueError, match="稳定记忆已达上限"):
        if operation == "propose":
            propose(store, "another fixture")
        elif operation == "rollback":
            store.rollback(item.id, 1)
        else:
            store.set_expiry(item.id, None if operation == "clear" else later + timedelta(days=1))
    assert (tmp_path / "agent/agent_fixture/items.json").read_bytes() == before
    store.decide(active.id, "disable")
    if operation == "propose":
        propose(store, "allowed after disabling")
    elif operation == "rollback":
        store.rollback(item.id, 1)
    else:
        store.set_expiry(item.id, None)
    assert len(store.search()) == 1


def test_approval_capacity_check_serializes_with_other_instance(tmp_path, monkeypatch):
    first, second = FileMemoryProvider(tmp_path), FileMemoryProvider(tmp_path)
    def candidate(text):
        return first.propose(
            scope=MemoryScope.AGENT, scope_id="agent_fixture", text=text,
            kind=MemoryKind.FACT, source=MemorySource.AGENT_PROPOSAL,
        )

    one, two = candidate("first draft"), candidate("second draft")
    monkeypatch.setattr(constants, "APPROVED_MEMORY_CAP", 1)
    started, release, other_started = Event(), Event(), Event()
    pause_write(first, started, release)
    with ThreadPoolExecutor(max_workers=2) as executor:
        accepted = executor.submit(first.decide, one.id, "approve")
        assert started.wait(5)

        def approve_other():
            other_started.set()
            return second.decide(two.id, "approve")

        refused = executor.submit(approve_other)
        try:
            assert other_started.wait(5)
        finally:
            release.set()
        accepted.result(timeout=5)
        with pytest.raises(ValueError, match="稳定记忆已达上限"):
            refused.result(timeout=5)
    assert [item.id for item in first.search()] == [one.id]


def test_correction_checks_character_capacity_without_publishing_partial_version(
    tmp_path, monkeypatch,
):
    store = FileMemoryProvider(tmp_path)
    item = propose(store, "short")
    monkeypatch.setattr(constants, "APPROVED_MEMORY_CHARS", 5)
    with pytest.raises(ValueError, match="稳定记忆已达上限"):
        store.correct(item.id, text="too many characters")
    assert store.get(item.id) == item

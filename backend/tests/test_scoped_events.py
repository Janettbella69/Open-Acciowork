"""Offline scoped-event durability, replay, lifecycle and isolation contracts."""

import json
import multiprocessing
import os
import shutil
import stat
from pathlib import Path

import pytest

from ophira_backend.constants import EventType
from ophira_backend.models import EventActor, ProjectCreate
from ophira_backend.project_lifecycle import ProjectUnavailableError
from ophira_backend.scoped_events import (
    ScopedEventCursorError,
    ScopedEventIntegrityError,
    ScopedEventLimitError,
    ScopedEventStore,
)
from ophira_backend.storage import FileLedger, LedgerNotFoundError


@pytest.fixture
def scoped(tmp_path):
    ledger = FileLedger(tmp_path)
    project = ledger.create_project(ProjectCreate(name="Events", objective="Keep observations"))
    store = ScopedEventStore(ledger)
    return ledger, project, store, store.project_stream(project.id)


def log_path(ledger, project):
    return ledger.projects_dir / project.id / "observations" / "events.jsonl"


def test_project_observations_have_no_fabricated_run_and_replay(scoped):
    ledger, project, store, stream = scoped
    assert store.last_sequence(stream) == 0
    assert not log_path(ledger, project).parent.exists()
    first = store.append(stream, "project.changed", "revision:1", {"revision": 1})
    second = store.append(stream, "artifact.updated", "artifact_1", {"version": 2})
    assert first.project_id == project.id
    assert first.run_id is first.provider_profile_id is first.sdk_session_id is None
    assert first.trace_id.startswith("trace_")
    assert first.id == f"{stream}:1"
    assert first.data == {"resource_id": "revision:1", "revision": 1}
    assert ledger.list_runs(project.id) == []
    restarted = ScopedEventStore(FileLedger(ledger.root))
    assert restarted.read(stream, limit=1) == [first]
    assert restarted.read(stream, after_sequence=1, limit=1) == [second]
    assert restarted.read(stream, after_sequence=2) == []
    assert restarted.last_sequence(stream) == 2
    assert stat.S_IMODE(log_path(ledger, project).stat().st_mode) == 0o600


def test_deduplication_keeps_original_payload_and_is_compound(scoped):
    _, _, store, stream = scoped
    original = store.append(stream, "schedule.skipped", "occurrence_1", {"reason": "overlap"})
    assert store.append(
        stream, "schedule.skipped", "occurrence_1", {"reason": "missed"},
    ) == original
    assert store.find(stream, "schedule.skipped", "occurrence_1") == original
    assert store.find(stream, "schedule.failed", "occurrence_1") is None
    other = store.append(stream, "schedule.failed", "occurrence_1", {"reason": "provider"})
    assert other.sequence == 2
    keys = store.observed_keys(stream)
    assert keys == {("schedule.skipped", "occurrence_1"), ("schedule.failed", "occurrence_1")}
    keys.clear()
    assert len(store.observed_keys(stream)) == 2


def test_provider_scopes_are_separate_and_do_not_need_a_profile(scoped):
    ledger, _, store, project_stream = scoped
    streams = [store.provider_stream("not_configured", tenant) for tenant in (
        None, "acme:department+one@example.test", "acme:department+two@example.test",
    )]
    assert len(set(streams)) == 3
    assert not (ledger.root / "providers").exists()
    for index, stream in enumerate(streams):
        event = store.append(stream, "provider.preflight.failed", "attempt_1", {"code": index})
        assert event.project_id is event.run_id is None
        assert event.provider_profile_id == "not_configured"
        assert event.sequence == 1
    assert [store.read(stream)[0].data["code"] for stream in streams] == [0, 1, 2]
    assert store.read(project_stream) == []
    assert not (ledger.root / "providers").exists()


def test_run_binding_uses_real_metadata_and_rejects_foreign_identity(scoped):
    ledger, project, store, stream = scoped
    run = ledger.create_run(project.id, "Existing run", provider_profile_id="provider_a")
    run = ledger.update_run(run.id, sdk_session_id="session_1")
    event = store.append(stream, EventType.SCHEDULE_FIRED, "fire_1", {}, run_id=run.id)
    assert event.run_id == run.id
    assert event.trace_id == run.trace_id
    assert event.sdk_session_id == "session_1"
    assert event.provider_profile_id == "provider_a"
    other = ledger.create_project(ProjectCreate(name="Other", objective="Isolate"))
    foreign = ledger.create_run(other.id, "Do not attribute here")
    for kwargs in ({"run_id": foreign.id}, {"run_id": run.id, "trace_id": "invented"},
                   {"run_id": run.id, "sdk_session_id": "invented"}):
        with pytest.raises(ValueError, match="identity"):
            store.append(stream, "schedule.completed", "end_1", {}, **kwargs)
    with pytest.raises(ValueError, match="cannot claim"):
        store.append(store.provider_stream("provider_a"), "provider.preflight.passed", "a", {},
                     run_id=run.id)


def test_redaction_occurs_before_persistence(scoped):
    ledger, project, store, stream = scoped
    store.append(stream, "project.changed", "revision_1", {
        "authorization": "Bearer scoped-canary", "safe": "token=scoped-canary",
        "nested": {"api_key": "scoped-canary", "count": 3},
    }, actor=EventActor(type="system", name="Bearer actor-canary"))
    raw = log_path(ledger, project).read_text()
    assert "scoped-canary" not in raw
    assert "actor-canary" not in raw
    assert store.read(stream)[0].data["nested"]["count"] == 3


@pytest.mark.parametrize("cursor", ["1", "-1", "project:elsewhere:1", "{stream}:-1",
                                   "{stream}:01", "{stream}:2", "{stream}:1.0"])
def test_replay_cursors_fail_closed(scoped, cursor):
    _, _, store, stream = scoped
    store.append(stream, "project.changed", "one", {})
    with pytest.raises(ScopedEventCursorError):
        store.parse_cursor(stream, cursor.replace("{stream}", stream))


def test_valid_cursor_and_bounded_page_validation(scoped):
    _, _, store, stream = scoped
    event = store.append(stream, "project.changed", "one", {})
    assert store.parse_cursor(stream, None) == 0
    assert store.parse_cursor(stream, "") == 0
    assert store.parse_cursor(stream, f"{stream}:0") == 0
    assert store.parse_cursor(stream, event.id) == 1
    for cursor in (-1, True, 2, 1.5):
        with pytest.raises(ScopedEventCursorError):
            store.read(stream, after_sequence=cursor)
    for limit in (0, -1, True, 201, 1.5):
        with pytest.raises(ScopedEventLimitError):
            store.read(stream, limit=limit)


@pytest.mark.parametrize("mutation", ["partial", "sequence", "scope", "duplicate",
                                     "json", "duplicate_field", "nan"])
def test_corrupt_tail_rejects_even_first_page_find_and_append_without_repair(scoped, mutation):
    ledger, project, store, stream = scoped
    first = store.append(stream, "project.changed", "one", {})
    second = store.append(stream, "project.changed", "two", {})
    path = log_path(ledger, project)
    rows = path.read_bytes().splitlines(keepends=True)
    value = second.model_dump(mode="json")
    if mutation == "partial":
        rows[-1] = rows[-1].rstrip(b"\n")
    elif mutation == "sequence":
        value["sequence"] = 3
    elif mutation == "scope":
        value["project_id"] = "foreign"
    elif mutation == "duplicate":
        value["resource_id"] = first.resource_id
        value["data"]["resource_id"] = first.resource_id
    elif mutation == "json":
        rows[-1] = b"not json\n"
    elif mutation == "duplicate_field":
        rows[-1] = b'{"id":"forged",' + rows[-1][1:]
    elif mutation == "nan":
        value["data"]["invalid"] = float("nan")
    if mutation in {"sequence", "scope", "duplicate", "nan"}:
        rows[-1] = (json.dumps(value) + "\n").encode()
    original = b"".join(rows)
    path.write_bytes(original)
    for operation in (lambda: store.read(stream, limit=1),
                      lambda: store.find(stream, "project.changed", "one"),
                      lambda: store.observed_keys(stream),
                      lambda: store.last_sequence(stream),
                      lambda: store.append(stream, "project.changed", "three", {})):
        with pytest.raises(ScopedEventIntegrityError):
            operation()
        assert path.read_bytes() == original


def test_append_acknowledgement_failure_replays_durable_event_once(scoped, monkeypatch):
    ledger, _, store, stream = scoped
    store.append(stream, "project.changed", "one", {})
    fsync = os.fsync

    def fail_after_sync(descriptor):
        fsync(descriptor)
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("Crash after durable append")

    with monkeypatch.context() as patch:
        patch.setattr("ophira_backend.scoped_events.os.fsync", fail_after_sync)
        with pytest.raises(OSError, match="durable append"):
            store.append(stream, "schedule.skipped", "occurrence", {"reason": "overlap"})
    restarted = ScopedEventStore(FileLedger(ledger.root))
    recovered = restarted.append(stream, "schedule.skipped", "occurrence", {"reason": "missed"})
    assert recovered.sequence == 2
    assert recovered.data["reason"] == "overlap"
    assert restarted.last_sequence(stream) == 2


def _race_append(root, project_id, barrier, writer):
    store = ScopedEventStore(FileLedger(Path(root)))
    stream = store.project_stream(project_id)
    barrier.wait(timeout=15)
    for index in range(5):
        store.append(stream, "schedule.skipped", f"shared_{index}", {"writer": writer})
        store.append(stream, "project.changed", f"own_{writer}_{index}", {})


def test_two_processes_share_sequence_and_deduplication(scoped):
    ledger, project, store, stream = scoped
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    workers = [context.Process(target=_race_append,
                               args=(str(ledger.root), project.id, barrier, index))
               for index in range(2)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(20)
            assert worker.exitcode == 0
        events = store.read(stream)
        assert [event.sequence for event in events] == list(range(1, 16))
        assert len({(event.type, event.resource_id) for event in events}) == 15
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.kill()
                worker.join(5)
            worker.close()


def test_deleted_project_is_not_recreated(scoped):
    ledger, project, store, stream = scoped
    store.append(stream, "project.changed", "one", {})
    project_root = ledger.projects_dir / project.id
    shutil.rmtree(project_root)
    for operation in (lambda: store.read(stream), lambda: store.last_sequence(stream),
                      lambda: store.append(stream, "project.changed", "two", {})):
        with pytest.raises(LedgerNotFoundError):
            operation()
    assert not project_root.exists()


def test_deletion_intent_blocks_observation_read_and_write(scoped):
    ledger, project, store, stream = scoped
    store.append(stream, "project.changed", "one", {})
    state_dir = ledger.root / "project-deletions" / project.id
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text("{}")
    for operation in (lambda: store.read(stream), lambda: store.last_sequence(stream),
                      lambda: store.append(stream, "project.changed", "two", {})):
        with pytest.raises(ProjectUnavailableError):
            operation()


@pytest.mark.parametrize("kind", ["directory", "file", "hardlink"])
def test_linked_log_or_ancestor_cannot_expose_or_modify_other_files(scoped, tmp_path, kind):
    ledger, project, store, stream = scoped
    target = tmp_path / "outside"
    target.mkdir()
    unrelated = target / "events.jsonl"
    unrelated.write_text("DO NOT READ OR CHANGE\n")
    path = log_path(ledger, project)
    if kind == "directory":
        path.parent.symlink_to(target, target_is_directory=True)
    else:
        path.parent.mkdir()
        if kind == "file":
            path.symlink_to(unrelated)
        else:
            os.link(unrelated, path)
    for operation in (lambda: store.read(stream),
                      lambda: store.append(stream, "project.changed", "one", {})):
        with pytest.raises((OSError, ScopedEventIntegrityError)):
            operation()
    assert unrelated.read_text() == "DO NOT READ OR CHANGE\n"


def test_event_and_stream_limits_leave_history_intact(scoped, monkeypatch):
    ledger, project, store, stream = scoped
    store.append(stream, "project.changed", "one", {})
    original = log_path(ledger, project).read_bytes()
    with pytest.raises(ScopedEventLimitError):
        store.append(stream, "project.changed", "huge", {str(i): "x" * 20_000 for i in range(8)})
    assert log_path(ledger, project).read_bytes() == original
    monkeypatch.setattr("ophira_backend.scoped_events.MAX_SCOPED_EVENTS", 1)
    assert store.append(stream, "project.changed", "one", {}) == store.read(stream)[0]
    with pytest.raises(ScopedEventLimitError):
        store.append(stream, "project.changed", "two", {})
    assert log_path(ledger, project).read_bytes() == original


@pytest.mark.parametrize("stream", ["../elsewhere", "project:../elsewhere", "project:a:b",
                                   "provider:tenant-acme:p", "provider:local:../p", "project:"])
def test_uncontrolled_stream_names_are_rejected(scoped, stream):
    _, _, store, _ = scoped
    with pytest.raises(ValueError):
        store.append(stream, "project.changed", "one", {})


def test_payload_resource_mismatch_is_rejected(scoped):
    _, _, store, stream = scoped
    with pytest.raises(ValueError, match="does not match"):
        store.append(stream, "project.changed", "one", {"resource_id": "two"})
    assert store.last_sequence(stream) == 0

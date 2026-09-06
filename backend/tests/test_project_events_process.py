"""Spawned project-event reconcilers recover durable records without a model."""

import asyncio
import multiprocessing
import os
from collections import Counter
from pathlib import Path

from ophira_backend.constants import EventType, EvidenceKind, EvidenceSourceType
from ophira_backend.events import EventPublisher
from ophira_backend.models import Decision, Evidence, ProjectCreate
from ophira_backend.project_events import ProjectEventProjector
from ophira_backend.storage import FileLedger


def persist_records(ledger, project_id, assignments):
    """Use the same ID across resource types to exercise the compound dedupe key."""
    evidence, decisions = [], []
    for source_run_id, label, count in assignments:
        for index in range(count):
            resource_id = f"resource_{label}_{index}"
            evidence.append(Evidence(
                id=resource_id, kind=EvidenceKind.USER_STATEMENT,
                claim=f"Synthetic evidence {label} {index}", source_title="Local process fixture",
                source_type=EvidenceSourceType.USER, confidence=1, recorded_by="fixture-writer",
                source_run_id=source_run_id,
            ))
            decisions.append(Decision(
                id=resource_id, question=f"Synthetic question {label} {index}",
                outcome="Retain the synthetic decision", made_by="fixture-writer",
                evidence_ids=[resource_id], source_run_id=source_run_id,
            ))

    def append(project):
        project.evidence.extend(evidence)
        project.decisions.extend(decisions)

    ledger.update_project(project_id, append)


def reconcile_worker(root, run_ids, barrier, connection):
    async def execute():
        ledger = FileLedger(Path(root))
        projector = ProjectEventProjector(ledger, EventPublisher(ledger))
        if barrier is not None:
            barrier.wait(timeout=15)
        for _ in range(3):
            for run_id in run_ids:
                await projector.reconcile_run(run_id)
        connection.send({"pid": os.getpid(), "reconciled_runs": run_ids})

    try:
        asyncio.run(execute())
    finally:
        connection.close()


def persist_then_exit(root, project_id, assignments, connection):
    """Terminate without projection or normal Python/process cleanup."""
    ledger = FileLedger(Path(root))
    persist_records(ledger, project_id, assignments)
    project = ledger.get_project(project_id)
    connection.send({
        "pid": os.getpid(), "revision": project.revision,
        "evidence_count": len(project.evidence), "decision_count": len(project.decisions),
    })
    os._exit(23)


def start_worker(context, target, *args):
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=target, args=(*args, send))
    process.start()
    send.close()
    return process, receive


def receive_result(process, connection):
    assert connection.poll(15), f"worker did not report; exit code {process.exitcode}"
    return connection.recv()


def cleanup(process, connection):
    process.join(15)
    if process.is_alive():
        process.kill()
        process.join(5)
    connection.close()
    process.close()


def expected_keys(label, count):
    return Counter({
        (event_type.value, f"resource_{label}_{index}"): 1
        for event_type in (EventType.EVIDENCE_ADDED, EventType.DECISION_RECORDED)
        for index in range(count)
    })


def assert_projection(ledger, run, expected, *, seed_count=0):
    events = ledger.list_events(run.id)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert len({event.id for event in events}) == len(events)
    projected = [event for event in events if event.type in {
        EventType.EVIDENCE_ADDED.value, EventType.DECISION_RECORDED.value,
    }]
    assert Counter((event.type, event.data["resource_id"]) for event in projected) == expected
    assert len(events) == sum(expected.values()) + seed_count
    for event in projected:
        assert event.run_id == run.id
        assert event.project_id == run.project_id
        assert event.trace_id == run.trace_id
        assert event.data["recorded_at"]
    summary = ledger.get_run_summary(run.id)
    assert summary.last_sequence == len(events)
    assert summary.event_counts == dict(Counter(event.type for event in events))
    return events


def create_projects(root):
    ledger = FileLedger(root)
    project = ledger.create_project(ProjectCreate(
        name="Projection processes", objective="Recover only actual source-run records",
    ))
    first = ledger.create_run(project.id, "First source")
    second = ledger.create_run(project.id, "Second source")
    other = ledger.create_project(ProjectCreate(
        name="Foreign project", objective="No cross-project projection",
    ))
    foreign = ledger.create_run(other.id, "Foreign source")
    return ledger, project, first, second, foreign


def test_spawned_reconcilers_publish_each_resource_once_in_its_source_run(tmp_path):
    ledger, project, first, second, foreign = create_projects(tmp_path)
    assignments = [
        (first.id, "first", 8), (second.id, "second", 4),
        (None, "unattributed", 1), ("run_missing_source", "missing", 1),
        (foreign.id, "foreign", 1),
    ]
    persist_records(ledger, project.id, assignments)
    # Existing unrelated event sequences must not be replaced or renumbered.
    seed = asyncio.run(EventPublisher(ledger).publish(first.id, EventType.RUN_STARTED))
    before_project = ledger.get_project(project.id)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    run_ids = [first.id, second.id, foreign.id]
    children = [start_worker(context, reconcile_worker, tmp_path, run_ids, barrier)
                for _ in range(2)]
    try:
        barrier.wait(timeout=15)
        outcomes = [receive_result(process, pipe) for process, pipe in children]
        assert len({outcome["pid"] for outcome in outcomes}) == 2
        assert all(outcome["pid"] != os.getpid() for outcome in outcomes)
        for process, _pipe in children:
            process.join(15)
            assert process.exitcode == 0
    finally:
        for process, pipe in children:
            cleanup(process, pipe)

    assert ledger.get_project(project.id) == before_project
    first_events = assert_projection(ledger, first, expected_keys("first", 8), seed_count=1)
    assert first_events[0] == seed
    assert_projection(ledger, second, expected_keys("second", 4))
    assert_projection(ledger, foreign, Counter())
    assert len(ledger.list_runs(project.id)) == 2


def test_new_process_recovers_records_after_writer_exits_before_event_projection(tmp_path):
    ledger, project, first, second, foreign = create_projects(tmp_path)
    assignments = [
        (first.id, "crashed", 3), (second.id, "other", 2),
        (None, "unattributed", 1), (foreign.id, "foreign", 1),
    ]
    context = multiprocessing.get_context("spawn")
    crashed, crash_pipe = start_worker(
        context, persist_then_exit, tmp_path, project.id, assignments,
    )
    try:
        persisted = receive_result(crashed, crash_pipe)
        crashed.join(15)
        assert crashed.exitcode == 23
    finally:
        cleanup(crashed, crash_pipe)
    durable_project = ledger.get_project(project.id)
    assert persisted["revision"] == durable_project.revision
    assert persisted["evidence_count"] == persisted["decision_count"] == 7
    assert all(ledger.list_events(run.id) == [] for run in (first, second, foreign))

    recoverer, recover_pipe = start_worker(
        context, reconcile_worker, tmp_path, [first.id, second.id, foreign.id], None,
    )
    try:
        recovered = receive_result(recoverer, recover_pipe)
        recoverer.join(15)
        assert recoverer.exitcode == 0
        assert recovered["pid"] not in {persisted["pid"], os.getpid()}
    finally:
        cleanup(recoverer, recover_pipe)

    first_events = assert_projection(ledger, first, expected_keys("crashed", 3))
    second_events = assert_projection(ledger, second, expected_keys("other", 2))
    assert_projection(ledger, foreign, Counter())
    assert ledger.get_project(project.id) == durable_project
    # A further fresh process proves that dedupe does not depend on a worker's
    # in-memory state, even after several successful reconciliations.
    repeat, repeat_pipe = start_worker(
        context, reconcile_worker, tmp_path, [first.id, second.id, foreign.id], None,
    )
    try:
        receive_result(repeat, repeat_pipe)
        repeat.join(15)
        assert repeat.exitcode == 0
    finally:
        cleanup(repeat, repeat_pipe)
    assert ledger.list_events(first.id) == first_events
    assert ledger.list_events(second.id) == second_events
    assert ledger.list_events(foreign.id) == []

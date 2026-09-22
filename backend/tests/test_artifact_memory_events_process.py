"""Real process exits and concurrent recovery, with no model/network calls."""

import asyncio
import multiprocessing
import os
from collections import Counter
from pathlib import Path

from test_artifact_memory_events import propose, register, setup
from test_project_events_process import cleanup, receive_result, start_worker

from ophira_backend.events import EventPublisher
from ophira_backend.memory import FileMemoryProvider
from ophira_backend.project_events import ProjectEventProjector
from ophira_backend.storage import FileLedger


def commit_then_exit(root, run_id, connection):
    ledger = FileLedger(Path(root))
    run = ledger.get_run(run_id)
    memory = FileMemoryProvider(Path(root) / "memory")
    first = register(ledger, run, "first")
    second = register(ledger, run, "second")
    item = propose(memory, run)
    connection.send((first.id, second.id, item.id))
    os._exit(23)


def recover(root, run_id, barrier, connection):
    ledger = FileLedger(Path(root))
    projector = ProjectEventProjector(
        ledger, EventPublisher(ledger), memory=FileMemoryProvider(Path(root) / "memory"),
    )
    barrier.wait(timeout=15)
    for _ in range(3):
        assert asyncio.run(projector.reconcile_run(run_id))
    connection.send(len(ledger.list_events(run_id)))
    connection.close()


def register_concurrently(root, project_id, run_id, barrier, connection):
    ledger = FileLedger(Path(root))
    barrier.wait(timeout=15)
    artifact = ledger.register_artifact(project_id, "report.md", run_id=run_id)
    connection.send((artifact.id, artifact.version))
    connection.close()


def test_process_death_after_commits_and_two_reconcilers_repair_each_notice_once(tmp_path):
    ledger, project, run, memory, events, projector = setup(tmp_path)
    context = multiprocessing.get_context("spawn")
    process, pipe = start_worker(context, commit_then_exit, tmp_path, run.id)
    try:
        first, second, proposal = receive_result(process, pipe)
        process.join(15)
        assert process.exitcode == 23
    finally:
        cleanup(process, pipe)
    assert ledger.list_events(run.id) == []
    barrier = context.Barrier(3)
    children = [start_worker(context, recover, tmp_path, run.id, barrier) for _ in range(2)]
    try:
        barrier.wait(timeout=15)
        assert [receive_result(p, c) for p, c in children] == [4, 4]
        for p, _ in children:
            p.join(15)
            assert p.exitcode == 0
    finally:
        for p, c in children:
            cleanup(p, c)
    stored = ledger.list_events(run.id)
    assert [e.sequence for e in stored] == [1, 2, 3, 4]
    assert Counter((e.type, e.data["resource_id"]) for e in stored) == {
        ("artifact.registered", first): 1,
        ("artifact.registered", second): 1,
        ("artifact.updated", second): 1,
        ("memory.proposed", proposal): 1,
    }
    assert ledger.get_run_summary(run.id).last_sequence == 4
    assert len(ledger.get_project(project.id).artifacts) == 2
    assert memory.get(proposal).status == "proposed"


def test_concurrent_registration_allocates_only_one_new_version(tmp_path):
    ledger, project, run, memory, events, projector = setup(tmp_path)
    register(ledger, run, "first")
    path = ledger.workspace_for_project(project.id) / "report.md"
    path.write_text("changed bytes")
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    children = [
        start_worker(context, register_concurrently, tmp_path, project.id, run.id, barrier)
        for _ in range(2)
    ]
    try:
        barrier.wait(timeout=15)
        results = [receive_result(p, c) for p, c in children]
        assert results[0] == results[1]
        assert results[0][1] == 2
        for p, _ in children:
            p.join(15)
            assert p.exitcode == 0
    finally:
        for p, c in children:
            cleanup(p, c)
    assert [a.version for a in ledger.get_project(project.id).artifacts] == [1, 2]

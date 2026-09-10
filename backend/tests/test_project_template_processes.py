"""Real independent workers and abrupt process death around atomic project publication."""

import multiprocessing
import os
from pathlib import Path

import pytest
from test_project_templates import saved, services, use_request

from ophira_backend.models import ProjectCreate
from ophira_backend.project_template_models import TemplateApply


def apply_worker(root, project_id, template_id, payload, gate, queue, crash=None):
    import ophira_backend.project_template_apply as implementation

    store, applications = services(Path(root))
    if crash == "before_publish":

        def stop(*_args):
            os._exit(73)

        implementation.publish_exclusive = stop
    elif crash == "after_publish":
        original = store.write

        def stop(path, record):
            if getattr(record, "state", None) == "applied":
                os._exit(73)
            return original(path, record)

        store.write = stop
    if gate:
        gate.wait(10)
    result = applications.apply(
        project_id, template_id, TemplateApply.model_validate_json(payload), None
    )
    if queue:
        queue.put(result["project"].id)


def fixture_data(tmp_path):
    store, _ = services(tmp_path)
    project = store.ledger.create_project(ProjectCreate(name="Source", objective="Reuse"))
    artifact = store.ledger.save_upload(project.id, "source.txt", b"Pinned reference")
    template, _ = saved(store, project, artifact_ids=[artifact.id])
    request = use_request(template).model_copy(update={"artifact_ids": [artifact.id]})
    return store, project, template, request


def test_two_processes_apply_one_reviewed_request_once(tmp_path):
    store, project, template, request = fixture_data(tmp_path)
    context = multiprocessing.get_context("spawn")
    gate, queue = context.Event(), context.Queue()
    workers = [
        context.Process(
            target=apply_worker,
            args=(
                str(tmp_path),
                project.id,
                template["id"],
                request.model_dump_json(),
                gate,
                queue,
            ),
        )
        for _ in range(2)
    ]
    try:
        for worker in workers:
            worker.start()
        gate.set()
        identifiers = [queue.get(timeout=20) for _ in workers]
        for worker in workers:
            worker.join(20)
            assert worker.exitcode == 0
        assert len(set(identifiers)) == 1 and len(store.ledger.list_projects()) == 2
        assert store.ledger.list_runs(identifiers[0]) == []
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
        queue.close()


@pytest.mark.parametrize("crash", ["before_publish", "after_publish"])
def test_abrupt_worker_death_recovers_durable_review_in_a_new_store(tmp_path, crash):
    store, project, template, request = fixture_data(tmp_path)
    context = multiprocessing.get_context("spawn")
    worker = context.Process(
        target=apply_worker,
        args=(
            str(tmp_path),
            project.id,
            template["id"],
            request.model_dump_json(),
            None,
            None,
            crash,
        ),
    )
    worker.start()
    try:
        worker.join(20)
        assert worker.exitcode == 73
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
    _, applications = services(tmp_path)
    pending = applications.list(project.id, template["id"], None)["items"][0]
    assert pending["state"] == "ready"
    assert len(store.ledger.list_projects()) == (1 if crash == "before_publish" else 2)
    result = applications.resume(project.id, template["id"], pending["id"], None)
    assert result["project"].id == pending["project_id"]
    assert len(store.ledger.list_projects()) == 2
    assert result["application"]["state"] == "applied"
    assert (
        store.ledger.capture_artifact(result["project"].id, result["project"].artifacts[0].id)[1]
        == b"Pinned reference"
    )

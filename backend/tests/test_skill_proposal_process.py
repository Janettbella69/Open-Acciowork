"""Independent hosts serialize candidate submission, review, and crash recovery."""

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from ophira_backend.models import ProjectCreate, SkillSelectionToken, utc_now
from ophira_backend.skill_proposal_models import SkillProposalReviewRequest
from ophira_backend.skill_proposals import SkillProposalConflictError, SkillProposalService
from ophira_backend.skills import SkillRegistry
from ophira_backend.storage import FileLedger

SKILL_ID = "ophira-tenant:reviewed-workflow"
EMPTY = SkillSelectionToken(current_hash=None, enabled=None, generation=0)


def build_service(root):
    root = Path(root)
    return SkillProposalService(
        FileLedger(root / "ledger"), SkillRegistry(root / "plugin", root / "registry"),
    )


@pytest.fixture
def prepared(tmp_path):
    plugin = tmp_path / "plugin" / ".claude-plugin"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text('{"name":"ophira-core"}')
    service = build_service(tmp_path)
    project = service.ledger.create_project(ProjectCreate(
        name="Local proposal processes", objective="Review a frozen workflow without a Provider",
    ))
    run = service.ledger.create_run(project.id, "Persist the proposed reusable workflow")
    service.ledger.update_run(
        run.id, started_at=utc_now(), sdk_session_id="offline-fixture-session",
    )
    root = service.ledger.workspace_for_project(project.id).joinpath(
        "skill-proposals", "reviewed-workflow",
    )
    package = root / "package"
    (package / "references").mkdir(parents=True)
    (package / "templates").mkdir()
    (package / "SKILL.md").write_text(
        "---\nname: reviewed-workflow\ndescription: Reviewed workflow\nversion: 1.0.0\n---\n"
        "Use references/evidence.md and templates/result.md.\n"
    )
    (package / "references" / "evidence.md").write_text("Distinguish observations and inference.\n")
    (package / "templates" / "result.md").write_text("# Decision\nEvidence: ...\n")
    (root / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "kind": "ophira.skill-proposal", "name": "reviewed-workflow",
        "version": "1.0.0", "package_dir": "package",
        "reason": "Reuse an explicitly reviewed method",
    }))
    artifact = service.ledger.register_artifact(
        project.id, "skill-proposals/reviewed-workflow/manifest.json", run_id=run.id,
    )
    return service, run, artifact


def approval(item):
    return SkillProposalReviewRequest(
        decision="approve", revision=item.revision, content_hash=item.content_hash,
        expected_target=EMPTY, note="Offline human-review boundary fixture",
    )


def _worker(root, operation, arguments, connection):
    service = build_service(root)
    connection.send({"phase": "ready", "pid": os.getpid()})
    assert connection.recv() == "go"
    try:
        if operation == "submit":
            item = service.submit_artifact(*arguments)
        elif operation == "review":
            identifier, request = arguments
            item = service.review(
                identifier, SkillProposalReviewRequest.model_validate_json(request), principal=None,
            )
        else:
            report = service.recover()
            item = service.get(arguments[0])
            connection.send({"outcome": "recovered", "pid": os.getpid(),
                             "report": report.model_dump(mode="json"),
                             "item": item.model_dump(mode="json")})
            return
        connection.send({"outcome": "success", "pid": os.getpid(),
                         "item": item.model_dump(mode="json")})
    except SkillProposalConflictError as exc:
        connection.send({"outcome": "stale", "pid": os.getpid(), "error": str(exc)})
    finally:
        connection.close()


def start(context, target, *args):
    parent, child = context.Pipe()
    process = context.Process(target=target, args=(*args, child))
    process.start()
    child.close()
    return process, parent


def receive(process, connection):
    assert connection.poll(15), f"worker did not report; exit code {process.exitcode}"
    return connection.recv()


def cleanup(children):
    for process, connection in children:
        if process.is_alive():
            process.kill()
        process.join(5)
        connection.close()
        process.close()


def execute_parallel(tmp_path, operation, arguments):
    context = multiprocessing.get_context("spawn")
    children = [start(context, _worker, str(tmp_path), operation, args) for args in arguments]
    try:
        pids = []
        for process, connection in children:
            ready = receive(process, connection)
            assert ready["phase"] == "ready"
            pids.append(ready["pid"])
        assert len(set(pids)) == len(children) and os.getpid() not in pids
        for _, connection in children:
            connection.send("go")
        results = [receive(process, connection) for process, connection in children]
        for process, _ in children:
            process.join(15)
            assert process.exitcode == 0
        return results
    finally:
        cleanup(children)


def test_spawned_same_artifact_submissions_converge_to_one_frozen_candidate(prepared, tmp_path):
    service, run, artifact = prepared
    before = service.registry.registry_state_path.read_bytes()
    results = execute_parallel(tmp_path, "submit", [(run.id, artifact.id)] * 2)
    assert [row["outcome"] for row in results] == ["success", "success"]
    assert results[0]["item"] == results[1]["item"]
    item = service.get(results[0]["item"]["id"])
    assert item.state == "pending" and item.revision == 1 and item.history == []
    assert item.source_run_id == run.id and item.source_artifact_id == artifact.id
    assert item.source_sdk_session_id == "offline-fixture-session"
    assert [entry.name for entry in service.root.iterdir()] == [item.id]
    assert len(service.list()) == 1
    assert service.registry.list() == service.registry.snapshot() == []
    assert not list(service.registry.versions_dir.iterdir())
    assert service.registry.registry_state_path.read_bytes() == before
    frozen = {row.path: service.read_file(item.id, row.path, content_hash=item.content_hash)[1]
              for row in item.files}
    assert set(frozen) == {"SKILL.md", "references/evidence.md", "templates/result.md"}
    # A subsequent Run can refer to this same Artifact without claiming authorship.
    another = service.ledger.create_run(run.project_id, "Refer to the prior candidate")
    service.ledger.update_run(
        another.id, started_at=utc_now(), sdk_session_id="second-fixture-session",
    )
    retried = execute_parallel(tmp_path, "submit", [(another.id, artifact.id)])[0]["item"]
    assert retried == item.model_dump(mode="json")


def test_spawned_same_revision_reviews_commit_one_receipt_and_reject_the_stale_request(
    prepared, tmp_path,
):
    service, run, artifact = prepared
    item = service.submit_artifact(run.id, artifact.id)
    request = approval(item).model_dump_json()
    results = execute_parallel(tmp_path, "review", [(item.id, request)] * 2)
    assert sorted(row["outcome"] for row in results) == ["stale", "success"]
    stale = next(row for row in results if row["outcome"] == "stale")
    assert "reload before reviewing" in stale["error"]
    published = build_service(tmp_path).get(item.id)
    assert published.state == "published" and published.revision == 3
    assert len(published.history) == 1 and published.review.outcome == "published"
    receipt = service.registry.publication_receipt(item.id)
    assert receipt == published.publication_receipt
    assert receipt.review_revision == 2 and receipt.previous_selection == EMPTY
    canonical = json.loads(service.registry.registry_state_path.read_bytes())
    assert set(canonical["publication_receipts"]) == {item.id}
    assert canonical["generations"] == {SKILL_ID: 1}
    assert service.registry.get(SKILL_ID).enabled is False
    assert len(service.registry.versions(SKILL_ID)) == 1


def _review_until_checkpoint(root, identifier, request_json, checkpoint, connection):
    service = build_service(root)

    def pause():
        connection.send({"phase": checkpoint, "pid": os.getpid()})
        # Parent kills this process after independently reading the durable state.
        connection.recv()
        raise AssertionError("checkpoint worker must be killed, not released")

    if checkpoint == "intent":
        original = service._save

        def save(item, **kwargs):
            original(item, **kwargs)
            if item.state == "publishing":
                pause()

        service._save = save
    else:
        original = service.registry.publish_reviewed_candidate

        def publish(*args, **kwargs):
            original(*args, **kwargs)
            pause()

        service.registry.publish_reviewed_candidate = publish
    service.review(identifier, SkillProposalReviewRequest.model_validate_json(request_json),
                   principal=None)


@pytest.mark.parametrize("checkpoint", ["intent", "receipt"])
def test_killed_review_recovers_from_durable_intent_and_exact_receipt_without_republication(
    prepared, tmp_path, checkpoint,
):
    service, run, artifact = prepared
    item = service.submit_artifact(run.id, artifact.id)
    request = approval(item).model_dump_json()
    context = multiprocessing.get_context("spawn")
    process, connection = start(context, _review_until_checkpoint, str(tmp_path), item.id,
                                request, checkpoint)
    try:
        reached = receive(process, connection)
        assert reached == {"phase": checkpoint, "pid": process.pid}
        assert process.pid != os.getpid()
        # Deliberately avoid get(): its proposal lock is held by the paused child.
        durable = json.loads((service.root / item.id / "record.json").read_bytes())
        assert durable["state"] == "publishing" and durable["revision"] == 2
        assert durable["review"]["outcome"] == "publishing"
        receipt = service.registry.publication_receipt(item.id)
        assert (receipt is not None) == (checkpoint == "receipt")
        if receipt:
            # A later ordinary operation must survive recovery of the candidate record.
            service.registry.set_enabled(SKILL_ID, True)
        canonical_before_recovery = service.registry.registry_state_path.read_bytes()
        process.kill()
        process.join(15)
        assert process.exitcode is not None and process.exitcode < 0
        recovered = execute_parallel(tmp_path, "recover", [(item.id,)])[0]
        expected_state = "published" if receipt else "pending"
        expected_outcome = "published" if receipt else "interrupted"
        assert recovered["report"] == {
            "inspected": 1, "published": int(receipt is not None),
            "reset_pending": int(receipt is None), "failed": 0,
        }
        restored = recovered["item"]
        assert restored["state"] == expected_state and restored["revision"] == 3
        assert restored["review"]["outcome"] == expected_outcome
        assert restored["history"] == [restored["review"]]
        assert restored["review"]["reviewer_binding"] == durable["review"]["reviewer_binding"]
        assert restored["publication_receipt"] == (
            receipt.model_dump(mode="json") if receipt else None
        )
        assert service.registry.registry_state_path.read_bytes() == canonical_before_recovery
        if receipt:
            assert service.registry.get(SKILL_ID).enabled is True
        else:
            assert service.registry.selection_token(SKILL_ID) == EMPTY
            assert not list(service.registry.versions_dir.iterdir())
        repeated = execute_parallel(tmp_path, "recover", [(item.id,)])[0]
        assert repeated["report"] == {
            "inspected": 1, "published": 0, "reset_pending": 0, "failed": 0,
        }
        assert repeated["item"] == restored
        stale = execute_parallel(tmp_path, "review", [(item.id, request)])[0]
        assert stale["outcome"] == "stale"
        assert service.registry.registry_state_path.read_bytes() == canonical_before_recovery
    finally:
        cleanup([(process, connection)])

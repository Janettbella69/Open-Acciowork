import json
import re
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from test_auth_api import principal, request_as
from test_project_exports import completed, setup

from ophira_backend.constants import MemoryKind, MemoryScope, MemorySource, PrincipalRole, RunStatus
from ophira_backend.deletion_models import ConversationDeletionRequest, ErasedRun
from ophira_backend.erasure_files import ErasureError, FileErasure, operation_lease
from ophira_backend.models import ProjectCreate
from ophira_backend.storage import LedgerNotFoundError
from ophira_backend.workspace_checkpoints import WorkspaceCheckpoints


def payload(preview):
    return ConversationDeletionRequest(
        **{
            key: preview[key]
            for key in (
                "request_id",
                "reviewed_at",
                "fingerprint",
            )
        },
        confirm_deletion=True,
    )


def native_fixture(root, project_id, session):
    workspace = root / "projects" / project_id / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    folder = (
        root / "runtime-config/environment/projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(workspace))
    )
    folder.mkdir(parents=True)
    transcript = folder / f"{session}.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": session,
                "cwd": str(workspace),
                "uuid": str(uuid4()),
                "message": {"role": "user", "content": "native-private"},
            }
        )
        + "\n"
    )
    child = folder / session / "subagents"
    child.mkdir(parents=True)
    (child / "agent-test.jsonl").write_text("branch-private")
    return transcript, child


def test_conversation_removes_continuations_and_context_but_keeps_saved_outputs(tmp_path):
    app, owner, project = setup(tmp_path)
    ledger, service = app.state.ledger, app.state.conversation_erasure
    session = str(uuid4())
    first = completed(app, owner, project, session=session)
    second = completed(app, owner, project, session=session, resume_source_run_id=first.id)
    other = completed(app, owner, project, session=str(uuid4()))
    transcript, child = native_fixture(tmp_path, project.id, session)
    workspace = ledger.workspace_for_project(project.id)
    (workspace / "saved.txt").write_text("saved-output")
    artifact = ledger.register_artifact(project.id, "saved.txt", run_id=first.id)
    memory = app.state.memory.propose(
        scope=MemoryScope.PROJECT,
        scope_id=project.id,
        text="private-memory",
        kind=MemoryKind.PREFERENCE,
        source=MemorySource.USER,
        source_run_id=first.id,
        principal=owner,
    )
    keep = app.state.memory.propose(
        scope=MemoryScope.PROJECT,
        scope_id=project.id,
        text="keep-memory",
        kind=MemoryKind.PREFERENCE,
        source=MemorySource.USER,
        principal=owner,
    )
    app.state.runs._write_diary(first.id, RunStatus.COMPLETED, "private-diary")
    app.state.runs._write_diary(other.id, RunStatus.COMPLETED, "keep-diary")
    preview = service.preview(project.id, owner, run_id=first.id)
    assert preview["run_ids"] == sorted([first.id, second.id])
    assert preview["native_session_count"] == 1 and preview["memory_count"] == 1
    result = service.delete(project.id, first.id, owner, payload(preview))
    assert result["run_ids"] == preview["run_ids"]
    assert not transcript.exists() and not child.exists()
    for run in (first, second):
        assert isinstance(ledger.read_run_entry(run.id), ErasedRun)
        assert set(p.name for p in (tmp_path / "runs" / run.id).iterdir()) == {"run.json"}
        with pytest.raises(LedgerNotFoundError):
            ledger.get_run(run.id)
    assert [run.id for run in ledger.list_runs(project.id)] == [other.id]
    assert ledger.get_project(project.id).artifacts[0].id == artifact.id
    assert (workspace / "saved.txt").read_text() == "saved-output"
    assert [item.id for item in app.state.memory.list()] == [keep.id]
    assert app.state.memory.list_tombstones()[0].memory_id == memory.id
    for path in (tmp_path / "memory").rglob("*"):
        if path.is_file():
            assert b"private-memory" not in path.read_bytes()
    diary = b"".join(
        path.read_bytes() for path in (tmp_path / "projects" / project.id / "diary").iterdir()
    )
    assert b"private-diary" not in diary and b"keep-diary" in diary
    assert service.delete(project.id, first.id, owner, payload(preview))["status"] == "deleted"
    # Project exports continue to include saved outputs plus content-free provenance.
    assert app.state.project_exports.preview(project.id, owner)["run_ids"] == [other.id]


@pytest.mark.parametrize(
    "change", ["event", "new_continuation", "active", "link", "foreign_session"]
)
def test_changed_or_unsafe_scope_is_refused_before_any_deletion(tmp_path, change):
    app, owner, project = setup(tmp_path)
    service, ledger = app.state.conversation_erasure, app.state.ledger
    run = completed(app, owner, project, session=str(uuid4()))
    preview = service.preview(project.id, owner, run_id=run.id)
    if change == "event":
        (tmp_path / "runs" / run.id / "events.jsonl").write_text("changed\n")
    elif change == "new_continuation":
        completed(app, owner, project, session=run.sdk_session_id)
    elif change == "active":
        ledger.update_run(run.id, status=RunStatus.RUNNING)
    elif change == "link":
        (tmp_path / "runs" / run.id / "unsafe").symlink_to(tmp_path / "projects")
    else:
        other = ledger.create_project(
            ProjectCreate(name="Other", objective="Other"), principal=owner
        )
        completed(app, owner, other, session=run.sdk_session_id)
    with pytest.raises((ErasureError, ValueError, OSError)):
        service.delete(project.id, run.id, owner, payload(preview))
    assert ledger.get_run(run.id).prompt == "Original user prompt"
    assert not (tmp_path / ".data-erasure/pending").exists()


def test_native_partial_failure_recovers_without_restoring_private_content(tmp_path, monkeypatch):
    app, owner, project = setup(tmp_path)
    service, ledger = app.state.conversation_erasure, app.state.ledger
    run = completed(app, owner, project, session=str(uuid4()))
    transcript, child = native_fixture(tmp_path, project.id, run.sdk_session_id)
    preview = service.preview(project.id, owner, run_id=run.id)
    from ophira_backend import sdk_session_erasure

    original = sdk_session_erasure.sdk_operation

    def fail(root, target, operation):
        if operation == "delete":
            transcript.unlink()
            raise OSError("interrupted after main transcript removal")
        return original(root, target, operation)

    monkeypatch.setattr(sdk_session_erasure, "sdk_operation", fail)
    with pytest.raises(OSError):
        service.delete(project.id, run.id, owner, payload(preview))
    assert child.exists()
    with pytest.raises(ErasureError):
        with operation_lease(tmp_path):
            pass
    FileErasure(tmp_path).recover()
    assert not child.exists()
    assert isinstance(ledger.read_run_entry(run.id), ErasedRun)
    assert service.delete(project.id, run.id, owner, payload(preview))["status"] == "deleted"


def test_saved_checkpoint_and_post_deletion_cold_backup_round_trip(tmp_path):
    from ophira_backend.application_backup import create_backup, restore_backup
    from ophira_backend.storage import FileLedger

    data = tmp_path / "data"
    app, owner, project = setup(data)
    ledger, service = app.state.ledger, app.state.conversation_erasure
    run = completed(app, owner, project, session=str(uuid4()))
    workspace = ledger.workspace_for_project(project.id)
    (workspace / "saved.md").write_text("saved project file")
    checkpoints = WorkspaceCheckpoints(ledger)
    checkpoint = checkpoints.capture(project.id, run_id=run.id)
    preview = service.preview(project.id, owner, run_id=run.id)
    service.delete(project.id, run.id, owner, payload(preview))
    assert checkpoints.load(project.id, checkpoint.id).run_id == run.id
    assert checkpoints.list(project.id)[0].id == checkpoint.id
    with pytest.raises(ValueError, match="already exists"):
        ledger.create_run(project.id, "recreate", run_id=run.id, principal=owner)
    assert ledger.runs_for_sdk_session(run.sdk_session_id) == []
    app.state.skills.snapshot()
    create_backup(data, app.state.skills.plugin_dir, tmp_path / "backup")
    restore_backup(tmp_path / "backup", tmp_path / "restored")
    restored = FileLedger(tmp_path / "restored/data")
    assert isinstance(restored.read_run_entry(run.id), ErasedRun)
    assert restored.list_runs(project.id) == []
    assert WorkspaceCheckpoints(restored).load(project.id, checkpoint.id).run_id == run.id


def test_exact_source_auxiliary_data_cleanup_preserves_other_sessions_and_auth(tmp_path):
    app, owner, project = setup(tmp_path)
    service = app.state.conversation_erasure
    run = completed(app, owner, project, session=str(uuid4()))
    session = run.sdk_session_id
    other = str(uuid4())
    native_fixture(tmp_path, project.id, session)
    config = tmp_path / "runtime-config/environment"
    for directory in ("file-history", "tasks", "uploads", "session-env", "image-cache"):
        for identifier in (session, other):
            folder = config / directory / identifier
            folder.mkdir(parents=True)
            (folder / "private.txt").write_text(identifier)
    (config / "history.jsonl").write_text(
        json.dumps({"sessionId": session, "display": "erase-private"})
        + "\n"
        + json.dumps({"sessionId": other, "display": "keep-private"})
        + "\n"
    )
    (config / ".credentials.json").write_text("keep-credentials")
    for directory in ("notifications", "feedback", "grants", "channel-deliveries"):
        folder = tmp_path / directory
        folder.mkdir(exist_ok=True)
        (folder / "selected.json").write_text(
            json.dumps({"run_id": run.id, "body": "erase-private"})
        )
        (folder / "other.json").write_text(json.dumps({"run_id": "other", "body": "keep-private"}))
    review = service.preview(project.id, owner, run_id=run.id)
    service.delete(project.id, run.id, owner, payload(review))
    assert "erase-private" not in (config / "history.jsonl").read_text()
    assert "keep-private" in (config / "history.jsonl").read_text()
    assert (config / ".credentials.json").read_text() == "keep-credentials"
    for directory in ("file-history", "tasks", "uploads", "session-env", "image-cache"):
        assert not (config / directory / session).exists()
        assert (config / directory / other / "private.txt").read_text() == other
    for directory in ("notifications", "feedback", "grants", "channel-deliveries"):
        assert not (tmp_path / directory / "selected.json").exists()
        assert "keep-private" in (tmp_path / directory / "other.json").read_text()


@pytest.mark.parametrize("attack", ["child_symlink", "duplicate", "wrong_cwd", "orphan"])
def test_native_ownership_and_link_attacks_fail_before_commit(tmp_path, attack):
    app, owner, project = setup(tmp_path)
    run = completed(app, owner, project, session=str(uuid4()))
    transcript, child = native_fixture(tmp_path, project.id, run.sdk_session_id)
    if attack == "child_symlink":
        (child / "outside").symlink_to(tmp_path / "projects")
    elif attack == "duplicate":
        folder = transcript.parent.parent / "duplicate"
        folder.mkdir()
        (folder / transcript.name).write_bytes(transcript.read_bytes())
    elif attack == "wrong_cwd":
        value = json.loads(transcript.read_text())
        value["cwd"] = str(tmp_path / "other-project")
        transcript.write_text(json.dumps(value) + "\n")
    else:
        transcript.unlink()
    with pytest.raises((ValueError, OSError)):
        app.state.conversation_erasure.preview(project.id, owner, run_id=run.id)
    assert app.state.ledger.get_run(run.id).prompt
    assert (child / "agent-test.jsonl").exists()
    assert not (tmp_path / ".data-erasure/pending").exists()


@pytest.mark.asyncio
async def test_human_api_authority_confirmation_and_request_lease_handoff(tmp_path):
    app, owner, project = setup(tmp_path)
    run = completed(app, owner, project, session=str(uuid4()))
    path = f"/api/projects/{project.id}/conversations/{run.id}/deletion"
    member = principal("member", PrincipalRole.MEMBER)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (
            await request_as(client, "GET", path, member.principal_id, member.role)
        ).status_code == 403
        review = await request_as(client, "GET", path, owner.principal_id, owner.role)
        assert review.status_code == 200, review.text
        assert review.headers["cache-control"] == "no-store"
        body = payload(review.json()).model_dump(mode="json")
        denied = await request_as(client, "POST", path, member.principal_id, member.role, json=body)
        assert denied.status_code == 403
        bad = await request_as(
            client,
            "POST",
            path,
            owner.principal_id,
            owner.role,
            json={**body, "confirm_deletion": False},
        )
        assert bad.status_code == 422
        deleted = await request_as(client, "POST", path, owner.principal_id, owner.role, json=body)
        assert deleted.status_code == 200, deleted.text
        again = await request_as(client, "POST", path, owner.principal_id, owner.role, json=body)
        assert again.status_code == 200
        assert (
            await request_as(client, "GET", f"/api/runs/{run.id}", owner.principal_id, owner.role)
        ).status_code == 404


def test_agent_memory_mirror_comments_and_skill_source_summary_are_scrubbed(tmp_path):
    from test_skill_proposals import write_candidate

    from ophira_backend.application_backup import inspect_application
    from ophira_backend.constants import AgentTemplate
    from ophira_backend.models import AgentCreate, utc_now
    from ophira_backend.project_comments import CommentAnchor, CommentCreate, ProjectComments

    app, owner, project = setup(tmp_path)
    ledger, service = app.state.ledger, app.state.conversation_erasure
    agent = ledger.create_agent(AgentCreate(name="Test agent", template=AgentTemplate.GENERAL))
    session = str(uuid4())
    source = completed(app, owner, project, session=session, agent_id=agent.id)
    source = ledger.update_run(source.id, started_at=utc_now())
    main = completed(app, owner, project, session=session)
    main = ledger.update_run(main.id, started_at=utc_now())
    artifact, _ = write_candidate(ledger, project, main, related_run_ids=[source.id])
    proposal = app.state.skill_proposals.submit_artifact(main.id, artifact.id)
    comments = ProjectComments(ledger, app.state.project_access, service.settings)
    comments.create(
        project.id,
        CommentCreate(
            request_id="human-comment-1234",
            text="keep human text",
            anchor=CommentAnchor(kind="run", id=source.id),
        ),
        owner,
    )
    memory = app.state.memory.propose(
        scope=MemoryScope.AGENT,
        scope_id=agent.id,
        text="private agent memory",
        source=MemorySource.USER,
        kind=MemoryKind.PREFERENCE,
        source_run_id=source.id,
        principal=owner,
    )
    app.state.memory.decide(memory.id, "approve")
    mirror = tmp_path / "agents" / agent.id / "MEMORY.md"
    with mirror.open("a") as handle:
        handle.write("\nUser-written unrelated note\n")
    review = service.preview(project.id, owner, run_id=source.id)
    service.delete(project.id, source.id, owner, payload(review))
    assert "private agent memory" not in mirror.read_text()
    assert "User-written unrelated note" in mirror.read_text()
    record = json.loads((tmp_path / "collaboration/comments" / f"{project.id}.json").read_text())
    assert record["comments"][0]["target"]["label"] == "Deleted conversation"
    assert record["comments"][0]["history"][0]["text"] == "keep human text"
    retained = app.state.skill_proposals.peek(proposal.id)
    assert retained.source_runs[0].prompt_excerpt == ""
    assert retained.content_hash == proposal.content_hash
    app.state.skills.snapshot()
    assert proposal.id in inspect_application(tmp_path, app.state.skills.plugin_dir)["proposals"]


def test_pending_memory_write_refuses_erasure_without_leaving_private_shadow_copies(tmp_path):
    app, owner, project = setup(tmp_path)
    run = completed(app, owner, project, session=str(uuid4()))
    app.state.memory.propose(
        scope=MemoryScope.PROJECT,
        scope_id=project.id,
        text="private shadow",
        source=MemorySource.USER,
        kind=MemoryKind.PREFERENCE,
        source_run_id=run.id,
        principal=owner,
    )
    folder = tmp_path / "memory/project" / project.id
    (folder / "items.json.tmp").write_bytes((folder / "items.json").read_bytes())
    with pytest.raises(ValueError, match="pending or unknown"):
        app.state.conversation_erasure.preview(project.id, owner, run_id=run.id)
    assert app.state.ledger.get_run(run.id).prompt


def test_failed_continuation_without_session_init_keeps_both_directions_in_scope(tmp_path):
    app, owner, project = setup(tmp_path)
    first = completed(app, owner, project, session=None)
    second = completed(app, owner, project, session=None, resume_source_run_id=first.id)
    service = app.state.conversation_erasure
    review = service.preview(project.id, owner, run_id=second.id)
    assert set(review["run_ids"]) == {first.id, second.id}
    service.delete(project.id, second.id, owner, payload(review))
    assert (
        service.delete(project.id, second.id, owner, payload(review))["run_ids"]
        == review["run_ids"]
    )
    assert app.state.ledger.list_runs(project.id) == []

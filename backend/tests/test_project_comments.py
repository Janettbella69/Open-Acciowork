"""Human discussion history and authorization, against real durable stores."""

import json
import multiprocessing
import os
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from test_auth_api import FakeRuntime, principal, request_as, trusted_settings

from ophira_backend.auth import (
    AuthAssertionExpiredError,
    AuthorizationError,
    ProjectAccessStore,
    ProjectPermission,
)
from ophira_backend.config import Settings
from ophira_backend.constants import EventType, EvidenceKind, PrincipalRole
from ophira_backend.events import EventPublisher
from ophira_backend.main import _project_route_permission, create_app
from ophira_backend.models import Decision, EventActor, Evidence, ProjectCreate
from ophira_backend.project_comments import (
    CollaborationConflict,
    CollaborationUnavailable,
    CommentAnchor,
    CommentChange,
    CommentCreate,
    CommentLedger,
    ProjectComments,
)
from ophira_backend.storage import FileLedger, LedgerNotFoundError


def make_store(root):
    settings = Settings(_env_file=None, data_dir=root)
    ledger = FileLedger(root)
    return ProjectComments(ledger, ProjectAccessStore(root / "auth" / "projects"), settings)


def test_comment_permission_matches_only_the_project_discussion_resource():
    assert (
        _project_route_permission("POST", "/api/projects/project_1/comments")
        == ProjectPermission.COMMENT_WRITE
    )
    assert (
        _project_route_permission("PATCH", "/api/projects/project_1/comments/comment_1")
        == ProjectPermission.COMMENT_WRITE
    )
    assert (
        _project_route_permission("PUT", "/api/projects/project_1/artifacts/comments_file/text")
        == ProjectPermission.PROJECT_WRITE
    )


def add(service, project_id, text="Check the quote", actor=None, **kwargs):
    return service.create(
        project_id,
        CommentCreate(
            request_id=kwargs.pop("request_id", uuid4().hex),
            text=text,
            **kwargs,
        ),
        actor,
    )


def change(service, project_id, item, action, actor=None, **kwargs):
    return service.change(
        project_id,
        item["id"],
        CommentChange(
            request_id=kwargs.pop("request_id", uuid4().hex),
            expected_revision=item["revision"],
            action=action,
            **kwargs,
        ),
        actor,
    )


@pytest.fixture
def discussion(tmp_path):
    service = make_store(tmp_path)
    project = service.ledger.create_project(ProjectCreate(name="Team", objective="Review sources"))
    return service, project


def test_thread_edit_removal_resolution_and_history_survive_restart(discussion):
    service, project = discussion
    original_project = service.ledger.get_project(project.id)
    root = add(service, project.id)
    reply = add(service, project.id, "Need two quotes", parent_id=root["id"])
    edited = change(service, project.id, root, "edit", text="Check the current quote")
    resolved = change(service, project.id, edited, "resolve")
    removed = change(service, project.id, reply, "remove")
    assert removed["text"] is None and not removed["actions"]["edit"]
    restarted = make_store(service.ledger.root)
    thread = restarted.thread(project.id, root["id"], None)
    assert thread["root"]["text"] == "Check the current quote"
    assert thread["root"]["state"] == "resolved"
    assert thread["replies"][0]["text"] is None
    assert restarted.list(project.id, None)["items"][0]["reply_count"] == 1
    history = restarted.history(project.id, root["id"], None)["revisions"]
    assert [row["action"] for row in history] == ["create", "edit", "resolve"]
    assert history[0]["text"] == "Check the quote"
    assert "request_hash" not in json.dumps(history, default=str)
    assert change(restarted, project.id, resolved, "reopen")["state"] == "open"
    assert service.ledger.get_project(project.id) == original_project
    assert not list(service.ledger.workspace_for_project(project.id).iterdir())
    assert service._path(project.id).stat().st_mode & 0o777 == 0o600


def test_retries_are_idempotent_and_stale_edits_do_not_overwrite(discussion):
    service, project = discussion
    key = uuid4().hex
    root = add(service, project.id, request_id=key)
    assert add(service, project.id, request_id=key)["id"] == root["id"]
    with pytest.raises(CollaborationConflict, match="different content"):
        add(service, project.id, "Changed payload", request_id=key)
    update_key = uuid4().hex
    edited = change(service, project.id, root, "edit", text="Updated", request_id=update_key)
    assert (
        change(service, project.id, root, "edit", text="Updated", request_id=update_key) == edited
    )
    with pytest.raises(CollaborationConflict, match="different content"):
        change(service, project.id, root, "edit", text="Overwritten", request_id=update_key)
    with pytest.raises(CollaborationConflict, match="reload"):
        change(service, project.id, root, "edit", text="Stale editor")
    removed = change(service, project.id, edited, "remove")
    with pytest.raises(CollaborationConflict, match="Removed"):
        change(service, project.id, removed, "edit", text="Resurrect")
    assert len(service.history(project.id, root["id"], None)["revisions"]) == 3


@pytest.mark.asyncio
async def test_targets_resolve_to_saved_versions_and_only_public_project_messages(discussion):
    service, project = discussion
    ledger = service.ledger
    run = ledger.create_run(project.id, "Review comparison")
    message = await EventPublisher(ledger).publish(
        run.id,
        EventType.ASSISTANT_MESSAGE_COMPLETED,
        {"text": "Visible conclusion"},
    )
    hidden = await EventPublisher(ledger).publish(
        run.id,
        EventType.ASSISTANT_MESSAGE_COMPLETED,
        {"text": "Branch result"},
        actor=EventActor(type="subagent", name="research", agent_id="child"),
    )
    artifact = ledger.save_upload(project.id, "quote.txt", b"One quote")
    evidence = Evidence(
        kind=EvidenceKind.USER_STATEMENT,
        claim="Quote needs validation",
        source_title="Supplier",
        confidence=0.4,
        recorded_by="main",
    )
    decision = Decision(question="Proceed?", outcome="Test first", made_by="main")
    ledger.update_project(project.id, lambda value: value.evidence.append(evidence))
    ledger.update_project(project.id, lambda value: value.decisions.append(decision))
    targets = [
        ("run", run.id),
        ("evidence", evidence.id),
        ("decision", decision.id),
        ("artifact", artifact.id),
        ("message", message.id),
    ]
    for kind, identifier in targets:
        item = add(
            service,
            project.id,
            anchor=CommentAnchor(
                kind=kind,
                id=identifier,
                run_id=run.id if kind == "message" else None,
            ),
        )
        reply = add(service, project.id, parent_id=item["id"])
        assert reply["target"] == item["target"]
        if kind == "artifact":
            assert item["target"]["artifact_checksum"] == artifact.checksum
            assert item["target"]["artifact_version"] == 1
        if kind == "message":
            assert item["target"]["trace_id"] == run.trace_id
            assert item["target"]["label"] == "Visible conclusion"
    with pytest.raises(LedgerNotFoundError):
        add(service, project.id, anchor=CommentAnchor(kind="message", id=hidden.id, run_id=run.id))
    other = ledger.create_project(ProjectCreate(name="Other", objective="Different team"))
    with pytest.raises(AuthorizationError):
        add(service, other.id, anchor=CommentAnchor(kind="run", id=run.id))
    with pytest.raises(LedgerNotFoundError):
        add(service, other.id, anchor=CommentAnchor(kind="artifact", id=artifact.id))
    with pytest.raises(LedgerNotFoundError):
        add(service, other.id, parent_id=item["id"])
    with pytest.raises(ValueError, match="inherit"):
        add(
            service,
            project.id,
            parent_id=item["id"],
            anchor=CommentAnchor(kind="decision", id=decision.id),
        )
    with pytest.raises(LedgerNotFoundError):
        add(service, project.id, parent_id=reply["id"])


def test_cursor_pages_are_stable_when_new_threads_and_replies_arrive(discussion):
    service, project = discussion
    roots = [add(service, project.id, str(index)) for index in range(4)]
    first = service.list(project.id, None, limit=2)
    add(service, project.id, "Arrived later")
    second = service.list(project.id, None, limit=2, before=first["next_before"])
    assert [row["id"] for row in first["items"] + second["items"]] == [
        row["id"] for row in reversed(roots)
    ]
    assert second["next_before"] is None
    replies = [add(service, project.id, str(index), parent_id=roots[0]["id"]) for index in range(3)]
    page = service.thread(project.id, roots[0]["id"], None, limit=2)
    last = service.thread(project.id, roots[0]["id"], None, after=page["next_after"])
    assert [row["id"] for row in page["replies"] + last["replies"]] == [
        row["id"] for row in replies
    ]


def test_redaction_scope_integrity_bounds_and_linked_history_fail_closed(discussion, monkeypatch):
    service, project = discussion
    root = add(service, project.id, "Use token=private-secret safely")
    assert "private-secret" not in root["text"]
    raw = service._path(project.id).read_text()
    assert "private-secret" not in raw
    with pytest.raises(ValueError, match="blank"):
        add(service, project.id, "   ")
    monkeypatch.setattr("ophira_backend.project_comments.MAX_COMMENTS", 1)
    with pytest.raises(CollaborationConflict, match="capacity"):
        add(service, project.id)
    data = json.loads(raw)
    data["comments"][0]["sequence"] = 9
    service._path(project.id).write_text(json.dumps(data))
    with pytest.raises(CollaborationUnavailable, match="verified"):
        service.list(project.id, None)
    service._path(project.id).write_text(raw)
    monkeypatch.setattr("ophira_backend.project_comments.MAX_COMMENT_BYTES", 10)
    with pytest.raises(CollaborationUnavailable, match="read limit"):
        service.list(project.id, None)
    service._path(project.id).unlink()
    external = service.root / "external.json"
    external.write_text(raw)
    service._path(project.id).symlink_to(external)
    with pytest.raises(OSError):
        service.list(project.id, None)


def test_write_limit_accounts_for_the_exact_ledger_serialization(discussion, monkeypatch):
    service, project = discussion
    add(service, project.id)
    original = service._path(project.id).read_bytes()
    record = CommentLedger.model_validate_json(original)
    monkeypatch.setattr("ophira_backend.project_comments.MAX_COMMENT_BYTES", len(original) - 1)
    with pytest.raises(CollaborationConflict, match="storage is full"):
        service._commit(record, None)
    assert service._path(project.id).read_bytes() == original


def comment_process(root, project_id, data, ready, start, output, *, editing=False, crash=False):
    service = make_store(Path(root))
    ready.put(True)
    if not start.wait(15):
        raise TimeoutError("start missing")
    if crash:
        original = service.ledger._atomic_write

        def save_then_crash(path, model):
            original(path, model)
            os._exit(73)

        service.ledger._atomic_write = save_then_crash
    try:
        value = (
            service.change(project_id, data["comment_id"], CommentChange(**data["payload"]), None)
            if editing
            else service.create(project_id, CommentCreate(**data), None)
        )
        output.put(value["id"])
    except CollaborationConflict:
        output.put("conflict")


def authenticated_writer(root, project_id, ready, release, result):
    settings = trusted_settings(Path(root))
    access = ProjectAccessStore(Path(root) / "auth" / "projects")
    service = ProjectComments(FileLedger(Path(root)), access, settings)
    original = service._commit

    def pause_commit(record, actor):
        ready.set()
        if not release.wait(15):
            raise TimeoutError("commit release missing")
        original(record, actor)

    service._commit = pause_commit
    result.put(add(service, project_id, actor=principal("writer", PrincipalRole.MEMBER))["id"])


def revoke_writer(root, project_id, started, finished):
    access = ProjectAccessStore(Path(root) / "auth" / "projects")
    started.set()
    access.revoke(project_id, principal("owner", PrincipalRole.OWNER), principal_id="writer")
    finished.set()


def test_cross_process_membership_lease_prevents_revocation_racing_a_comment_commit(tmp_path):
    ledger = FileLedger(tmp_path)
    access = ProjectAccessStore(tmp_path / "auth" / "projects")
    owner = principal("owner", PrincipalRole.OWNER)
    project = ledger.create_project(ProjectCreate(name="Shared", objective="Race"), principal=owner)
    access.bind_project(project.id, owner)
    access.grant(project.id, owner, principal_id="writer", role=PrincipalRole.MEMBER)
    context = multiprocessing.get_context("spawn")
    ready, release, started, finished = (context.Event() for _ in range(4))
    result = context.Queue()
    writer = context.Process(
        target=authenticated_writer, args=(str(tmp_path), project.id, ready, release, result)
    )
    revoker = context.Process(
        target=revoke_writer, args=(str(tmp_path), project.id, started, finished)
    )
    try:
        writer.start()
        assert ready.wait(15)
        revoker.start()
        assert started.wait(15)
        assert not finished.wait(0.2)
        release.set()
        assert result.get(timeout=15).startswith("comment_")
        assert finished.wait(15)
        writer.join(15)
        revoker.join(15)
        assert writer.exitcode == revoker.exitcode == 0
        service = ProjectComments(ledger, access, trusted_settings(tmp_path))
        assert len(service.list(project.id, owner)["items"]) == 1
        with pytest.raises(AuthorizationError):
            service.list(project.id, principal("writer", PrincipalRole.MEMBER))
    finally:
        release.set()
        for process in (writer, revoker):
            if process.is_alive():
                process.kill()
                process.join(5)
        result.close()


@pytest.mark.parametrize("editing", [False, True])
def test_real_workers_share_one_commit_and_edit_conflicts(discussion, editing):
    service, project = discussion
    root = add(service, project.id)
    context = multiprocessing.get_context("spawn")
    ready, output, start = context.Queue(), context.Queue(), context.Event()
    payload = {"request_id": uuid4().hex, "text": "Simultaneous submission"}
    inputs = [
        {
            "comment_id": root["id"],
            "payload": {
                "request_id": uuid4().hex,
                "action": "edit",
                "expected_revision": 1,
                "text": f"Editor {i}",
            },
        }
        if editing
        else payload
        for i in range(2)
    ]
    processes = [
        context.Process(
            target=comment_process,
            args=(
                str(service.ledger.root),
                project.id,
                data,
                ready,
                start,
                output,
            ),
            kwargs={"editing": editing},
        )
        for data in inputs
    ]
    try:
        for process in processes:
            process.start()
        assert all(ready.get(timeout=15) for _ in processes)
        start.set()
        results = [output.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
        if editing:
            assert sorted(results) == sorted([root["id"], "conflict"])
            assert len(service.history(project.id, root["id"], None)["revisions"]) == 2
        else:
            assert len(set(results)) == 1 and results[0].startswith("comment_")
            assert len(service.list(project.id, None)["items"]) == 2
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(5)
        ready.close()
        output.close()


def test_killed_worker_after_commit_can_retry_without_duplicate_comment(discussion):
    service, project = discussion
    payload = {"request_id": uuid4().hex, "text": "Saved before process exit"}
    context = multiprocessing.get_context("spawn")
    ready, output, start = context.Queue(), context.Queue(), context.Event()
    process = context.Process(
        target=comment_process,
        args=(
            str(service.ledger.root),
            project.id,
            payload,
            ready,
            start,
            output,
        ),
        kwargs={"crash": True},
    )
    try:
        process.start()
        assert ready.get(timeout=15)
        start.set()
        process.join(15)
        assert process.exitcode == 73
        restarted = make_store(service.ledger.root)
        item = restarted.create(project.id, CommentCreate(**payload), None)
        assert item["text"] == payload["text"] and item["revision"] == 1
        assert len(restarted.list(project.id, None)["items"]) == 1
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        ready.close()
        output.close()


@pytest.mark.asyncio
async def test_trusted_api_enforces_roles_authorship_revocation_and_membership_revision(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        owner_id = "owner@example.com"
        owner = principal(owner_id, PrincipalRole.OWNER)
        project = (
            await request_as(
                client,
                "POST",
                "/api/projects",
                owner_id,
                owner.role,
                json={"name": "Shared", "objective": "Discuss"},
            )
        ).json()
        path = f"/api/projects/{project['id']}"
        roles = [PrincipalRole.MEMBER, PrincipalRole.REVIEWER, PrincipalRole.VIEWER]
        for role in roles:
            app.state.project_access.grant(
                project["id"], owner, principal_id=f"{role}@example.com", role=role
            )
        created = await request_as(
            client,
            "POST",
            path + "/comments",
            "reviewer@example.com",
            PrincipalRole.REVIEWER,
            json={"text": "Review <script>this</script>", "request_id": uuid4().hex},
        )
        assert created.status_code == 200, created.text
        assert created.headers["cache-control"] == "no-store"
        root = created.json()
        assert root["author_id"] == "reviewer@example.com"
        assert root["actions"]["edit"] is True
        for role in roles:
            user = f"{role}@example.com"
            read = await request_as(client, "GET", path + "/comments", user, role)
            assert read.status_code == 200
            assert read.json()["items"][0]["actions"]["edit"] == (role == PrincipalRole.REVIEWER)
            posting = await request_as(
                client,
                "POST",
                path + "/comments",
                user,
                role,
                json={"text": "Reply", "parent_id": root["id"], "request_id": uuid4().hex},
            )
            assert posting.status_code == (403 if role == PrincipalRole.VIEWER else 200)
        for user, role in [("member@example.com", PrincipalRole.MEMBER), (owner_id, owner.role)]:
            edit = await request_as(
                client,
                "PATCH",
                path + "/comments/" + root["id"],
                user,
                role,
                json={
                    "request_id": uuid4().hex,
                    "expected_revision": 1,
                    "action": "edit",
                    "text": "Spoofed author",
                },
            )
            assert edit.status_code == 403  # Moderators cannot rewrite another author's words.
        history = await request_as(
            client,
            "GET",
            path + f"/comments/{root['id']}/history",
            "member@example.com",
            PrincipalRole.MEMBER,
        )
        assert history.status_code == 403
        removed = await request_as(
            client,
            "PATCH",
            path + f"/comments/{root['id']}",
            owner_id,
            owner.role,
            json={"request_id": uuid4().hex, "expected_revision": 1, "action": "remove"},
        )
        assert removed.status_code == 200 and removed.json()["text"] is None
        caps = await request_as(client, "GET", path + "/collaboration", owner_id, owner.role)
        assert caps.json()["can_manage_members"] is True
        assert caps.json()["grantable_roles"] == ["admin", "member", "reviewer", "viewer"]
        access = (await request_as(client, "GET", path + "/access", owner_id, owner.role)).json()
        payload = {
            "principal_id": "new+region:01@example.com",
            "role": "member",
            "expected_revision": access["revision"],
        }
        assert (
            await request_as(client, "POST", path + "/access", owner_id, owner.role, json=payload)
        ).status_code == 200
        stale = await request_as(
            client, "POST", path + "/access", owner_id, owner.role, json=payload
        )
        assert stale.status_code == 409
        assert stale.headers["cache-control"] == "no-store"
        stale_revoke = await request_as(
            client,
            "DELETE",
            path + f"/access/{payload['principal_id']}?expected_revision={access['revision']}",
            owner_id,
            owner.role,
        )
        assert stale_revoke.status_code == 409
        current_revision = app.state.project_access.get(project["id"]).revision
        revoked = await request_as(
            client, "DELETE",
            path + f"/access/{payload['principal_id']}?expected_revision={current_revision}",
            owner_id, owner.role,
        )
        assert revoked.status_code == 200
        assert payload["principal_id"] not in revoked.json()["memberships"]
        app.state.project_access.revoke(project["id"], owner, principal_id="reviewer@example.com")
        for suffix in [
            "/comments",
            "/collaboration",
            f"/comments/{root['id']}/thread",
            f"/comments/{root['id']}/history",
        ]:
            denied = await request_as(
                client, "GET", path + suffix, "reviewer@example.com", PrincipalRole.REVIEWER
            )
            assert denied.status_code == 403
            assert denied.headers["cache-control"] == "no-store"
        with pytest.raises(AuthorizationError):
            app.state.project_comments.list(project["id"], None)


@pytest.mark.parametrize(
    ("suffix", "error_type", "fail_at"),
    [
        ("comments", AuthAssertionExpiredError, 1),
        ("comments", AuthAssertionExpiredError, 3),
        ("collaboration", AuthAssertionExpiredError, 2),
        ("access", AuthorizationError, 2),
        ("access", AuthAssertionExpiredError, 2),
    ],
)
@pytest.mark.asyncio
async def test_authority_changes_between_request_checks_return_private_denial(
    tmp_path,
    monkeypatch,
    suffix,
    error_type,
    fail_at,
):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    owner = principal("owner", PrincipalRole.OWNER)
    project = app.state.ledger.create_project(
        ProjectCreate(name="Shared", objective="Review"),
        principal=owner,
    )
    app.state.project_access.bind_project(project.id, owner)
    original = app.state.project_access.authorize
    checks = 0

    def authority(*args, **kwargs):
        nonlocal checks
        checks += 1
        if checks == fail_at:
            raise error_type("private authentication diagnostic")
        return original(*args, **kwargs)

    monkeypatch.setattr(app.state.project_access, "authorize", authority)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        response = await request_as(
            client, "GET", f"/api/projects/{project.id}/{suffix}", "owner", PrincipalRole.OWNER
        )
        assert response.status_code == 403
        assert response.headers["cache-control"] == "no-store"
        assert "private authentication diagnostic" not in response.text


@pytest.mark.asyncio
async def test_local_api_rejects_identity_injection_and_bounds_request_without_echo(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=FakeRuntime())
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://127.0.0.1"
    ) as client:
        project = (
            await client.post("/api/projects", json={"name": "Local", "objective": "Review"})
        ).json()
        path = f"/api/projects/{project['id']}"
        caps = (await client.get(path + "/collaboration")).json()
        assert caps["identity_mode"] == "local_only"
        assert caps["can_comment"] and not caps["can_manage_members"]
        for extra in [{"author_id": "someone-else"}, {"text": "token=private-secret" * 500}]:
            invalid = await client.post(
                path + "/comments",
                json={"request_id": uuid4().hex, "text": "token=private-secret", **extra},
            )
            assert invalid.status_code == 422
            assert "private-secret" not in invalid.text
        huge = await client.post(path + "/comments", content=b"x" * (32 * 1024 + 1))
        assert huge.status_code == 413
        assert huge.headers["cache-control"] == "no-store"
        assert (await client.get(path + "/comments?limit=101")).status_code == 422
        assert (await client.get(path + "/comments?before=-1")).status_code == 422

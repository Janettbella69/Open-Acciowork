import hashlib
import io
import json
import os
import zipfile
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from test_auth_api import FakeRuntime, principal, request_as, trusted_settings

from ophira_backend import project_exports
from ophira_backend.auth import AuthenticationError, AuthorizationError
from ophira_backend.constants import (
    EventType,
    MemoryKind,
    MemoryScope,
    MemorySource,
    PrincipalRole,
    RunStatus,
)
from ophira_backend.main import create_app
from ophira_backend.models import EventActor, ProjectCreate, utc_now
from ophira_backend.project_export_api import PrivateArchiveResponse
from ophira_backend.project_exports import ProjectExportError
from ophira_backend.workspace_checkpoints import workspace_lease


def setup(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    owner = principal("owner", PrincipalRole.OWNER)
    ledger = app.state.ledger
    project = ledger.create_project(
        ProjectCreate(name="Private project", objective="Export it"), principal=owner
    )
    app.state.project_access.bind_project(project.id, owner)
    return app, owner, project


def completed(app, owner, project, *, session="session-one", **kwargs):
    run = app.state.ledger.create_run(project.id, "Original user prompt", principal=owner, **kwargs)
    return app.state.ledger.update_run(
        run.id, status=RunStatus.COMPLETED, sdk_session_id=session, finished_at=utc_now()
    )


def read_archive(archive):
    with zipfile.ZipFile(archive) as zipped:
        files = {name: zipped.read(name) for name in zipped.namelist()}
    manifest = json.loads(files["manifest.json"])
    for item in manifest["files"]:
        assert len(files[item["path"]]) == item["size"]
        assert hashlib.sha256(files[item["path"]]).hexdigest() == item["sha256"]
    expected = dict(manifest)
    for name in ("fingerprint", "exported_at", "exported_by"):
        expected.pop(name)
    assert hashlib.sha256(project_exports.encode(expected)).hexdigest() == manifest["fingerprint"]
    return files, manifest


@pytest.mark.asyncio
async def test_project_archive_has_exact_files_history_conversation_and_project_records(tmp_path):
    app, owner, project = setup(tmp_path)
    ledger = app.state.ledger
    run = completed(app, owner, project)
    await app.state.events.publish(run.id, EventType.ASSISTANT_TEXT_DELTA, {"text": "Answer"})
    await app.state.events.publish(
        run.id, EventType.ASSISTANT_MESSAGE_COMPLETED, {"text": "Answer"}
    )
    await app.state.events.publish(run.id, EventType.ASSISTANT_TEXT_DELTA, {"text": "unfinished"})
    workspace = ledger.workspace_for_project(project.id)
    (workspace / "quotation.txt").write_text("version-one")
    first = ledger.register_artifact(project.id, "quotation.txt", run_id=run.id)
    (workspace / "quotation.txt").write_text("version-two")
    second = ledger.register_artifact(project.id, "quotation.txt", run_id=run.id)
    (workspace / "empty").mkdir()
    for directory in (
        "notifications",
        "intents",
        "feedback",
        "grants",
        "channel-deliveries",
        "shop-browsers",
        "task-experiences",
    ):
        folder = tmp_path / directory
        folder.mkdir(exist_ok=True)
        (folder / "own.json").write_text(json.dumps({"project_id": project.id, "value": directory}))
        (folder / "other.json").write_text(
            '{"project_id":"other","value":"foreign-private-canary"}'
        )
    app.state.memory.propose(
        scope=MemoryScope.PROJECT,
        scope_id=project.id,
        text="Project preference",
        kind=MemoryKind.PREFERENCE,
        source=MemorySource.USER,
        principal=owner,
    )
    preview = app.state.project_exports.preview(project.id, owner)
    archive, size = app.state.project_exports.create(project.id, owner, preview["fingerprint"])
    assert os.fstat(archive.fileno()).st_mode & 0o777 == 0o600
    files, manifest = read_archive(archive)
    assert size > 0 and manifest["exported_by"] == "owner"
    assert files[f"data/projects/{project.id}/workspace/quotation.txt"] == b"version-two"
    for item, expected in ((first, b"version-one"), (second, b"version-two")):
        assert files[f"data/projects/{project.id}/artifact-versions/{item.checksum}"] == expected
    assert files["conversation.md"].count(b"Answer") == 1
    assert (
        b"Assistant (partial)" in files["conversation.md"]
        and b"unfinished" in files["conversation.md"]
    )
    assert b"Original user prompt" in files["conversation.md"]
    assert b"Project preference" in files["project-memory.json"]
    assert not any(b"foreign-private-canary" in data for data in files.values())
    assert f"data/projects/{project.id}/workspace/empty/" in files
    archive.close()


@pytest.mark.asyncio
async def test_session_groups_resumptions_without_exporting_other_conversations_or_files(tmp_path):
    app, owner, project = setup(tmp_path)
    first = completed(app, owner, project)
    resumed = completed(
        app, owner, project, resume_session_id=first.sdk_session_id, resume_source_run_id=first.id
    )
    unrelated = completed(app, owner, project, session="session-two")
    workspace = app.state.ledger.workspace_for_project(project.id)
    (workspace / "selected.txt").write_text("selected-output")
    artifact = app.state.ledger.register_artifact(project.id, "selected.txt", run_id=resumed.id)
    (workspace / "other.txt").write_text("private-other-output")
    app.state.ledger.register_artifact(project.id, "other.txt", run_id=unrelated.id)
    preview = app.state.project_exports.preview(project.id, owner, run_id=resumed.id)
    assert set(preview["run_ids"]) == {first.id, resumed.id}
    archive, _ = app.state.project_exports.create(
        project.id, owner, preview["fingerprint"], run_id=resumed.id
    )
    files, _ = read_archive(archive)
    assert (
        b"selected-output"
        in files[f"data/projects/{project.id}/artifact-versions/{artifact.checksum}"]
    )
    assert all(b"private-other-output" not in raw for raw in files.values())
    assert all(unrelated.id not in path for path in files)
    assert not any(path.endswith("project.json") for path in files)
    archive.close()


@pytest.mark.asyncio
async def test_owner_http_preview_and_stream_download_and_non_owner_denial(tmp_path):
    app, owner, project = setup(tmp_path)
    completed(app, owner, project)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        path = f"/api/projects/{project.id}/data-export"
        preview = await request_as(client, "GET", path, "owner", PrincipalRole.OWNER)
        assert preview.status_code == 200 and preview.headers["cache-control"] == "no-store"
        response = await request_as(
            client,
            "POST",
            path,
            "owner",
            PrincipalRole.OWNER,
            json={"fingerprint": preview.json()["fingerprint"]},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert int(response.headers["content-length"]) == len(response.content)
        read_archive(io.BytesIO(response.content))
        for role in (PrincipalRole.MEMBER, PrincipalRole.REVIEWER, PrincipalRole.VIEWER):
            app.state.project_access.grant(project.id, owner, principal_id="member", role=role)
            for method in ("GET", "POST"):
                response = await request_as(client, method, path, "member", role)
                assert response.status_code == 403
        assert (await client.get(path)).status_code == 401


@pytest.mark.parametrize("change", ["file", "run", "record", "memory"])
def test_review_rejects_mutations_in_every_project_area(tmp_path, change):
    app, owner, project = setup(tmp_path)
    completed(app, owner, project)
    preview = app.state.project_exports.preview(project.id, owner)
    if change == "file":
        (app.state.ledger.workspace_for_project(project.id) / "new.txt").write_text("new")
    elif change == "run":
        completed(app, owner, project)
    elif change == "record":
        (tmp_path / "notifications/new.json").write_text(json.dumps({"project_id": project.id}))
    else:
        app.state.memory.propose(
            scope=MemoryScope.PROJECT,
            scope_id=project.id,
            text="Changed",
            kind=MemoryKind.FACT,
            source=MemorySource.USER,
            principal=owner,
        )
    with pytest.raises(ProjectExportError, match="changed"):
        app.state.project_exports.create(project.id, owner, preview["fingerprint"])


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "fifo", "parent"])
def test_untrusted_workspace_paths_never_escape_export(tmp_path, attack):
    app, owner, project = setup(tmp_path)
    workspace = app.state.ledger.workspace_for_project(project.id)
    outside = tmp_path / "private-source"
    outside.write_text("private-outside-canary")
    if attack == "symlink":
        (workspace / "linked").symlink_to(outside)
    elif attack == "hardlink":
        os.link(outside, workspace / "linked")
    elif attack == "fifo":
        os.mkfifo(workspace / "pipe")
    else:
        workspace.rmdir()
        workspace.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        app.state.project_exports.preview(project.id, owner)
    assert outside.read_text() == "private-outside-canary"


def test_execution_lease_and_nonterminal_run_prevent_inconsistent_export(tmp_path):
    app, owner, project = setup(tmp_path)
    service = app.state.project_exports
    with workspace_lease(app.state.ledger, project.id), pytest.raises(ValueError, match="in use"):
        service.preview(project.id, owner)
    app.state.ledger.create_run(project.id, "Queued", principal=owner)
    with pytest.raises(ProjectExportError, match="finish"):
        service.preview(project.id, owner)


@pytest.mark.parametrize(
    "limit",
    ["MAX_FILES", "MAX_ENTRIES", "MAX_TOTAL_BYTES", "MAX_SCAN_BYTES", "MAX_RUNS", "MAX_EVENTS"],
)
@pytest.mark.asyncio
async def test_limits_fail_explicitly_without_truncating(tmp_path, monkeypatch, limit):
    app, owner, project = setup(tmp_path)
    run = completed(app, owner, project)
    await app.state.events.publish(
        run.id, EventType.ASSISTANT_MESSAGE_COMPLETED, {"text": "response"}
    )
    monkeypatch.setattr(project_exports, limit, 0)
    with pytest.raises(ValueError):
        app.state.project_exports.preview(project.id, owner)


def test_vault_runtime_and_browser_private_data_are_never_opened(tmp_path, monkeypatch):
    app, owner, project = setup(tmp_path)
    completed(app, owner, project)
    original = project_exports.read_relative

    def protected(root, relative, limit):
        assert relative.split("/")[0] not in {
            "secrets",
            "runtime-config",
            "browser-profiles",
            "companion",
            "companion-push",
            "agents",
        }
        return original(root, relative, limit)

    monkeypatch.setattr(project_exports, "read_relative", protected)
    preview = app.state.project_exports.preview(project.id, owner)
    archive, _ = app.state.project_exports.create(project.id, owner, preview["fingerprint"])
    read_archive(archive)
    archive.close()


def test_export_detects_new_records_while_capturing_and_closes_private_temporary_file(
    tmp_path, monkeypatch
):
    app, owner, project = setup(tmp_path)
    preview = app.state.project_exports.preview(project.id, owner)
    temporary = project_exports.tempfile.TemporaryFile
    handles = []

    def capture(*args, **kwargs):
        result = temporary(*args, **kwargs)
        handles.append(result)
        return result

    write = zipfile.ZipFile.writestr
    changed = False

    def changing(zipped, *args, **kwargs):
        nonlocal changed
        result = write(zipped, *args, **kwargs)
        if not changed:
            changed = True
            (tmp_path / "notifications/late.json").write_text(
                json.dumps({"project_id": project.id})
            )
        return result

    monkeypatch.setattr(project_exports.tempfile, "TemporaryFile", capture)
    monkeypatch.setattr(zipfile.ZipFile, "writestr", changing)
    with pytest.raises(ProjectExportError, match="changed during"):
        app.state.project_exports.create(project.id, owner, preview["fingerprint"])
    assert len(handles) == 1 and handles[0].closed


@pytest.mark.parametrize("change", ["expiry", "membership"])
def test_export_rechecks_authority_during_capture(tmp_path, monkeypatch, change):
    app, owner, project = setup(tmp_path)
    admin = principal("admin", PrincipalRole.ADMIN)
    app.state.project_access.grant(
        project.id, owner, principal_id="admin", role=PrincipalRole.ADMIN
    )
    preview = app.state.project_exports.preview(project.id, admin)
    write = zipfile.ZipFile.writestr
    changed = False

    def revoking(zipped, *args, **kwargs):
        nonlocal changed
        result = write(zipped, *args, **kwargs)
        if not changed:
            changed = True
            if change == "expiry":
                app.state.project_access._clock = lambda: utc_now() + timedelta(hours=1)
            else:
                app.state.project_access.revoke(project.id, owner, principal_id="admin")
        return result

    monkeypatch.setattr(zipfile.ZipFile, "writestr", revoking)
    with pytest.raises((AuthenticationError, AuthorizationError)):
        app.state.project_exports.create(project.id, admin, preview["fingerprint"])


def test_memory_history_and_deletion_survive_portable_export(tmp_path):
    app, owner, project = setup(tmp_path)
    item = app.state.memory.propose(
        scope=MemoryScope.PROJECT,
        scope_id=project.id,
        text="private-forgotten-canary",
        kind=MemoryKind.FACT,
        source=MemorySource.USER,
        principal=owner,
    )
    history = tmp_path / "memory/project" / project.id / "history.jsonl"
    original = history.read_bytes()
    app.state.memory.delete(item.id, deleted_by="owner", reason="Remove it")
    # A previous crash can leave old history behind. Tombstones stay authoritative.
    history.write_bytes(original)
    preview = app.state.project_exports.preview(project.id, owner)
    archive, _ = app.state.project_exports.create(project.id, owner, preview["fingerprint"])
    files, _ = read_archive(archive)
    assert not any(b"private-forgotten-canary" in content for content in files.values())
    assert json.loads(files["project-memory.json"])["tombstones"][0]["memory_id"] == item.id
    archive.close()


@pytest.mark.parametrize("attack", ["link", "scope", "corrupt", "limit"])
def test_memory_export_rejects_unverified_scope_history_or_paths(tmp_path, attack):
    app, owner, project = setup(tmp_path)
    item = app.state.memory.propose(
        scope=MemoryScope.PROJECT,
        scope_id=project.id,
        text="Memory",
        kind=MemoryKind.FACT,
        source=MemorySource.USER,
        principal=owner,
    )
    path = tmp_path / "memory/project" / project.id / "items.json"
    if attack == "link":
        source = tmp_path / "outside-memory.json"
        path.rename(source)
        path.symlink_to(source)
    elif attack == "scope":
        values = item.model_dump(mode="json")
        values["scope_id"] = "somebody-else"
        path.write_text(json.dumps([values]))
    elif attack == "corrupt":
        path.with_name("history.jsonl").write_text('{"item":null}\n')
    else:
        with pytest.raises(ValueError):
            app.state.memory.export_scope(MemoryScope.PROJECT, project.id, max_bytes=1)
        return
    with pytest.raises((ValueError, OSError)):
        app.state.project_exports.preview(project.id, owner)


@pytest.mark.parametrize("tamper", ["tail", "sequence", "scope"])
@pytest.mark.asyncio
async def test_invalid_conversation_events_fail_closed(tmp_path, tamper):
    app, owner, project = setup(tmp_path)
    run = completed(app, owner, project)
    await app.state.events.publish(
        run.id, EventType.ASSISTANT_MESSAGE_COMPLETED, {"text": "answer"}
    )
    path = app.state.ledger._events_path(run.id)
    value = json.loads(path.read_text())
    if tamper == "sequence":
        value["sequence"] = 2
    elif tamper == "scope":
        value["project_id"] = "elsewhere"
    path.write_text(json.dumps(value) + ("" if tamper == "tail" else "\n"))
    with pytest.raises(ProjectExportError):
        app.state.project_exports.preview(project.id, owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["revoked", "disconnect"])
async def test_stream_rechecks_access_and_closes_on_disconnection(stop):
    from starlette.requests import ClientDisconnect

    archive = io.BytesIO(b"private-data" * 30_000)
    sent, checks = [], 0

    def authorize():
        nonlocal checks
        checks += 1
        if stop == "revoked" and checks > 1:
            raise AuthorizationError("revoked")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.body":
            if stop == "disconnect":
                raise OSError("client disconnected")
            sent.append(message["body"])

    response = PrivateArchiveResponse(archive, len(archive.getvalue()), authorize)
    with pytest.raises(AuthorizationError if stop == "revoked" else ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert archive.closed
    assert sum(map(len, sent)) <= 64 * 1024


def test_legacy_missing_bytes_are_explicit_but_missing_immutable_bytes_fail(tmp_path):
    app, owner, project = setup(tmp_path)
    ledger = app.state.ledger
    workspace = ledger.workspace_for_project(project.id)
    (workspace / "old.txt").write_text("old content")
    artifact = ledger.register_artifact(project.id, "old.txt")
    ledger.update_project(
        project.id, lambda value: setattr(value.artifacts[0], "content_storage", "workspace")
    )
    (workspace / "old.txt").unlink()
    preview = app.state.project_exports.preview(project.id, owner)
    assert preview["warnings"] == [{"code": "legacy_artifact_unavailable", "id": artifact.id}]
    ledger.update_project(
        project.id, lambda value: setattr(value.artifacts[0], "content_storage", "snapshot")
    )
    (ledger._project_path(project.id).parent / "artifact-versions" / artifact.checksum).unlink()
    with pytest.raises(ProjectExportError, match="artifact version"):
        app.state.project_exports.preview(project.id, owner)


def test_export_refuses_a_conversation_in_another_project(tmp_path):
    app, owner, project = setup(tmp_path)
    other = app.state.ledger.create_project(
        ProjectCreate(name="Other", objective="Private"), principal=owner
    )
    run = completed(app, owner, other)
    with pytest.raises(KeyError):
        app.state.project_exports.preview(project.id, owner, run_id=run.id)


@pytest.mark.parametrize(
    "first,second",
    [
        ("data/Report", "data/report/file.txt"),
        ("data/Report/file.txt", "data/report"),
        ("data/Report/one.txt", "data/report/two.txt"),
        ("data/caf\u00e9.txt", "data/cafe\u0301.txt"),
    ],
)
def test_zip_paths_cannot_alias_files_or_parent_directories(tmp_path, first, second):
    inventory = project_exports.ExportInventory(tmp_path, lambda: None)
    inventory.add(first, b"first")
    with pytest.raises(ProjectExportError, match="ambiguous paths"):
        inventory.add(second, b"second")


@pytest.mark.asyncio
async def test_readable_conversation_keeps_interleaved_branch_streams_separate(tmp_path):
    app, owner, project = setup(tmp_path)
    run = completed(app, owner, project)
    branch = EventActor(type="subagent", name="delegated-agent", agent_id="branch-one")
    events = app.state.events
    await events.publish(run.id, EventType.ASSISTANT_TEXT_DELTA, {"text": "Main partial"})
    await events.publish(
        run.id,
        EventType.ASSISTANT_TEXT_DELTA,
        {"text": "Branch draft"},
        actor=branch,
        parent_id="branch-one",
    )
    await events.publish(
        run.id,
        EventType.ASSISTANT_MESSAGE_COMPLETED,
        {"text": "Branch answer"},
        actor=branch,
        parent_id="branch-one",
    )
    await events.publish(run.id, EventType.ASSISTANT_TEXT_DELTA, {"text": " continues"})
    preview = app.state.project_exports.preview(project.id, owner)
    archive, _ = app.state.project_exports.create(project.id, owner, preview["fingerprint"])
    files, _ = read_archive(archive)
    conversation = files["conversation.md"].decode()
    assert '### Branch "branch-one"\n\nBranch answer' in conversation
    assert "### Assistant (partial)\n\nMain partial continues" in conversation
    assert "Branch draft" not in conversation
    archive.close()

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from test_auth_api import principal, request_as
from test_conversation_erasure import native_fixture
from test_conversation_erasure import payload as conversation_confirm
from test_project_exports import completed, setup
from test_project_templates import saved, use_request

from ophira_backend.application_backup import create_backup, inspect_application, restore_backup
from ophira_backend.auth import AuthenticationError, AuthorizationError
from ophira_backend.companion import PairingCreate
from ophira_backend.constants import MemoryKind, MemoryScope, MemorySource, PrincipalRole, RunStatus
from ophira_backend.deletion_models import ErasedProject, ErasedRun, ProjectDeletionRequest
from ophira_backend.erasure_files import ErasureError, FileErasure, operation_lease
from ophira_backend.models import ProjectCreate, Schedule, utc_now
from ophira_backend.project_templates import TemplateConflict
from ophira_backend.shop_browser_store import ShopBrowserStore, ShopIdentity
from ophira_backend.storage import FileLedger, LedgerNotFoundError


def confirm(preview, **changes):
    return ProjectDeletionRequest(
        **(
            {
                key: preview[key]
                for key in (
                    "request_id",
                    "reviewed_at",
                    "fingerprint",
                )
            }
            | {"confirm_deletion": True, "confirm_project_name": preview["project_name"]}
            | changes
        )
    )


def other_project(app, owner):
    project = app.state.ledger.create_project(
        ProjectCreate(name="Other", objective="Keep"), principal=owner
    )
    app.state.project_access.bind_project(project.id, owner)
    return project


def test_project_erases_workspace_sessions_automation_and_access_but_keeps_other_projects(tmp_path):
    app, owner, project = setup(tmp_path)
    ledger, service = app.state.ledger, app.state.project_erasure
    other = other_project(app, owner)
    run = completed(app, owner, project, session=str(uuid4()))
    keep_run = completed(app, owner, other, session=str(uuid4()))
    transcript, _ = native_fixture(tmp_path, project.id, run.sdk_session_id)
    keep_transcript, _ = native_fixture(tmp_path, other.id, keep_run.sdk_session_id)
    ledger.save_upload(project.id, "private.txt", b"project-private-canary")
    ledger.save_upload(other.id, "keep.txt", b"other-project-canary")
    for directory in ("auth/marketplaces", "collaboration/comments"):
        folder = tmp_path / directory
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f".{project.id}.json.crashed.tmp").write_text("project-private-canary")
        (folder / f".{other.id}.json.crashed.tmp").write_text("keep-unpublished")
    schedule = ledger.create_schedule(
        Schedule(project_id=project.id, prompt="project-private-canary", cron="*/5 * * * *")
    )
    for directory in (
        "intents",
        "notifications",
        "feedback",
        "grants",
        "channel-deliveries",
        "task-experiences",
        "channels/feishu-bindings",
    ):
        folder = tmp_path / directory
        folder.mkdir(exist_ok=True, parents=True)
        (folder / "selected.json").write_text(
            json.dumps({"project_id": project.id, "text": "project-private-canary"})
        )
        (folder / "other.json").write_text(json.dumps({"project_id": other.id, "text": "keep"}))
    app.state.memory.propose(
        scope=MemoryScope.PROJECT,
        scope_id=project.id,
        text="project-private-canary",
        source=MemorySource.USER,
        kind=MemoryKind.PREFERENCE,
        principal=owner,
    )
    review = service.preview(project.id, owner)
    assert review["schedules"] == 1 and review["memory_count"] == 1
    service.delete(project.id, owner, confirm(review))
    assert isinstance(ledger.read_project_entry(project.id), ErasedProject)
    assert isinstance(ledger.read_run_entry(run.id), ErasedRun)
    assert ledger.list_projects()[0].id == other.id
    assert [row.id for row in ledger.list_runs()] == [keep_run.id]
    assert not transcript.parent.exists() and keep_transcript.exists()
    assert not (tmp_path / "schedules" / schedule.id).exists()
    assert not (tmp_path / "auth/projects" / project.id).exists()
    assert not (tmp_path / "memory/project" / project.id).exists()
    for directory in ("auth/marketplaces", "collaboration/comments"):
        assert not (tmp_path / directory / f".{project.id}.json.crashed.tmp").exists()
        retained = tmp_path / directory / f".{other.id}.json.crashed.tmp"
        assert retained.read_text() == "keep-unpublished"
    for directory in (
        "intents",
        "notifications",
        "feedback",
        "grants",
        "channel-deliveries",
        "task-experiences",
        "channels/feishu-bindings",
    ):
        assert not (tmp_path / directory / "selected.json").exists()
        assert (tmp_path / directory / "other.json").exists()
    with pytest.raises(LedgerNotFoundError):
        ledger.get_project(project.id)
    with pytest.raises(ValueError, match="already exists"):
        ledger.create_project(
            ProjectCreate(name="Recreate", objective="No"), project_id=project.id, principal=owner
        )
    with pytest.raises(LedgerNotFoundError):
        ledger.create_run(project.id, "Restart", principal=owner)
    assert service.delete(project.id, owner, confirm(review))["status"] == "deleted"
    assert b"project-private-canary" not in b"".join(
        path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    )


@pytest.mark.parametrize("missing_workspace", [False, True])
def test_project_discovers_unindexed_native_session_and_clears_project_metadata(
    tmp_path, missing_workspace
):
    app, owner, project = setup(tmp_path)
    session = str(uuid4())  # No Run ever received this session-init event.
    transcript, _ = native_fixture(tmp_path, project.id, session)
    config = tmp_path / "runtime-config/environment"
    workspace = str(app.state.ledger.workspace_for_project(project.id))
    if missing_workspace:
        app.state.ledger.workspace_for_project(project.id).rmdir()
    (config / "history.jsonl").write_text(
        json.dumps({"project": workspace, "display": "private"})
        + "\n"
        + json.dumps({"project": "other", "display": "keep"})
        + "\n"
    )
    (config / ".claude.json").write_text(
        json.dumps(
            {
                "projects": {workspace: {"note": "private"}, "other": {"note": "keep"}},
                "credential": "keep-secret",
            }
        )
    )
    service = app.state.project_erasure
    review = service.preview(project.id, owner)
    assert review["run_ids"] == [] and review["native_session_count"] == 1
    service.delete(project.id, owner, confirm(review))
    assert not transcript.parent.exists()
    assert "private" not in (config / "history.jsonl").read_text()
    assert json.loads((config / ".claude.json").read_text()) == {
        "projects": {"other": {"note": "keep"}},
        "credential": "keep-secret",
    }


def test_project_deletion_removes_shop_login_and_only_the_selected_device_scope(tmp_path):
    app, owner, project = setup(tmp_path)
    other = other_project(app, owner)
    shared, _ = app.state.companions.create(
        PairingCreate(project_ids=[project.id, other.id]), owner
    )
    sole, _ = app.state.companions.create(PairingCreate(project_ids=[project.id]), owner)
    store = ShopBrowserStore(app.state.ledger)
    shop = ShopIdentity(
        name="Private shop",
        platform="other",
        seller_shop_id="own-shop",
        entry_url="https://seller.example.com",
        project_id=project.id,
        tenant_id=project.tenant_id,
        identity_binding=project.identity_binding,
        created_by=owner.principal_id,
    )
    store.write(shop)
    directory = store.directory(shop.id)
    (directory / "profile").mkdir()
    (directory / "profile/Cookies").write_bytes(b"private-browser-login")
    push = tmp_path / "companion-push" / f"{sole.id}.json"
    push.write_text('{"subscription":"private-push"}')
    service = app.state.project_erasure
    review = service.preview(project.id, owner)
    assert review["devices"] == 2 and review["shops"] == 1
    service.delete(project.id, owner, confirm(review))
    assert not directory.exists() and not store.path(shop.id).exists()
    assert not push.exists()
    assert not (tmp_path / "companion-devices" / f"{sole.id}.json").exists()
    assert json.loads((tmp_path / "companion-devices" / f"{shared.id}.json").read_text())[
        "project_ids"
    ] == [other.id]


@pytest.mark.parametrize("delete_source", [True, False])
def test_template_copy_survives_source_deletion_and_deleted_target_cannot_reappear(
    tmp_path, delete_source
):
    app, owner, source = setup(tmp_path / "data")
    templates, applications = app.state.project_templates, app.state.template_applications
    artifact = app.state.ledger.save_upload(source.id, "copied.txt", b"independently-saved")
    template, _ = saved(templates, source, owner, artifact_ids=[artifact.id])
    request = use_request(template).model_copy(update={"artifact_ids": [artifact.id]})
    copied = applications.apply(source.id, template["id"], request, owner)["project"]
    selected, retained = (source, copied) if delete_source else (copied, source)
    service = app.state.project_erasure
    review = service.preview(selected.id, owner)
    service.delete(selected.id, owner, confirm(review))
    assert app.state.ledger.get_project(retained.id).id == retained.id
    app.state.skills.snapshot()
    inspected = inspect_application(tmp_path / "data", app.state.skills.plugin_dir)
    assert inspected["projects"] == [retained.id]
    create_backup(tmp_path / "data", app.state.skills.plugin_dir, tmp_path / "backup")
    restore_backup(tmp_path / "backup", tmp_path / "restored")
    restored = FileLedger(tmp_path / "restored/data")
    assert isinstance(restored.read_project_entry(selected.id), ErasedProject)
    if delete_source:
        assert restored.get_project(copied.id).template_origin == copied.template_origin
        assert (
            app.state.ledger.capture_artifact(copied.id, copied.artifacts[0].id)[1]
            == b"independently-saved"
        )
        child_review = service.preview(copied.id, owner)
        service.delete(copied.id, owner, confirm(child_review))
        assert app.state.ledger.read_project_entry(source.id).template_copies == {}
        assert inspect_application(tmp_path / "data", app.state.skills.plugin_dir)["projects"] == []
    else:
        with pytest.raises(TemplateConflict, match="permanently deleted"):
            applications.apply(source.id, template["id"], request, owner)


@pytest.mark.parametrize("change", ["file", "new_run", "active", "symlink", "wrong_name"])
def test_changed_or_unsafe_project_refuses_before_mutation(tmp_path, change):
    app, owner, project = setup(tmp_path)
    run = completed(app, owner, project, session=str(uuid4()))
    service = app.state.project_erasure
    review = service.preview(project.id, owner)
    payload = confirm(review)
    if change == "file":
        app.state.ledger.save_upload(project.id, "new.txt", b"new content")
    elif change == "new_run":
        completed(app, owner, project, session=str(uuid4()))
    elif change == "active":
        app.state.ledger.update_run(run.id, status=RunStatus.RUNNING)
    elif change == "symlink":
        workspace = app.state.ledger.workspace_for_project(project.id)
        (workspace / "outside").symlink_to(tmp_path / "memory")
    else:
        payload = confirm(review, confirm_project_name="Not this project")
    with pytest.raises((ErasureError, ValueError, OSError)):
        service.delete(project.id, owner, payload)
    assert app.state.ledger.get_project(project.id).name == project.name
    assert not (tmp_path / ".data-erasure/pending").exists()


def test_project_cleanup_interruption_recovers_before_application_access(tmp_path, monkeypatch):
    app, owner, project = setup(tmp_path)
    app.state.ledger.save_upload(project.id, "private.txt", b"erase-me")
    service = app.state.project_erasure
    review = service.preview(project.id, owner)
    original = FileErasure._apply_file
    calls = 0

    def interrupt(self, *args):
        nonlocal calls
        original(self, *args)
        calls += 1
        if calls == 2:
            raise OSError("simulated lost process after partial cleanup")

    with monkeypatch.context() as patch:
        patch.setattr(FileErasure, "_apply_file", interrupt)
        with pytest.raises(OSError):
            service.delete(project.id, owner, confirm(review))
    with pytest.raises(ErasureError):
        with operation_lease(tmp_path):
            pass
    FileErasure(tmp_path).recover()
    assert service.delete(project.id, owner, confirm(review))["status"] == "deleted"
    assert set(path.name for path in (tmp_path / "projects" / project.id).iterdir()) == {
        "project.json"
    }


def test_project_deletion_accepts_previously_erased_conversations(tmp_path):
    app, owner, project = setup(tmp_path)
    run = completed(app, owner, project, session=str(uuid4()))
    native_fixture(tmp_path, project.id, run.sdk_session_id)
    conversation = app.state.conversation_erasure
    review = conversation.preview(project.id, owner, run_id=run.id)
    conversation.delete(project.id, run.id, owner, conversation_confirm(review))
    service = app.state.project_erasure
    review = service.preview(project.id, owner)
    assert review["run_ids"] == [run.id] and review["native_session_count"] == 0
    service.delete(project.id, owner, confirm(review))
    assert isinstance(app.state.ledger.read_project_entry(project.id), ErasedProject)
    assert isinstance(app.state.ledger.read_run_entry(run.id), ErasedRun)


@pytest.mark.parametrize("change", ["expiry", "membership"])
def test_project_deletion_rechecks_authority_after_selection(tmp_path, monkeypatch, change):
    app, owner, project = setup(tmp_path)
    admin = principal("admin", PrincipalRole.ADMIN)
    app.state.project_access.grant(
        project.id, owner, principal_id="admin", role=PrincipalRole.ADMIN
    )
    artifact = app.state.ledger.save_upload(project.id, "private.txt", b"keep-before-commit")
    service = app.state.project_erasure
    review = service.preview(project.id, admin)
    select = service._select

    def revoke_after_selection(*args):
        result = select(*args)
        if change == "expiry":
            app.state.project_access._clock = lambda: utc_now() + timedelta(hours=1)
        else:
            app.state.project_access.revoke(project.id, owner, principal_id="admin")
        return result

    monkeypatch.setattr(service, "_select", revoke_after_selection)
    with pytest.raises((AuthenticationError, AuthorizationError, ErasureError)):
        service.delete(project.id, admin, confirm(review))
    assert app.state.ledger.get_project(project.id).id == project.id
    assert app.state.ledger.capture_artifact(project.id, artifact.id)[1] == b"keep-before-commit"
    assert not (tmp_path / ".data-erasure/pending").exists()


@pytest.mark.asyncio
async def test_project_api_current_authority_exact_confirmation_and_retry_after_acl_erasure(
    tmp_path,
):
    app, owner, project = setup(tmp_path)
    path = f"/api/projects/{project.id}/deletion"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (
            await request_as(client, "GET", path, "member", PrincipalRole.MEMBER)
        ).status_code == 403
        review = await request_as(client, "GET", path, "owner", PrincipalRole.OWNER)
        assert review.status_code == 200, review.text
        body = confirm(review.json()).model_dump(mode="json")
        bad = await request_as(
            client,
            "POST",
            path,
            "owner",
            PrincipalRole.OWNER,
            json={**body, "confirm_deletion": "true"},
        )
        assert bad.status_code == 422
        result = await request_as(client, "POST", path, "owner", PrincipalRole.OWNER, json=body)
        assert result.status_code == 200, result.text
        retry = await request_as(client, "POST", path, "owner", PrincipalRole.OWNER, json=body)
        assert retry.status_code == 200, retry.text
        foreign = await request_as(
            client, "POST", path, "another-owner", PrincipalRole.OWNER, json=body
        )
        assert foreign.status_code == 403
        assert (
            await request_as(
                client, "GET", f"/api/projects/{project.id}", "owner", PrincipalRole.OWNER
            )
        ).status_code == 404

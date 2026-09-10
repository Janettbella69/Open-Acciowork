"""Actual project/reference copies, immutable versions, and recoverable publication."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_auth_api import principal, trusted_settings

from ophira_backend.auth import AuthorizationError, ProjectAccessStore
from ophira_backend.config import Settings
from ophira_backend.constants import EvidenceKind, PrincipalRole
from ophira_backend.models import Decision, Evidence, ProjectCreate
from ophira_backend.project_template_apply import TemplateApplications
from ophira_backend.project_template_models import (
    ProjectSeed,
    TemplateApply,
    TemplateArchive,
    TemplateSave,
)
from ophira_backend.project_templates import ProjectTemplates, TemplateConflict, TemplateUnavailable
from ophira_backend.storage import FileLedger, LedgerNotFoundError


def services(root, *, trusted=False):
    ledger = FileLedger(root)
    access = ProjectAccessStore(root / "auth" / "projects")
    settings = trusted_settings(root) if trusted else Settings(_env_file=None, data_dir=root)
    store = ProjectTemplates(ledger, access, settings)
    return store, TemplateApplications(store)


def seed(name="Next research"):
    return ProjectSeed(
        name=name,
        objective="Verify new supplier quotes",
        product_brief="Reusable brief",
        target_markets=["Japan"],
        constraints=["Confirm the current MOQ"],
    )


def saved(store, project, actor=None, **kwargs):
    payload = TemplateSave(request_id=uuid4().hex, title="Supplier review", seed=seed(), **kwargs)
    summary = store.save(project.id, payload, actor)
    return summary, payload


def use_request(template, **kwargs):
    return TemplateApply(
        request_id=uuid4().hex,
        version=1,
        content_hash=template["content_hash"],
        seed=seed("Fresh opportunity"),
        artifact_ids=[],
        **kwargs,
    )


@pytest.fixture
def source(tmp_path):
    store, applications = services(tmp_path)
    project = store.ledger.create_project(ProjectCreate(name="Original", objective="Old task"))
    return store, applications, project


def test_reference_versions_and_curated_seed_create_a_fresh_project_without_old_truth(source):
    store, applications, project = source
    ledger = store.ledger
    evidence = Evidence(
        kind=EvidenceKind.USER_STATEMENT,
        claim="Old quote was 20",
        source_title="Previous supplier",
        recorded_by="old-run",
        confidence=0.2,
    )
    decision = Decision(question="Proceed?", outcome="Old decision", made_by="old-run")
    ledger.update_project(project.id, lambda value: value.evidence.append(evidence))
    ledger.update_project(project.id, lambda value: value.decisions.append(decision))
    first = ledger.save_upload(project.id, "quote.txt", b"Original supplier reference")
    (ledger.workspace_for_project(project.id) / first.path).write_bytes(b"Changed supplier quote")
    second = ledger.register_artifact(project.id, first.path)
    template, _ = saved(store, project, artifact_ids=[first.id])
    detail = store.detail(project.id, template["id"], 1, None)
    assert detail["version"]["references"][0]["checksum"] == first.checksum
    # The template is a frozen copy, even if the source working file disappears.
    (ledger.workspace_for_project(project.id) / first.path).unlink()
    assert (
        store.reference(project.id, template["id"], 1, first.id, None)[1]
        == b"Original supplier reference"
    )
    request = use_request(template).model_copy(update={"artifact_ids": [first.id]})
    result = applications.apply(project.id, template["id"], request, None)
    copied = result["project"]
    assert copied.id != project.id and copied.name == request.seed.name
    assert (
        copied.objective == request.seed.objective
        and copied.constraints == request.seed.constraints
    )
    assert copied.evidence == copied.decisions == copied.hypotheses == copied.stage_decisions == []
    assert copied.assessments == {} and ledger.list_runs(copied.id) == []
    assert len(copied.artifacts) == 1
    artifact = copied.artifacts[0]
    assert artifact.id not in {first.id, second.id} and artifact.source_ids == []
    assert artifact.created_by == "local-operator" and artifact.run_id is None
    assert ledger.capture_artifact(copied.id, artifact.id)[1] == b"Original supplier reference"
    assert (
        ledger.workspace_for_project(copied.id) / artifact.path
    ).read_bytes() == b"Original supplier reference"
    assert copied.template_origin.references[0].source.artifact_id == first.id
    assert copied.template_origin.references[0].source.version == 1
    assert copied.template_origin.source_project_id == project.id
    assert result["application"]["state"] == "applied"
    assert ledger.get_project(project.id).evidence == [evidence]


def test_versions_archive_and_request_replays_survive_restarting_the_store(source):
    store, applications, project = source
    template, original = saved(store, project)
    assert store.save(project.id, original, None)["id"] == template["id"]
    with pytest.raises(TemplateConflict, match="different content"):
        store.save(project.id, original.model_copy(update={"title": "Different"}), None)
    updated_request = TemplateSave(
        request_id=uuid4().hex,
        expected_revision=1,
        title="Updated supplier review",
        seed=seed("Updated seed"),
    )
    newer = store.save(project.id, updated_request, None, template_id=template["id"])
    assert newer["latest_version"] == 2
    assert store.save(project.id, updated_request, None, template_id=template["id"]) == newer
    with pytest.raises(TemplateConflict, match="reload"):
        store.save(
            project.id,
            updated_request.model_copy(update={"request_id": uuid4().hex}),
            None,
            template_id=template["id"],
        )
    assert (
        store.detail(project.id, template["id"], 1, None)["version"]["seed"]["name"]
        == "Next research"
    )
    archive = TemplateArchive(request_id=uuid4().hex, expected_revision=2, archived=True)
    archived = store.archive(project.id, template["id"], archive, None)
    assert archived["archived"] and not archived["can_apply"]
    assert store.archive(project.id, template["id"], archive, None)["revision"] == 3
    with pytest.raises(TemplateConflict, match="Restore"):
        applications.apply(project.id, template["id"], use_request(template), None)
    reopened = store.archive(
        project.id,
        template["id"],
        TemplateArchive(
            request_id=uuid4().hex,
            expected_revision=3,
            archived=False,
        ),
        None,
    )
    assert not reopened["archived"]
    restarted, _ = services(store.ledger.root)
    assert restarted.list(project.id, None)["items"][0] == reopened
    assert len(restarted.load(project.id, template["id"]).history) == 4


def test_template_application_replay_keeps_current_project_edits_and_starts_no_run(source):
    store, applications, project = source
    template, _ = saved(store, project)
    request = use_request(template)
    result = applications.apply(project.id, template["id"], request, None)
    created_id = result["project"].id
    store.ledger.update_project(
        created_id, lambda value: setattr(value, "name", "Later human edit")
    )
    retried = applications.apply(project.id, template["id"], request, None)
    assert retried["project"].id == created_id and retried["project"].name == "Later human edit"
    assert len(store.ledger.list_projects()) == 2
    assert store.ledger.list_runs(created_id) == []
    with pytest.raises(TemplateConflict, match="different content"):
        applications.apply(
            project.id, template["id"], request.model_copy(update={"seed": seed("Another")}), None
        )
    history = applications.list(project.id, template["id"], None)
    assert len(history["items"]) == 1 and history["items"][0]["state"] == "applied"


def test_interrupted_publication_is_reviewable_and_resumes_without_original_browser_payload(
    source, monkeypatch
):
    import ophira_backend.project_template_apply as implementation

    store, applications, project = source
    first = store.ledger.save_upload(project.id, "guide.txt", b"Reference")
    template, _ = saved(store, project, artifact_ids=[first.id])
    request = use_request(template).model_copy(update={"artifact_ids": [first.id]})
    original = implementation.publish_exclusive

    def disconnected(*_args):
        raise OSError("Synthetic publication interruption")

    monkeypatch.setattr(implementation, "publish_exclusive", disconnected)
    with pytest.raises(OSError):
        applications.apply(project.id, template["id"], request, None)
    assert len(store.ledger.list_projects()) == 1
    pending = applications.list(project.id, template["id"], None)["items"][0]
    assert pending["state"] == "ready" and pending["can_resume"]
    assert pending["seed"]["name"] == request.seed.name
    monkeypatch.setattr(implementation, "publish_exclusive", original)
    _, restarted = services(store.ledger.root)
    result = restarted.resume(project.id, template["id"], pending["id"], None)
    assert result["project"].id == pending["project_id"]
    assert len(store.ledger.list_projects()) == 2
    assert result["project"].artifacts[0].checksum == first.checksum


def test_members_can_curate_templates_but_new_project_ownership_is_not_forged(tmp_path):
    store, applications = services(tmp_path, trusted=True)
    owner = principal("owner", PrincipalRole.OWNER)
    member = principal("curator", PrincipalRole.MEMBER)
    other = principal("other", PrincipalRole.MEMBER)
    viewer = principal("reader", PrincipalRole.VIEWER)
    project = store.ledger.create_project(
        ProjectCreate(name="Team", objective="Review"), principal=owner
    )
    store.access.bind_project(project.id, owner)
    for person in [member, other, viewer]:
        store.access.grant(project.id, owner, principal_id=person.principal_id, role=person.role)
    template, payload = saved(store, project, member)
    assert store.list(project.id, viewer)["can_save"] is False
    assert store.list(project.id, member)["can_create_project"] is False
    with pytest.raises(AuthorizationError):
        store.save(project.id, payload, viewer)
    with pytest.raises(AuthorizationError):
        store.save(
            project.id,
            payload.model_copy(update={"request_id": uuid4().hex, "expected_revision": 1}),
            other,
            template_id=template["id"],
        )
    with pytest.raises(AuthorizationError):
        applications.apply(project.id, template["id"], use_request(template), member)
    result = applications.apply(project.id, template["id"], use_request(template), owner)
    created = result["project"]
    assert created.owner_principal_id == "owner" and created.tenant_id == owner.tenant_id
    assert set(store.access.get(created.id).memberships) == {"owner"}
    store.access.revoke(project.id, owner, principal_id=member.principal_id)
    with pytest.raises(AuthorizationError):
        store.detail(project.id, template["id"], 1, member)
    with pytest.raises(AuthorizationError):
        store.list(project.id, None)


def test_templates_reject_corrupt_manifests_and_cross_project_files(source):
    store, _, project = source
    other = store.ledger.create_project(ProjectCreate(name="Other", objective="Private"))
    file = store.ledger.save_upload(other.id, "private.txt", b"Private source")
    with pytest.raises(LedgerNotFoundError):
        saved(store, project, artifact_ids=[file.id])
    template, _ = saved(store, project)
    path = store.folder(project.id, template["id"]) / "template.json"
    raw = json.loads(path.read_text())
    raw["versions"][0]["seed"]["name"] = "Injected change"
    path.write_text(json.dumps(raw))
    with pytest.raises(TemplateUnavailable, match="verified"):
        store.detail(project.id, template["id"], 1, None)


def test_template_inputs_cannot_import_project_truth_or_execution_configuration():
    for extra in [
        {"evidence": []},
        {"provider_profile_id": "secret"},
        {"owner_principal_id": "owner"},
        {"stage": "sales"},
        {"template_origin": {}},
    ]:
        with pytest.raises(ValidationError):
            ProjectSeed.model_validate({**seed().model_dump(), **extra})


def test_reference_corruption_and_symlinks_never_publish_a_partial_project(source, tmp_path):
    from ophira_backend.artifact_files import snapshot_relative

    store, applications, project = source
    artifact = store.ledger.save_upload(project.id, "guide.txt", b"Original")
    template, _ = saved(store, project, artifact_ids=[artifact.id])
    snapshot = store.folder(project.id, template["id"]) / snapshot_relative(artifact.checksum)
    snapshot.chmod(0o600)
    snapshot.write_bytes(b"Corrupted")
    request = use_request(template).model_copy(update={"artifact_ids": [artifact.id]})
    with pytest.raises(TemplateUnavailable):
        applications.apply(project.id, template["id"], request, None)
    assert len(store.ledger.list_projects()) == 1
    external = tmp_path / "external.txt"
    external.write_bytes(b"Original")
    snapshot.unlink()
    snapshot.symlink_to(external)
    with pytest.raises(TemplateUnavailable):
        store.reference(project.id, template["id"], 1, artifact.id, None)
    assert external.read_bytes() == b"Original"


def test_selected_originals_use_normal_markdown_ingestion_in_the_new_project(source):
    store, applications, project = source
    original = store.ledger.save_upload(project.id, "unreadable.pdf", b"%PDF-1.7\nbroken")
    template, _ = saved(store, project, artifact_ids=[original.id])
    request = use_request(template).model_copy(update={"artifact_ids": [original.id]})
    copied = applications.apply(project.id, template["id"], request, None)["project"]
    assert len(copied.artifacts) == 2
    source_file = next(row for row in copied.artifacts if row.mime == "application/pdf")
    markdown = next(row for row in copied.artifacts if row.mime == "text/markdown")
    assert markdown.source_ids == [source_file.id]
    assert original.id not in markdown.source_ids
    assert store.ledger.capture_artifact(copied.id, source_file.id)[1] == b"%PDF-1.7\nbroken"
    assert len(store.ledger.capture_artifact(copied.id, markdown.id)[1]) > 0


def test_template_text_and_pending_creation_are_redacted_before_persistence(source, monkeypatch):
    store, applications, project = source
    sensitive = seed().model_copy(update={"product_brief": "token=template-private-canary"})
    payload = TemplateSave(request_id=uuid4().hex, title="Guide", seed=sensitive)
    template = store.save(project.id, payload, None)
    assert (
        "template-private-canary"
        not in store.detail(project.id, template["id"], 1, None)["version"]["seed"]["product_brief"]
    )

    def interrupted(*_args):
        raise OSError("Synthetic interrupted creation")

    monkeypatch.setattr("ophira_backend.project_template_apply.publish_exclusive", interrupted)
    request = use_request(template).model_copy(update={"seed": sensitive})
    with pytest.raises(OSError):
        applications.apply(project.id, template["id"], request, None)
    for path in store.root.rglob("*.json"):
        assert "template-private-canary" not in path.read_text()


def test_reference_and_history_capacity_checks_preserve_the_previous_version(source, monkeypatch):
    store, _, project = source
    artifact = store.ledger.save_upload(project.id, "guide.txt", b"Reference")
    template, _ = saved(store, project)
    monkeypatch.setattr("ophira_backend.project_templates.MAX_TEMPLATE_REFERENCE_BYTES", 4)
    update = TemplateSave(
        request_id=uuid4().hex,
        title="Too big",
        seed=seed(),
        expected_revision=1,
        artifact_ids=[artifact.id],
    )
    with pytest.raises(TemplateConflict, match="50 MiB"):
        store.save(project.id, update, None, template_id=template["id"])
    monkeypatch.setattr("ophira_backend.project_templates.MAX_TEMPLATE_MUTATIONS", 1)
    with pytest.raises(TemplateConflict, match="history is full"):
        store.archive(
            project.id,
            template["id"],
            TemplateArchive(request_id=uuid4().hex, expected_revision=1, archived=True),
            None,
        )
    assert store.load(project.id, template["id"]).revision == 1


def test_late_assertion_expiry_leaves_a_reviewable_creation_without_publishing(
    tmp_path, monkeypatch
):
    from ophira_backend.auth import AuthAssertionExpiredError
    from ophira_backend.project_template_backup import inspect_project_templates

    store, applications = services(tmp_path, trusted=True)
    owner = principal("owner", PrincipalRole.OWNER)
    project = store.ledger.create_project(
        ProjectCreate(name="Team", objective="Reuse"), principal=owner
    )
    store.access.bind_project(project.id, owner)
    template, _ = saved(store, project, owner)
    prepare = applications.prepare

    def expire_after_copy(*args):
        ready = prepare(*args)

        def expired(*_args, **_kwargs):
            raise AuthAssertionExpiredError("Synthetic expired assertion")

        monkeypatch.setattr(store.access, "authorize", expired)
        return ready

    with monkeypatch.context() as patch:
        patch.setattr(applications, "prepare", expire_after_copy)
        with pytest.raises(AuthAssertionExpiredError):
            applications.apply(project.id, template["id"], use_request(template), owner)
    assert len(store.ledger.list_projects()) == 1
    restarted, resumed = services(tmp_path, trusted=True)
    fresh_owner = principal("owner", PrincipalRole.OWNER, session_id="new-owner-session")
    pending = resumed.list(project.id, template["id"], fresh_owner)["items"][0]
    created = resumed.resume(project.id, template["id"], pending["id"], fresh_owner)["project"]
    assert created.owner_principal_id == owner.principal_id
    assert set(restarted.access.get(created.id).memberships) == {owner.principal_id}
    inspect_project_templates(tmp_path, {row.id: row for row in restarted.ledger.list_projects()})

"""Production app, native MCP submission and authorized frozen human review."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from claude_agent_sdk import PermissionResultDeny
from httpx import ASGITransport, AsyncClient
from mcp_test_client import call_sdk_tool
from test_auth_api import FakeRuntime, principal, request_as, signed_headers, trusted_settings

from ophira_backend.agent_core import SECURITY_PREAMBLE, SKILL_PROPOSAL_GUIDANCE
from ophira_backend.config import Settings
from ophira_backend.constants import (
    LEDGER_MCP_SERVER_NAME,
    TOOL_REGISTER_ARTIFACT,
    PrincipalRole,
    RunStatus,
)
from ophira_backend.ledger_tools import build_ledger_tools
from ophira_backend.main import create_app
from ophira_backend.models import ProjectCreate, utc_now
from ophira_backend.skill_proposal_models import SkillProposalReviewRequest


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(_env_file=None, data_dir=tmp_path), runtime=FakeRuntime())


def prepare(app, *, name="reusable-method", owner=None, submit=True, body="Review evidence."):
    ledger = app.state.ledger
    project = ledger.create_project(
        ProjectCreate(name=f"Project {name}", objective="Review a reusable method"),
        principal=owner,
    )
    if owner is not None:
        app.state.project_access.bind_project(project.id, owner)
    run = ledger.create_run(project.id, "Propose the repeated method", principal=owner)
    run = ledger.update_run(run.id, sdk_session_id=f"fixture-{run.id}", started_at=utc_now())
    base = ledger.workspace_for_project(project.id) / "skill-proposals" / name
    package = base / "package"
    package.mkdir(parents=True)
    text = f"---\nname: {name}\ndescription: Reusable method\nversion: 1.0.0\n---\n{body}\n"
    (package / "SKILL.md").write_text(text)
    (package / "template.bin").write_bytes(b"\x00\xfffrozen-template")
    manifest = {
        "schema_version": 1, "kind": "ophira.skill-proposal", "name": name,
        "version": "1.0.0", "package_dir": "package", "reason": "Reuse a repeated method",
    }
    (base / "manifest.json").write_text(json.dumps(manifest))
    path = f"skill-proposals/{name}/manifest.json"
    artifact = ledger.register_artifact(project.id, path, run_id=run.id)
    item = app.state.skill_proposals.submit_artifact(run.id, artifact.id) if submit else None
    return SimpleNamespace(
        project=project, run=run, item=item, artifact=artifact,
        path=path, package=package, text=text,
    )


def review_payload(detail, **changes):
    return {
        "decision": "approve", "revision": detail["revision"],
        "content_hash": detail["content_hash"], "expected_target": detail["target"],
        "note": "Reviewed all frozen files", **changes,
    }


def client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost")


@pytest.mark.asyncio
async def test_local_review_freezes_package_publishes_disabled_and_preserves_running_snapshot(app):
    row = prepare(app, body='<img src="https://example.invalid/track">\nOriginal instructions.')
    prefix = f"/api/skill-proposals/{row.item.id}"
    before = app.state.skills.snapshot()
    app.state.ledger.update_run(row.run.id, skill_snapshot=before)
    (row.package / "SKILL.md").write_text("Mutable workspace changed after submission")
    async with client(app) as http:
        page = (await http.get("/api/skill-proposals?state=pending")).json()
        assert [item["id"] for item in page["items"]] == [row.item.id]
        detail = (await http.get(prefix)).json()
        assert detail["project_name"] == row.project.name
        assert detail["actions"] == {"can_publish": True, "can_reject": True, "reason": None}
        assert detail["target"] == {"current_hash": None, "enabled": None, "generation": 0}
        assert str(app.state.ledger.root) not in json.dumps(detail)
        params = {"path": "SKILL.md", "content_hash": row.item.content_hash}
        preview = await http.get(f"{prefix}/files", params=params)
        assert preview.json()["text"] == row.text
        assert preview.json()["complete"] is True
        assert preview.headers["content-type"].startswith("application/json")
        binary = await http.get(f"{prefix}/files", params={**params, "path": "template.bin"})
        assert binary.json()["text"] is None and binary.json()["complete"] is False
        download = await http.get(f"{prefix}/download", params={**params, "path": "template.bin"})
        assert download.content == b"\x00\xfffrozen-template"
        assert download.headers["content-disposition"].startswith("attachment;")
        assert download.headers["x-content-type-options"] == "nosniff"
        assert "no-store" in download.headers["cache-control"]
        reviewed = await http.post(f"{prefix}/review", json=review_payload(detail))
        assert reviewed.status_code == 200, reviewed.text
        saved = reviewed.json()
        assert saved["state"] == "published"
        assert saved["review"]["reviewer"] == {
            "kind": "local_owner", "id": "local-owner", "role": "owner",
        }
        assert saved["publication_receipt"]["content_hash"] == row.item.content_hash
        assert saved["target"]["enabled"] is False
        assert saved["actions"]["can_publish"] is False
        duplicate = await http.post(f"{prefix}/review", json=review_payload(detail))
        assert duplicate.status_code == 409
    assert app.state.skills.snapshot() == before
    assert app.state.ledger.get_run(row.run.id).skill_snapshot == before
    assert app.state.skills.get(row.item.skill_id).enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("force", True), ("reviewer", {"id": "spoof"}), ("revision", True),
    ("revision", "1"), ("content_hash", "bad"), ("decision", "publish"),
    ("expected_target", None),
])
async def test_review_rejects_mass_assignment_and_invalid_cas(app, field, value):
    row = prepare(app)
    prefix = f"/api/skill-proposals/{row.item.id}"
    async with client(app) as http:
        detail = (await http.get(prefix)).json()
        response = await http.post(
            f"{prefix}/review", json=review_payload(detail, **{field: value}),
        )
    assert response.status_code == 422
    assert app.state.skill_proposals.get(row.item.id).state == "pending"
    assert app.state.skills.selection_token(row.item.skill_id).current_hash is None


@pytest.mark.asyncio
async def test_failed_scan_can_only_be_rejected_with_a_note(app):
    row = prepare(app, body="Run rm -rf /tmp/business-data")
    prefix = f"/api/skill-proposals/{row.item.id}"
    async with client(app) as http:
        detail = (await http.get(prefix)).json()
        assert detail["actions"]["can_publish"] is False
        assert detail["actions"]["can_reject"] is True
        result = await http.post(f"{prefix}/review", json=review_payload(detail))
        assert result.status_code == 409
        refreshed = (await http.get(prefix)).json()
        assert refreshed["revision"] > detail["revision"]
        empty = await http.post(f"{prefix}/review", json=review_payload(
            refreshed, decision="reject", note="  ", expected_target=None,
        ))
        assert empty.status_code == 422
        rejected = await http.post(f"{prefix}/review", json=review_payload(
            refreshed, decision="reject", note="Dangerous shell command", expected_target=None,
        ))
        assert rejected.status_code == 200 and rejected.json()["state"] == "rejected"


@pytest.mark.asyncio
async def test_target_aba_conflict_requires_fresh_human_review(app):
    row = prepare(app)
    prefix = f"/api/skill-proposals/{row.item.id}"
    async with client(app) as http:
        old = (await http.get(prefix)).json()
        # A different reviewed proposal publishes the same name before this review.
        second = prepare(app)
        second_detail = (await http.get(f"/api/skill-proposals/{second.item.id}")).json()
        assert (await http.post(f"/api/skill-proposals/{second.item.id}/review",
                                json=review_payload(second_detail))).status_code == 200
        app.state.skills.set_enabled(row.item.skill_id, True)
        app.state.skills.set_enabled(row.item.skill_id, False)
        result = await http.post(f"{prefix}/review", json=review_payload(old))
        assert result.status_code == 409
        fresh = (await http.get(prefix)).json()
        assert fresh["state"] == "pending" and fresh["revision"] > old["revision"]
        assert fresh["history"][-1]["outcome"] == "interrupted"
        assert fresh["target"]["generation"] > old["target"]["generation"]
        assert (await http.post(f"{prefix}/review", json=review_payload(fresh))).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("path,hash_value,status", [
    ("../record.json", None, 400), ("/etc/passwd", None, 400),
    ("missing.md", None, 404), ("SKILL.md", "0" * 64, 409),
])
async def test_frozen_preview_enforces_path_and_whole_package_identity(
    app, path, hash_value, status,
):
    row = prepare(app)
    async with client(app) as http:
        for route in ("files", "download"):
            response = await http.get(f"/api/skill-proposals/{row.item.id}/{route}", params={
                "path": path, "content_hash": hash_value or row.item.content_hash,
            })
            assert response.status_code == status


@pytest.mark.asyncio
async def test_keyset_pagination_survives_review_removing_first_page(app):
    rows = [prepare(app, name=f"method-{i}") for i in range(3)]
    async with client(app) as http:
        first = (await http.get("/api/skill-proposals?state=pending&limit=1")).json()
        row = first["items"][0]
        assert row["id"] == rows[-1].item.id
        detail = (await http.get(f"/api/skill-proposals/{row['id']}")).json()
        await http.post(f"/api/skill-proposals/{row['id']}/review", json=review_payload(
            detail, decision="reject", note="Keep as a one-off", expected_target=None,
        ))
        second = (await http.get("/api/skill-proposals", params={
            "state": "pending", "limit": 1, "cursor": first["next_cursor"],
        })).json()
        assert second["items"][0]["id"] == rows[-2].item.id
        third = (await http.get("/api/skill-proposals", params={
            "state": "pending", "limit": 1, "cursor": second["next_cursor"],
        })).json()
        assert third["items"][0]["id"] == rows[0].item.id and third["next_cursor"] is None
        assert (await http.get("/api/skill-proposals?cursor=malformed")).status_code == 400
        assert (await http.get("/api/skill-proposals?state=enabled")).status_code == 422


@pytest.mark.asyncio
async def test_gateway_filters_before_paging_and_requires_current_admin_membership(tmp_path):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    owner = principal("owner@example.com", PrincipalRole.OWNER)
    row = prepare(app, owner=owner)
    private = prepare(app, name="private-method", owner=principal("other", PrincipalRole.OWNER))
    access = app.state.project_access
    for identity, role in (("admin", PrincipalRole.ADMIN), ("member", PrincipalRole.MEMBER)):
        access.grant(row.project.id, owner, principal_id=identity, role=role)
    prefix = f"/api/skill-proposals/{row.item.id}"
    async with client(app) as http:
        assert (await http.get(prefix)).status_code == 401
        page = await request_as(http, "GET", "/api/skill-proposals?limit=1", "member",
                                PrincipalRole.MEMBER)
        assert [item["id"] for item in page.json()["items"]] == [row.item.id]
        assert page.json()["next_cursor"] is None
        hidden = await request_as(http, "GET", f"/api/skill-proposals/{private.item.id}",
                                  "admin", PrincipalRole.ADMIN)
        assert hidden.status_code == 404
        detail = (await request_as(http, "GET", prefix, "member", PrincipalRole.MEMBER)).json()
        assert detail["actions"]["can_publish"] is False
        denied = await request_as(http, "POST", f"{prefix}/review", "member", PrincipalRole.MEMBER,
                                  json=review_payload(detail))
        assert denied.status_code == 403
        detail = (await request_as(http, "GET", prefix, "admin", PrincipalRole.ADMIN)).json()
        assert detail["actions"]["can_publish"] is True
        access.revoke(row.project.id, owner, principal_id="admin")
        for suffix in ("", "/files?path=SKILL.md&content_hash=" + row.item.content_hash,
                       "/download?path=SKILL.md&content_hash=" + row.item.content_hash):
            assert (await request_as(http, "GET", prefix + suffix, "admin",
                                     PrincipalRole.ADMIN)).status_code == 404
        assert (await request_as(http, "POST", f"{prefix}/review", "admin", PrincipalRole.ADMIN,
                                 json=review_payload(detail))).status_code == 404
        cross_tenant = await http.get(prefix, headers=signed_headers(
            "GET", prefix, owner.principal_id, owner.role, tenant_id="tenant_other",
        ))
        assert cross_tenant.status_code == 401
        access.grant(row.project.id, owner, principal_id="admin", role=PrincipalRole.ADMIN)
        accepted = await request_as(http, "POST", f"{prefix}/review", "admin", PrincipalRole.ADMIN,
                                    json=review_payload(detail))
        assert accepted.status_code == 200
        assert accepted.json()["review"]["reviewer"]["id"] == "admin"


@pytest.mark.asyncio
async def test_revoke_during_frozen_read_does_not_return_file(tmp_path, monkeypatch):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    owner = principal("owner", PrincipalRole.OWNER)
    row = prepare(app, owner=owner)
    app.state.project_access.grant(row.project.id, owner, principal_id="admin",
                                   role=PrincipalRole.ADMIN)
    original = app.state.skill_proposals.read_file

    def read_then_revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        app.state.project_access.revoke(row.project.id, owner, principal_id="admin")
        return result

    monkeypatch.setattr(app.state.skill_proposals, "read_file", read_then_revoke)
    async with client(app) as http:
        response = await request_as(http, "GET", f"/api/skill-proposals/{row.item.id}/files"
                                    f"?path=SKILL.md&content_hash={row.item.content_hash}",
                                    "admin", PrincipalRole.ADMIN)
        assert response.status_code == 404 and row.text not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [False, True])
async def test_invisible_candidate_never_recovers_or_blocks_authorized_listing(
    tmp_path, monkeypatch, corrupt,
):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    owner = principal("owner", PrincipalRole.OWNER)
    other = principal("other", PrincipalRole.OWNER)
    row = prepare(app, owner=owner)
    hidden = prepare(app, name="hidden-method", owner=other)
    proposals = app.state.skill_proposals

    class HostStopped(BaseException):
        pass

    def stop_before_publication(*args, **kwargs):
        raise HostStopped

    with monkeypatch.context() as patch:
        patch.setattr(app.state.skills, "publish_reviewed_candidate", stop_before_publication)
        with pytest.raises(HostStopped):
            proposals.review(hidden.item.id, SkillProposalReviewRequest(
                decision="approve", revision=hidden.item.revision,
                content_hash=hidden.item.content_hash,
                expected_target=app.state.skills.selection_token(hidden.item.skill_id),
            ), other)
    record = proposals._path(hidden.item.id) / "record.json"
    assert json.loads(record.read_bytes())["state"] == "publishing"
    if corrupt:
        record.write_text("{")
    before = record.read_bytes()
    prefix = f"/api/skill-proposals/{hidden.item.id}"
    async with client(app) as http:
        denied = await request_as(http, "GET", prefix, owner.principal_id, owner.role)
        assert denied.status_code == 404
        page = await request_as(http, "GET", "/api/skill-proposals?limit=1",
                                owner.principal_id, owner.role)
        assert page.status_code == 200
        assert [item["id"] for item in page.json()["items"]] == [row.item.id]
        assert page.json()["next_cursor"] is None
        assert record.read_bytes() == before
        if not corrupt:
            allowed = await request_as(http, "GET", prefix, other.principal_id, other.role)
            assert allowed.status_code == 200 and allowed.json()["state"] == "pending"
            assert allowed.json()["history"][-1]["outcome"] == "interrupted"


@pytest.mark.asyncio
@pytest.mark.parametrize("expiration", [False, True])
async def test_publication_rechecks_human_auth_after_registry_rescan(
    tmp_path, monkeypatch, expiration,
):
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    owner = principal("owner", PrincipalRole.OWNER)
    row = prepare(app, owner=owner)
    access = app.state.project_access
    access.grant(row.project.id, owner, principal_id="admin", role=PrincipalRole.ADMIN)
    prefix = f"/api/skill-proposals/{row.item.id}"
    original = app.state.skills._test_skill
    scans = []

    def scan_then_lose_auth(skill):
        report = original(skill)
        scans.append(True)
        if len(scans) == 2:
            if expiration:
                monkeypatch.setattr(access, "_clock", lambda: utc_now() + timedelta(minutes=5))
            else:
                access.revoke(row.project.id, owner, principal_id="admin")
        return report

    async with client(app) as http:
        detail = (await request_as(http, "GET", prefix, "admin", PrincipalRole.ADMIN)).json()
        monkeypatch.setattr(app.state.skills, "_test_skill", scan_then_lose_auth)
        response = await request_as(http, "POST", f"{prefix}/review", "admin", PrincipalRole.ADMIN,
                                    json=review_payload(detail))
        assert response.status_code == (401 if expiration else 403)
    assert len(scans) >= 2
    assert app.state.skills.publication_receipt(row.item.id) is None
    assert app.state.skills.selection_token(row.item.skill_id).current_hash is None
    assert app.state.skill_proposals.peek(row.item.id).state == "pending"


@pytest.mark.asyncio
async def test_actual_sdk_mcp_requires_explicit_submission_and_keeps_native_tool_boundary(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path))
    row = prepare(app, submit=False)
    ledger = app.state.ledger
    ledger.scan_workspace(row.project.id, run_id=row.run.id)
    assert app.state.skill_proposals.list() == []
    runtime = app.state.runtime
    assert runtime.skill_proposals is app.state.skill_proposals
    options = runtime._build_options(row.run)
    assert options.setting_sources == [] and options.strict_mcp_config is True
    assert options.tools == {"type": "preset", "preset": "claude_code"}
    assert options.system_prompt["append"].startswith(SECURITY_PREAMBLE)
    assert SKILL_PROPOSAL_GUIDANCE in options.system_prompt["append"]
    assert all("proposal" not in tool.name for tool in build_ledger_tools(ledger, row.project.id))
    denied = await options.can_use_tool(
        TOOL_REGISTER_ARTIFACT, {"path": row.path, "source_ids": []},
        SimpleNamespace(agent_id="read-only-researcher"),
    )
    assert isinstance(denied, PermissionResultDeny)
    server = options.mcp_servers[LEDGER_MCP_SERVER_NAME]["instance"]
    response = await call_sdk_tool(server, "register_artifact", {
            "path": row.path, "source_ids": [], "mime": "application/json",
    })
    assert response.is_error is False
    returned = json.loads(response.content[0].text)
    assert returned["id"] == row.artifact.id
    assert returned["skill_proposal"]["state"] == "pending"
    assert len(app.state.skill_proposals.list()) == 1
    assert not any(item.id.startswith("ophira-tenant:") for item in app.state.skills.list())


@pytest.mark.asyncio
async def test_project_context_offers_bounded_true_run_ids_without_creating_proposals(app):
    row = prepare(app, submit=False)
    ledger = app.state.ledger
    for i in range(22):
        old = ledger.create_run(row.project.id, "api_key=fixture-secret " + str(i))
        ledger.update_run(old.id, status=RunStatus.COMPLETED,
                          started_at=utc_now() - timedelta(hours=1),
                          finished_at=utc_now())
    ledger.create_run(row.project.id, "Queued has no repeated execution")
    tools = build_ledger_tools(ledger, row.project.id, run_id=row.run.id,
                               skill_proposals=app.state.skill_proposals)
    handler = next(tool.handler for tool in tools if tool.name == "read_project_context")
    result = await handler({})
    payload = json.loads(result["content"][0]["text"])
    context = payload["skill_proposal_context"]
    assert context["current_run_id"] == row.run.id and len(context["related_runs"]) == 20
    assert all(item["id"] != row.run.id and item["status"] == "completed"
               for item in context["related_runs"])
    assert "fixture-secret" not in json.dumps(context)
    assert app.state.skill_proposals.list() == []


@pytest.mark.asyncio
async def test_normal_artifact_response_and_bounded_text_preview(app):
    row = prepare(app, submit=False)
    (row.package / "large-reference.txt").write_text("审阅材料" * 30_000)
    workspace = app.state.ledger.workspace_for_project(row.project.id)
    (workspace / "ordinary.json").write_text('{"kind":"ophira.skill-proposal"}')
    handler = next(tool.handler for tool in build_ledger_tools(
        app.state.ledger, row.project.id, run_id=row.run.id,
        skill_proposals=app.state.skill_proposals,
    ) if tool.name == "register_artifact")
    result = json.loads((await handler({"path": "ordinary.json", "source_ids": []}))[
        "content"][0]["text"])
    assert "skill_proposal" not in result and result["path"] == "ordinary.json"
    assert app.state.skill_proposals.list() == []
    frozen = app.state.skill_proposals.submit_artifact(row.run.id, row.artifact.id)
    async with client(app) as http:
        response = await http.get(f"/api/skill-proposals/{frozen.id}/files", params={
            "path": "large-reference.txt", "content_hash": frozen.content_hash,
        })
        preview = response.json()
        assert preview["complete"] is False and preview["preview_kind"] == "text"
        assert preview["size_bytes"] == 360_000
        assert len(preview["text"].encode()) <= 200_000
        assert "\ufffd" not in preview["text"]


@pytest.mark.asyncio
async def test_application_startup_recovers_human_intents_without_submitting_workspace(
    app, monkeypatch,
):
    row = prepare(app, submit=False)
    original = app.state.skill_proposals.recover
    calls = []

    def recover():
        calls.append(True)
        return original()

    monkeypatch.setattr(app.state.skill_proposals, "recover", recover)
    async with app.router.lifespan_context(app):
        assert calls == [True]
        assert app.state.skill_proposals.list() == []
        assert (row.package / "SKILL.md").read_text() == row.text

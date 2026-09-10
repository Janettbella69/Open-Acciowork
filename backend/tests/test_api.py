import asyncio
import io
import json
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from ophira_backend.config import Settings
from ophira_backend.constants import RunStatus
from ophira_backend.main import create_app
from ophira_backend.models import RunRecord


class FakeRuntime:
    def __init__(self) -> None:
        self.runs: list[str] = []
        self.cancelled: list[str] = []

    async def run(self, run: RunRecord) -> None:
        self.runs.append(run.id)

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)


class BlockingRuntime(FakeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def run(self, run: RunRecord) -> None:
        self.runs.append(run.id)
        self.started.set()
        await asyncio.Event().wait()


def _skill_archive(version: str, body: str) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "tenant-research/SKILL.md",
            "---\n"
            "name: tenant-research\n"
            "description: Immutable tenant research method\n"
            f"version: {version}\n"
            "---\n"
            f"{body}\n",
        )
    return payload.getvalue()


async def _publish_private_skill(client, version, body):
    staged = await client.post(
        f"/api/skills/install?request_id={uuid4().hex}",
        content=_skill_archive(version, body),
        headers={"content-type": "application/zip"},
    )
    assert staged.status_code == 201, staged.text
    detail = staged.json()
    assert detail["import"]["state"] == "pending"
    published = await client.post(
        f"/api/skills/imports/{detail['import']['id']}/review",
        json={
            "request_id": uuid4().hex,
            "decision": "approve",
            "revision": detail["import"]["revision"],
            "content_hash": detail["import"]["skill"]["content_hash"],
            "expected_target": detail["current_target"],
            "note": "Test human reviewed the frozen package",
        },
    )
    assert published.status_code == 200, published.text
    return published.json()["import"]["skill"]


@pytest.mark.asyncio
async def test_runtime_provider_status_never_returns_the_key(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            data_dir=tmp_path,
            claude_model="qwen3.7-plus",
            ANTHROPIC_BASE_URL="https://coding.dashscope.aliyuncs.com/apps/anthropic",
            ANTHROPIC_AUTH_TOKEN="qwen-secret",
        ),
        runtime=FakeRuntime(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.get("/api/runtime/provider")

    assert response.status_code == 200
    assert response.json() == {
        "api_credentials_configured": True,
        "authentication": "auth_token",
        "base_url": "https://coding.dashscope.aliyuncs.com/apps/anthropic",
        "model": "qwen3.7-plus",
    }
    assert "qwen-secret" not in response.text


@pytest.mark.asyncio
async def test_project_run_and_protocol_api(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    app = create_app(Settings(data_dir=tmp_path), runtime=runtime)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.get("/api/meta/protocol")
        assert response.status_code == 200
        protocol = response.json()
        assert "tool.started" in protocol["event_types"]
        assert protocol["commercial_review_decisions"] == ["approve", "reject"]
        assert protocol["external_execution_statuses"] == [
            "not_executed",
            "attempting",
            "retryable_failed",
            "succeeded",
            "terminal_failed",
        ]
        assert protocol["presentation_schema_versions"] == ["1"]
        assert protocol["presentation_components"] == [
            "evidence_comparison",
            "supplier_comparison",
            "unit_economics",
            "decision_brief",
            "commercial_action_diff",
            "artifact_list",
        ]

        response = await client.post(
            "/api/projects",
            json={"name": "Kettle", "objective": "Validate EU demand"},
        )
        assert response.status_code == 201
        project_id = response.json()["id"]

        response = await client.get("/api/projects")
        assert response.status_code == 200
        assert [project["id"] for project in response.json()] == [project_id]

        response = await client.post(
            f"/api/projects/{project_id}/runs",
            json={"prompt": "Find the weakest dimension"},
        )
        assert response.status_code == 202
        run_id = response.json()["id"]

        for _ in range(30):
            response = await client.get(f"/api/runs/{run_id}")
            if response.json()["status"] == RunStatus.COMPLETED.value:
                break
            await asyncio.sleep(0.01)
        assert response.json()["status"] == RunStatus.COMPLETED.value
        assert runtime.runs == [run_id]

        response = await client.get(f"/api/projects/{project_id}/runs")
        assert response.status_code == 200
        assert [run["id"] for run in response.json()] == [run_id]

        response = await client.get("/api/projects/project_missing/runs")
        assert response.status_code == 404

        events = app.state.ledger.list_events(run_id)
        assert events[0].type == "run.created"
        assert events[-1].type == "run.completed"


@pytest.mark.asyncio
async def test_cancel_interrupts_runtime_and_reaches_terminal_state(tmp_path: Path) -> None:
    runtime = BlockingRuntime()
    app = create_app(Settings(data_dir=tmp_path), runtime=runtime)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        project = await client.post(
            "/api/projects",
            json={"name": "Kettle", "objective": "Validate"},
        )
        project_id = project.json()["id"]
        started = await client.post(
            f"/api/projects/{project_id}/runs",
            json={"prompt": "Start"},
        )
        run_id = started.json()["id"]
        await asyncio.wait_for(runtime.started.wait(), timeout=1)
        partial = app.state.ledger.workspace_for_project(project_id) / "partial-report.md"
        partial.write_text("recoverable work", encoding="utf-8")

        response = await client.post(f"/api/runs/{run_id}/cancel")
        assert response.status_code == 200
        assert response.json()["status"] == RunStatus.CANCELLED.value
        assert runtime.cancelled == [run_id]
        events = app.state.ledger.list_events(run_id)
        assert events[-1].type == "run.cancelled"
        artifact_events = [event for event in events if event.type == "artifact.registered"]
        assert artifact_events
        assert artifact_events[-1].data["path"] == "partial-report.md"
        assert artifact_events[-1].sequence < events[-1].sequence


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/projects/project_any/files?filename=large.bin",
        "/api/skills/install?request_id=oversize_upload_request_0001",
    ],
)
async def test_binary_uploads_reject_declared_oversize_before_buffering(
    tmp_path: Path,
    path: str,
) -> None:
    app = create_app(Settings(data_dir=tmp_path), runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.post(
            path,
            content=b"small",
            headers={"content-length": str(100 * 1024 * 1024)},
        )

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_skill_history_and_rollback_are_available_through_the_api(
    tmp_path: Path,
) -> None:
    app = create_app(Settings(data_dir=tmp_path), runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        first = await _publish_private_skill(client, "1.0.0", "Use the first reviewed method.")
        second = await _publish_private_skill(client, "2.0.0", "Use the second reviewed method.")
        assert "path" not in first
        assert "path" not in second
        skill_id = second["id"]

        history = await client.get(f"/api/skills/{skill_id}/versions")
        assert history.status_code == 200
        assert {(item["version"], item["content_hash"]) for item in history.json()} == {
            ("1.0.0", first["content_hash"]),
            ("2.0.0", second["content_hash"]),
        }

        restored = await client.post(
            f"/api/skills/{skill_id}/rollback",
            json={"content_hash": first["content_hash"]},
        )
        assert restored.status_code == 200
        assert restored.json()["version"] == "1.0.0"
        assert restored.json()["content_hash"] == first["content_hash"]
        assert restored.json()["enabled"] is False

        invalid = await client.post(
            f"/api/skills/{skill_id}/rollback",
            json={
                "version": "1.0.0",
                "content_hash": first["content_hash"],
            },
        )
        assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_skill_history_api_distinguishes_input_conflict_and_integrity_errors(
    tmp_path: Path,
) -> None:
    app = create_app(Settings(data_dir=tmp_path), runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        invalid_id = await client.get("/api/skills/not-a-skill-id/versions")
        missing = await client.get("/api/skills/ophira-tenant:missing/versions")
        application_conflict = await client.post(
            "/api/skills/ophira-core:customer-path/rollback",
            json={"version": "1.0.0"},
        )

        assert invalid_id.status_code == 400
        assert missing.status_code == 404
        assert application_conflict.status_code == 409

        installed = await _publish_private_skill(client, "1.0.0", "Reviewed package.")
        internal = app.state.skills.get(installed["id"])
        skill_path = Path(internal.path)

        def tamper_history() -> None:
            skill_path.chmod(0o644)
            skill_path.write_text(
                skill_path.read_text(encoding="utf-8") + "\ntampered after publish\n",
                encoding="utf-8",
            )

        await asyncio.to_thread(tamper_history)
        corrupted = await client.get(
            f"/api/skills/{installed['id']}/versions"
        )

    assert corrupted.status_code == 500
    assert corrupted.json()["detail"] == "Skill registry integrity validation failed"
    assert "path" not in corrupted.text


@pytest.mark.asyncio
async def test_skill_install_hides_corrupted_registry_state_details(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path), runtime=FakeRuntime())
    app.state.skills.registry_state_path.write_text("[]\n", encoding="utf-8")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.post(
            "/api/skills/install?request_id=corrupt_state_request_0001",
            content=_skill_archive("1.0.0", "Reviewed package."),
            headers={"content-type": "application/zip"},
        )
        app.state.skills.registry_state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "enabled": {"ophira-tenant:ghost": False},
                    "current": {"ophira-tenant:ghost": "0" * 64},
                }
            ),
            encoding="utf-8",
        )
        missing_history = await client.post(
            "/api/skills/ophira-tenant:ghost/enable"
        )
        invalid_test = await client.post("/api/skills/not-an-id/test")

    assert response.status_code == 409
    assert response.json()["detail"] == "Private Skill storage could not be verified"
    assert str(tmp_path) not in response.text
    assert missing_history.status_code == 500
    assert missing_history.json()["detail"] == "Skill registry integrity validation failed"
    assert invalid_test.status_code == 400

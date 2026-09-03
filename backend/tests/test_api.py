import asyncio
from pathlib import Path

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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/meta/protocol")
        assert response.status_code == 200
        assert "tool.started" in response.json()["event_types"]

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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
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
        "/api/skills/install",
    ],
)
async def test_binary_uploads_reject_declared_oversize_before_buffering(
    tmp_path: Path,
    path: str,
) -> None:
    app = create_app(Settings(data_dir=tmp_path), runtime=FakeRuntime())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            path,
            content=b"small",
            headers={"content-length": str(100 * 1024 * 1024)},
        )

    assert response.status_code == 413

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ophira_backend.config import Settings
from ophira_backend.main import create_app


class OfflineRuntime:
    async def run(self, _run) -> None:
        raise AssertionError("request-source tests must not start an Agent")

    async def cancel(self, _run_id: str) -> None:
        return None


@pytest.fixture
def app(tmp_path: Path):
    return create_app(
        Settings(_env_file=None, data_dir=tmp_path), runtime=OfflineRuntime()
    )


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://untrusted.example"},
        {"Origin": "null"},
        {"Origin": "http://localhost:5173.evil.example"},
        {"Origin": "http://localhost:5173/path"},
        {"Origin": "http://localhost:5173@evil.example"},
        {"Referer": "https://untrusted.example/form"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Host": "untrusted.example:8000"},
        {"Host": "localhost.evil.example"},
        {"Host": "localhost:8000@untrusted.example"},
        {"Host": "localhost:8000/path"},
        {"Host": "[::1%25lo0]:8000"},
        [("Host", "localhost"), ("Host", "untrusted.example")],
        [("Origin", "http://localhost:5173"), ("Origin", "https://untrusted.example")],
    ],
)
async def test_untrusted_sources_cannot_read_or_mutate_local_state(app, headers) -> None:
    app.state.notifications.add(title="Unread", body="Must remain unread")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8000"
    ) as client:
        read = await client.get("/api/projects", headers=headers)
        write = await client.post("/api/notifications/read-all", headers=headers, content="")
    assert read.status_code == write.status_code == 403
    assert len(app.state.notifications.list(unread_only=True)) == 1


@pytest.mark.parametrize(
    ("base_url", "headers"),
    [
        ("http://localhost:8000", {}),
        ("http://127.0.0.1:8000", {}),
        ("http://[::1]:8000", {}),
        ("http://localhost:8000", {"Origin": "http://localhost:5173"}),
        ("http://localhost:8000", {"Referer": "http://localhost:5173/app"}),
        ("http://localhost:5173", {"Origin": "http://localhost:5173"}),
        ("http://127.0.0.1:5173", {"Origin": "http://127.0.0.1:5173"}),
        ("http://localhost", {"Origin": "http://localhost:80"}),
    ],
)
async def test_local_cli_and_trusted_frontend_remain_usable(app, base_url, headers) -> None:
    app.state.notifications.add(title="Unread", body="Trusted user action")
    async with AsyncClient(transport=ASGITransport(app=app), base_url=base_url) as client:
        response = await client.post("/api/notifications/read-all", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"marked": 1}


async def test_preflight_checks_origin_before_cors(app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8000"
    ) as client:
        trusted = await client.options(
            "/api/projects",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        denied = await client.options(
            "/api/projects",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert trusted.status_code == 200
    assert trusted.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert denied.status_code == 403


async def test_cors_wildcard_does_not_grant_local_api_access(tmp_path: Path) -> None:
    app = create_app(
        Settings(_env_file=None, data_dir=tmp_path, cors_origins=["*"]),
        runtime=OfflineRuntime(),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8000"
    ) as client:
        response = await client.post(
            "/api/notifications/read-all", headers={"Origin": "https://untrusted.example"}
        )
    assert response.status_code == 403

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ophira_backend.config import Settings
from ophira_backend.desktop_host import DESKTOP_COOKIE_NAME, DesktopHost
from ophira_backend.main import create_app

PORT = 41273
ORIGIN = f"http://127.0.0.1:{PORT}"
TOKEN = "a" * 64


class FakeRuntime:
    async def run(self, _run) -> None:
        return None

    async def cancel(self, _run_id: str) -> None:
        return None


def desktop_app(tmp_path: Path) -> DesktopHost:
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<html>Desktop UI</html>", encoding="utf-8")
    (ui / "assets" / "main.js").write_text("const desktop = true", encoding="utf-8")
    (ui / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
    settings = Settings(
        _env_file=None, data_dir=tmp_path / "data", host="127.0.0.1", port=PORT,
    )
    api = create_app(settings, runtime=FakeRuntime())
    return DesktopHost(api, ui_dir=ui, token=TOKEN, port=PORT)


@pytest.mark.asyncio
async def test_desktop_session_guards_ui_api_and_health(tmp_path: Path) -> None:
    app = desktop_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        paths = ("/", "/app", "/app/settings", "/assets/main.js", "/api/auth/status", "/health")
        for path in paths:
            assert (await client.get(path)).status_code == 401

        client.cookies.set(DESKTOP_COOKIE_NAME, TOKEN)
        index = await client.get("/app/settings")
        assert index.status_code == 200
        assert "Desktop UI" in index.text
        assert index.headers["cache-control"] == "no-store"
        assert (await client.get("/assets/main.js")).status_code == 200
        assert (await client.get("/favicon.svg")).status_code == 200
        assert (await client.get("/api/auth/status")).status_code == 200
        assert (await client.get("/health")).status_code == 200


@pytest.mark.asyncio
async def test_desktop_health_probe_never_authenticates_other_routes(tmp_path: Path) -> None:
    app = desktop_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        header = {"X-Ophira-Desktop-Token": TOKEN}
        assert (await client.get("/health", headers=header)).status_code == 200
        assert (await client.get("/api/auth/status", headers=header)).status_code == 401
        assert (await client.get("/app", headers=header)).status_code == 401


@pytest.mark.asyncio
async def test_desktop_rejects_cross_origin_and_non_loopback_even_with_cookie(
    tmp_path: Path,
) -> None:
    app = desktop_app(tmp_path)
    client_kwargs = {"base_url": ORIGIN, "cookies": {DESKTOP_COOKIE_NAME: TOKEN}}
    async with AsyncClient(transport=ASGITransport(app=app), **client_kwargs) as client:
        for headers in (
            {"Host": "localhost:41273"},
            {"Origin": "http://127.0.0.1:5173"},
            {"Origin": "https://evil.example"},
            {"Sec-Fetch-Site": "same-site"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            assert (await client.get("/api/auth/status", headers=headers)).status_code == 401
        assert (await client.get("/api/auth/status", headers={"Origin": ORIGIN})).status_code == 200

    remote = ASGITransport(app=app, client=("192.0.2.25", 5000))
    async with AsyncClient(transport=remote, **client_kwargs) as client:
        assert (await client.get("/app")).status_code == 401


def test_desktop_requires_private_token_and_complete_ui(tmp_path: Path) -> None:
    ui = tmp_path / "ui"
    ui.mkdir()
    with pytest.raises(ValueError, match="token"):
        DesktopHost(None, ui_dir=ui, token="guess", port=PORT)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="incomplete"):
        DesktopHost(None, ui_dir=ui, token=TOKEN, port=PORT)  # type: ignore[arg-type]

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from ophira_backend.config import Settings
from ophira_backend.constants import EvidenceKind
from ophira_backend.media_tools import generate_media_impl
from ophira_backend.models import Evidence, ProjectCreate
from ophira_backend.storage import FileLedger

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"test-payload"


def _ledger(tmp_path):
    ledger = FileLedger(tmp_path / "state")
    project = ledger.create_project(
        ProjectCreate(name="Media", objective="Create campaign media")
    )
    return ledger, project


def _settings() -> Settings:
    return Settings(
        state_dir="state",
        media_endpoint="https://media.example.test/generate",
        media_api_key=SecretStr("secret-token"),
    )


@pytest.mark.asyncio
async def test_generated_media_is_validated_and_registered(tmp_path, monkeypatch) -> None:
    ledger, project = _ledger(tmp_path)
    evidence = Evidence(
        kind=EvidenceKind.USER_STATEMENT,
        claim="Use the supplied product photograph",
        source_title="User brief",
        confidence=1,
        recorded_by="user",
    )
    ledger.update_project(project.id, lambda item: item.evidence.append(evidence))
    monkeypatch.setattr("ophira_backend.media_tools.check_public_http_url", lambda _url: None)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-token"
        return httpx.Response(
            200, headers={"content-type": "image/png"}, content=PNG_BYTES
        )

    result = await generate_media_impl(
        ledger,
        project.id,
        _settings(),
        {
            "kind": "image",
            "prompt": "Make a clean listing image",
            "filename": "listing.png",
            "source_ids": [evidence.id],
        },
        run_id="run_media",
        transport=httpx.MockTransport(handler),
    )

    assert result["ok"] is True
    artifact = ledger.get_project(project.id).artifacts[0]
    assert artifact.id == result["artifact_id"]
    assert artifact.run_id == "run_media"
    assert artifact.source_ids == [evidence.id]
    assert artifact.mime == "image/png"
    assert (
        ledger.workspace_for_project(project.id) / "media/listing.png"
    ).read_bytes() == PNG_BYTES


@pytest.mark.asyncio
async def test_generated_media_never_overwrites_existing_file(tmp_path, monkeypatch) -> None:
    ledger, project = _ledger(tmp_path)
    monkeypatch.setattr("ophira_backend.media_tools.check_public_http_url", lambda _url: None)
    media_dir = ledger.workspace_for_project(project.id) / "media"
    media_dir.mkdir()
    (media_dir / "listing.png").write_bytes(b"USER FILE")

    result = await generate_media_impl(
        ledger,
        project.id,
        _settings(),
        {"kind": "image", "prompt": "Make an image", "filename": "listing.png"},
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, headers={"content-type": "image/png"}, content=PNG_BYTES
            )
        ),
    )

    assert result["path"] == "media/listing.v2.png"
    assert (media_dir / "listing.png").read_bytes() == b"USER FILE"
    assert (media_dir / "listing.v2.png").read_bytes() == PNG_BYTES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://media.example.test/generate", "MEDIA_ENDPOINT_REJECTED"),
        ("https://127.0.0.1/generate", "MEDIA_ENDPOINT_REJECTED"),
    ],
)
async def test_media_endpoint_must_be_public_https(
    tmp_path, monkeypatch, endpoint: str, expected: str
) -> None:
    ledger, project = _ledger(tmp_path)
    settings = _settings().model_copy(update={"media_endpoint": endpoint})
    monkeypatch.setattr(
        "ophira_backend.media_tools.check_public_http_url",
        lambda url: "private target" if "127.0.0.1" in url else None,
    )
    result = await generate_media_impl(
        ledger,
        project.id,
        settings,
        {"kind": "image", "prompt": "Make an image", "filename": "image.png"},
        transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
    )
    assert result["error_code"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "filename", "expected"),
    [
        (
            httpx.Response(302, headers={"location": "https://elsewhere.test"}),
            "x.png",
            "MEDIA_REDIRECT_REJECTED",
        ),
        (
            httpx.Response(
                200, headers={"content-type": "application/json"}, content=b"{}"
            ),
            "x.png",
            "MEDIA_CONTENT_TYPE_REJECTED",
        ),
        (
            httpx.Response(200, headers={"content-type": "image/png"}, content=b"PNG"),
            "x.jpg",
            "MEDIA_EXTENSION_MISMATCH",
        ),
        (
            httpx.Response(200, headers={"content-type": "image/png"}, content=b""),
            "x.png",
            "MEDIA_EMPTY_RESPONSE",
        ),
        (
            httpx.Response(
                200,
                headers={"content-type": "image/png"},
                content=b"<html>upstream error</html>",
            ),
            "x.png",
            "MEDIA_SIGNATURE_REJECTED",
        ),
    ],
)
async def test_media_rejects_redirects_wrong_types_and_empty_files(
    tmp_path, monkeypatch, response: httpx.Response, filename: str, expected: str
) -> None:
    ledger, project = _ledger(tmp_path)
    monkeypatch.setattr("ophira_backend.media_tools.check_public_http_url", lambda _url: None)
    result = await generate_media_impl(
        ledger,
        project.id,
        _settings(),
        {"kind": "image", "prompt": "Make an image", "filename": filename},
        transport=httpx.MockTransport(lambda _request: response),
    )
    assert result["error_code"] == expected
    assert ledger.get_project(project.id).artifacts == []


@pytest.mark.asyncio
async def test_media_rejects_unknown_sources_and_nested_filenames(tmp_path, monkeypatch) -> None:
    ledger, project = _ledger(tmp_path)
    monkeypatch.setattr("ophira_backend.media_tools.check_public_http_url", lambda _url: None)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, headers={"content-type": "image/png"}, content=PNG_BYTES
        )
    )
    unknown = await generate_media_impl(
        ledger,
        project.id,
        _settings(),
        {
            "kind": "image",
            "prompt": "Make an image",
            "filename": "image.png",
            "source_ids": ["ev_missing"],
        },
        transport=transport,
    )
    nested = await generate_media_impl(
        ledger,
        project.id,
        _settings(),
        {"kind": "image", "prompt": "Make an image", "filename": "../image.png"},
        transport=transport,
    )
    assert unknown["error_code"] == "MEDIA_SOURCE_NOT_FOUND"
    assert nested["error_code"] == "MEDIA_FILENAME_REJECTED"

from __future__ import annotations

import base64
import json

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
    project = ledger.create_project(ProjectCreate(name="Media", objective="Create campaign media"))
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
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG_BYTES)

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
            httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}"),
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


def _openai_settings() -> Settings:
    return _settings().model_copy(
        update={
            "media_image_provider": "openai",
            "media_openai_api_key": SecretStr("openai-image-secret"),
        }
    )


@pytest.mark.asyncio
async def test_openai_image_uses_fixed_official_endpoint_and_registers_artifact(tmp_path) -> None:
    ledger, project = _ledger(tmp_path)
    evidence = Evidence(
        kind=EvidenceKind.USER_STATEMENT,
        claim="Blue ceramic mug",
        source_title="User brief",
        confidence=1,
        recorded_by="user",
    )
    ledger.update_project(project.id, lambda item: item.evidence.append(evidence))

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.openai.com/v1/images/generations"
        assert request.headers["authorization"] == "Bearer openai-image-secret"
        assert json.loads(request.content) == {
            "model": "gpt-image-2.5-flare-2026-09-08",
            "prompt": "Blue ceramic mug on a white background",
            "n": 1,
            "size": "1024x1024",
            "quality": "medium",
            "output_format": "png",
        }
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode()}]},
        )

    result = await generate_media_impl(
        ledger,
        project.id,
        _openai_settings(),
        {
            "kind": "image",
            "prompt": "Blue ceramic mug on a white background",
            "filename": "mug.png",
            "source_ids": [evidence.id],
        },
        run_id="run_image",
        transport=httpx.MockTransport(handler),
    )

    assert result["ok"] is True
    assert result["provider"] == "openai"
    assert result["model"] == "gpt-image-2.5-flare-2026-09-08"
    artifact = ledger.get_project(project.id).artifacts[0]
    assert artifact.id == result["artifact_id"]
    assert artifact.run_id == "run_image"
    assert artifact.source_ids == [evidence.id]
    assert artifact.mime == "image/png"
    assert (ledger.workspace_for_project(project.id) / "media/mug.png").read_bytes() == PNG_BYTES


@pytest.mark.asyncio
async def test_openai_image_requires_key_and_png_without_network_call(tmp_path) -> None:
    ledger, project = _ledger(tmp_path)
    transport = httpx.MockTransport(
        lambda _request: pytest.fail("Invalid image request reached the provider")
    )
    missing_key = await generate_media_impl(
        ledger,
        project.id,
        _openai_settings().model_copy(update={"media_openai_api_key": None}),
        {"kind": "image", "prompt": "Make an image", "filename": "image.png"},
        transport=transport,
    )
    wrong_extension = await generate_media_impl(
        ledger,
        project.id,
        _openai_settings(),
        {"kind": "image", "prompt": "Make an image", "filename": "image.jpg"},
        transport=transport,
    )
    assert missing_key["error_code"] == "MEDIA_API_NOT_CONFIGURED"
    assert wrong_extension["error_code"] == "MEDIA_EXTENSION_MISMATCH"
    assert ledger.get_project(project.id).artifacts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            httpx.Response(302, headers={"location": "https://other.example/"}),
            "MEDIA_REDIRECT_REJECTED",
        ),
        (httpx.Response(401, json={"error": {"message": "secret"}}), "MEDIA_API_FAILED"),
        (
            httpx.Response(200, headers={"content-type": "image/png"}, content=PNG_BYTES),
            "MEDIA_CONTENT_TYPE_REJECTED",
        ),
        (
            httpx.Response(200, headers={"content-type": "application/json"}, content=b"{"),
            "MEDIA_RESPONSE_INVALID",
        ),
        (
            httpx.Response(200, headers={"content-type": "application/json"}, json={"data": []}),
            "MEDIA_RESPONSE_INVALID",
        ),
        (
            httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"data": [{"b64_json": "!"}]},
            ),
            "MEDIA_RESPONSE_INVALID",
        ),
        (
            httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"data": [{"b64_json": base64.b64encode(b"not a png").decode()}]},
            ),
            "MEDIA_SIGNATURE_REJECTED",
        ),
    ],
)
async def test_openai_image_rejects_invalid_provider_response(
    tmp_path, response: httpx.Response, expected: str
) -> None:
    ledger, project = _ledger(tmp_path)
    result = await generate_media_impl(
        ledger,
        project.id,
        _openai_settings(),
        {"kind": "image", "prompt": "Make an image", "filename": "image.png"},
        transport=httpx.MockTransport(lambda _request: response),
    )
    assert result["error_code"] == expected
    assert "secret" not in str(result)
    assert ledger.get_project(project.id).artifacts == []
    assert not list((ledger.workspace_for_project(project.id) / "media").glob("*.part"))


@pytest.mark.asyncio
async def test_openai_image_bounds_json_before_decoding(tmp_path, monkeypatch) -> None:
    ledger, project = _ledger(tmp_path)
    monkeypatch.setattr("ophira_backend.media_tools._OPENAI_MAX_RESPONSE_BYTES", 80)
    result = await generate_media_impl(
        ledger,
        project.id,
        _openai_settings(),
        {"kind": "image", "prompt": "Make an image", "filename": "image.png"},
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "data": [{"b64_json": base64.b64encode(PNG_BYTES).decode()}],
                    "extra": "x" * 100,
                },
            )
        ),
    )
    assert result["error_code"] == "MEDIA_TOO_LARGE"
    assert ledger.get_project(project.id).artifacts == []


@pytest.mark.asyncio
async def test_openai_image_setting_keeps_video_on_binary_connector(tmp_path, monkeypatch) -> None:
    ledger, project = _ledger(tmp_path)
    monkeypatch.setattr("ophira_backend.media_tools.check_public_http_url", lambda _url: None)
    video = b"\x00\x00\x00\x18ftypisom" + b"test-video"

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://media.example.test/generate"
        assert request.headers["authorization"] == "Bearer secret-token"
        assert json.loads(request.content)["kind"] == "video"
        return httpx.Response(200, headers={"content-type": "video/mp4"}, content=video)

    result = await generate_media_impl(
        ledger,
        project.id,
        _openai_settings(),
        {"kind": "video", "prompt": "Animate the mug", "filename": "mug.mp4"},
        transport=httpx.MockTransport(handler),
    )
    assert result["ok"] is True
    assert result["provider"] == "raw_binary"
    assert (ledger.workspace_for_project(project.id) / "media/mug.mp4").read_bytes() == video


@pytest.mark.asyncio
async def test_media_rejects_symlinked_output_directory_before_provider_call(tmp_path) -> None:
    ledger, project = _ledger(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (ledger.workspace_for_project(project.id) / "media").symlink_to(outside)
    result = await generate_media_impl(
        ledger,
        project.id,
        _openai_settings(),
        {"kind": "image", "prompt": "Make an image", "filename": "image.png"},
        transport=httpx.MockTransport(
            lambda _request: pytest.fail("Symlinked output reached the provider")
        ),
    )
    assert result["error_code"] == "MEDIA_FILENAME_REJECTED"
    assert list(outside.iterdir()) == []

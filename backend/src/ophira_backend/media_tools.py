"""Safe image/video draft generation and immediate Artifact registration (FR-27)."""

from __future__ import annotations

import base64
import binascii
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

from .config import Settings
from .constants import MEDIA_MCP_SERVER_NAME
from .storage import FileLedger
from .web_tools import check_public_http_url

_MAX_BYTES = {"image": 25_000_000, "video": 250_000_000}
_OPENAI_IMAGE_URL = "https://api.openai.com/v1/images/generations"
_OPENAI_IMAGE_MODEL = "gpt-image-2.5-flare-2026-09-08"
_OPENAI_MAX_RESPONSE_BYTES = ((_MAX_BYTES["image"] + 2) // 3) * 4 + 100_000
_ALLOWED_MEDIA_TYPES: dict[str, dict[str, set[str]]] = {
    "image": {
        "image/png": {".png"},
        "image/jpeg": {".jpg", ".jpeg"},
        "image/webp": {".webp"},
        "image/gif": {".gif"},
    },
    "video": {
        "video/mp4": {".mp4"},
        "video/webm": {".webm"},
        "video/quicktime": {".mov"},
    },
}


def _matches_media_signature(content_type: str, header: bytes) -> bool:
    """Reject HTML/JSON error bodies mislabeled as downloadable media."""
    if content_type == "image/png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return header.startswith(b"\xff\xd8\xff")
    if content_type == "image/gif":
        return header.startswith((b"GIF87a", b"GIF89a"))
    if content_type == "image/webp":
        return len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP"
    if content_type in {"video/mp4", "video/quicktime"}:
        return len(header) >= 12 and header[4:8] == b"ftyp"
    if content_type == "video/webm":
        return header.startswith(b"\x1aE\xdf\xa3")
    return False


def _result(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
        "is_error": is_error,
    }


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"ok": False, "error_code": code, "error": message, **details}


async def _openai_image_bytes(
    settings: Settings,
    prompt: str,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[bytes | None, dict[str, Any] | None]:
    """Read one fixed-format Image API result with a bound before JSON/base64 decoding."""
    key = settings.media_openai_api_key
    if key is None or not key.get_secret_value():
        return None, _error(
            "MEDIA_API_NOT_CONFIGURED",
            "尚未配置 OpenAI 图像生成密钥。",
            hint="Configure OPHIRA_MEDIA_OPENAI_API_KEY for the image adapter.",
        )
    async with httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        timeout=150,
    ) as client:
        async with client.stream(
            "POST",
            _OPENAI_IMAGE_URL,
            headers={"Authorization": f"Bearer {key.get_secret_value()}"},
            json={
                "model": _OPENAI_IMAGE_MODEL,
                "prompt": prompt,
                "n": 1,
                "size": "1024x1024",
                "quality": "medium",
                "output_format": "png",
            },
        ) as response:
            if response.is_redirect:
                return None, _error(
                    "MEDIA_REDIRECT_REJECTED",
                    "图像生成服务重定向已拒绝。",
                    status=response.status_code,
                )
            if response.status_code != 200:
                return None, _error(
                    "MEDIA_API_FAILED",
                    "图像生成服务返回错误。",
                    status=response.status_code,
                )
            content_type = response.headers.get("content-type", "").split(";", 1)[0]
            if content_type.strip().casefold() != "application/json":
                return None, _error(
                    "MEDIA_CONTENT_TYPE_REJECTED",
                    "图像生成服务未返回 JSON 结果。",
                )
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    declared_bytes = int(content_length)
                except ValueError:
                    return None, _error(
                        "MEDIA_CONTENT_LENGTH_INVALID",
                        "图像生成服务返回了无效的 Content-Length。",
                    )
                if declared_bytes < 0 or declared_bytes > _OPENAI_MAX_RESPONSE_BYTES:
                    return None, _error(
                        "MEDIA_TOO_LARGE",
                        "图像生成结果超过大小上限。",
                        max_bytes=_MAX_BYTES["image"],
                    )
            payload = bytearray()
            async for chunk in response.aiter_bytes():
                if len(payload) + len(chunk) > _OPENAI_MAX_RESPONSE_BYTES:
                    return None, _error(
                        "MEDIA_TOO_LARGE",
                        "图像生成结果超过大小上限。",
                        max_bytes=_MAX_BYTES["image"],
                    )
                payload.extend(chunk)
    try:
        result = json.loads(payload)
    except (UnicodeError, ValueError):
        return None, _error("MEDIA_RESPONSE_INVALID", "图像生成服务返回了无效 JSON。")
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        return None, _error("MEDIA_RESPONSE_INVALID", "图像生成结果缺少图像数据。")
    data = result["data"]
    if len(data) != 1 or not isinstance(data[0], dict):
        return None, _error("MEDIA_RESPONSE_INVALID", "图像生成结果数量不正确。")
    encoded = data[0].get("b64_json")
    if not isinstance(encoded, str) or not encoded:
        return None, _error("MEDIA_RESPONSE_INVALID", "图像生成结果缺少图像数据。")
    if len(encoded) > ((_MAX_BYTES["image"] + 2) // 3) * 4:
        return None, _error("MEDIA_TOO_LARGE", "生成媒体超过大小上限。")
    try:
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None, _error("MEDIA_RESPONSE_INVALID", "图像生成服务返回了无效图像编码。")
    if not image:
        return None, _error("MEDIA_EMPTY_RESPONSE", "图像生成服务返回了空文件。")
    if len(image) > _MAX_BYTES["image"]:
        return None, _error(
            "MEDIA_TOO_LARGE",
            "生成媒体超过大小上限。",
            max_bytes=_MAX_BYTES["image"],
        )
    return image, None


def _safe_media_target(workspace: Path, filename: str, kind: str) -> tuple[Path, str | None]:
    if Path(filename).name != filename or "\x00" in filename:
        return workspace, "文件名必须是单一文件名，不能包含目录"
    suffix = Path(filename).suffix.casefold()
    allowed = set().union(*_ALLOWED_MEDIA_TYPES[kind].values())
    if suffix not in allowed:
        return workspace, f"{kind} 文件扩展名必须是：{', '.join(sorted(allowed))}"
    media_dir = workspace / "media"
    if media_dir.is_symlink():
        return workspace, "媒体目录不能是符号链接"
    media_dir.mkdir(parents=True, exist_ok=True)
    media_dir = media_dir.resolve()
    candidate = media_dir / filename
    if candidate.is_symlink():
        return workspace, "媒体目标不能是符号链接"
    target = candidate.resolve()
    try:
        target.relative_to(workspace.resolve())
    except ValueError:
        return workspace, "媒体文件路径越过项目工作区"
    index = 2
    while target.exists():
        target = media_dir / f"{Path(filename).stem}.v{index}{Path(filename).suffix}"
        index += 1
    return target, None


async def generate_media_impl(
    ledger: FileLedger,
    project_id: str,
    settings: Settings,
    args: dict[str, Any],
    *,
    run_id: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Generate one draft through the selected adapter and register its validated bytes."""
    kind = str(args["kind"])
    use_openai = kind == "image" and settings.media_image_provider == "openai"
    endpoint = settings.media_endpoint
    token = settings.media_api_key.get_secret_value() if settings.media_api_key else ""
    if not use_openai:
        if not endpoint or not token:
            return _error(
                "MEDIA_API_NOT_CONFIGURED",
                "尚未配置媒体生成端点与密钥。",
                hint="Configure a media connector (HTTPS endpoint + key).",
            )

        reason = check_public_http_url(endpoint)
        try:
            scheme = urlsplit(endpoint).scheme.casefold()
        except ValueError:
            scheme = ""
        if reason or scheme != "https":
            return _error(
                "MEDIA_ENDPOINT_REJECTED",
                reason or "携带生成密钥的媒体端点必须使用 HTTPS。",
            )

    workspace = ledger.workspace_for_project(project_id).resolve()
    target, target_error = _safe_media_target(workspace, str(args["filename"]), kind)
    if target_error:
        return _error("MEDIA_FILENAME_REJECTED", target_error)
    if use_openai and target.suffix.casefold() != ".png":
        return _error(
            "MEDIA_EXTENSION_MISMATCH",
            "OpenAI 图像适配器只生成 PNG，请使用 .png 文件名。",
            content_type="image/png",
        )

    source_ids = list(dict.fromkeys(args.get("source_ids") or []))
    project = ledger.get_project(project_id)
    known_source_ids = {
        *(evidence.id for evidence in project.evidence),
        *(artifact.id for artifact in project.artifacts),
    }
    unknown_source_ids = sorted(set(source_ids) - known_source_ids)
    if unknown_source_ids:
        return _error(
            "MEDIA_SOURCE_NOT_FOUND",
            "部分来源 ID 不属于当前项目。",
            unknown_source_ids=unknown_source_ids,
        )

    temporary = target.with_name(f".{target.name}.{uuid4().hex}.part")
    max_bytes = _MAX_BYTES[kind]
    content_type = ""
    written = 0
    signature = bytearray()
    try:
        if use_openai:
            image, error = await _openai_image_bytes(settings, str(args["prompt"]), transport)
            if error is not None:
                return error
            if image is None:
                return _error("MEDIA_RESPONSE_INVALID", "图像生成结果缺少图像数据。")
            content_type = "image/png"
            written = len(image)
            signature.extend(image[:16])
            with temporary.open("xb") as handle:
                handle.write(image)
                handle.flush()
                os.fsync(handle.fileno())
        else:
            async with httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                timeout=settings.web_timeout_seconds,
            ) as client:
                async with client.stream(
                    "POST",
                    endpoint,
                    headers={"Authorization": f"Bearer {token}"},
                    json={"prompt": args["prompt"], "kind": kind},
                ) as response:
                    if response.is_redirect:
                        return _error(
                            "MEDIA_REDIRECT_REJECTED",
                            "媒体端点重定向已拒绝；请直接配置最终 HTTPS 端点。",
                            status=response.status_code,
                        )
                    if response.status_code >= 400:
                        return _error(
                            "MEDIA_API_FAILED",
                            "媒体生成服务返回错误。",
                            status=response.status_code,
                        )
                    content_type = (
                        response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                    )
                    allowed_suffixes = _ALLOWED_MEDIA_TYPES[kind].get(content_type)
                    if allowed_suffixes is None:
                        return _error(
                            "MEDIA_CONTENT_TYPE_REJECTED",
                            "媒体生成服务未返回受支持的二进制媒体类型。",
                            content_type=content_type or "unknown",
                        )
                    if target.suffix.casefold() not in allowed_suffixes:
                        return _error(
                            "MEDIA_EXTENSION_MISMATCH",
                            "文件扩展名与服务返回的媒体类型不一致。",
                            content_type=content_type,
                        )
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            declared_bytes = int(content_length)
                        except ValueError:
                            return _error(
                                "MEDIA_CONTENT_LENGTH_INVALID",
                                "媒体生成服务返回了无效的 Content-Length。",
                            )
                        if declared_bytes > max_bytes:
                            return _error(
                                "MEDIA_TOO_LARGE",
                                "生成媒体超过大小上限。",
                                max_bytes=max_bytes,
                            )
                    with temporary.open("xb") as handle:
                        async for chunk in response.aiter_bytes():
                            written += len(chunk)
                            if written > max_bytes:
                                raise OverflowError
                            if len(signature) < 16:
                                signature.extend(chunk[: 16 - len(signature)])
                            handle.write(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
        if written == 0:
            return _error("MEDIA_EMPTY_RESPONSE", "媒体生成服务返回了空文件。")
        if not _matches_media_signature(content_type, bytes(signature)):
            return _error(
                "MEDIA_SIGNATURE_REJECTED",
                "媒体生成服务返回的文件签名与 Content-Type 不匹配。",
                content_type=content_type,
            )
        try:
            os.link(temporary, target)
        except FileExistsError:
            # A second generator may have claimed the friendly versioned name after
            # our existence check. Fall back to a collision-proof sibling without
            # ever replacing that file.
            target = target.with_name(f"{target.stem}.{uuid4().hex[:10]}{target.suffix}")
            os.link(temporary, target)
        temporary.unlink()
        relative_path = target.relative_to(workspace).as_posix()
        try:
            artifact = ledger.register_artifact(
                project_id,
                relative_path,
                run_id=run_id,
                source_ids=source_ids,
                mime=content_type,
            )
        except (OSError, ValueError):
            target.unlink(missing_ok=True)
            return _error(
                "MEDIA_ARTIFACT_REGISTRATION_FAILED",
                "媒体已生成但无法安全登记，因此已移除未登记文件。",
            )
        return {
            "ok": True,
            "path": relative_path,
            "kind": kind,
            "mime": content_type,
            "size_bytes": written,
            "provider": "openai" if use_openai else "raw_binary",
            "model": _OPENAI_IMAGE_MODEL if use_openai else None,
            "artifact_id": artifact.id,
            "source_ids": artifact.source_ids,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "note": "Draft only; publishing needs a separate high-risk approval.",
        }
    except OverflowError:
        return _error(
            "MEDIA_TOO_LARGE",
            "生成媒体超过大小上限。",
            max_bytes=max_bytes,
        )
    except (httpx.HTTPError, OSError):
        return _error(
            "MEDIA_GENERATION_FAILED",
            "媒体生成或安全落盘失败；没有登记不完整产物。",
        )
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def build_media_mcp_server(
    ledger: FileLedger,
    project_id: str,
    settings: Settings,
    *,
    run_id: str | None = None,
):
    @tool(
        "generate_media",
        "Generate a listing or social image/video draft via the configured media "
        "connector. Does not publish. Requires a raw binary response, validates type "
        "and size, and immediately registers the file as an Artifact.",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["image", "video"]},
                "prompt": {"type": "string", "minLength": 4, "maxLength": 4000},
                "filename": {"type": "string", "minLength": 3, "maxLength": 120},
                "source_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "maxItems": 100,
                    "default": [],
                },
            },
            "required": ["kind", "prompt", "filename"],
            "additionalProperties": False,
        },
    )
    async def generate_media(args: dict[str, Any]) -> dict[str, Any]:
        result = await generate_media_impl(
            ledger,
            project_id,
            settings,
            args,
            run_id=run_id,
        )
        return _result(result, is_error=not bool(result.get("ok")))

    return create_sdk_mcp_server(
        name=MEDIA_MCP_SERVER_NAME,
        version="1.0.0",
        tools=[generate_media],
    )

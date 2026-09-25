"""Durable, opt-in Runway text-to-video task and bounded public output download."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field, ValidationError

from .config import Settings
from .storage import FileLedger
from .web_tools import send_pinned_public_request

RUNWAY_MODEL = "gen4.5"
_RUNWAY_API = "https://api.dev.runwayml.com/v1"
_RUNWAY_VERSION = "2024-11-06"
_MAX_JSON_BYTES = 64_000
_MAX_VIDEO_BYTES = 250_000_000
_DOWNLOAD_LEASE = timedelta(minutes=10)


class RunwayJob(BaseModel):
    schema_version: Literal[1] = 1
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["creating", "submitted", "fetching", "failed", "complete"]
    task_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    )
    run_id: str | None = None
    fetching_until: datetime | None = None
    artifact_id: str | None = None
    path: str | None = None


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"ok": False, "error_code": code, "error": message, **details}


def _job_location(
    ledger: FileLedger,
    project_id: str,
    prompt: str,
    filename: str,
    source_ids: list[str],
) -> tuple[Path, str]:
    request_hash = hashlib.sha256(
        json.dumps(
            [project_id, RUNWAY_MODEL, prompt, filename, source_ids],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    path = ledger._project_path(project_id).parent / "media-tasks" / f"{request_hash}.json"
    return path, request_hash


def _load_job(path: Path, request_hash: str) -> RunwayJob:
    if path.is_symlink() or path.stat().st_size > _MAX_JSON_BYTES:
        raise ValueError("invalid media task record")
    job = RunwayJob.model_validate_json(path.read_bytes())
    if job.request_hash != request_hash:
        raise ValueError("media task identity mismatch")
    return job


def _save_job(ledger: FileLedger, path: Path, job: RunwayJob) -> None:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    ledger._atomic_write(path, job)


def _update_job(
    ledger: FileLedger,
    path: Path,
    request_hash: str,
    *,
    expected: str,
    changes: dict[str, Any],
) -> RunwayJob | None:
    with ledger._lock_for(f"media-task:{request_hash}"):
        job = _load_job(path, request_hash)
        if job.status != expected:
            return None
        updated = job.model_copy(update=changes)
        _save_job(ledger, path, updated)
        return updated


async def _api_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    async with client.stream(method, url, headers=headers, json=payload) as response:
        if response.is_redirect:
            return None, _error(
                "MEDIA_REDIRECT_REJECTED",
                "Runway API 重定向已拒绝。",
                status=response.status_code,
            )
        if response.status_code not in {200, 201}:
            return None, _error(
                "MEDIA_API_FAILED",
                "Runway API 返回错误。",
                status=response.status_code,
            )
        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
        if mime != "application/json":
            return None, _error("MEDIA_RESPONSE_INVALID", "Runway API 未返回 JSON。")
        declared = response.headers.get("content-length")
        if declared:
            try:
                length = int(declared)
            except ValueError:
                return None, _error("MEDIA_CONTENT_LENGTH_INVALID", "无效的 Content-Length。")
            if length < 0 or length > _MAX_JSON_BYTES:
                return None, _error("MEDIA_TOO_LARGE", "Runway 状态结果超过大小上限。")
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            if len(raw) + len(chunk) > _MAX_JSON_BYTES:
                return None, _error("MEDIA_TOO_LARGE", "Runway 状态结果超过大小上限。")
            raw.extend(chunk)
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError):
        return None, _error("MEDIA_RESPONSE_INVALID", "Runway API 返回无效 JSON。")
    if not isinstance(value, dict):
        return None, _error("MEDIA_RESPONSE_INVALID", "Runway API 返回无效对象。")
    return value, None


async def _download_video(
    url: str,
    temporary: Path,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[tuple[int, bytes] | None, dict[str, Any] | None]:
    current = url
    async with httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
        timeout=60,
        limits=httpx.Limits(max_keepalive_connections=0),
    ) as client:
        for _hop in range(4):
            if urlsplit(current).scheme != "https":
                return None, _error("MEDIA_OUTPUT_URL_REJECTED", "视频下载地址必须使用 HTTPS。")
            response, reason = await send_pinned_public_request(
                client,
                "GET",
                current,
                stream=True,
            )
            if reason or response is None:
                return None, _error("MEDIA_OUTPUT_URL_REJECTED", "视频下载地址未通过公网校验。")
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return None, _error("MEDIA_OUTPUT_URL_REJECTED", "视频重定向缺少目标。")
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    return None, _error(
                        "MEDIA_DOWNLOAD_FAILED",
                        "视频下载失败。",
                        status=response.status_code,
                    )
                mime = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                if mime not in {"video/mp4", "application/octet-stream"}:
                    return None, _error("MEDIA_CONTENT_TYPE_REJECTED", "视频下载类型不受支持。")
                declared = response.headers.get("content-length")
                if declared:
                    try:
                        length = int(declared)
                    except ValueError:
                        return None, _error(
                            "MEDIA_CONTENT_LENGTH_INVALID", "无效的 Content-Length。"
                        )
                    if length < 0 or length > _MAX_VIDEO_BYTES:
                        return None, _error("MEDIA_TOO_LARGE", "生成视频超过大小上限。")
                written = 0
                signature = bytearray()
                with temporary.open("xb") as handle:
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        if written > _MAX_VIDEO_BYTES:
                            return None, _error("MEDIA_TOO_LARGE", "生成视频超过大小上限。")
                        if len(signature) < 16:
                            signature.extend(chunk[: 16 - len(signature)])
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                return (written, bytes(signature)), None
            finally:
                await response.aclose()
    return None, _error("MEDIA_OUTPUT_URL_REJECTED", "视频下载重定向次数过多。")


async def runway_video_to_temporary(
    ledger: FileLedger,
    project_id: str,
    settings: Settings,
    *,
    prompt: str,
    filename: str,
    target_relative: str,
    source_ids: list[str],
    run_id: str | None,
    temporary: Path,
    transport: httpx.AsyncBaseTransport | None,
) -> dict[str, Any]:
    key = settings.media_runway_api_key
    if key is None or not key.get_secret_value():
        return _error(
            "MEDIA_API_NOT_CONFIGURED",
            "尚未配置 Runway 视频生成密钥。",
            hint="Configure OPHIRA_MEDIA_RUNWAY_API_KEY for the video adapter.",
        )
    path, request_hash = _job_location(ledger, project_id, prompt, filename, source_ids)
    try:
        with ledger._lock_for(f"media-task:{request_hash}"):
            if path.exists() or path.is_symlink():
                job = _load_job(path, request_hash)
                fresh = False
            else:
                job = RunwayJob(
                    request_hash=request_hash, status="creating",
                    run_id=run_id, path=target_relative,
                )
                _save_job(ledger, path, job)
                fresh = True
    except (OSError, ValueError, ValidationError):
        return _error("MEDIA_TASK_STATE_INVALID", "视频任务记录无法安全读取。")

    if job.status == "complete":
        artifact = next(
            (
                item
                for item in ledger.get_project(project_id).artifacts
                if item.id == job.artifact_id and item.path == job.path
            ),
            None,
        )
        if artifact is None:
            return _error("MEDIA_TASK_STATE_INVALID", "已完成视频任务缺少产物记录。")
        return {
            "ok": True,
            "path": artifact.path,
            "kind": "video",
            "mime": artifact.mime,
            "size_bytes": artifact.size_bytes,
            "provider": "runway",
            "model": RUNWAY_MODEL,
            "artifact_id": artifact.id,
            "source_ids": artifact.source_ids,
            "task_id": job.task_id,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "note": "Draft only; publishing needs a separate high-risk approval.",
        }
    if job.status == "failed":
        return _error(
            "MEDIA_TASK_FAILED", "Runway 视频任务失败；不会自动重新计费生成。", task_id=job.task_id
        )
    if job.status == "fetching":
        if job.fetching_until and job.fetching_until > datetime.now(UTC):
            return _error("MEDIA_TASK_PENDING", "视频文件正在安全下载。", task_id=job.task_id)
        recovered = _update_job(
            ledger,
            path,
            request_hash,
            expected="fetching",
            changes={"status": "submitted", "fetching_until": None},
        )
        if recovered is None:
            return _error(
                "MEDIA_TASK_PENDING", "视频任务状态已变化，请稍后重试。", task_id=job.task_id
            )
        job = recovered
    if job.status == "creating" and not fresh:
        return _error(
            "MEDIA_TASK_CREATE_UNCERTAIN",
            "视频任务创建结果不确定；不会自动重复提交付费请求。",
            request_id=request_hash,
        )

    headers = {
        "Authorization": f"Bearer {key.get_secret_value()}",
        "X-Runway-Version": _RUNWAY_VERSION,
    }
    async with httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
        timeout=30,
    ) as client:
        if fresh:
            try:
                created, error = await _api_json(
                    client,
                    "POST",
                    f"{_RUNWAY_API}/image_to_video",
                    headers,
                    {
                        "model": RUNWAY_MODEL,
                        "promptText": prompt,
                        "ratio": "720:1280",
                        "duration": 5,
                    },
                )
            except httpx.HTTPError:
                return _error(
                    "MEDIA_TASK_CREATE_UNCERTAIN",
                    "视频任务创建结果不确定；不会自动重复提交付费请求。",
                    request_id=request_hash,
                )
            if error is not None:
                if error.get("status", 500) < 500:
                    _update_job(
                        ledger,
                        path,
                        request_hash,
                        expected="creating",
                        changes={"status": "failed"},
                    )
                return error
            task_id = created.get("id") if created else None
            if not isinstance(task_id, str) or not re.fullmatch(
                r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", task_id
            ):
                return _error(
                    "MEDIA_TASK_CREATE_UNCERTAIN",
                    "视频任务已提交但未返回有效任务 ID；不会自动重试。",
                    request_id=request_hash,
                )
            job = (
                _update_job(
                    ledger,
                    path,
                    request_hash,
                    expected="creating",
                    changes={"status": "submitted", "task_id": task_id},
                )
                or job
            )
        if job.status != "submitted" or not job.task_id or not job.path:
            return _error("MEDIA_TASK_STATE_INVALID", "视频任务状态不一致。")
        try:
            status, error = await _api_json(
                client,
                "GET",
                f"{_RUNWAY_API}/tasks/{job.task_id}",
                headers,
            )
        except httpx.HTTPError:
            return _error(
                "MEDIA_TASK_STATUS_FAILED",
                "视频状态查询失败，可用相同参数重试。",
                task_id=job.task_id,
            )
    if error is not None:
        return {**error, "task_id": job.task_id}
    if status is None or status.get("id") != job.task_id:
        return _error("MEDIA_RESPONSE_INVALID", "Runway 返回了不匹配的任务 ID。")
    state = status.get("status")
    if state in {"PENDING", "THROTTLED", "RUNNING"}:
        return _error(
            "MEDIA_TASK_PENDING",
            "视频仍在生成；请至少五秒后用相同参数调用。",
            task_id=job.task_id,
            retry_after_seconds=5,
        )
    if state in {"FAILED", "CANCELED"}:
        _update_job(
            ledger,
            path,
            request_hash,
            expected="submitted",
            changes={"status": "failed"},
        )
        return _error("MEDIA_TASK_FAILED", "Runway 视频任务未完成。", task_id=job.task_id)
    if state != "SUCCEEDED":
        return _error("MEDIA_RESPONSE_INVALID", "Runway 返回未知视频任务状态。")
    output = status.get("output")
    if not isinstance(output, list) or len(output) != 1 or not isinstance(output[0], str):
        return _error("MEDIA_RESPONSE_INVALID", "视频任务缺少唯一下载地址。")
    claimed = _update_job(
        ledger,
        path,
        request_hash,
        expected="submitted",
        changes={"status": "fetching", "fetching_until": datetime.now(UTC) + _DOWNLOAD_LEASE},
    )
    if claimed is None:
        return _error("MEDIA_TASK_PENDING", "视频文件已由其他工作进程认领。", task_id=job.task_id)
    try:
        downloaded, error = await _download_video(output[0], temporary, transport)
    except (httpx.HTTPError, OSError, ValueError):
        downloaded, error = (
            None,
            _error("MEDIA_DOWNLOAD_FAILED", "视频下载失败，可用相同参数重试。"),
        )
    if error is not None:
        _update_job(
            ledger,
            path,
            request_hash,
            expected="fetching",
            changes={"status": "submitted", "fetching_until": None},
        )
        return {**error, "task_id": job.task_id}
    if downloaded is None:
        return _error("MEDIA_DOWNLOAD_FAILED", "视频下载结果缺失。", task_id=job.task_id)
    return {
        "ok": True,
        "request_hash": request_hash,
        "task_id": job.task_id,
        "written": downloaded[0],
        "signature": downloaded[1],
        "run_id": job.run_id,
        "path": job.path,
    }


def release_runway_download(ledger: FileLedger, project_id: str, request_hash: str) -> None:
    path = ledger._project_path(project_id).parent / "media-tasks" / f"{request_hash}.json"
    _update_job(
        ledger,
        path,
        request_hash,
        expected="fetching",
        changes={"status": "submitted", "fetching_until": None},
    )


def finish_runway_download(
    ledger: FileLedger,
    project_id: str,
    request_hash: str,
    artifact_id: str,
    path_value: str,
) -> None:
    path = ledger._project_path(project_id).parent / "media-tasks" / f"{request_hash}.json"
    updated = _update_job(
        ledger,
        path,
        request_hash,
        expected="fetching",
        changes={
            "status": "complete",
            "fetching_until": None,
            "artifact_id": artifact_id,
            "path": path_value,
        },
    )
    if updated is None:
        raise ValueError("video task changed before Artifact registration completed")

"""Bounded local PDF/image preprocessing, never a second Agent runtime."""

from __future__ import annotations

import json
import math
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .untrusted import (
    UNTRUSTED_LIMITS,
    UntrustedSource,
    sanitize_untrusted_text,
    wrap_untrusted_text,
)

if TYPE_CHECKING:
    from .config import Settings

PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".gif"}
EXTRACTION_FAILURE_NOTICE = (
    "未能提取到可读文本。请配置本地 PaddleOCR-VL 后重新上传，或提供可检索的 "
    "PDF/文字版材料。原件仅供用户下载核对；Agent 不得绕过摄取直接读取 PDF/图片，"
    "也不得从缺失文本推断商业事实。"
)
_DIAGNOSTICS = {
    "paddle_not_configured": "PaddleOCR-VL 未配置，使用显式降级路径。",
    "paddle_unavailable": "PaddleOCR-VL 依赖不可用，使用显式降级路径。",
    "paddle_failed": "PaddleOCR-VL 未完成有效提取，使用显式降级路径。",
    "paddle_invalid_config": "PaddleOCR-VL 本地模型目录无效，未初始化模型。",
    "figures_not_transcribed": "Markdown 中的图像引用未转写，已标为缺失；不包含原图或坐标。",
    "pypdf_text_only": "pypdf 仅提取已有文本层，不识别扫描图片、不验证版式或表格结构。",
    "missing_pages": "部分页面没有可读文本，页内标记指出缺失位置；不可视为完整摄取。",
    "truncated": "提取文本达到安全长度上限，已截断；请拆分材料后重新上传。",
    "page_limit": "材料超过摄取页数上限，未处理；请拆分后重新上传。",
    "pixel_limit": "图片超过像素上限，未处理；请降低分辨率后重新上传。",
    "encrypted": "PDF 受密码保护，未处理；请由用户提供已解密副本。",
    "invalid_document": "文件损坏或格式不符，未处理。",
    "unavailable": "所需提取工具未安装。",
    "failed": "提取工具失败，未返回可信的提取结果。",
    "timeout": "摄取达到墙钟时限，工作进程已停止；请拆分材料后重新上传。",
    "source_limit": "原件超过摄取大小上限，未处理；请拆分后重新上传。",
    "source_invalid": "摄取只接受可读取的普通本地文件，不接受符号链接或目录。",
}


@dataclass(frozen=True)
class IngestConfig:
    paddle_enabled: bool = False
    layout_model_dir: Path | None = None
    vl_model_dir: Path | None = None
    device: str = "cpu"
    timeout_seconds: float = 60
    max_bytes: int = 32_000_000
    max_pages: int = 20
    max_pixels: int = 40_000_000
    memory_mb: int = 8_192
    tesseract_languages: str = "chi_sim+eng"

    @classmethod
    def from_settings(cls, settings: Settings) -> IngestConfig:
        return cls(
            paddle_enabled=settings.ocr_paddle_enabled,
            layout_model_dir=settings.ocr_layout_model_dir,
            vl_model_dir=settings.ocr_vl_model_dir,
            device=settings.ocr_device,
            timeout_seconds=settings.ocr_timeout_seconds,
            max_bytes=settings.ocr_max_bytes,
            max_pages=settings.ocr_max_pages,
            max_pixels=settings.ocr_max_pixels,
            memory_mb=settings.ocr_memory_mb,
            tesseract_languages=settings.ocr_tesseract_languages,
        )

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 600:
            raise ValueError("OCR timeout must be positive and at most 600 seconds")
        if not 1 <= self.max_pages <= 200 or not 1 <= self.max_bytes <= 100_000_000:
            raise ValueError("OCR source limits out of range")
        if not 1 <= self.max_pixels <= 100_000_000 or not 256 <= self.memory_mb <= 32_768:
            raise ValueError("OCR resource limits out of range")
        if self.device not in {"cpu", "gpu:0"}:
            raise ValueError("OCR device must be cpu or gpu:0")
        if not self.tesseract_languages or len(self.tesseract_languages) > 120:
            raise ValueError("invalid Tesseract languages")


def document_kind(filename: str, *, mime: str | None = None, header: bytes = b"") -> str | None:
    """Route by signature, then declared media type/suffix; decoders validate content."""
    if header.lstrip().startswith(b"%PDF-"):
        return "pdf"
    if (
        header.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a",
                           b"II*\x00", b"MM\x00*"))
        or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")
    ):
        return "image"
    if mime and mime.split(";", 1)[0].strip().lower() == "application/pdf":
        return "pdf"
    if mime and mime.split(";", 1)[0].strip().lower() in {
        "image/png", "image/jpeg", "image/webp", "image/tiff", "image/gif",
    }:
        return "image"
    suffix = Path(filename).suffix.lower()
    return "pdf" if suffix in PDF_SUFFIXES else "image" if suffix in IMAGE_SUFFIXES else None


def needs_markdown_companion(
    filename: str, *, mime: str | None = None, header: bytes = b"",
) -> bool:
    return document_kind(filename, mime=mime, header=header) is not None


def file_needs_markdown_companion(source: Path) -> bool:
    return needs_markdown_companion(source.name, header=_source_header(source))


def _source_header(source: Path) -> bytes:
    try:
        if not stat.S_ISREG(source.lstat().st_mode):
            return b""
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return b""
            return stream.read(16)
    except OSError:
        return b""


def ingest_to_markdown(
    source: Path, *, original_name: str, config: IngestConfig | None = None,
    mime: str | None = None,
) -> tuple[str, str]:
    """Return (markdown_text, backend_name). Never raises on extract failure."""
    kind = document_kind(original_name, mime=mime, header=_source_header(source))
    if kind is None:
        return "", "passthrough"
    extract = _pdf_text if kind == "pdf" else _image_text
    text, backend = extract(source) if config is None else extract(source, config)
    safe_name = sanitize_untrusted_text(
        source.name,
        UntrustedSource.UPLOAD_EXTRACT,
        max_chars=240,
    ).text
    safe_name = " ".join(safe_name.split()).replace("`", "") or "upload"
    header = (
        f"# 摄取文本 · {safe_name}\n\n"
        f"- 后端：{backend}\n"
        f"- 原件（仅供用户核对）：`uploads/{safe_name}`\n"
        "- Agent 输入：仅使用本 Markdown；不直接读取原件。\n\n"
    )
    body = text.strip() or EXTRACTION_FAILURE_NOTICE
    wrapped = wrap_untrusted_text(body, UntrustedSource.UPLOAD_EXTRACT)
    return header + wrapped.text + "\n", backend


def _pdf_text(source: Path, config: IngestConfig | None = None) -> tuple[str, str]:
    return _extract(source, "pdf", config or IngestConfig())


def _image_text(source: Path, config: IngestConfig | None = None) -> tuple[str, str]:
    return _extract(source, "image", config or IngestConfig())


def _extract(source: Path, kind: str, config: IngestConfig) -> tuple[str, str]:
    try:
        result = _run_worker(source, kind, config)
    except (OSError, ValueError, TypeError):
        result = {"text": "", "backend": "unavailable", "warnings": ["failed"]}
    backend, text = result.get("backend"), result.get("text")
    warnings = result.get("warnings", [])
    if (
        backend not in {"paddleocr-vl", "pypdf", "tesseract", "unavailable"}
        or not isinstance(text, str) or not isinstance(warnings, list)
        or any(not isinstance(item, str) or item not in _DIAGNOSTICS for item in warnings)
    ):
        backend, text, warnings = "unavailable", "", ["failed"]
    notices = "\n".join(f"- {_DIAGNOSTICS[item]}" for item in dict.fromkeys(warnings))
    status = "失败" if not text.strip() else ("降级/不完整" if warnings else "已提取（仍需核对）")
    body = f"摄取状态：{status}\n{notices}\n\n{text or EXTRACTION_FAILURE_NOTICE}"
    return body, backend


def _run_worker(source: Path, kind: str, config: IngestConfig) -> dict:
    """Disposable subprocess; no inherited provider credentials or unbounded pipes."""
    try:
        source_stat = source.lstat()
    except OSError:
        return {"text": "", "backend": "unavailable", "warnings": ["source_invalid"]}
    if not stat.S_ISREG(source_stat.st_mode):
        return {"text": "", "backend": "unavailable", "warnings": ["source_invalid"]}
    if source_stat.st_size > config.max_bytes:
        return {"text": "", "backend": "unavailable", "warnings": ["source_limit"]}
    with tempfile.TemporaryDirectory(prefix="ophira-ingest-") as directory:
        scratch = Path(directory)
        # Paddle's input reader also selects decoders by suffix; normalize only
        # our private input copy when the uploaded name lacks the right suffix.
        staged = scratch / ("source.pdf" if kind == "pdf" else "source.image")
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as input_file:
            opened = os.fstat(input_file.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError("source is no longer a regular file")
            content = input_file.read(config.max_bytes + 1)
        if len(content) > config.max_bytes:
            return {"text": "", "backend": "unavailable", "warnings": ["source_limit"]}
        staged.write_bytes(content)
        request = asdict(config)
        request.update({
            "source": str(staged), "kind": kind,
            "result_path": str(scratch / "result.json"),
            "max_chars": UNTRUSTED_LIMITS[UntrustedSource.UPLOAD_EXTRACT].field_chars - 2_000,
            "tesseract": shutil.which("tesseract"),
        })
        for key in ("layout_model_dir", "vl_model_dir"):
            request[key] = str(request[key].resolve()) if request[key] else None
        environment = {
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
            "XDG_CACHE_HOME": str(scratch / "cache"),
            "PADDLE_PDX_CACHE_HOME": str(scratch / "paddlex"),
            "HF_HOME": str(scratch / "huggingface"),
            "TMPDIR": str(scratch),
        }
        with subprocess.Popen(
            [sys.executable, "-I", str(Path(__file__).with_name("ingest_worker.py"))],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=environment, cwd=scratch, start_new_session=True,
        ) as process:
            try:
                process.communicate(json.dumps(request).encode(), timeout=config.timeout_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                return {"text": "", "backend": "unavailable", "warnings": ["timeout"]}
        result_path = scratch / "result.json"
        if process.returncode or not result_path.is_file() or result_path.stat().st_size > 400_000:
            return {"text": "", "backend": "unavailable", "warnings": ["failed"]}
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError("invalid ingestion worker result")
        return result

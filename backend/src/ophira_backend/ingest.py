"""Turn uploaded PDF/images into Markdown before they reach the Agent.

PaddleOCR-VL is the production OCR backend when installed. Without it, PDFs
use pypdf text extraction and images try Tesseract on PATH. The original
binary is always kept; the Agent is pointed at the Markdown companion.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .untrusted import UntrustedSource, sanitize_untrusted_text, wrap_untrusted_text

PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".gif"}


def needs_markdown_companion(filename: str) -> bool:
    suffix = Path(filename).suffix.lower()
    return suffix in PDF_SUFFIXES or suffix in IMAGE_SUFFIXES


def ingest_to_markdown(source: Path, *, original_name: str) -> tuple[str, str]:
    """Return (markdown_text, backend_name). Never raises on extract failure."""
    suffix = Path(original_name).suffix.lower()
    if suffix in PDF_SUFFIXES:
        text, backend = _pdf_text(source)
    elif suffix in IMAGE_SUFFIXES:
        text, backend = _image_text(source)
    else:
        return "", "passthrough"
    safe_name = sanitize_untrusted_text(
        source.name,
        UntrustedSource.UPLOAD_EXTRACT,
        max_chars=240,
    ).text
    safe_name = " ".join(safe_name.split()) or "upload"
    header = (
        f"# 摄取文本 · {safe_name}\n\n"
        f"- 后端：{backend}\n"
        f"- 原件：`uploads/{safe_name}`\n\n"
    )
    body = text.strip() or "（未能提取到可读文本。若 Provider 支持视觉，请直接阅读原件。）"
    wrapped = wrap_untrusted_text(body, UntrustedSource.UPLOAD_EXTRACT)
    return header + wrapped.text + "\n", backend


def _pdf_text(source: Path) -> tuple[str, str]:
    paddle = _try_paddle(source)
    if paddle is not None:
        return paddle, "paddleocr-vl"
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", "unavailable"
    try:
        reader = PdfReader(str(source))
        pages = []
        for index, page in enumerate(reader.pages, start=1):
            extracted = page.extract_text() or ""
            pages.append(f"## 第 {index} 页\n\n{extracted.strip()}")
        return "\n\n".join(pages), "pypdf"
    except Exception:
        return "", "pypdf-failed"


def _image_text(source: Path) -> tuple[str, str]:
    paddle = _try_paddle(source)
    if paddle is not None:
        return paddle, "paddleocr-vl"
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return "", "vision-pending"
    try:
        result = subprocess.run(
            [tesseract, str(source), "stdout", "-l", "chi_sim+eng"],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "", "tesseract-failed"
    return result.stdout, "tesseract"


def _try_paddle(source: Path) -> str | None:
    try:
        from paddleocr import PaddleOCR  # type: ignore
    except ImportError:
        return None
    try:
        ocr = PaddleOCR(lang="ch")
        result = ocr.ocr(str(source))
    except Exception:
        return None
    lines: list[str] = []
    for page in result or []:
        for item in page or []:
            if item and len(item) > 1 and item[1]:
                lines.append(str(item[1][0]))
    return "\n".join(lines) if lines else None

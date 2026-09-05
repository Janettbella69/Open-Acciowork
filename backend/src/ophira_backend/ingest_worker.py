"""Private, one-request OCR worker executed with Python isolated mode.

Do not import the application or its configuration here: this process receives
only an input copy, local model locations, and bounded extraction settings.
"""

from __future__ import annotations

import json
import math
import re
import resource
import subprocess
import sys
from pathlib import Path
from typing import Any


def _result(text: str = "", backend: str = "unavailable", *warnings: str) -> dict:
    return {"text": text, "backend": backend, "warnings": list(warnings)}


def _assemble(pages: list[str], backend: str, config: dict, warnings: list[str]) -> dict:
    image_pattern = r"!\[[^\]]*\]\([^\n]*?\)|<img\b[^>]*>"
    clean_pages = []
    for page in pages:
        clean, replacements = re.subn(
            image_pattern, "【图像区域未转写，不可推断内容】", page, flags=re.IGNORECASE,
        )
        if replacements:
            warnings.append("figures_not_transcribed")
        clean_pages.append(clean)
    pages = clean_pages
    if not any(page.strip() for page in pages):
        return _result("", backend, *warnings, "missing_pages")
    if any(not page.strip() for page in pages):
        warnings.append("missing_pages")
    text = "\n\n".join(
        f"## 第 {index} 页\n\n{page.strip() or '【本页未提取到文本，不可推断内容】'}"
        for index, page in enumerate(pages, 1)
    )
    if len(text) > config["max_chars"]:
        text = text[:config["max_chars"]]
        warnings.append("truncated")
    return _result(text, backend, *warnings)


def _paddle_pipeline(config: dict) -> tuple[Any | None, list[str]]:
    if not config["paddle_enabled"]:
        return None, ["paddle_not_configured"]
    for key in ("layout_model_dir", "vl_model_dir"):
        directory = config.get(key)
        if not directory or not Path(directory).is_dir() or not any(Path(directory).iterdir()):
            return None, ["paddle_invalid_config"]
    try:
        from paddleocr import PaddleOCRVL  # type: ignore[import-not-found]
    except (ImportError, OSError):
        return None, ["paddle_unavailable"]
    try:
        # Explicitly select the full, local layout + recognition pipeline. Never
        # initialize default model paths (which cause implicit model downloads),
        # external VLM servers, legacy PaddleOCR.ocr(), or optional extra models.
        pipeline = PaddleOCRVL(
            pipeline_version="v1.6",
            layout_detection_model_name="PP-DocLayoutV3",
            layout_detection_model_dir=config["layout_model_dir"],
            vl_rec_model_name="PaddleOCR-VL-1.6-0.9B",
            vl_rec_model_dir=config["vl_model_dir"],
            vl_rec_backend="native",
            use_layout_detection=True,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_ocr_for_image_block=False,
            use_chart_recognition=True,
            use_seal_recognition=True,
            markdown_ignore_labels=[],
            use_queues=False,
            device=config["device"],
        )
        return pipeline, []
    except Exception:
        return None, ["paddle_failed"]


def _paddle_pages(pipeline: Any, source: Path, expected_pages: int, config: dict) -> list[str]:
    pages = []
    # The official iterator avoids retaining every page's image arrays. A page
    # must expose the documented Markdown result, not legacy OCR tuple output.
    for result in pipeline.predict_iter(input=str(source), max_new_tokens=4_096):
        if len(pages) >= expected_pages:
            raise ValueError("unexpected extra OCR page")
        markdown = result.markdown
        if not isinstance(markdown, dict) or not isinstance(markdown.get("markdown_texts"), str):
            raise ValueError("unsupported PaddleOCR-VL result")
        page_index = result.get("page_index")
        if page_index is not None and page_index != len(pages):
            raise ValueError("out-of-order PaddleOCR-VL page")
        pages.append(markdown["markdown_texts"][:config["max_chars"] + 1])
    if len(pages) != expected_pages or not any(page.strip() for page in pages):
        raise ValueError("missing PaddleOCR-VL pages")
    return pages


def _pdf(config: dict) -> dict:
    try:
        from pypdf import PdfReader
    except ImportError:
        return _result("", "unavailable", "unavailable")
    source = Path(config["source"])
    try:
        reader = PdfReader(source)
        if reader.is_encrypted and not reader.decrypt(""):
            return _result("", "unavailable", "encrypted")
        count = len(reader.pages)
        if count > config["max_pages"]:
            return _result("", "unavailable", "page_limit")
        if count == 0:
            return _result("", "unavailable", "invalid_document")
    except Exception:
        return _result("", "unavailable", "invalid_document")
    pipeline, warnings = _paddle_pipeline(config)
    if pipeline is not None:
        try:
            pages = _paddle_pages(pipeline, source, count, config)
            return _assemble(pages, "paddleocr-vl", config, [])
        except Exception:
            warnings.append("paddle_failed")
    pages = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
            pages.append(text[:config["max_chars"] + 1])
        except Exception:
            pages.append("")
    return _assemble(pages, "pypdf", config, [*warnings, "pypdf_text_only"])


def _tesseract(source: Path, config: dict) -> str:
    executable = config.get("tesseract")
    if not executable:
        raise FileNotFoundError("Tesseract unavailable")
    # stdout goes to a bounded regular file, never an unbounded capture pipe.
    output = Path(config["source"]).parent / "tesseract.txt"
    with output.open("wb") as stream:
        result = subprocess.run(
            [executable, str(source), "stdout", "-l", config["tesseract_languages"]],
            stdout=stream, stderr=subprocess.DEVNULL, check=False,
            timeout=config["timeout_seconds"],
        )
    if result.returncode:
        raise ValueError("Tesseract failed")
    with output.open(encoding="utf-8", errors="replace") as stream:
        return stream.read(config["max_chars"] + 1)


def _image(config: dict) -> dict:
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        return _result("", "unavailable", "unavailable")
    source = Path(config["source"])
    # Verify before invoking either decoder/OCR backend, including TIFF/GIF frames.
    try:
        with Image.open(source) as opened:
            count = getattr(opened, "n_frames", 1)
            if count > config["max_pages"]:
                return _result("", "unavailable", "page_limit")
            pixels = 0
            for index in range(count):
                opened.seek(index)
                pixels += opened.width * opened.height
                if pixels > config["max_pixels"]:
                    return _result("", "unavailable", "pixel_limit")
        with Image.open(source) as opened:
            opened.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return _result("", "unavailable", "invalid_document")
    pipeline, warnings = _paddle_pipeline(config)
    paddle_pages: list[str] = []
    fallback_pages: list[str] = []
    with Image.open(source) as opened:
        for index in range(count):
            opened.seek(index)
            frame = source.parent / "frame.png"
            opened.convert("RGB").save(frame)
            if pipeline is not None:
                try:
                    paddle_pages.extend(_paddle_pages(pipeline, frame, 1, config))
                    continue
                except Exception:
                    # Retain no mixed-backend result labeled as VL; rerun all
                    # frames through the fallback after a failed VL page.
                    warnings.append("paddle_failed")
                    pipeline = None
                    break
    if pipeline is not None:
        return _assemble(paddle_pages, "paddleocr-vl", config, [])
    if not config.get("tesseract"):
        return _result("", "unavailable", *warnings, "unavailable")
    with Image.open(source) as opened:
        for index in range(count):
            opened.seek(index)
            frame = source.parent / "frame.png"
            opened.convert("RGB").save(frame)
            try:
                fallback_pages.append(_tesseract(frame, config))
            except (OSError, ValueError, subprocess.TimeoutExpired):
                fallback_pages.append("")
                warnings.append("failed")
    return _assemble(fallback_pages, "tesseract", config, warnings)


def _restrict(config: dict) -> None:
    # Defence in depth, not an OS filesystem/network sandbox. Deployment still
    # needs a restricted service identity/container for hostile binary decoders.
    cpu = max(1, math.ceil(config["timeout_seconds"]))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    file_limit = max(config["max_bytes"], 4_000_000)
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if sys.platform.startswith("linux"):
        memory = config["memory_mb"] * 1_024 * 1_024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))

    def reject_network(event: str, _args: tuple) -> None:
        if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo"}:
            raise PermissionError("OCR worker network access is disabled")

    sys.addaudithook(reject_network)


def main() -> None:
    config = json.loads(sys.stdin.buffer.read(16_384))
    _restrict(config)
    try:
        result = _pdf(config) if config["kind"] == "pdf" else _image(config)
    except Exception:
        result = _result("", "unavailable", "failed")
    Path(config["result_path"]).write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8",
    )


if __name__ == "__main__":
    main()

"""A bounded, read-only reference index derived from registered project artifacts.

The durable sources are the artifact ledger and workspace files. Rebuilding this
view on each request avoids a second corpus with independent retention or stale
cache semantics. A match is reference material, never a verified Evidence record.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from .events import redact
from .models import APIModel, Artifact, utc_now
from .office_reference import (
    OFFICE_SUFFIXES,
    OfficeBudget,
    OfficeBudgetError,
    OfficeInvalidError,
    OfficeLimitError,
    OfficeLocation,
    extract_office,
)
from .skill_proposal_files import read_relative, relative_parts
from .storage import FileLedger, LedgerNotFoundError

MAX_KNOWLEDGE_DOCUMENTS = 250
MAX_KNOWLEDGE_FILE_BYTES = 2 * 1024 * 1024
MAX_KNOWLEDGE_TOTAL_BYTES = 16 * 1024 * 1024
MAX_KNOWLEDGE_QUERY_CHARS = 200
MAX_KNOWLEDGE_RESULTS = 30
MAX_EXCERPT_CHARS = 1400
TEXT_SUFFIXES = frozenset({
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".xml", ".html", ".htm", ".rst",
})
REFERENCE_SUFFIXES = TEXT_SUFFIXES | OFFICE_SUFFIXES


class ReferenceStatus(StrEnum):
    INDEXED = "indexed"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    CHANGED = "changed"
    TOO_LARGE = "too_large"
    INVALID_TEXT = "invalid_text"
    INVALID_DOCUMENT = "invalid_document"
    BUDGET_EXCEEDED = "budget_exceeded"


class ReferenceDocument(APIModel):
    artifact_id: str
    name: str
    path: str
    mime: str
    version: int
    checksum: str
    run_id: str | None
    source_ids: list[str]
    created_at: datetime
    status: ReferenceStatus
    extraction_notice: str | None = None


class ReferenceMatch(APIModel):
    artifact_id: str
    excerpt: str
    start_line: int
    end_line: int
    start_column: int
    excerpt_truncated: bool
    source_location: OfficeLocation | None = None


class KnowledgeSearch(APIModel):
    project_id: str
    query: str
    observed_at: datetime
    documents: list[ReferenceDocument]
    matches: list[ReferenceMatch]
    total_documents: int
    indexed_documents: int
    omitted_documents: int
    matching_documents: int
    bytes_read: int
    office_bytes_reserved: int = 0
    reference_only: bool = True


class KnowledgeQuery(APIModel):
    query: str = Field(default="", max_length=MAX_KNOWLEDGE_QUERY_CHARS)
    limit: int = Field(default=10, ge=1, le=MAX_KNOWLEDGE_RESULTS, strict=True)


class KnowledgeToolQuery(KnowledgeQuery):
    # Leave room for both passage and OCR preamble within the Agent data boundary.
    limit: int = Field(default=10, ge=1, le=10, strict=True)


def capture_reference(
    ledger: FileLedger, project_id: str, artifact_id: str,
) -> tuple[Artifact, bytes]:
    """Capture exact registered bytes; HTTP sends this copy, never a live path."""
    project = ledger.get_project(project_id)
    artifact = next((item for item in project.artifacts if item.id == artifact_id), None)
    if artifact is None:
        raise LedgerNotFoundError(artifact_id)
    parts = relative_parts(artifact.path)
    if (
        any(part.startswith(".") for part in parts)
        or Path(artifact.path).suffix.lower() not in REFERENCE_SUFFIXES
        or artifact.size_bytes > MAX_KNOWLEDGE_FILE_BYTES
    ):
        raise ValueError("reference is outside the supported capture limits")
    workspace = ledger.projects_dir / ledger._safe_id(project_id) / "workspace"
    raw = read_relative(workspace, artifact.path, MAX_KNOWLEDGE_FILE_BYTES)
    if hashlib.sha256(raw).hexdigest() != artifact.checksum:
        raise ValueError("reference no longer matches the registered version")
    return artifact, raw


def _document(artifact: Artifact, status: ReferenceStatus) -> ReferenceDocument:
    return ReferenceDocument(
        artifact_id=artifact.id, name=str(redact(artifact.name)),
        path=str(redact(artifact.path)), mime=artifact.mime, version=artifact.version,
        checksum=artifact.checksum, run_id=artifact.run_id,
        source_ids=artifact.source_ids, created_at=artifact.created_at, status=status,
    )


def _excerpt(text: str, position: int, artifact_id: str) -> ReferenceMatch:
    # Keep real source coordinates even when a single CSV row is very long.
    start = max(text.rfind("\n", 0, position) + 1, position - 240)
    end = min(len(text), start + MAX_EXCERPT_CHARS)
    if end < len(text):
        newline = text.rfind("\n", start, end)
        if newline > position:
            end = newline
    excerpt = str(redact(text[start:end]))
    return ReferenceMatch(
        artifact_id=artifact_id, excerpt=excerpt[:MAX_EXCERPT_CHARS],
        start_line=text.count("\n", 0, start) + 1,
        end_line=text.count("\n", 0, max(start, end - 1)) + 1,
        start_column=start - text.rfind("\n", 0, start),
        excerpt_truncated=start > 0 or end < len(text) or len(excerpt) > MAX_EXCERPT_CHARS,
    )


def search_project_knowledge(
    ledger: FileLedger, project_id: str, *, query: str = "", limit: int = 10,
) -> KnowledgeSearch:
    request = KnowledgeQuery(query=query, limit=limit)
    query = request.query.strip()
    project = ledger.get_project(project_id)
    workspace = ledger.projects_dir / ledger._safe_id(project_id) / "workspace"
    latest: dict[str, Artifact] = {}
    artifacts_by_id = {item.id: item for item in project.artifacts}
    for item in project.artifacts:
        current = latest.get(item.path)
        if current is None or (item.version, item.created_at, item.id) > (
            current.version, current.created_at, current.id,
        ):
            latest[item.path] = item
    selected = sorted(
        latest.values(), key=lambda item: (item.created_at, item.id), reverse=True,
    )[:MAX_KNOWLEDGE_DOCUMENTS]
    terms = list(dict.fromkeys(query.split()))
    if len(terms) > 16:
        raise ValueError("Use at most 16 search terms")
    patterns = [re.compile(re.escape(term), re.IGNORECASE) for term in terms]
    documents: list[ReferenceDocument] = []
    ranked: list[tuple[int, int, ReferenceMatch]] = []
    bytes_read = 0
    # Charge attempted reads up front, even when a source grows or fails capture.
    charged_bytes = 0
    office_budget = OfficeBudget()
    for order, artifact in enumerate(selected):
        state = ReferenceStatus.INDEXED
        try:
            parts = relative_parts(artifact.path)
            if any(part.startswith(".") for part in parts):
                state = ReferenceStatus.UNSUPPORTED
        except ValueError:
            state = ReferenceStatus.UNAVAILABLE
        if state == ReferenceStatus.INDEXED:
            if Path(artifact.path).suffix.lower() not in REFERENCE_SUFFIXES:
                state = ReferenceStatus.UNSUPPORTED
            elif artifact.size_bytes > MAX_KNOWLEDGE_FILE_BYTES:
                state = ReferenceStatus.TOO_LARGE
            elif charged_bytes + artifact.size_bytes + 1 > MAX_KNOWLEDGE_TOTAL_BYTES:
                state = ReferenceStatus.BUDGET_EXCEEDED
        if state != ReferenceStatus.INDEXED:
            documents.append(_document(artifact, state))
            continue
        allowance = min(artifact.size_bytes + 1, MAX_KNOWLEDGE_FILE_BYTES)
        charged_bytes += allowance
        try:
            raw = read_relative(workspace, artifact.path, allowance)
            bytes_read += len(raw)
        except (OSError, ValueError):
            documents.append(_document(artifact, ReferenceStatus.UNAVAILABLE))
            continue
        if hashlib.sha256(raw).hexdigest() != artifact.checksum:
            documents.append(_document(artifact, ReferenceStatus.CHANGED))
            continue
        office = None
        try:
            suffix = Path(artifact.path).suffix.lower()
            if suffix in OFFICE_SUFFIXES:
                office = extract_office(raw, suffix, office_budget)
                text = office.text
            else:
                text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
                if "\x00" in text:
                    raise ValueError("binary data")
        except (OfficeInvalidError, OfficeLimitError, OfficeBudgetError) as exc:
            status = (
                ReferenceStatus.TOO_LARGE if isinstance(exc, OfficeLimitError)
                else ReferenceStatus.BUDGET_EXCEEDED if isinstance(exc, OfficeBudgetError)
                else ReferenceStatus.INVALID_DOCUMENT
            )
            documents.append(_document(artifact, status))
            continue
        except (UnicodeError, ValueError):
            documents.append(_document(artifact, ReferenceStatus.INVALID_TEXT))
            continue
        document = _document(artifact, ReferenceStatus.INDEXED)
        if office is not None:
            document.extraction_notice = office.notice
        elif any(
            source_id in artifacts_by_id
            and artifacts_by_id[source_id].path + ".md" == artifact.path
            for source_id in artifact.source_ids
        ):
            # Preserve the ingestion preamble even if the hit is deep in a file.
            # It is a bounded source excerpt, not a new host quality attestation.
            document.extraction_notice = str(redact(text[:1400]))[:1400]
        documents.append(document)
        if not patterns:
            continue
        positions: list[int] = []
        title_hits = 0
        for pattern in patterns:
            title_hit = pattern.search(artifact.path)
            body_hit = pattern.search(text)
            if not title_hit and not body_hit:
                break
            title_hits += int(title_hit is not None)
            if body_hit:
                positions.append(body_hit.start())
        else:
            position = min(positions, default=0)
            match = _excerpt(text, position, artifact.id)
            if office is not None and office.blocks:
                block = next(item for item in office.blocks
                             if item.start <= position < item.start + len(item.text))
                # Never label a multi-cell/slide excerpt with one cell/slide locator.
                local_position = position - block.start
                match = _excerpt(
                    block.text, local_position if len(block.text) > MAX_EXCERPT_CHARS else 0,
                    artifact.id,
                )
                match.source_location = OfficeLocation.model_validate(
                    redact(block.location.model_dump())
                )
                match.excerpt_truncated |= len(office.blocks) > 1
            ranked.append((-title_hits, order, match))
    ranked.sort(key=lambda item: item[:2])
    return KnowledgeSearch(
        project_id=project_id, query=str(redact(query)), observed_at=utc_now(),
        documents=documents, matches=[item[2] for item in ranked[:request.limit]],
        total_documents=len(latest), indexed_documents=sum(
            item.status == ReferenceStatus.INDEXED for item in documents
        ), omitted_documents=len(latest) - len(selected),
        matching_documents=len(ranked), bytes_read=bytes_read,
        office_bytes_reserved=office_budget.reserved_bytes,
    )

"""Bounded text extraction from registered OOXML references, without file writes."""

from __future__ import annotations

import io
import posixpath
import re
import stat
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

OFFICE_SUFFIXES = frozenset({".docx", ".xlsx", ".pptx"})
MAX_OFFICE_PARTS = 512
MAX_OFFICE_EXPANDED_BYTES = 16 * 1024 * 1024
MAX_OFFICE_PART_BYTES = 8 * 1024 * 1024
MAX_OFFICE_LINES = 20_000
MAX_OFFICE_TEXT_CHARS = 300_000
MAX_OFFICE_XML_ELEMENTS = 200_000
MAX_OFFICE_XML_DEPTH = 128

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"
CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"


class InvalidOfficeDocument(ValueError):
    """The package cannot be interpreted as the advertised Office format."""


class OfficeExtractionLimit(ValueError):
    """The source has more content than the bounded index can inspect."""


@dataclass(frozen=True)
class OfficePassage:
    text: str
    location: str


class _Passages:
    def __init__(self) -> None:
        self.lines: list[OfficePassage] = []
        self.characters = 0

    def add(self, text: str, location: str) -> None:
        text = " ".join(text.split())
        if not text:
            return
        self.characters += len(text)
        if len(self.lines) >= MAX_OFFICE_LINES or self.characters > MAX_OFFICE_TEXT_CHARS:
            raise OfficeExtractionLimit("Office reference exceeds extracted text limits")
        self.lines.append(OfficePassage(text, location))


def _parts(payload: bytes) -> dict[str, bytes]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise InvalidOfficeDocument("Office reference is not a ZIP package") from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_OFFICE_PARTS:
            raise OfficeExtractionLimit("Office reference has too many package parts")
        if sum(item.file_size for item in entries) > MAX_OFFICE_EXPANDED_BYTES:
            raise OfficeExtractionLimit("Office reference expands beyond its byte limit")
        parts: dict[str, bytes] = {}
        seen = set()
        total = 0
        for item in entries:
            name = item.filename
            segments = name.rstrip("/").split("/")
            mode = stat.S_IFMT(item.external_attr >> 16)
            if (
                not name or len(name) > 300 or name.startswith("/")
                or "\\" in name or "\x00" in name
                or any(segment in {"", ".", ".."} for segment in segments)
                or ":" in segments[0] or item.flag_bits & 1
                or (mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)))
            ):
                raise InvalidOfficeDocument("Office reference has an unsafe package part")
            folded = name.rstrip("/").casefold()
            if folded in seen:
                raise InvalidOfficeDocument("Office reference has duplicate package parts")
            seen.add(folded)
            if item.is_dir():
                continue
            if item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise InvalidOfficeDocument("Office package uses unsupported compression")
            if item.file_size > MAX_OFFICE_PART_BYTES:
                raise OfficeExtractionLimit("Office reference has an oversized package part")
            try:
                with archive.open(item) as source:
                    raw = source.read(MAX_OFFICE_PART_BYTES + 1)
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
                raise InvalidOfficeDocument("Office package part could not be read") from exc
            total += len(raw)
            if (
                len(raw) != item.file_size or len(raw) > MAX_OFFICE_PART_BYTES
                or total > MAX_OFFICE_EXPANDED_BYTES
            ):
                raise OfficeExtractionLimit("Office reference exceeded its expanded byte limit")
            if name.endswith((".xml", ".rels")):
                if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                    raise InvalidOfficeDocument("Office XML contains entity declarations")
                parts[name] = raw
    if "[Content_Types].xml" not in parts:
        raise InvalidOfficeDocument("Office package is missing its content types")
    return parts


def _xml(parts: dict[str, bytes], path: str, root_tag: str) -> ET.Element:
    raw = parts.get(path)
    if raw is None:
        raise InvalidOfficeDocument("Office package is missing a required part")
    try:
        parser = ET.XMLPullParser(events=("start", "end"))
        root = None
        depth = 0
        elements = 0
        for start in range(0, len(raw), 64 * 1024):
            parser.feed(raw[start:start + 64 * 1024])
            for event, element in parser.read_events():
                if event == "start":
                    root = element if root is None else root
                    depth += 1
                    elements += 1
                    if depth > MAX_OFFICE_XML_DEPTH or elements > MAX_OFFICE_XML_ELEMENTS:
                        raise OfficeExtractionLimit("Office XML exceeds structural limits")
                else:
                    depth -= 1
        parser.close()
    except ET.ParseError as exc:
        raise InvalidOfficeDocument("Office package contains invalid XML") from exc
    if root is None or depth != 0 or root.tag != root_tag:
        raise InvalidOfficeDocument("Office package part has an unexpected root")
    return root


def _optional_xml(parts: dict[str, bytes], path: str, root_tag: str) -> ET.Element | None:
    return _xml(parts, path, root_tag) if path in parts else None


def _relationships(parts: dict[str, bytes], path: str, base: str) -> dict[str, tuple[str, str]]:
    root = _optional_xml(parts, path, PR + "Relationships")
    if root is None:
        return {}
    result = {}
    for item in root.findall(PR + "Relationship"):
        identifier = item.get("Id")
        target = item.get("Target", "")
        if not identifier or identifier in result:
            raise InvalidOfficeDocument("Office relationships are ambiguous")
        if item.get("TargetMode") == "External":
            continue
        if not target or "\\" in target or "?" in target or "#" in target:
            raise InvalidOfficeDocument("Office relationship target is invalid")
        resolved = posixpath.normpath(
            target.lstrip("/") if target.startswith("/")
            else posixpath.join(base, target)
        )
        if (
            resolved.startswith("../") or resolved in {".", ".."}
            or any(part in {"", ".", ".."} for part in PurePosixPath(resolved).parts)
        ):
            raise InvalidOfficeDocument("Office relationship leaves the package")
        result[identifier] = (item.get("Type", ""), resolved)
    return result


def _word_text(element: ET.Element) -> str:
    return "".join(item.text or "" for item in element.iter(W + "t"))


def _word_paragraphs(root: ET.Element, label: str, out: _Passages) -> None:
    parents = {child: parent for parent in root.iter() for child in parent}
    cell_locations = {}
    for table_number, table in enumerate(root.iter(W + "tbl"), start=1):
        for row_number, row in enumerate(table.findall(W + "tr"), start=1):
            for cell_number, cell in enumerate(row.findall(W + "tc"), start=1):
                cell_locations[cell] = (
                    f"{label} table {table_number} row {row_number} cell {cell_number}"
                )
    for paragraph_number, paragraph in enumerate(root.iter(W + "p"), start=1):
        parent = parents.get(paragraph)
        location = f"{label} paragraph {paragraph_number}"
        while parent is not None:
            if parent in cell_locations:
                location = cell_locations[parent]
                break
            parent = parents.get(parent)
        out.add(_word_text(paragraph), location)


def _docx(parts: dict[str, bytes], out: _Passages) -> str:
    document = _xml(parts, "word/document.xml", W + "document")
    body = document.find(W + "body")
    if body is None:
        raise InvalidOfficeDocument("Word document has no body")
    _word_paragraphs(body, "Body", out)
    for name in sorted(parts):
        match = re.fullmatch(r"word/(header|footer)([0-9]+)\.xml", name)
        if match:
            root = _xml(parts, name, W + ("hdr" if match.group(1) == "header" else "ftr"))
            _word_paragraphs(root, f"{match.group(1).title()} {match.group(2)}", out)
    for kind in ("footnote", "endnote"):
        root = _optional_xml(parts, f"word/{kind}s.xml", W + kind + "s")
        if root is not None:
            for entry in root.findall(W + kind):
                identifier = entry.get(W + "id", "")
                if identifier.isdigit():
                    _word_paragraphs(entry, f"{kind.title()} {identifier}", out)
    return (
        "Word paragraphs, tables, headers, footers, footnotes and endnotes are indexed; "
        "images and layout are not read."
    )


def _shared_strings(parts: dict[str, bytes]) -> list[str]:
    root = _optional_xml(parts, "xl/sharedStrings.xml", S + "sst")
    if root is None:
        return []
    return ["".join(item.text or "" for item in entry.iter(S + "t"))
            for entry in root.findall(S + "si")]


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.get("t")
    value = cell.findtext(S + "v") or ""
    if kind == "s":
        try:
            index = int(value)
            if index < 0:
                raise ValueError("negative shared string index")
            value = shared[index]
        except (IndexError, ValueError) as exc:
            raise InvalidOfficeDocument("Spreadsheet shared string index is invalid") from exc
    elif kind == "inlineStr":
        inline = cell.find(S + "is")
        value = (
            "".join(item.text or "" for item in inline.iter(S + "t"))
            if inline is not None else ""
        )
    elif kind == "b":
        value = "true" if value == "1" else "false" if value == "0" else value
    formula = cell.findtext(S + "f")
    return (f"={formula} {value}" if formula and value else f"={formula}" if formula else value)


def _xlsx(parts: dict[str, bytes], out: _Passages) -> str:
    workbook = _xml(parts, "xl/workbook.xml", S + "workbook")
    relationships = _relationships(parts, "xl/_rels/workbook.xml.rels", "xl")
    shared = _shared_strings(parts)
    sheets = workbook.find(S + "sheets")
    if sheets is None or not len(sheets):
        raise InvalidOfficeDocument("Spreadsheet has no worksheets")
    found = False
    for sheet in sheets.findall(S + "sheet"):
        relation = relationships.get(sheet.get(R + "id", ""))
        if relation is None or not relation[0].endswith("/worksheet"):
            continue
        name = sheet.get("name", "")
        if not name or len(name) > 31:
            raise InvalidOfficeDocument("Spreadsheet sheet name is invalid")
        worksheet = _xml(parts, relation[1], S + "worksheet")
        found = True
        for cell in worksheet.iter(S + "c"):
            coordinate = cell.get("r", "")
            if not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", coordinate):
                raise InvalidOfficeDocument("Spreadsheet cell coordinate is invalid")
            out.add(_cell_text(cell, shared), f"{name}!{coordinate}")
    if not found:
        raise InvalidOfficeDocument("Spreadsheet worksheet parts are missing")
    return (
        "Cell text, stored values and formula expressions are indexed; "
        "formulas are not recalculated. Images and charts are not read."
    )


def _drawing_text(paragraph: ET.Element) -> str:
    return "".join(item.text or "" for item in paragraph.iter(A + "t"))


def _pptx(parts: dict[str, bytes], out: _Passages) -> str:
    presentation = _xml(parts, "ppt/presentation.xml", P + "presentation")
    relationships = _relationships(parts, "ppt/_rels/presentation.xml.rels", "ppt")
    slide_ids = presentation.find(P + "sldIdLst")
    if slide_ids is None:
        raise InvalidOfficeDocument("Presentation has no slide list")
    for number, slide_id in enumerate(slide_ids.findall(P + "sldId"), start=1):
        relation = relationships.get(slide_id.get(R + "id", ""))
        if relation is None or not relation[0].endswith("/slide"):
            raise InvalidOfficeDocument("Presentation slide relationship is missing")
        path = relation[1]
        slide = _xml(parts, path, P + "sld")
        for paragraph_number, paragraph in enumerate(slide.iter(A + "p"), start=1):
            out.add(_drawing_text(paragraph), f"Slide {number} paragraph {paragraph_number}")
        rel_path = posixpath.join(
            posixpath.dirname(path), "_rels", posixpath.basename(path) + ".rels",
        )
        slide_rels = _relationships(parts, rel_path, posixpath.dirname(path))
        for kind, target in slide_rels.values():
            if kind.endswith("/notesSlide"):
                notes = _xml(parts, target, P + "notes")
                for shape in notes.iter(P + "sp"):
                    placeholder = shape.find(f"{P}nvSpPr/{P}nvPr/{P}ph")
                    if placeholder is not None and placeholder.get("type") == "body":
                        for note_number, paragraph in enumerate(shape.iter(A + "p"), start=1):
                            out.add(
                                _drawing_text(paragraph),
                                f"Slide {number} notes paragraph {note_number}",
                            )
            elif kind.endswith("/chart"):
                chart = _xml(parts, target, C + "chartSpace")
                for chart_number, value in enumerate(chart.iter(C + "v"), start=1):
                    out.add(value.text or "", f"Slide {number} chart item {chart_number}")
    return "Slide, speaker-note and cached chart text are indexed; images and layout are not read."


def extract_office_passages(payload: bytes, suffix: str) -> tuple[list[OfficePassage], str]:
    if suffix not in OFFICE_SUFFIXES:
        raise ValueError("Unsupported Office reference type")
    parts = _parts(payload)
    _xml(parts, "[Content_Types].xml", CT + "Types")
    out = _Passages()
    if suffix == ".docx":
        notice = _docx(parts, out)
    elif suffix == ".xlsx":
        notice = _xlsx(parts, out)
    else:
        notice = _pptx(parts, out)
    return out.lines, notice

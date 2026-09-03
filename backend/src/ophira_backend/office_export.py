"""Validated Office writers used by the ``office-documents`` Skill (FR-16).

The writers deliberately use the native Office libraries instead of hand-built
partial OOXML packages. Text from users is always stored as text; only formulas
created by this module are written as formulas.
"""

from __future__ import annotations

import argparse
import re
from datetime import date, datetime
from decimal import Decimal
from math import ceil
from pathlib import Path
from typing import TypeAlias
from unicodedata import east_asian_width

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches as DocxInches
from docx.shared import Pt as DocxPt
from openpyxl import Workbook
from openpyxl.cell.cell import Cell
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

CellValue: TypeAlias = str | int | float | bool | Decimal | date | datetime | None

_NUMBER = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")
_NAVY = "17233C"
_TEAL = "087E8B"
_PALE = "E9F4F5"
_CORE_PROPERTY_MAX_LENGTH = 255
_XLSX_MAX_COLUMNS = 16_384
_XLSX_MAX_ROWS = 1_048_576
_XLSX_MAX_TEXT_LENGTH = 32_767
_PPTX_MAX_ITEM_LENGTH = 32_767
_PPTX_LINE_WIDTH = 78
_PPTX_MAX_CARD_LINES = 12
_PPTX_MAX_CARDS = 5
_PPTX_CONTENT_HEIGHT = 5.15
_PPTX_CARD_GAP = 0.18


def _validate_title(title: str) -> None:
    if len(title) > _CORE_PROPERTY_MAX_LENGTH:
        raise ValueError("title cannot exceed 255 characters")


def _validate_xlsx_text(value: str, *, location: str) -> None:
    if len(value) > _XLSX_MAX_TEXT_LENGTH:
        raise ValueError(f"{location} cannot exceed 32,767 characters")


def _typed_value(value: CellValue) -> CellValue:
    if isinstance(value, Decimal):
        return float(value)
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not _NUMBER.fullmatch(stripped):
        return value
    unsigned = stripped.removeprefix("-")
    integer = unsigned.partition(".")[0]
    if len(integer) > 1 and integer.startswith("0"):
        return value
    # Excel only preserves 15 significant decimal digits. Keep larger numeric-looking
    # identifiers as text rather than coercing them into a lossy number (or overflowing
    # openpyxl's float serialization for very long digit strings).
    significant = unsigned.replace(".", "").lstrip("0") or "0"
    if len(significant) > 15:
        return value
    return float(stripped) if "." in stripped else int(stripped)


def _write_text_cell(cell: Cell, value: str) -> None:
    """Prevent user-controlled leading '=' text from becoming an Excel formula."""
    cell.value = value
    cell.data_type = "s"


def write_xlsx(path: Path, headers: list[str], rows: list[list[CellValue]]) -> None:
    if not headers:
        raise ValueError("xlsx export requires at least one header")
    if len(headers) > _XLSX_MAX_COLUMNS:
        raise ValueError("xlsx export cannot exceed 16,384 columns")
    if any(len(row) > len(headers) for row in rows):
        raise ValueError("xlsx rows cannot contain more cells than headers")
    # A non-empty export reserves one row for the header and one for totals.
    if len(rows) > _XLSX_MAX_ROWS - 2:
        raise ValueError("xlsx export cannot exceed 1,048,574 data rows")
    for column, header in enumerate(headers, start=1):
        _validate_xlsx_text(str(header), location=f"xlsx header {column}")
    for row_index, row in enumerate(rows, start=2):
        for column, value in enumerate(row, start=1):
            if isinstance(value, str):
                _validate_xlsx_text(value, location=f"xlsx cell {row_index},{column}")

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "A2"

    for column, header in enumerate(headers, start=1):
        cell = sheet.cell(row=1, column=column)
        _write_text_cell(cell, str(header))
        cell.font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=_NAVY)
        cell.alignment = Alignment(vertical="center")
    sheet.row_dimensions[1].height = 24

    for row_index, row in enumerate(rows, start=2):
        for column, raw_value in enumerate(row, start=1):
            cell = sheet.cell(row=row_index, column=column)
            value = _typed_value(raw_value)
            if isinstance(value, str):
                _write_text_cell(cell, value)
                cell.alignment = Alignment(vertical="center", wrap_text=True)
            else:
                cell.value = value
                cell.alignment = Alignment(horizontal="right", vertical="center")
                if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                    cell.number_format = "#,##0.00" if isinstance(value, float) else "#,##0"

    if rows:
        total_row = len(rows) + 2
        total_column = len(headers)
        if total_column > 1:
            label = sheet.cell(row=total_row, column=total_column - 1)
            _write_text_cell(label, "Total")
            label.font = Font(name="Arial", size=10, bold=True, color=_NAVY)
            label.alignment = Alignment(horizontal="right")
        total = sheet.cell(row=total_row, column=total_column)
        total.value = (
            f"=SUM({get_column_letter(total_column)}2:"
            f"{get_column_letter(total_column)}{total_row - 1})"
        )
        total.font = Font(name="Arial", size=10, bold=True, color=_NAVY)
        total.fill = PatternFill("solid", fgColor=_PALE)
        total.number_format = "#,##0.00"
        total.border = Border(top=Side(style="thin", color=_TEAL))

    sheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"
    for column, header in enumerate(headers, start=1):
        values = [str(header), *(str(row[column - 1]) for row in rows if column <= len(row))]
        sheet.column_dimensions[get_column_letter(column)].width = min(
            max(12, max(len(value) for value in values) + 2),
            42,
        )
    workbook.calculation.fullCalcOnLoad = True
    workbook.calculation.forceFullCalc = True
    workbook.properties.creator = "OPHIRA"
    workbook.properties.title = "OPHIRA export"
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def _docx_rgb(value: str):
    from docx.shared import RGBColor as DocxRGBColor

    return DocxRGBColor.from_string(value)


def write_docx(path: Path, title: str, paragraphs: list[str]) -> None:
    _validate_title(title)
    document = Document()
    section = document.sections[0]
    section.start_type = WD_SECTION.NEW_PAGE
    section.page_width = DocxInches(8.5)
    section.page_height = DocxInches(11)
    section.top_margin = DocxInches(0.85)
    section.bottom_margin = DocxInches(0.85)
    section.left_margin = DocxInches(0.9)
    section.right_margin = DocxInches(0.9)

    normal = document.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = DocxPt(10.5)
    normal.paragraph_format.space_after = DocxPt(7)
    normal.paragraph_format.line_spacing = 1.15

    heading = document.add_paragraph(style="Title")
    heading.alignment = WD_ALIGN_PARAGRAPH.LEFT
    heading.paragraph_format.space_after = DocxPt(16)
    run = heading.add_run(title)
    run.bold = True
    run.font.name = "Arial"
    run.font.size = DocxPt(25)
    run.font.color.rgb = _docx_rgb(_NAVY)

    for text in paragraphs:
        document.add_paragraph(str(text))

    document.core_properties.author = "OPHIRA"
    document.core_properties.title = title
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)


def _set_slide_background(slide, color: str) -> None:
    background = slide.background.fill
    background.solid()
    background.fore_color.rgb = RGBColor.from_string(color)


def _add_slide_title(slide, text: str, *, page: int | None = None) -> None:
    box = slide.shapes.add_textbox(Inches(0.72), Inches(0.52), Inches(11.0), Inches(0.8))
    frame = box.text_frame
    frame.clear()
    frame.word_wrap = True
    paragraph = frame.paragraphs[0]
    paragraph.text = text
    paragraph.font.name = "Aptos Display"
    title_width = _display_width(text)
    paragraph.font.size = Pt(30 if title_width <= 70 else 22 if title_width <= 140 else 16)
    paragraph.font.bold = True
    paragraph.font.color.rgb = RGBColor.from_string(_NAVY)
    if page is not None:
        marker = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE,
            Inches(11.9),
            Inches(0.57),
            Inches(0.7),
            Inches(0.38),
        )
        marker.fill.solid()
        marker.fill.fore_color.rgb = RGBColor.from_string(_TEAL)
        marker.line.fill.background()
        marker.text_frame.clear()
        marker.text_frame.paragraphs[0].text = str(page)
        marker.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER
        marker.text_frame.paragraphs[0].font.size = Pt(12)
        marker.text_frame.paragraphs[0].font.bold = True
        marker.text_frame.paragraphs[0].font.color.rgb = RGBColor(255, 255, 255)


def _character_width(character: str) -> int:
    return 2 if east_asian_width(character) in {"F", "W"} else 1


def _display_width(text: str) -> int:
    return max(
        (sum(_character_width(character) for character in line) for line in text.split("\n")),
        default=0,
    )


def _estimated_lines(text: str) -> int:
    return sum(
        max(1, ceil(sum(_character_width(character) for character in line) / _PPTX_LINE_WIDTH))
        for line in text.split("\n")
    )


def _split_pptx_text(text: str) -> list[str]:
    """Split oversized card text without changing or dropping user content."""
    if not text:
        return [""]

    chunks: list[str] = []
    start = 0
    index = 0
    lines = 1
    line_width = 0
    while index < len(text):
        character = text[index]
        if character == "\n":
            next_lines = lines + 1
            next_width = 0
        else:
            width = _character_width(character)
            wrapped = line_width > 0 and line_width + width > _PPTX_LINE_WIDTH
            next_lines = lines + int(wrapped)
            next_width = width if wrapped else line_width + width

        if next_lines > _PPTX_MAX_CARD_LINES and index > start:
            chunks.append(text[start:index])
            start = index
            lines = 1
            line_width = 0
            continue

        lines = next_lines
        line_width = next_width
        index += 1

    chunks.append(text[start:])
    return chunks


def _pptx_pages(bullets: list[str]) -> list[list[tuple[int, str]]]:
    cards = [
        (number, fragment)
        for number, item in enumerate(bullets, start=1)
        for fragment in _split_pptx_text(str(item))
    ]
    if not cards:
        return [[]]

    pages: list[list[tuple[int, str]]] = []
    page: list[tuple[int, str]] = []
    used_height = 0.0
    for card in cards:
        height = max(0.88, 0.48 + 0.29 * _estimated_lines(card[1]))
        required_height = height + (_PPTX_CARD_GAP if page else 0)
        if page and (
            len(page) >= _PPTX_MAX_CARDS
            or used_height + required_height > _PPTX_CONTENT_HEIGHT
        ):
            pages.append(page)
            page = []
            used_height = 0.0
            required_height = height
        page.append(card)
        used_height += required_height
    pages.append(page)
    return pages


def write_pptx(path: Path, title: str, bullets: list[str]) -> None:
    _validate_title(title)
    for index, item in enumerate(bullets, start=1):
        if len(str(item)) > _PPTX_MAX_ITEM_LENGTH:
            raise ValueError(f"pptx item {index} cannot exceed 32,767 characters")

    presentation = Presentation()
    presentation.slide_width = Inches(13.333)
    presentation.slide_height = Inches(7.5)
    presentation.core_properties.author = "OPHIRA"
    presentation.core_properties.title = title
    blank = presentation.slide_layouts[6]

    pages = _pptx_pages(bullets)
    for slide_index, page in enumerate(pages, start=1):
        slide = presentation.slides.add_slide(blank)
        _set_slide_background(slide, "F7FAFC")
        _add_slide_title(
            slide,
            title if len(pages) == 1 else f"{title} · {slide_index}",
            page=slide_index if len(pages) > 1 else None,
        )
        if not page:
            continue

        top = 1.45
        for number_value, item in page:
            card_height = max(0.88, 0.48 + 0.29 * _estimated_lines(item))
            number = slide.shapes.add_shape(
                MSO_SHAPE.OVAL,
                Inches(0.82),
                Inches(top + 0.08),
                Inches(0.55),
                Inches(0.55),
            )
            number.fill.solid()
            number.fill.fore_color.rgb = RGBColor.from_string(_TEAL)
            number.line.fill.background()
            number.text_frame.clear()
            number.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
            number.text_frame.paragraphs[0].text = str(number_value)
            number.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER
            number.text_frame.paragraphs[0].font.size = Pt(13)
            number.text_frame.paragraphs[0].font.bold = True
            number.text_frame.paragraphs[0].font.color.rgb = RGBColor(255, 255, 255)

            card = slide.shapes.add_shape(
                MSO_SHAPE.ROUNDED_RECTANGLE,
                Inches(1.55),
                Inches(top),
                Inches(10.75),
                Inches(card_height),
            )
            card.fill.solid()
            card.fill.fore_color.rgb = RGBColor(255, 255, 255)
            card.line.color.rgb = RGBColor.from_string("D9E2EC")
            card.text_frame.clear()
            card.text_frame.margin_left = Inches(0.22)
            card.text_frame.margin_right = Inches(0.22)
            card.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
            card.text_frame.word_wrap = True
            paragraph = card.text_frame.paragraphs[0]
            paragraph.text = str(item)
            paragraph.font.name = "Aptos"
            paragraph.font.size = Pt(17)
            paragraph.font.color.rgb = RGBColor.from_string(_NAVY)
            top += card_height + _PPTX_CARD_GAP

    path.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Write a local office artifact")
    parser.add_argument("kind", choices=["xlsx", "docx", "pptx"])
    parser.add_argument("output")
    parser.add_argument("--title", default="OPHIRA export")
    parser.add_argument("--text", action="append", default=[])
    args = parser.parse_args()
    target = Path(args.output)
    if args.kind == "xlsx":
        headers = ["item", "value"]
        rows = [line.split(",", 1) if "," in line else [line, ""] for line in args.text]
        write_xlsx(target, headers, rows)
    elif args.kind == "docx":
        write_docx(target, args.title, args.text)
    else:
        write_pptx(target, args.title, args.text)


if __name__ == "__main__":
    main()

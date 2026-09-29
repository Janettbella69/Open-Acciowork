# Office export acceptance

FR-16 and PRD P0-5 require usable DOCX/XLSX/PPTX files, including native rendering
and visual inspection. `office_export.py` remains the existing local writer used
by the Office Skill. It does not start a new model runtime or render untrusted
uploads in the API process.

## Export behavior

- DOCX preserves the title and paragraphs, including Chinese text. The Title
  style has no inherited decorative border.
- XLSX preserves typed numbers, identifiers and literal formula-like user text.
  An automatic last-column SUM is created only when its nonempty values are all
  numeric. Headers wrap, widths account for wide characters, and printed pages
  repeat the header row and first column. The print area includes the total.
- PPTX reserves space for the complete title before calculating body capacity.
  Body text is explicitly left aligned. Continuations prefer paragraph breaks,
  then whitespace; an unbroken oversized token may still require a character
  split. Paragraph contents are preserved across fragments. Page numbers use
  the separate marker, leaving the title unchanged. Titles over 255 characters
  or with too many explicit line breaks fail clearly before saving.

## Reproduce

Install the backend development dependencies, LibreOffice, Poppler (`pdftoppm`
and `pdftotext`) and fonts covering the input languages. From the repository root:

```sh
PYTHONPATH=backend/src .venv/bin/python -m pytest backend/tests/test_office_export.py
PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_office_acceptance.py \
  --output /tmp/ophira-office-acceptance
```

The output directory must be new. Executables can be selected with `--soffice`,
`--pdftoppm` and `--pdftotext`. If an isolated renderer cannot discover installed
fonts, pass repeatable `--font-dir` paths, such as `/System/Library/Fonts` on macOS.
The script writes an isolated [Fontconfig configuration](https://fontconfig.pages.freedesktop.org/fontconfig/fontconfig-user.html),
without changing the system font installation or runtime dependencies. Missing
Chinese glyphs were reproduced with the bundled renderer's default configuration;
the input-text checks correctly failed instead of accepting blank output.

The script generates synthetic fixtures using production writers, registers
Run-linked immutable artifacts in a temporary ledger, changes their workspace
files, then verifies the original bytes through the ASGI download API. The
downloaded originals are rendered by real LibreOffice to PDF and Poppler to PNG.
It checks rendered input text, each PPT text box's bounds, and a recalculated XLSX
copy retaining `=SUM(C2:C31)` with cached result `465`.

The generated report always says visual review is pending. Inspect every PNG
and record the review separately; text extraction can see text hidden under
another object. The script's rendering checks are not a pixel-diff gold baseline.

## 2026-09-29 evidence and limits

[`evals/reports/office-20260929/`](../evals/reports/office-20260929/README.md)
retains nine downloaded Office files, their hashes, nine PDFs and 22 page PNGs.
All native checks passed. Every page was visually inspected; the ten slides
also received independent review and the long-paragraph pages were rechecked
after correcting cross-page word splits. The two DOCX files were additionally
rendered through the documents skill's `render_docx.py` and all four pages reviewed.

This proves the basic writer and immutable-download path for these fixtures on
the recorded macOS LibreOffice build. It does not prove live model authorship,
continuous model edits, full frontend/browser acceptance, Microsoft Office/WPS
compatibility, arbitrary customer layouts, charts/images or production rendering
isolation. Font substitution can change appearance and pagination. Small
continuation-card inset differences and natural short final pages remain visible
but do not obscure content in these samples. FR-16 retains those wider acceptance
requirements in the delivery inventory.

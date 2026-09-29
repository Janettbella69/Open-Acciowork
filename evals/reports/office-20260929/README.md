# Native Office export acceptance 2026-09-29

All data is synthetic. No customer files, account credentials or model services
were used. Reproduction commands and scope: [Office acceptance](../../../docs/office-export.md).

`report.json` records the automated checks and original downloaded SHA-256 hashes.
`downloaded/` contains the production-generated, immutable API downloads. Each
named case directory retains its PDF and every rendered page PNG.

| Fixture | Pages | Verified behavior |
|---|---:|---|
| brief.docx | 1 | Mixed Chinese/English title and paragraphs |
| long-brief.docx | 3 | All 16 long records, including final END16 |
| findings.pptx | 5 | All 13 findings; balanced final four cards |
| long-finding.pptx | 4 | Paragraph continuations 01–04, 05–08, 09–12, 13–14 |
| long-title.pptx | 1 | Complete 254-character Chinese title clear of body |
| quantities.xlsx | 2 | 30 rows, repeated headers, formula recalculation to 465 |
| wide.xlsx | 4 | All 12 columns with repeated row identifier |
| text.xlsx | 1 | Literal formula-like input, no false numeric total |
| long-headers.xlsx | 1 | Wrapped Chinese header without clipping |

Visual review completed separately from the automated report: all 22 PNGs
were inspected. The PowerPoint samples had an independent reviewer; two instances
of a word split across pages were found, fixed, then all four continuation pages
were re-rendered and independently rechecked. Every final PNG was byte-compared
with the corresponding reviewed render and matched. Word's four pages were also
inspected after a separate canonical `render_docx.py` pass.

No observed content clipping or title/body overlap remains in this bundle.
The short final Word page, wide spreadsheet print pagination, and small final
PowerPoint card inset differences are recorded layout characteristics, not
claims of universal Office compatibility. Native Microsoft Office/WPS, other
font installations, live Agent authoring and customer-designed layouts remain
outside this acceptance.

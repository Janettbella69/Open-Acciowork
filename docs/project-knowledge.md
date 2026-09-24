# Project reference knowledge

PRD §3.7 P2 calls for a project reference index serving retrieval alongside the
evidence ledger. Open **Project knowledge** inside a saved workspace to inspect
file coverage, search passages, inspect provenance and download the exact source.
Files attached through the existing project upload flow and registered Agent
artifacts enter this view automatically.

## Source and search contract

- The artifact ledger plus workspace files remain authoritative. Each read builds
  a bounded reference index from those durable sources; there is no second corpus,
  vector service, model call, hidden conversation capture or independent cache to
  retain deleted text. Restarting the application requires no index migration.
- Only the latest registered version for each path is selected, newest first.
  Unregistered files, hidden paths, symlinks, hard links and special files are not
  read. Every path component is opened through the existing descriptor-based
  capture helper; files changing during capture are rejected.
- Supported sources are UTF-8 text, Markdown, CSV/TSV, JSON/JSONL, YAML, XML, HTML
  and RST, plus registered DOCX/XLSX/PPTX originals. Word paragraphs, tables,
  headers, footers, footnotes and endnotes; spreadsheet cells and formula
  expressions; and presentation slides, speaker notes and cached chart text
  are indexed.
  PDF/image originals remain available in the existing artifact area;
  their registered OCR Markdown companions are searchable, with original artifact
  ids and a bounded copy of the ingestion preamble alongside matching passages.
  OCR notices remain source text, not an assurance of extraction quality.
- Search terms are literal and case insensitive; all whitespace-separated terms
  must appear somewhere in the file or path. Chinese substrings work without a
  remote tokenizer. Filename matches rank first, then newest files. Each matching
  document returns one excerpt, including its first body match when present.
  This is lexical retrieval; it does not infer synonyms or semantic relevance.
- Every result identifies the artifact id, path, version, SHA-256, registration
  time, originating Run and source record ids. Text line/column coordinates are
  one-based Unicode positions after BOM and newline normalization. Office hits
  additionally show their logical source location, such as a Word paragraph or
  table cell, an Excel sheet and cell, or a PowerPoint slide/notes/chart item.
  The source location refers to extracted text, not a page coordinate.
  Displayed excerpts are redacted and bounded, so downloads are the authority
  for exact original text.
- A file whose current bytes disagree with its registered checksum is excluded.
  Missing, changed, unsupported, undecodable and over-limit sources have explicit
  coverage states. A failed search never establishes that the project has no such
  information. Refresh recalculates the view, and artifact revision changes also
  refresh an open panel.

Limits: latest 250 paths, 2 MiB per file, 16 MiB total read allowance, 200 query
characters, 16 terms and 1,400 characters per excerpt. Failed read attempts consume
their reserved allowance too. HTTP returns up to 30 matches; MCP up to 10 to retain
space for provenance and OCR notices. Matching-file totals and omitted-file counts
are separate from returned-result counts. Office packages are additionally limited
to 512 parts, 16 MiB expanded, 8 MiB per part, 20,000 extracted lines and
300,000 text characters; XML parsing is capped at 200,000 elements and depth 128.
Unsafe ZIP paths/links, XML entity declarations and invalid parts are rejected.
Over-limit and unreadable Office files have separate coverage states; no partial
extraction is advertised as complete. Images and layout are not OCRed, formulas
are not recalculated, and external links are not
followed. Larger corpora and real-world retrieval quality remain open.

## Interfaces and authorization

`GET /api/projects/{project_id}/knowledge?q=...&limit=...` returns file coverage and
passage matches. Empty queries inspect the index without returning passages.
`GET /api/projects/{project_id}/knowledge/{artifact_id}/download` captures a bounded,
hash-checked copy before responding. A changed/unreadable source returns HTTP 409
with refresh guidance; an unknown artifact returns 404. Successful downloads use
attachment disposition, `nosniff`, a checksum ETag and `Cache-Control: no-store`.
The handler sends captured bytes, so a later workspace path replacement cannot
switch the download to another file.

Both routes require current project read permission at entry and after file I/O,
in addition to normal control-plane middleware. Responses are never publicly
cacheable. The React panel cancels requests on close/navigation and discards late
results; project changes remove the previous project's results immediately.

`mcp__ophira_ledger__search_project_knowledge` uses the same project-bound reader
through the existing SDK MCP server. It has a native read-only annotation and
cannot choose a different project. The SDK permission list and post-tool boundary
classify the result as untrusted project data; matching text cannot approve actions
or become instructions. Agent output includes matched source metadata and coverage
counts, without dumping every document into the context. A match creates no
Evidence, Decision, Memory, Skill or automation commitment. The main Agent may
inspect the source and separately propose or record evidence through existing tools.

SDK integration follows the local `docs/claude-agent-sdk/custom-tools.md` snapshot,
the installed 0.2.143 decorator API and the
[official custom-tool contract](https://code.claude.com/docs/en/agent-sdk/custom-tools)
checked on 2026-09-09. No runtime or dependency upgrade was introduced.

## Verification boundary

Offline tests cover multilingual search, source coordinates, version replacement,
missing files, cross-project exclusion, limits, regex literal treatment, OCR notice
retention, fresh ledger reconstruction, read-only behavior, path/link attacks,
gateway access and revocation during reads/downloads. UI tests cover searching,
coverage gaps, provenance, scoped downloads, stale responses, project changes,
refresh/error recovery, untrusted HTML and Chinese labels.

Office tests now cover real generated DOCX/XLSX/PPTX files, logical source positions,
authorization and exact-byte download, shared strings, notes/charts, and hostile
or oversized OOXML packages. An application cold backup test restores an Office
artifact and rebuilds the same indexed source location. These checks do not
certify real-model retrieval quality, arbitrary Office documents, large-corpus
performance, production isolation or visual browser acceptance. In-app Browser
discovery on 2026-09-24 returned an empty list after troubleshooting; no visual acceptance
is claimed.

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
- Supported sources are UTF-8 text, Markdown, CSV/TSV, JSON/JSONL, YAML, XML, HTML,
  RST and bounded DOCX/XLSX/PPTX text views (see below). PDF/image originals remain
  available in the existing artifact area;
  their registered OCR Markdown companions are searchable, with original artifact
  ids and a bounded copy of the ingestion preamble alongside matching passages.
  OCR notices remain source text, not an assurance of extraction quality.
- Search terms are literal and case insensitive; all whitespace-separated terms
  must appear somewhere in the file or path. Chinese substrings work without a
  remote tokenizer. Filename matches rank first, then newest files. Each matching
  document returns one excerpt, including its first body match when present.
  This is lexical retrieval; it does not infer synonyms or semantic relevance.
- Every result identifies the artifact id, path, version, SHA-256, registration
  time, originating Run and source record ids. Line/column coordinates are
  one-based Unicode text positions for text sources, after removing a UTF-8 BOM and normalizing
  CRLF/CR to LF. Long-line excerpts include a starting column. Displayed excerpts
  are redacted and bounded, so downloads are the authority for exact original text.
- A file whose current bytes disagree with its registered checksum is excluded.
  Missing, changed, unsupported, undecodable and over-limit sources have explicit
  coverage states. A failed search never establishes that the project has no such
  information. Refresh recalculates the view, and artifact revision changes also
  refresh an open panel.

Limits: latest 250 paths, 2 MiB per file, 16 MiB total read allowance, 200 query
characters, 16 terms and 1,400 characters per excerpt. Failed read attempts consume
their reserved allowance too. HTTP returns up to 30 matches; MCP up to 10 to retain
space for provenance and OCR notices. Matching-file totals and omitted-file counts
are separate from returned-result counts. Larger corpora and legacy Office formats
remain explicit gaps; no file is silently advertised as fully indexed.

## Office references (2026-09-22)

Office originals use the same latest-version, checksum, descriptor capture and
project authorization boundary. Exact originals remain downloadable; extraction
does not overwrite files, create OCR companions or publish verified Evidence.

- DOCX: body paragraphs and table cells in document order. Merged cells are read
  once. The locator is the one-based body paragraph in extraction order, including
  paragraphs inside tables, not a rendered page or a Word paragraph number.
  Headers, footers, tracked changes, comments, text boxes and images are omitted.
- XLSX: actual stored cells, including shared/inline rich strings, booleans and
  raw values, with exact worksheet name and A1 cell reference. Empty grid areas
  are never expanded, even if a sheet declares a million-row dimension. Hidden
  sheets/cells are included. Number/date display formats are not applied. Formula
  expressions are displayed as text; any stored result is labelled **Cached value**
  and may be stale. No formula, external reference or macro is executed or fetched.
  Shared strings follow the [Microsoft Open XML format](https://learn.microsoft.com/en-us/office/open-xml/spreadsheet/working-with-the-shared-string-table).
- PPTX: text, tables, grouped shapes and speaker notes, located by actual
  presentation order rather than the slide part filename. Hidden slides are
  included. Notes have a separate locator. Images, chart content, embedded objects,
  master/layout text and visual reading order are omitted. Presentation ordering
  follows its [slide list](https://learn.microsoft.com/en-us/office/open-xml/presentation/working-with-presentations).

The optional `source_location` on each match identifies `paragraph`, `cell`,
`slide` or `slide_notes`. Line/column numbers then refer to the extracted block,
not to Office file bytes or page layout. Each excerpt stays inside its own block;
short blocks retain their full context (including formula/cache labels). All query
terms still match at document level, so some terms can occur outside the excerpt.
Source names and excerpts are redacted at the same output boundary as text files.
Both coverage and matching results display extraction limitations, including in
Chinese. Empty-query coverage also shows these notices.

Before native Word/PowerPoint readers run, every ZIP member is bounded and checked.
XML uses explicit `resolve_entities=False`, `load_dtd=False`, `no_network=True` and
`huge_tree=False`, per the [lxml parser contract](https://lxml.de/parsing.html).
DTD declarations (including UTF-16), duplicate/traversal entry names and encrypted
ZIP entries are rejected. Package parts are never extracted to disk. The XLSX
reader resolves only internal worksheet/shared-string XML relationships and never
iterates a declared rectangular grid. Ambiguous cell references fail closed.

Additional limits: 512 archive parts, 16 MiB expanded per package, 4 MiB per XML
part, 100,000 XML nodes, 20,000 text blocks/Word table grid cells and 2 Mi characters
of extracted text. A search reserves at most 64 MiB of archive expansion before
decoding; failed parses retain their reservation. `office_bytes_reserved` reports
this charged allowance, independently of compressed `bytes_read`. These limits
supplement the original 2 MiB file and 16 MiB read limits. Malformed/unsupported
Office internals report `invalid_document`; per-file expansion/extraction limits
report `too_large`; the aggregate allowance reports `budget_exceeded`. None returns
partial text disguised as a fully searchable file.

Legacy DOC/XLS/PPT, password-protected files, Strict OOXML variants, chartsheets,
rendering/OCR of embedded images, chart semantics and layout fidelity remain outside
this extractor. These limits are bounded local parsing, not OS process isolation or
a certification of arbitrary hostile document decoding in production.

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

Office tests additionally cover real application-generated packages, hyperlinks,
tables, presentation reordering, grouped shapes, notes, shared strings, sparse cells,
inert formulas/cache labels, archive/XML attacks, budgets, version replacement and
exact authenticated downloads; native MCP returns the same untrusted locators.
UI tests cover Office locations, extraction coordinates and Chinese coverage notices.

These checks do not certify real-model retrieval quality, Office visual fidelity,
large-corpus performance, production isolation or visual browser acceptance. Browser
setup on 2026-09-09 returned `No browser is available`; the documented discovery
check returned an empty list. No visual acceptance is claimed.

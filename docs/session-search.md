# Project session search

FR-35 searches one project's saved Run prompts, user directions sent during a Run,
main Agent messages, and SDK-visible Run results. Every result includes its Run ID,
message type, and an event sequence when it came from the event log. Opening a
result replays that Run. The API requires `RUN_READ`; the Agent's in-process MCP
tool uses the active project's ledger and marks historical text as untrusted.
Tool output, private reasoning, other projects, and branch Agent messages are
outside this search.

The search is literal and case insensitive. It checks at most 500 Runs per
request, 512 KiB per event log, and 8 MiB or 10,000 events per request. A response
contains at most 20 matches, with one match per Run and 320 characters per
excerpt. `next_cursor` lets the user or MCP tool continue through older Runs;
the cursor is tied to the project and normalized query. New Runs created after a
page do not repeat in its continuation. `scanned_runs` and `skipped_event_logs`
describe the current response; skipped oversized logs keep coverage incomplete
even after the last page. A request budget reached before a Run's event log is
searched leaves that Run for the next page. Event data is searched only after
the normal event redaction boundary, and prompt excerpts are redacted before
returning them.

The local test suite covers project isolation, `RUN_READ`, replay identifiers,
redaction, user and Agent message types, bounded logs, cursor continuity and
the MCP trust boundary. Each page still reads all Run metadata from the file
ledger before filtering by project, but retains only the newest 501 eligible
Run records in memory. Large history I/O performance, real conversation quality
and browser interaction still need separate acceptance.

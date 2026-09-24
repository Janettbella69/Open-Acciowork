# Project session search

FR-35 searches one project's saved Run prompts, user directions sent during a Run,
main Agent messages, and SDK-visible Run results. Every result includes its Run ID,
message type, and an event sequence when it came from the event log. Opening a
result replays that Run. The API requires `RUN_READ`; the Agent's in-process MCP
tool uses the active project's ledger and marks historical text as untrusted.
Tool output, private reasoning, other projects, and branch Agent messages are
outside this search.

The search is literal and case insensitive. It checks at most the newest 500
Runs, 512 KiB per event log, and 8 MiB or 10,000 events per request. A response
contains at most 20 matches, with one match per Run and 320 characters per
excerpt. It reports skipped logs and incomplete coverage; an empty result with
`truncated=true` does not prove the text is absent. Event data is searched only
after the normal event redaction boundary, and prompt excerpts are redacted
before returning them.

The local test suite covers project isolation, `RUN_READ`, replay identifiers,
redaction, user and Agent message types, bounded logs, and the MCP trust boundary.
Real conversation quality, large history performance, and browser interaction
still need separate acceptance.

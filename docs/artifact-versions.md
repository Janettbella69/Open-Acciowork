# Artifact history and text editing

PRD FR-16 and §3.5 E require files that can be changed, downloaded and continued
without losing their version lineage. Open **Project files** in a saved workspace
to find every registered path, download an exact version, inspect its history, or
preview and edit a supported text file. The panel replaces the previous four-row
artifact list and supports English and Chinese.

## Version storage

Every new registration captures a regular file through descriptor-based path
traversal. Symlinks, hard links, special files and files changing during capture
are rejected. The host saves a content-addressed copy under
`projects/{project_id}/artifact-versions/{sha256}`, outside the writable workspace.
It flushes that copy before atomically publishing the Artifact record. Copies use
mode 0400, and the directory uses mode 0700; checksum verification remains the
integrity authority even if an operator changes permissions or files.

The project transaction lock covers capture, deduplication, version allocation
and ledger publication across API workers. Registering unchanged current content
returns the same Artifact. Changing A → B → A produces versions 1 → 2 → 3, with
the third record pointing to the second as its parent; its bytes can reuse the
first copy. A partially completed registration can leave an unreferenced copy,
but cannot publish a record before its copy is durable.

New records use `content_storage: "snapshot"`. Historical records without that
field retain `"workspace"` semantics and can be downloaded only when the current
bytes still match their original checksum and size. Registering an unchanged
legacy file pins its surviving bytes without inventing a new version. Missing
historical bytes are not reconstructed from the current file or a model.

The limits are 250,000,000 bytes per file (matching the existing video boundary),
2,000,000,000 bytes and 10,000 entries per project's snapshot directory. Existing
copies are reused. Interrupted staging files consume allowance until an operator
cleans them; there is no automatic deletion of registered history. These are
local storage limits, not production capacity or metadata scalability claims.

## Downloads and edits

`GET /api/projects/{project_id}/artifacts/{artifact_id}/download` captures and
verifies the requested version before returning bytes. It checks current project
read permission both before and after I/O, and returns attachment disposition,
an SHA-256 ETag, `nosniff`, and `Cache-Control: no-store`. Missing/corrupt versions
return 409. Replacing a working file after capture cannot switch the response.

`GET .../{artifact_id}/text` returns the exact UTF-8 content, selected Artifact,
latest version id, edit availability and a reason when editing is unavailable.
Supported formats are the project's existing text reference formats: text,
Markdown, CSV/TSV, JSON/JSONL, YAML, XML, HTML and RST. Previews and edits are
limited to 512 KiB, reject NUL bytes, and exclude hidden paths. Binary formats
remain downloadable and can still be changed by the Agent's existing file tools.
The web editor does not execute HTML or evaluate formulas.

`PUT .../{artifact_id}/text` accepts:

```json
{
  "content": "Revised report\n",
  "expected_checksum": "<SHA-256 of the selected version>",
  "request_id": "<unique id for this exact draft>"
}
```

The route requires project write permission, bounds the streamed request body,
and rechecks authorization after reading it and immediately before replacement.
It requires the selected Artifact to be the latest version and the working file
to match that Artifact. A different saved version, unregistered edit or directory
replacement produces a conflict; the editor retains the draft for comparison or
download. Retrying a committed request returns the same version, while reusing
its id with a different author, base or content is rejected.

All nonterminal project Runs block human file edits, including paused approval
waits. Terminal Runs with live execution leases also block edits during cleanup.
Run publication and editing share the project lock, so a new Run cannot begin
while the file transaction is in progress. Native Agent tools remain unchanged.
Uncoordinated external applications do not participate in this lock; file and
directory identity checks narrow that race but do not provide an OS filesystem
transaction against arbitrary external writers.

The editor displays saved and proposed content before enabling **Save new
version**. Saved edits have no fabricated Run id: they record the authenticated
principal (or `local-operator`) and link to the previous Artifact, which retains
its own sources. They update the workspace file and project revision, so the
existing reference index and subsequent Agent runs consume the edited content.
They create no Evidence, Decision or approved Memory automatically.

## Interrupted edits and backups

Both old and new bytes are frozen before replacing a working file. A durable
`pending-artifact-edit.json` binds the proposed record and request id before the
atomic file swap. If the process dies before publishing the project record, a
later preview, retry, Run creation or Artifact registration reconciles that
intent under the project lock. Matching current bytes complete the original
record; absent/different bytes preserve the working file and discard the
uncommitted intent. Recovery never repeats the file write. Already committed
edits simply clear the leftover intent. This does not restore arbitrary Bash
changes to unregistered files or claim recovery from disk corruption.

The application cold backup includes the version directory and validates every
snapshot-backed Artifact's bytes before accepting its copied ledger. Restoring
to a new directory preserves historical downloads even when the workspace file
was deleted. A corrupt snapshot fails backup verification. Legacy bytes that were
lost before this feature remain unavailable after restoration.

## Verification boundary

Offline verification covers real file edits, bytes and lineage, current-only
knowledge retrieval, download integrity, legacy compatibility, quota and link
attacks, concurrent registration/edits, permission revocation, request retries,
and three edit interruption windows. Additional tests terminate a real spawned
process after the workspace swap, race two/four independent registration
processes, and cold-restore version history into a different directory.

React tests cover all-version downloads, preview-before-save, retained conflict
drafts, idempotent retries, read-only access, explicit draft discard, source text
escaping, late reads/saves after project navigation, and Chinese labels.

Browser setup on 2026-09-09 returned `No browser is available`; the prescribed
discovery check returned an empty list. No visual browser or real-model editing
acceptance is claimed. Office rendering, project-level change streams,
broader filesystem checkpoints and production
isolation remain in the full PRD inventory.

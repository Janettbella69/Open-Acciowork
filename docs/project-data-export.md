# Project and conversation data exports

Open **Export project data** in a saved workspace. Review either the entire project
or the current conversation, then download the reviewed ZIP. The selected runs must
have finished; another worker using the project workspace prevents export. The
current project owner/admin, or the local operator in local mode, can export.
Members, reviewers, companion devices and anonymous organization requests cannot.

The archive contains private business information and is **unencrypted**. It is a
portable record of application data, not an application restore package. Use
[application cold backup](application-backup.md) for operator recovery and
[credential management](credential-management.md) for encrypted credential exports.

## Contents and boundaries

Both archive types contain:

- `manifest.json`: schema version, scope, project revision, Run IDs, review
  fingerprint, export time/actor, exclusions, warnings, and file sizes/SHA-256 hashes.
  The manifest does not hash itself. Its fingerprint is SHA-256 of the sorted,
  indented UTF-8 JSON descriptor plus newline, excluding `fingerprint`, `exported_at`
  and `exported_by`.
- `conversation.md`: original Run prompts and application-visible assistant text.
  Completed messages replace their corresponding streamed draft; unfinished text
  is labelled partial and branch output has a separate label. Raw Run event logs
  preserve timestamps, actors and order.
- `data/runs/{id}/`: Run record, available events, summary and task-experience context.
- `artifacts.json`: registered version metadata and the exact archive path for each
  available version. Missing historical workspace-only versions are explicitly
  listed with `export_path: null` and a warning. Missing or changed immutable
  snapshot bytes fail the export rather than silently substituting current files.
- `project-memory.json`: current project memories, history and deletion tombstones.
  Deleted memory bodies are excluded even if an interrupted deletion left stale
  source history. Pending and rejected records remain available to the owner/admin.

A **project archive** also contains the project's directory under `data/projects/`
(including working files, immutable artifact bytes, checkpoints and local project
diary), project-scoped receipts, commercial actions, template records/references,
discussions, project access and marketplace grants. Notifications, intents,
feedback, approval grants, channel deliveries, shop identities, task experience,
schedules and frozen Skill proposals are selected by their project association.
Associated records retain their original bytes under `data/`.

A **conversation archive** selects the requested Run and other Runs in the same
project with the same SDK session or resume-session ID. `session.json` records this
selection. It includes only those Runs' registered outputs and project memories
whose source Run belongs to the selection. It does not copy the whole project
workspace, unrelated conversations, all project evidence/decisions or schedules.

Neither archive copies Provider/Connector secrets, internal SDK transcripts or
runtime configuration, browser login profiles, device credentials, shared Agent
core files, shared user/Agent memories, other projects or external-service data.
Shop identity JSON is included; browser profile directories are excluded. Exported
business records and uploaded files can themselves contain sensitive text; this
is not a content-redaction or malware-removal service. Export shared memories
separately in Settings.

SDK native transcript portability, project/conversation deletion, application
restore UI and production disaster recovery remain separate outstanding work.
This delivery does not mark all PRD data-sovereignty requirements complete.

## API and consistency

- `GET /api/projects/{project_id}/data-export?run_id={optional_run_id}` returns review
  metadata, counts, exclusions, warnings and a fingerprint; it creates no archive.
- `POST /api/projects/{project_id}/data-export` accepts
  `{"fingerprint":"<reviewed sha256>","run_id":null}` (or the reviewed Run ID) and
  streams `application/zip` with attachment, `no-store` and `nosniff` headers.

Both requests reauthorize against current membership. Capture runs under the
existing cross-process workspace execution lease and rejects active/nonterminal
selected Runs. The service derives an inventory from authoritative stores, checks
every file again while writing the ZIP, then recomputes the full inventory before
returning it. Changed bytes, new related records, scope mismatches or stale review
produce HTTP 409. The user must review again; no partial archive is returned by
the build step. Authorization is checked throughout capture and before each
streamed chunk; a revoked request may leave an incomplete downloaded ZIP.

Archive creation uses one process-local slot and a private anonymous temporary
file. Normal completion, stream disconnection and build failures close that file.
There is no persistent export catalog or duplicated project ledger. Memory capture
uses the `MemoryProvider` port and verifies source history twice.

Paths are captured through file descriptors without following symlinks; hard
links, special files, unsafe relative paths, case/Unicode aliases and file/directory
collisions are refused. Limits fail explicitly rather than truncate: 20,000 files,
50,000 discovered entries, depth 32, 250 MB per source file, 512 MB archive content,
128 MB discovery records (4 MB per record), 1,000 project Runs, 200,000 selected
events, and 16 MiB project memory source bytes. Discovery scans the deployment's
Run metadata and designated project-associated stores, so deployment size can
exceed these bounds even for a small project.

## Verification

`backend/tests/test_project_exports.py` covers authorized HTTP ZIP downloads and
hashes, resumed-session scope, cross-project rejection, historical artifact bytes,
memory deletion/history, mutation during capture, limits, unsafe paths, runtime
and vault exclusion, owner/admin revocation, unfinished Runs, and stream cleanup.
Frontend tests cover explicit review, scope matching, download and stale-review
errors, duplicate-click prevention, cancellation on close/project/Run changes,
Chinese labels and ZIP response validation. These are local automated checks;
browser visual acceptance and live Provider behavior are not claimed.

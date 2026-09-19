# Memory governance contract

OPHIRA memory remains a typed, user-governed record. Agent and distillation writes are
proposals; only user-created or explicitly approved records are eligible for recall.
Memory identity is server-bound and never comes from an API payload or model argument.

## Lifetime and recall

- A memory may be permanent (`expires_at: null`) or have an explicit UTC/offset-aware
  `expires_at`. `POST /api/memory` accepts either an absolute `expires_at` or a relative
  `ttl_seconds` (1 second through 10 years), never both.
- `PATCH /api/memory/{id}/expiry` sets an absolute expiry, assigns a new TTL, or clears
  expiry with `{"clear_expiry": true}`. The change creates a new version and an audit
  history entry.
- Expired memory remains visible to an authorized person for review, export, renewal,
  or deletion. It is excluded from `gather_approved`, direct approved-memory search,
  the `search_memory` MCP tool, prompt context, and request-signal retrieval.
- Agent-scope marker lines are removed from `MEMORY.md` when expiry is observed and at
  provider startup. Freehand lines are preserved. This is lazy expiry enforcement;
  expiry does not depend on a background scheduler and does not rewrite status.

## Request-signal retrieval

`POST /api/memory/retrieve` accepts a required `query`, optional authorized
`project_id` and `agent_id`, `include_user_scope` (default `true`), and a limit of 1–20.
It returns only records that are all of:

1. in one requested scope;
2. visible to the middleware-authenticated principal (or the fixed local-only operator);
3. approved; and
4. not expired at the response's single `evaluated_at` instant.

The algorithm identifier is
`unicode_nfkc_casefold_all_terms_substring_v1`. Both query and memory text are Unicode
NFKC-normalized, case-folded, and whitespace-collapsed. Every distinct whitespace term
must be a substring of the normalized memory text. Results sort by whole-query substring
match, descending term occurrence count, descending `updated_at`, then ascending memory
ID. Metadata reports only authorized candidate counts, excluded-expired count, matched
and returned counts, normalized terms, requested scopes, and evaluation time.

This deterministic lexical prefetch is locally testable and auditable. It is not claimed
to be vector or semantic retrieval, and it does not make model calls.

## Export and deletion

Settings → Memory review provides an authorized JSON download, expiry/renewal,
disable, and permanent deletion. Deletion first shows the selected text,
explains the backup boundary, and requires a non-empty human reason plus
an explicit permanent-delete action. Cancelling the form does not delete anything.
Expired records must be renewed before approval or editing. Downloads report that
the file was sent to the browser; they do not claim that the user saved it.

List loading and mutations cannot overlap in this interface. Forbidden/missing
records clear their local text and editor; transient errors preserve retry context.
Late responses after cancellation or navigation cannot download or restore content.
Memory API successes and errors use `Cache-Control: no-store` and
`X-Content-Type-Options: nosniff`.

- `GET /api/memory/export` returns a downloadable `memory-export-v1` JSON object. It may
  be filtered by `scope` plus `scope_id` and includes every authorized, non-deleted
  record, including proposed/rejected/disabled/expired records. Each item exposes text,
  kind, source, origin, run/anchor provenance, confidence, sensitivity, status, versions,
  timestamps, expiry, and an evaluated `expired` flag. Principal session bindings are
  deliberately absent from the export. The same authorization pass includes minimal
  tombstones in a separate array, so deletion can be audited without restoring content.
- `DELETE /api/memory/{id}` requires a non-empty human reason and memory-write authority.
  It first fsyncs a minimal tombstone, then removes the record from `items.json`, scrubs
  every full-content version from `history.jsonl`, and removes the exact agent marker
  from `MEMORY.md`. A startup recovery pass finishes content scrubbing if the process
  stopped after the tombstone became durable.
- Tombstones do not copy memory text. They contain the memory ID/scope and identity binding,
  last version/status, source/origin/sensitivity, source run, SHA-256 content digest,
  deletion actor/reason/time. This preserves a minimal audit without making deleted
  content recallable or exportable.

Application hard deletion covers the live file-backed store and its application history.
Deletion from filesystem snapshots, infrastructure backups, replicas, and storage media
requires the deployment operator's retention/erasure policy; this repository does not
claim secure physical overwrite or remote-backup deletion.

## Concurrent workers and recovery

Every scope read, update, deletion, expiry-marker reconciliation and startup recovery
uses the same reentrant transaction across provider instances, threads and local
processes. Stable lock files are kept outside scope directories and must not be
removed while a worker is running. A writer rereads the current snapshot inside the
transaction; rollback and deletion audit the latest locked state. This prevents a
late correction or recovery pass from rewriting deleted text or losing another write.

Snapshots and marker rewrites use unique private temporary files, file fsync, atomic
replacement and directory fsync. Recovery and deletion remove abandoned memory
staging files while holding the same scope transaction. A durable deletion tombstone
survives termination before snapshot publication and drives subsequent content scrub.
The approved-memory limit remains 32 records / 8,000 characters per scope; all
effective-approved writes, including user creation, rollback and renewal, check the
same limit inside the transaction. Capacity failures are HTTP 400, not a missing-record
404. Disabling or deleting an approved record frees capacity.

This protocol requires all cooperating workers to run the new code. Stop older workers
before upgrading. Local multi-process and killed-process recovery checks do not certify
network filesystem locking, storage loss, physical erasure or infrastructure backups.

## Authorization boundary

- Project memory requires current project membership and the appropriate `memory.read`
  or `memory.write` permission.
- User- and agent-scope memory requires the same authenticated tenant and principal.
- Local-only requests can see only local-only records. Trusted records cannot be claimed
  by a local request, and a trusted request cannot claim legacy local records.
- Export/list paths filter every record independently. Retrieval rejects an explicitly
  requested unauthorized project instead of returning its existence or candidate counts.

## Requirement inventory

| Requirement | Status | Evidence/boundary |
|---|---|---|
| Explicit TTL / `expires_at` | Delivered locally | Versioned create/update models and provider enforcement |
| Exclude expiry from prompt recall and search | Delivered locally | Shared expiry predicate used by gather, search, and retrieval |
| Hard delete plus durable tombstone | Delivered locally | Tombstone-first write, content/history scrub, startup recovery |
| Exact `MEMORY.md` cleanup | Delivered locally | Marker-only removal preserves user freehand content |
| Authorized JSON export with provenance/sensitivity | Delivered locally | Public DTO omits principal-session binding |
| Memory review lifecycle UI | Delivered locally | JSON download, expiry/renewal, disable, confirmed deletion; EN/ZH and narrow-screen checks |
| Cross-process deletion and capacity consistency | Delivered locally | Scope flock transactions, real spawned workers and killed-process recovery |
| Request-signal retrieval with deterministic metadata | Delivered locally | Lexical algorithm contract above; no external dependency |
| Vector/semantic retrieval | Deferred | Not required for deterministic local acceptance; no embedding provider selected |
| Backup/media erasure | Deployment responsibility | Requires infrastructure retention controls outside this repository |

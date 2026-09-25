# Independent conversation deletion

Implementation contract for PRD v2.2 §3.9. The PRD and AGENTS.md remain the architecture authorities.

## Requirement inventory

| Requirement | Status / boundary |
| --- | --- |
| Resolve the whole conversation from a durable Run anchor | Implemented: transitive resume-source and Provider/session links, including failed resume attempts; retry-as-new remains independent |
| Review exact scope, owner/admin authorization, explicit name and reason | Delivered: HTTP authorization and EN/ZH review UI, exact name gate, immutable operation ID |
| Remove prompts, summaries, events, native transcripts/subagent output and native checkpoints | Delivered: bounded inventory, official SDK deletion, supplemental cleanup and residue checks |
| Remove linked inbox/reply/feedback copies, exact manifest and generated diary entries | Delivered: selected records only; Agent and legacy project diaries preserve adjacent entries and manual notes |
| Preserve other sessions within the same project | Implemented; native SDK integration tests cover preservation and interrupted retries |
| Durable intent, cross-worker exclusion, same-operation recovery | Delivered: real dual-worker HTTP crash after native deletion, peer exclusion and same-ID recovery |
| Block deleted Run/session revival and preserve project provenance | Delivered: deleted Run reads/resume fail; project checkpoints, exports and cold backup/restore use content-free provenance |
| Real HTTP and bilingual browser verification | Delivered: EN/ZH at 320/400/1280 px, retained other conversation, local draft/files and refreshable progress |
| Historical backups, external copies, physical-media erasure | Restore CLI can compare current project/conversation records and memory tombstones before publishing; backups and external copies are not erased |

## Product boundary

The human chooses a Run in a project. Review resolves all connected Runs and displays their IDs/count.
Confirmation uses the project name, an explicit irreversible-action acknowledgement and a reason whose
hash alone is stored. A fingerprint binds the exact inventory and reviewer; changed reviews must be
refreshed. Only project owners/admins, or the fixed local operator for local-only projects, may delete.

This removes the conversation's application Run directories, native transcript/subagent copies,
native file history and session sidecars, exact generated Agent diary blocks, Run manifest rows, and
linked notifications, feedback and pending channel replies. Already sent external replies are not
recalled. SDK `delete_session()` from pinned 0.2.143 is used, with bounded no-follow inventory and
supplemental cleanup for its documented omissions.

The project remains. Its files, artifacts, evidence/source receipts, workspace recovery snapshots,
memories, schedules/task experience, Skill proposals and commercial records are separate retained
records. They can contain independently saved extracts of the conversation. Other native sessions and
shared native project memory remain. Minimal Run ID/project/trace/session/timestamps/status and
hashes of identity and prompt excerpt keep retained recovery checkpoints, source receipts, commercial
records and Skill provenance verifiable. These hashes retain neither the prompt nor the original
principal-session binding. This provenance is never a resumable Run. Authorized project exports
include it in `deleted-conversations/run-origins.json` without exporting the deletion requester.

Deletion blocks if native ownership is ambiguous, a legacy session index needs reconciliation, native
checkpoint hardlinks extend outside the selected conversation, task experience has a pending
transaction, a selected Run is nonterminal, or project work still holds a lease. No unreviewed session
is silently included to make deletion succeed.

Once an intent is committed, the project is temporarily unavailable until the operation completes.
After completion, other conversations and new work are available. Deleted Run IDs and Provider/session
IDs cannot be recreated through the application. The operation status remains available outside the
Run/project routes and is bound to its original requester session. Startup retries pending intents
before constructing content-recovering services. A failed response after a commit attempt is ambiguous:
clients must inspect or retry the same operation ID.

## Verification

Evidence: [`evals/reports/conversation-deletion-20260919/`](../evals/reports/conversation-deletion-20260919/).
Backend: 2138 passed, 13 intentionally gated skips. Frontend: 470 passed in 44 files.
Ruff, ESLint, production build, 59 evaluation-tool tests and 61 offline snapshot cases passed.
The build retains the existing bundle-size warning (835.31 kB main JavaScript bundle).

HTTP and browser checks use disposable local projects, synthetic native transcripts and real
SDK file deletion. They do not invoke a model, paid Provider, external shop or actual user data.
This does not establish live model quality, historical-backup governance or physical-media erasure.

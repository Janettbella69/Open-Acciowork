# Conversation deletion acceptance — 2026-09-19

Base commit: `4b1684f`. This report accompanies the independent conversation deletion changes.

## Automated checks

- Backend: **2138 passed, 13 skipped**, 129.64 s; final source, isolated startup data directory.
- Frontend: **470 passed / 44 files**.
- Ruff, ESLint and production build: passed. Existing bundle warning remains: main JS 835.31 kB
  (249.03 kB gzip).
- Evaluation tools: **59 passed**. Offline contract/mutation snapshot gate: **61/61**.
- Targeted diary/provenance suite: 32 passed; full suite above also covers these final changes.

`backend-tests.txt`, `frontend-tests.txt`, `backend-lint.txt`, `frontend-build.txt`,
`eval-tests.txt` and `snapshot-evals.txt` retain the command output. The backend runner patches
startup settings before importing the app, uses `_env_file=None` and a temporary data directory,
and runs from a script file so macOS `spawn` tests work. An earlier stdin-based harness was
discarded because spawned children cannot import `<stdin>`; it was not an application failure.

## Real HTTP and browser checks

`http-verification.json`: two loopback Uvicorn processes share only disposable fixture data.
The first exits with code 72 immediately after the real pinned SDK erases native transcripts.
The peer rejects project reads/new Runs, reports the same operation as pending, finishes cleanup
through `/retry`, preserves another conversation and permits project export afterward.
Repeated confirmation uses the same immutable operation rather than creating another deletion.

`browser-verification.json` and PNGs: isolated Chromium against real Vite/API. English and Chinese
reviews at 1280, 400 and 320 px require the exact project name, reason and acknowledgement.
No horizontal overflow. Deletion keeps unsent text/local attachments, clears the removed response,
preserves the project, reopens its other conversation, and restores status after reload. No page
JavaScript errors. Browser assertions use uniquely named disposable fixtures; initial locator
ambiguities in the harness were corrected before the final pass.

## Retention boundary

Deleted: the selected resume chain's application Run records, events/summaries/context, native
transcripts/subagent output/session sidecars/checkpoints, linked inbox/reply/feedback records and
exact generated diary/manifest entries. Deferred native ownership, shared checkpoint links or
unknown session indexes block deletion instead of erasing another session.

Retained and disclosed: other conversations, project files/artifacts/evidence/source receipts,
workspace recovery snapshots, memories, automation/experience, Skill/commercial records and
minimal provenance. Independently copied conversation excerpts in these records remain.
External messages, exported copies, historical backups and physical media require separate
management. No live model, paid Provider, external shop or actual business data was used.

# Requirement inventory

Updated: 2026-08-05

| Requirement | Owner | Status | Evidence / note |
| --- | --- | --- | --- |
| Replicate Accio Work's useful team/runtime mechanisms without copying branding or source | Backend | Delivered | Team lead, durable project/run state, delegation, permissions, audit, replay, budget, and cancel foundation |
| Use Claude Agent SDK | Backend | Delivered | Official Python SDK 0.2.82 pinned; integration lives behind an adapter |
| Opportunity Lead manages the whole opportunity and stage decisions | Backend | Delivered | Lead prompt, shared ledger, weakest-link loop, and stage-decision tool |
| Demand Agent validates demand | Backend | Delivered | Programmatic SDK subagent |
| Supply Agent validates supply availability | Backend | Delivered | Programmatic SDK subagent |
| Capital Agent validates capital and downside | Backend | Delivered | Programmatic SDK subagent |
| Sales Agent validates sales executability | Backend | Delivered | Programmatic SDK subagent |
| All five agents use the same project, hypotheses, constraints, and evidence | Backend | Delivered | Structured, atomic per-project file ledger and in-process MCP tools |
| Frontend can render intermediate process and tool-call details | Backend | Delivered | Replayable SSE contract for text, summarized reasoning, tool, permission, task, subagent, usage, cost, and result events |
| Connect the pushed frontend to the backend | Full stack | Delivered | Typed client, Vite development proxy, project/run flow, protocol-driven SSE listeners, live ledger views, approvals, and cancellation |
| Expose the entire private model chain-of-thought | Backend | Omitted | Not an available or appropriate application surface; visible SDK reasoning summaries are labeled and streamed |
| Follow the supplied Feishu production AI coding rules | Repository | Delivered | Rules codified in `AGENTS.md`; file ledger, trace, logging, stop conditions, and requirement gate reflected in architecture |
| Build frontend | Separate frontend agent | Deferred | Explicitly assigned by the user to another agent; backend provides an integration contract |
| Push verified backend to GitHub | Backend | Delivered | 10 tests, Ruff, dependency check, and editable package build pass; commit contains only backend-owned paths |
| Agent profiles and templates (PRD FR-1) | Backend | Delivered | `POST /api/agents` creates from `general`/`research`/`sourcing`/`marketing`/`docs`/`opportunity` templates; config revisioned via `update`; opportunity team preserved as a template |
| Agent Core files and prompt assembly (PRD §11) | Backend | Delivered | Per-agent `IDENTITY/AGENTS/SOUL/USER/MEMORY/TOOLS.md` + `diary/` + `workspace/`; security preamble pinned first, files never overwritten |
| Memory governance thin layer (PRD FR-7 first half) | Backend | Delivered | `MemoryProvider` port + file store; agent proposals stay `proposed` until user decision; approved-only retrieval and prompt injection; versioned rollback; `propose_memory`/`search_memory` MCP tools |
| Session resume (SDK-native) | Backend | Delivered | `resume_session_id` on run creation maps to SDK `resume` |
| Write/shell tools gated by the permission broker (PRD FR-4 first half) | Backend | Delivered | `Write`/`Edit`/`Bash` removed from pre-approval; workspace path + symlink-escape guard; `Bash` excluded from `allow_run` reuse |
| Scheduled tasks (PRD thin slice) | Backend | Delivered | cron or one-shot schedules, timezone-aware; startup missed-run reconciliation (`catch_up`/`skip`); overlap guard; manual test run; 35 tests + Ruff pass |
| Natural-language schedule creation | Backend | Deferred | Form/cron first; NL→cron belongs to a chat flow later |
| Persistent grant management UI, per-tool four-level policies | Backend | Deferred | Broker supports `allow_once`/`allow_run`/`deny`; durable per-agent grants need their own store and UI |
| Memory consolidation (PRD diary + MEMORY.md) | Backend | Delivered | Approved agent-scope memories sync into `MEMORY.md` as marker lines (freehand edits preserved); every finished run appends a dated diary entry; 40 tests + Ruff pass |
| Memory consolidation (/dream), skill crystallization, identity self-update | Backend | Deferred | Self-evolution track; requires the memory layer delivered here first |
| Desktop shell (Electron/Tauri), Skills/Plugins, connectors, browser control | Product | Deferred | PRD P0 remainder; architecture unchanged by this slice |

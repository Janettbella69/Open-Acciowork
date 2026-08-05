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
| Expose the entire private model chain-of-thought | Backend | Omitted | Not an available or appropriate application surface; visible SDK reasoning summaries are labeled and streamed |
| Follow the supplied Feishu production AI coding rules | Repository | Delivered | Rules codified in `AGENTS.md`; file ledger, trace, logging, stop conditions, and requirement gate reflected in architecture |
| Build frontend | Separate frontend agent | Deferred | Explicitly assigned by the user to another agent; backend provides an integration contract |
| Push verified backend to GitHub | Backend | Delivered | 10 tests, Ruff, dependency check, and editable package build pass; commit contains only backend-owned paths |

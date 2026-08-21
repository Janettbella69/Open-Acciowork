# OPHIRA Engineering Rules

These rules apply to every contributor and AI agent in this repository.

## Product boundary

- OPHIRA is a global-business system for small cross-border teams. A single Claude Agent SDK main Agent turns goals and source material into traceable decisions, editable artifacts, and next actions.
- Normal runs must remain one main Agent. Demand, Supply, Capital, and Sales are optional Skill perspectives and evidence tags, not mandatory agents or workflow stages.
- Only an explicitly selected Deep Research run may expose short-lived, read-only research subagents. The main Agent remains responsible for synthesis and is the only writer of project truth.
- Frontend and backend work must remain in their existing directories. Coordinate through documented API and event contracts rather than cross-editing ownership areas.

## Before changing code

1. Read this file, the local README, relevant source, tests, and recent Git history.
2. Distinguish observed facts, user statements, community signals, and inference.
3. For an unfamiliar or changing SDK, use current official documentation and the installed package API before implementation. For the Claude Agent SDK this is mandatory, not advisory: read the relevant page in `docs/claude-agent-sdk/` (local snapshot of the official docs, indexed in its README) before developing any Agent-related fix or capability — the SDK is too new to be in AI training data.
4. Clarify product, interaction, technical, and boundary ambiguity before an irreversible or high-risk change.
5. Keep an explicit requirement inventory and mark every item delivered, deferred, or omitted with a reason.

## Architecture

- Use one source of truth for event types, statuses, tool names, configuration, and error codes. Consumers import these definitions; do not retype protocol strings.
- Persist project state and evidence in a structured file ledger. Chat context and model memory are not authoritative storage.
- Each agent owns a core directory under `data/agents/{agent_id}/` (`IDENTITY.md`, `AGENTS.md`, `SOUL.md`, `USER.md`, `MEMORY.md`, `TOOLS.md`, `diary/`, `workspace/`). Prompt assembly always pins the security preamble first; agent-writable files must not be able to override it.
- Agent memory writes are proposals only. Memory persists through the `MemoryProvider` port (file-backed today, Managed Agents adaptable later); only user-approved memories are injected into prompts or searchable by agents. Approved agent-scope memories are mirrored into the agent's `MEMORY.md` via marker lines without clobbering user edits, and finished runs append dated entries to `diary/`.
- Keep business capabilities behind ports/adapters. Claude Agent SDK integration belongs in an adapter and must not modify SDK internals.
- Claude Agent SDK is the only model runtime. Preserve the Claude Code tools and system-prompt presets, load application Skills through a managed local Plugin, and pass MCP servers explicitly with strict MCP configuration.
- Do not introduce LangGraph, LangChain, Dify, a direct model-client fallback, or a hand-written tool loop.
- Prefer official framework extension points. Do not rebuild session, subagent, permission, hook, or tool mechanisms already supplied by Claude Agent SDK.
- Add a new abstraction only when wrong coupling has a concrete cost: unsafe consequences, different change cadence, different consumers, or different deployment.
- Audit for duplicate implementations and duplicate concepts before adding infrastructure.

## Diagnosis and risk

- For root-cause work use: investigate without edits, report evidence/options/impact, then implement after confirmation.
- Diagnose across application code, adjacent code, framework/library behavior, environment, and external dependencies.
- Add observability before speculative fixes. A confirmed defect may be fixed; an uncertain one must first be reproduced or instrumented.
- If three attempts fail for the same issue, stop patching and reassess the architecture or assumption.
- Use minimum contact surface for risky operations: read before write, operate on copies before originals, and preserve recovery paths.

## Observability

- Propagate one `trace_id` across API, run, Agent, subagent, tool, and client events.
- Keep three log levels: a run manifest/index, a compact run summary, and detailed JSONL events.
- Stream only SDK-observable output: status, user-visible text, summarized reasoning when explicitly supplied, tool input/output, permissions, subagent/task lifecycle, usage, and result.
- Never claim or expose private chain-of-thought. Redact secrets from every event and log boundary.

## Verification and delivery

- Keep changes small and focused. Cut optional MVP scope before compromising the core architecture or correctness.
- Add tests for changed behavior and protocol contracts.
- Run the relevant test, lint, and build commands from the directory being changed.
- Commit and push verified work promptly. Stage only files owned by the current task; never sweep in another agent's unfinished changes.

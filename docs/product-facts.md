# Accio Work product facts

> Verified: 2026-08-05
> Sources: https://www.accio.com/work/, https://www.accio.com/work/feature, https://www.accio.com/work/permissions, https://www.accio.com/work/doc?slug=automations-guide, https://www.accio.com/work/doc?slug=agent-memory-guide

## Existence and status

- Accio Work is a released local-first desktop AI agent product.
- Its official page lists macOS and Windows desktop support and identifies Electron as the desktop runtime.
- The official product page displayed release `v0.17.0` dated 2026-06-18 when verified.

## Verified capability baseline

- Agent execution can use local files, terminal commands, browser automation, external APIs, and MCP.
- Agent profiles can carry roles, instructions, models, tools, workspaces, Skills, and Plugins.
- Automations support natural-language or form-based schedules, pause/resume, manual runs, run history, and missed-run reconciliation after restart.
- Permissions are session/workspace scoped and cover filesystem paths, terminal commands, browser actions, and connectors.
- The public permission model includes automatic allow, ask, remembered choice, and deny behavior.
- Agent memory includes a local searchable index, diary-style records, `MEMORY.md`, and identity/user files.
- Agent Teams can use a lead agent and member agents for delegated work.

## Implementation boundary for OPHIRA

- Replicate the observable product mechanisms and business workflow, not Accio Work branding, copy, visual assets, source code, or pixel-level interface.
- OPHIRA event streaming must expose SDK-observable status, assistant output, tool lifecycle, permission, task/subagent, usage, and result events.
- Private model chain-of-thought is not an application event. When the API provides a user-visible reasoning summary, it may be forwarded as a labeled summary event.

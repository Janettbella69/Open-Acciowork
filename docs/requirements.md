# Requirement inventory

> **Superseded and corrected (2026-09-02).** This file was originally a 2026-08-05
> implementation snapshot. Its fixed-role Opportunity/Demand/Supply/Capital/Sales Agent
> inventory is no longer a requirement and must not be used as an implementation plan.
> The authoritative inventory is the FR-01–FR-28 table in
> `OPHIRA-PRD-and-Technical-Architecture-v2.md` §3.8.

Updated: 2026-09-04

| Binding correction | Current requirement |
| --- | --- |
| Agent topology | One main Agent owns the result. There is no fixed-role Agent team or fixed business workflow. |
| Ordinary-mode delegation | Rare SDK-native, one-off, read-only branches are allowed only when context isolation is worthwhile. Code denies subagent ledger writes, Artifact registration, automation commitments, and further delegation. |
| Deep Research | Exactly three read-only research subagents; one shared Run budget; unified cancellation; wall-clock timeout; no turn-count limit; only the main Agent writes project truth. |
| Runtime | Claude Agent SDK remains the only core model runtime; direct model clients and hand-written tool loops are prohibited. |
| Status semantics | “Implemented” means local code and automated-test coverage only. It does not prove a real Provider, browser, connector, IM channel, mobile push, media generator, or Office-rendering E2E. |
| Current major gaps | The current nine-case DeepSeek Provider contract matrix passed its seven core cases on the real endpoint with SDK 0.2.143 on 2026-09-04; vision and invalid-token fast failure remain explicitly degraded. The archived `greenpath-20260904T082202Z` bundle passed three consecutive real DeepSeek rounds, including actual Skill invocation, full native-tool coverage, verified same-Run Web Evidence, SSE replay, and session resume. Browser L1/L2 real-browser smoke acceptance passed on 2026-09-03, but L3 is absent and the browser security edge-case matrix remains primarily deterministic/offline. A reviewed `send_message` HTTPS JSON adapter is implemented locally, but no authorized sandbox/test-account E2E has proved a real commercial side effect. Feishu reply delivery and durable channel-to-principal/project binding are implemented locally, but no real-tenant E2E has run and WeChat is absent. Trusted-gateway principals and project membership exist, while global stores remain physically unpartitioned and `/api/auth/status` correctly reports `multi_tenant=false`. The autonomous standing-intent watcher is locally closed, but real data-source quality and long-running delivery remain unproved. Mobile still lacks authenticated pairing/push; media lacks a configured real generator; Office lacks LibreOffice visual QA. |

Historical rows were removed from this file so search results cannot present superseded
fixed-role Agents or obsolete delivery claims as current requirements. Git history remains the
source for the 2026-08-05 snapshot.

## Review fixes — 2026-09-05

These scoped fixes supplement the FR inventory; they do not certify the remaining
commercial, channel, media, or production acceptance requirements.

| Requirement | Status | Verification |
| --- | --- | --- |
| Reject untrusted Host/Origin requests before local API reads or writes | Delivered | Tests cover bodyless writes, host rebinding inputs, opaque/duplicate origins, Referer/Fetch metadata, CORS preflight, IPv4/IPv6 and legitimate frontend/CLI requests. |
| Keep delayed project/run responses and SSE callbacks within their selected conversation | Delivered | Hook regression tests cover project refresh, terminal refresh, delayed creation/discovery, cancellation, selection changes and unmount; workspace notices and Provider selection also check navigation ownership. |
| Recover interrupted runs without replaying work or interrupting a live worker | Delivered | Startup/API tests cover queued/running/waiting/paused recovery, permission expiry, preserved artifacts/session, SSE replay and retry, cancellation, idempotence, competing worker leases, shutdown and interrupted event persistence. |

Validation: backend 585 passed / 8 live-only skipped; frontend 97 passed; Ruff,
ESLint, TypeScript and production build passed. The existing bundle-size warning
remains. Real Provider and external connector E2Es were not rerun for these fixes.

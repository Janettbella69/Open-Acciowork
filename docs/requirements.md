# Requirement inventory

> **Superseded and corrected (2026-09-02).** This file was originally a 2026-08-05
> implementation snapshot. Its fixed-role Opportunity/Demand/Supply/Capital/Sales Agent
> inventory is no longer a requirement and must not be used as an implementation plan.
> The authoritative inventory is the FR-01–FR-28 table in
> `OPHIRA-PRD-and-Technical-Architecture-v2.md` §3.8.

Updated: 2026-09-03

| Binding correction | Current requirement |
| --- | --- |
| Agent topology | One main Agent owns the result. There is no fixed-role Agent team or fixed business workflow. |
| Ordinary-mode delegation | Rare SDK-native, one-off, read-only branches are allowed only when context isolation is worthwhile. Code denies subagent ledger writes, Artifact registration, automation commitments, and further delegation. |
| Deep Research | Exactly three read-only research subagents; one shared Run budget; unified cancellation; wall-clock timeout; no turn-count limit; only the main Agent writes project truth. |
| Runtime | Claude Agent SDK remains the only core model runtime; direct model clients and hand-written tool loops are prohibited. |
| Status semantics | “Implemented” means local code and automated-test coverage only. It does not prove a real Provider, browser, connector, IM channel, mobile push, media generator, or Office-rendering E2E. |
| Current major gaps | Browser L1/L2 real-browser smoke acceptance passed on 2026-09-03, but L3 is absent and the browser security edge-case matrix remains primarily deterministic/offline. A reviewed `send_message` HTTPS JSON adapter is implemented locally, but no authorized sandbox/test-account E2E has proved a real commercial side effect. Feishu reply delivery and durable channel-to-principal/project binding are implemented locally, but no real-tenant E2E has run and WeChat is absent. Trusted-gateway principals and project membership exist, while global stores remain physically unpartitioned and `/api/auth/status` correctly reports `multi_tenant=false`. The autonomous standing-intent watcher is locally closed, but real Provider/data-source quality and long-running delivery remain unproved. Mobile still lacks authenticated pairing/push; media lacks a configured real generator; Office lacks LibreOffice visual QA; real Provider Skill invocation still lacks a current Green Path proof. |

Historical rows were removed from this file so search results cannot present superseded
fixed-role Agents or obsolete delivery claims as current requirements. Git history remains the
source for the 2026-08-05 snapshot.

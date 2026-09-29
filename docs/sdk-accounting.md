# Native streaming cost and resumed budgets

The pinned `claude-agent-sdk==0.2.143` CLI reports `total_cost_usd` and
`model_usage` cumulatively within one CLI invocation. Its `usage` counters are
per turn. Adding every Result's cost double-counted prior turns after live user
directions: the native estimates `$0.0045 → $0.0135` became `$0.018` in the ledger.

The adapter now freezes the total from earlier CLI invocations on connection.
Each new Result replaces the current invocation's cumulative cost/model usage,
while turn usage continues to accumulate. A new CLI used for durable approval
resume adds its costs to that frozen earlier total. Run lists and the resumed
native `max_budget_usd` use the corrected Run total. Historical events are not
rewritten, and unavailable estimates remain unknown.

The CLI's `/clear`, `/reset` and `/new` commands reset both its session and
budget. They are rejected at the live-direction API and again before injecting
old persisted directions. Ordinary prose mentioning these strings is allowed;
users can start another task with a separately frozen budget.

Reproduce the real SDK/CLI acceptance without an external model account:

```sh
PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_sdk_accounting_e2e.py \
  --report /tmp/ophira-sdk-accounting.json
```

The runner uses a temporary ledger and loopback synthetic Anthropic SSE responses.
It retains real RunManager/workspace leases, native SDK hooks, a deferred Write,
same-Run/same-session approval resume and API task-list reads. The three cases
check live directions, cost accumulation across native resume and exhaustion of
the remaining budget. It also checks management-command rejection and direct CLI
child cleanup. The retained report is
[`sdk-accounting-20260929.json`](../evals/reports/sdk-accounting-20260929.json).

The budget is the SDK's estimated-cost stopping rule, not a prepaid hard billing
cap: a completed request can take the total above the threshold before the next
step stops. The `$0.02` test intentionally ends failed at an observed `$0.027`
with `error_max_budget_usd`, preserving the already-written file. This acceptance
does not certify real Provider billing, generation quality, concurrent load,
browser behavior or real-time reporting before a native Result arrives.

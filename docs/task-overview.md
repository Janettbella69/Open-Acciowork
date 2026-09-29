# Independent task overview

FR-32 lists the current principal's Runs across visible projects. A new task
detaches the workspace from its current Run without cancelling that Run. The
task list refreshes every five seconds and links to the exact project and Run
so approvals and saved results can be reopened.

The server filters projects by `RUN_READ` before returning any Run data. It
shows at most the 50 newest visible Runs, reads at most 512 KiB of events per
Run, and caps event replay for one request at 8 MiB or 10,000 events. The
response reports when older Runs or event observations were omitted. The
workspace accepts a task budget below the operator's configured ceiling; an
empty choice uses that ceiling. The API freezes the selected amount in the Run
before starting the SDK, and the SDK's native `max_budget_usd` enforces the
remaining per-Run amount after applying the provider's configured cost scale.
Each row shows that frozen task budget, the SDK threshold when it differs,
pending approval count, and SDK reported estimated cost when a result event
contains one. A dash means that the cost
or a historical Run's budget is not available; it is not an estimate of zero.
SDK cost is a client-side estimate, not billing data. Prompts, project
names, and result excerpts pass through redaction before being returned.

The [native accounting acceptance](sdk-accounting.md) additionally verifies
multiple Result messages during live directions, same-session approval resume
and the remaining native budget, using the pinned real CLI with a local synthetic
transport. These are SDK estimates, not certified Provider charges.

Local tests cover membership filtering, budget selection/default/operator
ceiling, SDK budget scaling, persistence after restarting the ledger, cost and
approval display, bounds, and exact Run navigation. Real
concurrent SDK Runs, live cost reporting, budget enforcement under concurrent
load, and browser interaction still need separate acceptance.

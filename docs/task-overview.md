# Independent task overview

FR-32 lists the current principal's Runs across visible projects. A new task
detaches the workspace from its current Run without cancelling that Run. The
task list refreshes every five seconds and links to the exact project and Run
so approvals and saved results can be reopened.

The server filters projects by `RUN_READ` before returning any Run data. It
shows at most the 50 newest visible Runs, reads at most 512 KiB of events per
Run, and caps event replay for one request at 8 MiB or 10,000 events. The
response reports when older Runs or event observations were omitted. Each row
shows the independent Run budget, pending approval count, and SDK reported
estimated cost when a result event contains one. A dash means that the cost
or budget is not available; it is not an estimate of zero. Prompts, project
names, and result excerpts pass through redaction before being returned.

Local tests cover membership filtering, persistence after restarting the
ledger, cost and approval display, bounds, and exact Run navigation. Real
concurrent SDK Runs, live cost reporting, budget enforcement under concurrent
load, and browser interaction still need separate acceptance.

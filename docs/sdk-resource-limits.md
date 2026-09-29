# SDK execution and output limits

PRD §3.9 requires bounded execution resources. The application now limits active
SDK execution time and SDK-observed output for ordinary, plan, Deep Research,
scheduled, event-triggered and manual-compaction Runs. These limits supplement
the native `max_budget_usd` fuse; ordinary runs still have no default turn cap.

| Operator setting | Default | Boundary |
|---|---:|---|
| `OPHIRA_SDK_ACTIVE_TIMEOUT_SECONDS` | 7,200 seconds | Active execution across invocations of the same Run |
| `OPHIRA_SDK_OUTPUT_LIMIT_BYTES` | 67,108,864 | Normalized SDK message JSON plus stderr UTF-8 |
| `OPHIRA_SDK_MESSAGE_BUFFER_BYTES` | 1,048,576 | SDK native `max_buffer_size` for one CLI message |

The first execution freezes these settings in `RunRecord.sdk_resource_usage`.
Restoring a native deferred approval uses the same frozen limits and accumulated
usage. Time between completed suspension and approval is excluded. Deep Research
also retains its existing original wall-clock deadline, including its wait time;
whichever remaining deadline is earlier stops the active session.

Active time uses a monotonic clock. Start and stop accounting is durable; when an
owner disappears before closing its accounting interval, a later invocation
conservatively charges the intervening wall-clock interval. A new Run, including
an explicit retry or next conversation turn, receives its own allowance.

Output is checked before application event mapping. Streamed deltas, final
messages, child messages, tool payloads present in those messages, and stderr all
count; repeated content in multiple SDK messages counts multiple times. Usage is
reserved on disk in 64 KiB blocks before admission. Unused reservation space is
not refunded, so an interrupted or suspended invocation can cost up to 64 KiB
extra, but restarting cannot replenish its allowance. Only numeric usage is
stored by this accounting code; it does not persist private thinking or stderr.

`max_buffer_size` is delegated to the pinned SDK. Despite its documented byte
name, version 0.2.143's Python framer checks decoded string length. This setting
therefore bounds message characters in that implementation, while the cumulative
application allowance explicitly counts UTF-8 bytes. Preflight uses the same
native message-buffer setting and its existing separate timeout; it does not
create a project Run or use the cumulative Run quota.

In a project Run, exceeding an application limit or the native message buffer produces
`SDK_RESOURCE_LIMIT_EXCEEDED`. The Run manager preserves prior evidence and
registers supported workspace artifacts through the existing failure path.
Session cancellation uses an AnyIO cancel scope so the SDK's own shielded CLI
shutdown can finish. Repeated cancellation waits for cleanup, keeping the Run and
workspace leases until it completes. SDK cleanup can add seconds after the limit
fires; this is not a promise that process exit occurs exactly at the deadline.

## Verification

Run the offline unit/integration suite:

```sh
PYTHONPATH=backend/src .venv/bin/python -m pytest backend/tests/test_sdk_resources.py
```

The native acceptance script uses the installed SDK and its real CLI subprocess,
native startup hooks, temporary ledgers, API status reads and artifact downloads.
Only the Anthropic HTTP response is replaced by a loopback SSE fixture:

```sh
PYTHONPATH=backend/src .venv/bin/python backend/scripts/run_sdk_resource_e2e.py \
  --report evals/reports/sdk-resources-20260927.json
```

It checks normal completion, cumulative output exhaustion, a single oversized
message, active timeout, retained artifact bytes and no remaining direct SDK
child process. No provider credentials, real model service or paid request is used.

Revalidated on 2026-09-29: all four native CLI cases passed; the report is
[`sdk-resources-20260929.json`](../evals/reports/sdk-resources-20260929.json).
The first sandboxed attempt could not bind the loopback test server. The successful
run used an environment allowing local sockets, with the same synthetic transport.

## Remaining PRD requirements

This is application-level accounting, not an OS isolation boundary. It does not
cap total process RSS, CPU, process count, disk growth, SDK transcript size or
raw network traffic; SDK control responses and in-process tool work are not all
represented in the application message stream. OS-enforced worker/container
limits, detached descendant containment, production concurrency/latency and
real Provider fault matrices remain unverified or unimplemented. Do not enable
multi-tenant hosting on the strength of this change.

Design references: the local [SDK deployment snapshot](claude-agent-sdk/secure-deployment.md),
[native API reference](claude-agent-sdk/python.md), and installed 0.2.143
`SubprocessCLITransport.close()` cancellation contract. The official
[deployment guidance](https://code.claude.com/docs/en/agent-sdk/secure-deployment)
places CPU, memory and process limits at the OS/container layer; Linux
[cgroup v2](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)
is a separate resource-control mechanism, not exercised by this macOS validation.

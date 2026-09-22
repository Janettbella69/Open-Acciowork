# SDK worker resource limits

PRD §3.9 requires bounded CPU, memory, processes, time and output for Bash workers.
The implementation uses the pinned Claude Agent SDK's public `cli_path` and
`max_buffer_size` options. `ClaudeSDKClient` still owns the model protocol, native
tools, hooks, approval replay and session lifecycle. The executable wrapper only
relays bytes and supervises the native CLI process.

## Deployment modes

`OPHIRA_WORKER_LIMITS` is one validated JSON setting. Missing keys retain defaults:

```dotenv
OPHIRA_WORKER_LIMITS='{"mode":"systemd","cpu_percent":200,"memory_bytes":2147483648,"tasks_max":128,"timeout_seconds":14400,"output_bytes":33554432,"frame_bytes":4194304}'
```

| Setting | Default | Meaning |
|---|---:|---|
| `mode` | `native` | Local supervision, or required Linux `systemd` enforcement |
| `cpu_percent` | 200 | Linux aggregate CPU quota; 100 is one CPU |
| `memory_bytes` | 2147483648 | Linux hard memory limit for the scope |
| `tasks_max` | 128 | Linux maximum processes **and threads** in the scope |
| `timeout_seconds` | 14400 | Original Run wall-clock deadline, including approval waits |
| `output_bytes` | 33554432 | Cumulative CLI stdout plus stderr allowance across Run segments |
| `frame_bytes` | 4194304 | SDK maximum buffered stdout frame size |

`native` works on the local macOS environment. It bounds wall time, SDK buffered
frames and observed output, watches for host exit, and kills its own child process
group on shutdown. It does **not** enforce aggregate CPU, memory or task quotas,
and cannot guarantee cleanup of descendants that create another process session
or survive a hard kill of the supervisor. The UI says so for CPU/memory/process
limits. Use the Linux mode when those limits are required.

`systemd` requires Linux, systemd 254 or newer, a reachable user manager, and
delegated cgroup v2 CPU, memory and pids controllers. It creates a generated,
per-segment user scope with CPUQuota, MemoryMax, MemorySwapMax=0, TasksMax,
RuntimeMaxSec, KillMode=control-group and OOMPolicy=kill. No privileged launcher
or shell evaluation of SDK arguments is used. Argument environment expansion is
explicitly disabled, and the unit description is fixed instead of echoing argv.

Before executing the native CLI, the gate reads its actual cgroup membership and
`cpu.max`, `memory.max`, `memory.swap.max`, `pids.max`, and `memory.oom.group`.
Missing controllers, a different scope, or weaker limits stop startup. There is
no automatic fallback to native mode. Scope cleanup kills the complete cgroup,
including descendants that created their own process sessions, and checks that
the unit is inactive, failed or absent. Unconfirmed cleanup is an error.

The existing SDK sandbox remains enabled with `failIfUnavailable=True` and
`allowUnsandboxedCommands=False`. Sandbox paths additionally deny the worker
control directory and, in Linux mode, cgroup and systemd control paths. The CLI
receives only basic execution variables plus the selected SDK environment;
unrelated inherited credentials and the host's systemd bus environment are removed.
Provider credential values never enter the launch-plan file or supervisor reports.

These controls cover the native CLI and its process descendants. In-process MCP
handlers and other services in the API host remain outside the scope. This does
not establish physical tenant isolation, protect against a compromised host user
or kernel, or replace the remaining production runner and deployment acceptance.

## Persistence and observations

The first supervised segment freezes its limits and deadline in the Run ledger.
Deep Research retains the earlier of its existing deadline and the worker
deadline. Deferred approval resumes reuse the original deadline and remaining
output allowance; changing deployment settings does not replenish either.
Each launch first persists its segment claim. A host crash or missing/invalid
report prevents a subsequent segment from obtaining a fresh allowance. The user
must start a new Run; unobserved bytes are never reported as measured usage.

The supervisor relays stdout unchanged until the output limit. Stderr is counted
but withheld because it may contain credentials or raw tool data. Both streams
are read in bounded chunks. The observed count may exceed the cap by at most two
chunks (one per reader); excess chunks are not forwarded. This allowance measures
protocol output, not artifact file sizes or model token usage. Byte totals enter
the ledger when a segment ends, so the API/UI explicitly exclude the current
segment's unfinished count.

The SDK's own `-v` executable probe retains its built-in two-second timeout and
runs before the selected SDK environment is supplied; it is not a Run segment.
All regular CLI sessions and Provider preflight sessions use the supervisor.

`GET /api/runs/{id}/resources` exposes frozen worker limits, known cumulative
output, an unconfirmed-total flag, the last outcome, and whether kernel limits
were verified at startup. Linux configuration and verified enforcement are shown
separately. Standard runs have an expandable resource panel; Deep Research keeps
its existing shared budget, deadline and native task observations.

Credential-free `started.json` and `finished.json` reports live in private
`data/runtime-workers/worker-*` directories. Normal host cleanup removes the
executable wrapper and launch plan. Crash recovery, retention policy and capacity
management for abandoned control directories remain deployment work; do not delete
these directories while workers are active.

## Verification

The deterministic suite uses real subprocesses for byte preservation, environment
filtering, output floods, wall-clock termination, host exit, descendant cleanup,
and installed SDK transport compatibility with an explicitly synthetic CLI. It
does not call a model or claim live Provider compatibility.

```bash
PYTHONPATH=backend/src .venv/bin/python -m pytest \
  backend/tests/test_worker_envelope.py backend/tests/test_run_resources.py -q
```

The separate `worker-resources` CI job provisions an unprivileged systemd user
manager on a disposable Ubuntu 24.04 runner. Its explicit acceptance mode fails,
rather than skips, if the required environment is absent. It tests actual CPU
throttling, OOM termination, process-count rejection, detached descendant cleanup,
and refusal to launch without the user manager. Local macOS runs skip these
Linux-only cases. A separate case starts the actual pinned CLI with `--help` in
the scope without invoking a model. Live model calls, native sandbox integration,
long-duration fault tests and production load remain separate acceptance work.

The missing-manager test makes both the private manager socket under
`XDG_RUNTIME_DIR` and the user bus unreachable. Setting only
`DBUS_SESSION_BUS_ADDRESS` does not simulate this failure: systemd first tries
the private socket, as shown in its official
[connection implementation](https://github.com/systemd/systemd/blob/v255/src/shared/bus-util.c#L217-L249).

Official references: SDK [Python API](claude-agent-sdk/python.md) and
[secure deployment](claude-agent-sdk/secure-deployment.md); systemd v255
[systemd-run](https://github.com/systemd/systemd/blob/v255/man/systemd-run.xml),
[resource control](https://github.com/systemd/systemd/blob/v255/man/systemd.resource-control.xml),
and [scope lifecycle](https://github.com/systemd/systemd/blob/v255/man/systemd.scope.xml).

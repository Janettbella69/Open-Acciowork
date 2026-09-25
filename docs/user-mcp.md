# FR-33 remote MCP connectors (local implementation)

Remote MCP connectors belong to one project. The connector page offers the
official Claude Code documentation MCP as the initial catalog entry. Adding it
performs a live MCP `initialize` and paginated `tools/list` review in one action.
The advanced form accepts a Streamable HTTP HTTPS address and an optional bearer
token. A custom hostname must first be added to the organization allowlist by
an owner or administrator (in local-only mode, by the local operator).

Saving or editing a connector disables it. Review pins each outbound request to
a DNS-validated public IP, keeps the original Host and TLS SNI, rejects redirects,
and permits only a bounded tool list with safe names. Failed or overlapping
reviews leave the connector disabled. The bearer token is kept in the server-side
secret vault and never returned by the API. A revoked connector loses its token.

Reviewed tools default to **ask**. Each tool can be set to **allow**, **ask each
time**, or **deny**. Ask decisions use the existing durable SDK approval path and
cannot create persistent grants. The native `PreToolUse` hook denies unknown,
revoked, changed, policy-blocked, and subagent calls. `PostToolUse` sanitizes
remote results, attaches the untrusted-data notice, and replaces the
model-visible result. A Run records a credential-free snapshot of reviewed
connectors. The SDK receives eligible servers through `mcp_servers` as
in-process MCP gateways, with `strict_mcp_config=True` and `setting_sources=[]`.
The Claude Code worker receives neither remote URL nor bearer token. Every
runtime `tools/call` opens a pinned, redirect-free HTTPS connection, checks that
the remote tool schema still matches the reviewed version, and returns only the
reviewed tool output. Remote tool descriptions are replaced with neutral tool
text and instruction-like schema descriptions are removed. Plan, maintenance
compaction, and standing-intent Runs receive no user MCP servers.

## Current limits

- The built-in directory contains one official server. More entries need
  verified endpoints, authentication, tool review, and product-specific E2E.
- Live review and an actual Agent call to the official server have not been
  exercised in this workspace. Tests stub the remote MCP exchange. A direct
  handshake attempt on 2026-09-24 failed closed because this machine's DNS
  returned a non-public address for `code.claude.com` (including outside
  the filesystem sandbox).
- Production deployments still need their normal outbound network policy for
  the Provider, browser, and other tools. The user MCP gateway itself pins and
  checks every remote request. Live compatibility of stateful remote servers
  needs acceptance testing; the gateway currently starts a fresh MCP session
  for each tool call.
- The installed MCP Python SDK 1.29.0 advertises protocol `2025-11-25`, and
  the gateway uses an `initialize` exchange for each call. The [2026-07-28 MCP
  specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)
  introduced a handshake-free HTTP path; the [Python SDK v2 migration
  guide](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/migration.md)
  describes that compatibility boundary. A server requiring only the new path
  has not been certified here. Moving the gateway to SDK v2 also requires
  checking the Claude Agent SDK's in-process MCP API and Provider contracts,
  so it remains deferred.
- OAuth authorization and token refresh are not implemented; the advanced form
  accepts a user-supplied bearer token only.

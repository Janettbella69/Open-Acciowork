# FR-33 remote MCP connectors (local implementation)

Remote MCP connectors belong to one project. The connector page offers the
official Claude Code documentation MCP as the initial catalog entry. Adding it
negotiates the server's MCP version and performs a paginated `tools/list` review
in one action. The gateway supports the handshake-free `2026-07-28` path and
falls back to the legacy `initialize` exchange for older servers.
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
runtime `tools/call` uses a pinned, redirect-free HTTPS connection. Calls in
one Run share a remote MCP session, while separate Runs have separate sessions;
every call checks that the remote tool schema still matches the reviewed
version and returns only the reviewed tool output. Remote tool descriptions
are replaced with neutral text and instruction-like schema descriptions are
removed. Plan, maintenance
compaction, and standing-intent Runs receive no user MCP servers.

## Current limits

- The built-in directory contains one official server. More entries need
  verified endpoints, authentication, tool review, and product-specific E2E.
- Live review and an actual Agent call to the official server have not been
  exercised in this workspace. Offline HTTP tests run real MCP client and
  server exchanges for both protocol paths, including pinned requests and a
  tool call. A direct connection attempt on 2026-09-24 failed closed because
  this machine's DNS returned a non-public address for `code.claude.com`
  (including outside the filesystem sandbox).
- Production deployments still need their normal outbound network policy for
  the Provider, browser, and other tools. The user MCP gateway itself pins and
  checks every remote request. A legacy stateful HTTP server has passed offline
  protocol tests with one session per Run, but live compatibility with public
  stateful servers still needs acceptance testing. A failed tool call is never
  retried automatically; a later call can open a new session.
- The gateway uses MCP Python SDK 2.2 with automatic version negotiation for
  the [2026-07-28 handshake-free protocol](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)
  and older servers, following the [official v2 migration
  guide](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/migration.md).
  The pinned Claude Agent SDK 0.2.143 accepts the v2 in-process server in
  offline modern and legacy tests. A public server requiring only the new path
  and a real Provider run still need acceptance testing.
- OAuth authorization and token refresh are not implemented; the advanced form
  accepts a user-supplied bearer token only.

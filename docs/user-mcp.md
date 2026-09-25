# FR-33 remote MCP connectors (local implementation)

Remote MCP connectors belong to one project. The directory offers the official
Claude Code documentation MCP and HubSpot's hosted CRM MCP. Adding Claude Code
negotiates the server's MCP version and performs a paginated `tools/list` review
in one action. Adding HubSpot creates a disabled OAuth entry; its tool review
occurs only after the user supplies a HubSpot MCP client and completes consent.
The gateway supports the handshake-free `2026-07-28` path and
falls back to the legacy `initialize` exchange for older servers.

## HubSpot CRM directory entry

HubSpot's [MCP connector guide](https://developers.hubspot.com/docs/apps/developer-platform/build-apps/integrate-with-the-remote-hubspot-mcp-server)
documents `https://mcp.hubspot.com/`, a HubSpot-created OAuth client and PKCE.
In **Plugins → Connectors → MCP connectors**, select the project and add
**HubSpot CRM**. Copy the displayed callback URL into the MCP connector in
HubSpot Development → MCP Connectors. Paste its client ID and secret into the
project entry, then select **Connect with OAuth** and approve access in HubSpot.
The callback reviews the current tool list before enabling the entry. Each
reviewed tool defaults to **ask** and can be changed individually.

This directory entry exposes only the named CRM lookup tools in
`HUBSPOT_READ_TOOLS`; HubSpot's mutation and unknown tools are excluded from
review and Run registration. The [HubSpot connector listing](https://claude.com/marketplace/connectors/hubspot)
lists `search_crm_objects`, `get_crm_objects`, `get_user_details` and other lookups.
The allowlist is rechecked on each runtime tool call. Tool output remains
untrusted and can receive a Run-bound source receipt, which proves the server
response, not the accuracy of a CRM record. The [HubSpot Postman collection](https://www.postman.com/hubspot/hubspot-public-api-workspace/collection/682b44c55ac051812a5ee1de)
uses `app.hubspot.com/oauth/authorize` and
`api.hubspot.com/oauth/v3/token`; the pinned transport permits exactly these
cross-host paths for this catalog entry, forbids Bearer forwarding there, and
still requires HTTPS/public DNS and no redirects. Other remote MCP connectors
retain their issuer-host restriction. A browser callback origin must be
configured before connecting. A live HubSpot account and Provider Run have not
yet been used to verify the flow.

## Advanced connector form

The advanced form accepts a Streamable HTTP HTTPS address, an optional bearer
token, or an OAuth issuer URL. It also accepts a pre-registered OAuth client ID
and, for confidential clients, a secret with Basic or form-body token endpoint
authentication. The form shows the exact callback URI to register with the
authorization server. A custom MCP or OAuth issuer hostname must first
be added to the organization allowlist by an owner or administrator (in
local-only mode, by the local operator). Saving an OAuth connector keeps it
disabled. **Connect with OAuth** returns a short-lived authorization link; the
browser callback completes the SDK's authorization-code/PKCE exchange and a
live tool-list review before enabling it. The link can be retrieved again while
authorization is pending. An interrupted process or rejected authorization
leaves the connector disabled; start the connection again.

Saving or editing a connector disables it. Review pins each outbound request to
a DNS-validated public IP, keeps the original Host and TLS SNI, rejects redirects,
and permits only a bounded tool list with safe names. Failed or overlapping
reviews leave the connector disabled. Bearer tokens, OAuth access/refresh tokens,
pre-registered client secrets, dynamic client registration, and reviewed
authorization-server metadata stay in the server-side secret vault and never
enter the API response or SDK worker.
A revoked connector loses those credentials. The OAuth issuer must match the
resource server's advertised issuer and organization policy. The browser may
visit only an authorization URL on that issuer host. The gateway accepts OAuth
metadata and token requests only on the approved resource/issuer hosts and
prevents Bearer tokens from reaching anything but the reviewed MCP endpoint.
Expired tokens are refreshed through the pinned issuer endpoint before a new
Run uses them. The SDK's native [OAuth client flow](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/oauth-clients.md)
performs discovery, client selection, PKCE/state/issuer validation, exchange, and
refresh; the host binds destinations, storage, callback, and review.

In `local_only` mode the callback is the local backend's loopback address. In
`trusted_gateway` mode set `OPHIRA_MCP_OAUTH_PUBLIC_ORIGIN` to the exact public
HTTPS control-plane origin. Route `/api/mcp/oauth/callback`,
`/api/mcp/oauth/complete`, and `/api/mcp/oauth/client-metadata` there without
the gateway's normal identity header; restrict all three to that exact host.
The last path publishes a fixed public JSON Client ID Metadata Document with
the callback URI and no secret. When an authorization server advertises
`client_id_metadata_document_supported`, the SDK uses that HTTPS document URL
as its client ID when no pre-registered client was supplied. Pre-registered
clients always take precedence. If neither is available, the SDK falls back to
dynamic registration. The metadata URL is unavailable in local-only
mode because the document requires public HTTPS.
The callback requires the one-time SDK state and is redirected to a URL without
the authorization code. The backend redacts that callback's query string from
Uvicorn access logs; the fronting gateway must also omit its query string from
access logs. Management routes still require project authorization.
The backend process must remain up during the five-minute browser grant.

Reviewed tools default to **ask**. Each tool can be set to **allow**, **ask each
time**, or **deny**. Ask decisions use the existing durable SDK approval path and
cannot create persistent grants. The native `PreToolUse` hook denies unknown,
revoked, changed, policy-blocked, and subagent calls. `PostToolUse` sanitizes
remote results, attaches the untrusted-data notice, and replaces the
model-visible result. A non-error main-Agent result from a still-approved tool
also receives a host-issued `tool` source receipt. The receipt binds the exact
sanitized result to the Run/session, reviewed connector revision, tool name,
endpoint URL, and a hash of the call arguments. The Agent can record evidence
with that receipt; the endpoint identifies the MCP server, not the original
source of any claim the server returned. Failed, delegated, unknown, or revoked
tool calls receive no receipt. A Run records a credential-free snapshot of reviewed
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

- The built-in directory contains two official servers. The HubSpot entry has
  an offline OAuth and read-tool review test; real account consent, current
  tool list, CRM reads, Provider use and browser acceptance remain open. Other
  CRM/social integrations also remain open.
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
- OAuth has an offline end-to-end HTTP test covering discovery, dynamic
  registration, Client ID Metadata Documents, pre-registered public and
  confidential clients, browser callback, PKCE, token exchange/refresh, tool
  review/call, issuer policy, and revocation. A real public OAuth MCP/Provider
  and deployed trusted-gateway callback still need acceptance testing.
- Remote MCP receipts prove what the reviewed server returned to this Run after
  sanitization. They do not verify the server's underlying sources or factual
  claims. Live server and Provider acceptance remains pending.

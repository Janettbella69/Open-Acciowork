# Connector and project MCP credential export

**Connectors → Connector credential downloads** lists each runtime connector. A
tenant owner or administrator selects **Export credential…** and then confirms
**Download credential file**. The POST-only endpoint
`/api/connectors/{connector_id}/credential/export` requires
`{"confirmation":"export"}`. It returns an unencrypted JSON attachment with
connector metadata and vault-managed static secret and active OAuth token, when
present. OAuth login requests still in progress are excluded. Normal connector
list, save, health, and revoke responses never include vault values. A missing
vault credential returns 409. A mailbox token bound to another tenant cannot be
exported. Host environment variables are outside the application vault: the
export includes only the variable **name**, never its process value.

Each project MCP card offers the same two-step download when a credential is
configured. The endpoint
`/api/projects/{project_id}/mcp/{connector_id}/credential/export` requires the
project export permission, which belongs to its owner or administrator. The
attachment contains only that project's MCP connector metadata and stored
bearer or OAuth vault entries. OAuth entries are raw JSON strings so the file
preserves exactly what the vault holds. A project member with write access
cannot export the raw credential. Both export endpoints send
`Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.

**Revoke** disables the connector and clears its local vault values. This also
clears pending OAuth authorization, tokens, and related local subscription
state for supported runtime connectors. Project MCP revocation clears its
bearer token and OAuth client, token, preset, and metadata entries. Already
running work can retain credentials in memory until it ends. Remote providers
may keep issued tokens valid; revoke them at the provider when needed. Removing
a host environment credential requires an operator to remove its variable and
restart the service. The application cannot erase browser downloads, copied
files, or older backups.

Registry operations use a cross-process lock. Changing a runtime connector's
type, endpoint, or environment reference cannot carry a vault secret to the new
authority. If a host environment reference would be reused, a new credential
or different reference is required. A failed key write leaves the previously
configured connector disabled.
Both vaults now maintain a private deletion count and hash chain for each removed
local value, including replaced credentials and OAuth state. An interrupted delete
is resumed before the vault is used again. Cold restore compares these registers
with a supplied latest stopped data directory, rejecting a snapshot from before a
deletion. Earlier backups, host environment variables, and remote issued tokens
still require their own cleanup.

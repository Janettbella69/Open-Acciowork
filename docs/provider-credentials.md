# Provider key export and revocation

**Settings → Model providers** shows each configured endpoint and its operator-entered
region. Selecting a Provider sends the task prompt and approved tool context to that
endpoint. The displayed region is configuration, not verification of where the
Provider stores or processes data; review its actual storage and transfer terms.

An organization owner or administrator can select **Export key…** and then
**Download key file**. The POST-only endpoint
`/api/providers/{provider_id}/credential/export` requires
`{"confirmation":"export"}` and returns one JSON attachment containing the
Provider configuration and its key. It uses `Cache-Control: no-store` and does not
include the key in normal list, save, or preflight responses. The file is
**unencrypted**; keep it private and delete any browser download copy separately.
Local-only mode uses the same endpoint under its local operator boundary.

**Revoke key…** requires a second click and sends
`{"confirmation":"revoke"}` to the corresponding `/revoke` endpoint. New Runs
cannot use that Provider until a new key is saved and preflighted. Already
started Agent workers may retain the credential in their process environment
until they finish. The Provider profile remains, but its saved verification is
cleared. A key supplied by the host's `ANTHROPIC_AUTH_TOKEN` or
`ANTHROPIC_API_KEY` cannot be durably revoked in the UI: remove the host
environment variable first, then restart the service.

Provider profile changes, key export, and revocation share a cross-process
registry lock; the private vault has its own cross-process file lock. A profile
requires a new key when its endpoint or auth variable changes. During
a key replacement the old profile is disabled before the vault is changed; a
write failure leaves it disabled until the administrator successfully saves the
new key. This keeps an old key from being used at a newly configured endpoint.
Every local vault deletion advances a private, content-free deletion register.
Interrupted deletions are completed before the vault is used again. A cold restore
with the latest stopped data directory compares this register and rejects an older
snapshot that could reintroduce a revoked key. The check cannot erase older backups,
recover deletions made before this register existed, or revoke a host environment key.
Runtime connector and project MCP credentials have their own export and revoke
flows in [connector-mcp-credentials.md](connector-mcp-credentials.md). Browser
login profiles and credentials outside the application vault remain separate
data-sovereignty work in the [PRD delivery inventory](prd-delivery-inventory.md).

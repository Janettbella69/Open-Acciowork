# Deployment-owner credentials

PRD §3.9 credential portability also covers the key that authenticates the trusted
identity gateway. Only the local deployment operator manages it. HTTP owner/admin
roles cannot list, export, delete or restore deployment credentials, because possession
of the gateway key permits signing identities. The ordinary service-credential panel
continues to manage Feishu and Web Push separately.

The local command manages two explicit identifiers:

| Identifier | Configuration | Effect |
|---|---|---|
| `trusted_gateway` | `OPHIRA_TRUSTED_GATEWAY_AUTH_SECRET` | Required for trusted-gateway authentication; deletion rejects new authentication attempts and trusted-mode startup. |
| `legacy_commercial_approval` | `OPHIRA_COMMERCIAL_APPROVAL_SECRET` | Legacy compatibility setting; no current commercial-action implementation consumes it. |

Values and deletion records are stored in the private
`data/secrets/deployment-secrets.json` vault. Each mutation is bound to a keyed review
revision. A deletion disables environment fallback across restarts; repeating the
same deletion acknowledges the committed result. An old review cannot delete or
overwrite a subsequently saved key. The legacy record does not change current
commercial approval behavior.

## Review and encrypted export

Run from the repository root with the deployed Python environment. Use the actual
data directory and explicitly select the deployment `.env` if it supplies these
values. Without `--env-file`, only the command's process environment is read; it does
not silently load the repository's `.env`. Process environment takes precedence over
the selected file, as it does for application settings. Root settings are shared with
the application; unrelated invalid application settings do not prevent local recovery.

```bash
PYTHONPATH=backend/src .venv/bin/python -m ophira_backend.deployment_credential_cli \
  --data-dir /absolute/deployment/data --env-file /absolute/deployment/backend.env list
```

Review the displayed data root, tenant, identifier, source, effect and `revision`.
Use that revision for the corresponding action:

```bash
PYTHONPATH=backend/src .venv/bin/python -m ophira_backend.deployment_credential_cli \
  --data-dir /absolute/deployment/data --env-file /absolute/deployment/backend.env \
  export trusted_gateway --expected-revision REVIEWED_REVISION \
  --output /absolute/private-backups/gateway.credential.json
```

The command asks twice for a new archive password, with terminal echo disabled.
Passwords and keys are never command arguments or printed values. A terminal that
cannot suppress echo is rejected. Archives use the existing authenticated AES-256-GCM
and fixed Scrypt format, and include the credential identifier and tenant inside the
encrypted payload. Export rereads the credential revision after encryption. The output
is a new `0600` file; an existing file, symbolic link, or linked parent is not overwritten.
An interrupted export may leave an incomplete encrypted file; inspect its outcome and
choose a new output path rather than assuming a successful backup.

The current [offline decryptor](credential-management.md#encrypted-export)
can recover an archive to a new private JSON file when needed. Prefer direct encrypted
restore below to avoid creating an additional plaintext copy. Older decryptors that do
not recognize `kind: deployment` reject these archives.

## Delete, replace and recover

Stop every application worker before any mutation. The command obtains the same
exclusive application-data lease as cold backup; a live cooperating worker, another
mutation or backup makes it fail before changing the key. Read-only list/export holds
a shared lease. This requires the current worker version and ASGI lifespan; upgrade
all old workers together. Neither command constructs the application nor starts a
model session, scheduler, notification or commercial action.

```bash
PYTHONPATH=backend/src .venv/bin/python -m ophira_backend.deployment_credential_cli \
  --data-dir /absolute/deployment/data --env-file /absolute/deployment/backend.env \
  delete trusted_gateway --expected-revision REVIEWED_REVISION --confirm-delete
```

List again after deletion or an interrupted command. To restore the encrypted backup
into the reviewed state:

```bash
PYTHONPATH=backend/src .venv/bin/python -m ophira_backend.deployment_credential_cli \
  --data-dir /absolute/deployment/data --env-file /absolute/deployment/backend.env \
  restore trusted_gateway --expected-revision CURRENT_REVIEWED_REVISION \
  --input /absolute/private-backups/gateway.credential.json --confirm-restore
```

Restore prompts for the archive password and checks the exact deployment kind,
identifier, tenant and current revision before saving. It accepts neither a service
archive nor a key exported for a different tenant. A cold restore to a different data
path is supported when the deployment tenant matches. A restored old signing key also
restores its signing authority; coordinate this explicitly with the identity gateway.

To save a new key instead, use `set trusted_gateway --expected-revision
CURRENT_REVIEWED_REVISION` with the same global options. It asks twice for the new key
without echo, requires 32–512 characters with no leading/trailing spaces, and changes
the vault generation even when saving the same value. Generate a random signing key
outside the application and configure the ingress signer separately. The command does
not modify deployment `.env` files or a remote gateway.

For a deployment that should start using only its saved local key, set:

```dotenv
OPHIRA_AUTH_MODE=trusted_gateway
OPHIRA_TRUSTED_GATEWAY_AUTH_SOURCE=local_vault
OPHIRA_TRUSTED_GATEWAY_AUTH_SECRET=
OPHIRA_TRUSTED_GATEWAY_TENANT_ID=your_existing_tenant
```

The default source, `environment`, preserves existing startup configuration checks
and allows fallback only when no vault record exists. A saved value overrides the
environment in either mode. `local_vault` requires a valid saved key at authentication
construction and ignores the environment fallback. Missing, deleted, corrupt or
unreadable storage never switches trusted authentication into `local_only` mode.
An existing authenticator reads the current key for each request; old signatures fail
after replacement. This does not revoke an already accepted request or delete device
pairings, project memberships, or nonce history.

## Backup and verification boundary

Application cold backups include the vault and its tombstones. Restoring a complete
backup retains them even after the original data and Plugin directories are removed.
External `.env`, ingress secrets, previously exported archives, other backups and
already issued external side effects are separate copies. Local deletion is neither
external credential revocation nor secure physical erasure.

Tests use synthetic keys and temporary data with real encrypted round trips, HTTP
authorization, a separate CLI process, lease contention and cold recovery. No real
gateway key is read or rotated. Production OS/tenant isolation, remote ingress
coordination, disaster recovery and the other PRD acceptance gaps remain open.

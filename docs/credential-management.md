# Credential export and deletion

Settings → Providers → **Credential backups and deletion** manages the credentials
for Provider profiles and connectors. The panel reads metadata only; it never displays
the saved key. Each action is bound to the exact credential version reviewed by the
user. If a key changes, refresh and review it again.

## Encrypted export

Choose **Export**, enter the same new archive password twice (12–1024 characters),
and download the encrypted JSON file. Keep the password separately: OPHIRA does not
store it and cannot recover a forgotten password. The archive includes one credential
and its profile name, kind, identifier, deployment scope, export time and actor. All
of that payload is encrypted. It does not include the rest of the application state.

To recover the credential locally, run this from the repository root with the
backend dependencies installed. The command prompts for the password instead of
accepting it in command arguments:

```bash
PYTHONPATH=backend/src .venv/bin/python -m ophira_backend.credential_archive \
  /absolute/path/export.credential.json /absolute/path/recovered-credential.json
```

The output is a new file with mode `0600` containing the decrypted `credential` and
export metadata. Existing files are never overwritten; linked paths are rejected.
The command does not contact a service, print the key, initialize application state,
or restore credentials automatically. Save the recovered key through the existing
Provider or connector editor when needed. Structured connector credentials remain
JSON strings in the `credential` field.

The version 1 archive uses a random 16-byte salt, a fixed Scrypt configuration
(`n=32768`, `r=8`, `p=1`, 32-byte output), a random 12-byte nonce and AES-256-GCM.
The format/version/algorithm identifiers are bound as authenticated associated data.
The decryptor rejects unsupported identifiers and oversized inputs before deriving
a key; a wrong password or modified payload releases no plaintext. This follows
the library's [AEAD](https://cryptography.io/en/latest/hazmat/primitives/aead/)
and [Scrypt](https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions/#scrypt)
APIs. Encryption uses memory only and writes no plaintext temporary export file.

## Confirmed deletion

Choose **Delete**, read the scope and explicitly check the confirmation box. Deletion
removes the saved key from the current vault and records a durable deletion receipt.
That receipt disables environment fallback and startup import until a new credential
is explicitly saved. Retrying the same deletion after a lost response returns its
receipt; an old request cannot delete a subsequently saved key.

Deletion does not revoke the key at the external service, alter the host's environment
configuration, erase older backups, or stop workers that already loaded the key.
Restarted workers resolve the current vault state. External revocation and removal
of separately retained copies remain separate actions. Vault files still use private
local file permissions; this feature does not provide encrypted storage at rest,
secure physical erasure, or deletion of separate identity-gateway and channel secrets.
The built-in search and media connectors also resolve credentials through this vault,
including values supplied through application settings or dotenv configuration.

The previous flat Provider/connector vault format migrates automatically under a
cross-process lock. Stop old application workers before upgrading: old code does not
understand deletion receipts. Corrupt or linked vault files fail closed instead of
being treated as empty. New writes use private temporary files, atomic replacement
and directory synchronization; interrupted staging files are cleaned on the next
vault operation. Application cold backups retain the resulting vault state, including
deletion receipts. Restoring an older backup can restore an older key.

## Access and API

Local mode is restricted by the existing local-origin middleware. Trusted-gateway
mode requires a current owner/admin identity in the configured deployment tenant;
members, reviewers and viewers cannot inspect or manage these credentials. The
management scope is durably bound to the deployment's auth mode and tenant. Copying
the data directory into another scope does not silently transfer management access.
This is a single-deployment boundary, not tenant-isolated secret storage.

| Endpoint | Request / result |
| --- | --- |
| `GET /api/credentials` | Metadata including `kind`, `id`, name, opaque `revision`, availability, source and deletion receipt; no keys |
| `POST /api/credentials/{kind}/{id}/export` | `expected_revision` and `password`; encrypted JSON attachment |
| `DELETE /api/credentials/{kind}/{id}` | `expected_revision` and strict `confirm_delete: true`; durable deletion metadata |

Kinds are `provider` and `connector`. Successful responses disable caching.
Review revisions are keyed commitments, not public hashes of secret material. Export
rechecks identity validity and the revision after encryption. Deletion checks
authority again while holding the vault transaction. Invalid bodies do not echo
passwords. The server bounds request size and concurrent encryption work; UI requests
are cancellable and stale responses cannot trigger downloads after closing the panel.
These are human management endpoints; no Agent or MCP export/delete tool was added.

Project/session export and deletion, application restore UI, production disaster
recovery and real identity-gateway acceptance remain separate PRD requirements.

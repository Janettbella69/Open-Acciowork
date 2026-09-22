# Credential export and deletion

Settings → Providers → **Credential backups and deletion** manages the credentials
for Provider profiles, connectors and application services. The panel reads metadata only; it never displays
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
Provider/connector editor or **Configure** beside an application service when needed.
Structured connector credentials remain JSON strings in the `credential` field.
Use this version's decryptor for service exports; older decryptors that only recognize
Provider/connector payloads reject the new service kind.

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
secure physical erasure, or deletion of separate identity-gateway signing secrets.
The built-in search and media connectors also resolve credentials through this vault,
including values supplied through application settings or dotenv configuration.

## Application service credentials

The same owner/admin panel also manages these deployment-wide services:

| Service ID | Existing configuration fallback | Effect of deletion and replacement |
| --- | --- | --- |
| `feishu_events` | `OPHIRA_FEISHU_ENCRYPT_KEY` | Blocks new webhook signature verification/decryption; saving a key replaces the active verifier without restarting |
| `feishu_app` | `OPHIRA_FEISHU_APP_SECRET` | Blocks new tenant-token/reply requests and invalidates local cached tenant tokens; the configured application ID and official Feishu/Lark origin remain separate |
| `web_push` | `OPHIRA_PUSH_VAPID_PRIVATE_KEY` | Blocks new Web Push requests; saving a different key changes the public key and requires devices to subscribe again |

Service keys share the existing private vault protocol in `secrets/service-secrets.json`.
An unset local key can use the configured environment fallback. Deletion persists a
tombstone that prevents fallback after a restart or environment change. Changing an
environment variable alone cannot undo deletion; explicitly save a replacement.
Existing deletion/export revision checks and encryption apply to these keys too.

**Configure** takes a password-masked replacement and the reviewed revision. Invalid
or changed revisions cannot overwrite another administrator's update. The request is
size-bounded and errors do not echo the replacement. Web Push keys must be inline
RAW/DER base64 containing a P-256 private key; file paths and malformed keys are rejected.
Only metadata is returned. Submitted drafts are cleared on success, failure or close;
after an ambiguous response the page reloads current metadata before another review.

The adapters read a credential and its generation together. Feishu drops cached tokens
on rotation, deletion or unreadable storage; token responses are rechecked after the
network wait so an old response cannot revive a revoked generation. Web Push refreshes
its signer/public key and rechecks before starting the transport. An already dispatched
network request remains in flight; local deletion is not external-service revocation.
Storage corruption fails closed instead of falling back to the original environment.

A configured Push worker stays alive while its key is absent, without sending requests.
Explicitly saving a valid key can resume pending replies/notifications under existing
policies. Restoring the same Push key can reuse existing subscriptions; a different key
does not silently renew devices. Feature flags, application IDs, subject, destinations,
project/device authorization and delivery retry limits remain independently enforced.

Cold snapshots retain saved service keys and deletion receipts. The source-deletion
restore test confirms a restored receipt still suppresses environment fallback and an
explicit saved key remains available. As with other credentials, older backups and
the operator's `.env` remain separate copies. Stop all old-version workers before
upgrading: they do not consult the new service vault or observe its revocations.

Identity-gateway signing keys and the unused legacy commercial signing setting are
not exposed to ordinary service-credential endpoints. Their local deployment-owner
export/deletion/recovery workflow remains open; web role management must not grant
the power to mint gateway identities.

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
| `PUT /api/credentials/service/{id}` | `expected_revision` and `secret`; saves one listed service credential and returns metadata |

Kinds are `provider`, `connector` and `service`. Service metadata also includes a
non-secret `effect` explanation. Successful responses disable caching.
Review revisions are keyed commitments, not public hashes of secret material. Export
rechecks identity validity and the revision after encryption. Deletion checks
authority again while holding the vault transaction. Invalid bodies do not echo
passwords or replacement keys. The server bounds request size and concurrent encryption
work. Closing the UI stops waiting for its request; it does not undo a submitted write.
Stale responses cannot trigger downloads after closing the panel; reopen and refresh
to inspect a possibly completed mutation.
These are human management endpoints; no Agent or MCP export/delete tool was added.

Project/session export and deletion and the standalone application restore UI have
their own contracts. Production disaster recovery, local identity-root key management
and real identity-gateway/service acceptance remain separate open PRD requirements.

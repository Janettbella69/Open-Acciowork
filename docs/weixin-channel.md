# Official Weixin direct messages

The Connectors page can pair a personal Weixin account, bind the scanning user to
one project, and explicitly enable text messages. Pairing never enables an account
or changes the organization channel-delivery switch. In local mode the project
must be local. In trusted-gateway mode only an owner/administrator can manage
accounts; the binding inherits that administrator's verified principal and tenant.
Project membership is checked again for each Run and each reply.

## Official protocol and product scope

This adapter uses Tencent's public HTTP protocol, without importing the OpenClaw
runtime. Claude Agent SDK remains OPHIRA's only Agent runtime. Sources inspected
on 2026-09-29:

- [Tencent protocol](https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol_zh_CN.md)
- [Tencent channel capabilities](https://github.com/Tencent/openclaw-weixin/blob/main/src/channel.ts)
- [Tencent QR login](https://github.com/Tencent/openclaw-weixin/blob/main/src/auth/login-qr.ts)

The official plugin declares direct-chat capability. A protocol `group_id` field
does not establish group-chat support. OPHIRA therefore accepts only a completed,
single text item from the QR-confirmed user to the paired bot, with a valid message
ID, creation timestamp and reply context. Groups, media and other senders are
rejected. **This does not complete FR-24's group @ acceptance requirement.**

The transport permits only `https://ilinkai.weixin.qq.com`. Public DNS is checked
and pinned per request; HTTP redirects, arbitrary base URLs, proxy environment
settings, oversized/deep/duplicate-key JSON and malformed acknowledgements are
rejected. Unknown regional login redirects are shown as unsupported until an
official host can be verified and explicitly added. No host is guessed from a
server-supplied redirect.

## Connection lifecycle

1. Select **Connect WeChat** in **Plugins → Connectors** and scan the short-lived
   QR code. The server encodes the official QR string locally; it does not fetch
   an external image or send pairing data to a QR service.
2. Complete any official verification-code step. The QR expires after five
   minutes, belongs to the initiating gateway session, and is never kept in
   browser storage. Cancelling it prevents a late poll result from creating an
   account. An already confirmed result is retained as confirmed.
3. Review the connected user, save the destination project, then separately
   enable direct messages. The organization channel-delivery switch must also be
   enabled. Historical messages predating this activation are not replayed as new
   tasks. Changing the project requires a new enablement.
4. Send a new text message. The existing RunManager persists its reply obligation
   before starting the single main Agent. Results return to that same user.
5. Pause the account to stop admission and invalidate old pending replies, or
   revoke it to remove its credential and contexts. Reconnection requires another
   scan and starts disabled. No API exports a Weixin token.
   Revocation removes this application's credentials; the Weixin-side binding
   must be managed in Weixin itself.

Each binding revision freezes the destination and authorization. In-flight polls
and reply targets from an older revision fail closed. The organization switch
pauses polling without advancing the cursor. No local account binding becomes
usable merely by switching the deployment to gateway authentication.

## Durability and delivery limits

- A cross-process account lease permits one active long poll. The cursor is saved
  after handling a complete batch; failed cursor writes leave the old position.
  Durable Run claim IDs and the shared replay guard reject repeated messages.
- An ambiguous Run-creation failure is recorded as `INGRESS_RUN_UNCERTAIN` and is
  not automatically rerun. This is not an exactly-once promise or a durable
  inbound-message queue. An operator can inspect the Run and ask for a new message.
- Replies use the original private context and a stable client correlation ID.
  There is no assumed server-side deduplication guarantee. A timeout, lost/invalid
  acknowledgement, interrupted send or uncertain HTTP result becomes a terminal
  uncertain outcome; recovery does not send it again automatically.
- The app sends one text reply, capped at 4,000 characters, with a workspace
  continuation notice when shortened. It does not send files, images, voice, group
  messages, or unsolicited messages without an inbound context.
- Reply contexts expire locally after 24 hours and are capped at 1,000 per
  account. Expired contexts cannot send replies. Periodic cleanup removes expired
  pairing credentials and contexts. A pending result may therefore require the
  user to return to the workspace.
- Accounts/cursors are bounded host records under `channels/weixin`. Tokens and
  reply contexts live only in the dedicated private `weixin-secrets.json` vault,
  which participates in cold-backup credential deletion registers. Revocation
  disables the account durably before removing its secret. Deleting a project
  removes its Weixin binding and contexts while retaining the account credential,
  as disclosed in the deletion review; revoke the account to remove that too.

The shared FileSecretVault now uses descriptor-based, no-follow reads/writes and
private regular-file validation. It rejects linked vault/lock/register files and
does not chmod their external targets. This applies to existing Provider,
Connector and MCP credentials as well.

## Control-plane API

All endpoints use the existing local-origin or trusted-gateway authentication.
Pairing and account management are owner/administrator operations in gateway mode.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/channels/weixin` | Capabilities, authorized accounts and runnable projects |
| POST | `/api/channels/weixin/pairings` | Start QR pairing |
| POST | `/api/channels/weixin/pairings/{id}/poll` | Poll, optionally with a verification code |
| DELETE | `/api/channels/weixin/pairings/{id}` | Cancel unconfirmed pairing |
| PUT | `/api/channels/weixin/accounts/{id}/binding` | Save project/enabled state with expected revision |
| DELETE | `/api/channels/weixin/accounts/{id}?expected_revision=N` | Revoke credential and binding |

Account responses omit private principal bindings and all credentials. QR responses
use `Cache-Control: no-store`. There is no public Weixin inbound webhook; only the
host-owned authenticated polling transport can dispatch a Weixin message.

The former empty, manually keyed WeChat connector is no longer seeded. Existing
generic connector records are preserved. Their derived generic secret-presence
bit allows the UI to hide an empty placeholder while retaining explicit management
of previously configured credentials, separately from official QR pairing.

## Acceptance boundary

Offline transport, account, API, channel, delivery and UI contracts use temporary
storage and synthetic official-protocol responses. The HTTP exercise starts real
Uvicorn with an explicit offline Agent runtime and mock Weixin transport; it proves
the local application path, not an actual Weixin login, delivery or model result.

Still open: real personal-account pairing and delivery, regional-host support,
account expiration/rotation against the live service, browser/phone visual
acceptance, and the original group @ requirement. No real Weixin credentials,
messages or paid Provider calls were used during implementation.

2026-09-29 verification: backend full suite **2,747 passed / 14 skipped**; after
the final QR-error handling and legacy-card changes, **125** relevant API,
credential and connector regression tests passed. Frontend full suite **618
passed / 63 files**. Ruff, ESLint, TypeScript/Vite build, dependency checks and
diff checks passed (Vite retains its existing large-chunk notice). The
[local HTTP report](../evals/reports/weixin-channel-20260929.json) records all six
checks and the explicit fixture boundaries.

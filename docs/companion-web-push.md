# Companion Web Push

Standard browser Web Push is implemented for approved companion devices. It is
optional and off by default. Native APNs/FCM application clients, OS sleep
prevention, and a guarantee that a phone displayed a notification are not provided.

## Enable deliberately

First deploy and pair the same-origin HTTPS companion described in
[mobile-companion.md](mobile-companion.md). Keep the desktop API private.
Configure `OPHIRA_PUSH_ENABLED=true`, a persistent operator-owned P-256
`OPHIRA_PUSH_VAPID_PRIVATE_KEY` (base64url raw private scalar or DER, not a filename),
and a valid `OPHIRA_PUSH_VAPID_SUBJECT` contact such as `mailto:operator@example.com`.
Keep the private key in the control plane's private environment/secret manager,
never a browser bundle, Agent environment, repository, issue, or log. Back it up
privately: rotating it requires browsers to explicitly renew their subscriptions.
This feature never generates or overwrites an operator key automatically.

`OPHIRA_PUSH_ALLOWED_ORIGINS` is an operator-owned comma-separated list of exact
HTTPS origins. Defaults cover `fcm.googleapis.com`,
`updates.push.services.mozilla.com`, and `web.push.apple.com`. If a browser uses a
different service origin, review and configure that exact origin; the client
cannot change the allowlist. No wildcard, userinfo, non-443 endpoint port,
private-network destination, or redirect is accepted. An allowed hostname must
resolve entirely to public IP addresses on every send.

On the approved phone, select **Enable background push**. Permission is requested
only from this click. A missing browser API, denied permission, expired
subscription, wrong application key, failed registration, or revoked device is
shown explicitly. Subscription renewal is never automatic. On iPhone/iPad, use
the installed Home Screen web app; the companion has its own manifest with
`start_url` and `scope` `/app/mobile`. Browser/system/network restrictions still
affect delivery. See [Apple's Web Push guidance](https://developer.apple.com/documentation/usernotifications/sending-web-push-notifications-in-web-apps-and-browsers).

## API and privacy

All three endpoints use the existing strict origin/header/cookie boundary and
require an approved, activated, unexpired device:

- `GET /api/companion/push/status`
- `POST /api/companion/push/subscription`: exact `PushSubscription.toJSON()` body
- `POST /api/companion/push/unsubscribe`: `{}`

Responses contain `enabled`, `public_key`, `subscribed`,
`subscription_fingerprint` (SHA-256 of the endpoint), and a safe short `reason`.
They never echo capability URLs or subscription encryption/authentication keys.
Subscription writes also revalidate bound project membership after consuming the
bounded request body. An endpoint already bound to a different device cannot be
transferred by registration.

`data/companion-push` is private (0700), including a pre-existing or restored
directory whose permissions are tightened on initialization; atomically replaced
subscription/delivery records are 0600 and fsynced. Endpoints and browser encryption keys are necessary
delivery capabilities, unlike the hashed companion cookie. Treat this directory
and its backups as sensitive. Unsubscribe, device revoke/logout, subscription
expiry/key drift, and terminal service rejection scrub live capability fields;
bounded, content-free attempt metadata remains. This is logical live-record
scrubbing, not physical-media or backup erasure.

The encrypted payload is only `{"type":"ophira-companion-update"}`. No project
name, prompt, tool parameters, approval token, or business result enters a push.
The worker ignores payload text/URLs/actions and displays a fixed generic notice.
A click can only open/focus the fixed same-origin `/app/mobile`; it cannot approve,
cancel, or start a Run. The authenticated page retrieves current authorized data.
The worker has no fetch handler or project-data cache. `httpx` and the actual
`httpcore` transport child loggers are suppressed only within the push send
context: URL logs and DEBUG response headers can expose capability paths.
Unrelated request diagnostics remain enabled.

Encryption and signing use pinned `pywebpush==2.5.0` / its VAPID implementation,
not custom cryptography. The transport retains HTTPS certificate verification,
original Host/TLS SNI, and a validated numeric destination; proxies, keep-alive
reuse, and redirects are disabled. DNS resolution runs off the event loop and a
15-second outer deadline bounds an attempt without blocking approval endpoints.
See the [library](https://github.com/web-push-libs/pywebpush),
[RFC 8291](https://www.rfc-editor.org/rfc/rfc8291), and
[RFC 8292](https://www.rfc-editor.org/rfc/rfc8292).

## Wake delivery contract

Every five seconds the host checks current, device-authorized unread project
notifications and pending, unexpired approvals. Resolved/expired durable approval
records are not new permission prompts. Global or other-project inbox contents
are excluded before the bounded notification limit. The sender never receives
permission to run a task from a push subscription.

New items since registration are coalesced into one generic wake. No old inbox
backfill occurs on enable. Each device retains one current/last wake and at most
1,000 hashed source IDs over a one-day window; overflow advances the time floor.
The latest 200 relevant unread notifications plus pending approvals are considered;
a burst yields a generic wake, not a promise of a separate alert for every row.

Delivery leases are nonblocking cross-process locks. An attempt and its budget
are durable before network transmission; a crash cannot reset the count. Retries
keep a stable Web Push `Topic`, are bounded to five attempts and a five-minute wake
lifetime, and obey bounded Retry-After/backoff. Both delta-seconds and HTTP-date
Retry-After forms are supported, capped at one hour; a delay past the five-minute
wake lifetime expires the wake without retrying early. The service-requested
cooldown persists across a cancelled/expired wake, process restart, and subscription
replacement, so a new notification cannot bypass it. Minimum spacing defaults to 15
seconds; the device budget is 100 attempts per UTC day, retained across subscription
replacement. These limits may suppress alerts: the companion inbox, not push, is
authoritative. Already-read/resolved or newly unauthorized sources cancel pending
retries. Each attempt rechecks device/project authority and subscription generation.

HTTP 2xx means **push service accepted**, not that the phone received/displayed it.
404/410 and authentication rejections disable the subscription. 429/5xx and network
failures retry within the budget; other failures are terminal. Generation checks
prevent a late in-flight response from restoring a replaced/unsubscribed capability.
An already transmitted/accepted push cannot be recalled; it still contains only
the generic notice and grants no access. Delivery is not exactly-once.

## Verification boundary

Local tests exercise real RFC encryption/decryption and ECDSA signature
verification, mocked HTTP acknowledgements/rejections, DNS rebinding defenses,
capability log suppression, cross-process leases, crash retry budgets, revocation,
in-flight unsubscribe, source filtering, and endpoint/key validation. Frontend
tests execute the actual service-worker script and cover explicit permissions,
subscription reconciliation, safe navigation, cleanup, and stale responses.

No real browser subscription, external push-service send, iOS/Android receipt,
HTTPS ingress, locked-phone notification display, or long-running host availability
was verified by these local tests. They remain required release acceptance, with
an explicitly authorized test device and VAPID deployment configuration.

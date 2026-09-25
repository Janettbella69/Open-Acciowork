# Signed event triggers (FR-37)

Open **Scheduled tasks → My tasks → Event triggers** to create a project-bound
trigger. Choose a webhook, inquiry email, or store order source; set the instruction,
budget per Run, and permission ceiling. Creation returns a 256-bit signing key once.
Save it in the sending system. The backend stores the key in a private `0600` file,
outside the Agent-readable project tree. Rotating it immediately invalidates the
old key and the current trigger revision; pausing stops new deliveries.

## Delivery contract

POST the raw JSON bytes (at most 64,000 bytes) to the trigger's **Delivery URL**.
Use three headers:

| Header | Value |
|---|---|
| `X-Ophira-Timestamp` | Current Unix time in seconds |
| `X-Ophira-Event-Id` | Stable ID for this source event, up to 160 safe characters |
| `X-Ophira-Signature` | `sha256=` plus lowercase hex HMAC-SHA256 |

Compute the MAC over UTF-8 bytes of `timestamp + "\n" + event_id + "\n"`, followed
by the **exact raw request body bytes**. The HMAC key is the signing key returned at
creation. Timestamp drift of five minutes or more is rejected. A sender may retry
the same event ID with a newly signed timestamp and identical body; it receives the
same Run ID. Reusing an ID with different bytes is rejected.

```python
import hashlib
import hmac
import json
import time

body = json.dumps({"signal": "new inquiry"}, separators=(",", ":")).encode()
timestamp = str(int(time.time()))
event_id = "source-event-123"
message = timestamp.encode() + b"\n" + event_id.encode() + b"\n" + body
signature = "sha256=" + hmac.new(signing_key.encode(), message, hashlib.sha256).hexdigest()
# POST body unchanged, with the three headers above.
```

The inquiry email source expects `message_id`, `from`, `subject`, and `body` as
nonempty strings. The store order source expects `order_id`, `shop_id`, and `status`.
Other fields remain available to the Agent as bounded, untrusted event data. A
sender or integration gateway must normalize its upstream email/store event to
this contract. Direct Mailgun, Gmail, Outlook Inbox and Shopify order events
are also supported below.

## Direct Gmail Inbox trigger

Gmail uses a Google OAuth web client and a Google Cloud Pub/Sub push subscription.
Set `OPHIRA_AUTH_MODE=trusted_gateway`, `OPHIRA_EVENT_PUBLIC_ORIGIN` to the
operator-owned public HTTPS origin, `OPHIRA_GMAIL_PUBSUB_TOPIC` to an exact
`projects/<project>/topics/<topic>` name, and
`OPHIRA_GMAIL_PUSH_SERVICE_ACCOUNT` to the service account email that signs the
push subscription. The callback URL is
`<event origin>/api/connectors/gmail/gmail-oauth/callback`; register that exact
redirect URI in the Google OAuth web client. Enable the Gmail API and grant
`gmail-api-push@system.gserviceaccount.com` Pub/Sub Publisher on the topic.
Configure the push subscription with the URL shown on the Gmail trigger as both
its endpoint and authentication audience, and choose the configured service
account for authenticated push. Follow Google's [Gmail push setup](https://developers.google.com/workspace/gmail/api/guides/push)
and [authenticated Pub/Sub push setup](https://docs.cloud.google.com/pubsub/docs/authenticate-push-subscriptions).

In **Connectors → Gmail**, save the OAuth web client ID and client secret, then
authorize the mailbox. The server requests only
`https://www.googleapis.com/auth/gmail.readonly` and stores the refresh token in
the private connector vault. This is a [restricted Google scope](https://developers.google.com/workspace/gmail/api/auth/scopes);
a public Google OAuth app may need verification and a security assessment.
Create **Inquiry email → Gmail Inbox via Pub/Sub** in Event triggers, then select
**Register or renew Gmail watch**. Multiple project triggers in the same tenant
may share the one mailbox watch. Each trigger begins at its own enrollment
history ID, so creating another trigger does not replay earlier mail.
The watch grants no project Agent access to earlier mail. Active search requires
the separate [project Gmail grant](gmail-connector.md).

Google sends only the mailbox address and history ID in a push notification.
OPHIRA verifies Google's RS256 JWT signature, exact audience, service account
email and `email_verified` claim before reading the notification. It then calls
Gmail `history.list`, fetches newly added Inbox messages, ignores Sent, Draft
and Trash, and passes at most 48 KB of UTF-8 plain text per message through the
same project-bound Run receipt, creator authority, budget, permission ceiling
and external-action approval checks. Attachments and HTML are excluded. A
durable Gmail message ID deduplicates retries; the shared mailbox cursor moves
only after all eligible project dispatches have succeeded. The service checks
for watches within two days of expiration every six hours and renews them;
Google requires renewal at least every seven days. See the [Gmail synchronization contract](https://developers.google.com/workspace/gmail/api/guides/sync).

If Gmail reports that the saved history is no longer available, the watch is
marked **stale** and delivery stops. A tenant administrator must explicitly
choose **Reset stale Gmail cursor and skip missing mail**; this starts from a
new Gmail history ID and does not claim to have processed the gap. Stop the
watch before revoking the connector or deleting its last trigger project. The
UI refuses to stop a watch while another active project trigger uses it.
Project deletion review discloses a remaining Gmail enrollment as an external
watch boundary; erasing the project removes its local enrollment but cannot
stop a shared remote watch on behalf of another project.
Google OAuth consent, topic permissions, authenticated public push delivery and
Provider Run execution still require validation with an authorized live account.

## Direct Outlook Inbox trigger

Authorize the Outlook connector in **Connectors → Outlook** with Microsoft
delegated `offline_access User.Read Mail.Read` and configure the same public
HTTPS event origin used for OAuth. Create **Inquiry email → Outlook Inbox via
Microsoft Graph** in Event triggers, then select **Register or renew Outlook
subscription**. A tenant administrator with project access manages it. The
server registers one Graph `created` subscription for the connector's Inbox;
multiple project triggers on that mailbox share it. Each trigger has its own
project enrollment. The callback is
`<event origin>/api/connectors/outlook/outlook/notifications`. Only the exact
configured host in trusted-gateway mode accepts it.

Graph validates the callback with a `validationToken` request. OPHIRA echoes
the decoded token as plain text. Notifications must carry the private
`clientState` created at registration, the matching subscription ID, and an
Inbox message ID. The endpoint persists each verified message notification
before returning `202`; a background worker fetches that immutable ID from
Graph. It checks the message remains in Inbox and is not a draft, requests
plain text, excludes HTML and attachments, and limits the body to 3,500
characters. Sender and subject are untrusted. The existing event dispatcher
rechecks project and trigger authority, budget, permission ceiling, and human
approval rules. Duplicate notifications share a durable queue item and Run
receipts. Completed queue items retain only an ID digest for 30 days; message
content and project targets are cleared. Project deletion waits for pending
Outlook notifications involving that project to finish, and its review warns
that a remote shared subscription may remain.

The subscription requests a six-day expiration and renews within one day of
expiry. Graph lifecycle `missed` and `subscriptionRemoved` events mark a
visible **stale** mail gap; they are never described as processed. This
implementation does not yet reconcile that gap with Graph delta. An uncertain
create response is retained and never retried automatically. **Reconcile
Outlook subscription with Graph** lists this app's subscriptions, adopts only
one exact app/Inbox/callback match, or marks a visible gap when none exists.
The list API does not return `clientState`, so verified notifications remain
the delivery proof. Multiple matches require manual review. Stopping
one trigger removes only its enrollment when another active trigger shares
the subscription; stopping the last trigger deletes the Graph subscription.
Stop the subscription before revoking its connector, and inspect any gap before
stopping it. A real Microsoft account, public HTTPS callback, lifecycle
delivery, renewal, and Provider Run still need live validation. See Microsoft's
[webhook delivery](https://learn.microsoft.com/en-us/graph/change-notifications-delivery-webhooks),
[Outlook notification](https://learn.microsoft.com/en-us/graph/outlook-change-notifications-overview),
and [lifecycle](https://learn.microsoft.com/en-us/graph/change-notifications-lifecycle-events)
contracts.

## Direct Mailgun inbound email

Choose **Inquiry email → Mailgun inbound route** and enter the exact receiving
address and the Mailgun **Webhook Signing Key** (not its API key). The key is kept
only in OPHIRA's private `0600` event-trigger secret file; it is never returned
by the create, list, or update API. A member with project Run-creation permission can replace it in
the Event triggers page. The signed JSON delivery URL remains available as the
separate **Signed JSON from an integration** option.

Configure a [Mailgun receiving route](https://documentation.mailgun.com/docs/mailgun/user-manual/receive-forward-store/routes)
matching that address with a `forward("https://.../mailgun")` action using the
**Delivery URL** shown in OPHIRA. The receiving domain must be configured in
Mailgun. In trusted gateway mode, set `OPHIRA_EVENT_PUBLIC_ORIGIN` to the
operator-owned public HTTPS event origin; local-only mode cannot receive public
Mailgun deliveries. Mailgun posts parsed inbound mail as URL-encoded form data,
or multipart form data when attachments exist. OPHIRA ignores attachments and
HTML; only bounded plain text reaches the Agent. See Mailgun's
[inbound HTTP contract](https://documentation.mailgun.com/docs/mailgun/user-manual/receive-forward-store/receive-http).

The endpoint verifies Mailgun's HMAC-SHA256 over `timestamp + token` with the
stored key, accepts only an eight-hour-ten-minute timestamp window to cover the
documented retry period, and requires the configured exact recipient. It
requires one `Message-ID` in Mailgun's `message-headers`. A durable
token receipt rejects the same signed token with changed email content; a
recipient/Message-ID digest deduplicates retries even if Mailgun gives them a
new token. The normalized body, sender and subject are untrusted data in the
project Run. A trigger still uses the same creator authority, frozen budget,
permission ceiling and separate external-action review as other FR-37 events.
The endpoint accepts up to 1 MB total request bytes and at most 48,000 plain-text
UTF-8 bytes; over-limit or malformed email is rejected without starting a Run.
Mailgun's signature authenticates its delivery, not the human sender's identity.
See Mailgun's [signature contract](https://documentation.mailgun.com/docs/mailgun/user-manual/webhooks/securing-webhooks).

## Direct Shopify order webhook

Configure the Shopify connector with the store's exact
`https://<store>.myshopify.com` origin and the app client ID and client secret.
Grant the project access to that shop, then select **Store order → Shopify
orders/create webhook** and its active shop grant in Event triggers. Creation
returns no signing key: Shopify signs delivery with the app client secret already
held in the connector vault. Register the displayed `/shopify` Delivery URL as
an HTTPS `orders/create` webhook subscription in the Shopify app. In a trusted
gateway deployment, set `OPHIRA_EVENT_PUBLIC_ORIGIN` to the operator-owned public
HTTPS origin (for example `https://events.example.com`). A project administrator
can then select **Register or verify with Shopify**. OPHIRA uses its configured
origin and the app's current shop token to create a shop-specific subscription;
the browser cannot choose the callback. The app must already have `read_orders`
granted by Shopify. Local-only mode cannot register a public subscription.
See Shopify's
[subscription guide](https://shopify.dev/docs/apps/build/webhooks/subscribe).

Registration first queries Shopify for this exact URL and topic. It creates a
subscription with only the order ID, name, time, financial/fulfillment status,
total price, and currency when none exists. A durable attempt is saved before
the external mutation. If a response is lost, the next request queries Shopify
again and reports an uncertain outcome without creating another subscription.
After inspecting Shopify, an administrator may explicitly retry. **Remove
Shopify subscription** checks the same URL and ID, removes that subscription,
then clears the local record; a lost removal response is reconciled by another
remove attempt. A changed public origin requires removal of the old subscription
before registration at the new URL. Subscription status shown in the UI is the
last local confirmation; **Register or verify** refreshes it from Shopify.
Remove registered or uncertain Shopify subscriptions before deleting the project.
The project deletion review discloses any local subscription record as an external
retention boundary. Project deletion erases the local callback ID but does not
revoke the remote Shopify webhook. Removal uses the recorded URL, so it still
works after `OPHIRA_EVENT_PUBLIC_ORIGIN` is changed or unset, provided the shop
grant and connector remain available. If those credentials are unavailable,
remove the subscription directly in Shopify before deleting the project.

The endpoint verifies `X-Shopify-Hmac-SHA256` against the **raw body**, then
requires `X-Shopify-Topic: orders/create`, the granted store's exact
`X-Shopify-Shop-Domain`, and `X-Shopify-Webhook-Id`. It rejects duplicate headers,
invalid JSON, the absence of a matching active shop grant, a disabled connector, and a
removed `read_orders` scope. The body cap is 1 MB. The webhook ID uses the same
durable receipt and uncertain-dispatch policy as signed events. Shopify order
payloads are reduced to order ID, shop ID, created status, order name, time,
financial/fulfillment status, total price, and currency before reaching the
Agent. Buyer email, customer, address, and line-item details are excluded from
the Run prompt. Rotate the Shopify app secret in the connector, not on the
trigger. Renewing a grant for the same connector and shop keeps the webhook URL
working; revoking it without a replacement stops delivery. See Shopify's
[delivery verification](https://shopify.dev/docs/apps/build/webhooks/verify-deliveries)
for the provider's signature and duplicate-delivery contract.
Pausing a trigger discards correctly signed Shopify deliveries with a success
response so Shopify does not retry events intentionally skipped during the pause.

## Authority and recovery

The signature proves possession of this trigger's key. In authenticated deployments,
dispatch also rechecks the saved creator's current project membership and `RUN_CREATE`
permission. The request cannot choose a project, principal, provider, budget, or
permission ceiling. Each accepted event starts a separate, single main Agent Run
through `RunManager.create_and_start` with a pinned budget and trigger revision.
The event body is framed as untrusted data. Shell/delegation are unavailable;
project file writes inside an opted-in ceiling still require human permission.
An optional ceiling permits commercial proposals only after a live human approves
the stage call. Commercial outbound actions keep their separate review and apply
gate; no signed event can approve or apply them on its own.
Changing or pausing a trigger blocks later tool calls in its existing Runs.

A durable receipt is written before dispatch. If a worker fails between the receipt
and Run creation, a repeat returns `dispatch_uncertain` with no Run ID. If the Run
was created but the receipt was not updated, a repeat recovers its Run ID. The
system never starts a second Run for an uncertain event ID. Inspect project Runs
before sending a distinct event ID after an uncertain result.

In trusted gateway mode, the exact `/deliver`, `/mailgun` and `/shopify` POST paths accept
their respective event signatures without a user assertion. Deploy them behind HTTPS,
keep the management API behind
the normal authenticated gateway, and rate limit the public path. Local-only mode
accepts delivery only from loopback. Project deletion includes trigger definitions,
receipts, and their private signing keys.

## Verification boundary

Local HTTP tests cover accepted deliveries, Mailgun form/signature/recipient/token
replay handling and private key rotation, Shopify subscription creation,
lookup, uncertain retries and removal, HMAC/shop/topic/grant checks,
PII reduction, budget and permission bindings, stale
or bad signatures, schemas, duplicates, key rotation, pause/revocation, creator
membership, and interrupted receipt binding. A local desktop/mobile headless
Chromium pass verified the signed management page, creation, one-time key display, and
dismissal. A separate local desktop/mobile Chromium pass used a simulated API to
check Shopify registration and removal controls and narrow-screen layout. Real Mailgun
email and Shopify deliveries, a public HTTPS ingress, a configured
Provider Run, and deployed browser verification remain open.

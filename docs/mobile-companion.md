# Trusted mobile companion

The `/app/mobile` surface has two distinct roles. A desktop owner manages pairing
invitations and trusted devices. A phone uses only the scoped companion API after
the backend has authenticated its device session. A browser preference selects a
surface; it never grants access.

With no pairing fragment or remembered companion flag, the entry page first
checks for an existing device session. If that HTTPS-only endpoint rejects a local
desktop (for example with 403 or a disabled-service 503), it probes the normal,
authenticated `/api/mobile/status` endpoint before showing desktop controls. A
remembered companion or a browser with a pairing fragment never takes this
administrative fallback. Entry selection does not weaken either API boundary.

## Pair a device

1. Configure the backend's secure mobile service and authenticated HTTPS public
   origin. Check **Secure pairing** on the desktop. A disabled service is shown as
   disabled; there is no local-storage toggle that pretends to enable it.
2. Explicitly select the projects this phone may access. Separately select whether
   it may start new runs. The new-run capability is off by default.
3. Create a one-time link and open it privately on the intended phone. Do not put
   this link in issue trackers, logs, analytics, or shared screenshots.
4. The phone removes the pairing fragment from its address bar before making any
   request. Enter a device name and select **Request pairing**.
5. Compare the verification code on both screens. On the desktop, type the code
   shown by the intended phone, then select **Approve device**. A different code
   or unfamiliar device must be rejected. Opening a link is not approval.
6. After approval, the phone loads only authorized projects, pending permissions,
   recent runs, and project notifications. The page polls every five seconds
   while open. Device expiration and revocation remove these controls.

The desktop can revoke an invitation or trusted device. **Disconnect this device**
on the phone revokes its own server session and clears its credential cookie.

## Scope and security contract

- Administrative device operations use `/api/mobile/status` and
  `/api/mobile/pairings` behind the normal desktop authentication boundary.
- The phone uses `/api/companion/pair`, `/session`, `/overview`, scoped run start,
  run cancellation, permission decisions, and `/logout`. It does not call global
  project, permission, provider, credential, or device-management endpoints.
- The server credential is a Secure, HttpOnly, SameSite=Strict cookie. JavaScript
  sends `credentials: 'include'`; it never receives or stores a bearer token.
  Requests use `X-Ophira-Companion: 1`; writes are JSON. The server must independently
  validate origin, authentication, device lifetime, and project scope for every
  operation. UI checks do not replace those server checks.
- Pairing secrets exist in the incoming URL fragment and page memory only, never
  localStorage or sessionStorage. Only the boolean `ophira-companion-mode` flag is
  persisted so a refreshed or revoked phone does not switch to administrative APIs.
  The desktop's one-time URL is displayed only until hidden, claimed, or expired.
- Mobile permission decisions are `allow_once` or `deny`. Structured
  `AskUserQuestion` answers are preserved. Destructive approvals also require an
  explicit exact-target confirmation checkbox. Reusable grants remain a desktop
  capability and must be rejected by companion endpoints.
- Pending pairing exposes no project content. Disabled, rejected, expired,
  revoked, and failed-authorization states do not expose task controls.
- Poll and mutation generation guards discard older responses after decisions,
  disconnect, or unmount; simultaneous duplicate submissions are blocked locally.
  Durable idempotency and permission resolution remain backend responsibilities.

Use the configured same-site HTTPS deployment for the phone. A cross-site frontend
cannot rely on a SameSite=Strict API cookie; exposing a local unauthenticated API
or weakening cookie policy is not an acceptable workaround.

## Deployment configuration

The service defaults to disabled. Configure an exact HTTPS origin (no path,
credentials, query, fragment, or wildcard), then restart the API:

```dotenv
OPHIRA_COMPANION_ENABLED=true
OPHIRA_COMPANION_PUBLIC_ORIGIN=https://companion.example.com
```

Serve the frontend and `/api/companion/*` on that same origin. Keep the frontend's
API base same-origin; a build aimed at a different API origin will fail the device
cookie and origin contract. Terminate TLS at a trusted ingress that preserves the
public Host and communicates HTTPS correctly to the ASGI server. Trust forwarded
scheme headers only from that ingress, never arbitrary public clients.

In `local_only` mode, the desktop remains on direct loopback **HTTP**. The public
ingress must expose only the static frontend and `/api/companion/*`, not the normal
`/api/*` desktop endpoints. The application also rejects normal local-only API
requests arriving via HTTPS or forwarding headers when companion access is enabled.
Do not expose port 8000, change the local listener to a public address, or disable
Host/Origin validation. Authenticated hosted control planes instead retain their
existing `trusted_gateway` assertions and project-membership checks.

An invitation lasts ten minutes and is consumed once across API processes. An
approved device lasts thirty days. Approval rotates the pending cookie; only its
hash is stored in private, atomically replaced `companion-devices` records. The
store caps active devices/invitations at 25 per owner and offers at five per minute.
Revalidate the current desktop role when approving and the bound principal's
current project membership/role on each phone action. Payload reads finish before
device authorization, so a slow request cannot retain pre-revocation authority.

Deployment still needs ingress request/time/rate limits, TLS and certificate
management, private backup/retention policy for historical device records, and
production security review. This is not tenant physical isolation or an atomic
transaction spanning revocation and every cross-process external side effect.
Revocation stops future authorized requests; it does not undo already completed
work or automatically cancel previously authorized running tasks.

Only hashes of device credentials persist; the claim secret must never be logged.
The cookie and request-origin defenses follow the
[OWASP session-management guidance](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
and [CSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html).

## Verification boundary

The frontend regression tests cover desktop scope selection, explicit code
approval, rejection/revocation, fragment removal including StrictMode replay,
cookie transport/JSON headers, no secret validation echo, absence of global API
calls from remote mode, structured answers, destructive confirmation, run
start/cancel, session polling, stale-response rejection, authorization failure,
and logout.

These local tests do **not** prove a real phone's HTTPS deployment or a complete
FR-25 release. Background Web Push/native push, OS sleep prevention, and long-lived
permission-wait behavior are not implemented by this frontend change. The page
states those limits explicitly. GUI tasks still require the host device online.

The current `PermissionBroker` and active SDK task handles belong to one API
process. Route the owner and its companion to that same process; arbitrary
multi-worker routing cannot yet list/resolve another worker's live approvals or
cancel its SDK task. A cancellation reaching a non-owning worker returns HTTP 409
without changing the run or claiming cancellation; it must reach the owner. A
server restart fails interrupted runs safely, not resumes a
live permission callback. Do not call this cross-process durable permission wait.

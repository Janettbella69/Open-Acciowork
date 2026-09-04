# Feishu channel identity boundary

Feishu's body signature proves that an event reached the configured application
without modification. It does **not** prove an OPHIRA tenant, principal, role, or
project. A business event may start a Run only after the host resolves a durable,
administrator-created identity binding.

## Binding key and target

The external key is the exact tuple read from the verified and, when applicable,
decrypted event body:

- `header.tenant_key`
- `event.message.chat_id`
- `event.sender.sender_id.open_id`

The host never derives this tuple from message text, query parameters, gateway
identity headers, or top-level lookalike fields. The binding target records the
deployment `tenant_id`, exact `principal_id`, `project_id`, project `role`, and a
server-generated principal session ID. Files are stored with mode `0600` below
`data/channels/feishu-bindings/`; updates are serialized with a cross-process lock
and preserve revoked records as audit history.

Creation is idempotent for the same external tuple and the same complete target.
An active tuple cannot be rebound to a different target. Rebinding requires an
explicit revoke followed by a new create. More than one active record is treated as
integrity ambiguity and fails closed, even if the duplicate arose through manual
disk tampering.

## Authorization and dispatch

Binding administration is available only in `trusted_gateway` mode to a signed
OPHIRA `owner` or `admin`. The actor must also have
`project.access.manage` on the selected project. The requested target role is not
trusted by itself: it must exactly match the target principal's current durable
project membership and must include `run.create`.

For every signed business callback, dispatch follows this order:

1. Bound the body size, validate the Feishu timestamp/signature, and decrypt the
   official envelope.
2. Handle signed URL verification separately; check mention policy for messages.
3. Extract the exact Feishu tenant/chat/user tuple from the body.
4. Resolve exactly one active server-owned binding.
5. Revalidate its tenant, principal, role, project membership, and `run.create`
   permission against `ProjectAccessStore`.
6. Validate policy, outbound delivery configuration, and the reply target.
7. Claim the signed event identity in the durable replay guard immediately before
   crossing the Run-creation side-effect boundary.
8. Start the Run using the binding's `PrincipalBinding` and persist its outbound
   reply obligation before execution.

Missing, revoked, ambiguous, corrupt, cross-tenant, role-drifted, and membership-
revoked bindings all fail closed. The callback never falls back to a query-selected
project or the first project in the ledger. Once execution reaches the Run-creation
boundary, a retry cannot create a second Run for the same signed event.

## Scope and remaining verification

Delivered locally:

- durable binding model/store, atomic create and revoke, idempotent create;
- exact signed-body identity extraction;
- project-membership and role revalidation on every resolve;
- trusted-gateway administrator API and callback wiring;
- Run-bound reply obligations plus the official tenant-token/reply-message adapter,
  durable retry/idempotency state, startup recovery, and safe terminal failures;
- offline tests for spoofing, replay, missing/revoked/ambiguous mappings, stale
  roles, cross-tenant isolation, one-reply Run closure, retry classes, and credential
  non-disclosure.

Deferred because it requires external authority:

- a real Feishu/Lark tenant, application credentials, event subscription, test
  chat, and authorized users for end-to-end callback and reply verification;
- production physical tenant partitioning. The current deployment truth remains
  `multi_tenant=false`; this binding closes channel-to-principal dispatch locally
  but does not turn global Provider/Skill/Connector stores into multi-tenant stores.

Omitted by design:

- identity or project selection from message content, callback query parameters,
  unsigned headers, or a model/tool argument;
- automatic first-project selection;
- real Feishu calls in unit or integration tests.

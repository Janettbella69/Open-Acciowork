# Gmail connector: project Inbox reads

Gmail authorization is owned by a tenant administrator in **Connectors → Gmail**.
The Google OAuth web client receives only
`https://www.googleapis.com/auth/gmail.readonly`. OPHIRA keeps the refresh and
access tokens in the private connector vault. See [event triggers](event-triggers.md)
for public HTTPS OAuth and Pub/Sub setup. The Inbox trigger and active Agent read
are separate permissions: registering a watch does not grant a project Agent
access to historical messages.

In **Connectors → Gmail → Project access for Agents**, choose a saved project and
grant the currently authorized mailbox for 1, 7, or 30 days. The actor must be
both a tenant administrator and a project access administrator. Reauthorizing a different mailbox or
reconfiguring/revoking the connector invalidates existing grants; the
administrator must issue a new grant. The project access API is
`GET/POST /api/projects/{project_id}/gmail-access` and
`POST /api/projects/{project_id}/gmail-access/{grant_id}/revoke`. POST requires
the current `expected_revision`, and grant duration is 60 seconds to 30 days.
The API returns metadata, never tokens.

The main Agent uses `mcp__ophira_ledger__read_gmail`:

- `operation=connections` lists this project's active `grant_id` values, five
  per page with `next_offset`.
- `operation=messages` requires one `grant_id`; it accepts an optional Gmail
  search `query`, `page_size` from 1 to 5, and `page_token` from the previous
  page. Google applies `labelIds=INBOX` on the list request. OPHIRA checks each
  fetched message again and drops Sent, Draft, Trash, or no longer Inbox items.
- Results contain sender, subject, and at most 3,500 characters of plain text
  per message; HTML and attachments are not fetched. `body_truncated` tells the
  Agent when the plain text was clipped. A fixed external-data warning and the
  final SDK hook treat message text as untrusted data.
- Each message page carries a `source_receipt.id` tied to the running SDK
  session. `record_evidence` can use it; reading alone does not write an
  Evidence claim. The receipt records the exact sanitized page, mailbox grant,
  retrieval time, fixed Gmail list path, and requested search/page parameters.
  Project export includes the grant record;
  project deletion removes it.

The service checks the Run's current project, principal membership, SDK session,
grant expiry/revocation, connector revision, and mailbox identity before and
after upstream reads and again before issuing the receipt. Research subagents
cannot call this private mailbox tool. The OAuth token never appears in tool
arguments, source receipts, or API responses. The connector has no send, modify,
or delete mail operation.

Implementation and tests follow Google's
[messages.list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list),
[messages.get](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get),
and [search/filter](https://developers.google.com/workspace/gmail/api/guides/filtering)
contracts. Local tests use an isolated Google transport. A real Google account,
approved restricted scope, public HTTPS OAuth callback, and a Provider Run are
needed to validate the live path.

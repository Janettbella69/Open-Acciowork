# Outlook connector: project Inbox reads

An administrator registers a Microsoft Entra web app, configures a client secret,
and adds the exact callback shown in **Connectors → Outlook**. The app must allow
the intended account type and delegated `User.Read` and `Mail.Read` permissions.
OPHIRA requests `offline_access User.Read Mail.Read`; there is no mail send or
write permission. OAuth requires trusted gateway identity and a public HTTPS
`OPHIRA_EVENT_PUBLIC_ORIGIN`. The delegated refresh and access tokens are held
in the private connector vault and removed on connector reconfiguration or
revocation. OAuth state is one use, expires in ten minutes, and is bound to the
connector revision and OPHIRA tenant.

In **Project access for Agents**, a tenant and project administrator chooses a
saved project and grants the authorized mailbox for 1, 7, or 30 days. A grant
binds the Microsoft Graph user ID, mailbox address and connector revision.
Changing the account or connector invalidates it. Grant and revoke use an
`expected_revision`; the API returns grant metadata without tokens:

- `GET/POST /api/projects/{project_id}/outlook-access`
- `POST /api/projects/{project_id}/outlook-access/{grant_id}/revoke`

The main Agent calls `mcp__ophira_ledger__read_outlook`. `connections` lists
active project grants with offset pagination. `messages` accepts a grant ID,
page size 1–5 and a previous `next_page_token`. It lists the signed-in user's
Inbox only, follows Microsoft's entire Graph continuation URL after validating
its fixed Graph origin and Inbox path, then fetches each message with
`Prefer: outlook.body-content-type="text"`. A fetched item must still be in
Inbox and not be a draft. The Agent receives sender, subject, time, and at most
3,500 characters of plain text per message; HTML responses fail closed and
attachments are never fetched. The connector currently pages recent Inbox
mail; it does not offer server-side mail search. New Inbox messages can
separately start Runs through a [Graph subscription trigger](event-triggers.md#direct-outlook-inbox-trigger).

Each successful page has a fixed Graph source path and a same Run/SDK session
receipt usable with `record_evidence`. Reads themselves do not assert Evidence.
Run identity, project membership, grant expiry, mailbox identity and connector
revision are rechecked around upstream reads and receipt creation. Research
subagents cannot use the private mailbox tool. Project export includes grant
metadata, and project deletion removes it.

This follows Microsoft's [delegated authorization](https://learn.microsoft.com/en-us/graph/auth-v2-user),
[message list/paging](https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0),
and [plain-text message](https://learn.microsoft.com/en-us/graph/api/message-get?view=graph-rest-1.0)
contracts. Local tests use a simulated Graph transport. A real Entra app,
Outlook mailbox, public HTTPS callback and Provider Run are needed to verify
the live service path.

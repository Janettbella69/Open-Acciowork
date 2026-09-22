# Reusable project templates

In a saved workspace, open **Project templates** to curate a reusable project brief
and explicitly select saved reference-file versions. Review the title, instructions,
markets, constraints and file checksums before saving. Each save creates an immutable
version; previous versions remain inspectable and downloadable. The current template
revision protects version creation, archive and restore from concurrent edits.

Select a version and choose **Create project from this version**. Edit the new
project's name, objective and brief, choose which reference files to copy, then review
and confirm. Creation opens a separate project. Sending its first message is a separate
user action; template creation never starts a Run or invokes a model.

## What a template carries

- A human-curated name, objective, product brief, target markets and constraints.
- Explicitly selected artifact IDs and versions, with name, MIME type, size and SHA-256.
  Bytes are copied into private, content-addressed template storage when the version is
  saved. Later source-workspace edits, new artifact versions or deletion of the working
  file do not change the saved template reference.
- Creator, timestamp, source-project revision, immutable version content hash, and
  append-only create/version/archive/restore history.

A new project starts with its own empty evidence, hypotheses, assessments and decisions.
Members, sessions, schedules, permissions, shops, Connector credentials and execution
configuration are established independently. Selected files become new artifact IDs
with fresh project-local source links. PDF/image originals use the normal bounded
Markdown ingestion path; extraction failures retain truthful diagnostics. Original
bytes remain downloadable. Template references are reference material, not verified
business evidence.

`OpportunityProject.template_origin` records the source project/template/version,
version hash, personalized seed hash, creation application, human actor and the mapping
between new artifacts and their source versions. This provenance survives subsequent
project edits and remains visible in the new project. It does not grant access to the
source project.

## Authority and storage

The local operator manages templates for local projects. In a signed organization
deployment, existing project read permission controls the library, version inspection
and downloads. Project writers can create templates. A template's author, owner or
administrator can create versions and archive/restore it, provided they retain project
write permission. Reviewers and viewers are read-only.

Creating a project retains the existing authenticated-owner requirement. A member can
curate a shared template, while the source project's owner creates the new project with
only their own owner membership. The service never turns a member assertion into an
owner assertion. Saved creation history is visible only to its creator. Tenant binding,
membership, asserted role and assertion expiry are checked again under the source
project access lease and before publication. HTTP responses, including denials, use
`Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.

Templates and creation journals live outside the Agent-writable workspace:

```text
data/collaboration/project-templates/{project_id}/{template_id}/
  template.json
  artifact-versions/{sha256}
  applications/{application_id}.json
```

Structured text is redacted before persistence. Selected file contents retain their
reviewed original bytes. Models reject extra input fields that could import project
truth or execution authority. Record reads reject malformed identity/history, content
hash mismatches, linked files and mismatched scope. Source references must belong to
the current project and be available as the exact selected version.

Current bounds: 50 templates per source project, 50 versions and 200 mutations per
template, 20 references totaling 50 MiB per version, 200 MiB of reference history per
template, 500 creation records per template, 2 MiB per JSON record. Project seeds have
a 64 KiB text budget plus individual field limits. HTTP save/apply bodies are bounded
at 256 KiB; archive/resume requests at 1 KiB. Capacity failures preserve published
versions and projects. Archives retain bytes and history and continue to count toward
these bounds.

## Interrupted creation and cold backup

Creation persists its redacted reviewed input before copying files. A private staging
ledger builds the complete new project and validates its bytes and origin. The service
flushes files and records a prepared-project hash, then publishes the directory through
an atomic exclusive rename. It cannot replace an existing project directory. Cross-process
source and target locks serialize competing requests.

The journal progresses through `preparing`, `ready`, and `applied`. Repeating the same
actor/project/template request ID and exact input returns the same project. Reusing
the key with different input returns a conflict. If the response is lost after
publication, retry returns the current saved project and retains later human edits.

After closing or restarting the browser, reopen the source template and **Your project
creations**. Inspect the saved settings and references, then **Confirm and resume
creation**. The stored application is sufficient; the old browser draft and old
assertion/session are not required. Source access and ownership must still be valid.
An archived template must be restored before an unpublished creation can continue.
Corrupt/missing prepared files fail verification instead of publishing a partial project.

Application cold backup validates every template manifest, frozen reference, completed
creation and target-project origin. A pending template save, incomplete creation or
uncleaned creation stage blocks backup. Complete its reviewed retry on the original
installation before making a cold copy. Completed templates and projects restore into
a new data directory without starting jobs or model sessions. See
[Application cold backup](application-backup.md).

## API

All paths below start with `/api/projects/{project_id}/templates`.

| Method and suffix | Operation |
| --- | --- |
| `GET /` | Scoped library and current capabilities |
| `POST /` | Save a new template with `request_id`, curated `seed`, title/description and `artifact_ids` |
| `POST /{template_id}/versions` | Save an immutable version with `expected_revision` |
| `GET /{template_id}/versions/{version}` | Inspect the selected version and current template metadata |
| `GET /{template_id}/versions/{version}/references/{artifact_id}/download` | Download a verified frozen reference |
| `POST /{template_id}/archive` | Reviewed archive/restore with revision and request ID |
| `POST /{template_id}/applications` | Create from a reviewed version/hash, personalized seed and selected references |
| `GET /{template_id}/applications` | Creator's durable creation history; `before` cursor and bounded `limit` |
| `POST /{template_id}/applications/{application_id}/resume` | Resume after explicit `confirm_creation: true` |

## Verification boundary

Automated coverage includes fixed source versions, new project-local artifacts and
Markdown companions, absence of copied business truth and Runs, membership boundaries,
late assertion expiry, conflict/replay behavior, corrupt and linked references,
redaction and quotas, two real competing processes, abrupt process exit immediately
before publication and immediately after publication, and cold backup/restore.

The React tests cover explicit review, reference selection, retained retry keys,
creation-history recovery, archive revision, read-only roles, Chinese copy, StrictMode,
and responses arriving after navigation. Runtime tests verify that a late initial
project-list response cannot remove the newly created project or start a Run.

Run the independent-process loopback HTTP exercise from the repository root:

```bash
OPHIRA_CONTRACT_PROVIDER= PYTHONPATH=backend/src .venv/bin/python backend/scripts/check_project_templates.py
```

It uses temporary files and a separate server process, checks two immutable versions,
downloads the original and copied reference, retries creation without duplication,
checks archive enforcement and verifies zero Runs. It does not test Provider execution.
Browser discovery currently has no connected instance, so browser visual acceptance
remains open. Production organization identity integration, long-term capacity and
cross-host recovery certification remain separate requirements.

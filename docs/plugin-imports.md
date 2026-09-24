# Claude plugin ZIP imports (FR-39)

Administrators can upload a ZIP containing one `.claude-plugin/plugin.json` from
**Plugins → Import a plugin package**. The archive may have one enclosing folder.
The importer recognizes default `skills/<name>/SKILL.md` packages, additional
directories named by the manifest `skills` field, and root `SKILL.md` when no
`skills/` directory exists. The manifest name, version, description, archive
SHA-256, imported Skill records, and ignored components are retained in a
deployment-scoped import report. Uploads are idempotent by request ID and bytes.
Application cold backups verify report identity, scope, and every referenced
Skill import before restoration; MCP-only packages bind the same scope.

Every discovered Skill becomes a separate frozen private Skill import. The
existing static scan, exact-file review, owner/admin approval, disabled
publication, and later explicit enablement apply to each one. A failed scan
cannot be overridden. Candidate metadata for all Skills is checked before any
child import is staged; a crash during staging may leave individually reviewable
pending imports, and retrying the same package reconciles them. Approved Skills
run under the managed `ophira-tenant` Plugin namespace with immutable per-Run
snapshots. The source plugin name remains in the import report.

The importer extracts compatible HTTPS Streamable HTTP MCP addresses from
`.mcp.json` or manifest `mcpServers` declarations. **It never activates them.**
The report links to project connector setup with the address prefilled. FR-33
then enforces the organization host allowlist, live tool discovery, per-tool
allow/ask/deny policy, and runtime gateway checks. MCP credentials declared in
the plugin configuration are neither returned nor imported; the operator enters
them separately. Local
command/stdio and unsafe MCP definitions are reported as ignored because the
project MCP boundary accepts remote HTTPS only.
OAuth declarations are reported but not configured; FR-33 still needs an
operator-supplied supported credential and does not perform OAuth sign-in.

Plugin `agents/` files and manifest agent paths are explicitly reported and
ignored. Hooks, commands, workflows, LSP, output styles, settings, monitors,
and other executable plugin components are not loaded. The uploader accepts at
most 2 MB compressed, 5 MB expanded, 256 entries, 20 Skills, and 20 compatible
remote MCP addresses. It rejects traversal, links, special files, ambiguous
paths, duplicate manifest keys, and files outside the plugin root.

The app implements the supported component import flow, not Claude Code's
marketplace installation or runtime plugin update mechanism. Additional real
plugin packages, remote MCP handshakes, and an authenticated deployment remain
acceptance work. [Claude's plugin reference](https://code.claude.com/docs/en/plugins-reference)
defines the input layout and manifest fields.

Compatibility check on 2026-09-24: a temporary checkout of
[Anthropic's marketing plugin](https://github.com/anthropics/knowledge-work-plugins/tree/da38ec1ee89d41e5380e652a97382695003396e7/marketing)
at `da38ec1ee89d41e5380e652a97382695003396e7` staged all eight Skills with
passing static scans and listed 11 compatible remote MCP addresses. Its two
empty URL entries were ignored, and its OAuth declaration was reported without
importing the OAuth configuration. This check did not connect to MCP servers,
activate Skills, or invoke a model.

# Published artifact pages (FR-41)

Configure trusted-gateway mode with an operator-owned, separate HTTPS host:

```text
OPHIRA_SITE_PUBLIC_ORIGIN=https://pages.example.com
```

Route only `/published/<project-id>/<site-id>` from that host to the backend.
The backend refuses other paths on the site host and refuses public pages on the
control-plane host. Keep the normal authenticated API on its own host. Publish
directly from **Project files → Published pages** in the workspace.

Select an immutable saved `.html` or `.htm` artifact version of at most 512 KiB.
For an interactive page, explicitly select **Run inline scripts** when staging.
The script setting is frozen into the draft and included in the approval check.
Existing publications and new drafts without this choice keep scripts disabled.
Staging records its exact artifact ID and SHA-256 without making it public. A
project administrator reviews the sandboxed preview and exact hash, explicitly
approves publication, and can withdraw the page later. A changed artifact creates
a new version; it cannot silently replace a published page. Publication and
withdrawal states are durable, so the list can reconcile a lost HTTP response.
Project deletion removes the owned publication records and artifact snapshots,
making their public URLs unavailable.

The public response contains the exact saved UTF-8 HTML and sets `Cache-Control:
no-store`. Static pages keep the no-script [CSP sandbox](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/sandbox).
Explicitly reviewed interactive pages use `sandbox allow-scripts` with an opaque
origin, allowing inline JavaScript and event handlers while denying origin
storage, forms, framing, remote scripts, background requests, and remote assets.
The review iframe uses the matching script mode and another sandbox layer.
Neither mode allows `allow-same-origin` or access to control-plane APIs. A
public top-level page can still link or navigate visitors to another site, so
the administrator must review the HTML and its scripts before approval.
Self-contained HTML can include inline CSS and data images. External assets
and hosted JavaScript bundles are not supported yet.

Local API tests cover staging, explicit approval, byte pinning across artifact
edits, host separation, withdrawal, and project deletion. Chromium review at
1360px and 390px uses a local simulated gateway to exercise staging and
approval. Real Chromium with actual ASGI responses verifies that old static
pages keep scripts inert; an opted-in page and its review iframe run inline
scripts, while storage and background network attempts are blocked. Real public
HTTPS routing, CDN behavior, wider browser compatibility, and a live generated
page remain deployment acceptance work.

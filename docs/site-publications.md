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
Staging records its exact artifact ID and SHA-256 without making it public. A
project administrator reviews the sandboxed preview and exact hash, explicitly
approves publication, and can withdraw the page later. A changed artifact creates
a new version; it cannot silently replace a published page. Publication and
withdrawal states are durable, so the list can reconcile a lost HTTP response.
Project deletion removes the owned publication records and artifact snapshots,
making their public URLs unavailable.

The public response contains the exact saved UTF-8 HTML. It sets `Cache-Control:
no-store`, a no-script [CSP sandbox](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/sandbox),
and a policy that allows inline CSS and data images while blocking scripts,
forms, remote resources, framing, and origin privileges. This supports static
landing pages and site prototypes made from self-contained HTML. Pages that need
JavaScript or external assets require a separately isolated publishing runtime;
this implementation does not claim that capability.

Local API tests cover staging, explicit approval, byte pinning across artifact
edits, host separation, withdrawal, and project deletion. Chromium review at
1360px and 390px uses a local simulated gateway to exercise staging and
approval; the browser CSP check confirms that inline CSS renders while script
execution and origin storage fail. Real public HTTPS routing, CDN behavior,
browser compatibility, and a live generated page remain deployment acceptance
work.

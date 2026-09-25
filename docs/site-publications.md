# Published artifact pages (FR-41)

Configure trusted-gateway mode with an operator-owned, separate HTTPS host:

```text
OPHIRA_SITE_PUBLIC_ORIGIN=https://pages.example.com
```

Route only `/published/<project-id>/<site-id>` and its `/assets/` children from
that host to the backend.
The backend refuses other paths on the site host and refuses public pages on the
control-plane host. Keep the normal authenticated API on its own host. Publish
directly from **Project files → Published pages** in the workspace.

Select an immutable saved `.html` or `.htm` artifact version of at most 512 KiB,
or a site `.zip` of at most 12 MiB. A ZIP contains `index.html` at the root and
up to 32 files in `assets/` with flat names. CSS, classic or module JavaScript
(`.js`), PNG, JPEG, WebP, GIF, and ICO are supported. Modules may import other
relative `.js` files in `assets/`. The expanded archive is capped at
20 MiB and each asset at 4 MiB. The HTML must use relative paths such as
`assets/site.css`; the ZIP page URL ends with `/` so these paths resolve inside
that one publication. Other files, traversal, symlinks, ambiguous duplicates,
encrypted entries and unsupported compression are rejected. A site ZIP can be
prepared in the project workspace or uploaded as a project file, then saved as
an Artifact version.

For an interactive page, explicitly select **Run page scripts** when staging.
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
origin, allowing inline JavaScript and only the ZIP's local JS files while
denying origin storage, forms, framing, third-party scripts and resources, and
background requests. CSS and image assets are allowed only under that site's
fixed asset path. ZIP assets are read from the same checksum-pinned artifact;
they become unavailable with the page on withdrawal or project deletion.
The review iframe uses the matching script mode and another sandbox layer.
Neither mode allows `allow-same-origin` or access to control-plane APIs. A
public top-level page can still link or navigate visitors to another site, so
the administrator must review the HTML and its scripts before approval.
Self-contained HTML can include inline CSS and data images. Remote CDNs,
fonts, media, and third-party network APIs remain outside this publication
format. A direct page visit can still follow normal links.

Sandboxed draft frames have an opaque origin and may omit the control-plane
session cookie on asset requests. For every ZIP draft, the authenticated preview
entry redirects to a five-minute, read-only capability URL on the control-plane
host. The URL is signed against the exact project, site, artifact checksum,
draft revision, host, and expiry. No credentials are needed on its HTML or
asset requests. Publishing, withdrawing, deleting, changing the origin or
rotating the gateway secret invalidates it. Refresh the preview to get a new
URL if a late module import fails after expiry. The response is never cached;
the iframe and page send no referrer. Redact the capability segment from
gateway and application access logs, and exclude these URLs from analytics.

Module requests from the sandbox send `Origin: null`. JavaScript responses
under a valid capability (or from a published site) return
`Access-Control-Allow-Origin: null` without credential access; protected draft
asset URLs never return CORS permission. The bearer URL, rather than `null`
origin, authorizes private bytes. Any opaque-origin document that learns the
URL can use it until expiry, including if reviewed page code deliberately
discloses it. Review the exact HTML and scripts before publication.

Local API tests cover staging, explicit approval, byte pinning across artifact
edits, host separation, withdrawal, and project deletion. Chromium review at
1360px and 390px uses a local simulated gateway to exercise staging and
approval. Real Chromium with actual ASGI responses verifies that old static
pages keep scripts inert; an opted-in page and its review iframe run scripts,
and ZIP page and preview load fixed CSS, image, classic JS and module imports
while storage and background network attempts are blocked. Real public
HTTPS routing, CDN behavior, wider browser compatibility, and a live generated
page remain deployment acceptance work.

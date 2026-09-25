# macOS desktop host and window capture (FR-42, in progress)

This native AppKit/WebKit shell starts the existing Python control plane as a
local sidecar. It serves the built React UI from the same loopback origin as the
API. A fresh 256-bit session token travels from the shell to the sidecar over
stdin; the webview receives an HTTP-only, SameSite=Strict session cookie. The
sidecar rejects requests with no token, a different Host/Origin, or a non-loopback
client. The token is absent from URLs, process arguments, environment variables,
and backend logs.

## Build and run from source

The development app requires this repository's Python 3.11 `.venv` with backend
dependencies installed. It copies the built UI, backend source, and capability
plugin into the app bundle but does not yet bundle a portable Python runtime.

```bash
cd frontend && npm ci && npm run build
cd ..
zsh desktop/macos/build-app.sh
open desktop/macos/build/OPHIRA.app
```

`OPHIRA_DESKTOP_PYTHON` can override the development Python executable at build
or launch time. The native app stores project data in the current user's
`Application Support/OPHIRA` directory, separate from `backend/data`.
`OPHIRA_DESKTOP_DATA_DIR` selects an isolated data directory for local testing.

## Native window capture

In **Settings → Organization policy**, enable native desktop capture and add
exact macOS bundle IDs for approved non-browser apps, one per line. Both controls
default to off/empty. In an attended active Run, the main Agent can request
`select_window`, which opens the macOS system window picker. After the user
selects a window and approves the next invocation, `snapshot_window` captures
only that selection. The host checks the selected window's ID, owning process,
and bundle ID again before recording a PNG as a Run-linked source receipt,
Evidence, and Artifact. Known browser bundles are refused so web browsing stays
with the URL/domain-gated L1/L2 tools.

The native command channel uses the already-private parent/child pipes. There
is no native-command HTTP route. The cookie-protected `/api/desktop/status`
reports the host's Screen Recording and Accessibility permission state. macOS
may ask for Screen Recording access when selecting or capturing a window.
Approval inside OPHIRA does not grant macOS privacy permission.

## Remaining FR-42 work

- Add an Accessibility action bridge for click, text entry, and key actions,
  with target validation and one human approval per action.
- Exercise the system picker, Screen Recording prompts, window capture, and
  cancellation against controlled native apps on a clean Mac. Known browser
  bundle exclusion is a finite list; the exact native-app allowlist is the
  primary boundary for apps that contain embedded web views.
- Package and sign a standalone Python runtime, then exercise macOS privacy
  prompts, real OS actions, cancellation, restart, and recovery on a clean Mac.

The current host exposes selected-window capture to an attended Agent Run, but
does not yet perform OS input actions.

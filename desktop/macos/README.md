# macOS desktop host and accessible window actions (FR-42, in progress)

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

## Native window capture and input

In **Settings → Organization policy**, enable native desktop control and add
exact macOS bundle IDs for approved non-browser apps, one per line. Both controls
default to off/empty. In an attended active Run, the main Agent can request
`select_window`, which opens the macOS system window picker. After the user
selects a window and approves the next invocation, `snapshot_window` captures
only that selection. The host checks the selected window's ID, owning process,
and bundle ID again before recording a PNG as a Run-linked source receipt,
Evidence, and Artifact. Known browser bundles are refused so web browsing stays
with the URL/domain-gated L1/L2 tools.

After a screenshot, the main Agent may request `press_window_control` with the
exact screenshot receipt, pixel coordinate, and visible control label. Each press
requires separate approval. The native host checks the same window and process,
unchanged bounds, a unique matching Accessibility window, and a control whose
label matches the request and supports `AXPress`. It supports accessible buttons,
checkboxes, radio buttons, popups, and menu items. A screenshot can authorize
only one input action. The action receives its own Run-linked receipt, and a new
screenshot is required to verify the resulting UI. macOS Accessibility access
must be granted to the desktop app. An uncertain OS or pipe result must not be
retried without a fresh screenshot and human review.

`replace_window_text` replaces the complete value of a labelled `AXTextField` or
`AXTextArea` at a screenshot coordinate. The caller supplies the complete current
value, which must still match the native Accessibility value. Secure text fields
are refused. The 4000-byte combined text limit keeps the private command bounded;
the receipt stores text lengths and SHA-256 hashes, not the field contents.

`send_window_key` dispatches one unmodified Return, Escape, Tab, Space,
Backspace, or arrow key to the selected app's process. The native host checks the
exact screenshot window title and the app's focused Accessibility window first.
It refuses dispatch while a physical modifier is held and uses an independent
event source with no modifier flags. The result is `dispatched`: Core Graphics
provides no delivery acknowledgement,
so a fresh screenshot is needed to observe what changed. Modifier shortcuts and
arbitrary coordinate clicks are not available.

The native command channel uses the already-private parent/child pipes. There
is no native-command HTTP route. The cookie-protected `/api/desktop/status`
reports the host's Screen Recording and Accessibility permission state; the
organization settings page displays and refreshes it. Desktop ingress rejects
WebSocket connections. macOS may ask for Screen Recording access when selecting
or capturing a window.
Approval inside OPHIRA does not grant macOS privacy permission.

## Remaining FR-42 work

- Extend action coverage for native apps whose controls do not expose `AXPress`
  or a writable Accessibility text value, and assess modifier shortcuts.
- Exercise the system picker, Screen Recording prompts, window capture, and
  actual press, text, key, and cancellation against controlled native apps on a
  clean Mac.
  Known browser bundle exclusion is a finite list; the exact native-app
  allowlist is the primary boundary for apps that contain embedded web views.
- Package and sign a standalone Python runtime, then exercise macOS privacy
  prompts, real OS actions, cancellation, restart, and recovery on a clean Mac.

The current host exposes selected-window capture, bounded `AXPress`, text
replacement, and unmodified keys to an attended Agent Run. These input paths
have compiled and synthetic protocol tests; real OS input has not yet been
exercised.

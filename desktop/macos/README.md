# macOS desktop host (FR-42, in progress)

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

## Remaining FR-42 work

- Add a native ScreenCaptureKit window picker and Accessibility action bridge,
  with session-scoped window selection and one human approval per action.
- Register L3 screenshots as Run-linked source receipts, Evidence, and Artifacts;
  enforce organization policy and browser domain rules at execution time.
- Package and sign a standalone Python runtime, then exercise macOS privacy
  prompts, real OS actions, cancellation, restart, and recovery on a clean Mac.

The current host supplies a private desktop UI and sidecar only. It does not
expose an OS control tool to the Agent.

"""Quiescence boundary for online erasure: HTTP streams and detached work."""

import asyncio
from functools import wraps

from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from .erasure_files import DataOperationBusy, operation_lease


def data_operation(*, wait=False):
    """Protect an async service method whose owner carries the ledger port.

    Detached Run tasks wait instead of losing a queued Run to a short maintenance
    window. Periodic services skip a busy tick through their existing retry loop.
    Leases are not inherited through contextvars: each detached task owns an fd.
    """

    def decorate(method):
        @wraps(method)
        async def guarded(self, *args, **kwargs):
            while True:
                lease = operation_lease(self.ledger.root)
                try:
                    lease.__enter__()
                except DataOperationBusy:
                    if not wait:
                        raise
                    await asyncio.sleep(0.05)
                else:
                    break
            try:
                return await method(self, *args, **kwargs)
            finally:
                lease.__exit__(None, None, None)

        return guarded

    return decorate


def protected_call(root, operation, *args, **kwargs):
    """Own the lease inside a thread even if its requesting coroutine disconnects."""
    with operation_lease(root):
        return operation(*args, **kwargs)


class DataOperationMiddleware:
    """Hold the gate until the ASGI response/stream has actually closed."""

    def __init__(self, app, root, cors_origins=()):
        self.app, self.root = app, root
        self.cors_origins = cors_origins

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/api" or path.startswith("/api/")):
            return await self.app(scope, receive, send)
        lease = operation_lease(self.root)
        try:
            lease.__enter__()
        except DataOperationBusy as exc:
            response = JSONResponse(
                status_code=409,
                content={"detail": str(exc)},
                headers={"Cache-Control": "no-store", "Retry-After": "1"},
            )
            # Reuse the configured CORS policy only for this early response.
            # Normal traffic still checks local origin before reaching CORS.
            # A busy preflight is refused directly, never granted by this gate.
            if scope.get("method") != "OPTIONS":
                response = CORSMiddleware(
                    response,
                    allow_origins=self.cors_origins,
                    allow_credentials=True,
                )
            return await response(scope, receive, send)
        try:
            return await self.app(scope, receive, send)
        finally:
            lease.__exit__(None, None, None)

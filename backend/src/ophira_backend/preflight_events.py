"""Durable, credential-free observations of Provider checks outside project Runs."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
from collections.abc import Awaitable, Callable, Iterator
from contextlib import ExitStack, contextmanager

from .constants import EventType, ProviderErrorCode
from .models import EventActor, ProviderPreflightResult, new_id
from .providers import ProviderRegistry, classify_provider_error, provider_error_message
from .scoped_events import ScopedEventStore
from .storage import FileLedger


class PreflightBusyError(RuntimeError):
    pass


class PreflightEventService:
    def __init__(self, ledger: FileLedger, providers: ProviderRegistry) -> None:
        self.ledger = ledger
        self.providers = providers
        self.events = ScopedEventStore(ledger)

    @contextmanager
    def _lease(self, provider_id: str) -> Iterator[bool]:
        # A separate nonblocking OS lease may span awaits. Registry transactions
        # remain short, so credential revocation never waits for a model call.
        digest = hashlib.sha256(provider_id.encode()).hexdigest()
        descriptor = os.open(
            self.ledger._locks_dir / f"preflight-{digest}.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            os.close(descriptor)

    def _repair(self, stream: str) -> None:
        # The same Provider lease protects repair AND every check. A new start
        # cannot pass an unrepaired older start, so only the tail can be pending.
        # Storage still validates the complete bounded log before returning it.
        last = self.events.last_sequence(stream)
        tail = self.events.read(stream, max(0, last - 1), 1)
        if tail and tail[0].type == EventType.PROVIDER_PREFLIGHT_STARTED:
            started = tail[0]
            identifier = started.data["preflight_id"]
            self.events.append(
                stream, EventType.PROVIDER_PREFLIGHT_FAILED, identifier,
                {**started.data, "status": "failed", "success": False,
                 "reason": "interrupted", "code": ProviderErrorCode.SDK_RUNTIME_FAILED},
                trace_id=started.trace_id, actor=EventActor(type="system", name="preflight"),
            )

    def reconcile(self, provider_id: str, tenant_id: str | None = None) -> None:
        with self._lease(provider_id) as acquired:
            if acquired:
                self._repair(self.events.provider_stream(provider_id, tenant_id))

    async def execute(
        self, provider_id: str, check: Callable[[str], Awaitable[ProviderPreflightResult]],
        tenant_id: str | None = None,
    ) -> ProviderPreflightResult:
        with ExitStack() as stack:
            # History reconciliation takes the same lease briefly. Let that
            # reader finish before declaring a competing check busy; otherwise
            # opening the status panel can spuriously reject its own Run button.
            for attempt_number in range(41):
                with ExitStack() as attempt:
                    if attempt.enter_context(self._lease(provider_id)):
                        stack.enter_context(attempt.pop_all())
                        break
                if attempt_number == 40:
                    raise PreflightBusyError("This Provider check is busy. Try again shortly.")
                await asyncio.sleep(0.025)
            stream = self.events.provider_stream(provider_id, tenant_id)
            self._repair(stream)
            profile = self.providers.get(provider_id)
            identifier = new_id("preflight")
            trace = new_id("trace")
            common = {"schema_version": 1, "preflight_id": identifier,
                      "configuration_revision": profile.configuration_revision}
            actor = EventActor(type="system", name="preflight")
            self.events.append(
                stream, EventType.PROVIDER_PREFLIGHT_STARTED, identifier,
                {**common, "status": "started"}, trace_id=trace, actor=actor,
            )
            try:
                result = await check(provider_id)
            except asyncio.CancelledError:
                self.events.append(
                    stream, EventType.PROVIDER_PREFLIGHT_FAILED, identifier,
                    {**common, "status": "failed", "success": False,
                     "reason": "interrupted", "code": ProviderErrorCode.SDK_RUNTIME_FAILED},
                    trace_id=trace, actor=actor,
                )
                raise
            except Exception as exc:
                code = classify_provider_error(exc)
                result = ProviderPreflightResult(
                    provider_profile_id=provider_id, ok=False, error_code=code,
                    message=provider_error_message(code),
                )
            changed = (self.providers.get(provider_id).configuration_revision
                       != profile.configuration_revision)
            if changed:
                result = result.model_copy(update={
                    "ok": False, "error_code": ProviderErrorCode.SDK_RUNTIME_FAILED,
                    "message": "Provider configuration changed; run preflight again.",
                })
            self.events.append(
                stream, (EventType.PROVIDER_PREFLIGHT_PASSED if result.ok
                         else EventType.PROVIDER_PREFLIGHT_FAILED), identifier,
                {**common, "status": "passed" if result.ok else "failed", "success": result.ok,
                 "code": result.error_code,
                 "reason": "configuration_changed" if changed else None},
                trace_id=trace, sdk_session_id=result.sdk_session_id, actor=actor,
            )
            return result

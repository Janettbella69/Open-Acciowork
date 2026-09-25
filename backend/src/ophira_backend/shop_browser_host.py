"""One persistent, visible Chromium session per shop, served over a private socket."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .auth import AuthorizationError, ProjectAccessStore, ProjectPermission
from .browser_tools import (
    _BLOCK_SERVICE_WORKER_REGISTRATION,
    _check_browser_target,
    _install_cdp_request_guard,
    _install_request_guard,
    _remove_cdp_request_guard,
    domain_allowed,
)
from .connector_run_access import authorize_connector_run
from .maintenance import data_directory_lease
from .models import OrgPolicy, new_id, utc_now
from .policy import OrgPolicyStore
from .shop_browser_store import (
    MAX_SHOP_FILE_BYTES,
    MAX_SHOP_RPC_BYTES,
    ShopBrowserCaller,
    ShopBrowserError,
    ShopBrowserOperation,
    ShopBrowserStore,
)
from .storage import FileLedger
from .untrusted import UntrustedSource, sanitize_untrusted_payload


@contextmanager
def session_lease(store: ShopBrowserStore, shop_id: str):
    shop = store.get(shop_id)
    with store.ledger.lifecycle.lease(shop.project_id):
        path = store.directory(shop_id) / "owner.lock"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(descriptor)


@contextmanager
def launch_lease(store: ShopBrowserStore, shop_id: str):
    shop = store.get(shop_id)
    with store.ledger.lifecycle.lease(shop.project_id):
        path = store.directory(shop_id) / "launch.lock"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(descriptor)


class ShopBrowserPolicy:
    """Read current organization policy for each browser request, intersected with shop scope."""

    def __init__(self, shop, store: OrgPolicyStore):
        self.shop, self.store = shop, store
        self.authorize = None

    @property
    def browser_enabled(self):
        if self.authorize:
            try:
                self.authorize()
            except (AuthorizationError, ShopBrowserError, ValueError, OSError):
                return False
        return self.store.get().browser_enabled

    @property
    def domain_allowlist(self):
        domains = self.shop.domains
        organization = self.store.get().domain_allowlist
        if not organization:
            return domains
        narrowed = {
            domain for domain in domains if domain_allowed(f"https://{domain}", organization)
        }
        narrowed.update(
            domain for domain in organization if domain_allowed(f"https://{domain}", domains)
        )
        # Empty means unrestricted to the shared browser guard; use an impossible host instead.
        return sorted(narrowed) or ["shop-access-disabled.invalid"]


def source_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def authorize_caller(store, access, shop_id, session_id, caller, operation):
    shop = store.get(shop_id)
    project = store.ledger.get_project(caller.project_id)
    if (
        shop.project_id != project.id
        or shop.tenant_id != project.tenant_id
        or shop.identity_binding != project.identity_binding
        or shop.revision != caller.shop_revision
    ):
        raise AuthorizationError("Shop identity or access changed")
    if caller.run_id:
        run = authorize_connector_run(store.ledger, access, project, caller.run_id)
        if (
            run.sdk_session_id != caller.sdk_session_id
            or not shop.authorized(session_id)
            or operation.operation == "close"
        ):
            raise AuthorizationError("Shop or SDK session access changed")
    elif project.identity_binding == "local_only":
        if caller.principal is not None:
            raise AuthorizationError("Local administration does not accept a principal")
    elif caller.principal is None:
        raise AuthorizationError("Shop administration requires a principal")
    else:
        access.authorize(project.id, caller.principal, ProjectPermission.PROJECT_ACCESS_MANAGE)


async def execute_authorized(driver, operation, authorize, reader):
    """Cancel in-flight work on client disconnect, grant change or Run termination."""
    authorize()
    driver.policy.authorize = authorize
    task = asyncio.create_task(driver.execute(operation))
    disconnected = asyncio.create_task(reader.read(1))
    try:
        while not task.done():
            await asyncio.wait(
                {task, disconnected}, timeout=0.05, return_when=asyncio.FIRST_COMPLETED
            )
            if disconnected.done():
                raise AuthorizationError("Browser caller disconnected")
            authorize()
        authorize()
        return await task
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        driver.discard_capture()
        raise
    finally:
        task.cancel()
        disconnected.cancel()
        await asyncio.gather(task, disconnected, return_exceptions=True)
        driver.policy.authorize = None


class ShopBrowserPage:
    def __init__(self, store: ShopBrowserStore, shop_id: str, session_id: str, policy: OrgPolicy):
        self.project_id = store.get(shop_id).project_id
        with store.ledger.lifecycle.lease(self.project_id):
            self.store, self.shop_id = store, shop_id
            self.session_id, self.policy = session_id, policy
            self.page = None
            self.observation_id = None
            self.observed_url = None
            self.elements = {}
            self.blocked = []
            self.capture_paths = set()
            self.expecting_download = False
            self.download_tasks = set()
            self.staging = store.directory(shop_id) / "staging"
            self.staging.mkdir(mode=0o700, exist_ok=True)
            if self.staging.is_symlink():
                raise ValueError("Invalid browser staging directory")

    def discard_capture(self):
        for path in self.capture_paths:
            path.unlink(missing_ok=True)
        self.capture_paths.clear()

    def download_started(self, download):
        if self.expecting_download:
            self.expecting_download = False
            return

        async def discard():
            try:
                await download.cancel()
                await download.delete()
            except Exception:
                pass  # Context shutdown also deletes its temporary downloads.

        task = asyncio.create_task(discard())
        self.download_tasks.add(task)
        task.add_done_callback(self.download_tasks.discard)

    def prepare_staging(self):
        total = 0
        for path in self.staging.iterdir():
            if path.is_symlink() or not path.is_file():
                raise ShopBrowserError("SHOP_CAPTURE_INVALID", "Invalid staging file.")
            info = path.stat()
            if info.st_mtime < time.time() - 600:
                path.unlink()
            else:
                total += info.st_size
        if total > 4 * MAX_SHOP_FILE_BYTES:
            raise ShopBrowserError("SHOP_CAPTURE_TOO_LARGE", "Browser staging is full.")

    async def _safe_page(self):
        reason = await _check_browser_target(self.page.url, self.policy)
        if reason:
            raise ShopBrowserError(
                "SHOP_PAGE_OUT_OF_SCOPE", "The page is outside this shop's domains."
            )
        if await self.page.locator(
            'input[type="password"]:visible, input[autocomplete="one-time-code"]:visible',
        ).count() or re.search(
            r"/(?:login|signin|sign-in|oauth|captcha)(?:/|$)", urlsplit(self.page.url).path
        ):
            raise ShopBrowserError(
                "SHOP_LOGIN_REQUIRED",
                "Complete sign-in or verification in the shop browser yourself.",
            )
        if self.blocked:
            raise ShopBrowserError(
                "SHOP_RESOURCE_BLOCKED",
                "A page resource was blocked. Review permitted domains.",
            )

    async def snapshot(self) -> dict:
        with self.store.ledger.lifecycle.lease(self.project_id):
            await self._safe_page()
            # Playwright temporarily changes caret styles while taking a screenshot.
            # Bind elements after those styles are restored, so our own capture cannot
            # make the next observed-element action appear stale.
            content = await self.page.screenshot(full_page=False, animations="disabled")
            await self._safe_page()
            self.observation_id = new_id("page")
            self.observed_url = self.page.url
            self.elements = {}
            descriptions = []
            handles = await self.page.locator(
                'a, button, input:not([type="hidden"]):not([type="password"]), '
                'textarea, select, [role="button"], [role="link"]',
            ).element_handles()
            for handle in handles[:150]:
                if not await handle.is_visible():
                    continue
                if len(descriptions) >= 60:
                    break
                info = await handle.evaluate("""el => ({
                    tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || '',
                    label: (el.getAttribute('aria-label') || el.innerText ||
                            el.getAttribute('placeholder') ||
                            el.getAttribute('name') || '').slice(0, 160),
                    markup: el.outerHTML
                })""")
                if info["type"] in {"file", "password"}:
                    continue
                identifier = f"element_{len(descriptions)}"
                self.elements[identifier] = (
                    handle,
                    hashlib.sha256(info.pop("markup").encode()).hexdigest(),
                )
                descriptions.append({"id": identifier, **info})
            text = await self.page.locator("body").evaluate("el => el.innerText.slice(0, 12000)")
            visible = sanitize_untrusted_payload(
                {
                    "title": (await self.page.title())[:500],
                    "excerpt": text,
                    "elements": descriptions,
                },
                UntrustedSource.MARKETPLACE_PAGE,
            ).value
            name = f"{self.observation_id}.png"
            if len(content) > MAX_SHOP_FILE_BYTES:
                raise ShopBrowserError(
                    "SHOP_CAPTURE_TOO_LARGE", "The page capture exceeds the size limit."
                )
            path = self.staging / name
            self.capture_paths.add(path)
            path.write_bytes(content)
            return {
                "ok": True,
                "observation_id": self.observation_id,
                "session_id": self.session_id,
                "source_url": source_url(self.page.url),
                "retrieved_at": utc_now().isoformat(),
                **visible,
                "files": [
                    {
                        "name": name,
                        "kind": "screenshot",
                        "mime": "image/png",
                        "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                ],
            }

    async def execute(self, operation: ShopBrowserOperation) -> dict:
        with self.store.ledger.lifecycle.lease(self.project_id):
            self.capture_paths = set()
            self.prepare_staging()
            try:
                return await self._execute(operation)
            except BaseException:
                self.discard_capture()
                raise

    async def _execute(self, operation: ShopBrowserOperation) -> dict:
        if operation.operation == "navigate":
            if await _check_browser_target(operation.url, self.policy):
                raise ShopBrowserError(
                    "SHOP_PAGE_OUT_OF_SCOPE", "Navigation is outside this shop's domains."
                )
            self.blocked.clear()
            await self.page.goto(operation.url, wait_until="domcontentloaded")
        elif operation.operation not in {"snapshot", "close"}:
            await self._safe_page()
            if (
                operation.observation_id != self.observation_id
                or self.page.url != self.observed_url
            ):
                raise ShopBrowserError(
                    "SHOP_PAGE_CHANGED", "The page changed. Take a new snapshot."
                )
            selected = self.elements.get(operation.target)
            if selected is None:
                raise ShopBrowserError(
                    "SHOP_TARGET_CHANGED", "Take a new snapshot to select an element."
                )
            handle, markup = selected
            current = await handle.evaluate("el => el.outerHTML")
            if (
                hashlib.sha256(current.encode()).hexdigest() != markup
                or not await handle.is_visible()
            ):
                raise ShopBrowserError(
                    "SHOP_TARGET_CHANGED", "The element changed. Take a new snapshot."
                )
            self.observation_id = None
            if operation.operation == "download":
                self.expecting_download = True
                try:
                    async with self.page.expect_download(timeout=20000) as event:
                        await handle.click()
                    download = await event.value
                finally:
                    self.expecting_download = False
                try:
                    if download.url.startswith(
                        ("http://", "https://")
                    ) and await _check_browser_target(
                        download.url,
                        self.policy,
                    ):
                        raise ShopBrowserError(
                            "SHOP_DOWNLOAD_REJECTED", "Report source is outside shop scope."
                        )
                    original = Path(download.suggested_filename.replace("\\", "/")).name[:160]
                    original = sanitize_untrusted_payload(
                        {"filename": original},
                        UntrustedSource.MARKETPLACE_PAGE,
                    ).value["filename"]
                    suffix = Path(original).suffix.lower()
                    if suffix not in {".csv", ".tsv", ".xlsx", ".xls", ".pdf", ".json", ".zip"}:
                        raise ShopBrowserError(
                            "SHOP_REPORT_FORMAT", "This download is not a supported report format."
                        )
                    name = f"{new_id('report')}{suffix}"
                    self.capture_paths.add(self.staging / name)
                    task = asyncio.create_task(download.save_as(self.staging / name))
                    try:
                        while not task.done():
                            # HTTP responses are already bounded by the pinned transport.
                            # Also stop large page-generated blob downloads while writing.
                            sizes = [
                                item.stat().st_size
                                for item in self.staging.iterdir()
                                if item.is_file()
                            ]
                            if (
                                any(size > MAX_SHOP_FILE_BYTES for size in sizes)
                                or sum(sizes) > 4 * MAX_SHOP_FILE_BYTES
                            ):
                                raise ShopBrowserError(
                                    "SHOP_REPORT_TOO_LARGE", "The report exceeds 20 MiB."
                                )
                            await asyncio.sleep(0.05)
                        await task
                    finally:
                        if not task.done():
                            await download.cancel()
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                    content = (self.staging / name).read_bytes()
                    if not content or len(content) > MAX_SHOP_FILE_BYTES:
                        raise ShopBrowserError(
                            "SHOP_REPORT_TOO_LARGE", "The report is empty or exceeds 20 MiB."
                        )
                    result = await self.snapshot()
                    result["files"].append(
                        {
                            "name": name,
                            "kind": "report",
                            "original_name": original,
                            "size": len(content),
                            "sha256": hashlib.sha256(content).hexdigest(),
                        }
                    )
                    return result
                finally:
                    await download.cancel()
                    await download.delete()
            if operation.operation == "click":
                await handle.click()
            elif operation.operation == "type":
                await handle.fill(operation.value or "")
            elif operation.operation == "submit":
                await handle.press("Enter")
        return await self.snapshot()


async def serve(
    root: Path,
    shop_id: str,
    *,
    headless: bool = False,
    raise_errors: bool = False,
    install_signals: bool = True,
):
    from playwright.async_api import async_playwright

    ledger = FileLedger(root)
    store = ShopBrowserStore(ledger)
    access = ProjectAccessStore(root / "auth" / "projects")
    shop = store.get(shop_id)
    session = {
        "id": new_id("shop_session"),
        "shop_id": shop_id,
        "state": "starting",
        "token": secrets.token_urlsafe(32),
        "started_at": utc_now().isoformat(),
        "profile_generation": shop.profile_generation,
    }
    with (
        data_directory_lease(root, exclusive=False),
        ledger.lifecycle.lease(shop.project_id),
        session_lease(store, shop_id),
    ):
        # A queued process must not reopen after a login deletion or identity change.
        current = store.get(shop_id)
        if current.revision != shop.revision:
            raise ShopBrowserError("SHOP_CONFLICT", "Shop access changed. Refresh and try again.")
        shop = current
        path = store.directory(shop_id) / "session.json"
        socket = store.socket_path(shop_id)
        ledger._atomic_write(path, session)
        policy = ShopBrowserPolicy(shop, OrgPolicyStore(root / "org" / "policy.json"))
        driver = ShopBrowserPage(store, shop_id, session["id"], policy)
        closed = asyncio.Event()
        guard = None
        try:
            async with async_playwright() as playwright:
                profile = store.directory(shop_id) / f"profile-{shop.profile_generation}"
                profile.mkdir(mode=0o700, exist_ok=True)
                if profile.is_symlink():
                    raise ValueError("Invalid shop browser profile directory")
                context = await playwright.chromium.launch_persistent_context(
                    profile,
                    headless=headless,
                    accept_downloads=True,
                    downloads_path=driver.staging,
                    service_workers="block",
                    chromium_sandbox=True,
                    viewport={"width": 1360, "height": 900},
                )
                context.on("close", closed.set)
                page = context.pages[0] if context.pages else await context.new_page()
                driver.page = page
                page.on("download", driver.download_started)
                page.set_default_timeout(15000)
                await context.add_init_script(_BLOCK_SERVICE_WORKER_REGISTRATION)
                await _install_request_guard(
                    context,
                    policy=policy,
                    baseline_pages=set(),
                    blocked=driver.blocked,
                    guarded_pages={page},
                )
                # A popup cannot acquire an unguarded network connection or another shop's session.
                context.on(
                    "page",
                    lambda other: asyncio.create_task(other.close()) if other != page else None,
                )
                guard = await _install_cdp_request_guard(
                    context, page, policy=policy, blocked=driver.blocked
                )
                lock = asyncio.Lock()

                async def handle(reader, writer):
                    try:
                        with ledger.lifecycle.lease(shop.project_id):
                            try:
                                async with asyncio.timeout(40):
                                    raw = await reader.readline()
                                    if len(raw) > MAX_SHOP_RPC_BYTES:
                                        raise ValueError("oversized command")
                                    value = json.loads(raw)
                                    if not hmac.compare_digest(
                                        str(value.pop("token", "")), session["token"]
                                    ):
                                        raise ValueError("invalid session credential")
                                    if value.pop("session_id", None) != session["id"]:
                                        raise ValueError("session changed")
                                    caller = ShopBrowserCaller.model_validate(value.pop("caller"))
                                    operation = ShopBrowserOperation.model_validate(value)
                                    async with lock:

                                        def authorize():
                                            authorize_caller(
                                                store,
                                                access,
                                                shop_id,
                                                session["id"],
                                                caller,
                                                operation,
                                            )

                                        authorize()
                                        if operation.operation == "close":
                                            result = {"ok": True}
                                            closed.set()
                                        else:
                                            result = await execute_authorized(
                                                driver,
                                                operation,
                                                authorize,
                                                reader,
                                            )
                            except AuthorizationError:
                                result = {
                                    "ok": False,
                                    "error_code": "SHOP_ACCESS_CHANGED",
                                    "error": "Shop, project or Run authorization changed.",
                                }
                            except ShopBrowserError as exc:
                                result = {"ok": False, "error_code": exc.code, "error": str(exc)}
                            except Exception:
                                result = {
                                    "ok": False,
                                    "error_code": "SHOP_BROWSER_FAILED",
                                    "error": "Browser operation failed. "
                                    "Inspect the shop window and retry.",
                                }
                            writer.write(json.dumps(result, ensure_ascii=False).encode() + b"\n")
                            await writer.drain()
                    finally:
                        writer.close()
                        await writer.wait_closed()

                socket.unlink(missing_ok=True)
                server = await asyncio.start_unix_server(
                    handle, path=socket, limit=MAX_SHOP_RPC_BYTES
                )
                os.chmod(socket, 0o600)
                async with server:
                    try:
                        await page.goto(shop.entry_url, wait_until="domcontentloaded")
                    except Exception:
                        pass  # Keep the visible window for the human to inspect/recover.
                    session["state"] = "open"
                    ledger._atomic_write(path, session)
                    if install_signals:
                        for signum in (signal.SIGTERM, signal.SIGINT):
                            asyncio.get_running_loop().add_signal_handler(signum, closed.set)
                    await closed.wait()
                await _remove_cdp_request_guard(*guard)
                guard = None
                await context.close()
                await asyncio.gather(*driver.download_tasks, return_exceptions=True)
                session["state"] = "closed"
        except Exception as exc:
            session["state"] = "failed"
            session["failure_type"] = type(exc).__name__
            if raise_errors:
                raise
        finally:
            socket.unlink(missing_ok=True)
            ledger._atomic_write(path, session)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--shop-id", required=True)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    asyncio.run(serve(args.root, args.shop_id, headless=args.headless))

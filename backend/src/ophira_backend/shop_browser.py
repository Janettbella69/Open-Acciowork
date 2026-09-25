"""Human shop/session controls and run-bound browser data collection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from .auth import AuthorizationError
from .browser_tools import _check_browser_target, _record_capture
from .constants import TOOL_ACT_ON_PAGE, TOOL_SNAPSHOT_PAGE
from .marketplace_access import MarketplaceAccessService
from .models import PrincipalContext, new_id, utc_now
from .policy import OrgPolicyStore
from .shop_browser_host import session_lease
from .shop_browser_login_archive import build_login_archive
from .shop_browser_store import (
    MAX_SHOP_FILE_BYTES,
    MAX_SHOP_RPC_BYTES,
    MAX_SHOPS,
    ShopBrowserCaller,
    ShopBrowserError,
    ShopBrowserOperation,
    ShopBrowserStore,
    ShopGrantInput,
    ShopIdentity,
    ShopIdentityInput,
    ShopUpdateInput,
)
from .untrusted import UntrustedSource, sanitize_untrusted_payload


class ShopBrowserService:
    def __init__(
        self,
        authority: MarketplaceAccessService,
        policy: OrgPolicyStore,
        *,
        headless: bool = False,
    ):
        # The seller API and browser path share the same project/Run/principal boundary.
        self.authority = authority
        self.ledger = authority.ledger
        self.store = ShopBrowserStore(self.ledger)
        self.policy = policy
        self.headless = headless
        self.children: dict[str, subprocess.Popen] = {}

    def _shop(self, project_id: str, shop_id: str) -> ShopIdentity:
        project = self.authority._project(project_id)
        shop = self.store.get(shop_id)
        if (
            shop.project_id != project_id
            or shop.tenant_id != project.tenant_id
            or shop.identity_binding != project.identity_binding
        ):
            raise AuthorizationError("Shop is outside this project")
        return shop

    def _revision(self, project_id: str, shop_id: str, expected: int):
        shop = self._shop(project_id, shop_id)
        if shop.revision != expected:
            raise ShopBrowserError("SHOP_CONFLICT", "Shop access changed. Refresh and try again.")
        return shop

    def _live(self, shop_id: str) -> dict | None:
        session = self.store.session(shop_id)
        if session is None or session["state"] not in {"starting", "open"}:
            return None
        try:
            with session_lease(self.store, shop_id):
                return None
        except BlockingIOError:
            return session

    def view(self, project_id: str, shop_id: str) -> dict:
        with self.ledger.lifecycle.lease(project_id):
            shop = self._shop(project_id, shop_id)
            session = self._live(shop_id)
            project = self.ledger.get_project(project_id)
            evidence = [item for item in project.evidence if f"shop:{shop_id}" in item.tags]
            return {
                **shop.model_dump(mode="json"),
                "browser_state": session["state"] if session else "closed",
                "session_id": session["id"] if session else None,
                "agent_access": bool(
                    session
                    and session["state"] == "open"
                    and shop.authorized(session["id"])
                    and self.policy.get().browser_enabled
                ),
                "capture_count": len(evidence),
                "browser_profile_present": self.store.has_profile_files(shop_id),
                "browser_login_data_present": self.store.has_login_files(shop_id),
            }

    def list(self, project_id: str) -> list[dict]:
        with self.ledger.lifecycle.lease(project_id):
            self.authority._project(project_id)
            return [self.view(project_id, shop.id) for shop in self.store.list(project_id)]

    async def create(
        self, project_id: str, payload: ShopIdentityInput, principal: PrincipalContext | None
    ):
        with self.ledger.lifecycle.lease(project_id):
            actor = self.authority.authorize_admin(project_id, principal)
            if await _check_browser_target(payload.entry_url, self.policy.get()):
                raise ShopBrowserError(
                    "SHOP_URL_REJECTED", "The shop entry is not allowed by browser policy.", 400
                )
            if payload.connector_id:
                connector = self.authority.connectors.get(payload.connector_id)
                if connector.type != payload.platform:
                    raise ShopBrowserError(
                        "SHOP_CONNECTOR_MISMATCH", "Choose a connector for this platform.", 400
                    )
            with (
                self.authority._identity_transaction(project_id),
                self.ledger._lock_for(f"shops:{project_id}"),
            ):
                self.authority.authorize_admin(project_id, principal)
                project = self.authority._project(project_id)
                if len(self.store.list(project_id)) >= MAX_SHOPS:
                    raise ShopBrowserError(
                        "SHOP_LIMIT", "This project already has 100 shop identities."
                    )
                shop = ShopIdentity(
                    **payload.model_dump(),
                    project_id=project_id,
                    tenant_id=project.tenant_id,
                    identity_binding=project.identity_binding,
                    created_by=actor,
                )
                self.store.write(shop)
            return self.view(project_id, shop.id)

    async def update(self, project_id: str, shop_id: str, payload: ShopUpdateInput, principal):
        with self.ledger.lifecycle.lease(project_id):
            self.authority.authorize_admin(project_id, principal)
            if await _check_browser_target(payload.entry_url, self.policy.get()):
                raise ShopBrowserError(
                    "SHOP_URL_REJECTED", "Shop entry is outside browser policy.", 400
                )
            if payload.connector_id:
                connector = self.authority.connectors.get(payload.connector_id)
                if connector.type != payload.platform:
                    raise ShopBrowserError(
                        "SHOP_CONNECTOR_MISMATCH", "Choose this platform's connector.", 400
                    )
            with (
                self.authority._identity_transaction(project_id),
                self.ledger._lock_for(f"shop:{shop_id}"),
            ):
                self.authority.authorize_admin(project_id, principal)
                shop = self._revision(project_id, shop_id, payload.expected_revision)
                if self._live(shop_id):
                    raise ShopBrowserError(
                        "SHOP_BROWSER_OPEN", "Close the shop browser before editing its identity."
                    )
                values = payload.model_dump(exclude={"expected_revision"})
                identity_changed = (
                    any(
                        values[key] != getattr(shop, key)
                        for key in (
                            "platform",
                            "seller_shop_id",
                            "connector_id",
                        )
                    )
                    or payload.entry_url.split("/")[2] != shop.entry_url.split("/")[2]
                )
                self.store.write(
                    shop.model_copy(
                        update={
                            **values,
                            "revision": shop.revision + 1,
                            "profile_generation": shop.profile_generation + int(identity_changed),
                            "authorized_session_id": None,
                            "grant_expires_at": None,
                            "revoked_at": utc_now(),
                            "revoked_by": principal.principal_id if principal else "local_owner",
                        }
                    )
                )
            return self.view(project_id, shop_id)

    async def open(self, project_id: str, shop_id: str, revision: int, principal):
        with self.ledger.lifecycle.lease(project_id):
            self.authority.authorize_admin(project_id, principal)
            self._revision(project_id, shop_id, revision)
            if not self.policy.get().browser_enabled:
                raise ShopBrowserError(
                    "SHOP_BROWSER_DISABLED", "Browser access is disabled by organization policy."
                )
            if self._live(shop_id):
                return self.view(project_id, shop_id)
            # Launch serialization is separate from the daemon's lifetime lease.
            from .shop_browser_host import launch_lease

            try:
                with launch_lease(self.store, shop_id):
                    if self._live(shop_id):
                        return self.view(project_id, shop_id)
                    command = [
                        sys.executable,
                        "-m",
                        "ophira_backend.shop_browser_host",
                        "--root",
                        str(self.ledger.root),
                        "--shop-id",
                        shop_id,
                    ]
                    if self.headless:
                        command.append("--headless")
                    env = {
                        key: os.environ[key]
                        for key in (
                            "PATH",
                            "HOME",
                            "TMPDIR",
                            "LANG",
                            "LC_ALL",
                            "DISPLAY",
                            "XAUTHORITY",
                            "XDG_RUNTIME_DIR",
                            "SYSTEMROOT",
                        )
                        if key in os.environ
                    }
                    env["PYTHONPATH"] = str(Path(__file__).parents[1])
                    child = await asyncio.to_thread(
                        subprocess.Popen,
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        env=env,
                        start_new_session=True,
                    )
                    previous = self.children.get(shop_id)
                    if previous is not None:
                        previous.poll()
                    self.children[shop_id] = child
                    try:
                        async with asyncio.timeout(30):
                            while True:
                                self.authority.authorize_admin(project_id, principal)
                                self._revision(project_id, shop_id, revision)
                                session = self._live(shop_id)
                                if session and session["state"] == "open":
                                    return self.view(project_id, shop_id)
                                if child.poll() is not None:
                                    raise ShopBrowserError(
                                        "SHOP_BROWSER_START_FAILED",
                                        "Browser could not open. "
                                        "Install Playwright Chromium and use a computer "
                                        "with a display.",
                                    )
                                await asyncio.sleep(0.1)
                    except BaseException:
                        if child.poll() is None:
                            child.terminate()
                        raise
            except BlockingIOError:
                raise ShopBrowserError(
                    "SHOP_BROWSER_STARTING", "This shop browser is already opening."
                ) from None
            except TimeoutError:
                raise ShopBrowserError(
                    "SHOP_BROWSER_START_FAILED", "Opening the shop browser timed out."
                ) from None

    async def _rpc(
        self,
        shop_id: str,
        session: dict,
        operation: ShopBrowserOperation,
        *,
        caller: ShopBrowserCaller | None = None,
        principal: PrincipalContext | None = None,
    ) -> dict:
        if caller is None:
            shop = self.store.get(shop_id)
            caller = ShopBrowserCaller(
                project_id=shop.project_id,
                shop_revision=shop.revision,
                principal=principal,
            )
        try:
            async with asyncio.timeout(40):
                reader, writer = await asyncio.open_unix_connection(
                    self.store.socket_path(shop_id),
                    limit=MAX_SHOP_RPC_BYTES,
                )
                try:
                    payload = {
                        **operation.model_dump(exclude_none=True),
                        "token": session["token"],
                        "session_id": session["id"],
                        "caller": caller.model_dump(mode="json"),
                    }
                    writer.write(json.dumps(payload).encode() + b"\n")
                    await writer.drain()
                    raw = await reader.readline()
                    if len(raw) > MAX_SHOP_RPC_BYTES:
                        raise ValueError("oversized browser reply")
                    result = json.loads(raw)
                finally:
                    writer.close()
                    await writer.wait_closed()
        except (OSError, ValueError, TimeoutError):
            raise ShopBrowserError(
                "SHOP_BROWSER_UNAVAILABLE", "Shop browser is unavailable. Reopen its window."
            ) from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            code = result.get("error_code") if isinstance(result, dict) else None
            if code not in {
                "SHOP_LOGIN_REQUIRED",
                "SHOP_PAGE_CHANGED",
                "SHOP_TARGET_CHANGED",
                "SHOP_RESOURCE_BLOCKED",
                "SHOP_PAGE_OUT_OF_SCOPE",
                "SHOP_REPORT_FORMAT",
                "SHOP_REPORT_TOO_LARGE",
                "SHOP_DOWNLOAD_REJECTED",
                "SHOP_BROWSER_FAILED",
                "SHOP_ACCESS_CHANGED",
            }:
                code = "SHOP_BROWSER_FAILED"
            raise ShopBrowserError(
                code,
                f"{code}: Inspect the shop window, permitted domains and sign-in state, "
                "then retry.",
            )
        return result

    async def close(self, project_id: str, shop_id: str, revision: int, principal):
        with self.ledger.lifecycle.lease(project_id):
            self.authority.authorize_admin(project_id, principal)
            self._revision(project_id, shop_id, revision)
            session = self._live(shop_id)
            if session:
                await self._rpc(
                    shop_id,
                    session,
                    ShopBrowserOperation(operation="close"),
                    principal=principal,
                )
                try:
                    async with asyncio.timeout(15):
                        while self._live(shop_id):
                            self.authority.authorize_admin(project_id, principal)
                            await asyncio.sleep(0.1)
                except TimeoutError:
                    raise ShopBrowserError(
                        "SHOP_BROWSER_CLOSING", "Browser is closing. Refresh its status."
                    ) from None
            return self.view(project_id, shop_id)

    async def grant(self, project_id: str, shop_id: str, payload: ShopGrantInput, principal):
        with self.ledger.lifecycle.lease(project_id):
            actor = self.authority.authorize_admin(project_id, principal)
            self._revision(project_id, shop_id, payload.expected_revision)
            session = self._live(shop_id)
            if session is None or session["id"] != payload.session_id or session["state"] != "open":
                raise ShopBrowserError(
                    "SHOP_SESSION_CHANGED",
                    "Open this shop browser and confirm its current identity."
                )
            snapshot = await self._rpc(
                shop_id,
                session,
                ShopBrowserOperation(operation="snapshot"),
                principal=principal,
            )
            self._discard_files(shop_id, snapshot)
            with (
                self.authority._identity_transaction(project_id),
                self.ledger._lock_for(f"shop:{shop_id}"),
            ):
                self.authority.authorize_admin(project_id, principal)
                shop = self._revision(project_id, shop_id, payload.expected_revision)
                if self._live(shop_id) != session:
                    raise ShopBrowserError("SHOP_SESSION_CHANGED", "The browser session changed.")
                now = utc_now()
                self.store.write(
                    shop.model_copy(
                        update={
                            "revision": shop.revision + 1,
                            "authorized_session_id": session["id"],
                            "grant_expires_at": now + timedelta(seconds=payload.expires_in_seconds),
                            "granted_at": now,
                            "granted_by": actor,
                            "revoked_at": None,
                            "revoked_by": None,
                        }
                    )
                )
            return self.view(project_id, shop_id)

    def revoke(self, project_id: str, shop_id: str, revision: int, principal):
        with self.ledger.lifecycle.lease(project_id):
            with (
                self.authority._identity_transaction(project_id),
                self.ledger._lock_for(f"shop:{shop_id}"),
            ):
                actor = self.authority.authorize_admin(project_id, principal)
                shop = self._revision(project_id, shop_id, revision)
                self.store.write(
                    shop.model_copy(
                        update={
                            "revision": shop.revision + 1,
                            "revoked_at": utc_now(),
                            "revoked_by": actor,
                        }
                    )
                )
            return self.view(project_id, shop_id)

    def delete_login(self, project_id: str, shop_id: str, revision: int, principal):
        """Remove every saved browser profile while retaining the shop and evidence."""
        from .shop_browser_host import launch_lease

        with self.ledger.lifecycle.lease(project_id):
            try:
                with (
                    self.authority._identity_transaction(project_id),
                    self.ledger._lock_for(f"shop:{shop_id}"),
                ):
                    actor = self.authority.authorize_admin(project_id, principal)
                    shop = self._revision(project_id, shop_id, revision)
                    if not shutil.rmtree.avoids_symlink_attacks:
                        raise ShopBrowserError(
                            "SHOP_PROFILE_DELETE_UNSAFE",
                            "This host cannot safely remove browser profiles.",
                        )
                    with launch_lease(self.store, shop_id), session_lease(self.store, shop_id):
                        profiles = self.store.profile_paths(shop_id)
                        directory = self.store.directory(shop_id)
                        session = directory / "session.json"
                        if session.exists() and session.is_dir():
                            raise ShopBrowserError(
                                "SHOP_SESSION_INVALID", "Browser session failed validation."
                            )
                        # Publish the revoked generation first. An interrupted
                        # deletion cannot make old login files usable again.
                        self.store.write(shop.model_copy(update={
                            "revision": shop.revision + 1,
                            "profile_generation": shop.profile_generation + 1,
                            "authorized_session_id": None,
                            "grant_expires_at": None,
                            "revoked_at": utc_now(),
                            "revoked_by": actor,
                        }))
                        for profile in profiles:
                            shutil.rmtree(profile)
                        session.unlink(missing_ok=True)
                        descriptor = os.open(
                            directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        )
                        try:
                            os.fsync(descriptor)
                        finally:
                            os.close(descriptor)
            except BlockingIOError:
                raise ShopBrowserError(
                    "SHOP_BROWSER_OPEN", "Close the shop browser before deleting its login."
                ) from None
            except OSError:
                raise ShopBrowserError(
                    "SHOP_LOGIN_DELETE_FAILED", "Browser login deletion did not finish. Retry."
                ) from None
            return self.view(project_id, shop_id)

    def export_login(self, project_id: str, shop_id: str, revision: int, principal):
        """Capture closed browser profiles without copying live or linked files."""
        from .shop_browser_host import launch_lease

        with self.ledger.lifecycle.lease(project_id):
            try:
                with (
                    self.authority._identity_transaction(project_id),
                    self.ledger._lock_for(f"shop:{shop_id}"),
                ):
                    self.authority.authorize_admin(project_id, principal)
                    shop = self._revision(project_id, shop_id, revision)
                    with launch_lease(self.store, shop_id), session_lease(self.store, shop_id):
                        directory = self.store.directory(shop_id)
                        archive = build_login_archive(
                            directory, self.store.profile_paths(shop_id),
                            project_id=project_id, shop_id=shop_id, revision=shop.revision,
                        )
                    try:
                        self.authority.authorize_admin(project_id, principal)
                    except BaseException:
                        archive.close()
                        raise
                    return archive
            except BlockingIOError:
                raise ShopBrowserError(
                    "SHOP_BROWSER_OPEN", "Close the shop browser before exporting its login."
                ) from None
            except (OSError, ValueError):
                raise ShopBrowserError(
                    "SHOP_PROFILE_EXPORT_FAILED", "Browser profile export could not be verified."
                ) from None

    def _staged_path(self, shop_id: str, name: str) -> Path:
        if not re.fullmatch(r"(?:page|report)_[a-f0-9]{32}\.[a-z]{2,5}", name):
            raise ShopBrowserError("SHOP_CAPTURE_INVALID", "Invalid browser capture.")
        folder = self.store.directory(shop_id) / "staging"
        if folder.is_symlink():
            raise ShopBrowserError("SHOP_CAPTURE_INVALID", "Invalid browser capture.")
        path = folder / name
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > MAX_SHOP_FILE_BYTES
        ):
            raise ShopBrowserError("SHOP_CAPTURE_INVALID", "Invalid browser capture.")
        return path

    def _discard_files(self, shop_id: str, result: dict):
        for item in result.get("files", []):
            try:
                self._staged_path(shop_id, item["name"]).unlink()
            except (OSError, ValueError, KeyError, ShopBrowserError):
                pass

    async def capture(
        self, project_id: str, run_id: str, shop_id: str, operation: ShopBrowserOperation
    ):
        with self.ledger.lifecycle.lease(project_id):
            if operation.operation == "close":
                raise ShopBrowserError(
                    "SHOP_ADMIN_REQUIRED", "Use the administrator's browser controls."
                )
            initial_run = self.authority._run(project_id, run_id)
            shop = self._shop(project_id, shop_id)
            session = self._live(shop_id)
            if session is None or not shop.authorized(session["id"]):
                raise ShopBrowserError(
                    "SHOP_ACCESS_REQUIRED",
                    "Ask the project administrator to authorize this shop session.",
                )

            def authorized():
                run = self.authority._run(project_id, run_id)
                current = self._shop(project_id, shop_id)
                if (
                    run.sdk_session_id != initial_run.sdk_session_id
                    or current != shop
                    or not current.authorized(session["id"])
                    or self._live(shop_id) != session
                    or not self.policy.get().browser_enabled
                ):
                    raise AuthorizationError("Shop, session, Run or browser permission changed")

            authorized()
            result = await self._rpc(
                shop_id,
                session,
                operation,
                caller=ShopBrowserCaller(
                    project_id=project_id,
                    shop_revision=shop.revision,
                    run_id=run_id,
                    sdk_session_id=initial_run.sdk_session_id,
                ),
            )
            try:
                authorized()
                return self._record(project_id, run_id, shop, result, operation, authorized)
            finally:
                self._discard_files(shop_id, result)

    def _record(self, project_id, run_id, shop, result, operation, authorized):
        with self.ledger.lifecycle.lease(project_id):
            files = result.get("files")
            if not isinstance(files, list) or not 1 <= len(files) <= 2:
                raise ShopBrowserError(
                    "SHOP_CAPTURE_INVALID", "Browser did not return a complete capture."
                )
            copies = []
            for item in files:
                content = self._staged_path(shop.id, item["name"]).read_bytes()
                if (
                    len(content) != item["size"]
                    or hashlib.sha256(content).hexdigest() != item["sha256"]
                ):
                    raise ShopBrowserError(
                        "SHOP_CAPTURE_INVALID", "Browser capture failed integrity verification."
                    )
                copies.append((item, content))
            folder = (
                self.ledger.workspace_for_project(project_id) / "shop-captures" / new_id("capture")
            )
            if folder.parent.is_symlink():
                raise ShopBrowserError("SHOP_CAPTURE_INVALID", "Invalid project capture directory.")
            visible = sanitize_untrusted_payload(
                {key: value for key, value in result.items() if key != "files"},
                UntrustedSource.MARKETPLACE_PAGE,
            ).value
            with (
                self.authority._identity_transaction(project_id),
                self.ledger._lock_for(f"shop:{shop.id}"),
                self.ledger._lock_for(run_id),
            ):
                authorized()
                folder.mkdir(parents=True, mode=0o700)
                for item, content in copies:
                    (folder / item["name"]).write_bytes(content)
                screenshot = next(item for item, _ in copies if item["kind"] == "screenshot")
                relative = str(
                    (folder / screenshot["name"]).relative_to(
                        self.ledger.workspace_for_project(project_id)
                    )
                )
                capture = {
                    **visible,
                    "shop_id": shop.id,
                    **sanitize_untrusted_payload(
                        {"shop_name": shop.name, "seller_shop_id": shop.seller_shop_id},
                        UntrustedSource.MARKETPLACE_PAGE,
                    ).value,
                    "platform": shop.platform,
                    "connector_id": shop.connector_id,
                    "browser_level": "shop_session",
                    "identity_source": "human_confirmed",
                    "final_url": visible["source_url"],
                    "files": [
                        {
                            **item,
                            "path": str(
                                (folder / item["name"]).relative_to(
                                    self.ledger.workspace_for_project(project_id)
                                )
                            ),
                        }
                        for item, _ in copies
                    ],
                    "screenshot_path": relative,
                    "action": None
                    if operation.operation in {"snapshot", "navigate"}
                    else operation.operation,
                }
                captured = _record_capture(self.ledger, project_id, run_id, capture)
                evidence_id = captured["evidence_id"]

                def describe_evidence(project):
                    evidence = next(item for item in project.evidence if item.id == evidence_id)
                    evidence.tags.extend([f"shop:{shop.id}", f"platform:{shop.platform}"])
                    evidence.source_title = f"{capture['shop_name']} · {visible['title']}"[:1000]

                self.ledger.update_project(project_id, describe_evidence)
                artifacts = []
                for item, _ in copies:
                    artifact = self.ledger.register_artifact(
                        project_id,
                        str(
                            (folder / item["name"]).relative_to(
                                self.ledger.workspace_for_project(project_id)
                            )
                        ),
                        run_id=run_id,
                        source_ids=[evidence_id],
                        mime=item.get("mime"),
                    )
                    if item["kind"] == "report" and item.get("original_name"):
                        artifact.name = item["original_name"]

                        def name_report(project, registered=artifact):
                            next(
                                row for row in project.artifacts if row.id == registered.id
                            ).name = registered.name

                        self.ledger.update_project(project_id, name_report)
                    artifacts.append(artifact.model_dump(mode="json"))
                    if artifact.checksum != item["sha256"] or artifact.size_bytes != item["size"]:
                        raise ShopBrowserError(
                            "SHOP_CAPTURE_INVALID",
                            "Registered report failed integrity verification."
                        )
                text_path = folder / "page.md"
                text_path.write_text(
                    f"# {visible['title']}\n\n{visible['source_url']}\n\n"
                    f"{visible['retrieved_at']}\n\n{visible['excerpt']}\n",
                    "utf-8",
                )
                text_artifact = self.ledger.register_artifact(
                    project_id,
                    str(text_path.relative_to(self.ledger.workspace_for_project(project_id))),
                    run_id=run_id,
                    source_ids=[evidence_id],
                    mime="text/markdown",
                )
                artifacts.append(text_artifact.model_dump(mode="json"))
                return {
                    **captured,
                    "artifacts": artifacts,
                    "tool": TOOL_ACT_ON_PAGE if capture["action"] else TOOL_SNAPSHOT_PAGE,
                }

    def data(self, project_id: str, shop_id: str) -> list[dict]:
        with self.ledger.lifecycle.lease(project_id):
            self._shop(project_id, shop_id)
            project = self.ledger.get_project(project_id)
            return [
                {
                    "evidence": item.model_dump(mode="json"),
                    "artifacts": [
                        artifact.model_dump(mode="json")
                        for artifact in project.artifacts
                        if item.id in artifact.source_ids
                    ],
                }
                for item in reversed(project.evidence)
                if f"shop:{shop_id}" in item.tags
            ][:50]

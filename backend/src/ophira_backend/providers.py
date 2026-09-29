from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .config import Settings
from .constants import ProviderErrorCode
from .models import (
    ProviderPreflightResult,
    ProviderProfile,
    ProviderProfileUpsert,
    utc_now,
)
from .skill_proposal_files import directory_fd, read_at

MODEL_SECRET_NAMES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
PROVIDER_VAULT_FILENAME = "provider-secrets.json"
CONNECTOR_VAULT_FILENAME = "connector-secrets.json"
MCP_VAULT_FILENAME = "mcp-secrets.json"
WEIXIN_VAULT_FILENAME = "weixin-secrets.json"
CREDENTIAL_VAULT_FILENAMES = (
    PROVIDER_VAULT_FILENAME, CONNECTOR_VAULT_FILENAME, MCP_VAULT_FILENAME,
    WEIXIN_VAULT_FILENAME,
)


def parse_credential_deletion_register(data: object) -> tuple[int, str]:
    if (
        not isinstance(data, dict)
        or set(data) != {"schema_version", "count", "sha256"}
        or type(data["schema_version"]) is not int
        or data["schema_version"] != 1
        or type(data["count"]) is not int
        or data["count"] < 1
        or not isinstance(data["sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", data["sha256"]) is None
    ):
        raise ValueError("invalid private vault deletion register")
    return data["count"], data["sha256"]


class ProviderNotFoundError(KeyError):
    pass


class ProviderConfigurationError(RuntimeError):
    def __init__(self, message: str, code: ProviderErrorCode) -> None:
        super().__init__(message)
        self.code = code


def provider_readiness_failures(profile: ProviderProfile) -> list[str]:
    """The same current registry gate applies to creation and native continuation."""
    failures: list[str] = []
    if not profile.enabled:
        failures.append("disabled")
    if not profile.production_eligible:
        failures.append("not production eligible")
    if not profile.secret_configured:
        failures.append("credential missing")
    if profile.last_verified_at is None:
        failures.append("preflight not completed")
    if profile.last_error_code is not None:
        failures.append(f"preflight failed ({profile.last_error_code})")
    return failures


class FileSecretVault:
    """Local key-vault port. The file is never served and is mode 0600."""

    def __init__(self, root: Path, filename: str = PROVIDER_VAULT_FILENAME) -> None:
        if re.fullmatch(r"[a-z][a-z0-9-]*\.json", filename) is None:
            raise ValueError("invalid private vault filename")
        # Resolve the application parent (including macOS /var aliases), never
        # the secrets directory itself: a linked vault must fail before chmod.
        self.root = root.parent.resolve() / root.name
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with directory_fd(self.root):
            pass
        self.path = self.root / filename
        self.lock_path = self.root / (filename + ".lock")
        self.deletion_register_path = self.root / (filename + ".deletion-register.json")
        self.deletion_pending_path = self.root / (filename + ".deletion-pending.json")
        self._lock = threading.RLock()
        if self.path.exists():
            self._secure()
        if self.deletion_pending_path.exists() or any(
            path.with_name(path.name + ".tmp").exists()
            for path in (self.path, self.deletion_register_path, self.deletion_pending_path)
        ) or self.path.with_suffix(".tmp").exists():
            with self._write_lock():
                pass
        self._register()

    def _secure(self) -> None:
        with directory_fd(self.root) as parent:
            os.fchmod(parent, 0o700)
            descriptor = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=parent)
            try:
                self._regular(descriptor)
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)

    @staticmethod
    def _regular(descriptor: int):
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("private vault files must be regular files without hard links")

    def _read_json(self, path: Path, limit: int):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("invalid private vault duplicate field")
                result[key] = value
            return result

        with directory_fd(self.root) as parent:
            raw = read_at(parent, path.name, limit)
        return json.loads(raw, object_pairs_hook=unique)

    def _sync_root(self) -> None:
        with directory_fd(self.root) as descriptor:
            os.fsync(descriptor)

    def _unlink(self, path: Path):
        with directory_fd(self.root) as parent:
            os.unlink(path.name, dir_fd=parent)
            os.fsync(parent)

    def _atomic_json(self, path: Path, value: object) -> None:
        temporary = path.with_name(path.name + ".tmp")
        with directory_fd(self.root) as parent:
            descriptor = os.open(
                temporary.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=parent,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary.name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)

    def _read(self) -> dict[str, str]:
        try:
            data = self._read_json(self.path, 128 * 1024 * 1024)
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in data.items()
        ):
            raise ValueError("invalid private vault")
        return data

    def _register(self) -> tuple[int, str]:
        try:
            data = self._read_json(self.deletion_register_path, 4096)
        except FileNotFoundError:
            return 0, "0" * 64
        return parse_credential_deletion_register(data)

    @staticmethod
    def _next_digest(previous: str, operation_id: str, key: str, value_digest: str) -> str:
        event = json.dumps({
            "operation_id": operation_id,
            "key_sha256": hashlib.sha256(key.encode()).hexdigest(),
            "value_sha256": value_digest,
        }, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(bytes.fromhex(previous) + event).hexdigest()

    def _recover_deletion_locked(self) -> None:
        removed = False
        for path in (self.path, self.deletion_register_path, self.deletion_pending_path):
            temporary = path.with_name(path.name + ".tmp")
            if temporary.exists():
                self._unlink(temporary)
                removed = True
        legacy_temporary = self.path.with_suffix(".tmp")
        if legacy_temporary.exists():
            self._unlink(legacy_temporary)
            removed = True
        if removed:
            self._sync_root()
        pending_path = self.deletion_pending_path
        if not pending_path.exists():
            return
        pending = self._read_json(pending_path, 16 * 1024)
        if (
            not isinstance(pending, dict)
            or set(pending) != {
                "schema_version", "operation_id", "key", "value_sha256",
                "previous_count", "previous_sha256", "next_sha256",
            }
            or type(pending["schema_version"]) is not int
            or pending["schema_version"] != 1
            or not isinstance(pending["operation_id"], str)
            or re.fullmatch(r"[0-9a-f]{32}", pending["operation_id"]) is None
            or not isinstance(pending["key"], str)
            or not 0 < len(pending["key"]) <= 1_000
            or type(pending["previous_count"]) is not int
            or pending["previous_count"] < 0
            or any(
                not isinstance(pending[name], str)
                or re.fullmatch(r"[0-9a-f]{64}", pending[name]) is None
                for name in ("value_sha256", "previous_sha256", "next_sha256")
            )
            or pending["next_sha256"] != self._next_digest(
                pending["previous_sha256"], pending["operation_id"],
                pending["key"], pending["value_sha256"],
            )
        ):
            raise ValueError("invalid private vault deletion intent")
        previous = pending["previous_count"], pending["previous_sha256"]
        next_state = previous[0] + 1, pending["next_sha256"]
        current = self._register()
        if current not in (previous, next_state):
            raise ValueError("private vault deletion register diverged")
        values = self._read()
        old_value = values.get(pending["key"])
        if old_value is not None:
            if hashlib.sha256(old_value.encode()).hexdigest() != pending["value_sha256"]:
                raise ValueError("private vault deletion target changed")
            values.pop(pending["key"])
            self._atomic_json(self.path, values)
        if current == previous:
            self._atomic_json(self.deletion_register_path, {
                "schema_version": 1, "count": next_state[0], "sha256": next_state[1],
            })
        self._unlink(pending_path)
        self._sync_root()

    @contextmanager
    def _write_lock(self) -> Iterator[None]:
        with self._lock:
            with directory_fd(self.root) as parent:
                descriptor = os.open(
                    self.lock_path.name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600, dir_fd=parent,
                )
            try:
                self._regular(descriptor)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                self._recover_deletion_locked()
                yield
            finally:
                os.close(descriptor)

    def set(self, profile_id: str, secret: str) -> None:
        with self._write_lock():
            values = self._read()
            if profile_id in values and values[profile_id] != secret:
                self._delete_locked(profile_id, values)
                values = self._read()
            values[profile_id] = secret
            self._atomic_json(self.path, values)
            self._secure()

    def get(self, profile_id: str) -> str | None:
        if self.deletion_pending_path.exists():
            with self._write_lock():
                pass
        with self._lock:
            return self._read().get(profile_id)

    def has(self, profile_id: str) -> bool:
        return self.get(profile_id) is not None

    def _delete_locked(self, profile_id: str, values: dict[str, str]) -> None:
        value = values.pop(profile_id)
        count, digest = self._register()
        operation_id = uuid4().hex
        value_digest = hashlib.sha256(value.encode()).hexdigest()
        next_digest = self._next_digest(digest, operation_id, profile_id, value_digest)
        self._atomic_json(self.deletion_pending_path, {
            "schema_version": 1, "operation_id": operation_id, "key": profile_id,
            "value_sha256": value_digest, "previous_count": count,
            "previous_sha256": digest, "next_sha256": next_digest,
        })
        self._atomic_json(self.path, values)
        self._atomic_json(self.deletion_register_path, {
            "schema_version": 1, "count": count + 1, "sha256": next_digest,
        })
        self._unlink(self.deletion_pending_path)
        self._sync_root()
        self._secure()

    def delete(self, profile_id: str) -> None:
        with self._write_lock():
            values = self._read()
            if profile_id in values:
                self._delete_locked(profile_id, values)


class ProviderRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = (settings.data_dir / "providers").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.vault = FileSecretVault(settings.data_dir / "secrets")
        self._lock = threading.RLock()
        self._held_transaction = threading.local()
        with self._transaction():
            self._bootstrap_environment_profile()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            if getattr(self._held_transaction, "active", False):
                yield
                return
            descriptor = os.open(
                self.root / ".registry.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                self._held_transaction.active = True
                yield
            finally:
                self._held_transaction.active = False
                os.close(descriptor)

    @staticmethod
    def _safe_id(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value) or value in {".", ".."}:
            raise ValueError("invalid provider profile id")
        return value

    def _path(self, profile_id: str) -> Path:
        return self.root / f"{self._safe_id(profile_id)}.json"

    def _write(self, profile: ProviderProfile) -> None:
        path = self._path(profile.id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            profile.model_dump_json(indent=2, exclude={"secret_configured"}) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def _bootstrap_environment_profile(self) -> None:
        credential = self.settings.anthropic_auth_token or self.settings.anthropic_api_key
        if not self.settings.claude_model or not credential:
            return
        auth_env_name = (
            "ANTHROPIC_AUTH_TOKEN"
            if self.settings.anthropic_auth_token
            else "ANTHROPIC_API_KEY"
        )
        path = self._path("environment")
        if not path.exists():
            profile = ProviderProfile(
                id="environment",
                display_name="Environment provider",
                base_url=self.settings.anthropic_base_url,
                auth_env_name=auth_env_name,
                model=self.settings.claude_model,
                default_haiku_model=self.settings.claude_default_haiku_model,
                default_sonnet_model=self.settings.claude_default_sonnet_model,
                default_opus_model=self.settings.claude_default_opus_model,
                subagent_model=self.settings.claude_subagent_model,
                region="configured environment",
            )
            self._write(profile)
        if not self.vault.has("environment"):
            self.vault.set("environment", credential.get_secret_value())

    def list(self) -> list[ProviderProfile]:
        profiles: list[ProviderProfile] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                profile = ProviderProfile.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            profiles.append(
                profile.model_copy(update={"secret_configured": self.vault.has(profile.id)})
            )
        return profiles

    def get(self, profile_id: str) -> ProviderProfile:
        path = self._path(profile_id)
        if not path.exists():
            raise ProviderNotFoundError(profile_id)
        profile = ProviderProfile.model_validate_json(path.read_text(encoding="utf-8"))
        return profile.model_copy(update={"secret_configured": self.vault.has(profile_id)})

    def upsert(self, profile_id: str, payload: ProviderProfileUpsert) -> ProviderProfile:
        self._safe_id(profile_id)
        with self._transaction():
            try:
                previous = self.get(profile_id)
            except ProviderNotFoundError:
                previous = None
            if previous is not None and payload.secret is None and (
                previous.base_url != payload.base_url
                or previous.auth_env_name != payload.auth_env_name
            ):
                raise ValueError("A new Provider key is required when its endpoint changes")
            configuration_changed = previous is None or payload.secret is not None or any(
                value != previous.model_dump()[key]
                for key, value in payload.model_dump(exclude={
                    "secret", "display_name", "region", "production_eligible", "terms_checked_at",
                }).items()
            )
            profile = ProviderProfile(
                id=profile_id,
                configuration_revision=(
                    previous.configuration_revision + int(configuration_changed) if previous else 1
                ),
                **payload.model_dump(exclude={"secret"}),
                last_verified_at=(
                    previous.last_verified_at if previous and not configuration_changed else None
                ),
                last_error_code=(
                    previous.last_error_code if previous and not configuration_changed else None
                ),
                last_error_message=(
                    previous.last_error_message if previous and not configuration_changed else None
                ),
            )
            if payload.secret is not None:
                if previous is not None:
                    # A crash after changing the key must leave the old profile
                    # unrunnable, never pair the new key with its old endpoint.
                    self._write(previous.model_copy(update={"enabled": False}))
                self.vault.set(profile_id, payload.secret)
            elif previous is None:
                # A failed earlier create may have left a key with no profile.
                self.vault.delete(profile_id)
            self._write(profile)
            return self.get(profile_id)

    def record_preflight(
        self, result: ProviderPreflightResult, *, expected_revision: int | None = None,
    ) -> ProviderProfile:
        with self._transaction():
            profile = self.get(result.provider_profile_id)
            if (expected_revision is not None
                    and profile.configuration_revision != expected_revision):
                raise ProviderConfigurationError(
                    "Provider configuration changed during preflight; run preflight again.",
                    ProviderErrorCode.SDK_RUNTIME_FAILED,
                )
            updated = profile.model_copy(
                update={
                    "last_verified_at": (
                        result.checked_at if result.ok else profile.last_verified_at
                    ),
                    "last_error_code": result.error_code,
                    "last_error_message": None if result.ok else result.message,
                    "secret_configured": False,
                }
            )
            self._write(updated)
            return self.get(profile.id)

    def export_credential(self, profile_id: str) -> dict[str, object]:
        with self._transaction():
            profile = self.get(profile_id)
            secret = self.vault.get(profile_id)
            if secret is None:
                raise ProviderConfigurationError(
                    "Provider credential is unavailable",
                    ProviderErrorCode.AUTH_REVOKED_OR_INVALID,
                )
            return {
                "schema_version": 1,
                "provider": profile.model_dump(mode="json", exclude={
                    "secret_configured", "last_error_message",
                }),
                "secret": secret,
            }

    def revoke_credential(self, profile_id: str) -> ProviderProfile:
        with self._transaction():
            profile = self.get(profile_id)
            if profile_id == "environment" and (
                self.settings.anthropic_auth_token or self.settings.anthropic_api_key
            ):
                raise ProviderConfigurationError(
                    "Remove the host environment key before revoking this credential",
                    ProviderErrorCode.AUTH_REVOKED_OR_INVALID,
                )
            self.vault.delete(profile_id)
            self._write(profile.model_copy(update={
                "configuration_revision": profile.configuration_revision + 1,
                "last_verified_at": None,
                "last_error_code": ProviderErrorCode.AUTH_REVOKED_OR_INVALID,
                "last_error_message": "Provider credential was revoked",
            }))
            return self.get(profile_id)

    def default_profile_id(self) -> str | None:
        profiles = [item for item in self.list() if item.enabled and item.secret_configured]
        if not profiles:
            return None
        environment = next((item for item in profiles if item.id == "environment"), None)
        return (environment or profiles[0]).id

    def snapshot(self, profile_id: str) -> dict[str, object]:
        profile = self.get(profile_id)
        return profile.model_dump(
            mode="json",
            exclude={"secret_configured", "last_error_message"},
        )

    def build_worker_env(self, profile_id: str) -> dict[str, str]:
        profile = self.get(profile_id)
        if not profile.enabled:
            raise ProviderConfigurationError(
                f"Provider {profile.display_name} is disabled.",
                ProviderErrorCode.AUTH_REVOKED_OR_INVALID,
            )
        secret = self.vault.get(profile_id)
        if not secret:
            raise ProviderConfigurationError(
                f"Provider {profile.display_name} has no configured credential.",
                ProviderErrorCode.AUTH_REVOKED_OR_INVALID,
            )
        config_dir = (self.settings.data_dir / "runtime-config" / profile.id).resolve()
        config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            config_dir.chmod(0o700)
        except OSError:
            pass
        env = {name: "" for name in MODEL_SECRET_NAMES}
        env.update(
            {
                "ANTHROPIC_BASE_URL": profile.base_url or "",
                profile.auth_env_name: secret,
                "ANTHROPIC_MODEL": profile.model,
                "CLAUDE_CONFIG_DIR": str(config_dir),
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            }
        )
        mappings = {
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": profile.default_haiku_model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": profile.default_sonnet_model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": profile.default_opus_model,
            "CLAUDE_CODE_SUBAGENT_MODEL": profile.subagent_model,
        }
        for name, value in mappings.items():
            env[name] = value or profile.model
        if profile.capabilities.context_window_tokens:
            # The SDK guesses 200K for unknown third-party models; pin the real window
            # so auto-compaction does not fire at a fraction of actual capacity.
            env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(
                profile.capabilities.context_window_tokens
            )
        return env

    def preflight_configuration(self, profile_id: str) -> tuple[ProviderProfile, dict[str, str]]:
        """Freeze options and the credential under one configuration transaction."""
        with self._transaction():
            return self.get(profile_id), self.build_worker_env(profile_id)


def classify_provider_error(error: BaseException | str) -> ProviderErrorCode:
    from .sdk_resources import SdkResourceLimitError

    if isinstance(error, SdkResourceLimitError):
        return ProviderErrorCode.SDK_RESOURCE_LIMIT_EXCEEDED
    message = str(error).casefold()
    if any(token in message for token in ("401", "unauthorized", "invalid api key", "oauth")):
        return ProviderErrorCode.AUTH_REVOKED_OR_INVALID
    if any(token in message for token in ("404", "model not found", "unknown model")):
        return ProviderErrorCode.MODEL_NOT_AVAILABLE
    if any(token in message for token in ("quota", "billing", "credit", "429")):
        return ProviderErrorCode.QUOTA_OR_BILLING
    if any(token in message for token in ("dns", "connection", "connecterror", "unreachable")):
        return ProviderErrorCode.BASE_URL_UNREACHABLE
    if "mcp" in message:
        return ProviderErrorCode.MCP_INIT_FAILED
    if "skill" in message or "plugin" in message:
        return ProviderErrorCode.SKILL_DISCOVERY_FAILED
    if any(token in message for token in ("protocol", "unexpected content", "invalid message")):
        return ProviderErrorCode.ANTHROPIC_PROTOCOL_INCOMPATIBLE
    return ProviderErrorCode.SDK_RUNTIME_FAILED


def provider_error_message(code: ProviderErrorCode) -> str:
    messages = {
        ProviderErrorCode.AUTH_REVOKED_OR_INVALID: (
            "凭据无效或已撤销，请更新当前 Provider 的 Key 后重试。"
        ),
        ProviderErrorCode.BASE_URL_UNREACHABLE: "无法连接 Base URL，请检查地址、网络和区域。",
        ProviderErrorCode.MODEL_NOT_AVAILABLE: (
            "当前 Provider 不提供所选模型，请核对模型名或账号权限。"
        ),
        ProviderErrorCode.QUOTA_OR_BILLING: "Provider 额度或计费状态异常，请充值或检查套餐。",
        ProviderErrorCode.ANTHROPIC_PROTOCOL_INCOMPATIBLE: "该端点未通过 Anthropic 协议兼容检查。",
        ProviderErrorCode.MCP_INIT_FAILED: "Claude Agent SDK 已启动，但 OPHIRA MCP 初始化失败。",
        ProviderErrorCode.SKILL_DISCOVERY_FAILED: "Claude Agent SDK 未发现 OPHIRA Plugin Skill。",
        ProviderErrorCode.SDK_RUNTIME_FAILED: "Claude Agent SDK 运行失败，请查看本次预检详情。",
        ProviderErrorCode.SDK_RESOURCE_LIMIT_EXCEEDED: (
            "运行达到执行时长或输出上限，已保存的工作保留；请检查输出量和运行资源配置。"
        ),
    }
    return messages[code]


def successful_preflight(profile_id: str, **changes: object) -> ProviderPreflightResult:
    return ProviderPreflightResult(
        provider_profile_id=profile_id,
        ok=True,
        message="Claude Agent SDK、MCP 与 Plugin Skill 预检通过。",
        checked_at=utc_now(),
        **changes,
    )

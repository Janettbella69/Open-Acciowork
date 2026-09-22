from __future__ import annotations

import os
import re
import threading
from pathlib import Path

from .config import Settings
from .constants import ProviderErrorCode
from .models import (
    ProviderPreflightResult,
    ProviderProfile,
    ProviderProfileUpsert,
    utc_now,
)
from .secret_vault import FileSecretVault as FileSecretVault

MODEL_SECRET_NAMES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


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


class ProviderRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = (settings.data_dir / "providers").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.vault = FileSecretVault(settings.data_dir.resolve() / "secrets")
        self._lock = threading.RLock()
        self._bootstrap_environment_profile()

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
        self.vault.set("environment", credential.get_secret_value(), only_if_absent=True)

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
        previous: ProviderProfile | None = None
        try:
            previous = self.get(profile_id)
        except ProviderNotFoundError:
            pass
        profile = ProviderProfile(
            id=profile_id,
            **payload.model_dump(exclude={"secret"}),
            last_verified_at=previous.last_verified_at if previous else None,
            last_error_code=previous.last_error_code if previous else None,
            last_error_message=previous.last_error_message if previous else None,
        )
        with self._lock:
            self._write(profile)
            if payload.secret is not None:
                self.vault.set(profile_id, payload.secret)
        return self.get(profile_id)

    def record_preflight(self, result: ProviderPreflightResult) -> ProviderProfile:
        profile = self.get(result.provider_profile_id)
        updated = profile.model_copy(
            update={
                "last_verified_at": result.checked_at if result.ok else profile.last_verified_at,
                "last_error_code": result.error_code,
                "last_error_message": None if result.ok else result.message,
                "secret_configured": False,
            }
        )
        with self._lock:
            self._write(updated)
        return self.get(profile.id)

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


def classify_provider_error(error: BaseException | str) -> ProviderErrorCode:
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

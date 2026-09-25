from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]


def companion_origin(value: str) -> str:
    """One explicit HTTPS origin, never a redirect or an arbitrary URL prefix."""
    try:
        parsed = urlsplit(value)
        if (
            not value.isascii() or any(char.isspace() for char in value)
            or parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"} or "\\" in value or "%" in parsed.netloc
            or parsed.hostname.endswith(".")
        ):
            raise ValueError
        hostname = parsed.hostname.lower()
        host = f"[{hostname}]" if ":" in hostname else hostname
        port = parsed.port
        return f"https://{host}" + (f":{port}" if port not in {None, 443} else "")
    except ValueError as exc:
        raise ValueError("companion_public_origin must be an exact HTTPS origin") from exc


def public_https_origin(value: str, setting_name: str) -> str:
    try:
        origin = companion_origin(value)
        hostname = urlsplit(origin).hostname or ""
        if (
            hostname == "localhost"
            or hostname.endswith((".localhost", ".local", ".test", ".invalid"))
        ):
            raise ValueError
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
        if address is None and "." not in hostname:
            raise ValueError
        return origin
    except ValueError as exc:
        raise ValueError(f"{setting_name} must be a public HTTPS origin") from exc


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPHIRA_",
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    data_dir: Path = BACKEND_DIR / "data"
    capability_plugin_dir: Path = BACKEND_DIR / "ophira-capabilities"
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:3000",
        "http://localhost:5173",
    ]
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65_535)
    claude_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPHIRA_CLAUDE_MODEL", "ANTHROPIC_MODEL"),
    )
    anthropic_base_url: str | None = Field(
        default=None,
        validation_alias="ANTHROPIC_BASE_URL",
    )
    anthropic_auth_token: SecretStr | None = Field(
        default=None,
        validation_alias="ANTHROPIC_AUTH_TOKEN",
    )
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="ANTHROPIC_API_KEY",
    )
    claude_default_haiku_model: str | None = Field(
        default=None,
        validation_alias="ANTHROPIC_DEFAULT_HAIKU_MODEL",
    )
    claude_default_fable_model: str | None = Field(
        default=None,
        validation_alias="ANTHROPIC_DEFAULT_FABLE_MODEL",
    )
    claude_default_sonnet_model: str | None = Field(
        default=None,
        validation_alias="ANTHROPIC_DEFAULT_SONNET_MODEL",
    )
    claude_default_opus_model: str | None = Field(
        default=None,
        validation_alias="ANTHROPIC_DEFAULT_OPUS_MODEL",
    )
    claude_subagent_model: str | None = Field(
        default=None,
        validation_alias="CLAUDE_CODE_SUBAGENT_MODEL",
    )
    claude_effort_level: str | None = Field(
        default=None,
        validation_alias="CLAUDE_CODE_EFFORT_LEVEL",
    )
    claude_auto_compact_window: int | None = Field(
        default=None,
        ge=1,
        validation_alias="CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    )
    claude_max_context_tokens: int | None = Field(
        default=None,
        ge=1,
        validation_alias="CLAUDE_CODE_MAX_CONTEXT_TOKENS",
    )
    # FR-23 retrieval fallback (ophira_web MCP). "none" keeps search honestly
    # unconfigured — the tool then returns a structured error instead of fake results.
    search_provider: str = Field(default="none", pattern="^(none|serper|tavily)$")
    search_api_key: SecretStr | None = None
    web_fetch_max_bytes: int = Field(default=2_000_000, ge=10_000, le=20_000_000)
    web_timeout_seconds: float = Field(default=20, ge=1, le=120)
    # Local OCR is explicit opt-in: models are provisioned by the operator, not
    # implicitly downloaded during an upload request. No remote VLM endpoint.
    ocr_paddle_enabled: bool = False
    ocr_layout_model_dir: Path | None = None
    ocr_vl_model_dir: Path | None = None
    ocr_device: Literal["cpu", "gpu:0"] = "cpu"
    ocr_timeout_seconds: float = Field(default=60, gt=0, le=600)
    ocr_max_bytes: int = Field(default=32_000_000, ge=1, le=100_000_000)
    ocr_max_pages: int = Field(default=20, ge=1, le=200)
    ocr_max_pixels: int = Field(default=40_000_000, ge=1, le=100_000_000)
    ocr_memory_mb: int = Field(default=8_192, ge=256, le=32_768)
    ocr_tesseract_languages: str = Field(
        default="chi_sim+eng", min_length=1, max_length=120,
        pattern=r"^[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*$",
    )
    permission_timeout_seconds: int = Field(default=28_800, ge=10, le=86_400)
    # ``local_only`` is an explicit single-operator boundary, not authentication.
    # A deployed multi-tenant control plane must opt into signed gateway assertions.
    auth_mode: Literal["local_only", "trusted_gateway"] = "local_only"
    trusted_gateway_auth_secret: SecretStr | None = Field(
        default=None,
        min_length=32,
        max_length=512,
    )
    trusted_gateway_tenant_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,159}$",
    )
    auth_assertion_max_age_seconds: int = Field(default=60, ge=10, le=300)
    auth_assertion_future_skew_seconds: int = Field(default=5, ge=0, le=30)
    companion_enabled: bool = False
    companion_public_origin: str | None = Field(default=None, max_length=500)
    # Operator-owned public HTTPS origin for external event deliveries.
    event_public_origin: str | None = Field(default=None, max_length=500)
    site_public_origin: str | None = Field(default=None, max_length=500)
    push_enabled: bool = False
    push_vapid_private_key: SecretStr | None = Field(default=None, max_length=2_000)
    push_vapid_subject: str | None = Field(default=None, max_length=500)
    # Operator-owned exact origins, never a client-provided destination allowlist.
    push_allowed_origins: Annotated[list[str], NoDecode] = [
        "https://fcm.googleapis.com", "https://updates.push.services.mozilla.com",
        "https://web.push.apple.com",
    ]
    push_worker_interval_seconds: float = Field(default=5, ge=1, le=60)
    push_min_interval_seconds: int = Field(default=15, ge=1, le=300)
    push_max_attempts_per_day: int = Field(default=100, ge=1, le=1_000)
    media_endpoint: str | None = Field(default=None, max_length=2_000)
    media_api_key: SecretStr | None = None
    channel_webhook_url: str | None = Field(default=None, max_length=2_000)
    feishu_encrypt_key: SecretStr | None = None
    feishu_bot_open_id: str | None = Field(default=None, max_length=160)
    # FR-24 outbound replies use the official Feishu/Lark tenant-token and
    # reply-message APIs. These credentials never enter Agent worker envs.
    feishu_app_id: str | None = Field(default=None, max_length=160)
    feishu_app_secret: SecretStr | None = Field(default=None, max_length=512)
    feishu_api_base_url: str = Field(
        default="https://open.feishu.cn",
        max_length=200,
    )
    feishu_delivery_timeout_seconds: float = Field(default=15, ge=1, le=120)
    feishu_delivery_max_attempts: int = Field(default=4, ge=1, le=20)
    feishu_delivery_retry_base_seconds: float = Field(default=2, ge=0.1, le=3_600)
    feishu_delivery_retry_max_seconds: float = Field(default=60, ge=0.1, le=3_600)
    feishu_delivery_worker_interval_seconds: float = Field(default=5, ge=0.1, le=300)
    # DingTalk Stream authenticates inbound callbacks with the app credential.
    # robot_code is matched against each callback before a Run is created.
    dingtalk_app_key: str | None = Field(default=None, max_length=160)
    dingtalk_app_secret: SecretStr | None = Field(default=None, max_length=512)
    dingtalk_robot_code: str | None = Field(default=None, max_length=160)
    dingtalk_delivery_timeout_seconds: float = Field(default=15, ge=1, le=120)
    # Shared only with the trusted authentication gateway. It is deliberately
    # shadowed out of every Claude Agent SDK worker environment.
    commercial_approval_secret: SecretStr | None = Field(
        default=None,
        min_length=32,
        max_length=512,
    )
    # A bad credential can make the SDK hang instead of failing fast (observed with
    # DeepSeek + an invalid token in the 2026-08-20 contract run); preflight must
    # always come back with an actionable diagnosis (FR-06).
    preflight_timeout_seconds: int = Field(default=120, ge=10, le=1_800)
    # 2026-08-23 拍板：不设轮数上限（教材口径：一个循环，让模型自己决定何时结束）；
    # 成本保险丝只留 max_budget_usd。显式配置仍可设值（如 CI/合约测试的小额探针）。
    max_turns: int | None = Field(default=None, ge=1)
    max_budget_usd: float = Field(default=10, gt=0, le=1_000)
    # Deep Research keeps the shared budget fuse and deliberate no-turn-cap policy,
    # but unattended fan-out still needs a wall-clock circuit breaker.
    deep_research_timeout_seconds: int = Field(default=1_800, ge=60, le=86_400)

    @field_validator(
        "claude_model",
        "anthropic_base_url",
        "anthropic_auth_token",
        "anthropic_api_key",
        "claude_default_haiku_model",
        "claude_default_fable_model",
        "claude_default_sonnet_model",
        "claude_default_opus_model",
        "claude_subagent_model",
        "claude_effort_level",
        "commercial_approval_secret",
        "trusted_gateway_auth_secret",
        "trusted_gateway_tenant_id",
        "channel_webhook_url",
        "feishu_encrypt_key",
        "feishu_bot_open_id",
        "feishu_app_id",
        "feishu_app_secret",
        "dingtalk_app_key",
        "dingtalk_app_secret",
        "dingtalk_robot_code",
        "push_vapid_private_key",
        "push_vapid_subject",
        mode="before",
    )
    @classmethod
    def empty_string_is_unset(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @field_validator("cors_origins", "push_allowed_origins", mode="before")
    @classmethod
    def parse_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def trusted_gateway_requires_a_signing_secret(self) -> Settings:
        dingtalk_fields = (
            self.dingtalk_app_key,
            self.dingtalk_app_secret,
            self.dingtalk_robot_code,
        )
        if any(value is not None for value in dingtalk_fields) and not all(
            value is not None for value in dingtalk_fields
        ):
            raise ValueError(
                "DingTalk Stream requires OPHIRA_DINGTALK_APP_KEY, "
                "OPHIRA_DINGTALK_APP_SECRET and OPHIRA_DINGTALK_ROBOT_CODE"
            )
        if (
            all(value is not None for value in dingtalk_fields)
            and self.auth_mode != "trusted_gateway"
        ):
            raise ValueError("DingTalk Stream requires trusted_gateway identity bindings")
        if len(self.push_allowed_origins) > 20:
            raise ValueError("at most 20 push service origins may be configured")
        self.push_allowed_origins = [companion_origin(item) for item in self.push_allowed_origins]
        if self.companion_public_origin is not None:
            self.companion_public_origin = companion_origin(self.companion_public_origin)
        if self.event_public_origin is not None:
            self.event_public_origin = public_https_origin(
                self.event_public_origin, "event_public_origin",
            )
        if self.site_public_origin is not None:
            self.site_public_origin = public_https_origin(
                self.site_public_origin, "site_public_origin",
            )
            site_host = urlsplit(self.site_public_origin).hostname
            if site_host in {
                urlsplit(origin).hostname for origin in (
                    self.event_public_origin, self.companion_public_origin,
                ) if origin is not None
            }:
                raise ValueError("site_public_origin must use a separate host")
        if self.companion_enabled and self.companion_public_origin is None:
            raise ValueError("companion access requires OPHIRA_COMPANION_PUBLIC_ORIGIN")
        if self.auth_mode == "local_only":
            try:
                loopback = (
                    self.host.casefold() == "localhost"
                    or ipaddress.ip_address(self.host).is_loopback
                )
            except ValueError:
                loopback = False
            if not loopback:
                raise ValueError(
                    "local_only auth mode requires a loopback OPHIRA_HOST"
                )
        if self.auth_mode == "trusted_gateway":
            missing: list[str] = []
            if self.trusted_gateway_auth_secret is None:
                missing.append("OPHIRA_TRUSTED_GATEWAY_AUTH_SECRET")
            if self.trusted_gateway_tenant_id is None:
                missing.append("OPHIRA_TRUSTED_GATEWAY_TENANT_ID")
            if missing:
                raise ValueError(
                    "trusted_gateway auth mode requires " + " and ".join(missing)
                )
        return self

    def auth_status(self) -> dict[str, str | bool]:
        """Describe the real deployment boundary without implying local IAM."""

        trusted = self.auth_mode == "trusted_gateway"
        return {
            "mode": self.auth_mode,
            "authenticated": trusted,
            # Global Agent/Provider/Skill/Connector stores are intentionally one
            # deployment tenant until they are physically partitioned.
            "multi_tenant": False,
            "trusted_gateway_assertions_required": trusted,
        }

    def claude_sdk_env(self, isolated_config_dir: Path | None = None) -> dict[str, str]:
        """Build an isolated Claude Code worker environment for the legacy env profile.

        Claude Agent SDK merges this mapping over its host process environment. Empty model
        credential values deliberately shadow host OAuth/API credentials; the selected credential
        is then restored below. `CLAUDE_CONFIG_DIR` prevents a revoked host Claude Code login from
        contaminating an OPHIRA run.
        """
        config_dir = (
            isolated_config_dir or self.data_dir / "runtime-config" / "environment"
        ).resolve()
        config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            config_dir.chmod(0o700)
        except OSError:
            pass
        env: dict[str, str] = {
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_AUTH_TOKEN": "",
            "CLAUDE_CODE_OAUTH_TOKEN": "",
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        if self.anthropic_base_url:
            env["ANTHROPIC_BASE_URL"] = self.anthropic_base_url
        if self.anthropic_auth_token:
            env["ANTHROPIC_AUTH_TOKEN"] = self.anthropic_auth_token.get_secret_value()
        if self.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = self.anthropic_api_key.get_secret_value()

        if self.claude_model:
            env["ANTHROPIC_MODEL"] = self.claude_model

        model_mappings = {
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": self.claude_default_haiku_model,
            "ANTHROPIC_DEFAULT_FABLE_MODEL": self.claude_default_fable_model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": self.claude_default_sonnet_model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": self.claude_default_opus_model,
            "CLAUDE_CODE_SUBAGENT_MODEL": self.claude_subagent_model,
        }
        for name, configured_model in model_mappings.items():
            model = configured_model or self.claude_model
            if model:
                env[name] = model

        if self.claude_effort_level:
            env["CLAUDE_CODE_EFFORT_LEVEL"] = self.claude_effort_level
        if self.claude_auto_compact_window:
            env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(self.claude_auto_compact_window)
        if self.claude_max_context_tokens:
            env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(self.claude_max_context_tokens)
        return env

    def provider_status(self) -> dict[str, str | bool | None]:
        """Return runtime provider metadata without exposing credentials."""
        if self.anthropic_auth_token:
            authentication = "auth_token"
        elif self.anthropic_api_key:
            authentication = "api_key"
        else:
            authentication = "not_configured"
        return {
            "api_credentials_configured": bool(
                self.anthropic_auth_token or self.anthropic_api_key
            ),
            "authentication": authentication,
            "base_url": self.anthropic_base_url,
            "model": self.claude_model,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]


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
    permission_timeout_seconds: int = Field(default=300, ge=10, le=3_600)
    # A bad credential can make the SDK hang instead of failing fast (observed with
    # DeepSeek + an invalid token in the 2026-08-20 contract run); preflight must
    # always come back with an actionable diagnosis (FR-06).
    preflight_timeout_seconds: int = Field(default=120, ge=10, le=1_800)
    # 2026-08-23 拍板：不设轮数上限（教材口径：一个循环，让模型自己决定何时结束）；
    # 成本保险丝只留 max_budget_usd。显式配置仍可设值（如 CI/合约测试的小额探针）。
    max_turns: int | None = Field(default=None, ge=1)
    max_budget_usd: float = Field(default=10, gt=0, le=1_000)

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
        mode="before",
    )
    @classmethod
    def empty_string_is_unset(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

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

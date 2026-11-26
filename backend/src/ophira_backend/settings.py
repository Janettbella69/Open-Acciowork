"""Deployment configuration, read once from the environment.

This is infrastructure in the textbook's sense (p.20: 网关、计费、鉴权、多模型接入):
which model endpoint to call, where files live, and the per-run cost fuse.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPHIRA_", extra="ignore")

    data_dir: Path = BACKEND_DIR / "data"
    capabilities_dir: Path = BACKEND_DIR / "ophira-capabilities"

    # The model gateway: an official Anthropic-compatible endpoint (DeepSeek, Bailian,
    # GLM, Kimi, ...) or empty for Anthropic itself. Never an OpenAI-protocol shim.
    model_base_url: str = ""
    model_auth_env: Literal["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"] = "ANTHROPIC_AUTH_TOKEN"
    model_auth_token: SecretStr = SecretStr("")
    model: str = ""
    # Third-party models are guessed at 200K by the CLI; pin the real window if known.
    model_context_window: int | None = None

    # Cost fuse per run (billing infrastructure, not a turn limit).
    max_budget_usd: float = 10.0

    # Where this backend listens, so the CLI can reach the model gateway on loopback.
    self_url: str = "http://127.0.0.1:8000"
    # Browser origins allowed besides same-host, comma-separated (the public site
    # reaches the backend through a proxy that rewrites Host).
    cors_origins: str = ""

    @property
    def allowed_origins(self) -> set[str]:
        return {o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()

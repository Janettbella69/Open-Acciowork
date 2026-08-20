from pathlib import Path

from ophira_backend.config import Settings
from ophira_backend.models import ProviderCapabilities, ProviderProfileUpsert
from ophira_backend.providers import ProviderRegistry


def build_registry(tmp_path: Path) -> ProviderRegistry:
    return ProviderRegistry(Settings(data_dir=tmp_path))


def upsert_profile(registry: ProviderRegistry, profile_id: str, **overrides) -> None:
    payload = {
        "display_name": profile_id,
        "base_url": "https://api.deepseek.com/anthropic",
        "auth_env_name": "ANTHROPIC_AUTH_TOKEN",
        "model": "deepseek-v4-pro",
        "secret": "test-credential",
    }
    payload.update(overrides)
    registry.upsert(profile_id, ProviderProfileUpsert(**payload))


def test_worker_env_pins_context_window_when_profile_declares_it(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)
    upsert_profile(
        registry,
        "deepseek",
        capabilities=ProviderCapabilities(context_window_tokens=1_000_000),
    )

    env = registry.build_worker_env("deepseek")

    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "1000000"


def test_worker_env_omits_context_window_when_profile_does_not_declare_it(
    tmp_path: Path,
) -> None:
    registry = build_registry(tmp_path)
    upsert_profile(registry, "plain")

    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in registry.build_worker_env("plain")


def test_budget_scale_round_trips_through_registry_storage(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)
    upsert_profile(registry, "deepseek", budget_scale=8.0)

    assert registry.get("deepseek").budget_scale == 8.0
    assert registry.get("deepseek").capabilities.context_window_tokens is None

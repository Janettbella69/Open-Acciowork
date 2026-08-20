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


def test_worker_env_blanks_all_credentials_then_injects_only_the_profile_secret(
    tmp_path: Path,
) -> None:
    registry = build_registry(tmp_path)
    upsert_profile(registry, "deepseek")

    env = registry.build_worker_env("deepseek")

    assert env["ANTHROPIC_AUTH_TOKEN"] == "test-credential"
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["CLAUDE_CONFIG_DIR"].endswith("runtime-config/deepseek")


def test_worker_env_pins_every_model_tier_with_profile_fallbacks(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)
    upsert_profile(registry, "deepseek", subagent_model="deepseek-v4-flash")

    env = registry.build_worker_env("deepseek")

    assert env["ANTHROPIC_MODEL"] == "deepseek-v4-pro"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "deepseek-v4-pro"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "deepseek-v4-pro"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "deepseek-v4-pro"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "deepseek-v4-flash"


def test_secret_vault_files_are_private_and_never_served_in_snapshots(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)
    upsert_profile(registry, "deepseek")

    vault_path = registry.vault.path
    assert vault_path.exists()
    assert (vault_path.stat().st_mode & 0o777) == 0o600
    assert (registry.vault.root.stat().st_mode & 0o777) == 0o700

    snapshot = registry.snapshot("deepseek")
    assert "secret_configured" not in snapshot
    assert "last_error_message" not in snapshot
    assert "test-credential" not in str(snapshot)
    profile_file = (registry.root / "deepseek.json").read_text(encoding="utf-8")
    assert "test-credential" not in profile_file


def test_upsert_without_secret_keeps_existing_credential_and_preflight_history(
    tmp_path: Path,
) -> None:
    from ophira_backend.providers import successful_preflight

    registry = build_registry(tmp_path)
    upsert_profile(registry, "deepseek")
    registry.record_preflight(successful_preflight("deepseek"))
    verified_at = registry.get("deepseek").last_verified_at
    assert verified_at is not None

    upsert_profile(registry, "deepseek", secret=None, display_name="DeepSeek renamed")

    profile = registry.get("deepseek")
    assert profile.display_name == "DeepSeek renamed"
    assert profile.secret_configured is True
    assert profile.last_verified_at == verified_at


def test_default_profile_requires_an_enabled_profile_with_a_credential(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)
    assert registry.default_profile_id() is None

    upsert_profile(registry, "deepseek")
    assert registry.default_profile_id() == "deepseek"


def test_profile_ids_reject_path_traversal(tmp_path: Path) -> None:
    import pytest

    registry = build_registry(tmp_path)
    for bad in ("../escape", "a/b", ".", "..", "a b"):
        with pytest.raises(ValueError):
            registry.get(bad)


def test_provider_errors_classify_into_the_eight_documented_codes(tmp_path: Path) -> None:
    from ophira_backend.constants import ProviderErrorCode
    from ophira_backend.providers import classify_provider_error

    cases = {
        "401 Unauthorized": ProviderErrorCode.AUTH_REVOKED_OR_INVALID,
        "OAuth access token has been revoked": ProviderErrorCode.AUTH_REVOKED_OR_INVALID,
        "404 model not found": ProviderErrorCode.MODEL_NOT_AVAILABLE,
        "insufficient quota, please top up": ProviderErrorCode.QUOTA_OR_BILLING,
        "429 too many requests": ProviderErrorCode.QUOTA_OR_BILLING,
        "DNS resolution failed": ProviderErrorCode.BASE_URL_UNREACHABLE,
        "MCP server failed to initialize": ProviderErrorCode.MCP_INIT_FAILED,
        "plugin skill missing": ProviderErrorCode.SKILL_DISCOVERY_FAILED,
        "unexpected content block in protocol": ProviderErrorCode.ANTHROPIC_PROTOCOL_INCOMPATIBLE,
        "something exploded": ProviderErrorCode.SDK_RUNTIME_FAILED,
    }
    for message, expected in cases.items():
        assert classify_provider_error(message) == expected, message

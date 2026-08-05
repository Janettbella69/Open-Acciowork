from ophira_backend.config import Settings


def test_comma_separated_cors_origins_from_environment(monkeypatch) -> None:
    monkeypatch.setenv(
        "OPHIRA_CORS_ORIGINS",
        "http://localhost:3000,http://localhost:5173",
    )

    settings = Settings(_env_file=None)

    assert settings.cors_origins == [
        "http://localhost:3000",
        "http://localhost:5173",
    ]


def test_anthropic_gateway_environment_is_forwarded_without_model_gaps(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "deepseek-secret")
    monkeypatch.setenv("OPHIRA_CLAUDE_MODEL", "deepseek-v4-pro[1m]")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_HAIKU_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    monkeypatch.setenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "1000000")

    settings = Settings(_env_file=None)

    assert settings.claude_sdk_env() == {
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "deepseek-secret",
        "ANTHROPIC_MODEL": "deepseek-v4-pro[1m]",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
        "ANTHROPIC_DEFAULT_FABLE_MODEL": "deepseek-v4-pro[1m]",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-pro[1m]",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro[1m]",
        "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-flash",
        "CLAUDE_CODE_EFFORT_LEVEL": "max",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000",
    }


def test_official_anthropic_model_variable_is_accepted(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_MODEL", "k3-256k")

    settings = Settings(_env_file=None)

    assert settings.claude_model == "k3-256k"
    assert settings.claude_sdk_env()["ANTHROPIC_MODEL"] == "k3-256k"


def test_blank_anthropic_values_do_not_override_claude_code_login(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "  ")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    settings = Settings(_env_file=None)

    assert settings.claude_sdk_env() == {}
    assert settings.provider_status() == {
        "api_credentials_configured": False,
        "authentication": "claude_code_login",
        "base_url": None,
        "model": None,
    }

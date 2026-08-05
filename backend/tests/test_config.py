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

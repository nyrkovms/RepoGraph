from app.config import Settings


def test_production_baseline_rejects_insecure_defaults() -> None:
    settings = Settings(app_env="production")
    errors = settings.production_baseline_errors()

    assert any("API_TOKEN_ADMIN" in err for err in errors)


def test_production_baseline_accepts_hardened_values() -> None:
    settings = Settings(
        app_env="production",
        api_token_admin="admin_token_strong",
        cors_allow_origins="https://app.example.com",
        trusted_hosts="api.example.com",
    )
    assert settings.production_baseline_errors() == []

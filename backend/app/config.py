from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    postgres_db: str = "packagehub"
    postgres_user: str = "packagehub"
    postgres_password: str = "packagehub"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"
    log_to_file: bool = True
    log_file: str = "/var/log/packagehub/app.log"
    log_file_max_bytes: int = 10485760
    log_file_backup_count: int = 3
    app_env: str = "development"
    api_docs_enabled: bool = True
    cors_allow_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    trusted_hosts: str = "localhost,127.0.0.1,testserver"
    security_hsts_enabled: bool = False

    api_token_viewer: str = "phub_viewer_dev"
    api_token_admin: str = "phub_admin_dev"

    sync_refresh_interval_hours: int = 6
    sync_refresh_batch_limit: int = 500
    sync_pypi_json_base_url: str = "https://pypi.org/pypi"
    sync_http_timeout_seconds: float = 10.0
    sync_http_max_retries: int = 3
    sync_http_retry_base_seconds: float = 0.5
    sync_http_retry_max_seconds: float = 8.0
    sync_http_retry_jitter_seconds: float = 0.25
    sync_failed_retry_interval_seconds: int = 900
    sync_failed_retry_batch_limit: int = 100
    sync_failed_retry_max_attempts: int = 8
    sync_failed_retry_backoff_seconds: float = 30.0
    sync_failed_retry_max_backoff_seconds: float = 1800.0
    sync_failed_retry_jitter_seconds: float = 3.0

    enumeration_batch_limit: int = 200
    enumeration_interval_hours: int = 6

    facts_timeout_seconds: float = 30.0

    probe_enabled: bool = True
    probe_max_wheel_bytes: int = 20_000_000
    probe_max_tarball_bytes: int = 30_000_000
    probe_readme_max_chars: int = 500_000
    probe_lock_ttl_seconds: int = 120
    probe_http_timeout_seconds: float = 60.0

    seed_packages: str = "flask,django,fastapi,requests,numpy,pandas"

    @property
    def async_db_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def sync_db_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def sync_pypi_json_base_url_normalized(self) -> str:
        return self.sync_pypi_json_base_url.rstrip("/")

    @property
    def seed_package_list(self) -> list[str]:
        return [pkg.strip() for pkg in self.seed_packages.split(",") if pkg.strip()]

    @property
    def cors_allow_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()]

    @property
    def trusted_hosts_list(self) -> list[str]:
        return [host.strip() for host in self.trusted_hosts.split(",") if host.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() in {"prod", "production"}

    def production_baseline_errors(self) -> list[str]:
        errors: list[str] = []
        if not self.is_production:
            return errors

        if self.api_token_viewer == "phub_viewer_dev":
            errors.append("API_TOKEN_VIEWER must be overridden in production.")
        if self.api_token_admin == "phub_admin_dev":
            errors.append("API_TOKEN_ADMIN must be overridden in production.")

        origins = self.cors_allow_origins_list
        if not origins:
            errors.append("CORS_ALLOW_ORIGINS must not be empty in production.")
        if "*" in origins:
            errors.append("CORS_ALLOW_ORIGINS must not contain '*' in production.")

        hosts = self.trusted_hosts_list
        if not hosts:
            errors.append("TRUSTED_HOSTS must not be empty in production.")
        if "*" in hosts:
            errors.append("TRUSTED_HOSTS must not contain '*' in production.")

        return errors


@lru_cache
def get_settings() -> Settings:
    return Settings()

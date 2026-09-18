from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Deployment configuration. Every field is documented in `.env.example`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Application role: what request handlers and workers use. DELETE is revoked on
    # immutable tables for this role (ADR-0001).
    database_url: str
    # Privileged purge role: only the retention-expiry job connects with it (ADR-0001).
    database_url_purge: str
    # Schema owner: Alembic only. Creates roles, tables, triggers and grants.
    database_url_migrate: str

    redis_url: str = "redis://localhost:6379/0"
    log_level: str = "INFO"

    # First-run setup token (tech-stack §17): written 0600 here on every boot until setup
    # completes. In compose this path is a volume, so it survives a container replacement.
    setup_token_file: str = "/var/lib/linsuite/setup-token"


@lru_cache
def get_settings() -> Settings:
    return Settings()

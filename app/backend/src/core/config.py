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

    # --- Sessions (tech-stack §14) ---------------------------------------------------
    # HS256 signing key for session JWTs. Rotating it logs everyone out, which is the
    # intended emergency lever. No default: a shipped default is a forgeable session.
    jwt_secret: str
    # Staff Mode is the long-lived session, so a practitioner is never logged out mid-treatment.
    staff_session_hours: int = 12
    # Admin Mode's window (PRD §1), held in Redis against the session — not in the token.
    # Idle: the sliding part. Every admin request pushes it out again.
    admin_idle_minutes: int = 15
    # Hard: the ceiling the sliding window never passes. Re-authentication after this.
    admin_hard_limit_minutes: int = 30
    # `Secure` on the session cookie. Off only for plain-HTTP local development.
    cookie_secure: bool = True

    # Screen new passwords against HaveIBeenPwned (only a 5-character SHA-1 prefix leaves
    # the server). Off for air-gapped installs; the bundled list is then the only check.
    breach_check_enabled: bool = True

    # First-run setup token (tech-stack §17): written 0600 here on every boot until setup
    # completes. In compose this path is a volume, so it survives a container replacement.
    setup_token_file: str = "/var/lib/linsuite/setup-token"


@lru_cache
def get_settings() -> Settings:
    return Settings()

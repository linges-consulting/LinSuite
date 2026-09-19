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

    # How long a password-reset link stays usable. tech-stack §14 says 30–60 minutes: long
    # enough to survive a slow mailbox, short enough that a link left in an inbox goes stale.
    password_reset_minutes: int = 45
    # How long an invitation to a new staff member stays usable. Far longer than a reset,
    # deliberately: nobody asked for it, so it has to survive a weekend, a holiday and a
    # mailbox somebody only reads on Monday. Re-issuable from the Staff screen either way.
    invitation_hours: int = 72

    # --- Multi-factor authentication (tech-stack §14, `auth/mfa.py`) -----------------------
    # AES-256-GCM key for the TOTP secrets at rest, 32 bytes as hex. A TOTP secret cannot be
    # hashed — the server needs it in the clear to verify — so this is what stands between a
    # database dump and a working authenticator for every enrolled account. No default, and
    # escrowed like `JWT_SECRET`: losing it makes every enrolment unreadable.
    mfa_encryption_key: str
    # How long a verified second factor lets an administrator open Admin Mode windows before
    # being asked again. PRD §1: once per twelve hours, never on every mode switch.
    admin_mfa_interval_hours: int = 12
    # Recovery codes issued at enrolment, and re-issued as one set when regenerated.
    mfa_recovery_code_count: int = 10
    # How long an emailed one-time code stays usable. Short: it is the lower-assurance path.
    mfa_email_otp_minutes: int = 10

    # --- Brute-force throttling (tech-stack §14, `auth/throttle.py`) -----------------------
    # Consecutive failed password checks that lock the account. Everything below it is a
    # progressive delay, and the delay is what makes reaching this number expensive.
    lockout_threshold: int = 10
    # Ceiling on the doubling delay (1, 2, 4 … seconds). Past it every further failure waits
    # the same time, until the threshold arrives.
    lockout_delay_cap_seconds: int = 128
    # How long an unbroken run of failures stays unbroken. Nothing older counts.
    lockout_failure_window_minutes: int = 15
    # Lock durations for a first, second, third … lockout of the same account. The last entry
    # is the cap — 24 hours — never "forever": a permanent lock is a denial-of-service weapon
    # handed to anyone who knows a username.
    lockout_tier_minutes: list[int] = [15, 30, 60, 1440]
    # How long an account is remembered as a repeat offender. A day clean and it starts over.
    lockout_tier_decay_hours: int = 24
    # Reset links one address may ask for per window, so the form cannot be used to flood
    # somebody's mailbox. Independent of the failure counter: asking is not guessing.
    reset_request_limit: int = 3
    reset_request_window_minutes: int = 15

    # --- Notifications (tech-stack §6) -----------------------------------------------------
    # Which implementation of `notifications.providers.NotificationProvider` sends. `console`
    # writes the whole message to the log, which is how a reset link is retrieved locally.
    notification_provider: str = "console"
    # Origin that links in outgoing messages point back at. No trailing slash.
    app_base_url: str = "http://localhost"

    # Screen new passwords against HaveIBeenPwned (only a 5-character SHA-1 prefix leaves
    # the server). Off for air-gapped installs; the bundled list is then the only check.
    breach_check_enabled: bool = True

    # First-run setup token (tech-stack §17): written 0600 here on every boot until setup
    # completes. In compose this path is a volume, so it survives a container replacement.
    setup_token_file: str = "/var/lib/linsuite/setup-token"


@lru_cache
def get_settings() -> Settings:
    return Settings()

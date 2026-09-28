"""S2: the two secrets the app refuses to boot without (fix wave, finding 8).

`JWT_SECRET=change-me-...` boots happily and mints sessions anybody who has read the repo
can forge; `MFA_ENCRYPTION_KEY=change-me-...` is not hex, so `core/crypto` raises lazily at
the owner's first enrolment — and since `mfa_required_for_admin` defaults true, that is the
clean-clone path: wizard, login, forced enrolment, 500, no way forward. Both are caught at
`Settings()` instead, with the command that fixes them in the message.
"""

import pytest
from pydantic import ValidationError

from core.config import GENERATE, Settings

DSN = "postgresql+asyncpg://linsuite@localhost/linsuite"
VALID = {
    "database_url": DSN,
    "database_url_purge": DSN,
    "database_url_migrate": DSN,
    "jwt_secret": "0" * 64,
    "mfa_encryption_key": "11" * 32,
    "document_master_key": "22" * 32,
    "notification_credential_key": "33" * 32,
}


def settings(**overrides) -> Settings:
    # `_env_file=None`: the repo's own .env must not decide whether this passes.
    return Settings(_env_file=None, **{**VALID, **overrides})


def test_valid_secrets_boot():
    assert settings().jwt_secret == "0" * 64


@pytest.mark.parametrize(
    "field",
    ["jwt_secret", "mfa_encryption_key", "document_master_key", "notification_credential_key"],
)
def test_the_shipped_placeholder_is_refused_with_the_command_that_fixes_it(field):
    with pytest.raises(ValidationError) as refused:
        settings(**{field: "change-me-openssl-rand-hex-32"})
    assert GENERATE in str(refused.value)


def test_a_short_jwt_secret_is_refused():
    with pytest.raises(ValidationError):
        settings(jwt_secret="tooshort")


@pytest.mark.parametrize(
    "field", ["mfa_encryption_key", "document_master_key", "notification_credential_key"]
)
def test_a_key_that_is_not_32_bytes_of_hex_is_refused(field):
    with pytest.raises(ValidationError) as refused:
        settings(**{field: "not hex at all, but long enough to look like a key ok"})
    assert GENERATE in str(refused.value)
    assert field.upper() in str(refused.value)
    with pytest.raises(ValidationError):
        settings(**{field: "11" * 16})


def test_the_document_master_key_must_differ_from_the_mfa_key():
    with pytest.raises(ValidationError) as refused:
        settings(document_master_key="11" * 32)
    assert "must be different keys" in str(refused.value)
    with pytest.raises(ValidationError):
        settings(document_master_key="AB" * 32, mfa_encryption_key="ab" * 32)


def test_the_notification_credential_key_must_differ_from_the_other_two():
    with pytest.raises(ValidationError) as refused:
        settings(notification_credential_key="11" * 32)  # same as mfa_encryption_key
    assert "must be different keys" in str(refused.value)
    with pytest.raises(ValidationError):
        settings(notification_credential_key="22" * 32)  # same as document_master_key
    with pytest.raises(ValidationError):
        # Case-insensitive, same as the existing pair check.
        settings(notification_credential_key="AB" * 32, mfa_encryption_key="ab" * 32)


def test_the_document_master_key_is_required(monkeypatch):
    # The harness exports a valid one for the whole session; this test is about its absence.
    monkeypatch.delenv("DOCUMENT_MASTER_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{k: v for k, v in VALID.items() if k != "document_master_key"})


def test_the_notification_credential_key_is_required(monkeypatch):
    monkeypatch.delenv("NOTIFICATION_CREDENTIAL_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            **{k: v for k, v in VALID.items() if k != "notification_credential_key"},
        )


# --- the purge DSN is the worker's, not the web process's (Task 7 fix round 1) -------------


@pytest.mark.parametrize("value", [None, "", "  "])
def test_the_purge_dsn_is_optional_and_blank_means_absent(value, monkeypatch):
    monkeypatch.delenv("DATABASE_URL_PURGE", raising=False)
    overrides = {"database_url_purge": value} if value is not None else {}
    base = {k: v for k, v in VALID.items() if k != "database_url_purge"}
    assert Settings(_env_file=None, **base, **overrides).database_url_purge is None


def _without_the_purge_dsn(monkeypatch):
    from core.config import get_settings

    monkeypatch.setenv("DATABASE_URL_PURGE", "")
    get_settings.cache_clear()  # the caller clears it again after `monkeypatch.undo()`
    return get_settings


def test_the_worker_and_the_task_engines_refuse_to_start_without_it(monkeypatch, database):
    from core.celery_app import _require_the_purge_dsn
    from core.db import get_task_engines

    get_settings = _without_the_purge_dsn(monkeypatch)
    try:
        with pytest.raises(RuntimeError, match="DATABASE_URL_PURGE"):
            _require_the_purge_dsn()
        with pytest.raises(RuntimeError, match="DATABASE_URL_PURGE"):
            get_task_engines()
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def test_the_web_app_boots_and_serves_without_it(monkeypatch, database):
    from httpx import ASGITransport, AsyncClient

    from main import app

    get_settings = _without_the_purge_dsn(monkeypatch)
    try:
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                resp = await c.get("/api/health")
        assert resp.status_code == 200, resp.text
        assert get_settings().database_url_purge is None
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()

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
}


def settings(**overrides) -> Settings:
    # `_env_file=None`: the repo's own .env must not decide whether this passes.
    return Settings(_env_file=None, **{**VALID, **overrides})


def test_valid_secrets_boot():
    assert settings().jwt_secret == "0" * 64


@pytest.mark.parametrize(
    "field",
    ["jwt_secret", "mfa_encryption_key", "document_master_key"],
)
def test_the_shipped_placeholder_is_refused_with_the_command_that_fixes_it(field):
    with pytest.raises(ValidationError) as refused:
        settings(**{field: "change-me-openssl-rand-hex-32"})
    assert GENERATE in str(refused.value)


def test_a_short_jwt_secret_is_refused():
    with pytest.raises(ValidationError):
        settings(jwt_secret="tooshort")


@pytest.mark.parametrize("field", ["mfa_encryption_key", "document_master_key"])
def test_a_key_that_is_not_32_bytes_of_hex_is_refused(field):
    with pytest.raises(ValidationError) as refused:
        settings(**{field: "not hex at all, but long enough to look like a key ok"})
    assert GENERATE in str(refused.value)
    assert field.upper() in str(refused.value)
    with pytest.raises(ValidationError):
        settings(**{field: "11" * 16})


def test_the_document_master_key_is_required(monkeypatch):
    # The harness exports a valid one for the whole session; this test is about its absence.
    monkeypatch.delenv("DOCUMENT_MASTER_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{k: v for k, v in VALID.items() if k != "document_master_key"})

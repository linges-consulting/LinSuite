"""Shared test harness.

Seam S1: HTTP over a real PostgreSQL. A throwaway Postgres container is started once per
session, Alembic migrations are applied to it, and requests go through httpx's ASGI
transport straight into the FastAPI app. The app connects as the `linsuite_app` role, so
grant/revoke behaviour is exercised exactly as in production. No mocked database, ever.

Seam S2 (pure functions) needs no fixtures.

Seam S3: outbound messages. `NOTIFICATION_PROVIDER=recording` selects the fake in
`tests/fake_notifications.py` — a real implementation of the provider protocol, not a
patched HTTP client — and Celery runs eagerly so a handler's `delay()` lands in it before
the response is returned.
"""

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session")
def redis_server() -> Iterator[str]:
    """A throwaway Redis, for the `jti` denylist that makes logout real. Never faked."""
    with RedisContainer("redis:7-alpine") as container:
        yield f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/0"


@pytest.fixture(scope="session")
def database(tmp_path_factory, redis_server) -> Iterator[dict[str, str]]:
    """Start Postgres, migrate it, and expose the three DSNs the app is configured with."""
    container = PostgresContainer("postgres:16-alpine", driver="asyncpg").with_env(
        "POSTGRES_HOST_AUTH_METHOD", "trust"
    )
    with container:
        migrate_url = container.get_connection_url()
        host, port = container.get_container_host_ip(), container.get_exposed_port(5432)
        db = container.dbname
        urls = {
            "DATABASE_URL_MIGRATE": migrate_url,
            "DATABASE_URL": f"postgresql+asyncpg://linsuite_app@{host}:{port}/{db}",
            "DATABASE_URL_PURGE": f"postgresql+asyncpg://linsuite_purge@{host}:{port}/{db}",
            "REDIS_URL": redis_server,
            # The setup token file must not land in the real data dir during tests.
            "SETUP_TOKEN_FILE": str(tmp_path_factory.mktemp("run") / "setup-token"),
            "JWT_SECRET": "0" * 64,
            # The AES-256 key the TOTP secrets are sealed with. Fixed, so a test can decrypt
            # what the app wrote and prove the column holds a ciphertext and not the secret.
            "MFA_ENCRYPTION_KEY": "11" * 32,
            # The master key every client's document key is wrapped under. Fixed and distinct
            # from the MFA key, so a test that unwraps under the wrong one fails for real.
            "DOCUMENT_MASTER_KEY": "22" * 32,
            # The Resend/SMTP credential key on `businesses` (Phase 12 Task 2). Fixed and
            # distinct from the two above, for the same reason.
            "NOTIFICATION_CREDENTIAL_KEY": "33" * 32,
            # The ASGI harness speaks http://, and a `Secure` cookie is never sent over it.
            # Production leaves this on; `test_the_session_cookie_is_secure_when_configured`
            # is what proves the flag is wired up.
            "COOKIE_SECURE": "false",
            # No outbound HTTP from the suite; the HIBP client is exercised with a
            # MockTransport in tests/test_password_policy.py instead.
            "BREACH_CHECK_ENABLED": "false",
            # S3: the recording provider, registered by tests/fake_notifications.py.
            "NOTIFICATION_PROVIDER": "recording",
            "APP_BASE_URL": "http://test.linsuite.example",
        }
        os.environ.update(urls)

        cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
        command.upgrade(cfg, "head")
        yield urls


@pytest.fixture(scope="session", autouse=True)
def eager_celery(database) -> None:
    """Run tasks in the calling process. The handler still goes through `delay()`, so the
    path under test is the real one; only the broker hop is removed."""
    import tests.fake_notifications  # noqa: F401 — registers the `recording` provider
    from core.celery_app import celery_app

    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True


@pytest.fixture
def sent_emails(eager_celery) -> Iterator[list]:
    from tests.fake_notifications import sent

    sent.clear()
    yield sent
    sent.clear()


@pytest.fixture
def sent_sms(eager_celery) -> Iterator[list]:
    from tests.fake_notifications import sent_sms

    sent_sms.clear()
    yield sent_sms
    sent_sms.clear()


@pytest.fixture
def fail_next_send(eager_celery) -> Iterator[tuple[list, list]]:
    """`(fail_email_next, fail_sms_next)` — push an exception to make the next send of that
    channel raise it (Task 4, #11: simulating a delivery failure). Cleared before and after,
    so a test that doesn't push anything never sees another test's leftovers."""
    from tests.fake_notifications import fail_email_next, fail_sms_next

    fail_email_next.clear()
    fail_sms_next.clear()
    yield fail_email_next, fail_sms_next
    fail_email_next.clear()
    fail_sms_next.clear()


@pytest.fixture
async def client(database) -> AsyncIterator[AsyncClient]:
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# An account and the staff row that now always accompanies one. Since Task 10 every user has
# exactly one `staff` record — the wizard makes the first, `POST /api/admin/staff` makes the
# rest, and migration 0009 backfilled the ones that predated the table — and sign-in reads
# `staff.active`. A suite that inserted a bare `users` row would be describing an account no
# instance can have, and would be refused at the door. One statement so the pair can never
# land half-written.
_ACCOUNT = """
WITH account AS (
    INSERT INTO users (email, password_hash, role_id)
    VALUES (
        :email, :hash,
        coalesce(cast(:role AS uuid), (SELECT id FROM roles WHERE name = 'Staff'))
    )
    RETURNING id, email
)
INSERT INTO staff (user_id, first_name, display_name, colour)
SELECT id, split_part(email, '@', 1), split_part(email, '@', 1), 'teal' FROM account
RETURNING user_id
"""


async def add_account(email: str, password_hash: str, *, role: str | None = None) -> str:
    """Create an account on `role` (default: the seeded Staff role), and return its id."""
    from sqlalchemy import text

    from core.db import session_scope

    async with session_scope() as db:
        row = await db.execute(
            text(_ACCOUNT), {"email": email, "hash": password_hash, "role": role}
        )
        user_id = row.scalar_one()
        await db.commit()
    return str(user_id)


async def wipe_document_keys() -> None:
    """Clear `documents`, `form_submissions`, `session_notes`, `customer_document_keys` and
    `business_document_keys`, so a fixture can delete its customers and its business.

    Neither runtime role may do this wholesale — the app role holds no DELETE and the purge
    role is refused by the trigger for any client under a retention hold (migration 0024) —
    so it runs as the schema owner, the one role the trigger lets through. Tests only.

    `business_document_keys` (#55, migration 0044) FKs to `businesses.id`, and `claimed_
    instance`'s `wipe()` deletes `businesses` every test — the one role that guard's trigger
    lets through is this same schema owner, so it has to go here too, ahead of that delete."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    owner = create_async_engine(os.environ["DATABASE_URL_MIGRATE"])
    try:
        async with owner.begin() as conn:
            # #72: append-only, and it holds appointments/users/package credits by FK.
            await conn.execute(text("DELETE FROM package_credit_redemptions"))
            await conn.execute(text("DELETE FROM documents"))  # their FK holds the keys
            await conn.execute(text("DELETE FROM form_submissions"))  # so does theirs
            await conn.execute(text("DELETE FROM session_notes"))
            await conn.execute(text("DELETE FROM customer_document_keys"))
            await conn.execute(text("DELETE FROM business_document_keys"))
    finally:
        await owner.dispose()

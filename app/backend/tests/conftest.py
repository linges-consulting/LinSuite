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
async def client(database) -> AsyncIterator[AsyncClient]:
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

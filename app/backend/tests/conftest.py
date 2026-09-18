"""Shared test harness.

Seam S1: HTTP over a real PostgreSQL. A throwaway Postgres container is started once per
session, Alembic migrations are applied to it, and requests go through httpx's ASGI
transport straight into the FastAPI app. The app connects as the `linsuite_app` role, so
grant/revoke behaviour is exercised exactly as in production. No mocked database, ever.

Seam S2 (pure functions) needs no fixtures.
"""

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from testcontainers.community.postgres import PostgresContainer

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session")
def database() -> Iterator[dict[str, str]]:
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
            "REDIS_URL": "redis://localhost:6379/15",
        }
        os.environ.update(urls)

        cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
        command.upgrade(cfg, "head")
        yield urls


@pytest.fixture
async def client(database) -> AsyncIterator[AsyncClient]:
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

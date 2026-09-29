"""S1: the health endpoint over a real PostgreSQL."""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.config import get_settings
from core.db import get_session
from main import app
from tests.test_partitions import FUNCTION, NEXT


async def test_health_reports_ok_when_database_reachable(client):
    resp = await client.get("/api/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok", "partitions": "ok"}


async def test_health_reports_next_year_missing_but_still_200(client):
    # Boot itself ensures both partitions exist, so a lenient next-year check needs one
    # deliberately detached — the strict, boot-time check stays out of scope here (#85).
    owner = create_async_engine(get_settings().database_url_migrate)
    try:
        async with owner.begin() as conn:
            await conn.execute(text(f"DROP TABLE {NEXT}"))

        resp = await client.get("/api/health")

        assert resp.status_code == 200
        assert resp.json() == {
            "status": "ok",
            "database": "ok",
            "partitions": "next_year_missing",
        }
    finally:
        # Leave the stack with both partitions again, whatever the assertion above found.
        async with owner.begin() as conn:
            await conn.execute(text(f"SELECT {FUNCTION}"))
        await owner.dispose()


async def test_health_reports_degraded_and_logs_when_database_unreachable(client, caplog):
    # A real engine pointed at a port nothing listens on — not a mock.
    dead = create_async_engine("postgresql+asyncpg://linsuite_app@127.0.0.1:1/nothing")

    async def dead_session():
        async with async_sessionmaker(dead)() as session:
            yield session

    app.dependency_overrides[get_session] = dead_session
    try:
        with caplog.at_level(logging.ERROR, logger="main"):
            resp = await client.get("/api/health")
    finally:
        app.dependency_overrides.clear()
        await dead.dispose()

    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded", "database": "unreachable"}
    record = next(r for r in caplog.records if r.name == "main")
    assert record.getMessage() == "health: database unreachable"
    assert record.exc_info is not None

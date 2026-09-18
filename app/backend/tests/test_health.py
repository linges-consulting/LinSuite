"""S1: the health endpoint over a real PostgreSQL."""

import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.db import get_session
from main import app


async def test_health_reports_ok_when_database_reachable(client):
    resp = await client.get("/api/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}


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

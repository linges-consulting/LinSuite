"""S1: the health endpoint over a real PostgreSQL."""


async def test_health_reports_ok_when_database_reachable(client):
    resp = await client.get("/api/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}

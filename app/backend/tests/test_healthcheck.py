"""The dead-man ping the two nightly maintenance jobs make on success (tech-stack §3, #85).

Stubbed at the transport, never the task: `httpx.AsyncClient` is monkeypatched to build on
`httpx.MockTransport`, the same shape `core/security.py`'s HaveIBeenPwned client and
`notifications/providers.py` already use. The real network is never reached, and the tasks'
own async bodies are called directly rather than through Celery's `.delay()`.
"""

import logging

import httpx
import pytest

from core.config import get_settings
from core.healthcheck import ping_maintenance

URL = "https://hc-ping.com/test-check"
_REAL_ASYNC_CLIENT = httpx.AsyncClient  # captured before any test monkeypatches the name


def _stub(monkeypatch, respond) -> list[httpx.Request]:
    """Every `httpx.AsyncClient` built anywhere during the test answers with `respond`."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond(request)

    def fake_client(**kwargs: object) -> httpx.AsyncClient:
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    return seen


@pytest.fixture
def pinged(monkeypatch):
    return _stub(monkeypatch, lambda _request: httpx.Response(200))


@pytest.fixture(autouse=True)
def with_url(database, monkeypatch):
    monkeypatch.setattr(get_settings(), "healthcheck_url_maintenance", URL)


# --- the helper --------------------------------------------------------------------------


async def test_pings_the_configured_url_once(pinged):
    await ping_maintenance()
    assert [str(r.url) for r in pinged] == [URL]


async def test_an_unset_url_makes_no_request(pinged, monkeypatch):
    monkeypatch.setattr(get_settings(), "healthcheck_url_maintenance", None)
    await ping_maintenance()
    assert pinged == []


async def test_a_non_2xx_response_is_logged_and_does_not_raise(monkeypatch, caplog):
    seen = _stub(monkeypatch, lambda _request: httpx.Response(500))
    with caplog.at_level(logging.WARNING, logger="core.healthcheck"):
        await ping_maintenance()  # must not raise
    assert len(seen) == 1
    assert any("healthcheck" in r.getMessage() for r in caplog.records)


async def test_an_unreachable_url_is_logged_and_does_not_raise(monkeypatch, caplog):
    def unreachable(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=_request)

    _stub(monkeypatch, unreachable)
    with caplog.at_level(logging.WARNING, logger="core.healthcheck"):
        await ping_maintenance()  # must not raise


# --- the two nightly tasks: exactly once on success, never on failure --------------------


async def test_maintain_partitions_pings_once_on_success(pinged, database):
    from core.tasks import _maintain_partitions

    await _maintain_partitions()

    assert len(pinged) == 1


async def test_maintain_partitions_does_not_ping_when_it_fails(pinged, monkeypatch, database):
    from core import tasks

    async def boom(_conn):
        raise RuntimeError("boom")

    monkeypatch.setattr(tasks, "ensure_access_log_partitions", boom)

    with pytest.raises(RuntimeError):
        await tasks._maintain_partitions()

    assert pinged == []


async def test_purge_expired_pings_once_on_success(pinged, database):
    from customers.tasks import _purge_expired

    await _purge_expired()

    assert len(pinged) == 1


async def test_purge_expired_does_not_ping_when_it_fails(pinged, monkeypatch, database):
    from customers import tasks

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(tasks, "get_task_engines", boom)

    with pytest.raises(RuntimeError):
        await tasks._purge_expired()

    assert pinged == []

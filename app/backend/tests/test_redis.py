"""`core/redis.py`'s bounded timeouts (Task 19 fix round 1).

Every request-path Redis caller shares this one client: the session `jti` denylist
(`auth/session.py`), login throttling (`auth/throttle.py`), Admin Mode (`auth/modes.py`),
MFA (`auth/mfa.py`) — and, since Task 19, the availability cache's generation counter and
cached entries on every read and every post-commit bump. Before this fix, `from_url` set no
`socket_timeout`/`socket_connect_timeout`: a Redis that accepted the TCP connection but never
answered (a wedged process, a black-holing firewall — not the same thing as connection
*refused*, which already raises immediately) would hang the calling request forever instead
of failing it.

None of the four auth callers above catch a Redis exception at all — a `ConnectionError`
today, same as a `TimeoutError` after this fix, propagates uncaught to FastAPI's default
handler as a 500. Adding a bounded socket timeout does not change that policy (this file does
not touch auth code); it only bounds how long a wedged Redis takes to produce the same
failure. `tests/test_availability_cache.py` covers the one caller that *does* catch Redis
errors — `scheduling/cache.py` — including a real hanging listener, not just the timeout
config asserted here.
"""

import asyncio
import time

import pytest
from redis.asyncio import from_url
from redis.exceptions import RedisError

from core.redis import SOCKET_CONNECT_TIMEOUT, SOCKET_TIMEOUT, get_redis


async def test_the_shared_client_is_built_with_bounded_timeouts(database):
    kwargs = get_redis().connection_pool.connection_kwargs
    assert kwargs["socket_connect_timeout"] == SOCKET_CONNECT_TIMEOUT
    assert kwargs["socket_timeout"] == SOCKET_TIMEOUT


async def test_a_socket_that_accepts_and_never_answers_raises_within_the_bound():
    """A raw listener standing in for a wedged Redis: it takes the TCP connection (so this is
    not the `ConnectionRefusedError` path every caller already handles) and then never writes
    a byte back."""

    async def _swallow(reader, writer):
        # Never answer; leave once the client gives up. The writer must be closed, or
        # `async with server` waits on this connection forever (3.12's `wait_closed`).
        await reader.read()
        writer.close()

    server = await asyncio.start_server(_swallow, "127.0.0.1", 0)
    async with server:
        host, port = server.sockets[0].getsockname()[:2]
        client = from_url(
            f"redis://{host}:{port}/0",
            decode_responses=True,
            socket_connect_timeout=SOCKET_CONNECT_TIMEOUT,
            socket_timeout=SOCKET_TIMEOUT,
        )
        started = time.monotonic()
        with pytest.raises(RedisError):
            await client.get("x")
        elapsed = time.monotonic() - started

        assert elapsed < SOCKET_CONNECT_TIMEOUT + SOCKET_TIMEOUT + 2
        await client.aclose()

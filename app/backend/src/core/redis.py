"""The Redis connection, shared by everything that needs one.

Celery has its own (`core/celery_app.py` hands it a URL); this is the client the request
path uses — the session `jti` denylist, login throttling, Admin Mode, MFA, and (Task 19) the
availability cache's generation counter and cached entries.

**Bounded timeouts, always.** Without them, a Redis that accepts the TCP connection but never
answers (a wedged process, a firewall black-holing the reply) hangs the calling request
forever rather than failing it — every caller above already has to handle "Redis raised", so
a timeout is just another way to raise, on a clock instead of on `ECONNREFUSED`. Both connect
and read/write get the same one-second budget: this is a cache and a pair of security
counters, not a query that legitimately takes a while.
"""

from functools import lru_cache

from redis.asyncio import Redis, from_url

from core.config import get_settings

# Seconds. Module constants rather than a setting: there is nothing here a deployment would
# tune — a Redis this slow is a Redis to page somebody about, not configure around.
SOCKET_CONNECT_TIMEOUT = 1.0
SOCKET_TIMEOUT = 1.0


@lru_cache
def get_redis() -> Redis:
    return from_url(
        get_settings().redis_url,
        decode_responses=True,
        socket_connect_timeout=SOCKET_CONNECT_TIMEOUT,
        socket_timeout=SOCKET_TIMEOUT,
    )

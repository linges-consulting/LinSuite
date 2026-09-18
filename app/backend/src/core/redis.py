"""The Redis connection, shared by everything that needs one.

Celery has its own (`core/celery_app.py` hands it a URL); this is the client the request
path uses — today, the session `jti` denylist that makes logout real.
"""

from functools import lru_cache

from redis.asyncio import Redis, from_url

from core.config import get_settings


@lru_cache
def get_redis() -> Redis:
    return from_url(get_settings().redis_url, decode_responses=True)

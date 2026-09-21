"""Redis cache for the two availability read endpoints (tech-stack §19: TTL ~60s +
invalidation; staleness is safe because booking always re-validates against the database —
see `scheduling/slots.py` and `scheduling/appointments.py`, which never call `cached()`).

**One generation counter**, `avail:gen`. Every mutation that can change what the engine
computes calls `bump()` once, **after its own commit** — never before. A reader that ran
between the bump and the commit would compute against the *old* row values but cache them
under the *new* generation's key, and nothing would ever invalidate that entry again; `TTL`
is the only thing that would eventually clear it, which defeats the point of bumping at all.

**A cache key is the generation plus a hash of the parsed query** — service/staff ids as
`uuid.UUID`/`str`, dates as `date` — never the raw query string, so a URL with its params in
a different order, or a client that serialises the same values into two JSON layouts, lands
on the same key (`json.dumps(..., sort_keys=True)`). `service_ids` for the group endpoint is
a list, not a dict key, so `sort_keys` leaves its order alone — order is the chain's own
semantics there, tech-stack §19 step 4.

**"Now"**: the engine's output depends on the wall clock (lead time, past slots drop off).
A cached answer can be stale on that axis too, bounded by `TTL_SECONDS` — the same bound that
covers a missed invalidation — so it is not put in the key.

Every Redis call here is wrapped: a get, set or incr that raises (unreachable, timed out,
whatever) logs a warning and is treated as a cache miss / no-op, never a 500. `get_redis()`
(`core/redis.py`) is a lazy client, so the outage usually only surfaces on the first command.
"""

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable

from core.redis import get_redis

log = logging.getLogger(__name__)

TTL_SECONDS = 60
_GENERATION_KEY = "avail:gen"


async def bump() -> None:
    """Call once, after commit, from every mutation that can change an availability answer.

    A failed bump is swallowed rather than raised: the write already succeeded, and a missed
    invalidation is bounded by `TTL_SECONDS` — not a reason to fail an otherwise-successful
    request.
    """
    try:
        await get_redis().incr(_GENERATION_KEY)
    except Exception:
        log.warning("availability cache: generation bump failed", exc_info=True)


async def cached(namespace: str, parts: dict, compute: Callable[[], Awaitable[dict]]) -> dict:
    """`compute()`'s JSON-able result, served from cache when the generation's copy exists.

    On a miss (or any Redis trouble), `compute()` is awaited directly and its result is what
    is returned — Redis is purely a shortcut in front of it, never a dependency it needs to
    answer. If `compute()` raises (the two callers use this for "service turned out not to be
    bookable" and similar), nothing is cached and the exception is the caller's to handle.
    """
    redis = get_redis()
    generation: int | None = None
    try:
        raw = await redis.get(_GENERATION_KEY)
        generation = int(raw) if raw else 0
    except Exception:
        log.warning("availability cache: unreachable, computing uncached", exc_info=True)

    key = _key(namespace, generation, parts) if generation is not None else None

    if key is not None:
        try:
            hit = await redis.get(key)
        except Exception:
            hit = None
            log.warning("availability cache: read failed, computing uncached", exc_info=True)
        if hit is not None:
            return json.loads(hit)

    result = await compute()

    if key is not None:
        try:
            await redis.set(key, json.dumps(result), ex=TTL_SECONDS)
        except Exception:
            log.warning("availability cache: write failed", exc_info=True)

    return result


def _key(namespace: str, generation: int, parts: dict) -> str:
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()
    return f"avail:{namespace}:{generation}:{digest}"

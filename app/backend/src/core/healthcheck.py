"""Dead-man ping after a nightly maintenance job succeeds (tech-stack §3, #85).

`core.tasks.maintain_partitions` and `customers.tasks.purge_expired` each call
`ping_maintenance()` once they have finished. `HEALTHCHECK_URL_MAINTENANCE` unset means no
request at all. A failed ping (network error, non-2xx) is logged and swallowed — the job
already succeeded; a monitoring outage must not turn that into a task failure.

`httpx.AsyncClient` is called at the module level rather than injected, since the tasks that
use this run under Celery and take no client parameter. A test stubs it at the transport by
monkeypatching `httpx.AsyncClient` itself (the same `httpx.MockTransport` shape
`core/security.py`'s HaveIBeenPwned client and `notifications/providers.py` already use).
"""

import logging

import httpx

from core.config import get_settings

log = logging.getLogger(__name__)

_TIMEOUT = 5.0


async def ping_maintenance() -> None:
    url = get_settings().healthcheck_url_maintenance
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError:
        log.warning("healthcheck: maintenance ping to %s failed", url)

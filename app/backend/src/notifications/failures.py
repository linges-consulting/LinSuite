"""Write side of `notification_failures` (Task 4, #11) — called from a Celery task's own
exception handler (`notifications/tasks.py`), never from a request handler.

Only a `PermanentDeliveryError` (`notifications/providers.py`) ever reaches here. A transient
failure (network error, a 5xx) keeps retrying under the task's existing `autoretry_for`
backoff and is never terminal within this task's own scope.

# ponytail: a transient failure that goes on to exhaust all 5 retries also never reaches this
# module — only an *immediate* `PermanentDeliveryError` does. #11's stated acceptance
# criterion is "a permanent failure stops retrying and appears on the client profile"; it does
# not ask for the exhausted-retry case too. Add a Celery `on_failure` hook on the task if that
# gap starts to matter.

`customer_id=None` or `notification_type=None` — a send with no customer behind it (password
reset, MFA, a staff notification) — writes nothing: there is no profile to surface it on, and
the column is `NOT NULL`.
"""

import asyncio
import uuid
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from notifications.models import NotificationFailure

_REASON_MAX = 2000  # `reason` is diagnostic text, not a compliance record — trimmed, not typed.


def _run(work: Coroutine[Any, Any, None]) -> None:
    """`asyncio.run`, from a worker (no loop running) or from an eager call made inside a
    request/test that already has one of its own (`customers/tasks.py::_run`'s same
    reasoning, copied rather than imported — this module has no other reason to depend on
    `customers`)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(work)
    else:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(asyncio.run, work).result()


def record_permanent_failure(
    customer_id: str | None,
    channel: str,
    notification_type: str | None,
    recipient: str,
    error: Exception,
) -> None:
    """Sync, since the Celery task calling it is. Skips writing when there is no customer or
    notification type to attach the row to — see the module docstring."""
    if customer_id is None or notification_type is None:
        return
    _run(_write(customer_id, channel, notification_type, recipient, str(error)))


async def _write(
    customer_id: str, channel: str, notification_type: str, recipient: str, reason: str
) -> None:
    # A fresh engine on this call's own event loop — the same shape `forms/tasks.py` uses,
    # since a worker call has no loop of its own and reusing a pooled connection from a
    # previous one is unusable here.
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db, db.begin():
            db.add(
                NotificationFailure(
                    customer_id=uuid.UUID(customer_id),
                    channel=channel,
                    notification_type=notification_type,
                    recipient=recipient,
                    reason=reason[:_REASON_MAX],
                )
            )
    finally:
        await engine.dispose()

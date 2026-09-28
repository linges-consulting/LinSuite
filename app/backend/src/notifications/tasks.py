"""Delivery, off the request path (CLAUDE.md: Celery workers, never inline).

A request handler enqueues and returns. It never waits on a provider, so a slow or dead mail
API cannot turn a password-reset request into a 30-second page — and the retry with backoff
that PRD §4 asks for belongs to the queue, not to a handler holding a connection open.

The provider is resolved *here*, inside the worker, rather than passed in: a Celery argument
has to be JSON, and an object that opens a connection is not. Same reason `_load_business`
below fetches the one `Business` row itself instead of taking it as an argument (Task 7, #11):
single-tenant means it's always `id=1` (CLAUDE.md), a cheap point lookup, and it's what lets a
*queued* send reach a tenant's actually-configured Resend/SMTP/Twilio sender — before this fix
every `.delay(...)` call resolved to `ConsoleProvider` regardless of what the settings panel
had configured, since `get_provider()`/`get_sms_provider()` were only ever called with no
business row here. `_load_business` uses its own `NullPool` engine, the same shape
`core/tasks.py::_maintain_partitions` already uses for a one-off task-run query — not the
request-scoped `SessionDep` (there is no request), and not the app-plus-privileged-role engine
pair the erasure tasks build (nothing here ever needs that other role). `core/db.py`'s
`run_task` runs it under the task's own event loop, or off a side thread when eager Celery
(the test suite) calls this from inside a request handler's already-running loop.

**Permanent vs transient (Task 4, #11).** `autoretry_for=(Exception,)` still retries anything
that escapes this function — a network failure, a provider's 5xx — with the same backoff as
before. A `PermanentDeliveryError` (`notifications/providers.py`) is caught here, inside the
task body, before Celery's autoretry wrapper ever sees it: that is what stops it from being
retried at all, since the decorator alone has no way to say "retry every `Exception` except
this subclass". `customer_id`/`notification_type` are optional keyword-only arguments —
existing callers (password reset, MFA, `forms/links.py`, staff notifications) are unaffected;
Task 5's trigger functions pass a customer through.
"""

import base64

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from core.celery_app import celery_app
from core.config import get_settings
from core.db import run_task
from core.models import Business
from notifications.failures import record_permanent_failure
from notifications.providers import (
    EmailAttachment,
    PermanentDeliveryError,
    get_provider,
    get_sms_provider,
)


async def _fetch_business() -> Business | None:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return await session.get(Business, 1)
    finally:
        await engine.dispose()


def _load_business() -> Business | None:
    """The one `businesses` row, or `None` before the setup wizard has ever run — `get_provider`/
    `get_sms_provider` both already treat a missing business as "fall back to console", the same
    as an unconfigured one, so a task run before setup completes still degrades safely."""
    return run_task(_fetch_business)


@celery_app.task(
    name="notifications.send_email",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def send_email(
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    *,
    customer_id: str | None = None,
    notification_type: str | None = None,
    attachments: list[dict[str, str]] | None = None,
) -> None:
    """`attachments` (#70) is JSON-safe, the same reason `business` never crosses the Celery
    boundary directly (module docstring): each entry is `{"filename", "content_b64",
    "content_type"}`, decoded into `EmailAttachment`s here — the one place bytes re-enter the
    picture. Optional and keyword-only, so every pre-#70 `.delay(...)` call (password reset,
    MFA, `notifications/triggers.py::dispatch`) is unaffected."""
    parsed = [
        EmailAttachment(
            filename=a["filename"],
            content=base64.b64decode(a["content_b64"]),
            content_type=a.get("content_type", "application/pdf"),
        )
        for a in (attachments or [])
    ]
    try:
        get_provider(_load_business()).send_email(
            to=to, subject=subject, text=text, html=html, attachments=parsed or None
        )
    except PermanentDeliveryError as error:
        record_permanent_failure(customer_id, "email", notification_type, to, error)


@celery_app.task(
    name="notifications.send_sms",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def send_sms(
    to: str,
    text: str,
    *,
    customer_id: str | None = None,
    notification_type: str | None = None,
) -> None:
    try:
        get_sms_provider(_load_business()).send_sms(to=to, text=text)
    except PermanentDeliveryError as error:
        record_permanent_failure(customer_id, "sms", notification_type, to, error)

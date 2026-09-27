"""Delivery, off the request path (CLAUDE.md: Celery workers, never inline).

A request handler enqueues and returns. It never waits on a provider, so a slow or dead mail
API cannot turn a password-reset request into a 30-second page — and the retry with backoff
that PRD §4 asks for belongs to the queue, not to a handler holding a connection open.

The provider is resolved *here*, inside the worker, rather than passed in: a Celery argument
has to be JSON, and an object that opens a connection is not.

**Permanent vs transient (Task 4, #11).** `autoretry_for=(Exception,)` still retries anything
that escapes this function — a network failure, a provider's 5xx — with the same backoff as
before. A `PermanentDeliveryError` (`notifications/providers.py`) is caught here, inside the
task body, before Celery's autoretry wrapper ever sees it: that is what stops it from being
retried at all, since the decorator alone has no way to say "retry every `Exception` except
this subclass". `customer_id`/`notification_type` are new, optional keyword-only arguments —
existing callers (password reset, MFA, `forms/links.py`, staff notifications) are unaffected;
Task 5's trigger functions are what will pass a customer through, once they exist.
"""

from core.celery_app import celery_app
from notifications.failures import record_permanent_failure
from notifications.providers import PermanentDeliveryError, get_provider


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
) -> None:
    try:
        get_provider().send_email(to=to, subject=subject, text=text, html=html)
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
        get_provider().send_sms(to=to, text=text)
    except PermanentDeliveryError as error:
        record_permanent_failure(customer_id, "sms", notification_type, to, error)

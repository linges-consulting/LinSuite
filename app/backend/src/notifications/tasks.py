"""Delivery, off the request path (CLAUDE.md: Celery workers, never inline).

A request handler enqueues and returns. It never waits on a provider, so a slow or dead mail
API cannot turn a password-reset request into a 30-second page — and the retry with backoff
that PRD §4 asks for belongs to the queue, not to a handler holding a connection open.

The provider is resolved *here*, inside the worker, rather than passed in: a Celery argument
has to be JSON, and an object that opens a connection is not.
"""

from core.celery_app import celery_app
from notifications.providers import get_provider


@celery_app.task(
    name="notifications.send_email",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def send_email(to: str, subject: str, text: str, html: str | None = None) -> None:
    get_provider().send_email(to=to, subject=subject, text=text, html=html)

"""Seam S3: the one way anything in this app sends a message.

Every later feature that notifies somebody — booking confirmations, reminders, secure form
links, package balances (PRD §4) — goes through `NotificationProvider`. Nothing grows its
own SMTP call, and nothing that wants to test "would this have been sent?" has to patch an
HTTP library: it registers an implementation of this protocol and reads what came out.

`console` is the only provider until Task 12 adds Resend and SMTP behind the same protocol.
It writes the whole message, links included, through the JSON logger — which is how a
developer retrieves a reset link locally (`docker compose logs app`), the same retrieval
path the first-run setup token already uses.

SMS is deliberately absent. tech-stack §6 treats it as an adapter that stays unimplemented
until a tenant asks and supplies credentials; adding a `send_sms` nobody implements would
put a method on every provider that can only raise.
"""

import logging
from collections.abc import Callable
from typing import Protocol

from core.config import get_settings

log = logging.getLogger(__name__)


class NotificationProvider(Protocol):
    def send_email(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        """Deliver, or raise. Retries are the caller's business (`notifications/tasks.py`)."""


class ConsoleProvider:
    def send_email(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        # The body is logged in full and on purpose: an unconfigured deployment still has to
        # be able to complete a password reset, and the link is the only way to do that.
        log.info("notifications: email to %s — %s\n%s", to, subject, text)


# Name -> factory. Task 12 adds "resend" and "smtp" here and nowhere else.
PROVIDERS: dict[str, Callable[[], NotificationProvider]] = {"console": ConsoleProvider}


def get_provider() -> NotificationProvider:
    name = get_settings().notification_provider
    factory = PROVIDERS.get(name)
    if factory is None:
        raise RuntimeError(f"NOTIFICATION_PROVIDER={name!r} is not one of {sorted(PROVIDERS)}.")
    return factory()

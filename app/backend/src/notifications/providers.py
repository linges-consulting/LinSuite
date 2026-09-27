"""Seam S3: the one way anything in this app sends a message.

Every later feature that notifies somebody — booking confirmations, reminders, secure form
links, package balances (PRD §4) — goes through `NotificationProvider`. Nothing grows its
own SMTP call, and nothing that wants to test "would this have been sent?" has to patch an
HTTP library: it registers an implementation of this protocol and reads what came out.

`console` writes the whole message, links included, through the JSON logger — which is how a
developer retrieves a reset link locally — with `docker compose logs worker`, because delivery
runs in the worker and never in the API process. `ResendProvider`/`SmtpProvider` (Task 2, #11)
are the two real senders: Resend called directly over `httpx` (bearer token, no SDK — one
endpoint doesn't justify a whole dependency when `httpx` is already here) and SMTP over
stdlib `smtplib`/`email.message.EmailMessage` (no new dependency).

**Selection changed shape in Task 2.** `NOTIFICATION_PROVIDER` used to be the only knob;
it now stays for the `console`/`recording` dev/test override *only* — picked unconditionally,
so the test suite never makes a real HTTP or SMTP call no matter what a business row holds.
Anything else reads the tenant's own configured sender off `business.email_sender` instead:
email is tenant-owned config now (Resend/SMTP credentials live on `businesses`), not a
deployment-wide env var. `get_provider()` takes the `Business` row for this reason; the
existing callers that don't pass one (password reset, MFA, ...) are unaffected as long as
`NOTIFICATION_PROVIDER` stays at its shipped default of `console` — degrading to `console`
is also what happens for a business that hasn't configured a sender yet, so an unconfigured
deployment can still complete a password reset. Task 5's trigger functions are what fetch the
business row and pass it through; wiring that up is their job, not this module's.

SMS is deliberately absent. tech-stack §6 treats it as an adapter that stays unimplemented
until a tenant asks and supplies credentials; adding a `send_sms` nobody implements would
put a method on every provider that can only raise.
"""

import logging
import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from typing import TYPE_CHECKING, Protocol

import httpx

from core.config import get_settings
from notifications.credentials import decrypt_credential

if TYPE_CHECKING:
    from core.models import Business

log = logging.getLogger(__name__)

_RESEND_ENDPOINT = "https://api.resend.com/emails"
_HTTP_TIMEOUT = 10.0
_SMTP_TIMEOUT = 10
_DEFAULT_SMTP_PORT = 587

# The dev/test override values of `NOTIFICATION_PROVIDER` — picked unconditionally, regardless
# of anything a business row holds. Everything else reads `business.email_sender`.
_OVERRIDE_PROVIDERS = frozenset({"console", "recording"})


class NotificationProvider(Protocol):
    def send_email(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        """Deliver, or raise. Retries are the caller's business (`notifications/tasks.py`)."""


class ConsoleProvider:
    def send_email(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        # The body is logged in full and on purpose: an unconfigured deployment still has to
        # be able to complete a password reset, and the link is the only way to do that.
        log.info("notifications: email to %s — %s\n%s", to, subject, text)


class ResendProvider:
    """Resend's REST API directly over `httpx` (owner decision, m3.md) — one endpoint, a
    bearer-token POST, no reason to add the SDK.

    `client` is for tests only, the same injectable-transport shape `core/security.py` uses
    for the HaveIBeenPwned client: a real send opens its own short-lived `httpx.Client`, and a
    test hands in one built on `httpx.MockTransport` so no real network call ever happens.
    """

    def __init__(
        self, api_key: str, from_address: str, *, client: httpx.Client | None = None
    ) -> None:
        self._api_key = api_key
        self._from_address = from_address
        self._client = client

    def send_email(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        payload: dict[str, object] = {
            "from": self._from_address,
            "to": [to],
            "subject": subject,
            "text": text,
        }
        if html is not None:
            payload["html"] = html
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if self._client is not None:
            response = self._client.post(_RESEND_ENDPOINT, json=payload, headers=headers)
        else:
            with httpx.Client(timeout=_HTTP_TIMEOUT) as owned:
                response = owned.post(_RESEND_ENDPOINT, json=payload, headers=headers)
        response.raise_for_status()


class SmtpProvider:
    """Stdlib `smtplib` + `email.message.EmailMessage` — no new dependency (owner decision).

    `smtp_cls` is for tests only: a fake standing in for `smtplib.SMTP`, the same injection
    shape `ResendProvider` uses for `httpx.Client`.
    """

    def __init__(
        self,
        host: str,
        port: int | None,
        username: str | None,
        password: str | None,
        from_address: str,
        *,
        smtp_cls: "type[smtplib.SMTP] | None" = None,
    ) -> None:
        self._host = host
        self._port = port or _DEFAULT_SMTP_PORT
        self._username = username
        self._password = password
        self._from_address = from_address
        self._smtp_cls = smtp_cls

    def send_email(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self._from_address
        message["To"] = to
        message.set_content(text)
        if html is not None:
            message.add_alternative(html, subtype="html")

        smtp_cls = self._smtp_cls or smtplib.SMTP
        with smtp_cls(self._host, self._port, timeout=_SMTP_TIMEOUT) as smtp:
            smtp.starttls()
            if self._username:
                smtp.login(self._username, self._password)
            smtp.send_message(message)


# Name -> factory. `console` (and `recording`, registered by `tests/fake_notifications.py`)
# take no arguments and are selected by `NOTIFICATION_PROVIDER`; `resend`/`smtp` take the
# tenant's own credentials and are only ever constructed by `_tenant_provider` below, off
# `business.email_sender` — never directly by the env var (see the module docstring).
PROVIDERS: dict[str, Callable[..., NotificationProvider]] = {
    "console": ConsoleProvider,
    "resend": ResendProvider,
    "smtp": SmtpProvider,
}


def email_ready(business: "Business") -> bool:
    """True once a real send should be attempted for this business, versus a no-op that
    leaves email behind Task 6's banner (#11: "email features remain disabled ... until a
    test send succeeds"). The test send itself, and the trigger-level gating that calls this,
    are Task 6's and Task 5's — this is only the pure check both of them need."""
    if business.email_sender == "resend":
        return bool(
            business.resend_api_key_encrypted
            and business.resend_from_address
            and business.resend_domain_verified_at
        )
    if business.email_sender == "smtp":
        return bool(business.smtp_host and business.smtp_from_address and business.smtp_verified_at)
    return False


def _tenant_provider(business: "Business") -> NotificationProvider:
    if business.email_sender == "resend":
        return PROVIDERS["resend"](
            api_key=decrypt_credential(business.resend_api_key_encrypted or ""),
            from_address=business.resend_from_address,
        )
    if business.email_sender == "smtp":
        return PROVIDERS["smtp"](
            host=business.smtp_host,
            port=business.smtp_port,
            username=business.smtp_username,
            password=(
                decrypt_credential(business.smtp_password_encrypted)
                if business.smtp_password_encrypted
                else None
            ),
            from_address=business.smtp_from_address,
        )
    raise RuntimeError(f"businesses.email_sender={business.email_sender!r} is not configured.")


def get_provider(business: "Business | None" = None) -> NotificationProvider:
    """See the module docstring for why `business` exists and how selection works now."""
    name = get_settings().notification_provider
    if name in _OVERRIDE_PROVIDERS:
        factory = PROVIDERS.get(name)
        if factory is None:
            raise RuntimeError(f"NOTIFICATION_PROVIDER={name!r} is not registered.")
        return factory()
    if business is None or not business.email_sender:
        return PROVIDERS["console"]()
    return _tenant_provider(business)

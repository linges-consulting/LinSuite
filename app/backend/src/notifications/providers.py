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

**Selection changed shape in Task 2.** `NOTIFICATION_PROVIDER=recording` is the *test* override
only, and wins unconditionally — the suite must never make a real HTTP/SMTP call no matter
what a business row holds. `console`, the shipped default, is **not** an unconditional
override (Task 6 fix, #11): `.env.example` always said to "leave this at console ... and
configure the sender from the app instead", but the first cut of `get_provider()` treated the
default the same as the test sentinel and returned `ConsoleProvider` even for a fully
configured, verified business — which meant no deployment could ever actually send through
Resend/SMTP without changing an env var the settings panel gives no reason to touch. Fixed by
narrowing `_OVERRIDE_PROVIDERS` to `{"recording"}`; `console` now only wins when there is no
business row or it has not chosen a sender, exactly the fallback `.env.example` describes and
`test_get_provider_degrades_to_console_when_unconfigured_and_not_overridden` already pinned
(that test sets a third, non-override value precisely to exercise this fallback, so it was
never actually testing the old unconditional-console branch — nothing had to change there).
Anything else reads the tenant's own configured sender off `business.email_sender` instead:
email is tenant-owned config now (Resend/SMTP credentials live on `businesses`), not a
deployment-wide env var. `get_provider()` takes the `Business` row for this reason; the
existing callers that don't pass one (password reset, MFA, ...) are unaffected — with no
business row, the fallback is still `console`, so an unconfigured deployment can still complete
a password reset. **Task 7 closes the queued-send gap this docstring used to note here**:
`notifications/tasks.py::send_email`/`send_sms` now fetch the one `Business` row themselves
(a Celery argument still has to be JSON, so the row is looked up inside the task body, not
passed through `.delay(...)`) and pass it to `get_provider(business)`/`get_sms_provider(business)`
— a queued send now reaches a tenant's configured Resend/SMTP/Twilio sender exactly like the
settings panel's synchronous test-send action already did.

`TwilioProvider` (Task 3, #11) is SMS's real sender: Twilio's REST API directly over `httpx`
(Basic Auth with the account SID/auth token), the same no-SDK shape as `ResendProvider`.
`ConsoleProvider`/`RecordingProvider` grow a working `send_sms` alongside it — the same "no
method that only raises" objection that used to keep SMS off the Protocol now cuts the other
way, since a real sender exists. `ResendProvider`/`SmtpProvider` stay email-only: they are
adapters for a specific email transport, not general-purpose senders, so giving them a
`send_sms` would mean either delegating to Twilio credentials that have nothing to do with an
email account (mixing two unrelated tenant configs into one object) or a permanent stub — the
same bad shape this docstring used to warn about, just moved. SMS provider selection
(`business.sms_enabled` + Twilio credentials, mirroring `_tenant_provider`) is Task 4/5's job;
this module only adds the adapter and the pure `sms_ready(business)` check they call first.

**`PermanentDeliveryError` (Task 4, #11)** is how a provider tells `notifications/tasks.py`
that retrying is pointless: a 4xx from Resend/Twilio (bad address/number, revoked credentials)
or an SMTP auth/recipient-refused error, versus a plain `Exception` for anything worth another
try (network failure, a 5xx). `is_permanent_status`/`is_permanent_smtp_error` are the pure
classifiers underneath — S2-testable on their own, no `httpx`/`smtplib` involved.
"""

import base64
import logging
import smtplib
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import TYPE_CHECKING, Protocol

import httpx

from core.config import get_settings
from notifications.credentials import decrypt_credential

if TYPE_CHECKING:
    from core.models import Business

log = logging.getLogger(__name__)

_RESEND_ENDPOINT = "https://api.resend.com/emails"
_TWILIO_ENDPOINT = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
_HTTP_TIMEOUT = 10.0
_SMTP_TIMEOUT = 10
_DEFAULT_SMTP_PORT = 587

# The dev/test override value of `NOTIFICATION_PROVIDER` — picked unconditionally, regardless
# of anything a business row holds. `console` is deliberately *not* in this set (module
# docstring, Task 6 fix): it is the shipped default and the no-business/no-sender fallback,
# not a permanent override.
_OVERRIDE_PROVIDERS = frozenset({"recording"})


class PermanentDeliveryError(Exception):
    """A delivery attempt failed for a reason retrying will not fix. Raised in place of the
    provider's own exception (chained via `from`); `notifications/tasks.py` catches this
    distinctly from a plain `Exception` and writes a `notification_failures` row instead of
    retrying."""


def is_permanent_status(status_code: int) -> bool:
    """A 4xx is the caller's own fault (bad number/address, bad/expired auth) — retrying the
    identical request changes nothing. A 5xx is the provider's, and worth another try. Pure,
    so it is tested on its own (S2), with no `httpx` request involved."""
    return 400 <= status_code < 500


# SMTP replies with its own 4xx (temporary)/5xx (permanent) reply codes, opposite of HTTP's
# convention — but these three `smtplib` exceptions are already unambiguous on their own:
# a login rejected, or every recipient/the sender refused, is never fixed by retrying the same
# message. A dropped connection or a timeout is not one of these and stays a plain `Exception`.
_PERMANENT_SMTP_ERRORS = (
    smtplib.SMTPAuthenticationError,
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPSenderRefused,
)


def is_permanent_smtp_error(error: Exception) -> bool:
    """Pure — S2-testable without ever opening a socket."""
    return isinstance(error, _PERMANENT_SMTP_ERRORS)


def _raise_for_delivery(response: httpx.Response) -> None:
    """Shared by `ResendProvider`/`TwilioProvider`: both are a bare `httpx` POST with no
    payload-specific error handling of their own."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        if is_permanent_status(response.status_code):
            raise PermanentDeliveryError(str(error)) from error
        raise


@dataclass(frozen=True)
class EmailAttachment:
    """One file to attach to an email (#70: invoice/treatment-receipt PDFs). Bytes in memory —
    small documents (a handful of pages at most), never a stream: the same "one shot, not
    chunked" shape every provider adapter already sends its `text`/`html` body as."""

    filename: str
    content: bytes
    content_type: str = "application/pdf"


class NotificationProvider(Protocol):
    def send_email(
        self,
        to: str,
        subject: str,
        text: str,
        html: str | None = None,
        attachments: list[EmailAttachment] | None = None,
    ) -> None:
        """Deliver, or raise. Retries are the caller's business (`notifications/tasks.py`).
        `attachments` is optional and keyword-compatible with every pre-#70 caller (password
        reset, MFA codes, the trigger dispatch table) — none of them pass it, and it defaults
        to no attachment."""

    def send_sms(self, to: str, text: str) -> None:
        """Deliver, or raise. Retries are the caller's business (`notifications/tasks.py`)."""


class ConsoleProvider:
    def send_email(
        self,
        to: str,
        subject: str,
        text: str,
        html: str | None = None,
        attachments: list[EmailAttachment] | None = None,
    ) -> None:
        # The body is logged in full and on purpose: an unconfigured deployment still has to
        # be able to complete a password reset, and the link is the only way to do that.
        log.info("notifications: email to %s — %s\n%s", to, subject, text)
        if attachments:
            # Names only, never the bytes — unlike the plain-text body above, an attachment is
            # routinely a client-linked financial document (#70), not a reset link.
            log.info(
                "notifications: %d attachment(s): %s",
                len(attachments),
                ", ".join(a.filename for a in attachments),
            )

    def send_sms(self, to: str, text: str) -> None:
        log.info("notifications: sms to %s — %s", to, text)


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

    def send_email(
        self,
        to: str,
        subject: str,
        text: str,
        html: str | None = None,
        attachments: list[EmailAttachment] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "from": self._from_address,
            "to": [to],
            "subject": subject,
            "text": text,
        }
        if html is not None:
            payload["html"] = html
        if attachments:
            # Resend's own attachment shape: base64 content, not raw bytes — the payload is
            # JSON, the same reason `notifications/tasks.py::send_email` base64-encodes an
            # attachment before it can even reach this call.
            payload["attachments"] = [
                {"filename": a.filename, "content": base64.b64encode(a.content).decode()}
                for a in attachments
            ]
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if self._client is not None:
            response = self._client.post(_RESEND_ENDPOINT, json=payload, headers=headers)
        else:
            with httpx.Client(timeout=_HTTP_TIMEOUT) as owned:
                response = owned.post(_RESEND_ENDPOINT, json=payload, headers=headers)
        _raise_for_delivery(response)


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

    def send_email(
        self,
        to: str,
        subject: str,
        text: str,
        html: str | None = None,
        attachments: list[EmailAttachment] | None = None,
    ) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self._from_address
        message["To"] = to
        message.set_content(text)
        if html is not None:
            message.add_alternative(html, subtype="html")
        for attachment in attachments or []:
            maintype, _, subtype = attachment.content_type.partition("/")
            message.add_attachment(
                attachment.content,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=attachment.filename,
            )

        smtp_cls = self._smtp_cls or smtplib.SMTP
        try:
            with smtp_cls(self._host, self._port, timeout=_SMTP_TIMEOUT) as smtp:
                smtp.starttls()
                if self._username:
                    smtp.login(self._username, self._password)
                smtp.send_message(message)
        except Exception as error:
            if is_permanent_smtp_error(error):
                raise PermanentDeliveryError(str(error)) from error
            raise


class TwilioProvider:
    """Twilio's REST API directly over `httpx` (owner decision, m3.md) — one endpoint, HTTP
    Basic Auth with the account SID as username and the auth token as password, no SDK, the
    same no-SDK precedent `ResendProvider` set in Task 2.

    `client` is for tests only, the same injectable-transport shape `ResendProvider` uses.
    """

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from_number = from_number
        self._client = client

    def send_sms(self, to: str, text: str) -> None:
        url = _TWILIO_ENDPOINT.format(sid=self._account_sid)
        data = {"From": self._from_number, "To": to, "Body": text}
        auth = (self._account_sid, self._auth_token)
        if self._client is not None:
            response = self._client.post(url, data=data, auth=auth)
        else:
            with httpx.Client(timeout=_HTTP_TIMEOUT) as owned:
                response = owned.post(url, data=data, auth=auth)
        _raise_for_delivery(response)


# Name -> factory. `console` (and `recording`, registered by `tests/fake_notifications.py`)
# take no arguments and are selected by `NOTIFICATION_PROVIDER`; `resend`/`smtp` take the
# tenant's own credentials and are only ever constructed by `_tenant_provider` below, off
# `business.email_sender` — never directly by the env var (see the module docstring).
PROVIDERS: dict[str, Callable[..., NotificationProvider]] = {
    "console": ConsoleProvider,
    "resend": ResendProvider,
    "smtp": SmtpProvider,
    "twilio": TwilioProvider,
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


def sms_ready(business: "Business") -> bool:
    """True once a real SMS send should be attempted for this business — mirrors
    `email_ready`. Task 5's trigger layer calls this (and `business.sms_enabled`) before ever
    constructing an SMS send; "disabling SMS leaves no code path attempting to send it" (#11
    acceptance criterion) is that call site's job, not this pure check's."""
    return bool(
        business.sms_enabled
        and business.twilio_account_sid
        and business.twilio_auth_token_encrypted
        and business.twilio_from_number
    )


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


def get_sms_provider(business: "Business | None" = None) -> NotificationProvider:
    """`get_provider`'s counterpart for SMS (Task 6, #11's settings-panel test-send action —
    the first real caller that passes a business through to an SMS send; Task 7 makes
    `notifications/tasks.py::send_sms` the second, with no `sms_ready` gate of its own to
    re-check). There is no `business.sms_sender` to switch on the way `email_sender` does:
    SMS has exactly one real adapter, so `sms_enabled` plus the three `twilio_*` columns are
    the whole configuration. `NOTIFICATION_PROVIDER=recording` still wins unconditionally,
    same as `get_provider` — the suite must never dial Twilio for real either. `business` is
    optional and, like `get_provider`, degrades to `console` when it is missing or not
    `sms_ready` — `dispatch()` already checked readiness before enqueueing, but a queued send
    can run after the config changed, and `console` is the same safe fallback `get_provider`
    uses rather than handing Twilio empty credentials."""
    if get_settings().notification_provider in _OVERRIDE_PROVIDERS:
        return PROVIDERS["recording"]()
    if business is None or not sms_ready(business):
        return PROVIDERS["console"]()
    return PROVIDERS["twilio"](
        account_sid=business.twilio_account_sid,
        auth_token=decrypt_credential(business.twilio_auth_token_encrypted or ""),
        from_number=business.twilio_from_number,
    )

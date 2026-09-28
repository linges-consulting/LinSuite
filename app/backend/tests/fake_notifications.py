"""Seam S3: a recording implementation of `NotificationProvider`.

The seam is the provider protocol, so this is a real implementation of it — registered under
its own name and selected by `NOTIFICATION_PROVIDER`, exactly as `console` is. Nothing here
mocks an HTTP library, because nothing in the sending path knows what HTTP is.

**Simulating a delivery failure (Task 4, #11).** A test arranges one by pushing an exception
onto `fail_email_next`/`fail_sms_next` before calling the task under test; the next send pops
it and raises instead of recording, then behaves normally again — exactly one simulated
failure per push, so a test of "retries, then succeeds" needs no separate reset. Push a
`PermanentDeliveryError` (`notifications.providers`) to simulate the permanent path, or a
plain `Exception` for the transient one — the provider raises whatever it's given, unclassified,
the same as a real adapter raises whatever `httpx`/`smtplib` gave it.
"""

from dataclasses import dataclass, field

from notifications.providers import PROVIDERS, EmailAttachment


@dataclass(frozen=True)
class SentEmail:
    to: str
    subject: str
    text: str
    html: str | None
    attachments: tuple[EmailAttachment, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SentSms:
    to: str
    text: str


# Module-level because the provider is constructed by the sending path, not by the test — a
# Celery task resolves it from settings and the test only gets to read what came out.
sent: list[SentEmail] = []
sent_sms: list[SentSms] = []

# A test pushes an exception here to make the *next* send raise it instead of recording (see
# the module docstring). Popped once, so a second send in the same test behaves normally.
fail_email_next: list[Exception] = []
fail_sms_next: list[Exception] = []


class RecordingProvider:
    def send_email(
        self,
        to: str,
        subject: str,
        text: str,
        html: str | None = None,
        attachments: list[EmailAttachment] | None = None,
    ) -> None:
        if fail_email_next:
            raise fail_email_next.pop(0)
        sent.append(
            SentEmail(
                to=to, subject=subject, text=text, html=html, attachments=tuple(attachments or ())
            )
        )

    def send_sms(self, to: str, text: str) -> None:
        if fail_sms_next:
            raise fail_sms_next.pop(0)
        sent_sms.append(SentSms(to=to, text=text))


PROVIDERS["recording"] = RecordingProvider

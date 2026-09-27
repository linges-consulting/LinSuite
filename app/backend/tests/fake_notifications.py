"""Seam S3: a recording implementation of `NotificationProvider`.

The seam is the provider protocol, so this is a real implementation of it — registered under
its own name and selected by `NOTIFICATION_PROVIDER`, exactly as `console` is. Nothing here
mocks an HTTP library, because nothing in the sending path knows what HTTP is.
"""

from dataclasses import dataclass

from notifications.providers import PROVIDERS


@dataclass(frozen=True)
class SentEmail:
    to: str
    subject: str
    text: str
    html: str | None


@dataclass(frozen=True)
class SentSms:
    to: str
    text: str


# Module-level because the provider is constructed by the sending path, not by the test — a
# Celery task resolves it from settings and the test only gets to read what came out.
sent: list[SentEmail] = []
sent_sms: list[SentSms] = []


class RecordingProvider:
    def send_email(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        sent.append(SentEmail(to=to, subject=subject, text=text, html=html))

    def send_sms(self, to: str, text: str) -> None:
        sent_sms.append(SentSms(to=to, text=text))


PROVIDERS["recording"] = RecordingProvider

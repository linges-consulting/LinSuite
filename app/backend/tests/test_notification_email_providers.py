"""Unit tests for `ResendProvider`/`SmtpProvider`'s own request-building, and for
`get_provider`'s tenant selection and `email_ready` (Task 2, #11).

Per m3.md: mock `httpx`/`smtplib` only here, at the adapter's own boundary — a test of a
trigger path (not built yet) must go through `tests/fake_notifications.py`'s recording fake
instead, never this. `httpx.MockTransport` is httpx's own test transport (the same shape
`core/security.py`'s HaveIBeenPwned client already uses), not a patch of the library; the
fake SMTP class below is the same idea for `smtplib.SMTP`'s constructor/context-manager shape.
"""

import base64
import json
import smtplib
from datetime import UTC, datetime

import httpx
import pytest

from core.models import Business
from notifications.credentials import encrypt_credential
from notifications.providers import (
    ConsoleProvider,
    EmailAttachment,
    PermanentDeliveryError,
    ResendProvider,
    SmtpProvider,
    email_ready,
    get_provider,
    is_permanent_smtp_error,
    is_permanent_status,
)


def _business(**overrides) -> Business:
    return Business(id=1, name="Cedar Lane Clinic", timezone="America/Toronto", **overrides)


# --- ConsoleProvider --------------------------------------------------------------------------


def test_console_logs_attachment_filenames_but_never_the_bytes(caplog):
    # #70: an attachment is routinely a client-linked financial document, unlike the reset
    # link/MFA code `test_the_console_provider_writes_the_message_to_the_log`
    # (`test_password_reset.py`) already proves is logged in full on purpose.
    with caplog.at_level("INFO"):
        ConsoleProvider().send_email(
            "someone@cedar.example",
            "Invoice #1",
            "Your invoice is attached.",
            attachments=[EmailAttachment(filename="invoice-1.pdf", content=b"%PDF-1.7 secret")],
        )

    assert "invoice-1.pdf" in caplog.text
    assert "secret" not in caplog.text


def test_console_send_email_is_unaffected_when_no_attachments_are_given(caplog):
    # Every pre-#70 caller (password reset, MFA, the trigger dispatch table) never passes
    # `attachments` — the extension must be a pure addition, not a changed default behaviour.
    with caplog.at_level("INFO"):
        ConsoleProvider().send_email("someone@cedar.example", "Reset", "body")

    assert "attachment" not in caplog.text


# --- ResendProvider --------------------------------------------------------------------------


def test_resend_posts_a_bearer_authenticated_json_payload():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "abc"})

    provider = ResendProvider(
        api_key="re_test_key",
        from_address="hello@cedar.example",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    provider.send_email(to="client@example.com", subject="Hi", text="body", html="<p>body</p>")

    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == "https://api.resend.com/emails"
    assert request.headers["authorization"] == "Bearer re_test_key"
    assert json.loads(request.content) == {
        "from": "hello@cedar.example",
        "to": ["client@example.com"],
        "subject": "Hi",
        "text": "body",
        "html": "<p>body</p>",
    }


def test_resend_omits_html_when_none():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "abc"})

    provider = ResendProvider(
        api_key="k",
        from_address="a@b.example",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    provider.send_email(to="c@d.example", subject="s", text="t")

    assert "html" not in json.loads(seen[0].content)


def test_resend_base64_encodes_attachments_since_the_payload_is_json():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "abc"})

    provider = ResendProvider(
        api_key="k",
        from_address="a@b.example",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    provider.send_email(
        to="c@d.example",
        subject="s",
        text="t",
        attachments=[EmailAttachment(filename="invoice-1.pdf", content=b"%PDF-1.7 body")],
    )

    payload = json.loads(seen[0].content)
    assert payload["attachments"] == [
        {"filename": "invoice-1.pdf", "content": base64.b64encode(b"%PDF-1.7 body").decode()}
    ]


def test_resend_omits_attachments_when_none_are_given():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "abc"})

    provider = ResendProvider(
        api_key="k",
        from_address="a@b.example",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    provider.send_email(to="c@d.example", subject="s", text="t")

    assert "attachments" not in json.loads(seen[0].content)


def test_resend_raises_permanently_on_a_4xx_response():
    # Task 4, #11: a 4xx (a bad address, a rejected payload) is the caller's own fault —
    # retrying the identical request changes nothing, so this is no longer a bare
    # `httpx.HTTPStatusError` (this test's premise before Task 4) but the distinct error
    # `notifications/tasks.py` catches to stop retrying immediately.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "invalid from"})

    provider = ResendProvider(
        api_key="k",
        from_address="a@b.example",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(PermanentDeliveryError):
        provider.send_email(to="c@d.example", subject="s", text="t")


def test_resend_raises_transiently_on_a_5xx_response():
    # A 5xx is Resend's own fault, and worth another try — still a bare `HTTPStatusError`,
    # which `autoretry_for=(Exception,)` catches for a retry, unchanged from before Task 4.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"message": "upstream unavailable"})

    provider = ResendProvider(
        api_key="k",
        from_address="a@b.example",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(httpx.HTTPStatusError):
        provider.send_email(to="c@d.example", subject="s", text="t")


# --- SmtpProvider ------------------------------------------------------------------------


class _FakeSMTP:
    """Stands in for `smtplib.SMTP`'s constructor/context-manager/method shape."""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host: str, port: int, timeout: int | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_call: tuple[str, str] | None = None
        self.sent = None
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> "_FakeSMTP":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def starttls(self) -> None:
        self.started_tls = True

    def login(self, username: str, password: str | None) -> None:
        self.login_call = (username, password)

    def send_message(self, message) -> None:
        self.sent = message


@pytest.fixture(autouse=True)
def _clear_fake_smtp():
    _FakeSMTP.instances.clear()
    yield
    _FakeSMTP.instances.clear()


def test_smtp_sends_through_starttls_and_login():
    provider = SmtpProvider(
        host="smtp.example.com",
        port=587,
        username="user",
        password="pass",
        from_address="hello@cedar.example",
        smtp_cls=_FakeSMTP,
    )
    provider.send_email(to="client@example.com", subject="Hi", text="body")

    smtp = _FakeSMTP.instances[0]
    assert smtp.host == "smtp.example.com"
    assert smtp.port == 587
    assert smtp.started_tls
    assert smtp.login_call == ("user", "pass")
    assert smtp.sent["Subject"] == "Hi"
    assert smtp.sent["From"] == "hello@cedar.example"
    assert smtp.sent["To"] == "client@example.com"
    assert smtp.sent.get_content().strip() == "body"


def test_smtp_skips_login_without_a_username():
    provider = SmtpProvider(
        host="smtp.example.com",
        port=None,
        username=None,
        password=None,
        from_address="hello@cedar.example",
        smtp_cls=_FakeSMTP,
    )
    provider.send_email(to="client@example.com", subject="Hi", text="body")

    smtp = _FakeSMTP.instances[0]
    assert smtp.port == 587  # the default, since none was configured
    assert smtp.login_call is None


def test_smtp_raises_permanently_when_the_login_is_rejected():
    class _RejectsLogin(_FakeSMTP):
        def login(self, username: str, password: str | None) -> None:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    provider = SmtpProvider(
        host="h",
        port=25,
        username="user",
        password="wrong",
        from_address="a@b.example",
        smtp_cls=_RejectsLogin,
    )
    with pytest.raises(PermanentDeliveryError):
        provider.send_email(to="c@d.example", subject="s", text="t")


def test_smtp_raises_permanently_when_every_recipient_is_refused():
    class _RefusesRecipient(_FakeSMTP):
        def send_message(self, message) -> None:
            raise smtplib.SMTPRecipientsRefused({"c@d.example": (550, b"no such user")})

    provider = SmtpProvider(
        host="h",
        port=25,
        username=None,
        password=None,
        from_address="a@b.example",
        smtp_cls=_RefusesRecipient,
    )
    with pytest.raises(PermanentDeliveryError):
        provider.send_email(to="c@d.example", subject="s", text="t")


def test_smtp_raises_transiently_on_a_dropped_connection():
    class _Disconnects(_FakeSMTP):
        def send_message(self, message) -> None:
            raise smtplib.SMTPServerDisconnected("connection lost")

    provider = SmtpProvider(
        host="h",
        port=25,
        username=None,
        password=None,
        from_address="a@b.example",
        smtp_cls=_Disconnects,
    )
    with pytest.raises(smtplib.SMTPServerDisconnected):
        provider.send_email(to="c@d.example", subject="s", text="t")


def test_smtp_attaches_an_html_alternative_when_given():
    provider = SmtpProvider(
        host="h",
        port=25,
        username=None,
        password=None,
        from_address="a@b.example",
        smtp_cls=_FakeSMTP,
    )
    provider.send_email(to="c@d.example", subject="s", text="plain", html="<p>rich</p>")

    assert _FakeSMTP.instances[0].sent.is_multipart()


def test_smtp_attaches_a_pdf_with_its_filename_and_content_type():
    provider = SmtpProvider(
        host="h",
        port=25,
        username=None,
        password=None,
        from_address="a@b.example",
        smtp_cls=_FakeSMTP,
    )
    provider.send_email(
        to="c@d.example",
        subject="s",
        text="plain",
        attachments=[EmailAttachment(filename="receipt-1-1.pdf", content=b"%PDF-1.7 body")],
    )

    sent = _FakeSMTP.instances[0].sent
    assert sent.is_multipart()
    attachment = next(iter(sent.iter_attachments()))
    assert attachment.get_filename() == "receipt-1-1.pdf"
    assert attachment.get_content_type() == "application/pdf"
    assert attachment.get_payload(decode=True) == b"%PDF-1.7 body"


def test_smtp_send_email_is_unaffected_when_no_attachments_are_given():
    provider = SmtpProvider(
        host="h",
        port=25,
        username=None,
        password=None,
        from_address="a@b.example",
        smtp_cls=_FakeSMTP,
    )
    provider.send_email(to="c@d.example", subject="s", text="plain")

    assert not _FakeSMTP.instances[0].sent.is_multipart()


# --- get_provider: tenant selection --------------------------------------------------------


def test_get_provider_degrades_to_console_when_unconfigured_and_not_overridden(monkeypatch):
    # `database`'s harness always sets `NOTIFICATION_PROVIDER=recording`, so this stubs
    # `get_settings` directly rather than requesting that fixture — the point of this test is
    # the "no business config, no override" branch, not the test harness's own override.
    from core.config import Settings

    monkeypatch.setattr(
        "notifications.providers.get_settings",
        lambda: Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://x@localhost/x",
            database_url_migrate="postgresql+asyncpg://x@localhost/x",
            jwt_secret="0" * 64,
            mfa_encryption_key="11" * 32,
            document_master_key="22" * 32,
            notification_credential_key="33" * 32,
            notification_provider="unused-in-production",
        ),
    )
    assert isinstance(get_provider(_business(email_sender=None)), ConsoleProvider)
    assert isinstance(get_provider(None), ConsoleProvider)


def test_get_provider_builds_a_resend_provider_from_the_business_row(database, monkeypatch):
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "notification_provider", "unused-in-production")
    business = _business(
        email_sender="resend",
        resend_api_key_encrypted=encrypt_credential("re_live_key"),
        resend_from_address="hello@cedar.example",
    )
    provider = get_provider(business)

    assert isinstance(provider, ResendProvider)
    assert provider._api_key == "re_live_key"
    assert provider._from_address == "hello@cedar.example"


def test_get_provider_builds_an_smtp_provider_from_the_business_row(database, monkeypatch):
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "notification_provider", "unused-in-production")
    business = _business(
        email_sender="smtp",
        smtp_host="smtp.example.com",
        smtp_port=2525,
        smtp_username="user",
        smtp_password_encrypted=encrypt_credential("s3cret"),
        smtp_from_address="hello@cedar.example",
    )
    provider = get_provider(business)

    assert isinstance(provider, SmtpProvider)
    assert provider._host == "smtp.example.com"
    assert provider._port == 2525
    assert provider._password == "s3cret"


def test_get_provider_reaches_the_tenant_sender_even_at_the_shipped_console_default(
    database, monkeypatch
):
    """Task 6 fix, #11: `.env.example` says to "leave this at console ... and configure the
    sender from the app instead" — so a deployment that never touches `NOTIFICATION_PROVIDER`
    (still `console`, the shipped default) must still reach a configured, verified business's
    real sender. Before this fix `console` was in `_OVERRIDE_PROVIDERS` and won unconditionally,
    the same as `recording`; only the test suite's own `NOTIFICATION_PROVIDER=recording` masked
    that this ever regressed a live deployment."""
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "notification_provider", "console")
    business = _business(
        email_sender="resend",
        resend_api_key_encrypted=encrypt_credential("re_live_key"),
        resend_from_address="hello@cedar.example",
    )

    provider = get_provider(business)

    assert isinstance(provider, ResendProvider)
    assert provider._api_key == "re_live_key"


def test_get_provider_override_wins_even_over_a_configured_business(database):
    # `NOTIFICATION_PROVIDER=recording` (the test harness default) is picked unconditionally —
    # the suite must never make a real HTTP/SMTP call, whatever a business row holds.
    business = _business(
        email_sender="resend",
        resend_api_key_encrypted=encrypt_credential("re_live_key"),
        resend_from_address="hello@cedar.example",
    )
    from tests.fake_notifications import RecordingProvider

    assert isinstance(get_provider(business), RecordingProvider)


# --- email_ready -----------------------------------------------------------------------------


def test_email_ready_is_false_when_unconfigured():
    assert email_ready(_business(email_sender=None)) is False


def test_email_ready_for_resend_requires_the_verified_timestamp():
    verified = _business(
        email_sender="resend",
        resend_api_key_encrypted="sealed",
        resend_from_address="hello@cedar.example",
        resend_domain_verified_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert email_ready(verified) is True

    unverified = _business(
        email_sender="resend",
        resend_api_key_encrypted="sealed",
        resend_from_address="hello@cedar.example",
        resend_domain_verified_at=None,
    )
    assert email_ready(unverified) is False


def test_email_ready_for_smtp_requires_the_verified_timestamp():
    verified = _business(
        email_sender="smtp",
        smtp_host="smtp.example.com",
        smtp_from_address="hello@cedar.example",
        smtp_verified_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert email_ready(verified) is True

    unverified = _business(
        email_sender="smtp",
        smtp_host="smtp.example.com",
        smtp_from_address="hello@cedar.example",
        smtp_verified_at=None,
    )
    assert email_ready(unverified) is False


# --- is_permanent_status / is_permanent_smtp_error (S2, pure — Task 4, #11) -------------------


@pytest.mark.parametrize("status_code", [400, 401, 404, 422, 429, 499])
def test_is_permanent_status_is_true_for_every_4xx(status_code):
    assert is_permanent_status(status_code) is True


@pytest.mark.parametrize("status_code", [200, 301, 500, 502, 503])
def test_is_permanent_status_is_false_outside_4xx(status_code):
    assert is_permanent_status(status_code) is False


def test_is_permanent_smtp_error_is_true_for_auth_and_refusal_errors():
    assert is_permanent_smtp_error(smtplib.SMTPAuthenticationError(535, b"no")) is True
    assert is_permanent_smtp_error(smtplib.SMTPRecipientsRefused({})) is True
    assert is_permanent_smtp_error(smtplib.SMTPSenderRefused(550, b"no", "a@b.example")) is True


def test_is_permanent_smtp_error_is_false_for_a_dropped_connection():
    assert is_permanent_smtp_error(smtplib.SMTPServerDisconnected("gone")) is False
    assert is_permanent_smtp_error(TimeoutError("timed out")) is False

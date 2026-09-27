"""Unit tests for `TwilioProvider`'s own request-building, `sms_ready`, and the SMS methods
grown onto `ConsoleProvider`/`RecordingProvider` (Task 3, #11).

Per m3.md: mock `httpx` only here, at the adapter's own boundary — a test of a trigger path
(not built yet) must go through `tests/fake_notifications.py`'s recording fake instead, never
this. `httpx.MockTransport` is httpx's own test transport, the same shape
`test_notification_email_providers.py` already uses for `ResendProvider`.
"""

from urllib.parse import parse_qs

import httpx
import pytest

from core.models import Business
from notifications.providers import ConsoleProvider, TwilioProvider, sms_ready


def _business(**overrides) -> Business:
    return Business(id=1, name="Cedar Lane Clinic", timezone="America/Toronto", **overrides)


# --- TwilioProvider --------------------------------------------------------------------------


def test_twilio_posts_a_basic_authenticated_form_body():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"sid": "SM123"})

    provider = TwilioProvider(
        account_sid="ACtest",
        auth_token="secrettoken",
        from_number="+15551234567",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    provider.send_sms(to="+15559876543", text="Your appointment is confirmed")

    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == "https://api.twilio.com/2010-04-01/Accounts/ACtest/Messages.json"
    # Basic Auth: account SID as username, auth token as password.
    assert request.headers["authorization"].startswith("Basic ")
    body = parse_qs(request.content.decode())
    assert body == {
        "From": ["+15551234567"],
        "To": ["+15559876543"],
        "Body": ["Your appointment is confirmed"],
    }


def test_twilio_raises_on_an_error_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "invalid number"})

    provider = TwilioProvider(
        account_sid="ACtest",
        auth_token="secrettoken",
        from_number="+15551234567",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(httpx.HTTPStatusError):
        provider.send_sms(to="+15559876543", text="hi")


# --- ConsoleProvider / RecordingProvider send_sms --------------------------------------------


def test_console_provider_logs_the_sms(caplog):
    with caplog.at_level("INFO"):
        ConsoleProvider().send_sms(to="+15559876543", text="hi there")
    assert "+15559876543" in caplog.text
    assert "hi there" in caplog.text


def test_recording_provider_records_the_sms():
    from tests.fake_notifications import RecordingProvider, sent_sms

    sent_sms.clear()
    RecordingProvider().send_sms(to="+15559876543", text="hi there")
    try:
        assert len(sent_sms) == 1
        assert sent_sms[0].to == "+15559876543"
        assert sent_sms[0].text == "hi there"
    finally:
        sent_sms.clear()


# --- sms_ready ---------------------------------------------------------------------------------


def test_sms_ready_is_false_when_disabled():
    business = _business(
        sms_enabled=False,
        twilio_account_sid="ACtest",
        twilio_auth_token_encrypted="sealed",
        twilio_from_number="+15551234567",
    )
    assert sms_ready(business) is False


def test_sms_ready_is_false_when_enabled_but_not_fully_configured():
    business = _business(sms_enabled=True, twilio_account_sid="ACtest")
    assert sms_ready(business) is False


def test_sms_ready_is_true_when_enabled_and_fully_configured():
    business = _business(
        sms_enabled=True,
        twilio_account_sid="ACtest",
        twilio_auth_token_encrypted="sealed",
        twilio_from_number="+15551234567",
    )
    assert sms_ready(business) is True

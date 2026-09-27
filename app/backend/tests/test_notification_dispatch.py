"""S1: Celery SMS dispatch + failure surfacing (Phase 12 Task 4, #11).

`send_sms` (new) copies `send_email`'s `autoretry_for=(Exception,)`/backoff shape verbatim.
What this file proves is the part that shape alone doesn't give: a `PermanentDeliveryError`
from the provider (`notifications/providers.py`) is caught inside the task body — before
Celery's autoretry wrapper ever sees it, so it is never retried — and written to
`notification_failures`, which then shows up on `GET /api/customers/{id}` (the client
profile). A plain transient `Exception` is left alone; the retry/backoff path itself is
`send_email`'s existing, unchanged behaviour and isn't re-proven here.

Both tasks are called directly (never through an endpoint — Task 5's trigger functions, which
would call them for real, don't exist yet); `tests.fake_notifications.RecordingProvider`'s
`fail_email_next`/`fail_sms_next` queues (extended for this task) are what make the *next*
send raise instead of recording.
"""

from notifications import tasks as notification_tasks
from notifications.providers import PermanentDeliveryError
from notifications.tasks import send_email, send_sms
from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    CUSTOMERS,
    as_admin,
    claimed_instance,
    make_customer,
)
from tests.test_form_links import make_email_ready
from tests.test_notification_triggers import make_sms_ready

PROFILE = CUSTOMERS  # GET /api/customers/{id}


async def test_a_permanent_email_failure_is_recorded_and_surfaced_on_the_profile(
    client, fail_next_send
):
    await as_admin(client)
    customer_id = await make_customer(client)
    fail_email, _ = fail_next_send
    fail_email.append(PermanentDeliveryError("550 5.1.1 no such address"))

    # No exception escapes: caught inside the task, not left for autoretry to chew on.
    send_email.delay(
        "client@example.com",
        "Your appointment is confirmed",
        "body",
        customer_id=customer_id,
        notification_type="booking_confirmation",
    )

    resp = await client.get(f"{PROFILE}/{customer_id}")
    assert resp.status_code == 200, resp.text
    failures = resp.json()["notification_failures"]
    assert len(failures) == 1
    assert failures[0]["channel"] == "email"
    assert failures[0]["notification_type"] == "booking_confirmation"
    assert failures[0]["recipient"] == "client@example.com"
    assert "no such address" in failures[0]["reason"]


async def test_a_permanent_sms_failure_is_recorded_and_surfaced_on_the_profile(
    client, fail_next_send
):
    await as_admin(client)
    customer_id = await make_customer(client)
    _, fail_sms = fail_next_send
    fail_sms.append(PermanentDeliveryError("21211 invalid 'To' phone number"))

    send_sms.delay(
        "+15559876543", "Reminder", customer_id=customer_id, notification_type="reminder"
    )

    resp = await client.get(f"{PROFILE}/{customer_id}")
    assert resp.status_code == 200, resp.text
    failures = resp.json()["notification_failures"]
    assert len(failures) == 1
    assert failures[0]["channel"] == "sms"
    assert failures[0]["notification_type"] == "reminder"
    assert failures[0]["recipient"] == "+15559876543"


async def test_a_failure_with_no_customer_writes_no_row(client, fail_next_send):
    # Every existing caller (password reset, MFA, forms/links.py, staff notifications) sends
    # with no `customer_id` — there is nothing here for a permanent failure to attach to.
    fail_email, _ = fail_next_send
    fail_email.append(PermanentDeliveryError("bad address"))

    send_email.delay("nobody@example.com", "subject", "body")  # no customer_id, no crash

    await as_admin(client)
    customer_id = await make_customer(client)
    resp = await client.get(f"{PROFILE}/{customer_id}")
    assert resp.json()["notification_failures"] == []


async def test_a_successful_send_records_no_failure(client, fail_next_send, sent_emails):
    await as_admin(client)
    customer_id = await make_customer(client)

    send_email.delay(
        "client@example.com",
        "Your appointment is confirmed",
        "body",
        customer_id=customer_id,
        notification_type="booking_confirmation",
    )

    assert len(sent_emails) == 1
    resp = await client.get(f"{PROFILE}/{customer_id}")
    assert resp.json()["notification_failures"] == []


async def test_send_email_looks_up_the_business_row_and_passes_it_to_get_provider(
    client, sent_emails, monkeypatch
):
    """Task 7, #11: before this fix, a queued send always called `get_provider()` with no
    business row (the settings panel's synchronous test-send action was the only caller that
    ever reached a tenant's real Resend/SMTP sender) — a real deployment's actual notifications
    would silently never send for real. Spying on `get_provider` (still delegating to it, so
    the send itself still goes through `tests/fake_notifications.py`'s recording override, not
    a real network call) proves the task now fetches and passes the one `businesses` row."""
    await make_email_ready()
    seen: list[object] = []
    real_get_provider = notification_tasks.get_provider

    def spy(business=None):
        seen.append(business)
        return real_get_provider(business)

    monkeypatch.setattr(notification_tasks, "get_provider", spy)

    send_email.delay("client@example.com", "subject", "body")

    assert len(seen) == 1
    assert seen[0] is not None
    assert seen[0].email_sender == "resend"
    assert len(sent_emails) == 1


async def test_send_sms_looks_up_the_business_row_and_passes_it_to_get_sms_provider(
    client, sent_sms, monkeypatch
):
    """`send_sms`'s counterpart to the `send_email` test above."""
    await make_sms_ready()
    seen: list[object] = []
    real_get_sms_provider = notification_tasks.get_sms_provider

    def spy(business=None):
        seen.append(business)
        return real_get_sms_provider(business)

    monkeypatch.setattr(notification_tasks, "get_sms_provider", spy)

    send_sms.delay("+15559876543", "Reminder")

    assert len(seen) == 1
    assert seen[0] is not None
    assert seen[0].sms_enabled is True
    assert len(sent_sms) == 1


async def test_a_transient_failure_is_not_recorded_as_permanent(client, fail_next_send):
    # A plain `Exception` (network error, a 5xx) is not a `PermanentDeliveryError` — the
    # task's `except PermanentDeliveryError` doesn't catch it, so it is left for
    # `autoretry_for=(Exception,)` exactly as before Task 4, and nothing is written here.
    await as_admin(client)
    customer_id = await make_customer(client)
    fail_email, _ = fail_next_send
    fail_email.append(TimeoutError("connection timed out"))

    try:
        send_email.delay(
            "client@example.com",
            "subject",
            "body",
            customer_id=customer_id,
            notification_type="booking_confirmation",
        )
    except Exception:
        pass  # eager autoretry may exhaust and re-raise; only the *absence* of a row matters

    resp = await client.get(f"{PROFILE}/{customer_id}")
    assert resp.json()["notification_failures"] == []

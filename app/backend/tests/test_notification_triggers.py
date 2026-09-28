"""S1: the five trigger functions (Phase 12 Task 5, #11).

Channel resolution is what this file proves: email only after `email_ready(business)`,
sms only once `sms_enabled` and `sms_ready(business)` both hold — the two gates Task 2/3
already built, read exactly once, here (`notifications/triggers.py::dispatch`). Rendering
itself is `tests/test_notification_render.py`'s job; this file only checks the right template
row was picked and the right recipient/customer_id got to `send_email.delay`/`send_sms.delay`,
via `tests/fake_notifications.py`'s recording fake — never a mocked provider.

`notify_booking_confirmed`/`_modified`/`_cancelled` have no caller anywhere in this codebase
yet (Booking Portal, Phase 6, is what raises those events) — called directly here, the same
way `tests/test_notification_dispatch.py` calls `send_email`/`send_sms` directly before any
trigger existed. `notify_form_link_issued`'s real caller (`forms/links.py`) is exercised by
`tests/test_form_links.py::test_the_link_is_emailed_when_the_client_has_an_address`; this file
adds nothing there. `notify_package_notice` has no caller anywhere (packages/billing is M4).
"""

import uuid

from sqlalchemy import text

from core.db import session_scope
from customers.models import Customer
from notifications.triggers import (
    notify_booking_cancelled,
    notify_booking_confirmed,
    notify_booking_modified,
    notify_package_notice,
)
from scheduling.models import Appointment
from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    CUSTOMERS,
    as_admin,
    at,
    claimed_instance,
    make_service,
    me_staff_id,
    put_hours,
)
from tests.test_form_links import make_email_ready

REACHABLE = {
    "first_name": "Priya",
    "last_name": "Nair",
    "email": "priya@x.example",
    "phone": "416-555-0142",
}


async def make_sms_ready() -> None:
    """Mirrors `tests.test_form_links.make_email_ready`: `sms_ready(business)` and
    `business.sms_enabled` are both read by `dispatch`; the credential value is never
    decrypted here either, since `NOTIFICATION_PROVIDER=recording` short-circuits `get_provider`
    before it would be."""
    async with session_scope() as db:
        await db.execute(
            text(
                "UPDATE businesses SET sms_enabled = true, twilio_account_sid = 'ACxxx', "
                "twilio_auth_token_encrypted = 'unused-in-recording-mode', "
                "twilio_from_number = '+15005550006'"
            )
        )
        await db.commit()


async def _book_reachable_appointment(client) -> str:
    """A confirmed appointment for a customer with both an email and a phone number, so a
    test can exercise either channel's gate independently of the other."""
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    service = await make_service(client, [me])
    customer_id = (await client.post(CUSTOMERS, json=REACHABLE)).json()["id"]
    resp = await client.post(
        "/api/appointments",
        json={
            "service_id": service,
            "staff_id": me,
            "starts_at": at("10:00"),
            "customer_id": customer_id,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_a_booking_confirmed_email_and_sms_are_both_sent_once_the_business_is_ready(
    client, sent_emails, sent_sms
):
    await make_email_ready()
    await make_sms_ready()
    appointment_id = await _book_reachable_appointment(client)

    async with session_scope() as db:
        appointment = await db.get(Appointment, uuid.UUID(appointment_id))
        await notify_booking_confirmed(db, appointment)

    assert len(sent_emails) == 1
    assert sent_emails[0].to == "priya@x.example"
    assert "Swedish Massage" in sent_emails[0].text
    assert len(sent_sms) == 1
    assert sent_sms[0].to == "4165550142"  # `Customer.phone` is stored as digits only


async def test_email_is_a_no_op_until_the_business_is_email_ready(client, sent_emails):
    # No `make_email_ready()` — `businesses.email_sender` is NULL, the setup wizard's default.
    appointment_id = await _book_reachable_appointment(client)

    async with session_scope() as db:
        appointment = await db.get(Appointment, uuid.UUID(appointment_id))
        await notify_booking_confirmed(db, appointment)

    assert sent_emails == []


async def test_sms_is_a_no_op_while_disabled_even_with_a_phone_on_file(client, sent_sms):
    # `sms_enabled` defaults false; no code path may attempt a send while it does.
    await make_email_ready()
    appointment_id = await _book_reachable_appointment(client)

    async with session_scope() as db:
        appointment = await db.get(Appointment, uuid.UUID(appointment_id))
        await notify_booking_confirmed(db, appointment)

    assert sent_sms == []


async def test_booking_modified_and_cancelled_render_their_own_templates(client, sent_emails):
    await make_email_ready()
    appointment_id = await _book_reachable_appointment(client)

    async with session_scope() as db:
        appointment = await db.get(Appointment, uuid.UUID(appointment_id))
        await notify_booking_modified(db, appointment)
        await notify_booking_cancelled(db, appointment)

    assert len(sent_emails) == 2
    assert "changed" in sent_emails[0].subject
    assert "cancelled" in sent_emails[1].subject


async def test_notify_package_notice_has_no_caller_yet_but_works_in_isolation(client, sent_emails):
    await make_email_ready()
    await as_admin(client)
    customer_id = (await client.post(CUSTOMERS, json=REACHABLE)).json()["id"]

    async with session_scope() as db:
        customer = await db.get(Customer, uuid.UUID(customer_id))
        await notify_package_notice(db, customer, package_name="10-Pack Massage", remaining=3)

    assert len(sent_emails) == 1
    assert sent_emails[0].to == "priya@x.example"
    assert "10-Pack Massage" in sent_emails[0].text
    assert "3" in sent_emails[0].text

"""S1: the booking-management link (Phase 6 Task 3, #10) — `POST /manage`, `.../cancel`,
`.../reschedule`, all reached through the token `POST /api/public/booking` returns.

Booking creation itself is `test_booking_public_create.py`'s job; this file starts from an
already-booked appointment and its `management_link`.
"""

from sqlalchemy import select, text

from core.audit import AuditEvent
from core.db import session_scope
from scheduling.models import Appointment
from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    as_admin,
    at,
    claimed_instance,
    make_service,
    me_staff_id,
    put_hours,
)
from tests.test_booking_public_create import booking_caps_wiped  # noqa: F401 — same reason
from tests.test_form_links import make_email_ready

BOOK = "/api/public/booking"
MANAGE = "/api/public/booking/manage"
CANCEL = f"{MANAGE}/cancel"
RESCHEDULE = f"{MANAGE}/reschedule"
ORIGIN = {"Origin": "http://test.linsuite.example"}


async def setup(client, *, hours=(480, 1200)) -> tuple[str, str]:
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, *hours)])
    service = await make_service(client, [me])
    client.cookies.clear()
    return service, me


def customer(email="alex.public@example.com", phone=None, **overrides) -> dict:
    return {
        "first_name": "Alex",
        "last_name": "Public",
        "email": email,
        "phone": phone,
        **overrides,
    }


async def book(client, service: str, staff: str, starts_at: str, **overrides) -> dict:
    resp = await client.post(
        BOOK,
        json={
            "service_id": service,
            "staff_id": staff,
            "starts_at": starts_at,
            "customer": customer(**overrides),
        },
        headers=ORIGIN,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def token_of(management_link: str) -> str:
    assert "#" in management_link, management_link
    fragment = management_link.split("#", 1)[1]
    # Never a path segment or query param (m3.md): nothing before the fragment names it.
    assert "?" not in management_link.split("#", 1)[0]
    return fragment


async def set_business(**columns) -> None:
    assignment = ", ".join(f"{k} = :{k}" for k in columns)
    async with session_scope() as db:
        await db.execute(text(f"UPDATE businesses SET {assignment}"), columns)
        await db.commit()


async def test_the_management_link_is_returned_and_fetches_the_booking_details(client):
    service, staff = await setup(client)
    booked = await book(client, service, staff, at("10:00"))
    assert booked["management_link"]
    token = token_of(booked["management_link"])

    resp = await client.post(MANAGE, json={"token": token}, headers=ORIGIN)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["appointment_id"] == booked["appointment_id"]
    assert body["status"] == "confirmed"
    assert body["service_name"] and body["staff_name"]
    # Booked a week-plus out (`test_appointments.MONDAY`); the default 24h cutoff never bites.
    assert body["cancellable"] is True


async def test_cancel_inside_the_cutoff_window_is_refused_with_a_clear_message(client):
    service, staff = await setup(client)
    booked = await book(client, service, staff, at("10:00"))
    token = token_of(booked["management_link"])
    # A cutoff wider than "a week from now" puts every future booking inside the window.
    await set_business(cancellation_cutoff_hours=24 * 365)

    resp = await client.post(CANCEL, json={"token": token}, headers=ORIGIN)
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "online_change_not_allowed"
    assert body["detail"]

    # Refused, not merely warned about: the appointment is still confirmed.
    check = await client.post(MANAGE, json={"token": token}, headers=ORIGIN)
    assert check.json()["status"] == "confirmed"


async def test_cancel_outside_the_cutoff_succeeds_and_notifies(client, sent_emails):
    service, staff = await setup(client)
    await make_email_ready()
    booked = await book(client, service, staff, at("10:00"))
    token = token_of(booked["management_link"])
    sent_emails.clear()  # only the cancellation email from here on

    resp = await client.post(CANCEL, json={"token": token}, headers=ORIGIN)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["cancellable"] is False
    assert len(sent_emails) == 1

    async with session_scope() as db:
        appointment = await db.get(Appointment, booked["appointment_id"])
        assert appointment.status == "cancelled"
        assert appointment.cancel_reason
        row = await db.scalar(
            select(AuditEvent).where(
                AuditEvent.target_id == booked["appointment_id"],
                AuditEvent.event_type == "appointment.cancelled",
            )
        )
        assert row.actor_user_id is None  # no session made this cancellation either


async def test_disabling_online_cancellation_via_a_direct_db_flip_still_refuses_at_the_api(client):
    """No admin endpoint flips this yet (Task 4) — flipped directly in the database, the same
    way #10's acceptance criterion has to be proven before that screen exists."""
    service, staff = await setup(client)
    booked = await book(client, service, staff, at("10:00"))
    token = token_of(booked["management_link"])
    await set_business(online_cancellation_enabled=False)

    resp = await client.post(CANCEL, json={"token": token}, headers=ORIGIN)
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "online_change_not_allowed"

    # The cutoff alone was never the reason: even far outside it, the toggle still refuses.
    await set_business(cancellation_cutoff_hours=0)
    resp = await client.post(CANCEL, json={"token": token}, headers=ORIGIN)
    assert resp.status_code == 422, resp.text


async def test_reschedule_moves_the_appointment_and_notifies(client, sent_emails):
    service, staff = await setup(client)
    await make_email_ready()
    booked = await book(client, service, staff, at("10:00"))
    token = token_of(booked["management_link"])
    sent_emails.clear()  # only the modification email from here on

    resp = await client.post(
        RESCHEDULE, json={"token": token, "starts_at": at("13:00")}, headers=ORIGIN
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["starts_at"] == at("13:00")
    assert body["cancellable"] is True
    assert len(sent_emails) == 1

    async with session_scope() as db:
        row = await db.scalar(
            select(AuditEvent).where(
                AuditEvent.target_id == booked["appointment_id"],
                AuditEvent.event_type == "appointment.rescheduled",
            )
        )
        assert row is not None
        assert row.actor_user_id is None


async def test_reschedule_honours_the_same_cutoff_as_cancel(client):
    service, staff = await setup(client)
    booked = await book(client, service, staff, at("10:00"))
    token = token_of(booked["management_link"])
    await set_business(cancellation_cutoff_hours=24 * 365)

    resp = await client.post(
        RESCHEDULE, json={"token": token, "starts_at": at("13:00")}, headers=ORIGIN
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "online_change_not_allowed"


async def test_disabling_online_cancellation_also_blocks_reschedule(client):
    """`cancellable` is one shared toggle for both actions (module docstring's own reading of
    the ticket's "Self-service cancellation and rescheduling, subject to admin policy" bullet —
    #10 names only one admin toggle, "online cancellation on/off", not a second one for
    rescheduling). `test_reschedule_honours_the_same_cutoff_as_cancel` already pins the cutoff
    half of that shared gate; this pins the toggle half, which nothing here tested directly."""
    service, staff = await setup(client)
    booked = await book(client, service, staff, at("10:00"))
    token = token_of(booked["management_link"])
    await set_business(online_cancellation_enabled=False)

    resp = await client.post(
        RESCHEDULE, json={"token": token, "starts_at": at("13:00")}, headers=ORIGIN
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "online_change_not_allowed"


async def test_disabling_online_booking_does_not_strand_an_existing_bookings_manage_flow(client):
    """`online_booking_enabled` gates *new* bookings only (`scheduling/public.py`'s
    `public_availability`/`book_public`) — a business turning off new online bookings must not
    strand a client who already booked online from managing that booking. Confirms
    `manage`/`cancel`/`reschedule` read nothing about `online_booking_enabled` at all."""
    service, staff = await setup(client)
    booked = await book(client, service, staff, at("10:00"))
    token = token_of(booked["management_link"])
    await set_business(online_booking_enabled=False)

    view = await client.post(MANAGE, json={"token": token}, headers=ORIGIN)
    assert view.status_code == 200, view.text
    assert view.json()["status"] == "confirmed"
    assert view.json()["cancellable"] is True

    rescheduled = await client.post(
        RESCHEDULE, json={"token": token, "starts_at": at("13:00")}, headers=ORIGIN
    )
    assert rescheduled.status_code == 200, rescheduled.text
    assert rescheduled.json()["starts_at"] == at("13:00")

    cancelled = await client.post(CANCEL, json={"token": token}, headers=ORIGIN)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"


async def test_an_unknown_token_and_an_already_cancelled_ones_get_the_same_404_shape(client):
    service, staff = await setup(client)
    booked = await book(client, service, staff, at("10:00"))
    token = token_of(booked["management_link"])
    dead = await client.post(CANCEL, json={"token": token}, headers=ORIGIN)
    assert dead.status_code == 200, dead.text  # cancels cleanly (well outside any cutoff)

    already_dead = await client.post(MANAGE, json={"token": token}, headers=ORIGIN)
    unknown = await client.post(MANAGE, json={"token": "not-a-real-token"}, headers=ORIGIN)
    assert already_dead.status_code == unknown.status_code == 404
    assert already_dead.json() == unknown.json()
    assert unknown.json()["code"] == "link_invalid"


async def test_an_appointment_whose_start_has_passed_gets_the_same_404_shape_too(client):
    """A second, different reason the link is dead — not "cancelled", but "expired" — proving
    the one `WHERE` treats both alike rather than one of them leaking a different response."""
    service, staff = await setup(client)
    booked = await book(client, service, staff, at("10:00"))
    token = token_of(booked["management_link"])
    async with session_scope() as db:
        await db.execute(
            text(
                "UPDATE appointments SET starts_at = now() - interval '1 hour', "
                "ends_at = now() - interval '30 minutes' WHERE id = :id"
            ),
            {"id": booked["appointment_id"]},
        )
        await db.commit()

    expired = await client.post(MANAGE, json={"token": token}, headers=ORIGIN)
    unknown = await client.post(MANAGE, json={"token": "not-a-real-token"}, headers=ORIGIN)
    assert expired.status_code == unknown.status_code == 404
    assert expired.json() == unknown.json()

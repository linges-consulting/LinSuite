"""S1: `POST /api/public/booking` — the client-facing booking creation route (Phase 6 Task 2,
#10). Availability itself (Task 1, `relax_advisory=False`) is covered in
`test_booking_public.py`; this file covers atomic creation, abuse controls, customer
match-or-create, and the no-actor audit/notification path.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from core.audit import AuditEvent
from core.db import session_scope
from customers.models import Customer
from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    add_colleague,
    as_admin,
    at,
    claimed_instance,
    make_service,
    me_staff_id,
    put_hours,
)
from tests.test_appointments import book as staff_book

BOOK = "/api/public/booking"
ORIGIN = {"Origin": "http://test.linsuite.example"}


@pytest.fixture(autouse=True)
async def booking_caps_wiped(claimed_instance):  # noqa: F811 — the imported fixture, by name
    """The daily per-IP/per-email caps (`_daily_cap_exceeded`) are Redis keys with a 24h TTL,
    on a Redis instance shared for the whole test session (`conftest.py`'s `redis_server` is
    session-scoped) — without this, one test's bookings count against the next test's cap.
    Same precedent as `test_form_links.py::forms_wiped` for `public:forms:*`."""
    from core.redis import get_redis

    async for key in get_redis().scan_iter("public:booking:*"):
        await get_redis().delete(key)
    yield


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


async def test_a_booking_is_created_atomically_and_confirmed(client):
    service, staff = await setup(client)
    resp = await client.post(
        BOOK,
        json={
            "service_id": service,
            "staff_id": staff,
            "starts_at": at("10:00"),
            "customer": customer(),
        },
        headers=ORIGIN,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["service_name"] and body["staff_name"]

    async with session_scope() as db:
        row = await db.scalar(
            select(AuditEvent).where(AuditEvent.target_id == body["appointment_id"])
        )
        assert row.event_type == "appointment.booked"
        assert row.actor_user_id is None  # no session at all made this booking


async def test_honeypot_is_accepted_and_silently_discarded(client):
    service, staff = await setup(client)
    resp = await client.post(
        BOOK,
        json={
            "service_id": service,
            "staff_id": staff,
            "starts_at": at("10:00"),
            "customer": customer(email="bot@example.com"),
            "website": "http://spam.example",
        },
        headers=ORIGIN,
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "received"}
    # The slot is still free — nothing was created.
    real = await client.post(
        BOOK,
        json={
            "service_id": service,
            "staff_id": staff,
            "starts_at": at("10:00"),
            "customer": customer(),
        },
        headers=ORIGIN,
    )
    assert real.status_code == 201, real.text


async def test_no_email_and_no_phone_is_refused(client):
    service, staff = await setup(client)
    resp = await client.post(
        BOOK,
        json={
            "service_id": service,
            "staff_id": staff,
            "starts_at": at("10:00"),
            "customer": customer(email=None, phone=None),
        },
        headers=ORIGIN,
    )
    assert resp.status_code == 422


async def test_a_second_booking_with_the_same_email_reuses_the_existing_customer(client):
    service, staff = await setup(client)
    first = await client.post(
        BOOK,
        json={
            "service_id": service,
            "staff_id": staff,
            "starts_at": at("10:00"),
            "customer": customer(),
        },
        headers=ORIGIN,
    )
    assert first.status_code == 201, first.text
    second = await client.post(
        BOOK,
        json={
            "service_id": service,
            "staff_id": staff,
            "starts_at": at("11:00"),
            "customer": customer(first_name="Alexandra", last_name="Publicist"),
        },
        headers=ORIGIN,
    )
    assert second.status_code == 201, second.text
    async with session_scope() as db:
        rows = (
            await db.scalars(select(Customer).where(Customer.email == "alex.public@example.com"))
        ).all()
        assert len(rows) == 1


async def test_an_erased_client_is_refused(client):
    service, staff = await setup(client)
    async with session_scope() as db:
        row = Customer(
            first_name="Erased", last_name="Client", email="erased@example.com", phone=None
        )
        db.add(row)
        await db.flush()
        row.suppressed_at = datetime.now(UTC)
        await db.commit()
    resp = await client.post(
        BOOK,
        json={
            "service_id": service,
            "staff_id": staff,
            "starts_at": at("10:00"),
            "customer": customer(email="erased@example.com"),
        },
        headers=ORIGIN,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "customer_suppressed"


async def test_a_staff_member_ineligible_for_the_service_is_refused(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 480, 1200)])
    service = await make_service(client, [me])
    # A second staff member, real account, simply never linked to this service.
    other_id = await add_colleague(client, "other@cedar.example", "correct horse battery 2")
    await put_hours(client, other_id, [(0, 480, 1200)])
    client.cookies.clear()

    resp = await client.post(
        BOOK,
        json={
            "service_id": service,
            "staff_id": other_id,
            "starts_at": at("10:00"),
            "customer": customer(),
        },
        headers=ORIGIN,
    )
    assert resp.status_code == 422


async def test_any_available_staff_mode_picks_an_eligible_one(client):
    service, staff = await setup(client)
    resp = await client.post(
        BOOK,
        json={
            "service_id": service,
            "staff_id": None,
            "starts_at": at("10:00"),
            "customer": customer(),
        },
        headers=ORIGIN,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["staff_name"]


async def test_a_slot_that_is_no_longer_offered_is_refused(client):
    service, staff = await setup(client)
    booked = await client.post(
        BOOK,
        json={
            "service_id": service,
            "staff_id": staff,
            "starts_at": at("10:00"),
            "customer": customer(),
        },
        headers=ORIGIN,
    )
    assert booked.status_code == 201, booked.text
    again = await client.post(
        BOOK,
        json={
            "service_id": service,
            "staff_id": staff,
            "starts_at": at("10:00"),
            "customer": customer(email="second@example.com"),
        },
        headers=ORIGIN,
    )
    assert again.status_code == 422


async def test_daily_cap_per_email_refuses_the_one_past_the_limit(client):
    service, staff = await setup(client)
    for i in range(5):
        resp = await client.post(
            BOOK,
            json={
                "service_id": service,
                "staff_id": staff,
                "starts_at": at(f"{9 + i}:00"),
                "customer": customer(email="cap@example.com"),
            },
            headers=ORIGIN,
        )
        assert resp.status_code == 201, (i, resp.text)
    over = await client.post(
        BOOK,
        json={
            "service_id": service,
            "staff_id": staff,
            "starts_at": at("15:00"),
            "customer": customer(email="cap@example.com"),
        },
        headers=ORIGIN,
    )
    assert over.status_code == 429, over.text
    assert over.headers["retry-after"] == "86400"


async def test_the_public_booking_creation_route_never_books_a_slot_only_a_staff_override_reaches(
    client,
):
    """The acceptance-critical claim (#10's own acceptance criteria: "the public endpoint never
    offers a slot that staff availability does not permit, including out-of-shift times"),
    proven through *booking creation* specifically — `test_booking_public.py`'s own adversarial
    test already proves it for `GET .../availability`, and Task 1's row in the ledger explicitly
    scopes that test to availability lookup only. This is the same override precedent
    (`tests/test_overrides.py`), but the client attempts the actual `POST /api/public/booking`
    against the identical start, both before and after staff's own override-booking exists."""
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])  # Monday 09:00-12:00
    service = await make_service(client, [me])

    # Before any override exists: the public creation route already refuses 11:30 as
    # not-offered — the engine's own advisory rule, never relaxed for this route.
    before = await client.post(
        BOOK,
        json={
            "service_id": service,
            "staff_id": me,
            "starts_at": at("11:30"),
            "customer": customer(email="adversary-before@example.com"),
        },
        headers=ORIGIN,
    )
    assert before.status_code == 422, before.text
    assert before.json()["code"] == "not_offered"

    # Staff, with the session still authenticated, actually override-books that same start —
    # the exact staff-side capability the ticket says a client must never reach.
    overridden = await staff_book(
        client, service, me, at("11:30"), override=True, override_reason="Client asked"
    )
    assert overridden.status_code == 201, overridden.text
    client.cookies.clear()

    # After the override booking exists, the public route still refuses the identical slot —
    # the engine's own advisory rule still holds, regardless of what a human has since done.
    after = await client.post(
        BOOK,
        json={
            "service_id": service,
            "staff_id": me,
            "starts_at": at("11:30"),
            "customer": customer(email="adversary-after@example.com"),
        },
        headers=ORIGIN,
    )
    assert after.status_code == 422, after.text
    assert after.json()["code"] == "not_offered"


async def test_daily_cap_per_ip_is_independent_of_email(client):
    service, staff = await setup(client)
    for i in range(20):
        resp = await client.post(
            BOOK,
            json={
                "service_id": service,
                "staff_id": staff,
                "starts_at": at(f"{9 + i % 10}:{'00' if i < 10 else '30'}"),
                "customer": customer(email=f"ip{i}@example.com"),
            },
            headers=ORIGIN,
        )
        assert resp.status_code in (201, 422), (i, resp.text)  # some slots collide, cap must not
    over = await client.post(
        BOOK,
        json={
            "service_id": service,
            "staff_id": staff,
            "starts_at": at("19:30"),
            "customer": customer(email="over-ip@example.com"),
        },
        headers=ORIGIN,
    )
    assert over.status_code == 429, over.text

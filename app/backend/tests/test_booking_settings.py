"""S1: Phase 6 Task 4 (#10) — the booking-portal toggles this task adds to
`settings/notifications_routes.py`'s panel, and their wiring at the API layer in
`scheduling/public.py`.

Not retesting Tasks 1-3's own coverage (`test_booking_public.py`, `test_booking_public_create.
py`, `test_booking_management.py` already own that ground) — only what this task changes:
`online_booking_enabled` reaching both public routes the same way an unbookable service
already does, the daily caps actually reading the business row rather than the old fixed
`_DAILY_CAP_PER_IP`/`_DAILY_CAP_PER_EMAIL` constants, and the cancellation toggle/cutoff being
editable through the real admin `PATCH` endpoint (not only a raw `UPDATE`, which is all Task 3's
own tests had to use since no admin endpoint existed yet) while still being enforced publicly.
"""

from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    MONDAY,
    as_admin,
    at,
    claimed_instance,
    make_service,
    me_staff_id,
    put_hours,
)
from tests.test_booking_management import book as book_public
from tests.test_booking_management import token_of
from tests.test_booking_public_create import booking_caps_wiped  # noqa: F401 — same reason

BOOK = "/api/public/booking"
AVAILABILITY = "/api/public/booking/availability"
NOTIFICATIONS = "/api/admin/business/notifications"
ORIGIN = {"Origin": "http://test.linsuite.example"}


def customer(email="alex.public@example.com", **overrides) -> dict:
    return {
        "first_name": "Alex",
        "last_name": "Public",
        "email": email,
        "phone": None,
        **overrides,
    }


async def setup(client, *, hours=(480, 1200)) -> tuple[str, str]:
    """Unlike the sibling test files' own `setup()`, this one leaves the admin session in
    place — every test here needs it, to reach the settings `PATCH` before acting as an
    anonymous client against the public routes."""
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, *hours)])
    service = await make_service(client, [me])
    return service, me


# --- online_booking_enabled: the whole-portal gate ------------------------------------------


async def test_disabling_online_booking_blocks_both_availability_and_creation(client):
    service, staff = await setup(client)

    resp = await client.patch(NOTIFICATIONS, json={"online_booking_enabled": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["online_booking_enabled"] is False
    client.cookies.clear()

    avail = await client.get(
        AVAILABILITY,
        params={"service_id": service, "from": MONDAY.isoformat(), "to": MONDAY.isoformat()},
    )
    assert avail.status_code == 404, avail.text
    assert avail.json()["detail"] == "No such service."

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
    assert booked.status_code == 404, booked.text
    assert booked.json()["detail"] == "No such service."


async def test_online_booking_stays_enabled_by_default(client):
    """The migration's own default (`server_default=true`): every existing Task 1-3 test
    keeps working through this migration with no change, because nobody has to touch this
    toggle for the portal to keep behaving exactly as it did before this task."""
    service, staff = await setup(client)
    client.cookies.clear()

    resp = await client.get(
        AVAILABILITY,
        params={"service_id": service, "from": MONDAY.isoformat(), "to": MONDAY.isoformat()},
    )
    assert resp.status_code == 200, resp.text


# --- daily caps: admin-configurable, not the old fixed constants ----------------------------


async def test_a_low_custom_per_email_cap_trips_at_the_configured_number(client):
    service, staff = await setup(client)

    resp = await client.patch(NOTIFICATIONS, json={"booking_daily_cap_per_email": 2})
    assert resp.status_code == 200, resp.text
    assert resp.json()["booking_daily_cap_per_email"] == 2
    client.cookies.clear()

    for i in range(2):
        ok = await client.post(
            BOOK,
            json={
                "service_id": service,
                "staff_id": staff,
                "starts_at": at(f"{9 + i}:00"),
                "customer": customer(email="cap@example.com"),
            },
            headers=ORIGIN,
        )
        assert ok.status_code == 201, (i, ok.text)

    # A third request from the same email, still well under the *old* hard-coded cap of 5,
    # is refused at the *new*, lower configured number of 2 — proof this reads the business
    # row, not the constant Task 2 shipped.
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


async def test_a_low_custom_per_ip_cap_trips_at_the_configured_number(client):
    service, staff = await setup(client)

    resp = await client.patch(NOTIFICATIONS, json={"booking_daily_cap_per_ip": 2})
    assert resp.status_code == 200, resp.text
    assert resp.json()["booking_daily_cap_per_ip"] == 2
    client.cookies.clear()

    for i in range(2):
        ok = await client.post(
            BOOK,
            json={
                "service_id": service,
                "staff_id": staff,
                "starts_at": at(f"{9 + i}:00"),
                "customer": customer(email=f"ip{i}@example.com"),
            },
            headers=ORIGIN,
        )
        assert ok.status_code == 201, (i, ok.text)

    over = await client.post(
        BOOK,
        json={
            "service_id": service,
            "staff_id": staff,
            "starts_at": at("15:00"),
            "customer": customer(email="ip-over@example.com"),
        },
        headers=ORIGIN,
    )
    assert over.status_code == 429, over.text


# --- online_cancellation_enabled / cancellation_cutoff_hours: editable through the panel ----


async def test_the_cancellation_toggle_is_editable_through_the_panel_and_enforced_publicly(
    client,
):
    service, staff = await setup(client)
    booked = await book_public(client, service, staff, at("10:00"))
    token = token_of(booked["management_link"])

    resp = await client.patch(NOTIFICATIONS, json={"online_cancellation_enabled": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["online_cancellation_enabled"] is False
    client.cookies.clear()

    cancel = await client.post(f"{BOOK}/manage/cancel", json={"token": token}, headers=ORIGIN)
    assert cancel.status_code == 422, cancel.text
    assert cancel.json()["code"] == "online_change_not_allowed"


async def test_the_cutoff_hours_is_editable_through_the_panel_and_enforced_publicly(client):
    service, staff = await setup(client)
    booked = await book_public(client, service, staff, at("10:00"))
    token = token_of(booked["management_link"])

    # A cutoff wider than "a week from now" (`MONDAY` is booked well out) puts this booking
    # inside the window — same premise `test_booking_management.py`'s own cutoff test uses,
    # set here through the real `PATCH` endpoint instead of a raw `UPDATE`.
    resp = await client.patch(NOTIFICATIONS, json={"cancellation_cutoff_hours": 24 * 365})
    assert resp.status_code == 200, resp.text
    assert resp.json()["cancellation_cutoff_hours"] == 24 * 365
    client.cookies.clear()

    cancel = await client.post(f"{BOOK}/manage/cancel", json={"token": token}, headers=ORIGIN)
    assert cancel.status_code == 422, cancel.text
    assert cancel.json()["code"] == "online_change_not_allowed"


# --- validation: positive integers, sane bounds ----------------------------------------------


async def test_a_zero_or_negative_daily_cap_is_rejected(client):
    await as_admin(client)
    for field in ("booking_daily_cap_per_ip", "booking_daily_cap_per_email"):
        resp = await client.patch(NOTIFICATIONS, json={field: 0})
        assert resp.status_code == 422, (field, resp.text)


async def test_an_implausible_daily_cap_is_rejected(client):
    await as_admin(client)
    resp = await client.patch(NOTIFICATIONS, json={"booking_daily_cap_per_ip": 100_000})
    assert resp.status_code == 422, resp.text


async def test_a_negative_cutoff_is_rejected(client):
    await as_admin(client)
    resp = await client.patch(NOTIFICATIONS, json={"cancellation_cutoff_hours": -1})
    assert resp.status_code == 422, resp.text


async def test_an_implausible_cutoff_is_rejected(client):
    await as_admin(client)
    resp = await client.patch(NOTIFICATIONS, json={"cancellation_cutoff_hours": 8761})
    assert resp.status_code == 422, resp.text

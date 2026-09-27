"""S1: `GET /api/public/booking/services` (Phase 6 Task 6, #10) — the listing the booking
page's first screen reads before it has a `service_id` to hand `GET /availability`.

Not retesting `_catalog_out`'s own bookability arithmetic (`test_services.py` already owns
that ground) — only that this new public route reuses it correctly: filters to
`bookable_online and bookable`, names staff rather than leaving bare ids, and answers the
whole-portal toggle with a signal a picker can render (`online_booking_enabled: false`, empty
list) rather than a 404 that assumes a `service_id` already exists.
"""

from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    as_admin,
    claimed_instance,
    make_service,
    me_staff_id,
)

SERVICES = "/api/public/booking/services"
NOTIFICATIONS = "/api/admin/business/notifications"


async def test_a_bookable_online_service_is_listed_with_its_staff_by_name(client):
    await as_admin(client)
    me = await me_staff_id(client)
    service = await make_service(client, [me])

    resp = await client.get(SERVICES)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["online_booking_enabled"] is True
    assert len(body["services"]) == 1
    listed = body["services"][0]
    assert listed["id"] == service
    assert listed["name"] == "Swedish Massage"
    assert listed["duration_minutes"] == 60
    assert listed["price_cents"] == 12000
    assert [s["display_name"] for s in listed["staff"]] == ["owner"]


async def test_a_service_not_opted_into_online_booking_is_absent(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await make_service(client, [me], bookable_online=False)

    resp = await client.get(SERVICES)

    assert resp.status_code == 200, resp.text
    assert resp.json()["services"] == []


async def test_a_service_with_nobody_eligible_is_absent(client):
    """`bookable_online` true but `bookable` false (no active staff) — the same reason
    `book_public` would refuse it (`unbookable`, 409) is why it should never be offered."""
    await as_admin(client)
    await make_service(client, [])  # no staff linked at all

    resp = await client.get(SERVICES)

    assert resp.status_code == 200, resp.text
    assert resp.json()["services"] == []


async def test_disabling_online_booking_answers_with_an_empty_list_not_a_404(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await make_service(client, [me])
    toggled = await client.patch(NOTIFICATIONS, json={"online_booking_enabled": False})
    assert toggled.status_code == 200, toggled.text

    resp = await client.get(SERVICES)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"online_booking_enabled": False, "services": []}


async def test_staff_are_named_in_display_name_order(client):
    await as_admin(client)
    me = await me_staff_id(client)
    from tests.test_appointments import add_colleague

    other = await add_colleague(client, "zed@cedar.example", "correct horse battery 3")
    await make_service(client, [me, other])

    resp = await client.get(SERVICES)

    assert resp.status_code == 200, resp.text
    names = [s["display_name"] for s in resp.json()["services"][0]["staff"]]
    assert names == sorted(names)
    assert len(names) == 2

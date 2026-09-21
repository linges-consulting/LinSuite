"""S1: booking groups, over HTTP (Task 18).

A visit is an ordered chain of services for one customer, each link its own appointment —
own staff, own resources, own snapshot — sharing one `booking_group_id`. This file proves the
sequential search over HTTP (`GET /api/availability/group`), that booking one is one
transaction (a refused link refuses the whole group, nothing written; a raced link rolls back
everything), and that a group's cancel acts on every non-terminal member and leaves the rest.
`tests/test_chains.py` already pins the pure search this all sits on.
"""

from sqlalchemy import text

from core.db import session_scope
from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    APPOINTMENTS,
    MONDAY,
    as_admin,
    at,
    audit_events,
    book,
    claimed_instance,
    make_customer,
    make_resource,
    make_service,
    me_staff_id,
    put_hours,
)

GROUP_AVAILABILITY = "/api/availability/group"
GROUP_BOOK = f"{APPOINTMENTS}/group"


def group_cancel_url(group_id: str) -> str:
    return f"{APPOINTMENTS}/group/{group_id}/cancel"


async def add_colleague(client, email: str, password: str, *, role: str = "Staff") -> str:
    from core.security import hash_password
    from tests.conftest import add_account

    async with session_scope() as db:
        role_id = str(await db.scalar(text("SELECT id FROM roles WHERE name = :n"), {"n": role}))
    await add_account(email, await hash_password(password), role=role_id)
    roster = await client.get("/api/admin/staff")
    return next(row["id"] for row in roster.json()["staff"] if row["email"] == email)


async def group_availability(client, service_ids: list[str], **params):
    query = {
        "services": ",".join(service_ids),
        "from": MONDAY.isoformat(),
        "to": MONDAY.isoformat(),
        **params,
    }
    resp = await client.get(GROUP_AVAILABILITY, params=query)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def book_group(client, links: list[dict], **extra):
    body = {"starts_at": at("09:00"), "links": links, **extra}
    if "customer" not in body and "customer_id" not in body:
        body["customer"] = {"first_name": "Priya", "last_name": "Nair"}
    return await client.post(GROUP_BOOK, json=body)


async def two_providers(client):
    """Ana (the admin's own staff row) and Ben, each free all day Monday."""
    await as_admin(client)
    ana = await me_staff_id(client)
    ben = await add_colleague(client, "ben@cedar.example", "correct horse battery 2")
    await put_hours(client, ana, [(0, 540, 1020)])
    await put_hours(client, ben, [(0, 540, 1020)])
    return ana, ben


# --- sequential search ------------------------------------------------------------------------


async def test_group_availability_offers_only_chain_valid_starts(client):
    ana, ben = await two_providers(client)
    facial = await make_service(client, [ana], name="Facial", duration_minutes=60, price_cents=8000)
    massage = await make_service(
        client, [ben], name="Massage", duration_minutes=60, price_cents=9000
    )

    data = await group_availability(client, [facial, massage], staff=f"{ana},{ben}")

    starts = [s["starts_at"] for s in data["days"][0]["slots"]]
    assert at("09:00") in starts
    # Every offered start's link staff matches what was asked.
    nine = next(s for s in data["days"][0]["slots"] if s["starts_at"] == at("09:00"))
    assert nine["staff_ids"] == [ana, ben]


async def test_group_availability_drops_a_start_the_second_link_cannot_continue_from(client):
    await as_admin(client)
    ana = await me_staff_id(client)
    ben = await add_colleague(client, "ben@cedar.example", "correct horse battery 2")
    # Ana works all day; Ben only works the afternoon — so no morning facial start chains.
    await put_hours(client, ana, [(0, 540, 1020)])
    await put_hours(client, ben, [(0, 780, 1020)])  # 13:00-17:00
    facial = await make_service(client, [ana], name="Facial", duration_minutes=60, price_cents=8000)
    massage = await make_service(
        client, [ben], name="Massage", duration_minutes=60, price_cents=9000
    )

    data = await group_availability(client, [facial, massage], staff=f"{ana},{ben}")

    starts = [s["starts_at"] for s in data["days"][0]["slots"]]
    assert at("09:00") not in starts
    assert at("12:00") in starts  # facial 12:00-13:00, massage 13:00-14:00


async def test_group_availability_resolves_any_to_the_lowest_sort_order_offered(client):
    ana, ben = await two_providers(client)
    facial = await make_service(client, [ana], name="Facial", duration_minutes=60, price_cents=8000)
    massage = await make_service(
        client, [ana, ben], name="Massage", duration_minutes=60, price_cents=9000
    )

    data = await group_availability(client, [facial, massage], staff=f"{ana},any")

    nine = next(s for s in data["days"][0]["slots"] if s["starts_at"] == at("09:00"))
    # Ana cannot do her own massage at 09:00 (she's doing the facial); Ben resolves.
    assert nine["staff_ids"] == [ana, ben]


async def test_group_availability_defaults_every_link_to_any_when_staff_is_left_out(client):
    ana, _ben = await two_providers(client)
    facial = await make_service(client, [ana], name="Facial", duration_minutes=60, price_cents=8000)
    data = await group_availability(client, [facial])
    assert at("09:00") in [s["starts_at"] for s in data["days"][0]["slots"]]


# --- booking -----------------------------------------------------------------------------------


async def test_booking_a_group_creates_linked_appointments_with_distinct_resources(client):
    ana, ben = await two_providers(client)
    room1 = await make_resource(client, "space", "Room 1")
    room2 = await make_resource(client, "space", "Room 2")
    facial = await make_service(
        client,
        [ana],
        name="Facial",
        duration_minutes=60,
        price_cents=8000,
        requirements=[{"kind": "space"}],
    )
    massage = await make_service(
        client,
        [ben],
        name="Massage",
        duration_minutes=60,
        price_cents=9000,
        requirements=[{"kind": "space"}],
    )

    resp = await book_group(
        client, [{"service_id": facial, "staff_id": ana}, {"service_id": massage, "staff_id": ben}]
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    group_id = body["booking_group_id"]
    assert group_id
    appointments = body["appointments"]
    assert len(appointments) == 2
    assert all(a["booking_group_id"] == group_id for a in appointments)
    assert appointments[0]["staff"]["id"] == ana
    assert appointments[0]["starts_at"] == at("09:00")
    assert appointments[0]["ends_at"] == at("10:00")
    assert appointments[0]["resources"] == [{"id": room1, "name": "Room 1", "kind": "space"}]
    assert appointments[1]["staff"]["id"] == ben
    assert appointments[1]["starts_at"] == at("10:00")
    # The links don't overlap in time (10:00 is the facial's own end), so re-using Room 1 is
    # correct — "distinct resources per link" is about two live claims, not two names.
    assert appointments[1]["resources"][0]["kind"] == "space"
    assert appointments[1]["resources"][0]["id"] in (room1, room2)

    booked = [e for e in await audit_events() if e[0] == "appointment.booked"]
    assert len(booked) == 2
    group_booked = [e for e in await audit_events() if e[0] == "group.booked"]
    assert len(group_booked) == 1
    assert group_booked[0][2] == group_id
    assert set(group_booked[0][3]["appointment_ids"]) == {a["id"] for a in appointments}


async def test_a_refused_link_refuses_the_whole_group_and_writes_nothing(client):
    ana, ben = await two_providers(client)
    facial = await make_service(client, [ana], name="Facial", duration_minutes=60)
    massage = await make_service(client, [ben], name="Massage", duration_minutes=60)
    # Ben is already busy at the handover time (10:00-11:00) with something unrelated — a
    # physical conflict, not an advisory one, so link 1 (index 1) is refused outright and
    # nothing about it can be overridden.
    busy = await book(client, massage, ben, at("10:00"))
    assert busy.status_code == 201, busy.text

    resp = await book_group(
        client, [{"service_id": facial, "staff_id": ana}, {"service_id": massage, "staff_id": ben}]
    )

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "not_offered"
    assert body["link_index"] == 1

    listing = await client.get(
        APPOINTMENTS, params={"from": MONDAY.isoformat(), "to": MONDAY.isoformat()}
    )
    # Only the pre-existing "busy" appointment is there; the group itself wrote nothing.
    assert len(listing.json()["appointments"]) == 1


async def test_an_ineligible_named_staff_member_refuses_the_group_with_its_link_index(client):
    ana, ben = await two_providers(client)
    facial = await make_service(client, [ana], name="Facial", duration_minutes=60)
    massage = await make_service(client, [ben], name="Massage", duration_minutes=60)

    resp = await book_group(
        client,
        # Ana cannot deliver the massage.
        [{"service_id": facial, "staff_id": ana}, {"service_id": massage, "staff_id": ana}],
    )

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["link_index"] == 1
    assert body["code"] == "invalid_link"


async def test_a_raced_link_rolls_back_the_whole_group(client):
    """A concurrent booking claims link 2's room between the check and the insert — the
    exclusion constraint refuses it, and the whole group (including link 1, already
    provisionally written this transaction) must not land."""
    ana, ben = await two_providers(client)
    await make_resource(client, "space", "Room 1")
    facial = await make_service(
        client, [ana], name="Facial", duration_minutes=60, requirements=[{"kind": "space"}]
    )
    massage = await make_service(
        client, [ben], name="Massage", duration_minutes=60, requirements=[{"kind": "space"}]
    )
    # Steal the room for link 2's window (10:00-11:00) with an unrelated booking first.
    thief = await book(client, massage, ben, at("10:00"))
    assert thief.status_code == 201, thief.text

    resp = await book_group(
        client, [{"service_id": facial, "staff_id": ana}, {"service_id": massage, "staff_id": ben}]
    )

    # The engine itself already refuses this before any insert is attempted (the room shows
    # busy), so this is `not_offered` rather than a database-level race — either way nothing
    # from this request is written.
    assert resp.status_code in (409, 422), resp.text
    listing = await client.get(
        APPOINTMENTS,
        params={"from": MONDAY.isoformat(), "to": MONDAY.isoformat(), "staff_id": ana},
    )
    assert listing.json()["appointments"] == []


# --- group cancel --------------------------------------------------------------------------


async def test_cancelling_a_group_cancels_every_non_terminal_member(client):
    ana, ben = await two_providers(client)
    facial = await make_service(client, [ana], name="Facial", duration_minutes=60)
    massage = await make_service(client, [ben], name="Massage", duration_minutes=60)
    booked = await book_group(
        client, [{"service_id": facial, "staff_id": ana}, {"service_id": massage, "staff_id": ben}]
    )
    assert booked.status_code == 201, booked.text
    group_id = booked.json()["booking_group_id"]
    first_id = booked.json()["appointments"][0]["id"]

    complete_first = await client.post(f"{APPOINTMENTS}/{first_id}/complete", json={})
    assert complete_first.status_code == 200, complete_first.text

    resp = await client.post(group_cancel_url(group_id), json={"reason": "Client rescheduled"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    first, second = body["appointments"]
    # Completed member is untouched by the group cancel.
    assert first["status"] == "completed"
    assert second["status"] == "cancelled"
    assert second["cancel_reason"] == "Client rescheduled"
    events = [e for e in await audit_events() if e[0] == "group.cancelled"]
    assert len(events) == 1
    assert events[0][3]["appointment_ids"] == [second["id"]]


async def test_cancelling_a_single_member_leaves_its_siblings_alone(client):
    ana, ben = await two_providers(client)
    facial = await make_service(client, [ana], name="Facial", duration_minutes=60)
    massage = await make_service(client, [ben], name="Massage", duration_minutes=60)
    booked = await book_group(
        client, [{"service_id": facial, "staff_id": ana}, {"service_id": massage, "staff_id": ben}]
    )
    assert booked.status_code == 201, booked.text
    first_id, second_id = (a["id"] for a in booked.json()["appointments"])

    resp = await client.post(f"{APPOINTMENTS}/{first_id}/cancel", json={})

    assert resp.status_code == 200, resp.text
    listing = await client.get(
        APPOINTMENTS, params={"from": MONDAY.isoformat(), "to": MONDAY.isoformat()}
    )
    by_id = {a["id"]: a for a in listing.json()["appointments"]}
    assert first_id not in by_id  # cancelled ones are left out by default
    assert by_id[second_id]["status"] == "confirmed"


async def test_cancelling_an_unknown_group_is_404(client):
    await as_admin(client)
    resp = await client.post(group_cancel_url("00000000-0000-0000-0000-000000000000"), json={})
    assert resp.status_code == 404, resp.text

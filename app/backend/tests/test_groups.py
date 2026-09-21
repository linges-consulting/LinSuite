"""S1: booking groups, over HTTP (Task 18).

A visit is an ordered chain of services for one customer, each link its own appointment —
own staff, own resources, own snapshot — sharing one `booking_group_id`. This file proves the
sequential search over HTTP (`GET /api/availability/group`), that booking one is one
transaction (a refused link refuses the whole group, nothing written; a raced link rolls back
everything), and that a group's cancel acts on every non-terminal member and leaves the rest.
`tests/test_chains.py` already pins the pure search this all sits on.
"""

from datetime import timedelta

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
    move,
    put_hours,
    resource_periods,
)
from tests.test_lifecycle import _concurrent_requests, _slowed

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


# --- the buffer waiver between consecutive links (fix round 1, Important) -----------------
#
# Same client, no turnover: a link's outgoing buffer and the next link's incoming buffer do
# not count against each other, for the staff member and for a shared resource. Against
# every other appointment those buffers apply in full, and the visit's own outer edges
# (the first link's before, the last link's after) are never touched.


async def test_a_same_staff_chain_offered_at_the_handover_actually_books(client):
    """The exact repro: a 60-minute service with a 15-minute after-buffer, then a 30-minute
    one, same staff. `GET .../group` must offer 09:00, and `POST /group` must book it —
    the same instant, the same answer."""
    ana, _ben = await two_providers(client)
    facial = await make_service(
        client, [ana], name="Facial", duration_minutes=60, buffer_after_minutes=15
    )
    addon = await make_service(client, [ana], name="Hot Stone Add-on", duration_minutes=30)

    data = await group_availability(client, [facial, addon], staff=f"{ana},{ana}")
    assert at("09:00") in [s["starts_at"] for s in data["days"][0]["slots"]]

    resp = await book_group(
        client, [{"service_id": facial, "staff_id": ana}, {"service_id": addon, "staff_id": ana}]
    )

    assert resp.status_code == 201, resp.text
    first, second = resp.json()["appointments"]
    assert first["starts_at"] == at("09:00")
    assert first["ends_at"] == at("10:00")
    assert second["starts_at"] == at("10:00")
    assert second["staff"]["id"] == ana


async def test_a_same_room_chain_offered_at_the_handover_actually_books(client):
    """The same repro, for a shared *resource* rather than a shared staff member: without
    the waiver, the two links' buffered periods would overlap and trip the exclusion
    constraint the moment the second link is inserted."""
    ana, ben = await two_providers(client)
    room = await make_resource(client, "space", "Room 1")
    facial = await make_service(
        client,
        [ana],
        name="Facial",
        duration_minutes=60,
        buffer_after_minutes=15,
        requirements=[{"kind": "space"}],
    )
    addon = await make_service(
        client,
        [ben],
        name="Hot Stone Add-on",
        duration_minutes=30,
        requirements=[{"kind": "space"}],
    )

    data = await group_availability(client, [facial, addon], staff=f"{ana},{ben}")
    assert at("09:00") in [s["starts_at"] for s in data["days"][0]["slots"]]

    resp = await book_group(
        client, [{"service_id": facial, "staff_id": ana}, {"service_id": addon, "staff_id": ben}]
    )

    assert resp.status_code == 201, resp.text
    first, second = resp.json()["appointments"]
    assert first["resources"] == [{"id": room, "name": "Room 1", "kind": "space"}]
    assert second["resources"] == [{"id": room, "name": "Room 1", "kind": "space"}]


async def test_an_unrelated_booking_right_after_the_visit_respects_the_last_links_buffer(client):
    """The waiver is only between consecutive *siblings* — the visit's own outer edge (the
    last link's after-buffer) still applies in full to everyone else."""
    ana, ben = await two_providers(client)
    facial = await make_service(client, [ana], name="Facial", duration_minutes=60)
    addon = await make_service(
        client, [ben], name="Hot Stone Add-on", duration_minutes=30, buffer_after_minutes=15
    )
    booked = await book_group(
        client, [{"service_id": facial, "staff_id": ana}, {"service_id": addon, "staff_id": ben}]
    )
    assert booked.status_code == 201, booked.text
    assert booked.json()["appointments"][1]["ends_at"] == at("10:30")

    # Ben is free by the clock at 10:30, but the add-on's own 15-minute after-buffer is real
    # turnover this unrelated client does not get to skip.
    unrelated = await book(client, addon, ben, at("10:30"))

    assert unrelated.status_code == 422, unrelated.text
    assert unrelated.json()["code"] == "not_offered"


async def test_an_outsider_inside_the_handover_window_still_blocks_the_chain(client):
    """The waiver only ever ignores a *sibling that does not exist yet* — a real, pre-existing
    appointment sitting in that same window is exactly what "not offered" still means."""
    ana, ben = await two_providers(client)
    facial = await make_service(
        client, [ana], name="Facial", duration_minutes=60, buffer_after_minutes=15
    )
    addon = await make_service(client, [ben], name="Hot Stone Add-on", duration_minutes=30)
    outsider = await book(client, addon, ben, at("10:00"))
    assert outsider.status_code == 201, outsider.text

    data = await group_availability(client, [facial, addon], staff=f"{ana},{ben}")

    assert at("09:00") not in [s["starts_at"] for s in data["days"][0]["slots"]]


async def test_a_no_op_patch_of_a_group_member_succeeds(client):
    """A move that asks for nothing new must not be refused merely because the loader,
    checking it against its own stored (waived) buffer, second-guesses the booking it was
    itself part of."""
    ana, _ben = await two_providers(client)
    facial = await make_service(
        client, [ana], name="Facial", duration_minutes=60, buffer_after_minutes=15
    )
    addon = await make_service(client, [ana], name="Hot Stone Add-on", duration_minutes=30)
    booked = await book_group(
        client, [{"service_id": facial, "staff_id": ana}, {"service_id": addon, "staff_id": ana}]
    )
    assert booked.status_code == 201, booked.text
    second = booked.json()["appointments"][1]
    assert second["starts_at"] == at("10:00")

    resp = await move(client, second["id"], starts_at=at("10:00"))

    assert resp.status_code == 200, resp.text


# --- the waiver is derived, never stored (fix round 2) --------------------------------------
#
# Round 1's fix zeroed the sibling's own `buffer_after_minutes`/`buffer_before_minutes` to make
# the handover book — a regression: the row started lying about the service's real buffer, and
# nothing restored it when the sibling later left. These prove the waiver is re-derived from
# current status and adjacency every time it matters, for staff and for a shared resource, and
# that it "restores" itself the instant a sibling stops being adjacent — with no code of its own.


async def find(client, appointment_id: str) -> dict:
    listing = await client.get(
        APPOINTMENTS,
        params={"from": MONDAY.isoformat(), "to": MONDAY.isoformat(), "include_cancelled": True},
    )
    return next(a for a in listing.json()["appointments"] if a["id"] == appointment_id)


async def test_cancelling_one_link_of_a_same_staff_chain_restores_the_others_buffer(client):
    ana, _ben = await two_providers(client)
    facial = await make_service(
        client, [ana], name="Facial", duration_minutes=60, buffer_after_minutes=15
    )
    addon = await make_service(client, [ana], name="Hot Stone Add-on", duration_minutes=30)
    booked = await book_group(
        client, [{"service_id": facial, "staff_id": ana}, {"service_id": addon, "staff_id": ana}]
    )
    assert booked.status_code == 201, booked.text
    first, second = booked.json()["appointments"]
    assert (await find(client, first["id"]))["buffer_after_minutes"] == 15

    cancel = await client.post(f"{APPOINTMENTS}/{second['id']}/cancel", json={})
    assert cancel.status_code == 200, cancel.text

    # The snapshot never moved — before the cancel or after it, the row still reports the
    # service's real buffer.
    assert (await find(client, first["id"]))["buffer_after_minutes"] == 15

    # The addon is gone, but its own former slot is not free: facial's after-buffer, no
    # longer waived against anything, covers it again.
    other = await book(client, addon, ana, at("10:00"))
    assert other.status_code == 422, other.text
    assert other.json()["code"] == "not_offered"

    data = await group_availability(client, [addon], staff=ana)
    assert at("10:00") not in [s["starts_at"] for s in data["days"][0]["slots"]]


async def test_cancelling_one_link_of_a_same_room_chain_restores_the_others_period(client):
    ana, ben = await two_providers(client)
    room = await make_resource(client, "space", "Room 1")
    facial = await make_service(
        client,
        [ana],
        name="Facial",
        duration_minutes=60,
        buffer_after_minutes=15,
        requirements=[{"kind": "space"}],
    )
    addon = await make_service(
        client,
        [ben],
        name="Hot Stone Add-on",
        duration_minutes=30,
        requirements=[{"kind": "space"}],
    )
    booked = await book_group(
        client, [{"service_id": facial, "staff_id": ana}, {"service_id": addon, "staff_id": ben}]
    )
    assert booked.status_code == 201, booked.text
    first, second = booked.json()["appointments"]
    # Waived while the addon is still there: facial's own stored period ends exactly at 10:00.
    assert (await resource_periods(first["id"])) == [(room, at("09:00"), at("10:00"))]

    cancel = await client.post(f"{APPOINTMENTS}/{second['id']}/cancel", json={})
    assert cancel.status_code == 200, cancel.text

    # The room is free by the clock at 10:00, but facial's own 15-minute after-buffer, no
    # longer waived, claims it again.
    other = await book(client, addon, ben, at("10:00"))
    assert other.status_code == 422, other.text
    assert other.json()["code"] == "not_offered"
    periods = await resource_periods(first["id"])
    assert periods == [(room, at("09:00"), at("10:15"))]


# --- no-show on a handover chain (fix wave, findings 1 and 2) --------------------------------
#
# A no-show stops occupying exactly as a cancel does, so the *status update itself* must not be
# compared against its sibling with the facing buffers back in force — that comparison overlaps
# and the trigger refuses it. The row has to be in the past to be markable, so the whole visit
# is pushed back (resource claims included) before the transition is asked for.

PAST_DAYS = 20
PAST_MONDAY = MONDAY - timedelta(days=PAST_DAYS)


async def push_group_to_past(group_id: str, days: int = PAST_DAYS) -> None:
    async with session_scope() as db:
        await db.execute(
            text(
                "UPDATE appointment_resources SET period = tstzrange("
                "lower(period) - make_interval(days => :d), "
                "upper(period) - make_interval(days => :d), '[)') "
                "WHERE appointment_id IN "
                "(SELECT id FROM appointments WHERE booking_group_id = :g)"
            ),
            {"d": days, "g": group_id},
        )
        await db.execute(
            text(
                "UPDATE appointments SET starts_at = starts_at - make_interval(days => :d), "
                "ends_at = ends_at - make_interval(days => :d) WHERE booking_group_id = :g"
            ),
            {"d": days, "g": group_id},
        )
        await db.commit()


async def _buffered_same_staff_chain(client):
    """Cut (15-minute after-buffer) then colour (10-minute before-buffer), one stylist, one
    room — the commonest salon visit, and the one the handover waiver exists for."""
    ana, _ben = await two_providers(client)
    room = await make_resource(client, "space", "Room 1")
    cut = await make_service(
        client,
        [ana],
        name="Cut",
        duration_minutes=60,
        buffer_after_minutes=15,
        requirements=[{"kind": "space"}],
    )
    colour = await make_service(
        client,
        [ana],
        name="Colour",
        duration_minutes=30,
        buffer_before_minutes=10,
        requirements=[{"kind": "space"}],
    )
    booked = await book_group(
        client, [{"service_id": cut, "staff_id": ana}, {"service_id": colour, "staff_id": ana}]
    )
    assert booked.status_code == 201, booked.text
    body = booked.json()
    await push_group_to_past(body["booking_group_id"])
    return room, body["appointments"]


async def test_no_showing_the_first_link_of_a_buffered_chain_frees_the_siblings_period(client):
    room, (first, second) = await _buffered_same_staff_chain(client)

    resp = await client.post(f"{APPOINTMENTS}/{first['id']}/no-show", json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "no_show"
    assert resp.json()["resources"] == []
    # The colour's own before-buffer is no longer waived against anything: its room period
    # springs back to cover the ten minutes before it starts.
    assert await resource_periods(second["id"]) == [
        (room, at("09:50", PAST_MONDAY), at("10:30", PAST_MONDAY))
    ]


async def test_no_showing_the_second_link_of_a_buffered_chain_frees_the_siblings_period(client):
    room, (first, second) = await _buffered_same_staff_chain(client)

    resp = await client.post(f"{APPOINTMENTS}/{second['id']}/no-show", json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "no_show"
    assert await resource_periods(first["id"]) == [
        (room, at("09:00", PAST_MONDAY), at("10:15", PAST_MONDAY))
    ]


async def test_moving_a_link_away_restores_the_previous_links_buffer(client):
    ana, _ben = await two_providers(client)
    facial = await make_service(
        client, [ana], name="Facial", duration_minutes=60, buffer_after_minutes=15
    )
    addon = await make_service(client, [ana], name="Hot Stone Add-on", duration_minutes=30)
    booked = await book_group(
        client, [{"service_id": facial, "staff_id": ana}, {"service_id": addon, "staff_id": ana}]
    )
    assert booked.status_code == 201, booked.text
    first, second = booked.json()["appointments"]

    moved = await move(client, second["id"], starts_at=at("11:00"))
    assert moved.status_code == 200, moved.text

    # Facial's after-buffer is back in force at its own old edge — nothing sits there anymore
    # to waive it against.
    other = await book(client, addon, ana, at("10:00"))
    assert other.status_code == 422, other.text
    assert other.json()["code"] == "not_offered"


async def test_a_before_buffer_on_the_second_link_is_waived_against_the_first(client):
    """The waiver is symmetric: a facing *before*-buffer on the later link is waived exactly
    like a facing after-buffer on the earlier one."""
    ana, _ben = await two_providers(client)
    facial = await make_service(client, [ana], name="Facial", duration_minutes=60)
    addon = await make_service(
        client, [ana], name="Hot Stone Add-on", duration_minutes=30, buffer_before_minutes=15
    )

    data = await group_availability(client, [facial, addon], staff=f"{ana},{ana}")
    assert at("09:00") in [s["starts_at"] for s in data["days"][0]["slots"]]

    resp = await book_group(
        client, [{"service_id": facial, "staff_id": ana}, {"service_id": addon, "staff_id": ana}]
    )

    assert resp.status_code == 201, resp.text
    first, second = resp.json()["appointments"]
    assert first["ends_at"] == at("10:00")
    assert second["starts_at"] == at("10:00")


async def test_an_a_b_a_three_link_chain_waives_both_edges_of_the_middle_link(client):
    ana, ben = await two_providers(client)
    first_service = await make_service(
        client, [ana], name="Prep", duration_minutes=30, buffer_after_minutes=10
    )
    middle_service = await make_service(
        client,
        [ben],
        name="Massage",
        duration_minutes=60,
        buffer_before_minutes=10,
        buffer_after_minutes=10,
    )
    last_service = await make_service(
        client, [ana], name="Finish", duration_minutes=30, buffer_before_minutes=10
    )

    resp = await book_group(
        client,
        [
            {"service_id": first_service, "staff_id": ana},
            {"service_id": middle_service, "staff_id": ben},
            {"service_id": last_service, "staff_id": ana},
        ],
    )

    assert resp.status_code == 201, resp.text
    a, b, c = resp.json()["appointments"]
    assert a["ends_at"] == at("09:30")
    assert b["starts_at"] == at("09:30")
    assert b["ends_at"] == at("10:30")
    assert c["starts_at"] == at("10:30")


async def test_a_buffered_chain_can_be_booked_cancelled_and_booked_again_at_the_same_start(client):
    """A round trip: booking a same-staff buffered chain, cancelling the whole group, and
    booking the identical chain at the identical start again must both succeed — nothing
    about the first booking's waiver may linger stuck-narrow after its group is gone."""
    ana, _ben = await two_providers(client)
    facial = await make_service(
        client, [ana], name="Facial", duration_minutes=60, buffer_after_minutes=15
    )
    addon = await make_service(client, [ana], name="Hot Stone Add-on", duration_minutes=30)
    links = [{"service_id": facial, "staff_id": ana}, {"service_id": addon, "staff_id": ana}]

    first = await book_group(client, links)
    assert first.status_code == 201, first.text
    group_id = first.json()["booking_group_id"]

    cancelled = await client.post(group_cancel_url(group_id), json={})
    assert cancelled.status_code == 200, cancelled.text

    second = await book_group(client, links)
    assert second.status_code == 201, second.text
    a, b = second.json()["appointments"]
    assert a["starts_at"] == at("09:00")
    assert b["starts_at"] == at("10:00")


# --- concurrent transitions on different members of one group (fix round 3) -----------------
#
# `cancel_appointment`, `mark_no_show` and `change_appointment` used to lock only their own
# row before `recompute_group_periods` read and rewrote the rest of the group unlocked — two
# such requests on two *different* members of one group could each read a stale "who's
# occupying" and race each other. `_lock` now takes the whole group's lock, in id order,
# before any of them read a status; same technique as `test_lifecycle.py`'s racing tests.


async def test_concurrent_cancels_of_two_different_group_members_do_not_race(client, monkeypatch):
    """Cancel A and cancel C, fired at the same time, on a three-link same-room chain A-B-C:
    without the group lock this is a genuine lost update — each cancel's `recompute` reads
    the *other* outer link as still confirmed (its cancel hasn't committed yet), so each only
    widens the one edge of the surviving middle link B it believes changed, and whichever
    commits second overwrites the first's write with its own stale, single-edge value. The
    group lock serialises them — whichever order the locks land in, B ends up with *both*
    edges correctly restored — and neither request waits forever on the other (a deadlock
    would hang this test)."""
    ana, _ben = await two_providers(client)
    room = await make_resource(client, "space", "Room 1")
    a = await make_service(
        client,
        [ana],
        name="A",
        duration_minutes=30,
        buffer_after_minutes=15,
        requirements=[{"kind": "space"}],
    )
    b = await make_service(
        client,
        [ana],
        name="B",
        duration_minutes=30,
        buffer_before_minutes=20,
        buffer_after_minutes=25,
        requirements=[{"kind": "space"}],
    )
    c = await make_service(
        client,
        [ana],
        name="C",
        duration_minutes=30,
        buffer_before_minutes=10,
        requirements=[{"kind": "space"}],
    )
    booked = await book_group(
        client,
        [
            {"service_id": a, "staff_id": ana},
            {"service_id": b, "staff_id": ana},
            {"service_id": c, "staff_id": ana},
        ],
    )
    assert booked.status_code == 201, booked.text
    link_a, link_b, link_c = booked.json()["appointments"]
    # Fully waived while both neighbours occupy: B's stored period is its own raw span.
    assert (await resource_periods(link_b["id"])) == [(room, at("09:30"), at("10:00"))]

    await _slowed(monkeypatch, "_load")
    resp_a, resp_c = await _concurrent_requests(
        client,
        ("POST", f"{APPOINTMENTS}/{link_a['id']}/cancel", {}),
        ("POST", f"{APPOINTMENTS}/{link_c['id']}/cancel", {}),
    )

    assert resp_a.status_code == 200, resp_a.text
    assert resp_c.status_code == 200, resp_c.text
    # B is the only survivor; nothing occupies either of its edges any more, so both its own
    # buffers are back in full — regardless of which cancel's lock landed first.
    periods = await resource_periods(link_b["id"])
    assert periods == [(room, at("09:10"), at("10:25"))]


async def test_a_cancel_racing_a_move_of_a_different_member_does_not_race(client, monkeypatch):
    """Cancel A and move B away, fired at the same time, on a two-link same-staff chain: the
    group lock serialises them regardless of which lock wins first, and both requests finish
    (no 500) with a final state — A cancelled, B moved — that is internally consistent."""
    ana, _ben = await two_providers(client)
    facial = await make_service(
        client, [ana], name="Facial", duration_minutes=60, buffer_after_minutes=15
    )
    addon = await make_service(
        client, [ana], name="Hot Stone Add-on", duration_minutes=30, buffer_before_minutes=15
    )
    booked = await book_group(
        client, [{"service_id": facial, "staff_id": ana}, {"service_id": addon, "staff_id": ana}]
    )
    assert booked.status_code == 201, booked.text
    link_a, link_b = booked.json()["appointments"]

    await _slowed(monkeypatch, "_load")
    cancel_resp, move_resp = await _concurrent_requests(
        client,
        ("POST", f"{APPOINTMENTS}/{link_a['id']}/cancel", {}),
        ("PATCH", f"{APPOINTMENTS}/{link_b['id']}", {"starts_at": at("14:00")}),
    )

    assert cancel_resp.status_code == 200, cancel_resp.text
    assert move_resp.status_code == 200, move_resp.text
    assert (await find(client, link_a["id"]))["status"] == "cancelled"
    moved = await find(client, link_b["id"])
    assert moved["status"] == "confirmed"
    assert moved["starts_at"] == at("14:00")


async def test_cancelling_a_link_that_a_real_outsider_has_since_taken_never_fails(client):
    """The widen half's savepoint fallback (fix round 2), with a genuine occupying outsider
    rather than a hypothetical one: cancelling B can't spring A's after-buffer all the way
    back when a real booking now legitimately sits in part of that space — the cancel still
    succeeds, A's period stays at its current, trimmed value, and the outsider is untouched."""
    ana, ben = await two_providers(client)
    room = await make_resource(client, "space", "Room 1")
    a = await make_service(
        client,
        [ana],
        name="A",
        duration_minutes=30,
        buffer_after_minutes=60,
        requirements=[{"kind": "space"}],
    )
    b = await make_service(
        client, [ana], name="B", duration_minutes=30, requirements=[{"kind": "space"}]
    )
    booked = await book_group(
        client, [{"service_id": a, "staff_id": ana}, {"service_id": b, "staff_id": ana}]
    )
    assert booked.status_code == 201, booked.text
    link_a, link_b = booked.json()["appointments"]
    assert link_b["ends_at"] == at("10:00")

    outsider_service = await make_service(
        client, [ben], name="Outsider", duration_minutes=30, requirements=[{"kind": "space"}]
    )
    outsider = await book(client, outsider_service, ben, at("10:00"))
    assert outsider.status_code == 201, outsider.text

    cancel = await client.post(f"{APPOINTMENTS}/{link_b['id']}/cancel", json={})
    assert cancel.status_code == 200, cancel.text

    # A's after-buffer would want to spring back to the full 60 minutes, but the outsider now
    # legitimately holds part of that space — the widen is refused, silently, and A keeps the
    # trimmed period it already had.
    periods = await resource_periods(link_a["id"])
    assert periods == [(room, at("09:00"), at("09:30"))]

    still_there = await find(client, outsider.json()["id"])
    assert still_there["status"] == "confirmed"
    assert still_there["resources"] == [{"id": room, "name": "Room 1", "kind": "space"}]

"""S1: the one read the calendar draws from, `GET /api/schedule`.

Everything the grid needs for a range of business-local days, in one call: the roster with
its colours, each person's working blocks on each date **as UTC instants** (converted the one
way `scheduling/clock.py` converts), time off, closures, and the appointments in the Task 15
listing shape. The tests pin the keys and the conversion, the `staff_id` filter, the range
rules it shares with the other date-ranged reads, and that `schedule.view` — which every
staff member holds — is enough to see one's own hours.
"""

from datetime import timedelta

from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    APPOINTMENTS,
    EMAIL,
    MONDAY,
    OTHER_PASSWORD,
    STAFF,
    TUESDAY,
    add_colleague,
    as_admin,
    as_staff,
    at,
    book,
    claimed_instance,
    make_resource,
    make_service,
    me_staff_id,
    put_hours,
)

SCHEDULE = "/api/schedule"
WEDNESDAY = MONDAY + timedelta(days=2)


async def read(client, from_, to, **params):
    return await client.get(
        SCHEDULE, params={"from": from_.isoformat(), "to": to.isoformat(), **params}
    )


async def test_one_read_carries_roster_blocks_absences_closures_and_appointments(client):
    await as_admin(client)
    me = await me_staff_id(client)
    rae = await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)
    # A split shift for me on Monday, a plain one for Rae on Monday and Tuesday.
    await put_hours(client, me, [(0, 540, 720), (0, 780, 1020)])
    await put_hours(client, rae, [(0, 600, 840), (1, 600, 840)])
    off_all_day = await client.post(
        f"/api/staff/{rae}/time-off",
        json={"all_day": True, "start_date": TUESDAY.isoformat(), "reason": "Dentist"},
    )
    assert off_all_day.status_code == 201, off_all_day.text
    off_timed = await client.post(
        f"/api/staff/{me}/time-off",
        json={
            "all_day": False,
            "starts_at_local": f"{MONDAY.isoformat()}T11:00:00",
            "ends_at_local": f"{MONDAY.isoformat()}T12:00:00",
        },
    )
    assert off_timed.status_code == 201, off_timed.text
    closed = await client.post(
        "/api/admin/closures", json={"date": WEDNESDAY.isoformat(), "name": "Retreat"}
    )
    assert closed.status_code == 201, closed.text
    room = await make_resource(client, "space", "Room 1")
    service = await make_service(client, [me, rae], requirements=[{"kind": "space"}])
    booked = await book(client, service, rae, at("10:00"))
    assert booked.status_code == 201, booked.text

    resp = await read(client, MONDAY, WEDNESDAY)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {
        "timezone",
        "granularity_minutes",
        "staff",
        "working_blocks",
        "time_off",
        "closures",
        "appointments",
    }
    assert body["timezone"] == "America/Toronto"
    assert body["granularity_minutes"] == 15
    assert [s["id"] for s in body["staff"]] == [me, rae]
    assert set(body["staff"][0]) == {
        "id",
        "display_name",
        "colour",
        "hex",
        "dark_hex",
        "max_concurrent_appointments",
    }
    assert body["staff"][0]["max_concurrent_appointments"] == 1
    assert body["staff"][0]["hex"].startswith("#") and body["staff"][0]["dark_hex"].startswith("#")

    # The blocks are instants, converted from the local rule for each date — never stored.
    def block(staff_id, day, start, end):
        return {
            "staff_id": staff_id,
            "date": day.isoformat(),
            "starts_at": at(start, day),
            "ends_at": at(end, day),
        }

    assert body["working_blocks"] == [
        block(me, MONDAY, "09:00", "12:00"),
        block(me, MONDAY, "13:00", "17:00"),
        block(rae, MONDAY, "10:00", "14:00"),
        block(rae, TUESDAY, "10:00", "14:00"),
    ]
    absences = [
        (t["staff_id"], t["all_day"], t["starts_at"], t["ends_at"], t["reason"])
        for t in body["time_off"]
    ]
    assert absences == [
        (me, False, at("11:00"), at("12:00"), None),
        (rae, True, at("00:00", TUESDAY), at("00:00", WEDNESDAY), "Dentist"),
    ]
    assert set(body["time_off"][0]) == {
        "id",
        "staff_id",
        "all_day",
        "reason",
        "starts_at",
        "ends_at",
    }
    closures = [(c["date"], c["name"]) for c in body["closures"]]
    assert closures == [(WEDNESDAY.isoformat(), "Retreat")]
    assert set(body["closures"][0]) == {"id", "date", "name"}
    (item,) = body["appointments"]
    assert item["id"] == booked.json()["id"]
    assert item["resources"] == [{"id": room, "name": "Room 1", "kind": "space"}]
    # The Task 15 shape, key for key.
    assert set(item) == set(booked.json())


async def test_staff_id_narrows_every_list_to_one_column(client):
    await as_admin(client)
    me = await me_staff_id(client)
    rae = await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)
    await put_hours(client, me, [(0, 540, 720)])
    await put_hours(client, rae, [(0, 540, 720)])
    off = await client.post(
        f"/api/staff/{me}/time-off", json={"all_day": True, "start_date": TUESDAY.isoformat()}
    )
    assert off.status_code == 201, off.text
    service = await make_service(client, [me, rae])
    for who in (me, rae):
        made = await book(client, service, who, at("10:00"))
        assert made.status_code == 201, made.text

    resp = await read(client, MONDAY, TUESDAY, staff_id=rae)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [s["id"] for s in body["staff"]] == [rae]
    assert {b["staff_id"] for b in body["working_blocks"]} == {rae}
    assert body["time_off"] == []
    assert [a["staff"]["id"] for a in body["appointments"]] == [rae]


async def test_a_deactivated_staff_members_booked_appointments_keep_their_column(client):
    """Deactivating somebody does not cancel what they were booked for, and a card with no
    column is a client nobody at the desk can see, move or cancel (fix wave, finding 9). The
    deactivate response says how many are still out there, so the confirm can warn."""
    await as_admin(client)
    me = await me_staff_id(client)
    rae = await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)
    await put_hours(client, me, [(0, 540, 720)])
    await put_hours(client, rae, [(0, 540, 720)])
    service = await make_service(client, [me, rae])
    booked = await book(client, service, rae, at("10:00"))
    assert booked.status_code == 201, booked.text

    gone = await client.post(f"{STAFF}/{rae}/deactivate", json={})
    assert gone.status_code == 200, gone.text
    assert gone.json()["future_appointments"] == 1

    resp = await read(client, MONDAY, MONDAY)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert rae in [s["id"] for s in body["staff"]]
    assert booked.json()["id"] in [a["id"] for a in body["appointments"]]
    # Nothing else changed: they have no shift any more, so no shading is drawn for them.
    assert rae not in {b["staff_id"] for b in body["working_blocks"]}


async def test_a_deactivated_staff_member_with_nothing_booked_keeps_no_column(client):
    await as_admin(client)
    me = await me_staff_id(client)
    rae = await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)
    await put_hours(client, me, [(0, 540, 720)])

    gone = await client.post(f"{STAFF}/{rae}/deactivate", json={})
    assert gone.status_code == 200, gone.text
    assert gone.json()["future_appointments"] == 0

    resp = await read(client, MONDAY, MONDAY)

    assert [s["id"] for s in resp.json()["staff"]] == [me]


async def test_the_range_rules_are_the_ones_every_dated_read_shares(client):
    await as_admin(client)

    month = await read(client, MONDAY, MONDAY + timedelta(days=30))
    more = await read(client, MONDAY, MONDAY + timedelta(days=31))
    backwards = await read(client, MONDAY, MONDAY - timedelta(days=1))

    assert month.status_code == 200, month.text
    assert more.status_code == 422, more.text
    assert backwards.status_code == 422, backwards.text


async def test_schedule_view_is_enough_so_staff_see_their_own_hours(client):
    await as_admin(client)
    me = await me_staff_id(client)
    rae = await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)
    await put_hours(client, rae, [(0, 540, 720)])
    client.cookies.clear()

    anonymous = await read(client, MONDAY, MONDAY)
    await as_staff(client, "rae@cedar.example", OTHER_PASSWORD)
    mine = await read(client, MONDAY, MONDAY)
    # `/admin/staff/{id}/hours` still needs `users.manage`; this is the door that opened.
    behind_admin = await client.get(f"{STAFF}/{rae}/hours")

    assert anonymous.status_code == 401
    assert mine.status_code == 200, mine.text
    assert [b["staff_id"] for b in mine.json()["working_blocks"]] == [rae]
    assert [s["id"] for s in mine.json()["staff"]] == [me, rae]
    assert behind_admin.status_code == 403, behind_admin.text

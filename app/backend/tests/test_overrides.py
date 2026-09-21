"""S1: availability overrides and staff concurrency, over HTTP (tech-stack §20, §22).

**Advisory versus physical.** A start the engine does not offer is diagnosed: if lifting the
shift, time off, closures and the horizon would offer it, the answer is 422
`override_available` naming which of those rules it breaks; if not — a busy room, a busy
device, a staff member at their limit — the answer stays `not_offered`, and no override
exists. `override: true` books past the advisory rules, records who authorized it, and is
a no-op when nothing needed overriding.

**Who may.** `schedule.override_availability` for one's own schedule; `admin` in Admin Mode
on top for anybody else's. The seeded Staff role deliberately does not hold the capability
(migration 0006), so a role that does is created here.

**Concurrency.** Two overlapping bookings for one person at limit 2, a third refused; one at
limit 1; and a room never overlapping whatever the limit says.
"""

from datetime import datetime

from sqlalchemy import text

from core.db import session_scope
from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    APPOINTMENTS,
    EMAIL,
    MONDAY,
    OTHER_PASSWORD,
    PASSWORD,
    STAFF,
    TUESDAY,
    add_colleague,
    as_admin,
    as_staff,
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
    slots_on,
)

OVERRIDER = [
    "schedule.view",
    "schedule.manage",
    "customers.manage",
    "schedule.override_availability",
]
BOOKER = ["schedule.view", "schedule.manage", "customers.manage"]

B = {"first_name": "B", "last_name": "Two"}
C = {"first_name": "C", "last_name": "Three"}
RAE = "rae@cedar.example"
DESK = "desk@cedar.example"


async def make_role(client, name: str, capabilities: list[str]) -> None:
    resp = await client.post(
        "/api/admin/roles",
        json={"name": name, "description": name, "capabilities": capabilities},
    )
    assert resp.status_code == 201, resp.text


async def rae_and_me(client, *, role_capabilities=OVERRIDER, my_hours=((0, 540, 720),)):
    """Signed in as the admin: my hours set, a colleague Rae on a role with `role_capabilities`,
    and a plain service either of us can deliver. Returns (me, rae, service)."""
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, list(my_hours))
    await make_role(client, "Overrider", role_capabilities)
    rae = await add_colleague(client, RAE, OTHER_PASSWORD, role="Overrider")
    await put_hours(client, rae, [(0, 540, 720)])
    service = await make_service(client, [me, rae])
    return me, rae, service


def rules_of(resp) -> list[str]:
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "override_available"
    assert body["detail"][0]["loc"] == ["body", "starts_at"]
    return body["rules"]


# --- the diagnosis --------------------------------------------------------------------------


async def test_a_start_past_the_shift_end_is_override_available_naming_outside_shift(client):
    me, _, service = await rae_and_me(client)

    resp = await book(client, service, me, at("11:30"))

    assert rules_of(resp) == ["outside_shift"]
    async with session_scope() as db:
        assert await db.scalar(text("SELECT count(*) FROM appointments")) == 0
        # The inline customer that came with it was never created either.
        assert await db.scalar(text("SELECT count(*) FROM customers")) == 0


async def test_a_start_on_time_off_names_time_off(client):
    me, _, service = await rae_and_me(client, my_hours=[(0, 540, 1020)])
    away = await client.post(
        f"/api/staff/{me}/time-off",
        json={
            "all_day": False,
            "starts_at_local": f"{MONDAY.isoformat()}T13:00:00",
            "ends_at_local": f"{MONDAY.isoformat()}T14:00:00",
        },
    )
    assert away.status_code == 201, away.text

    assert rules_of(await book(client, service, me, at("13:30"))) == ["time_off"]
    # 14:00 starts as the absence ends: offered, no override in sight.
    assert (await book(client, service, me, at("14:00"))).status_code == 201


async def test_a_start_on_a_closure_names_closure(client):
    me, _, service = await rae_and_me(client)
    closed = await client.post(
        "/api/admin/closures", json={"date": MONDAY.isoformat(), "name": "Retreat"}
    )
    assert closed.status_code == 201, closed.text

    assert rules_of(await book(client, service, me, at("10:00"))) == ["closure"]


async def test_a_start_past_the_horizon_names_beyond_horizon(client):
    me, _, service = await rae_and_me(client)
    profile = await client.get("/api/admin/business")
    horizon = await client.put(
        "/api/admin/business", json={**profile.json(), "booking_horizon_days": 1}
    )
    assert horizon.status_code == 200, horizon.text

    assert rules_of(await book(client, service, me, at("10:00"))) == ["beyond_horizon"]


async def test_every_rule_broken_is_named_together(client):
    me, _, service = await rae_and_me(client)
    away = await client.post(
        f"/api/staff/{me}/time-off",
        json={"all_day": True, "start_date": MONDAY.isoformat(), "reason": "Away"},
    )
    assert away.status_code == 201, away.text
    closed = await client.post(
        "/api/admin/closures", json={"date": MONDAY.isoformat(), "name": "Retreat"}
    )
    assert closed.status_code == 201, closed.text

    assert rules_of(await book(client, service, me, at("14:00"))) == [
        "outside_shift",
        "time_off",
        "closure",
    ]


async def test_a_physical_refusal_is_not_offered_with_or_without_override(client):
    """A busy room is nobody's to override: not diagnosed as overridable, and `override:
    true` changes nothing — the same 422 `not_offered`, with no rules beside it."""
    me, _, _ = await rae_and_me(client, my_hours=[(0, 540, 1020)])
    await make_resource(client, "space", "Room 1")
    service = await make_service(client, [me], requirements=[{"kind": "space"}], name="Hot Stone")
    first = await book(client, service, me, at("10:00"))
    assert first.status_code == 201, first.text

    plain = await book(client, service, me, at("10:30"))
    forced = await book(client, service, me, at("10:30"), override=True, override_reason="Please")

    for resp in (plain, forced):
        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "not_offered"
        assert "rules" not in resp.json()
    # Past the shift *and* into the busy room: still physical, still not offered.
    late = await book(client, service, me, at("17:30"))
    assert rules_of(late) == ["outside_shift"]
    async with session_scope() as db:
        await db.execute(
            text("UPDATE appointments SET starts_at = :s, ends_at = :e"),
            {"s": datetime.fromisoformat(at("17:00")), "e": datetime.fromisoformat(at("18:00"))},
        )
        await db.execute(
            text("UPDATE appointment_resources SET period = tstzrange(:s, :e, '[)')"),
            {"s": datetime.fromisoformat(at("17:00")), "e": datetime.fromisoformat(at("18:00"))},
        )
        await db.commit()
    assert (await book(client, service, me, at("17:30"))).json()["code"] == "not_offered"


async def test_any_available_is_never_diagnosed_and_never_overridden(client):
    """The rules and the authorization are about one named person. "Any" past the shift is
    simply not offered; "any" with `override` is refused for naming nobody."""
    me, _, service = await rae_and_me(client)

    plain = await book(client, service, None, at("11:30"))
    forced = await book(client, service, None, at("11:30"), override=True)

    assert plain.status_code == 422 and plain.json()["code"] == "not_offered"
    assert forced.status_code == 422, forced.text
    assert forced.json()["detail"][0]["loc"] == ["body", "staff_id"]


# --- the override ---------------------------------------------------------------------------


async def test_staff_may_override_their_own_schedule_and_it_is_recorded(client):
    me, rae, service = await rae_and_me(client)
    customer = await make_customer(client)
    client.cookies.clear()
    await as_staff(client, RAE, OTHER_PASSWORD)

    refused = await book(client, service, rae, at("11:30"), customer_id=customer)
    assert rules_of(refused) == ["outside_shift"]
    booked = await book(
        client,
        service,
        rae,
        at("11:30"),
        customer_id=customer,
        override=True,
        override_reason="Client asked, I'm happy to stay",
    )

    assert booked.status_code == 201, booked.text
    body = booked.json()
    assert body["starts_at"] == at("11:30") and body["ends_at"] == at("12:30")
    assert body["overridden_rules"] == ["outside_shift"]
    assert body["override_reason"] == "Client asked, I'm happy to stay"
    async with session_scope() as db:
        rae_user = await db.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": RAE})
    events = [e for e in await audit_events() if e[0] == "appointment.availability_overridden"]
    assert events == [
        (
            "appointment.availability_overridden",
            "appointment",
            body["id"],
            {
                "appointment_id": body["id"],
                "rules": ["outside_shift"],
                "reason": "Client asked, I'm happy to stay",
                "authorizer_user_id": str(rae_user),
            },
        )
    ]
    # The calendar's reads carry the marker too.
    listed = await client.get(
        APPOINTMENTS, params={"from": MONDAY.isoformat(), "to": MONDAY.isoformat()}
    )
    assert listed.json()["appointments"][0]["overridden_rules"] == ["outside_shift"]
    # And the engine now counts the appointment as busy like any other.
    assert "11:00" not in await slots_on(client, service, staff_id=rae)


async def test_override_without_the_capability_is_refused_at_the_api(client):
    me, rae, service = await rae_and_me(client, role_capabilities=BOOKER)
    client.cookies.clear()
    await as_staff(client, RAE, OTHER_PASSWORD)

    # Diagnosed all the same — the screen tells them who can.
    assert rules_of(await book(client, service, rae, at("11:30"))) == ["outside_shift"]
    forced = await book(client, service, rae, at("11:30"), override=True)

    assert forced.status_code == 403, forced.text
    assert forced.json()["code"] == "capability_required"
    async with session_scope() as db:
        assert await db.scalar(text("SELECT count(*) FROM appointments")) == 0


async def test_another_persons_schedule_takes_admin_in_admin_mode(client):
    me, rae, service = await rae_and_me(client)
    customer = await make_customer(client)

    # Rae holds the capability but not `admin`: her own evening, not mine.
    client.cookies.clear()
    await as_staff(client, RAE, OTHER_PASSWORD)
    as_rae = await book(client, service, me, at("11:30"), customer_id=customer, override=True)
    assert as_rae.status_code == 403, as_rae.text
    assert as_rae.json()["code"] == "capability_required"

    # The administrator in Staff Mode: the window is what is missing.
    client.cookies.clear()
    await as_staff(client, EMAIL, PASSWORD)
    staff_mode = await book(client, service, rae, at("11:30"), customer_id=customer, override=True)
    assert staff_mode.status_code == 403, staff_mode.text
    assert staff_mode.json()["code"] == "admin_mode_required"

    # In Admin Mode, anybody's.
    client.cookies.clear()
    await as_admin(client)
    admin_mode = await book(client, service, rae, at("11:30"), customer_id=customer, override=True)
    assert admin_mode.status_code == 201, admin_mode.text
    assert admin_mode.json()["staff"]["id"] == rae
    async with session_scope() as db:
        my_user = await db.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": EMAIL})
    events = [e for e in await audit_events() if e[0] == "appointment.availability_overridden"]
    assert len(events) == 1 and events[0][3]["authorizer_user_id"] == str(my_user)


async def test_override_on_a_start_that_needed_none_is_a_no_op(client):
    me, _, service = await rae_and_me(client)

    booked = await book(
        client, service, me, at("10:00"), override=True, override_reason="Just in case"
    )

    assert booked.status_code == 201, booked.text
    assert booked.json()["overridden_rules"] is None
    assert booked.json()["override_reason"] is None
    assert not [e for e in await audit_events() if e[0] == "appointment.availability_overridden"]


async def test_the_engine_never_relaxes_for_availability(client):
    """`/api/availability` is what the booking screen and, later, the portal read. The
    switch exists for the diagnosis and the override and reaches nothing else."""
    me, _, service = await rae_and_me(client)

    assert await slots_on(client, service, staff_id=me) == [
        "09:00",
        "09:15",
        "09:30",
        "09:45",
        "10:00",
        "10:15",
        "10:30",
        "10:45",
        "11:00",
    ]


# --- moving and resizing: the same rule ------------------------------------------------------


async def test_a_move_past_the_shift_end_is_diagnosed_overridden_and_cleared_when_moved_back(
    client,
):
    me, rae, service = await rae_and_me(client)
    booked = await book(client, service, me, at("10:00"))
    assert booked.status_code == 201, booked.text
    appointment = booked.json()["id"]

    refused = await move(client, appointment, starts_at=at("11:30"))
    assert rules_of(refused) == ["outside_shift"]
    # A resize that runs past the end is the same question.
    assert rules_of(await move(client, appointment, duration_minutes=150)) == ["outside_shift"]

    moved = await move(
        client, appointment, starts_at=at("11:30"), override=True, override_reason="Running late"
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["starts_at"] == at("11:30")
    assert moved.json()["overridden_rules"] == ["outside_shift"]
    assert moved.json()["override_reason"] == "Running late"
    events = [e for e in await audit_events() if e[0] == "appointment.availability_overridden"]
    assert len(events) == 1 and events[0][3]["rules"] == ["outside_shift"]
    assert events[0][3]["reason"] == "Running late"

    # Back inside the shift, no override needed — and the marker comes off.
    back = await move(client, appointment, starts_at=at("09:00"))
    assert back.status_code == 200, back.text
    assert back.json()["overridden_rules"] is None
    assert back.json()["override_reason"] is None


async def test_a_move_of_another_persons_appointment_takes_admin_mode_to_override(client):
    me, rae, service = await rae_and_me(client)
    booked = await book(client, service, me, at("10:00"))
    appointment = booked.json()["id"]
    client.cookies.clear()
    await as_staff(client, RAE, OTHER_PASSWORD)

    refused = await move(client, appointment, starts_at=at("11:30"), override=True)

    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "capability_required"
    still = await client.get(
        APPOINTMENTS, params={"from": MONDAY.isoformat(), "to": MONDAY.isoformat()}
    )
    assert still.json()["appointments"][0]["starts_at"] == at("10:00")


async def test_a_move_onto_a_taken_room_stays_not_offered_under_override(client):
    me, _, _ = await rae_and_me(client, my_hours=[(0, 540, 1020)])
    await make_resource(client, "space", "Room 1")
    service = await make_service(client, [me], requirements=[{"kind": "space"}], name="Hot Stone")
    first = await book(client, service, me, at("10:00"))
    second = await book(client, service, me, at("14:00"))
    assert first.status_code == 201 and second.status_code == 201

    resp = await move(client, second.json()["id"], starts_at=at("10:30"), override=True)

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "not_offered"


# --- concurrency, end to end -------------------------------------------------------------------


async def set_limit(client, staff_id: str, limit: int) -> None:
    resp = await client.patch(f"{STAFF}/{staff_id}", json={"max_concurrent_appointments": limit})
    assert resp.status_code == 200, resp.text


async def test_limit_two_books_a_second_overlapping_appointment_and_refuses_a_third(
    client, monkeypatch
):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    await set_limit(client, me, 2)
    service = await make_service(client, [me])

    first = await book(client, service, me, at("10:00"))
    assert first.status_code == 201, first.text
    # Still on offer with one chair taken.
    assert "10:00" in await slots_on(client, service) and "10:30" in await slots_on(client, service)
    second = await book(client, service, me, at("10:30"), customer=B)
    assert second.status_code == 201, second.text
    # Both chairs are taken 10:30–11:00: the engine stops offering anything overlapping it.
    offered = await slots_on(client, service)
    assert "10:00" not in offered and "10:30" not in offered and "09:45" not in offered
    assert "11:00" in offered and "09:30" in offered  # 09:30 ends as the second chair fills
    third = await book(client, service, me, at("10:45"), customer=C)
    assert third.status_code == 422 and third.json()["code"] == "not_offered"
    # And should the engine's picture be stale, the trigger is what refuses.
    from scheduling import slots

    async def nothing_busy(db, staff_ids, resource_ids, window, **kwargs):
        return {}, {}

    monkeypatch.setattr(slots, "busy_intervals", nothing_busy)
    stale = await book(client, service, me, at("10:45"), customer=C)
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "slot_taken"
    async with session_scope() as db:
        assert await db.scalar(text("SELECT count(*) FROM appointments")) == 2


async def test_limit_one_refuses_the_second(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    await set_limit(client, me, 1)
    service = await make_service(client, [me])

    first = await book(client, service, me, at("10:00"))
    second = await book(client, service, me, at("10:30"), customer=B)

    assert first.status_code == 201, first.text
    assert second.status_code == 422 and second.json()["code"] == "not_offered"
    # Not an override either: the limit is physical.
    forced = await book(client, service, me, at("10:30"), customer=B, override=True)
    assert forced.status_code == 422 and forced.json()["code"] == "not_offered"


async def test_a_room_never_overlaps_whatever_the_limit(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    await set_limit(client, me, 3)
    await make_resource(client, "space", "Room 1")
    service = await make_service(client, [me], requirements=[{"kind": "space"}])

    first = await book(client, service, me, at("10:00"))
    second = await book(client, service, me, at("10:30"), customer=B)

    assert first.status_code == 201, first.text
    assert second.status_code == 422 and second.json()["code"] == "not_offered"
    # A second room, and the second chair is usable again.
    await make_resource(client, "space", "Room 2")
    third = await book(client, service, me, at("10:30"), customer=B)
    assert third.status_code == 201, third.text
    assert third.json()["resources"][0]["name"] == "Room 2"


async def test_the_schedule_roster_carries_the_limit_and_the_staff_roster_the_account(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await set_limit(client, me, 2)
    async with session_scope() as db:
        my_user = await db.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": EMAIL})

    schedule = await client.get(
        "/api/schedule", params={"from": MONDAY.isoformat(), "to": TUESDAY.isoformat()}
    )
    roster = await client.get("/api/staff")

    assert schedule.status_code == 200, schedule.text
    assert schedule.json()["staff"][0]["max_concurrent_appointments"] == 2
    assert roster.status_code == 200, roster.text
    # Which column is *mine* is how the screen knows whose evening it is committing.
    assert roster.json()["staff"][0]["user_id"] == str(my_user)

"""S1 and S5: booking an appointment end to end, and the database rules underneath it.

The engine (`test_availability.py`) and its loader (`test_slots.py`) already say what is
offered; this file proves that what is offered can be booked, that what is booked is stored
with the service snapshotted onto it and its resources claimed, that the engine then stops
offering the slot, and that the database — not the handler — is what refuses a double booking.

Dates are chosen relative to today because the engine drops slots that have already begun.
"""

import asyncio
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from core.db import get_purge_engine, session_scope
from scheduling.clock import localize

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"
OTHER_PASSWORD = "correct horse battery 2"

SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": EMAIL,
    "admin_password": PASSWORD,
}
TORONTO = ZoneInfo("America/Toronto")

STAFF = "/api/admin/staff"
SERVICES = "/api/admin/services"
RESOURCES = "/api/admin/resources"
AVAILABILITY = "/api/availability"
APPOINTMENTS = "/api/appointments"
CUSTOMERS = "/api/customers"

ADDRESSES = (EMAIL, "rae@cedar.example", "desk@cedar.example")

# The next Monday at least a week out — see test_slots.py.
MONDAY = date.today() + timedelta(days=7 + (7 - date.today().weekday()) % 7)
TUESDAY = MONDAY + timedelta(days=1)

CUSTOMER = {"first_name": "Priya", "last_name": "Nair", "phone": "416-555-0199"}


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async def wipe():
        async with get_purge_engine().begin() as purge:
            await purge.execute(text("DELETE FROM audit_events"))
        async with session_scope() as db:
            for table in (
                "appointment_resources",
                "appointments",
                "customers",
                "service_requirements",
                "service_staff",
                "services",
                "working_hours",
                "time_off",
                "closures",
                "resources",
                "staff",
                "password_reset_tokens",
                "users",
                "businesses",
                "setup_token",
            ):
                await db.execute(text(f"DELETE FROM {table}"))
            await db.execute(text("DELETE FROM roles WHERE NOT is_system"))
            await db.commit()

    await wipe()
    from auth import setup, throttle
    from core.redis import get_redis

    await get_redis().delete(*(throttle.delay_key(e) for e in ADDRESSES))

    async with session_scope() as db:
        token = await setup.bootstrap_setup_token(db)
    resp = await client.post("/api/setup", json={**SETUP, "token": token})
    assert resp.status_code == 201, resp.text
    async with session_scope() as db:
        await db.execute(text("UPDATE businesses SET mfa_required_for_admin = false"))
        await db.commit()
    client.cookies.clear()
    yield
    # Appointments point at staff, services and customers without cascading — history must
    # never lose its author — so they have to be gone before another file's fixture deletes
    # what they point at.
    await wipe()


# --- helpers --------------------------------------------------------------------------------


async def as_admin(client, email=EMAIL, password=PASSWORD):
    login = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    resp = await client.post("/api/auth/mode", json={"mode": "admin", "password": password})
    assert resp.status_code == 200, resp.text


async def as_staff(client, email, password):
    login = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text


async def me_staff_id(client) -> str:
    roster = await client.get(STAFF)
    assert roster.status_code == 200, roster.text
    return next(row["id"] for row in roster.json()["staff"] if row["email"] == EMAIL)


async def add_colleague(client, email: str, password: str, *, role: str = "Staff") -> str:
    from core.security import hash_password
    from tests.conftest import add_account

    async with session_scope() as db:
        role_id = str(await db.scalar(text("SELECT id FROM roles WHERE name = :n"), {"n": role}))
    await add_account(email, await hash_password(password), role=role_id)
    roster = await client.get(STAFF)
    return next(row["id"] for row in roster.json()["staff"] if row["email"] == email)


async def put_hours(client, staff_id: str, blocks: list[tuple[int, int, int]]):
    resp = await client.put(
        f"{STAFF}/{staff_id}/hours",
        json={"blocks": [{"weekday": w, "start_minute": s, "end_minute": e} for w, s, e in blocks]},
    )
    assert resp.status_code == 200, resp.text


async def make_service(client, staff_ids: list[str], requirements=None, **overrides) -> str:
    body = {"name": "Swedish Massage", "duration_minutes": 60, "price_cents": 12000}
    body.update(overrides)
    created = await client.post(SERVICES, json=body)
    assert created.status_code == 201, created.text
    service_id = created.json()["id"]
    linked = await client.put(f"{SERVICES}/{service_id}/staff", json={"staff_ids": staff_ids})
    assert linked.status_code == 200, linked.text
    if requirements:
        needs = await client.put(
            f"{SERVICES}/{service_id}/requirements", json={"requirements": requirements}
        )
        assert needs.status_code == 200, needs.text
    return service_id


async def make_resource(client, kind: str, name: str, **overrides) -> str:
    resp = await client.post(RESOURCES, json={"kind": kind, "name": name, **overrides})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def make_customer(client) -> str:
    resp = await client.post(CUSTOMERS, json=CUSTOMER)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def at(hhmm: str, day: date = MONDAY) -> str:
    """A business-local wall-clock time on `day`, as the UTC `...Z` instant the API speaks."""
    hour, minute = map(int, hhmm.split(":"))
    instant = localize(datetime.combine(day, time(hour, minute)), TORONTO)
    return instant.isoformat().replace("+00:00", "Z")


async def book(client, service_id: str, staff_id: str | None, starts_at: str, **extra):
    body = {"service_id": service_id, "staff_id": staff_id, "starts_at": starts_at, **extra}
    if "customer" not in body and "customer_id" not in body:
        body["customer"] = CUSTOMER
    return await client.post(APPOINTMENTS, json=body)


async def slots_on(client, service_id: str, day: date = MONDAY, **params) -> list[str]:
    query = {"service_id": service_id, "from": day.isoformat(), "to": day.isoformat(), **params}
    resp = await client.get(AVAILABILITY, params=query)
    assert resp.status_code == 200, resp.text
    return [
        datetime.fromisoformat(slot["starts_at"]).astimezone(TORONTO).strftime("%H:%M")
        for slot in resp.json()["days"][0]["slots"]
    ]


async def audit_events() -> list[tuple[str, str, str | None, dict]]:
    """This ticket's events, in order — sign-ins and mode switches are logged too."""
    async with get_purge_engine().connect() as purge:
        rows = (
            await purge.execute(
                text(
                    "SELECT event_type, target_type, target_id, metadata FROM audit_events "
                    "WHERE event_type LIKE 'appointment.%' OR event_type LIKE 'customer.%' "
                    "ORDER BY id"
                )
            )
        ).all()
    return [(r.event_type, r.target_type, r.target_id, r.metadata) for r in rows]


def constraint_of(error: IntegrityError) -> str | None:
    """The name Postgres attached to the refusal — asyncpg keeps it on the cause."""
    for candidate in (error.orig, getattr(error.orig, "__cause__", None)):
        if getattr(candidate, "constraint_name", None):
            return candidate.constraint_name
    return None


# --- booking --------------------------------------------------------------------------------


async def test_an_offered_slot_is_booked_with_the_service_snapshotted_and_its_room_claimed(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    room = await make_resource(client, "space", "Room 1")
    service = await make_service(
        client,
        [me],
        requirements=[{"kind": "space"}],
        buffer_before_minutes=5,
        buffer_after_minutes=15,
    )
    customer = await make_customer(client)

    resp = await book(client, service, me, at("10:00"), customer_id=customer, notes="First visit")

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["starts_at"] == at("10:00")
    assert body["ends_at"] == at("11:00")
    assert body["duration_minutes"] == 60
    assert body["buffer_before_minutes"] == 5
    assert body["buffer_after_minutes"] == 15
    assert body["price_cents"] == 12000
    assert body["notes"] == "First visit"
    assert body["booking_group_id"] is None
    assert body["customer"]["id"] == customer
    assert body["customer"]["first_name"] == "Priya"
    assert body["service"] == {"id": service, "name": "Swedish Massage"}
    assert body["staff"]["id"] == me
    assert body["staff"]["colour"]
    assert body["resources"] == [{"id": room, "name": "Room 1", "kind": "space"}]

    # The resource row carries the *buffered* span, half-open — what the constraint compares.
    async with session_scope() as db:
        period = (
            await db.execute(
                text(
                    "SELECT lower(period), upper(period), lower_inc(period), upper_inc(period), "
                    "kind FROM appointment_resources WHERE resource_id = :r"
                ),
                {"r": room},
            )
        ).one()
    assert period[0] == datetime.fromisoformat(at("09:55"))
    assert period[1] == datetime.fromisoformat(at("11:15"))
    assert (period[2], period[3]) == (True, False)
    assert period[4] == "space"

    # SNAPSHOT CONTRACT: raising the price afterwards changes nothing already booked.
    raised = await client.patch(
        f"{SERVICES}/{service}", json={"price_cents": 15000, "duration_minutes": 90}
    )
    assert raised.status_code == 200, raised.text
    listed = await client.get(
        APPOINTMENTS, params={"from": MONDAY.isoformat(), "to": MONDAY.isoformat()}
    )
    assert listed.status_code == 200, listed.text
    (item,) = listed.json()["appointments"]
    assert item["id"] == body["id"]
    assert item["price_cents"] == 12000
    assert item["duration_minutes"] == 60
    assert item["ends_at"] == at("11:00")

    # Ids only in the audit log — never the notes, never a name.
    events = await audit_events()
    booked = [e for e in events if e[0] == "appointment.booked"]
    assert len(booked) == 1
    assert booked[0][1:3] == ("appointment", body["id"])
    assert booked[0][3] == {"service_id": service, "staff_id": me, "customer_id": customer}


async def test_a_slot_the_engine_does_not_offer_is_refused(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    service = await make_service(client, [me])

    before_the_shift = await book(client, service, me, at("08:00"))
    off_the_grid = await book(client, service, me, at("10:07"))
    too_late_to_finish = await book(client, service, me, at("11:15"))
    a_day_nobody_works = await book(client, service, me, at("10:00", TUESDAY))

    for resp in (before_the_shift, off_the_grid, too_late_to_finish, a_day_nobody_works):
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"][0]["loc"] == ["body", "starts_at"]
    # Off the grid is simply not a start. The other three are the shift's rule, which a
    # human may override (tests/test_overrides.py) — and the answer says so.
    assert off_the_grid.json()["code"] == "not_offered"
    for resp in (before_the_shift, too_late_to_finish, a_day_nobody_works):
        assert resp.json()["code"] == "override_available"
        assert resp.json()["rules"] == ["outside_shift"]
    async with session_scope() as db:
        assert await db.scalar(text("SELECT count(*) FROM appointments")) == 0


async def test_any_provider_takes_the_first_free_by_sort_order(client):
    await as_admin(client)
    me = await me_staff_id(client)
    rae = await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)
    for staff_id, order in ((me, 5), (rae, 1)):
        await put_hours(client, staff_id, [(0, 540, 720)])
        sorted_ = await client.patch(f"{STAFF}/{staff_id}", json={"sort_order": order})
        assert sorted_.status_code == 200, sorted_.text
    service = await make_service(client, [me, rae])

    first = await book(client, service, None, at("10:00"))
    second = await book(client, service, None, at("10:00"))
    third = await book(client, service, None, at("10:00"))

    assert first.status_code == 201, first.text
    assert first.json()["staff"]["id"] == rae
    assert second.status_code == 201, second.text
    assert second.json()["staff"]["id"] == me
    assert third.status_code == 422, third.text
    # And a named provider who is busy is refused rather than silently swapped.
    assert (await book(client, service, rae, at("10:00"))).status_code == 422


async def test_requirements_receive_distinct_rooms_and_any_never_takes_the_named_one(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    room_a = await make_resource(client, "space", "Room A", sort_order=2)
    room_b = await make_resource(client, "space", "Room B", sort_order=1)
    # "Any space" plus Room B by name. Room B is also the first "any" pick by sort order —
    # so handing the "any" requirement its favourite would leave the named one a double
    # claim the exclusion constraint refuses at commit. Named first, then the rest.
    service = await make_service(
        client,
        [me],
        requirements=[{"kind": "space"}, {"kind": "space", "resource_id": room_b}],
    )

    resp = await book(client, service, me, at("10:00"))

    assert resp.status_code == 201, resp.text
    assert [r["id"] for r in resp.json()["resources"]] == [room_b, room_a]
    # Both rooms are taken for the hour; the engine now offers nothing that overlaps it, and
    # the adjacent hour on either side, which merely touches.
    assert await slots_on(client, service) == ["09:00", "11:00"]


async def test_a_busy_named_device_refuses_the_slot_though_the_practitioner_is_free(client):
    await as_admin(client)
    me = await me_staff_id(client)
    rae = await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)
    await put_hours(client, me, [(0, 540, 720)])
    await put_hours(client, rae, [(0, 540, 720)])
    laser = await make_resource(client, "equipment", "Laser 2")
    needs_laser = [{"kind": "equipment", "resource_id": laser}]
    with_me = await make_service(client, [me], requirements=needs_laser)
    with_rae = await make_service(
        client, [rae], requirements=needs_laser, name="Laser Facial", duration_minutes=60
    )

    mine = await book(client, with_me, me, at("10:00"))
    assert mine.status_code == 201, mine.text

    # Rae is free all morning; the laser is not.
    assert await slots_on(client, with_rae) == ["09:00", "11:00"]
    refused = await book(client, with_rae, rae, at("10:00"))
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"][0]["loc"] == ["body", "starts_at"]
    adjacent = await book(client, with_rae, rae, at("11:00"))
    assert adjacent.status_code == 201, adjacent.text


async def test_after_booking_the_engine_subtracts_the_appointment_with_its_buffers(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 480, 840)])  # 08:00–14:00
    service = await make_service(client, [me], buffer_before_minutes=15, buffer_after_minutes=15)
    # Ninety minutes wide with the turnaround: 08:15 is the first start, 12:45 the last.
    assert (
        await slots_on(client, service)
        == [f"{h:02d}:{m:02d}" for h in range(8, 13) for m in (0, 15, 30, 45)][1:]
    )

    booked = await book(client, service, me, at("10:00"))
    assert booked.status_code == 201, booked.text

    # Busy from 09:45 to 11:15. Another buffered span has to sit clear of that: the last
    # morning start is 08:30 (08:15–09:45), the first afternoon one 11:30 (11:15–12:45).
    assert await slots_on(client, service) == [
        "08:15",
        "08:30",
        "11:30",
        "11:45",
        "12:00",
        "12:15",
        "12:30",
        "12:45",
    ]
    # And the buffers came from the appointment's own snapshot, not from the live service:
    # shrinking them afterwards frees nothing.
    shrunk = await client.patch(
        f"{SERVICES}/{service}", json={"buffer_before_minutes": 0, "buffer_after_minutes": 0}
    )
    assert shrunk.status_code == 200, shrunk.text
    assert "09:00" not in await slots_on(client, service)
    assert "11:00" not in await slots_on(client, service)


async def test_an_inline_customer_is_created_in_the_same_transaction_and_audited(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    service = await make_service(client, [me])

    resp = await book(
        client,
        service,
        me,
        at("10:00"),
        customer={"first_name": "Sam", "last_name": "Okonkwo", "email": "Sam@Example.com"},
    )

    assert resp.status_code == 201, resp.text
    customer = resp.json()["customer"]
    assert customer["first_name"] == "Sam"
    assert customer["email"] == "sam@example.com"
    found = await client.get(CUSTOMERS, params={"q": "oko"})
    assert [c["id"] for c in found.json()["customers"]] == [customer["id"]]
    assert [e[:3] for e in await audit_events()] == [
        ("customer.created", "customer", customer["id"]),
        ("appointment.booked", "appointment", resp.json()["id"]),
    ]

    # A refused booking creates nobody.
    refused = await book(
        client,
        service,
        me,
        at("08:00"),
        customer={"first_name": "Nobody", "last_name": "Here"},
    )
    assert refused.status_code == 422, refused.text
    assert (await client.get(CUSTOMERS, params={"q": "nobody"})).json()["customers"] == []


async def test_exactly_one_way_to_name_the_customer(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    service = await make_service(client, [me])
    customer = await make_customer(client)

    both = await client.post(
        APPOINTMENTS,
        json={
            "service_id": service,
            "staff_id": me,
            "starts_at": at("10:00"),
            "customer_id": customer,
            "customer": CUSTOMER,
        },
    )
    neither = await client.post(
        APPOINTMENTS, json={"service_id": service, "staff_id": me, "starts_at": at("10:00")}
    )
    unknown = await book(
        client, service, me, at("10:00"), customer_id="00000000-0000-0000-0000-000000000000"
    )

    assert both.status_code == 422, both.text
    assert neither.status_code == 422, neither.text
    assert unknown.status_code == 422, unknown.text
    assert unknown.json()["detail"][0]["loc"] == ["body", "customer_id"]


async def test_an_unbookable_service_is_a_409_with_reasons_and_a_wrong_provider_a_422(client):
    await as_admin(client)
    me = await me_staff_id(client)
    rae = await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)
    await put_hours(client, me, [(0, 540, 720)])
    nobody = await make_service(client, [])
    mine = await make_service(client, [me], name="Mine")

    unbookable = await book(client, nobody, None, at("10:00"))
    wrong_provider = await book(client, mine, rae, at("10:00"))

    assert unbookable.status_code == 409, unbookable.text
    assert unbookable.json()["code"] == "not_bookable"
    assert unbookable.json()["unbookable_reasons"] == ["Nobody active can deliver this."]
    assert wrong_provider.status_code == 422, wrong_provider.text
    assert wrong_provider.json()["detail"][0]["loc"] == ["body", "staff_id"]


async def test_a_stale_picture_of_the_day_is_refused_by_the_database_as_slot_taken(
    client, monkeypatch
):
    """§19: displayed slots are advisory, the constraints are authoritative. With the busy
    seam blinded, the handler believes 10:00 is free twice; the trigger says otherwise, and
    the caller is told the time was just taken rather than handed a 500."""
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    room = await make_resource(client, "space", "Room 1")
    service = await make_service(client, [me], requirements=[{"kind": "space"}])
    first = await book(client, service, me, at("10:00"))
    assert first.status_code == 201, first.text

    from scheduling import slots

    async def nothing_busy(db, staff_ids, resource_ids, window, **kwargs):
        return {}, {}

    monkeypatch.setattr(slots, "busy_intervals", nothing_busy)

    resp = await book(client, service, me, at("10:00"))

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "slot_taken"
    async with session_scope() as db:
        assert await db.scalar(text("SELECT count(*) FROM appointments")) == 1
        assert (
            await db.scalar(
                text("SELECT count(*) FROM appointment_resources WHERE resource_id = :r"),
                {"r": room},
            )
            == 1
        )
    # The customer that came with the refused booking was never created either.
    assert len((await client.get(CUSTOMERS)).json()["customers"]) == 1


async def test_two_concurrent_bookings_of_one_slot_yield_one_201_and_one_409(client, monkeypatch):
    """Two ASGI clients, one slot, no resources — so the staff trigger is the only thing
    standing between them. Both pass the engine's check before either inserts (the pause
    after it is what makes the interleaving certain rather than lucky, as in test_rbac.py);
    the database serialises the inserts and the second is told the truth."""
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    service = await make_service(client, [me])
    cookie = client.cookies["linsuite_session"]

    from main import app as main_app
    from scheduling import appointments

    real_offered = appointments.offered_slot

    async def slow_offered(*args, **kwargs):
        slot = await real_offered(*args, **kwargs)
        await asyncio.sleep(0.25)
        return slot

    monkeypatch.setattr(appointments, "offered_slot", slow_offered)

    async def attempt(customer: dict):
        async with AsyncClient(transport=ASGITransport(app=main_app), base_url="http://test") as c:
            c.cookies.set("linsuite_session", cookie)
            return await book(c, service, me, at("10:00"), customer=customer)

    results = await asyncio.gather(
        attempt({"first_name": "A", "last_name": "One"}),
        attempt({"first_name": "B", "last_name": "Two"}),
    )

    assert sorted(r.status_code for r in results) == [201, 409], [r.text for r in results]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["code"] == "slot_taken"
    async with session_scope() as db:
        assert await db.scalar(text("SELECT count(*) FROM appointments")) == 1
        assert await db.scalar(text("SELECT count(*) FROM customers")) == 1


# --- listing --------------------------------------------------------------------------------


async def test_the_list_is_by_local_date_inclusive_and_filterable_by_staff(client):
    await as_admin(client)
    me = await me_staff_id(client)
    rae = await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)
    await put_hours(client, me, [(0, 540, 720), (1, 540, 720)])
    await put_hours(client, rae, [(0, 540, 720)])
    service = await make_service(client, [me, rae])
    for staff_id, start in ((rae, at("09:00")), (me, at("10:00")), (me, at("09:00", TUESDAY))):
        made = await book(client, service, staff_id, start)
        assert made.status_code == 201, made.text

    monday = await client.get(
        APPOINTMENTS, params={"from": MONDAY.isoformat(), "to": MONDAY.isoformat()}
    )
    both_days = await client.get(
        APPOINTMENTS, params={"from": MONDAY.isoformat(), "to": TUESDAY.isoformat()}
    )
    only_me = await client.get(
        APPOINTMENTS,
        params={"from": MONDAY.isoformat(), "to": TUESDAY.isoformat(), "staff_id": me},
    )

    assert monday.status_code == 200, monday.text
    assert monday.json()["timezone"] == "America/Toronto"
    assert [(a["staff"]["id"], a["starts_at"]) for a in monday.json()["appointments"]] == [
        (rae, at("09:00")),
        (me, at("10:00")),
    ]
    assert len(both_days.json()["appointments"]) == 3
    assert [a["starts_at"] for a in only_me.json()["appointments"]] == [
        at("10:00"),
        at("09:00", TUESDAY),
    ]
    # The shape Task 16's calendar draws from. Pinned key by key so a rename is a failure here
    # rather than a blank column there.
    item = monday.json()["appointments"][0]
    assert set(item) == {
        "id",
        "status",
        "starts_at",
        "ends_at",
        "duration_minutes",
        "buffer_before_minutes",
        "buffer_after_minutes",
        "price_cents",
        "notes",
        "booking_group_id",
        "overridden_rules",
        "override_reason",
        "customer",
        "service",
        "staff",
        "resources",
    }
    assert set(item["customer"]) == {"id", "first_name", "last_name", "email", "phone"}
    assert set(item["staff"]) == {"id", "display_name", "colour"}


async def test_the_list_range_rules_match_availability(client):
    await as_admin(client)

    month = await client.get(
        APPOINTMENTS,
        params={"from": MONDAY.isoformat(), "to": (MONDAY + timedelta(days=30)).isoformat()},
    )
    more = await client.get(
        APPOINTMENTS,
        params={"from": MONDAY.isoformat(), "to": (MONDAY + timedelta(days=31)).isoformat()},
    )
    backwards = await client.get(
        APPOINTMENTS,
        params={"from": MONDAY.isoformat(), "to": (MONDAY - timedelta(days=1)).isoformat()},
    )

    assert month.status_code == 200, month.text
    assert more.status_code == 422, more.text
    assert backwards.status_code == 422, backwards.text


# --- who may --------------------------------------------------------------------------------


async def test_the_roster_every_scheduler_reads_is_active_staff_in_column_order(client):
    await as_admin(client)
    me = await me_staff_id(client)
    rae = await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)
    gone = await add_colleague(client, "desk@cedar.example", OTHER_PASSWORD)
    for staff_id, order in ((me, 5), (rae, 1)):
        ordered = await client.patch(f"{STAFF}/{staff_id}", json={"sort_order": order})
        assert ordered.status_code == 200, ordered.text
    left = await client.post(f"{STAFF}/{gone}/deactivate", json={})
    assert left.status_code == 200, left.text
    client.cookies.clear()
    await as_staff(client, "rae@cedar.example", OTHER_PASSWORD)

    resp = await client.get("/api/staff")

    assert resp.status_code == 200, resp.text
    roster = resp.json()["staff"]
    assert [row["id"] for row in roster] == [rae, me]
    assert set(roster[0]) == {
        "id",
        "display_name",
        "colour",
        "hex",
        "dark_hex",
        "sort_order",
        "user_id",
    }


async def test_booking_is_staff_work_and_schedule_view_alone_may_only_look(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    service = await make_service(client, [me])
    role = await client.post(
        "/api/admin/roles",
        json={"name": "Viewer", "description": "Looks.", "capabilities": ["schedule.view"]},
    )
    assert role.status_code == 201, role.text
    await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)  # the seeded Staff role
    await add_colleague(client, "desk@cedar.example", OTHER_PASSWORD, role="Viewer")

    client.cookies.clear()
    await as_staff(client, "rae@cedar.example", OTHER_PASSWORD)
    as_staff_member = await book(client, service, me, at("10:00"))
    assert as_staff_member.status_code == 201, as_staff_member.text

    client.cookies.clear()
    await as_staff(client, "desk@cedar.example", OTHER_PASSWORD)
    looked = await client.get(
        APPOINTMENTS, params={"from": MONDAY.isoformat(), "to": MONDAY.isoformat()}
    )
    tried = await book(client, service, me, at("11:00"))
    assert looked.status_code == 200, looked.text
    assert len(looked.json()["appointments"]) == 1
    assert tried.status_code == 403, tried.text
    assert tried.json()["code"] == "capability_required"

    client.cookies.clear()
    assert (await book(client, service, me, at("11:00"))).status_code == 401
    anonymous = await client.get(APPOINTMENTS, params={"from": "2026-06-15", "to": "2026-06-15"})
    assert anonymous.status_code == 401


async def test_creating_the_client_inline_needs_customers_manage_on_top(client):
    """A scheduler who may not add customers may still book somebody who exists."""
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    service = await make_service(client, [me])
    customer = await make_customer(client)
    role = await client.post(
        "/api/admin/roles",
        json={
            "name": "Booker",
            "description": "Books, never adds.",
            "capabilities": ["schedule.view", "schedule.manage"],
        },
    )
    assert role.status_code == 201, role.text
    await add_colleague(client, "desk@cedar.example", OTHER_PASSWORD, role="Booker")
    client.cookies.clear()
    await as_staff(client, "desk@cedar.example", OTHER_PASSWORD)

    existing = await book(client, service, me, at("10:00"), customer_id=customer)
    inline = await book(
        client, service, me, at("11:00"), customer={"first_name": "New", "last_name": "Face"}
    )

    assert existing.status_code == 201, existing.text
    assert inline.status_code == 403, inline.text
    assert inline.json()["code"] == "capability_required"
    async with session_scope() as db:
        assert await db.scalar(text("SELECT count(*) FROM appointments")) == 1
        assert await db.scalar(text("SELECT count(*) FROM customers")) == 1


# --- S5: the database's own rules ----------------------------------------------------------------


async def _seed_rows() -> tuple[str, str, str, str]:
    """A staff member, a customer, a service and a user id, straight into the tables."""
    async with session_scope() as db:
        staff_id, user_id = (await db.execute(text("SELECT id, user_id FROM staff LIMIT 1"))).one()
        customer_id = await db.scalar(
            text(
                "INSERT INTO customers (first_name, last_name) VALUES ('Direct', 'Insert') "
                "RETURNING id"
            )
        )
        service_id = await db.scalar(
            text("INSERT INTO services (name, duration_minutes) VALUES ('Direct', 60) RETURNING id")
        )
        await db.commit()
    return str(staff_id), str(customer_id), str(service_id), str(user_id)


_INSERT_APPOINTMENT = text(
    "INSERT INTO appointments (customer_id, staff_id, service_id, starts_at, ends_at, "
    "duration_minutes, buffer_before_minutes, buffer_after_minutes, price_cents, status, "
    "created_by_user_id) VALUES (:c, :s, :v, :start, :end, 60, :before, :after, 0, :status, :u) "
    "RETURNING id"
)


async def _insert_appointment(
    db, seed, start: str, end: str, *, before=0, after=0, status="confirmed"
) -> str:
    staff_id, customer_id, service_id, user_id = seed
    return str(
        await db.scalar(
            _INSERT_APPOINTMENT,
            {
                "c": customer_id,
                "s": staff_id,
                "v": service_id,
                "start": datetime.fromisoformat(start),
                "end": datetime.fromisoformat(end),
                "before": before,
                "after": after,
                "status": status,
                "u": user_id,
            },
        )
    )


async def test_s5_the_exclusion_constraint_refuses_an_overlapping_claim_on_a_resource(client):
    await as_admin(client)
    room = await make_resource(client, "space", "Room 1")
    seed = await _seed_rows()

    async with session_scope() as db:
        first = await _insert_appointment(db, seed, at("10:00"), at("11:00"))
        second = await _insert_appointment(db, seed, at("11:00"), at("12:00"), status="cancelled")
        third = await _insert_appointment(db, seed, at("10:30"), at("11:30"), status="cancelled")
        claim = text(
            "INSERT INTO appointment_resources (appointment_id, resource_id, kind, period) "
            "VALUES (:a, :r, 'space', tstzrange(:start, :end, '[)'))"
        )
        await db.execute(
            claim,
            {
                "a": first,
                "r": room,
                "start": datetime.fromisoformat(at("10:00")),
                "end": datetime.fromisoformat(at("11:00")),
            },
        )
        # Touching is not overlapping: the room turns over at 11:00 exactly.
        await db.execute(
            claim,
            {
                "a": second,
                "r": room,
                "start": datetime.fromisoformat(at("11:00")),
                "end": datetime.fromisoformat(at("12:00")),
            },
        )
        with pytest.raises(IntegrityError) as refused:
            await db.execute(
                claim,
                {
                    "a": third,
                    "r": room,
                    "start": datetime.fromisoformat(at("10:30")),
                    "end": datetime.fromisoformat(at("11:30")),
                },
            )
        assert constraint_of(refused.value) == "ex_appointment_resources_no_overlap"


@pytest.mark.parametrize("limit", [1, 2])
async def test_s5_the_trigger_enforces_max_concurrent_appointments_over_buffered_spans(
    client, limit
):
    await as_admin(client)
    me = await me_staff_id(client)
    set_limit = await client.patch(f"{STAFF}/{me}", json={"max_concurrent_appointments": limit})
    assert set_limit.status_code == 200, set_limit.text
    seed = await _seed_rows()

    async with session_scope() as db:
        # 10:00–11:00, turning over until 11:15.
        await _insert_appointment(db, seed, at("10:00"), at("11:00"), after=15)
        # A cancelled overlap never counts.
        await _insert_appointment(db, seed, at("10:00"), at("11:00"), status="cancelled")
        # 11:15–12:15 merely touches the turnaround.
        await _insert_appointment(db, seed, at("11:15"), at("12:15"))
        await db.commit()

    async with session_scope() as db:
        # 10:30–11:15 overlaps only the first booking's buffered span (it touches the third):
        # refused at limit 1, the second chair at limit 2.
        if limit == 1:
            with pytest.raises(IntegrityError) as refused:
                await _insert_appointment(db, seed, at("10:30"), at("11:15"))
            assert constraint_of(refused.value) == "tg_appointments_staff_concurrency"
        else:
            await _insert_appointment(db, seed, at("10:30"), at("11:15"))
            await db.commit()

    if limit == 2:
        async with session_scope() as db:
            # A third over 10:45 — the original, the second chair, and one too many.
            with pytest.raises(IntegrityError) as refused:
                await _insert_appointment(db, seed, at("10:45"), at("11:00"))
            assert constraint_of(refused.value) == "tg_appointments_staff_concurrency"


BOOKING_TABLES = ("customers", "appointments", "appointment_resources")


def _ours(names: set[str]) -> set[str]:
    return {name for name in names if not name.endswith("_pkey")}


async def test_the_models_declare_every_constraint_the_database_actually_has(client):
    """The same drift check `test_hours.py` and `test_services.py` run, over this ticket's
    tables. Losing `ex_appointment_resources_no_overlap` to an autogenerated DROP would be
    the one failure the whole design exists to make impossible."""
    from core.db import Base

    async with session_scope() as db:
        for table in BOOKING_TABLES:
            constraints = set(
                await db.scalars(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = cast(:t AS regclass) AND contype IN ('c', 'u', 'x')"
                    ),
                    {"t": table},
                )
            )
            indexes = set(
                await db.scalars(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = :t"), {"t": table}
                )
            )
            database = _ours(constraints | indexes)

            metadata = Base.metadata.tables[table]
            declared = _ours(
                {c.name for c in metadata.constraints if c.name}
                | {i.name for i in metadata.indexes if i.name}
            )

            assert database == declared, (
                f"{table}: only in the database {sorted(database - declared)}, "
                f"only in the model {sorted(declared - database)}"
            )


# --- moving and resizing (Task 16) ---------------------------------------------------------------


async def move(client, appointment_id: str, **changes):
    return await client.patch(f"{APPOINTMENTS}/{appointment_id}", json=changes)


async def resource_periods(appointment_id: str) -> list[tuple[str, str, str]]:
    async with session_scope() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT resource_id, lower(period), upper(period) FROM appointment_resources "
                    "WHERE appointment_id = :a ORDER BY resource_id"
                ),
                {"a": appointment_id},
            )
        ).all()

    def z(moment):
        return moment.isoformat().replace("+00:00", "Z")

    return [(str(r[0]), z(r[1]), z(r[2])) for r in rows]


async def test_a_move_to_a_free_slot_moves_the_resource_rows_with_it_and_is_audited(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 1020)])
    room = await make_resource(client, "space", "Room 1")
    service = await make_service(
        client, [me], requirements=[{"kind": "space"}], buffer_after_minutes=15
    )
    booked = await book(client, service, me, at("10:00"))
    assert booked.status_code == 201, booked.text
    appointment = booked.json()["id"]

    moved = await move(client, appointment, starts_at=at("14:00"))

    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body["starts_at"] == at("14:00")
    assert body["ends_at"] == at("15:00")
    assert body["duration_minutes"] == 60
    assert body["price_cents"] == 12000
    assert body["resources"] == [{"id": room, "name": "Room 1", "kind": "space"}]
    assert await resource_periods(appointment) == [(room, at("14:00"), at("15:15"))]
    # The old hour is on offer again; the new one is not.
    slots = await slots_on(client, service)
    assert "10:00" in slots and "14:00" not in slots and "13:15" not in slots
    events = [e for e in await audit_events() if e[0] == "appointment.rescheduled"]
    assert events == [
        (
            "appointment.rescheduled",
            "appointment",
            appointment,
            {"from": at("10:00"), "to": at("14:00")},
        )
    ]


async def test_a_move_never_blocks_itself_but_is_refused_on_another_appointments_buffer(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 1020)])
    service = await make_service(client, [me], buffer_after_minutes=15)
    first = await book(client, service, me, at("10:00"))  # busy until 11:15
    second = await book(client, service, me, at("12:00"))
    assert first.status_code == 201 and second.status_code == 201
    appointment = second.json()["id"]

    # Overlapping its own old span is fine: the appointment does not stand in its own way.
    nudged = await move(client, appointment, starts_at=at("12:15"))
    # 11:00–12:15 lands on the first appointment's turnaround.
    on_the_buffer = await move(client, appointment, starts_at=at("11:00"))
    # 11:15 merely touches it.
    touching = await move(client, appointment, starts_at=at("11:15"))
    off_the_grid = await move(client, appointment, starts_at=at("13:07"))
    past_the_shift = await move(client, appointment, starts_at=at("16:30"))

    assert nudged.status_code == 200, nudged.text
    assert on_the_buffer.status_code == 422, on_the_buffer.text
    assert on_the_buffer.json()["code"] == "not_offered"
    assert touching.status_code == 200, touching.text
    assert off_the_grid.status_code == 422, off_the_grid.text
    assert past_the_shift.status_code == 422, past_the_shift.text
    listed = await client.get(
        APPOINTMENTS, params={"from": MONDAY.isoformat(), "to": MONDAY.isoformat()}
    )
    assert [a["starts_at"] for a in listed.json()["appointments"]] == [at("10:00"), at("11:15")]


async def test_a_move_onto_a_taken_room_is_refused_and_the_old_claim_kept(client):
    await as_admin(client)
    me = await me_staff_id(client)
    rae = await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)
    await put_hours(client, me, [(0, 540, 1020)])
    await put_hours(client, rae, [(0, 540, 1020)])
    room = await make_resource(client, "space", "Room 1")
    service = await make_service(client, [me, rae], requirements=[{"kind": "space"}])
    mine = await book(client, service, me, at("10:00"))
    raes = await book(client, service, rae, at("12:00"))
    assert mine.status_code == 201 and raes.status_code == 201
    appointment = raes.json()["id"]

    refused = await move(client, appointment, starts_at=at("10:00"))

    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "not_offered"
    assert await resource_periods(appointment) == [(room, at("12:00"), at("13:00"))]


async def test_a_named_requirement_keeps_its_room_and_any_keeps_its_old_one_when_free(client):
    await as_admin(client)
    me = await me_staff_id(client)
    rae = await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)
    desk = await add_colleague(client, "desk@cedar.example", OTHER_PASSWORD)
    for who in (me, rae, desk):
        await put_hours(client, who, [(0, 540, 1020)])
    room_a = await make_resource(client, "space", "Room A", sort_order=1)
    room_b = await make_resource(client, "space", "Room B", sort_order=2)
    any_room = await make_service(client, [me, rae], requirements=[{"kind": "space"}])
    named_b = await make_service(
        client, [desk], name="In B", requirements=[{"kind": "space", "resource_id": room_b}]
    )
    # At 10:00: me in A (first by sort order), Rae in B. At 13:00: the desk, in B by name.
    assert (await book(client, any_room, me, at("10:00"))).json()["resources"][0]["id"] == room_a
    raes = await book(client, any_room, rae, at("10:00"))
    assert raes.json()["resources"][0]["id"] == room_b
    desks = await book(client, named_b, desk, at("13:00"))
    assert desks.status_code == 201, desks.text

    # Rae to 11:00: both rooms free, and she keeps B rather than being handed A.
    kept = await move(client, raes.json()["id"], starts_at=at("11:00"))
    # Rae to 13:00: B is the desk's, so A it is.
    repicked = await move(client, raes.json()["id"], starts_at=at("13:00"))
    # The desk to 14:00: named is named.
    named = await move(client, desks.json()["id"], starts_at=at("14:00"))

    assert kept.status_code == 200 and kept.json()["resources"][0]["id"] == room_b, kept.text
    assert repicked.status_code == 200, repicked.text
    assert repicked.json()["resources"][0]["id"] == room_a
    assert named.status_code == 200 and named.json()["resources"][0]["id"] == room_b, named.text


async def test_a_resize_changes_duration_and_end_only_and_is_audited(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 1020)])
    room = await make_resource(client, "space", "Room 1")
    service = await make_service(
        client, [me], requirements=[{"kind": "space"}], buffer_after_minutes=15
    )
    first = await book(client, service, me, at("10:00"))
    later = await book(client, service, me, at("12:00"))
    assert first.status_code == 201 and later.status_code == 201
    appointment = first.json()["id"]

    longer = await move(client, appointment, duration_minutes=90)
    # 10:00–12:15 with the turnaround, onto the 12:00 booking.
    too_long = await move(client, appointment, duration_minutes=120)
    nothing = await move(client, appointment, duration_minutes=0)

    assert longer.status_code == 200, longer.text
    assert longer.json()["starts_at"] == at("10:00")
    assert longer.json()["ends_at"] == at("11:30")
    assert longer.json()["duration_minutes"] == 90
    assert longer.json()["price_cents"] == 12000
    assert await resource_periods(appointment) == [(room, at("10:00"), at("11:45"))]
    assert too_long.status_code == 422, too_long.text
    assert too_long.json()["code"] == "not_offered"
    assert nothing.status_code == 422, nothing.text
    events = [e for e in await audit_events() if e[0] == "appointment.resized"]
    assert events == [("appointment.resized", "appointment", appointment, {"from": 60, "to": 90})]


async def test_a_move_the_database_refuses_is_slot_taken_and_changes_nothing(client, monkeypatch):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 1020)])
    room = await make_resource(client, "space", "Room 1")
    service = await make_service(client, [me], requirements=[{"kind": "space"}])
    first = await book(client, service, me, at("10:00"))
    second = await book(client, service, me, at("12:00"))
    assert first.status_code == 201 and second.status_code == 201
    appointment = second.json()["id"]

    from scheduling import slots

    async def nothing_busy(db, staff_ids, resource_ids, window, **kwargs):
        return {}, {}

    monkeypatch.setattr(slots, "busy_intervals", nothing_busy)

    resp = await move(client, appointment, starts_at=at("10:00"))

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "slot_taken"
    listed = await client.get(
        APPOINTMENTS, params={"from": MONDAY.isoformat(), "to": MONDAY.isoformat()}
    )
    starts = sorted(a["starts_at"] for a in listed.json()["appointments"])
    assert starts == [at("10:00"), at("12:00")]
    assert await resource_periods(appointment) == [(room, at("12:00"), at("13:00"))]


async def test_only_a_confirmed_appointment_moves_and_only_a_scheduler_may(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 1020)])
    service = await make_service(client, [me])
    booked = await book(client, service, me, at("10:00"))
    assert booked.status_code == 201, booked.text
    appointment = booked.json()["id"]
    role = await client.post(
        "/api/admin/roles",
        json={"name": "Viewer", "description": "Looks.", "capabilities": ["schedule.view"]},
    )
    assert role.status_code == 201, role.text
    await add_colleague(client, "desk@cedar.example", OTHER_PASSWORD, role="Viewer")

    missing = await move(client, "00000000-0000-0000-0000-000000000000", starts_at=at("11:00"))
    empty = await move(client, appointment)
    async with session_scope() as db:
        await db.execute(
            text("UPDATE appointments SET status = 'cancelled' WHERE id = :a"), {"a": appointment}
        )
        await db.commit()
    cancelled = await move(client, appointment, starts_at=at("11:00"))
    client.cookies.clear()
    await as_staff(client, "desk@cedar.example", OTHER_PASSWORD)
    looker = await move(client, appointment, starts_at=at("11:00"))

    assert missing.status_code == 404, missing.text
    assert empty.status_code == 422, empty.text
    assert cancelled.status_code == 409, cancelled.text
    assert looker.status_code == 403, looker.text

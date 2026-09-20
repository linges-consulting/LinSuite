"""S1: `GET /api/availability` — the engine's inputs read from a real database, and served.

The arithmetic is pinned at S2 (`test_availability.py`); what this file proves is the wiring.
Hours saved through `/admin/staff/{id}/hours`, a closure through `/admin/closures`, time off
through `/staff/{id}/time-off` and a service through `/admin/services` come out the other end
as the slots the engine says they should — in the business's zone, on its grid, and no further
ahead than its horizon. Plus who may ask, what a bad range is told, and what an unbookable
service is told.

Dates are chosen relative to today because the endpoint drops slots that have already begun:
a week out, on a weekday the test sets hours for, every slot is in the future.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from core.db import get_purge_engine, session_scope

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

ADDRESSES = (EMAIL, "rae@cedar.example", "noview@cedar.example")

# The next Monday at least a week out: hours are set on weekday 0, and nothing on it has
# started yet whatever the time of day the suite runs at.
MONDAY = date.today() + timedelta(days=7 + (7 - date.today().weekday()) % 7)


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async with get_purge_engine().begin() as purge:
        await purge.execute(text("DELETE FROM audit_events"))
    async with session_scope() as db:
        for table in (
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


async def make_service(client, staff_ids: list[str], **overrides) -> str:
    body = {"name": "Swedish Massage", "duration_minutes": 60, "price_cents": 12000}
    body.update(overrides)
    created = await client.post(SERVICES, json=body)
    assert created.status_code == 201, created.text
    service_id = created.json()["id"]
    linked = await client.put(f"{SERVICES}/{service_id}/staff", json={"staff_ids": staff_ids})
    assert linked.status_code == 200, linked.text
    return service_id


async def ask(client, service_id: str, start: date, end: date | None = None, **params):
    query = {"service_id": service_id, "from": start.isoformat(), "to": (end or start).isoformat()}
    query.update(params)
    return await client.get(AVAILABILITY, params=query)


def local_starts(day: dict) -> list[str]:
    return [
        datetime.fromisoformat(slot["starts_at"]).astimezone(TORONTO).strftime("%H:%M")
        for slot in day["slots"]
    ]


# --- the round trip ---------------------------------------------------------------------------


async def test_hours_closures_time_off_and_the_grid_come_out_as_slots(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720), (0, 780, 855)])  # Mon 09–12, 13:00–14:15
    service = await make_service(client, [me], buffer_after_minutes=15)
    away = await client.post(
        f"/api/staff/{me}/time-off",
        json={
            "all_day": False,
            "starts_at_local": f"{MONDAY.isoformat()}T10:00",
            "ends_at_local": f"{MONDAY.isoformat()}T10:30",
        },
    )
    assert away.status_code == 201, away.text
    tuesday = MONDAY + timedelta(days=1)
    closed = await client.post(
        "/api/admin/closures", json={"date": tuesday.isoformat(), "name": "Retreat"}
    )
    assert closed.status_code == 201, closed.text

    resp = await ask(client, service, MONDAY, tuesday)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["service_id"] == service
    assert body["timezone"] == "America/Toronto"
    assert body["granularity_minutes"] == 15
    assert [d["date"] for d in body["days"]] == [MONDAY.isoformat(), tuesday.isoformat()]
    monday, closure = body["days"]
    # Sixty minutes plus a fifteen-minute turnaround. 09:00 would be turning over during the
    # 10:00–10:30 absence; 10:30 is the first start clear of it and 10:45 the last that ends,
    # turnaround included, by noon. The afternoon block holds exactly one appointment.
    assert local_starts(monday) == ["10:30", "10:45", "13:00"]
    assert all(slot["staff_ids"] == [me] for slot in monday["slots"])
    first = monday["slots"][0]
    assert first["starts_at"].endswith("Z") and first["ends_at"].endswith("Z")
    assert datetime.fromisoformat(first["ends_at"]) - datetime.fromisoformat(
        first["starts_at"]
    ) == timedelta(minutes=60)
    assert closure["slots"] == []


async def test_any_provider_is_the_union_and_a_filter_narrows_it(client):
    await as_admin(client)
    me = await me_staff_id(client)
    rae = await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)
    await put_hours(client, me, [(0, 540, 660)])  # 09–11
    await put_hours(client, rae, [(0, 600, 720)])  # 10–12
    service = await make_service(client, [me, rae])

    both = (await ask(client, service, MONDAY)).json()["days"][0]
    starts = local_starts(both)
    assert starts == [
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
    takers = dict(zip(starts, (slot["staff_ids"] for slot in both["slots"]), strict=True))
    assert takers["09:00"] == [me]
    assert sorted(takers["10:00"]) == sorted([me, rae])  # the one start both could take
    assert takers["11:00"] == [rae]

    only_rae = (await ask(client, service, MONDAY, staff_id=rae)).json()["days"][0]
    assert local_starts(only_rae) == ["10:00", "10:15", "10:30", "10:45", "11:00"]
    assert all(slot["staff_ids"] == [rae] for slot in only_rae["slots"])


async def test_a_named_device_and_the_business_grid_are_read(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    laser = await client.post(RESOURCES, json={"kind": "equipment", "name": "Laser 2"})
    assert laser.status_code == 201, laser.text
    service = await make_service(client, [me])
    needs = await client.put(
        f"{SERVICES}/{service}/requirements",
        json={"requirements": [{"kind": "equipment", "resource_id": laser.json()["id"]}]},
    )
    assert needs.status_code == 200, needs.text
    profile = await client.get("/api/admin/business")
    grid = await client.put(
        "/api/admin/business", json={**profile.json(), "slot_granularity_minutes": 30}
    )
    assert grid.status_code == 200, grid.text

    resp = await ask(client, service, MONDAY)

    assert resp.status_code == 200, resp.text
    assert resp.json()["granularity_minutes"] == 30
    # Nothing is booked on the laser yet (Task 15), so it takes nothing away.
    assert local_starts(resp.json()["days"][0]) == ["09:00", "09:30", "10:00", "10:30", "11:00"]


async def test_a_staff_member_who_cannot_deliver_it_is_refused_as_a_filter(client):
    await as_admin(client)
    me = await me_staff_id(client)
    rae = await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)
    service = await make_service(client, [me])

    resp = await ask(client, service, MONDAY, staff_id=rae)

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["query", "staff_id"]


# --- the horizon and the range ----------------------------------------------------------------


async def test_the_horizon_is_today_plus_n_days_inclusive_and_the_response_says_where_it_ends(
    client,
):
    """A horizon of 1 computes today and tomorrow; the day after is answered empty."""
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(w, 540, 720) for w in range(7)])
    service = await make_service(client, [me])
    profile = await client.get("/api/admin/business")
    horizon = await client.put(
        "/api/admin/business", json={**profile.json(), "booking_horizon_days": 1}
    )
    assert horizon.status_code == 200, horizon.text
    today = datetime.now(UTC).astimezone(TORONTO).date()

    resp = await ask(client, service, today, today + timedelta(days=2))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["horizon_ends_on"] == (today + timedelta(days=1)).isoformat()
    # Today is computable too (how many of its slots remain depends on the time of day).
    assert body["days"][0]["date"] == today.isoformat()
    tomorrow, beyond = body["days"][1], body["days"][2]
    assert len(tomorrow["slots"]) == 9
    assert beyond["slots"] == []


async def test_more_than_a_month_is_refused_and_a_month_is_not(client):
    await as_admin(client)
    me = await me_staff_id(client)
    service = await make_service(client, [me])

    month = await ask(client, service, MONDAY, MONDAY + timedelta(days=30))
    more = await ask(client, service, MONDAY, MONDAY + timedelta(days=31))
    backwards = await ask(client, service, MONDAY, MONDAY - timedelta(days=1))

    assert month.status_code == 200, month.text
    assert len(month.json()["days"]) == 31
    assert more.status_code == 422, more.text
    assert more.json()["detail"][0]["loc"] == ["query", "to"]
    assert backwards.status_code == 422, backwards.text


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-06-15", "to": "2026-06-15"},  # no service
        {"service_id": "00000000-0000-0000-0000-000000000000", "to": "2026-06-15"},  # no from
        {"service_id": "not-a-uuid", "from": "2026-06-15", "to": "2026-06-15"},
        {"service_id": "00000000-0000-0000-0000-000000000000", "from": "June", "to": "2026-06-15"},
    ],
)
async def test_a_request_that_is_not_a_question_is_refused(client, params):
    await as_admin(client)

    resp = await client.get(AVAILABILITY, params=params)

    assert resp.status_code == 422, resp.text


# --- what cannot be booked ------------------------------------------------------------------


async def test_a_service_nobody_can_deliver_is_a_409_with_the_reasons(client):
    await as_admin(client)
    service = await make_service(client, [])

    resp = await ask(client, service, MONDAY)

    assert resp.status_code == 409, resp.text
    assert resp.json()["unbookable_reasons"] == ["Nobody active can deliver this."]


async def test_a_service_needing_a_room_the_business_does_not_have_is_a_409(client):
    await as_admin(client)
    me = await me_staff_id(client)
    service = await make_service(client, [me])
    needs = await client.put(
        f"{SERVICES}/{service}/requirements", json={"requirements": [{"kind": "space"}]}
    )
    assert needs.status_code == 200, needs.text

    resp = await ask(client, service, MONDAY)

    assert resp.status_code == 409, resp.text
    assert resp.json()["unbookable_reasons"] == ["There is no active space."]


async def test_an_unknown_or_deactivated_service_is_a_404(client):
    await as_admin(client)
    me = await me_staff_id(client)
    service = await make_service(client, [me])
    gone = await client.post(f"{SERVICES}/{service}/deactivate", json={})
    assert gone.status_code == 200, gone.text

    assert (await ask(client, service, MONDAY)).status_code == 404
    assert (await ask(client, "00000000-0000-0000-0000-000000000000", MONDAY)).status_code == 404


# --- who may ask --------------------------------------------------------------------------------


async def test_any_staff_member_may_ask_in_staff_mode(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    service = await make_service(client, [me])
    await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)
    client.cookies.clear()
    await as_staff(client, "rae@cedar.example", OTHER_PASSWORD)  # the seeded Staff role

    resp = await ask(client, service, MONDAY)

    assert resp.status_code == 200, resp.text
    assert len(resp.json()["days"][0]["slots"]) == 9


async def test_a_role_without_schedule_view_is_refused(client):
    await as_admin(client)
    me = await me_staff_id(client)
    service = await make_service(client, [me])
    role = await client.post(
        "/api/admin/roles",
        json={"name": "Counter", "description": "Sells.", "capabilities": ["customers.view"]},
    )
    assert role.status_code == 201, role.text
    await add_colleague(client, "noview@cedar.example", OTHER_PASSWORD, role="Counter")
    client.cookies.clear()
    await as_staff(client, "noview@cedar.example", OTHER_PASSWORD)

    resp = await ask(client, service, MONDAY)

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "capability_required"


async def test_nobody_signed_in_is_refused(client):
    resp = await client.get(
        AVAILABILITY,
        params={
            "service_id": str(__import__("uuid").uuid4()),
            "from": "2026-06-15",
            "to": "2026-06-15",
        },
    )

    assert resp.status_code == 401, resp.text

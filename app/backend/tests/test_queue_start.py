"""S1: `POST /queue-entries/{id}/start` — a waiting queue entry becomes an ordinary
`Appointment` (Phase 7 Task 5, #12).

`tests/test_queue_eligibility.py` already proves `can_start` itself (S2); this file proves the
real database transaction around it: staff resolution (named and "any"), the literal
acceptance criterion ("a walk-in cannot be started if it would run into a booked appointment")
through this endpoint specifically, the bare-name customer-identity path, resource claiming,
`cache.bump()`, and the entry's own status transition (through to `done` on the resulting
appointment's completion).
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from core.db import get_purge_engine, session_scope
from core.redis import get_redis
from scheduling import cache as avail_cache
from tests.conftest import wipe_document_keys

TORONTO = ZoneInfo("America/Toronto")

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"
OTHER_PASSWORD = "correct horse battery 2"

SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": EMAIL,
    "admin_password": PASSWORD,
}

ADDRESSES = (EMAIL, "colleague@cedar.example")

STAFF = "/api/admin/staff"
SERVICES = "/api/admin/services"
RESOURCES = "/api/admin/resources"
CUSTOMERS = "/api/customers"
APPOINTMENTS = "/api/appointments"
NOTIFICATIONS = "/api/admin/business/notifications"
QUEUE = "/api/queue-entries"

# Every weekday, the whole day — this is about "right now", whatever real day/time the suite
# runs on, not a fixed business-local Monday the way `test_appointments.py`'s fixtures are.
FULL_WEEK = [(w, 0, 24 * 60) for w in range(7)]


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async def wipe():
        async with get_purge_engine().begin() as purge:
            await purge.execute(text("DELETE FROM audit_events"))
            await purge.execute(text("DELETE FROM erasure_requests"))
            await purge.execute(text("DELETE FROM form_links"))
        await wipe_document_keys()
        async with session_scope() as db:
            for table in (
                # #59's draft bill lines/bills FK to appointments with no cascade from that
                # side — deleted first, before appointments, the same reason queue_entries
                # already comes before it (test_appointments.py's fixture carries the same
                # addition).
                "service_bill_lines",
                "service_bills",
                "queue_entries",
                "appointment_resources",
                "appointments",
                "customers",
                "service_requirements",
                "service_staff",
                "package_definition_services",
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

    await get_redis().delete(*(throttle.delay_key(e) for e in ADDRESSES))
    await get_redis().delete(avail_cache._GENERATION_KEY)

    async with session_scope() as db:
        token = await setup.bootstrap_setup_token(db)
    resp = await client.post("/api/setup", json={**SETUP, "token": token})
    assert resp.status_code == 201, resp.text
    async with session_scope() as db:
        await db.execute(text("UPDATE businesses SET mfa_required_for_admin = false"))
        await db.commit()
    client.cookies.clear()
    yield
    async with session_scope() as db:
        for table in (
            "service_bill_lines",
            "service_bills",
            "queue_entries",
            "appointment_resources",
            "appointments",
        ):
            await db.execute(text(f"DELETE FROM {table}"))
        await db.commit()


# --- helpers ----------------------------------------------------------------------------------


async def as_admin(client, email=EMAIL, password=PASSWORD):
    login = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    resp = await client.post("/api/auth/mode", json={"mode": "admin", "password": password})
    assert resp.status_code == 200, resp.text


async def me_staff_id(client) -> str:
    roster = await client.get(STAFF)
    assert roster.status_code == 200, roster.text
    return next(row["id"] for row in roster.json()["staff"] if row["email"] == EMAIL)


async def add_colleague(client, email: str, password: str) -> str:
    from core.security import hash_password
    from tests.conftest import add_account

    await add_account(email, await hash_password(password))
    roster = await client.get(STAFF)
    return next(row["id"] for row in roster.json()["staff"] if row["email"] == email)


async def set_sort_order(client, staff_id: str, sort_order: int) -> None:
    resp = await client.patch(f"{STAFF}/{staff_id}", json={"sort_order": sort_order})
    assert resp.status_code == 200, resp.text


async def put_hours(client, staff_id: str, blocks=None) -> None:
    blocks = blocks if blocks is not None else FULL_WEEK
    resp = await client.put(
        f"{STAFF}/{staff_id}/hours",
        json={"blocks": [{"weekday": w, "start_minute": s, "end_minute": e} for w, s, e in blocks]},
    )
    assert resp.status_code == 200, resp.text


async def enable_queue(client) -> None:
    resp = await client.patch(NOTIFICATIONS, json={"enable_walk_in_queue": True})
    assert resp.status_code == 200, resp.text


async def make_service(client, staff_ids: list[str], requirements=None, **overrides) -> str:
    body = {"name": "Haircut", "duration_minutes": 20, "price_cents": 4000}
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


async def make_customer(client, **overrides) -> str:
    body = {"first_name": "Priya", "last_name": "Nair", "phone": "416-555-0199"}
    body.update(overrides)
    resp = await client.post(CUSTOMERS, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def add_entry(client, **overrides) -> dict:
    resp = await client.post(QUEUE, json=overrides)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def book_now(
    staff_id: str,
    customer_id: str,
    service_id: str,
    *,
    start_offset_minutes: int,
    end_offset_minutes: int,
    buffer_before: int = 0,
    buffer_after: int = 0,
) -> str:
    """Books an appointment directly against the database, `now()` as the reference instant —
    the real-time counterpart to `test_queue_eligibility.py`'s pure "a booking starting in 10
    minutes" scenario, for an S1 test that cannot pin a fixed business-local day."""
    duration = end_offset_minutes - start_offset_minutes
    async with session_scope() as db:
        appointment_id = await db.scalar(
            text(
                "INSERT INTO appointments (customer_id, staff_id, service_id, starts_at, "
                "ends_at, duration_minutes, buffer_before_minutes, buffer_after_minutes, "
                "price_cents) VALUES (:c, :s, :sv, now() + make_interval(mins => :start), "
                "now() + make_interval(mins => :end), :dur, :bb, :ba, 0) RETURNING id"
            ),
            {
                "c": customer_id,
                "s": staff_id,
                "sv": service_id,
                "start": start_offset_minutes,
                "end": end_offset_minutes,
                "dur": duration,
                "bb": buffer_before,
                "ba": buffer_after,
            },
        )
        await db.commit()
    return str(appointment_id)


async def claim_resource_now(appointment_id: str, resource_id: str, kind: str = "space") -> None:
    async with session_scope() as db:
        await db.execute(
            text(
                "INSERT INTO appointment_resources (appointment_id, resource_id, kind, period) "
                "VALUES (:a, :r, :k, tstzrange(now() - interval '5 minutes', "
                "now() + interval '25 minutes', '[)'))"
            ),
            {"a": appointment_id, "r": resource_id, "k": kind},
        )
        await db.commit()


async def generation() -> int:
    return int(await get_redis().get(avail_cache._GENERATION_KEY) or 0)


# --- the happy path, a named staff member -----------------------------------------------------


async def test_a_preferred_staff_member_starts_and_becomes_a_complete_appointment(client):
    await as_admin(client)
    await enable_queue(client)
    me = await me_staff_id(client)
    await put_hours(client, me)
    service = await make_service(client, [me], duration_minutes=20)
    customer = await make_customer(client)
    entry = await add_entry(
        client, customer_id=customer, requested_service_id=service, preferred_staff_id=me
    )

    resp = await client.post(f"{QUEUE}/{entry['id']}/start", json={})
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["queue_entry"]["status"] == "in_service"
    assert body["queue_entry"]["appointment_id"] == body["appointment"]["id"]
    assert body["appointment"]["status"] == "confirmed"
    assert body["appointment"]["staff"]["id"] == me
    assert body["appointment"]["customer"]["id"] == customer
    assert body["appointment"]["duration_minutes"] == 20

    # A real, complete appointment — not a stand-in shape: it shows up on the ordinary list.
    # A two-day, business-local window bracketing "now" — the list endpoint refuses more than
    # 31 days at once, and "now" may sit on either side of local midnight.
    today = datetime.now(UTC).astimezone(TORONTO).date()
    listed = await client.get(
        f"{APPOINTMENTS}?from={(today - timedelta(days=1)).isoformat()}&to="
        f"{(today + timedelta(days=1)).isoformat()}"
    )
    assert listed.status_code == 200, listed.text
    assert body["appointment"]["id"] in [a["id"] for a in listed.json()["appointments"]]


async def test_a_known_customer_is_reused_not_duplicated(client):
    await as_admin(client)
    await enable_queue(client)
    me = await me_staff_id(client)
    await put_hours(client, me)
    service = await make_service(client, [me], duration_minutes=20)
    customer = await make_customer(client)
    entry = await add_entry(
        client, customer_id=customer, requested_service_id=service, preferred_staff_id=me
    )

    resp = await client.post(f"{QUEUE}/{entry['id']}/start", json={})
    assert resp.status_code == 201, resp.text
    assert resp.json()["appointment"]["customer"]["id"] == customer

    total = await client.get(CUSTOMERS)
    assert total.json()["total"] == 1


# --- "any eligible staff" resolution -----------------------------------------------------------


async def test_any_eligible_staff_resolution_skips_a_busy_candidate(client):
    await as_admin(client)
    await enable_queue(client)
    me = await me_staff_id(client)
    colleague = await add_colleague(client, "colleague@cedar.example", OTHER_PASSWORD)
    await put_hours(client, me)
    await put_hours(client, colleague)
    # `me` is tried first (lowest sort_order) but is busy; `colleague` is free.
    await set_sort_order(client, me, 0)
    await set_sort_order(client, colleague, 1)
    service = await make_service(client, [me, colleague], duration_minutes=20)
    customer = await make_customer(client)
    # A booking starting in 10 minutes, a 20-minute service walks right into it — the literal
    # acceptance criterion, here used to prove the "any" resolution actually filters through
    # `can_start` rather than just taking the lowest `sort_order`.
    await book_now(me, customer, service, start_offset_minutes=10, end_offset_minutes=40)
    entry = await add_entry(client, customer_id=customer, requested_service_id=service)

    resp = await client.post(f"{QUEUE}/{entry['id']}/start", json={})
    assert resp.status_code == 201, resp.text
    assert resp.json()["appointment"]["staff"]["id"] == colleague


async def test_a_preferred_staff_member_ineligible_for_the_service_is_refused(client):
    """Task 2's own note: no staff-service eligibility check at add-time — this is where it's
    actually asked."""
    await as_admin(client)
    await enable_queue(client)
    me = await me_staff_id(client)
    colleague = await add_colleague(client, "colleague@cedar.example", OTHER_PASSWORD)
    await put_hours(client, me)
    await put_hours(client, colleague)
    service = await make_service(client, [colleague], duration_minutes=20)  # `me` not eligible
    customer = await make_customer(client)
    entry = await add_entry(
        client, customer_id=customer, requested_service_id=service, preferred_staff_id=me
    )

    resp = await client.post(f"{QUEUE}/{entry['id']}/start", json={})
    assert resp.status_code == 422


# --- the literal acceptance criterion, through this endpoint specifically ----------------------


async def test_a_walk_in_that_would_run_into_a_booked_appointment_is_refused(client):
    await as_admin(client)
    await enable_queue(client)
    me = await me_staff_id(client)
    await put_hours(client, me)
    service = await make_service(client, [me], duration_minutes=20)
    customer = await make_customer(client)
    await book_now(me, customer, service, start_offset_minutes=10, end_offset_minutes=40)
    entry = await add_entry(
        client, customer_id=customer, requested_service_id=service, preferred_staff_id=me
    )

    resp = await client.post(f"{QUEUE}/{entry['id']}/start", json={})
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "not_eligible"
    assert body["reason"] == "staff_busy"

    # Refused cleanly: nothing booked, the entry is still waiting.
    listed = await client.get(QUEUE)
    assert listed.json()["entries"][0]["status"] == "waiting"


async def test_starting_an_already_started_entry_is_refused(client):
    await as_admin(client)
    await enable_queue(client)
    me = await me_staff_id(client)
    await put_hours(client, me)
    service = await make_service(client, [me], duration_minutes=20)
    customer = await make_customer(client)
    entry = await add_entry(
        client, customer_id=customer, requested_service_id=service, preferred_staff_id=me
    )
    first = await client.post(f"{QUEUE}/{entry['id']}/start", json={})
    assert first.status_code == 201, first.text

    again = await client.post(f"{QUEUE}/{entry['id']}/start", json={})
    assert again.status_code == 409
    assert again.json()["code"] == "invalid_transition"


# --- physical resources: a different question from `can_start`'s own -----------------------------


async def test_a_busy_required_resource_refuses_the_start(client):
    await as_admin(client)
    await enable_queue(client)
    me = await me_staff_id(client)
    colleague = await add_colleague(client, "colleague@cedar.example", OTHER_PASSWORD)
    await put_hours(client, me)
    await put_hours(client, colleague)
    room = await make_resource(client, "space", "Room 1")
    service = await make_service(
        client, [me], requirements=[{"kind": "space"}], duration_minutes=20
    )
    other_service = await make_service(client, [colleague], name="Massage", duration_minutes=30)
    customer = await make_customer(client)
    # A different staff member's own appointment has the room claimed right now — `me` is
    # entirely free, so `can_start` alone would say yes; the room is what refuses this.
    other = await book_now(
        colleague, customer, other_service, start_offset_minutes=-5, end_offset_minutes=25
    )
    await claim_resource_now(other, room)

    entry = await add_entry(
        client, customer_id=customer, requested_service_id=service, preferred_staff_id=me
    )
    resp = await client.post(f"{QUEUE}/{entry['id']}/start", json={})
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "resource_unavailable"


# --- the bare-name customer-identity path -------------------------------------------------------


async def test_a_bare_name_entry_creates_a_real_customer_split_on_the_first_space(client):
    await as_admin(client)
    await enable_queue(client)
    me = await me_staff_id(client)
    await put_hours(client, me)
    service = await make_service(client, [me], duration_minutes=20)
    entry = await add_entry(
        client,
        bare_name="Jamie Lee",
        bare_phone="604-555-0111",
        requested_service_id=service,
        preferred_staff_id=me,
    )

    before = await client.get(CUSTOMERS)
    assert before.json()["total"] == 0

    resp = await client.post(f"{QUEUE}/{entry['id']}/start", json={})
    assert resp.status_code == 201, resp.text
    body = resp.json()["appointment"]["customer"]
    assert body["first_name"] == "Jamie"
    assert body["last_name"] == "Lee"

    after = await client.get(CUSTOMERS)
    assert after.json()["total"] == 1


async def test_a_single_word_bare_name_becomes_both_first_and_last_name(client):
    await as_admin(client)
    await enable_queue(client)
    me = await me_staff_id(client)
    await put_hours(client, me)
    service = await make_service(client, [me], duration_minutes=20)
    entry = await add_entry(
        client, bare_name="Cher", requested_service_id=service, preferred_staff_id=me
    )

    resp = await client.post(f"{QUEUE}/{entry['id']}/start", json={})
    assert resp.status_code == 201, resp.text
    body = resp.json()["appointment"]["customer"]
    assert body["first_name"] == "Cher"
    assert body["last_name"] == "Cher"


# --- cache.bump() --------------------------------------------------------------------------


async def test_starting_a_queue_entry_bumps_the_availability_cache(client):
    await as_admin(client)
    await enable_queue(client)
    me = await me_staff_id(client)
    await put_hours(client, me)
    service = await make_service(client, [me], duration_minutes=20)
    customer = await make_customer(client)
    entry = await add_entry(
        client, customer_id=customer, requested_service_id=service, preferred_staff_id=me
    )

    before = await generation()
    resp = await client.post(f"{QUEUE}/{entry['id']}/start", json={})
    assert resp.status_code == 201, resp.text
    after = await generation()

    assert after == before + 1


# --- done, on the resulting appointment's completion --------------------------------------------


async def test_completing_the_converted_appointment_marks_the_queue_entry_done(client):
    await as_admin(client)
    await enable_queue(client)
    me = await me_staff_id(client)
    await put_hours(client, me)
    service = await make_service(client, [me], duration_minutes=20)
    customer = await make_customer(client)
    entry = await add_entry(
        client, customer_id=customer, requested_service_id=service, preferred_staff_id=me
    )
    started = await client.post(f"{QUEUE}/{entry['id']}/start", json={})
    appointment_id = started.json()["appointment"]["id"]

    completed = await client.post(f"{APPOINTMENTS}/{appointment_id}/complete", json={})
    assert completed.status_code == 200, completed.text

    listed = await client.get(QUEUE)
    status = next(e["status"] for e in listed.json()["entries"] if e["id"] == entry["id"])
    assert status == "done"


async def test_cancelling_the_converted_appointment_marks_the_queue_entry_done(client):
    """A walk-in whose converted appointment gets cancelled is not stuck `in_service` on the
    front-desk screen forever — cancel clears it exactly like completion does."""
    await as_admin(client)
    await enable_queue(client)
    me = await me_staff_id(client)
    await put_hours(client, me)
    service = await make_service(client, [me], duration_minutes=20)
    customer = await make_customer(client)
    entry = await add_entry(
        client, customer_id=customer, requested_service_id=service, preferred_staff_id=me
    )
    started = await client.post(f"{QUEUE}/{entry['id']}/start", json={})
    appointment_id = started.json()["appointment"]["id"]

    cancelled = await client.post(f"{APPOINTMENTS}/{appointment_id}/cancel", json={})
    assert cancelled.status_code == 200, cancelled.text

    listed = await client.get(QUEUE)
    status = next(e["status"] for e in listed.json()["entries"] if e["id"] == entry["id"])
    assert status == "done"


async def test_a_no_show_on_the_converted_appointment_marks_the_queue_entry_done(client):
    await as_admin(client)
    await enable_queue(client)
    me = await me_staff_id(client)
    await put_hours(client, me)
    service = await make_service(client, [me], duration_minutes=20)
    customer = await make_customer(client)
    entry = await add_entry(
        client, customer_id=customer, requested_service_id=service, preferred_staff_id=me
    )
    started = await client.post(f"{QUEUE}/{entry['id']}/start", json={})
    appointment_id = started.json()["appointment"]["id"]

    async with session_scope() as db:
        await db.execute(
            text("UPDATE appointments SET starts_at = now() - interval '1 minute' WHERE id = :id"),
            {"id": appointment_id},
        )
        await db.commit()

    no_show = await client.post(f"{APPOINTMENTS}/{appointment_id}/no-show", json={})
    assert no_show.status_code == 200, no_show.text

    listed = await client.get(QUEUE)
    status = next(e["status"] for e in listed.json()["entries"] if e["id"] == entry["id"])
    assert status == "done"


async def test_an_ordinary_appointment_with_no_queue_entry_completes_unaffected(client):
    """The `UPDATE ... WHERE appointment_id = ...` touches zero rows for an appointment that
    never came through the queue at all — completing it must not error. Booked the same way
    `test_a_busy_required_resource_refuses_the_start` books one directly, since there is no
    slot-grid start "right now" to book through the ordinary staff endpoint reliably in a
    real-time test."""
    await as_admin(client)
    me = await me_staff_id(client)
    service = await make_service(client, [me], duration_minutes=20)
    customer = await make_customer(client)
    appointment_id = await book_now(
        me, customer, service, start_offset_minutes=0, end_offset_minutes=20
    )

    completed = await client.post(f"{APPOINTMENTS}/{appointment_id}/complete", json={})
    assert completed.status_code == 200, completed.text

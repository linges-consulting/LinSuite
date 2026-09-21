"""S1: the appointment status lifecycle, over HTTP (Task 18).

`confirmed → completed | cancelled | no_show`, every one of the three terminal — no reopen in
M1. Each transition is its own endpoint, each records its own audit event, and each refuses
from anywhere but `confirmed` with 409 `invalid_transition`. Completion is the one recorded
event later phases (treatment receipts, package credits, commission) key off
(CLAUDE.md "Domain rules"); cancellation and no-show both free the appointment's resources —
`appointment_resources` has no WHERE clause on its exclusion constraint, so a booking frees
its room by no longer claiming it, and this file proves a new booking can then take it.
"""

from sqlalchemy import text

from core.db import session_scope
from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    APPOINTMENTS,
    EMAIL,
    MONDAY,
    OTHER_PASSWORD,
    PASSWORD,
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

VIEWER_ROLE = ["schedule.view"]


async def add_viewer(client, email: str, password: str) -> None:
    """A role with `schedule.view` but not `schedule.manage`, for the capability check."""
    from core.security import hash_password
    from tests.conftest import add_account

    async with session_scope() as db:
        from auth.models import Role, RoleCapability

        role = Role(name="Front desk viewer", is_system=False)
        db.add(role)
        await db.flush()
        for key in VIEWER_ROLE:
            db.add(RoleCapability(role_id=role.id, capability=key))
        await db.commit()
        role_id = str(role.id)
    await add_account(email, await hash_password(password), role=role_id)


async def push_to_past(appointment_id: str, days: int = 20) -> None:
    """Move a booked appointment's span into the past, so a no-show has something to have
    missed. Direct SQL rather than the API: the engine never offers a past start to book."""
    async with session_scope() as db:
        await db.execute(
            text(
                "UPDATE appointments SET starts_at = starts_at - make_interval(days => :d), "
                "ends_at = ends_at - make_interval(days => :d) WHERE id = :id"
            ),
            {"d": days, "id": appointment_id},
        )
        await db.commit()


async def book_one(client, *, room: bool = False) -> tuple[str, str, str, str]:
    """A confirmed appointment for Monday 10:00, with a room if asked. Returns
    (appointment_id, service_id, staff_id, room_id | "")."""
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    room_id = await make_resource(client, "space", "Room 1") if room else ""
    service = await make_service(client, [me], requirements=[{"kind": "space"}] if room else None)
    made = await book(client, service, me, at("10:00"))
    assert made.status_code == 201, made.text
    return made.json()["id"], service, me, room_id


# --- complete --------------------------------------------------------------------------------


async def test_completing_a_confirmed_appointment_stamps_completed_at_and_audits_it(client):
    appointment_id, *_ = await book_one(client)

    resp = await client.post(f"{APPOINTMENTS}/{appointment_id}/complete", json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["completed_at"] is not None
    assert body["cancelled_at"] is None
    assert body["no_show_at"] is None
    events = [e for e in await audit_events() if e[0] == "appointment.completed"]
    assert len(events) == 1
    assert events[0][2] == appointment_id


async def test_completing_it_twice_is_the_second_time_a_409_invalid_transition(client):
    appointment_id, *_ = await book_one(client)
    first = await client.post(f"{APPOINTMENTS}/{appointment_id}/complete", json={})
    assert first.status_code == 200, first.text

    again = await client.post(f"{APPOINTMENTS}/{appointment_id}/complete", json={})

    assert again.status_code == 409, again.text
    assert again.json()["code"] == "invalid_transition"


async def test_completing_a_cancelled_appointment_is_refused(client):
    appointment_id, *_ = await book_one(client)
    cancelled = await client.post(f"{APPOINTMENTS}/{appointment_id}/cancel", json={})
    assert cancelled.status_code == 200, cancelled.text

    resp = await client.post(f"{APPOINTMENTS}/{appointment_id}/complete", json={})

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "invalid_transition"


# --- cancel ------------------------------------------------------------------------------


async def test_cancelling_stamps_the_reason_frees_the_room_and_audits_it(client):
    appointment_id, service, me, room_id = await book_one(client, room=True)

    resp = await client.post(
        f"{APPOINTMENTS}/{appointment_id}/cancel", json={"reason": "Client called it off"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["cancelled_at"] is not None
    assert body["cancel_reason"] == "Client called it off"
    assert body["resources"] == []
    events = [e for e in await audit_events() if e[0] == "appointment.cancelled"]
    assert len(events) == 1
    assert events[0][2] == appointment_id
    assert events[0][3]["reason"] == "Client called it off"

    # The room is free again: the engine offers the slot, and a second client can take it.
    assert "10:00" in await slots_on(client, service, staff_id=me)
    second = await book(client, service, me, at("10:00"))
    assert second.status_code == 201, second.text
    assert second.json()["resources"] == [{"id": room_id, "name": "Room 1", "kind": "space"}]


async def test_cancelling_an_already_cancelled_appointment_is_refused(client):
    appointment_id, *_ = await book_one(client)
    first = await client.post(f"{APPOINTMENTS}/{appointment_id}/cancel", json={})
    assert first.status_code == 200, first.text

    again = await client.post(f"{APPOINTMENTS}/{appointment_id}/cancel", json={})

    assert again.status_code == 409, again.text
    assert again.json()["code"] == "invalid_transition"


async def test_a_terminal_appointment_cannot_be_moved(client):
    appointment_id, *_ = await book_one(client)
    cancelled = await client.post(f"{APPOINTMENTS}/{appointment_id}/cancel", json={})
    assert cancelled.status_code == 200, cancelled.text

    resp = await move(client, appointment_id, starts_at=at("11:00"))

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "invalid_transition"


# --- no-show -----------------------------------------------------------------------------


async def test_marking_no_show_before_the_start_is_refused(client):
    appointment_id, *_ = await book_one(client)

    resp = await client.post(f"{APPOINTMENTS}/{appointment_id}/no-show", json={})

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "not_yet_started"


async def test_marking_no_show_after_the_start_frees_the_room_and_audits_it(client):
    appointment_id, service, me, room_id = await book_one(client, room=True)
    await push_to_past(appointment_id)

    resp = await client.post(f"{APPOINTMENTS}/{appointment_id}/no-show", json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "no_show"
    assert body["no_show_at"] is not None
    assert body["cancelled_at"] is None
    assert body["resources"] == []
    events = [e for e in await audit_events() if e[0] == "appointment.no_show"]
    assert len(events) == 1
    assert events[0][2] == appointment_id


async def test_a_no_show_frees_the_staff_members_remaining_time(client):
    """A no-show does not occupy: the stylist is bookable again for the span the client did
    not turn up for. The row is pushed into the past to be markable and then pushed back, so
    the transition itself goes through the real endpoint (fix wave, finding 2)."""
    appointment_id, service, me, _ = await book_one(client)
    await push_to_past(appointment_id)
    marked = await client.post(f"{APPOINTMENTS}/{appointment_id}/no-show", json={})
    assert marked.status_code == 200, marked.text
    await push_to_past(appointment_id, days=-20)

    assert "10:00" in await slots_on(client, service, staff_id=me)
    second = await book(client, service, me, at("10:00"))
    assert second.status_code == 201, second.text


async def test_lowering_the_concurrency_limit_never_blocks_completing_an_existing_appointment(
    client,
):
    """The staff-concurrency trigger is about taking a span, not keeping one: an appointment
    that already holds its slot may still be completed after the limit is lowered under it
    (fix wave, finding 3)."""
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    raised = await client.patch(f"/api/admin/staff/{me}", json={"max_concurrent_appointments": 2})
    assert raised.status_code == 200, raised.text
    service = await make_service(client, [me])
    first = await book(client, service, me, at("10:00"))
    assert first.status_code == 201, first.text
    second = await book(client, service, me, at("10:30"))
    assert second.status_code == 201, second.text
    lowered = await client.patch(f"/api/admin/staff/{me}", json={"max_concurrent_appointments": 1})
    assert lowered.status_code == 200, lowered.text

    resp = await client.post(f"{APPOINTMENTS}/{first.json()['id']}/complete", json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"


async def test_no_show_is_distinguishable_from_cancelled(client):
    """Both terminal, both free their resources — but the status the report reads is not
    the same one (acceptance criteria: "No-show is distinguishable from cancelled")."""
    a, *_ = await book_one(client)
    await push_to_past(a)
    no_show = await client.post(f"{APPOINTMENTS}/{a}/no-show", json={})
    assert no_show.status_code == 200, no_show.text
    assert no_show.json()["status"] == "no_show"
    assert no_show.json()["cancel_reason"] is None


# --- listing: include_cancelled ---------------------------------------------------------------


async def test_cancelled_appointments_are_left_out_unless_asked_for(client):
    appointment_id, *_ = await book_one(client)
    cancelled = await client.post(f"{APPOINTMENTS}/{appointment_id}/cancel", json={})
    assert cancelled.status_code == 200, cancelled.text

    default = await client.get(
        APPOINTMENTS, params={"from": MONDAY.isoformat(), "to": MONDAY.isoformat()}
    )
    shown = await client.get(
        APPOINTMENTS,
        params={"from": MONDAY.isoformat(), "to": MONDAY.isoformat(), "include_cancelled": True},
    )

    assert appointment_id not in [a["id"] for a in default.json()["appointments"]]
    assert appointment_id in [a["id"] for a in shown.json()["appointments"]]


async def test_the_schedule_read_honours_include_cancelled_too(client):
    schedule = "/api/schedule"
    appointment_id, *_ = await book_one(client)
    cancelled = await client.post(f"{APPOINTMENTS}/{appointment_id}/cancel", json={})
    assert cancelled.status_code == 200, cancelled.text

    default = await client.get(
        schedule, params={"from": MONDAY.isoformat(), "to": MONDAY.isoformat()}
    )
    shown = await client.get(
        schedule,
        params={"from": MONDAY.isoformat(), "to": MONDAY.isoformat(), "include_cancelled": True},
    )

    assert appointment_id not in [a["id"] for a in default.json()["appointments"]]
    assert appointment_id in [a["id"] for a in shown.json()["appointments"]]


# --- capability enforcement ---------------------------------------------------------------


async def test_a_role_without_schedule_manage_may_not_transition_an_appointment(client):
    appointment_id, *_ = await book_one(client)
    viewer_email = "frontdesk@cedar.example"
    await add_viewer(client, viewer_email, OTHER_PASSWORD)
    await as_staff(client, viewer_email, OTHER_PASSWORD)

    resp = await client.post(f"{APPOINTMENTS}/{appointment_id}/complete", json={})

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "capability_required"


# --- concurrent transitions (fix round 1: Critical) ---------------------------------------
#
# Two requests racing on one appointment's status must not both win. `_load` is the read
# both `complete_appointment` and `cancel_appointment` do before their status check — slowing
# it (the same technique as `test_two_concurrent_bookings_of_one_slot...`) is what turns the
# race from lucky into certain: both requests reach "read the row" before either has
# committed a change.


async def _slowed(monkeypatch, name: str):
    """Patch `scheduling.appointments.<name>` so every call pauses just after the real one,
    widening whatever race the caller is about to provoke."""
    import asyncio

    from scheduling import appointments

    real = getattr(appointments, name)

    async def slow(*args, **kwargs):
        result = await real(*args, **kwargs)
        await asyncio.sleep(0.25)
        return result

    monkeypatch.setattr(appointments, name, slow)


async def _concurrent_requests(client, *requests: tuple[str, str, dict]):
    """`requests` is (method, url, json_body) triples, fired together on independent ASGI
    clients that share this test's session cookie — genuinely concurrent connections, not
    just concurrent coroutines on one."""
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from main import app as main_app

    cookie = client.cookies["linsuite_session"]

    async def attempt(method: str, url: str, body: dict):
        async with AsyncClient(transport=ASGITransport(app=main_app), base_url="http://test") as c:
            c.cookies.set("linsuite_session", cookie)
            return await c.request(method, url, json=body)

    return await asyncio.gather(*(attempt(method, url, body) for method, url, body in requests))


async def test_a_complete_and_a_cancel_racing_on_one_appointment_yield_one_winner(
    client, monkeypatch
):
    appointment_id, *_ = await book_one(client)
    await _slowed(monkeypatch, "_load")

    complete_resp, cancel_resp = await _concurrent_requests(
        client,
        ("POST", f"{APPOINTMENTS}/{appointment_id}/complete", {}),
        ("POST", f"{APPOINTMENTS}/{appointment_id}/cancel", {"reason": "Racing"}),
    )

    assert sorted([complete_resp.status_code, cancel_resp.status_code]) == [200, 409], (
        complete_resp.text,
        cancel_resp.text,
    )
    loser = complete_resp if complete_resp.status_code == 409 else cancel_resp
    assert loser.json()["code"] == "invalid_transition"

    async with session_scope() as db:
        row = (
            await db.execute(
                text("SELECT status, completed_at, cancelled_at FROM appointments WHERE id = :id"),
                {"id": appointment_id},
            )
        ).one()
    # Exactly one terminal timestamp survives — the loser never touched the row.
    stamps = [row.completed_at, row.cancelled_at]
    assert sum(1 for s in stamps if s is not None) == 1
    if row.status == "completed":
        assert row.completed_at is not None
        assert row.cancelled_at is None
    else:
        assert row.status == "cancelled"
        assert row.cancelled_at is not None
        assert row.completed_at is None


async def test_a_move_racing_a_cancel_never_reattaches_resources_to_a_cancelled_appointment(
    client, monkeypatch
):
    appointment_id, *_ = await book_one(client, room=True)
    await _slowed(monkeypatch, "_load")

    move_resp, cancel_resp = await _concurrent_requests(
        client,
        ("PATCH", f"{APPOINTMENTS}/{appointment_id}", {"starts_at": at("11:00")}),
        ("POST", f"{APPOINTMENTS}/{appointment_id}/cancel", {"reason": "Racing"}),
    )
    assert move_resp.status_code in (200, 409), move_resp.text
    # Cancel never blocks on a move (a move alone never terminalizes the row), so it always
    # eventually succeeds whichever order the lock was granted in.
    assert cancel_resp.status_code == 200, cancel_resp.text
    if move_resp.status_code == 409:
        assert move_resp.json()["code"] == "invalid_transition"

    async with session_scope() as db:
        row = (
            await db.execute(
                text("SELECT status FROM appointments WHERE id = :id"), {"id": appointment_id}
            )
        ).one()
        resources = await db.scalar(
            text("SELECT count(*) FROM appointment_resources WHERE appointment_id = :id"),
            {"id": appointment_id},
        )
    assert row.status == "cancelled"
    # Whichever order the lock was granted in, cancellation is the final word: no resource
    # claim survives it, even one a racing move tried to re-attach.
    assert resources == 0

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

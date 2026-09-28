"""S1: `/api/queue-entries` — add a walk-in, list who is waiting, mark one abandoned (Phase 7
Task 2, #12).

Toggle-off is the acceptance criterion carried over from Task 1 ("with the queue disabled, no
queue surface exists anywhere in the product"), applied here to the API layer: every route
must 404 while `enable_walk_in_queue` is off, checked first.
"""

import pytest
from sqlalchemy import text

from core.db import get_purge_engine, session_scope
from tests.conftest import wipe_document_keys

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"

SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": EMAIL,
    "admin_password": PASSWORD,
}

ADDRESSES = (EMAIL, "colleague@cedar.example")

SERVICES = "/api/admin/services"
CUSTOMERS = "/api/customers"
NOTIFICATIONS = "/api/admin/business/notifications"
QUEUE = "/api/queue-entries"


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
                "queue_entries",
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


# --- helpers ----------------------------------------------------------------------------------


async def as_admin(client, email=EMAIL, password=PASSWORD):
    login = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    resp = await client.post("/api/auth/mode", json={"mode": "admin", "password": password})
    assert resp.status_code == 200, resp.text


async def as_staff(client, email, password):
    login = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text


async def add_colleague(client, email: str, password: str, capabilities: list[str]) -> None:
    """A brand-new custom role holding exactly `capabilities`, and an account on it — the
    same shape `tests/test_resources.py::test_every_resource_endpoint_needs_catalog_manage`
    uses to prove a capability gate, not just an admin-mode one."""
    from core.security import hash_password
    from tests.conftest import add_account

    role = await client.post(
        "/api/admin/roles",
        json={"name": "Colleague", "description": "Narrow.", "capabilities": capabilities},
    )
    assert role.status_code == 201, role.text
    await add_account(email, await hash_password(password), role=role.json()["id"])


async def enable_queue(client) -> None:
    resp = await client.patch(NOTIFICATIONS, json={"enable_walk_in_queue": True})
    assert resp.status_code == 200, resp.text


async def make_service(client, **overrides) -> str:
    body = {"name": "Haircut", "duration_minutes": 30, "price_cents": 4000}
    body.update(overrides)
    created = await client.post(SERVICES, json=body)
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def make_customer(client, **overrides) -> str:
    body = {"first_name": "Priya", "last_name": "Nair", "phone": "416-555-0199"}
    body.update(overrides)
    resp = await client.post(CUSTOMERS, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def audit_events() -> list[tuple[str, str, str | None]]:
    async with get_purge_engine().connect() as purge:
        rows = (
            await purge.execute(
                text(
                    "SELECT event_type, target_type, target_id FROM audit_events "
                    "WHERE event_type LIKE 'queue.%' ORDER BY id"
                )
            )
        ).all()
    return [(r.event_type, r.target_type, r.target_id) for r in rows]


# --- the toggle gate ---------------------------------------------------------------------------


async def test_every_queue_route_404s_while_the_toggle_is_off(client):
    await as_admin(client)
    service = await make_service(client)

    refusals = [
        await client.get(QUEUE),
        await client.post(QUEUE, json={"bare_name": "Walk-in Jo", "requested_service_id": service}),
        await client.post(f"{QUEUE}/00000000-0000-0000-0000-000000000000/abandon", json={}),
        # Task 5: `start` is gated the same way as add/list/abandon.
        await client.post(f"{QUEUE}/00000000-0000-0000-0000-000000000000/start", json={}),
    ]
    assert [r.status_code for r in refusals] == [404, 404, 404, 404]


async def test_the_toggle_is_off_by_default_and_the_route_appears_once_it_is_on(client):
    await as_admin(client)
    resp = await client.get(QUEUE)
    assert resp.status_code == 404

    await enable_queue(client)
    resp = await client.get(QUEUE)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"entries": []}


# --- the capability gate -----------------------------------------------------------------------


async def test_adding_an_entry_needs_the_queue_manage_capability(client):
    await as_admin(client)
    await enable_queue(client)
    service = await make_service(client)
    await add_colleague(client, "colleague@cedar.example", "correct horse battery 2", ["admin"])
    client.cookies.clear()
    await as_staff(client, "colleague@cedar.example", "correct horse battery 2")

    refusals = [
        await client.get(QUEUE),
        await client.post(QUEUE, json={"bare_name": "Nope", "requested_service_id": service}),
    ]
    assert [r.status_code for r in refusals] == [403, 403]
    assert {r.json()["code"] for r in refusals} == {"capability_required"}


async def test_the_seeded_staff_role_already_holds_queue_manage(client):
    """Migration 0042 — the same call `forms.issue` already made: front-desk work, on both
    seeded roles, not an Administrator-only escalation."""
    from core.security import hash_password
    from tests.conftest import add_account

    await as_admin(client)
    await enable_queue(client)
    service = await make_service(client)
    # `role=None` defaults to the seeded Staff role (`tests/conftest.py::_ACCOUNT`).
    await add_account("colleague@cedar.example", await hash_password("correct horse battery 2"))
    client.cookies.clear()
    await as_staff(client, "colleague@cedar.example", "correct horse battery 2")

    resp = await client.post(
        QUEUE, json={"bare_name": "Front Desk Add", "requested_service_id": service}
    )
    assert resp.status_code == 201, resp.text


# --- quick-create identity ----------------------------------------------------------------------


async def test_adding_a_bare_name_walk_in_creates_no_customer_record(client):
    await as_admin(client)
    await enable_queue(client)
    service = await make_service(client)

    resp = await client.post(
        QUEUE,
        json={
            "bare_name": "Walk-in Jamie",
            "bare_phone": "604-555-0111",
            "requested_service_id": service,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["bare_name"] == "Walk-in Jamie"
    assert body["bare_phone"] == "604-555-0111"
    assert body["customer"] is None
    assert body["status"] == "waiting"
    assert body["requested_service"] == {"id": service, "name": "Haircut"}

    customers = await client.get(CUSTOMERS)
    assert customers.json()["total"] == 0  # no customer record was ever created


async def test_adding_an_entry_for_a_known_customer(client):
    await as_admin(client)
    await enable_queue(client)
    service = await make_service(client)
    customer = await make_customer(client)

    resp = await client.post(QUEUE, json={"customer_id": customer, "requested_service_id": service})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["customer"]["id"] == customer
    assert body["customer"]["first_name"] == "Priya"
    assert body["bare_name"] is None
    assert body["bare_phone"] is None


async def test_adding_an_entry_refuses_neither_or_both_identities(client):
    await as_admin(client)
    await enable_queue(client)
    service = await make_service(client)
    customer = await make_customer(client)

    neither = await client.post(QUEUE, json={"requested_service_id": service})
    assert neither.status_code == 422

    both = await client.post(
        QUEUE,
        json={"customer_id": customer, "bare_name": "Also Jamie", "requested_service_id": service},
    )
    assert both.status_code == 422


async def test_a_bare_phone_without_a_bare_name_is_refused(client):
    await as_admin(client)
    await enable_queue(client)
    service = await make_service(client)
    customer = await make_customer(client)

    resp = await client.post(
        QUEUE,
        json={
            "customer_id": customer,
            "bare_phone": "604-555-0111",
            "requested_service_id": service,
        },
    )
    assert resp.status_code == 422


async def test_adding_an_entry_refuses_an_unknown_or_inactive_service(client):
    await as_admin(client)
    await enable_queue(client)
    service = await make_service(client)
    deactivate = await client.post(f"{SERVICES}/{service}/deactivate", json={})
    assert deactivate.status_code == 200, deactivate.text

    resp = await client.post(QUEUE, json={"bare_name": "Jo", "requested_service_id": service})
    assert resp.status_code == 404


async def test_adding_an_entry_refuses_an_unknown_customer(client):
    await as_admin(client)
    await enable_queue(client)
    service = await make_service(client)

    resp = await client.post(
        QUEUE,
        json={
            "customer_id": "00000000-0000-0000-0000-000000000000",
            "requested_service_id": service,
        },
    )
    assert resp.status_code == 404


async def test_adding_an_entry_with_a_preferred_staff_member(client):
    await as_admin(client)
    await enable_queue(client)
    service = await make_service(client)
    roster = await client.get("/api/admin/staff")
    me = next(row["id"] for row in roster.json()["staff"] if row["email"] == EMAIL)

    resp = await client.post(
        QUEUE,
        json={"bare_name": "Jo", "requested_service_id": service, "preferred_staff_id": me},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["preferred_staff"]["id"] == me


# --- listing, ordered by arrival -----------------------------------------------------------------


async def test_the_list_is_ordered_by_arrival(client):
    await as_admin(client)
    await enable_queue(client)
    service = await make_service(client)

    first = await client.post(QUEUE, json={"bare_name": "First", "requested_service_id": service})
    second = await client.post(QUEUE, json={"bare_name": "Second", "requested_service_id": service})
    assert first.status_code == second.status_code == 201

    listed = await client.get(QUEUE)
    assert listed.status_code == 200, listed.text
    names = [e["bare_name"] for e in listed.json()["entries"]]
    assert names == ["First", "Second"]


# --- abandon ---------------------------------------------------------------------------------


async def test_abandoning_an_entry_excludes_it_from_the_default_list(client):
    await as_admin(client)
    await enable_queue(client)
    service = await make_service(client)
    added = await client.post(QUEUE, json={"bare_name": "Gone", "requested_service_id": service})
    entry_id = added.json()["id"]

    abandoned = await client.post(f"{QUEUE}/{entry_id}/abandon", json={})
    assert abandoned.status_code == 200, abandoned.text
    assert abandoned.json()["status"] == "abandoned"

    default_list = await client.get(QUEUE)
    assert default_list.json()["entries"] == []

    full_list = await client.get(QUEUE, params={"include_abandoned": "true"})
    statuses = [e["status"] for e in full_list.json()["entries"]]
    assert statuses == ["abandoned"]


async def test_abandoning_is_audited(client):
    await as_admin(client)
    await enable_queue(client)
    service = await make_service(client)
    added = await client.post(QUEUE, json={"bare_name": "Gone", "requested_service_id": service})
    entry_id = added.json()["id"]

    await client.post(f"{QUEUE}/{entry_id}/abandon", json={})

    events = await audit_events()
    assert ("queue.entry_added", "queue_entry", entry_id) in events
    assert ("queue.entry_abandoned", "queue_entry", entry_id) in events


async def test_abandoning_an_already_abandoned_entry_is_refused(client):
    await as_admin(client)
    await enable_queue(client)
    service = await make_service(client)
    added = await client.post(QUEUE, json={"bare_name": "Gone", "requested_service_id": service})
    entry_id = added.json()["id"]
    await client.post(f"{QUEUE}/{entry_id}/abandon", json={})

    again = await client.post(f"{QUEUE}/{entry_id}/abandon", json={})
    assert again.status_code == 409
    assert again.json()["code"] == "invalid_transition"


async def test_abandoning_an_unknown_entry_is_a_404(client):
    await as_admin(client)
    await enable_queue(client)
    resp = await client.post(f"{QUEUE}/00000000-0000-0000-0000-000000000000/abandon", json={})
    assert resp.status_code == 404

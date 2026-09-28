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
from tests.test_form_compliance import _essential_template, _sign_v1
from tests.test_forms import _wipe_forms

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
    # Task 7's own tests are the first thing in this file to touch form templates — wiped the
    # same way `tests/test_forms.py`'s own fixture does (owner-role delete; `form_templates`/
    # `form_template_versions`/`form_submissions` are immutable to the app role), both before
    # and after, so a template created here never leaks into another file's compliance test.
    await _wipe_forms()

    async def wipe():
        async with get_purge_engine().begin() as purge:
            await purge.execute(text("DELETE FROM audit_events"))
            await purge.execute(text("DELETE FROM erasure_requests"))
            await purge.execute(text("DELETE FROM form_links"))
        await wipe_document_keys()
        async with session_scope() as db:
            for table in (
                # #59's draft bill lines/bills FK to appointments with no cascade — deleted
                # first, same reason queue_entries already precedes appointments below.
                "service_bill_lines",
                "service_bills",
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
    await _wipe_forms()


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


async def make_service(client, staff_ids: list[str] | None = None, **overrides) -> str:
    body = {"name": "Haircut", "duration_minutes": 30, "price_cents": 4000}
    body.update(overrides)
    created = await client.post(SERVICES, json=body)
    assert created.status_code == 201, created.text
    service_id = created.json()["id"]
    if staff_ids is not None:
        linked = await client.put(f"{SERVICES}/{service_id}/staff", json={"staff_ids": staff_ids})
        assert linked.status_code == 200, linked.text
    return service_id


async def add_staff(client, email: str, password: str) -> str:
    """A second staff account, purely as a body eligible to deliver a service — Task 6's
    "available staff count" needs more than one candidate to prove the split arithmetic."""
    from core.security import hash_password
    from tests.conftest import add_account

    await add_account(email, await hash_password(password))
    roster = await client.get("/api/admin/staff")
    return next(row["id"] for row in roster.json()["staff"] if row["email"] == email)


async def me_staff_id(client) -> str:
    roster = await client.get("/api/admin/staff")
    return next(row["id"] for row in roster.json()["staff"] if row["email"] == EMAIL)


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


# --- wait estimate (Task 6) ---------------------------------------------------------------------


async def test_the_first_waiting_entry_has_a_zero_minute_estimate(client):
    await as_admin(client)
    await enable_queue(client)
    me = await me_staff_id(client)
    service = await make_service(client, staff_ids=[me])

    # `add`'s own single-entry response has no view of who else is ahead of it — only the list
    # endpoint computes this (module docstring's own documented call).
    added = await client.post(QUEUE, json={"bare_name": "Jo", "requested_service_id": service})
    assert added.json()["estimated_wait_minutes"] is None

    listed = await client.get(QUEUE)
    assert listed.json()["entries"][0]["estimated_wait_minutes"] == 0


async def test_the_estimate_splits_remaining_service_time_across_available_staff(client):
    await as_admin(client)
    await enable_queue(client)
    me = await me_staff_id(client)
    colleague = await add_staff(client, "colleague@cedar.example", "correct horse battery 2")
    # 30-minute service, two eligible staff, neither with any conflicting appointment: the
    # literal arithmetic `test_queue_wait.py::test_split_across_available_staff` already pins.
    service = await make_service(client, staff_ids=[me, colleague], duration_minutes=30)

    await client.post(QUEUE, json={"bare_name": "First", "requested_service_id": service})
    await client.post(QUEUE, json={"bare_name": "Second", "requested_service_id": service})
    await client.post(QUEUE, json={"bare_name": "Third", "requested_service_id": service})

    listed = await client.get(QUEUE)
    estimates = [e["estimated_wait_minutes"] for e in listed.json()["entries"]]
    assert estimates == [0, 15, 30]


async def test_zero_eligible_staff_still_returns_a_finite_floored_estimate(client):
    """No staff assigned to the service at all — `available_staff_count` is 0, floored to 1
    (`queue_wait.py`'s own documented choice) rather than raising or reporting an instant "0"
    wait for someone who is, in fact, second in an unstaffed line."""
    await as_admin(client)
    await enable_queue(client)
    service = await make_service(client, staff_ids=[], duration_minutes=30)

    await client.post(QUEUE, json={"bare_name": "First", "requested_service_id": service})
    await client.post(QUEUE, json={"bare_name": "Second", "requested_service_id": service})

    listed = await client.get(QUEUE)
    estimates = [e["estimated_wait_minutes"] for e in listed.json()["entries"]]
    assert estimates == [0, 30]


async def test_a_non_waiting_entry_carries_no_wait_estimate(client):
    await as_admin(client)
    await enable_queue(client)
    me = await me_staff_id(client)
    service = await make_service(client, staff_ids=[me])
    added = await client.post(QUEUE, json={"bare_name": "Gone", "requested_service_id": service})
    entry_id = added.json()["id"]

    abandoned = await client.post(f"{QUEUE}/{entry_id}/abandon", json={})
    assert abandoned.json()["estimated_wait_minutes"] is None

    full_list = await client.get(QUEUE, params={"include_abandoned": "true"})
    entry = next(e for e in full_list.json()["entries"] if e["id"] == entry_id)
    assert entry["estimated_wait_minutes"] is None


# --- essential-form gaps (Task 7) ----------------------------------------------------------------


async def test_a_missing_essential_form_shows_as_a_gap_and_clears_once_signed(client):
    """The literal acceptance criterion: computed live at every `list_queue` read, never cached
    at add-time — signing the form between two `GET`s changes the second read's answer."""
    await as_admin(client)
    await enable_queue(client)
    service = await make_service(client)
    template = await _essential_template(client)  # applies_to_all=True — no appointment needed
    customer_id = await make_customer(client)
    await client.post(QUEUE, json={"customer_id": customer_id, "requested_service_id": service})

    before = await client.get(QUEUE)
    assert before.json()["entries"][0]["compliance_gaps"] == [
        {"template_id": template["id"], "name": template["name"], "status": "missing"}
    ]

    await _sign_v1(client, customer_id, template["id"])

    after = await client.get(QUEUE)
    assert after.json()["entries"][0]["compliance_gaps"] == []


async def test_a_fully_compliant_customers_entry_shows_no_gaps(client):
    await as_admin(client)
    await enable_queue(client)
    service = await make_service(client)
    template = await _essential_template(client)
    customer_id = await make_customer(client)
    await _sign_v1(client, customer_id, template["id"])

    await client.post(QUEUE, json={"customer_id": customer_id, "requested_service_id": service})
    listed = await client.get(QUEUE)
    assert listed.json()["entries"][0]["compliance_gaps"] == []


async def test_a_service_scoped_essential_forms_gap_shows_for_a_waiting_walk_in(client):
    """Fix round: a template mapped to a *specific* service (not `applies_to_all`) used to
    never show for a queue entry at all, because `_applicable_services` only ever checked
    confirmed appointments — a walk-in waiting for that exact service has none yet. This is
    the population #12's "while the client is still waiting" criterion is actually about."""
    await as_admin(client)
    await enable_queue(client)
    me = await me_staff_id(client)
    service = await make_service(client, staff_ids=[me])
    template = await _essential_template(client, service)  # mapped to this service, not all
    customer_id = await make_customer(client)
    await client.post(QUEUE, json={"customer_id": customer_id, "requested_service_id": service})

    listed = await client.get(QUEUE)
    assert listed.json()["entries"][0]["compliance_gaps"] == [
        {"template_id": template["id"], "name": template["name"], "status": "missing"}
    ]


async def test_a_bare_name_entry_has_no_chart_and_shows_the_same_empty_shape_as_compliant(client):
    """No `customer_id` at all to check — mirrors `_compliance`'s own "nothing to report" shape
    for a compliant customer (an empty list), never a third shape for "no chart to check"."""
    await as_admin(client)
    await enable_queue(client)
    service = await make_service(client)
    await _essential_template(client)  # a real gap exists for anyone with a chart to check

    await client.post(QUEUE, json={"bare_name": "Walk-in Jamie", "requested_service_id": service})
    listed = await client.get(QUEUE)
    assert listed.json()["entries"][0]["compliance_gaps"] == []


async def test_single_entry_responses_do_not_compute_compliance_gaps(client):
    """Same "not computed here" convention `estimated_wait_minutes` already uses — only
    `list_queue` has paid for the one bulk `_compliance` query."""
    await as_admin(client)
    await enable_queue(client)
    service = await make_service(client)
    await _essential_template(client)
    customer_id = await make_customer(client)

    added = await client.post(
        QUEUE, json={"customer_id": customer_id, "requested_service_id": service}
    )
    assert added.json()["compliance_gaps"] is None

    entry_id = added.json()["id"]
    abandoned = await client.post(f"{QUEUE}/{entry_id}/abandon", json={})
    assert abandoned.json()["compliance_gaps"] is None

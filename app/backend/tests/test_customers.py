"""S1: the minimal customer record — enough of a person to book for.

Name and contact details only. Phase 3 (#7) grows this in place; what is pinned here is the
part the booking screen depends on: create, search by name, phone or email prefix, and a
case-insensitive unique email.
"""

import pytest
from sqlalchemy import text

from core.db import get_purge_engine, session_scope
from tests.conftest import get_owner_engine, wipe_document_keys

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"
OTHER_PASSWORD = "correct horse battery 2"

SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": EMAIL,
    "admin_password": PASSWORD,
}

CUSTOMERS = "/api/customers"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async with get_owner_engine().begin() as owner:
        await owner.execute(text("DELETE FROM audit_events"))
    await wipe_document_keys()
    async with session_scope() as db:
        for table in (
            # #59's draft bill lines/bills FK to appointments with no cascade — deleted
            # first, same reason queue_entries already precedes appointments below.
            "service_bill_lines",
            "service_bills",
            # A leftover queue entry (Phase 7 Task 1, #12) FKs to customers/staff with no
            # cascade — deleted first, same reason appointments precedes customers below.
            "queue_entries",
            "appointment_resources",
            "appointments",
            "customers",
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

    await get_redis().delete(throttle.delay_key(EMAIL), throttle.delay_key("desk@cedar.example"))

    async with session_scope() as db:
        token = await setup.bootstrap_setup_token(db)
    resp = await client.post("/api/setup", json={**SETUP, "token": token})
    assert resp.status_code == 201, resp.text
    async with session_scope() as db:
        await db.execute(text("UPDATE businesses SET mfa_required_for_admin = false"))
        await db.commit()
    client.cookies.clear()
    yield


async def sign_in(client, email=EMAIL, password=PASSWORD):
    login = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text


async def add_colleague(email: str, password: str, *, role: str = "Staff") -> None:
    from core.security import hash_password
    from tests.conftest import add_account

    async with session_scope() as db:
        role_id = str(await db.scalar(text("SELECT id FROM roles WHERE name = :n"), {"n": role}))
    await add_account(email, await hash_password(password), role=role_id)


async def test_a_customer_is_created_with_normalised_contact_details_and_audited(client):
    await sign_in(client)

    resp = await client.post(
        CUSTOMERS,
        json={
            "first_name": "  Priya ",
            "last_name": "Nair",
            "email": "Priya.Nair@Example.com",
            "phone": "(416) 555-0199",
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["first_name"] == "Priya"
    assert body["last_name"] == "Nair"
    assert body["email"] == "priya.nair@example.com"
    assert body["phone"] == "4165550199"
    assert body["created_at"]

    async with get_purge_engine().connect() as purge:
        events = (
            await purge.execute(
                text(
                    "SELECT event_type, target_type, target_id FROM audit_events "
                    "WHERE event_type LIKE 'customer.%'"
                )
            )
        ).all()
    assert [(e.event_type, e.target_type, e.target_id) for e in events] == [
        ("customer.created", "customer", body["id"])
    ]


async def test_email_and_phone_are_optional_and_blank_is_none(client):
    await sign_in(client)

    resp = await client.post(
        CUSTOMERS, json={"first_name": "Walk", "last_name": "In", "email": "", "phone": " "}
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["email"] is None
    assert resp.json()["phone"] is None


async def test_a_duplicate_email_is_a_409_case_insensitively(client):
    await sign_in(client)
    first = await client.post(
        CUSTOMERS, json={"first_name": "A", "last_name": "B", "email": "same@example.com"}
    )
    assert first.status_code == 201, first.text

    resp = await client.post(
        CUSTOMERS, json={"first_name": "C", "last_name": "D", "email": "SAME@example.com"}
    )

    assert resp.status_code == 409, resp.text


async def test_a_blank_name_is_refused(client):
    await sign_in(client)

    resp = await client.post(CUSTOMERS, json={"first_name": "  ", "last_name": "Nair"})

    assert resp.status_code == 422, resp.text


async def test_search_matches_name_phone_and_email_prefixes(client):
    await sign_in(client)
    for first, last, email, phone in (
        ("Priya", "Nair", "priya@example.com", "416-555-0199"),
        ("Sam", "Okonkwo", "sam@example.com", "647-555-0100"),
        ("Samira", "Haddad", None, None),
    ):
        made = await client.post(
            CUSTOMERS,
            json={"first_name": first, "last_name": last, "email": email, "phone": phone},
        )
        assert made.status_code == 201, made.text

    def names(resp):
        assert resp.status_code == 200, resp.text
        return [f"{c['first_name']} {c['last_name']}" for c in resp.json()["customers"]]

    assert names(await client.get(CUSTOMERS, params={"q": "sam"})) == [
        "Samira Haddad",
        "Sam Okonkwo",
    ]
    assert names(await client.get(CUSTOMERS, params={"q": "nai"})) == ["Priya Nair"]
    assert names(await client.get(CUSTOMERS, params={"q": "(416) 555"})) == ["Priya Nair"]
    assert names(await client.get(CUSTOMERS, params={"q": "PRIYA@"})) == ["Priya Nair"]
    assert names(await client.get(CUSTOMERS, params={"q": "zzz"})) == []
    # No query is the whole list, alphabetical by last name then first, with its total.
    everyone = await client.get(CUSTOMERS)
    assert names(everyone) == ["Samira Haddad", "Priya Nair", "Sam Okonkwo"]
    assert everyone.json()["total"] == 3


async def test_the_list_is_paginated_and_reports_the_total(client):
    await sign_in(client)
    for last in ("Bravo", "alpha", "Charlie", "Delta", "Echo"):
        made = await client.post(CUSTOMERS, json={"first_name": "X", "last_name": last})
        assert made.status_code == 201, made.text

    def lasts(resp):
        assert resp.status_code == 200, resp.text
        return [c["last_name"] for c in resp.json()["customers"]]

    first = await client.get(CUSTOMERS, params={"page": 1, "page_size": 2})
    second = await client.get(CUSTOMERS, params={"page": 2, "page_size": 2})
    third = await client.get(CUSTOMERS, params={"page": 3, "page_size": 2})
    beyond = await client.get(CUSTOMERS, params={"page": 4, "page_size": 2})

    # Case-insensitive: "alpha" sorts with the As, not after the Zs.
    assert lasts(first) == ["alpha", "Bravo"]
    assert lasts(second) == ["Charlie", "Delta"]
    assert lasts(third) == ["Echo"]
    assert lasts(beyond) == []
    assert all(r.json()["total"] == 5 for r in (first, second, third, beyond))
    # A search is paginated the same way, and its total is the number of matches.
    matched = await client.get(CUSTOMERS, params={"q": "x", "page_size": 100})
    assert matched.json()["total"] == 5
    assert (await client.get(CUSTOMERS, params={"page_size": 101})).status_code == 422
    assert (await client.get(CUSTOMERS, params={"page": 0})).status_code == 422


async def test_duplicate_names_across_a_page_boundary_are_stable_and_never_lost(client):
    """The sort key alone (last, first) ties for identical names, and a tied order is free
    to reshuffle between the two queries a paginated read makes — the count and the page
    could each see a different arbitrary ordering. `id` as the final key removes the tie."""
    await sign_in(client)
    for _ in range(5):
        made = await client.post(CUSTOMERS, json={"first_name": "Sam", "last_name": "Okonkwo"})
        assert made.status_code == 201, made.text

    seen = []
    for page in (1, 2, 3):
        resp = await client.get(CUSTOMERS, params={"page": page, "page_size": 2})
        assert resp.status_code == 200, resp.text
        seen += [c["id"] for c in resp.json()["customers"]]

    assert len(seen) == 5
    assert len(set(seen)) == 5


async def test_the_two_capabilities_gate_the_two_verbs(client):
    role = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert role.status_code == 200
    mode = await client.post("/api/auth/mode", json={"mode": "admin", "password": PASSWORD})
    assert mode.status_code == 200, mode.text
    viewer = await client.post(
        "/api/admin/roles",
        json={"name": "Viewer", "description": "Looks.", "capabilities": ["customers.view"]},
    )
    assert viewer.status_code == 201, viewer.text
    await add_colleague("desk@cedar.example", OTHER_PASSWORD, role="Viewer")
    client.cookies.clear()
    await sign_in(client, "desk@cedar.example", OTHER_PASSWORD)

    listed = await client.get(CUSTOMERS, params={"q": "x"})
    created = await client.post(CUSTOMERS, json={"first_name": "A", "last_name": "B"})

    assert listed.status_code == 200, listed.text
    assert created.status_code == 403, created.text
    assert created.json()["code"] == "capability_required"


async def test_a_role_that_may_only_see_the_schedule_may_not_search_customers(client):
    await sign_in(client)
    mode = await client.post("/api/auth/mode", json={"mode": "admin", "password": PASSWORD})
    assert mode.status_code == 200, mode.text
    viewer = await client.post(
        "/api/admin/roles",
        json={"name": "Looker", "description": "Looks.", "capabilities": ["schedule.view"]},
    )
    assert viewer.status_code == 201, viewer.text
    await add_colleague("desk@cedar.example", OTHER_PASSWORD, role="Looker")
    client.cookies.clear()
    await sign_in(client, "desk@cedar.example", OTHER_PASSWORD)

    resp = await client.get(CUSTOMERS, params={"q": "x"})

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "capability_required"


async def test_nobody_signed_in_is_refused(client):
    assert (await client.get(CUSTOMERS)).status_code == 401

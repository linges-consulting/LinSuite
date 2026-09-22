"""S1: the grown profile (Task 3, #38) — editing it, and the classification derived from it.

`PATCH` is the one route under test that is not a `GET`. It never returns PHI — its response
is `CustomerOut`, the same shape the list and the search send, never `CustomerDetailOut` — so
it needs neither `LogAccess` nor an `audit_access_log` row of its own; a no-change (or a
`PATCH {}`) request is a 200 with nothing to show for it in either audit trail. What this file
proves: it is audited in `audit_events` by field name only, a no-change request audits
nothing, the response itself never carries a PHI field, opening it never writes an access-log
row, the same normalisation `CustomerIn` applies reaches the two new contacts, an explicit
`null` for a required name is a 422 rather than a 500, and `customers.manage` gates it the
same way it gates `POST`. `tests/test_access_log.py`'s route enumeration is the general
version of the PHI-response claim, proven against every route in the app; this file is the
one for this specific route's behaviour.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.db import get_purge_engine, session_scope
from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    APPOINTMENTS,
    CUSTOMERS,
    EMAIL,
    OTHER_PASSWORD,
    PASSWORD,
    as_admin,
    as_staff,
    at,
    audit_events,
    book,
    claimed_instance,
    make_customer,
    make_service,
    me_staff_id,
    put_hours,
)
from tests.test_lifecycle import push_to_past

BUSINESS = "/api/admin/business"

# The fields `CustomerOut` must never carry — see `CustomerDetailOut` in customers/routes.py.
_PHI_KEYS = (
    "date_of_birth",
    "emergency_contact_name",
    "emergency_contact_phone",
    "emergency_contact_relationship",
    "secondary_contact_name",
    "secondary_contact_phone",
    "secondary_contact_email",
    "notes",
)


async def access_rows_for(customer_id: str) -> list[dict]:
    async with get_purge_engine().connect() as purge:
        rows = (
            await purge.execute(
                text("SELECT action FROM audit_access_log WHERE customer_id = :id"),
                {"id": customer_id},
            )
        ).all()
    return [dict(r._mapping) for r in rows]


async def add_role(client, name: str, capabilities: list[str], email: str) -> None:
    from core.security import hash_password
    from tests.conftest import add_account

    made = await client.post(
        "/api/admin/roles",
        json={"name": name, "description": "Test role.", "capabilities": capabilities},
    )
    assert made.status_code == 201, made.text
    await add_account(email, await hash_password(OTHER_PASSWORD), role=made.json()["id"])


async def ready_customer(client) -> tuple[str, str, str]:
    """A staff member with a wide-open Monday and a service, and a customer with no visits
    yet. Returns (customer_id, service_id, staff_id)."""
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 1260)])  # 9:00–21:00
    service = await make_service(client, [me])
    customer_id = await make_customer(client)
    return customer_id, service, me


async def complete_visits(client, service: str, staff_id: str, customer_id: str, n: int) -> None:
    for i in range(n):
        made = await book(client, service, staff_id, at(f"{9 + i}:00"), customer_id=customer_id)
        assert made.status_code == 201, made.text
        done = await client.post(f"{APPOINTMENTS}/{made.json()['id']}/complete", json={})
        assert done.status_code == 200, done.text


async def business_profile(client) -> dict:
    resp = await client.get(BUSINESS)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def set_vip_threshold(client, threshold: int):
    profile = await business_profile(client)
    profile["vip_visit_threshold"] = threshold
    resp = await client.put(BUSINESS, json=profile)
    assert resp.status_code == 200, resp.text
    return resp


# --- PATCH: editing the profile -------------------------------------------------------------


async def test_patch_updates_dob_both_contacts_and_notes_and_a_subsequent_get_reflects_it(client):
    """The PATCH response itself carries none of this
    (`test_the_patch_response_never_carries_a_phi_field`) — the edit is only visible through
    the profile `GET` that follows, which is the point."""
    customer_id, _, _ = await ready_customer(client)

    resp = await client.patch(
        f"{CUSTOMERS}/{customer_id}",
        json={
            "date_of_birth": "1990-05-14",
            "emergency_contact_name": "  Amira Haddad ",
            "emergency_contact_phone": "(416) 555-0111",
            "emergency_contact_relationship": "Sister",
            "secondary_contact_name": "Leo Haddad",
            "secondary_contact_phone": "647.555.0122",
            "secondary_contact_email": "Leo.Haddad@Example.com",
            "notes": "Prefers the corner chair.",
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["classification"] == "new"

    profile = await client.get(f"{CUSTOMERS}/{customer_id}")
    assert profile.status_code == 200, profile.text
    fetched = profile.json()["customer"]
    assert fetched["date_of_birth"] == "1990-05-14"
    assert fetched["emergency_contact_name"] == "Amira Haddad"
    assert fetched["emergency_contact_phone"] == "4165550111"
    assert fetched["emergency_contact_relationship"] == "Sister"
    assert fetched["secondary_contact_name"] == "Leo Haddad"
    assert fetched["secondary_contact_phone"] == "6475550122"
    assert fetched["secondary_contact_email"] == "leo.haddad@example.com"
    assert fetched["notes"] == "Prefers the corner chair."


async def test_a_dob_in_the_future_is_a_422(client):
    customer_id, _, _ = await ready_customer(client)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    resp = await client.patch(f"{CUSTOMERS}/{customer_id}", json={"date_of_birth": tomorrow})

    assert resp.status_code == 422, resp.text


async def test_a_dob_before_1900_is_a_422(client):
    customer_id, _, _ = await ready_customer(client)

    resp = await client.patch(f"{CUSTOMERS}/{customer_id}", json={"date_of_birth": "1899-12-31"})

    assert resp.status_code == 422, resp.text


async def test_a_duplicate_email_on_patch_is_a_409(client):
    await as_admin(client)
    taken = await client.post(
        CUSTOMERS, json={"first_name": "A", "last_name": "B", "email": "taken@example.com"}
    )
    assert taken.status_code == 201, taken.text
    made = await client.post(CUSTOMERS, json={"first_name": "C", "last_name": "D"})
    customer_id = made.json()["id"]

    resp = await client.patch(f"{CUSTOMERS}/{customer_id}", json={"email": "TAKEN@example.com"})

    assert resp.status_code == 409, resp.text


async def test_patch_records_exactly_one_audit_event_naming_the_changed_fields_only(client):
    customer_id, _, _ = await ready_customer(client)

    resp = await client.patch(
        f"{CUSTOMERS}/{customer_id}",
        json={"notes": "Allergic to lavender oil.", "date_of_birth": "1985-01-01"},
    )
    assert resp.status_code == 200, resp.text

    events = [e for e in await audit_events() if e[0] == "customer.updated"]
    assert len(events) == 1
    event_type, target_type, target_id, metadata = events[0]
    assert target_type == "customer"
    assert target_id == customer_id
    assert sorted(metadata["changed"]) == ["date_of_birth", "notes"]
    # Field names only — never the value that was set.
    assert "Allergic to lavender oil." not in str(metadata)
    assert "1985-01-01" not in str(metadata)


async def test_a_no_change_patch_records_nothing(client):
    customer_id, _, _ = await ready_customer(client)
    first = await client.patch(f"{CUSTOMERS}/{customer_id}", json={"notes": "Same every time."})
    assert first.status_code == 200, first.text

    resp = await client.patch(f"{CUSTOMERS}/{customer_id}", json={"notes": "Same every time."})

    assert resp.status_code == 200, resp.text
    events = [e for e in await audit_events() if e[0] == "customer.updated"]
    assert len(events) == 1


async def test_an_omitted_field_is_left_alone_not_cleared(client):
    customer_id, _, _ = await ready_customer(client)
    first = await client.patch(
        f"{CUSTOMERS}/{customer_id}", json={"notes": "Keep me.", "date_of_birth": "1990-01-01"}
    )
    assert first.status_code == 200, first.text

    resp = await client.patch(f"{CUSTOMERS}/{customer_id}", json={"date_of_birth": "1991-01-01"})
    assert resp.status_code == 200, resp.text

    profile = await client.get(f"{CUSTOMERS}/{customer_id}")
    fetched = profile.json()["customer"]
    assert fetched["notes"] == "Keep me."
    assert fetched["date_of_birth"] == "1991-01-01"


async def test_a_customers_view_only_role_gets_403_on_patch(client):
    customer_id, _, _ = await ready_customer(client)
    await add_role(client, "Looker", ["customers.view"], "desk@cedar.example")
    client.cookies.clear()
    await as_staff(client, "desk@cedar.example", OTHER_PASSWORD)

    resp = await client.patch(f"{CUSTOMERS}/{customer_id}", json={"notes": "Nope."})

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "capability_required"


# --- PATCH never discloses PHI, logged or not (fix round 1) -------------------------------


async def test_the_patch_response_never_carries_a_phi_field(client):
    """A `customers.manage` session editing a client's notes still must not get the DOB, the
    contacts or the notes back — the response is the same `CustomerOut` shape the list uses,
    never `CustomerDetailOut`. Regression: it used to return the full profile."""
    customer_id, _, _ = await ready_customer(client)

    resp = await client.patch(
        f"{CUSTOMERS}/{customer_id}",
        json={"notes": "Prefers the corner chair.", "date_of_birth": "1990-01-01"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in _PHI_KEYS:
        assert key not in body, f"{key} leaked onto the PATCH response"
    assert set(body) == {
        "id",
        "first_name",
        "last_name",
        "email",
        "phone",
        "created_at",
        "classification",
    }


async def test_an_empty_patch_is_a_200_with_no_phi_and_writes_no_access_row(client):
    """`PATCH {}` — nothing to change — used to be a free, unlogged read of the whole
    profile: 200, full PHI body, no `audit_events` row (nothing changed) and no
    `audit_access_log` row (no `LogAccess`). It must now disclose nothing at all."""
    customer_id, _, _ = await ready_customer(client)

    resp = await client.patch(f"{CUSTOMERS}/{customer_id}", json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in _PHI_KEYS:
        assert key not in body, f"{key} leaked onto an empty PATCH response"
    assert await access_rows_for(customer_id) == []
    assert [e for e in await audit_events() if e[0] == "customer.updated"] == []


async def test_an_explicit_null_first_name_is_a_422_not_a_500(client):
    customer_id, _, _ = await ready_customer(client)

    resp = await client.patch(f"{CUSTOMERS}/{customer_id}", json={"first_name": None})

    assert resp.status_code == 422, resp.text


async def test_an_explicit_null_last_name_is_a_422_not_a_500(client):
    customer_id, _, _ = await ready_customer(client)

    resp = await client.patch(f"{CUSTOMERS}/{customer_id}", json={"last_name": None})

    assert resp.status_code == 422, resp.text


# --- classification --------------------------------------------------------------------------


async def test_no_completed_appointments_is_new(client):
    customer_id, _, _ = await ready_customer(client)

    resp = await client.get(f"{CUSTOMERS}/{customer_id}")

    assert resp.json()["customer"]["classification"] == "new"


async def test_one_completed_and_one_upcoming_is_repeat(client):
    customer_id, service, staff_id = await ready_customer(client)
    await complete_visits(client, service, staff_id, customer_id, 1)
    upcoming = await book(client, service, staff_id, at("11:00"), customer_id=customer_id)
    assert upcoming.status_code == 201, upcoming.text

    resp = await client.get(f"{CUSTOMERS}/{customer_id}")

    assert resp.json()["customer"]["classification"] == "repeat"


async def test_three_completed_with_threshold_three_is_vip(client):
    customer_id, service, staff_id = await ready_customer(client)
    await set_vip_threshold(client, 3)
    await complete_visits(client, service, staff_id, customer_id, 3)

    resp = await client.get(f"{CUSTOMERS}/{customer_id}")

    assert resp.json()["customer"]["classification"] == "vip"


async def test_cancelled_and_no_show_visits_do_not_count(client):
    customer_id, service, staff_id = await ready_customer(client)
    await set_vip_threshold(client, 2)
    await complete_visits(client, service, staff_id, customer_id, 1)

    cancel_me = await book(client, service, staff_id, at("11:00"), customer_id=customer_id)
    assert cancel_me.status_code == 201, cancel_me.text
    cancelled = await client.post(
        f"{APPOINTMENTS}/{cancel_me.json()['id']}/cancel", json={"reason": "Client called."}
    )
    assert cancelled.status_code == 200, cancelled.text

    no_show_me = await book(client, service, staff_id, at("12:00"), customer_id=customer_id)
    assert no_show_me.status_code == 201, no_show_me.text
    await push_to_past(no_show_me.json()["id"])
    missed = await client.post(f"{APPOINTMENTS}/{no_show_me.json()['id']}/no-show", json={})
    assert missed.status_code == 200, missed.text

    resp = await client.get(f"{CUSTOMERS}/{customer_id}")

    assert resp.json()["customer"]["classification"] == "repeat"


async def test_lowering_the_threshold_reclassifies_on_the_next_read_with_no_write(client):
    customer_id, service, staff_id = await ready_customer(client)
    await complete_visits(client, service, staff_id, customer_id, 3)

    before = await client.get(f"{CUSTOMERS}/{customer_id}")
    assert before.json()["customer"]["classification"] == "repeat"

    async with session_scope() as db:
        stamp = await db.scalar(
            text("SELECT updated_at FROM customers WHERE id = :id"), {"id": customer_id}
        )

    await set_vip_threshold(client, 3)

    after = await client.get(f"{CUSTOMERS}/{customer_id}")
    assert after.json()["customer"]["classification"] == "vip"
    async with session_scope() as db:
        restamp = await db.scalar(
            text("SELECT updated_at FROM customers WHERE id = :id"), {"id": customer_id}
        )
    assert restamp == stamp


async def test_the_list_endpoint_returns_classification_on_every_row(client):
    customer_id, service, staff_id = await ready_customer(client)
    await set_vip_threshold(client, 2)
    await complete_visits(client, service, staff_id, customer_id, 2)
    other_id = await make_customer(client)

    resp = await client.get(CUSTOMERS)

    assert resp.status_code == 200, resp.text
    by_id = {c["id"]: c["classification"] for c in resp.json()["customers"]}
    assert by_id[customer_id] == "vip"
    assert by_id[other_id] == "new"


# --- vip_visit_threshold on the business profile ---------------------------------------------


async def test_vip_visit_threshold_accepts_the_documented_range(client):
    await as_admin(client)

    resp = await set_vip_threshold(client, 250)

    assert resp.json()["vip_visit_threshold"] == 250


async def test_vip_visit_threshold_of_one_is_a_422(client):
    await as_admin(client)
    profile = await business_profile(client)
    profile["vip_visit_threshold"] = 1

    resp = await client.put(BUSINESS, json=profile)

    assert resp.status_code == 422, resp.text


async def test_the_database_refuses_a_vip_visit_threshold_of_one_directly(database):
    """S5: the API's `Field(ge=2, le=1000)` is the 422 a person sees first; the CHECK
    declared on the model (migration 0020) is what still holds if a write reaches Postgres
    some other way — a data fix in `psql`, a future import."""
    async with session_scope() as db:
        with pytest.raises(DBAPIError) as refused:
            await db.execute(text("UPDATE businesses SET vip_visit_threshold = 1"))
        await db.rollback()
    # 23514 check_violation.
    assert getattr(refused.value.orig, "sqlstate", None) == "23514", refused.value

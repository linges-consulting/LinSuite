"""S1: discount definitions (#58) — the admin CRUD surface. The resolved-amount math itself
is S2-tested in `tests/test_discount_resolver.py`; this file only proves the definitions
survive the database, are gated by `billing.manage`/Admin Mode, and toggle without ever being
hard-deleted.
"""

import pytest
from sqlalchemy import text

from core.db import session_scope
from tests.conftest import get_owner_engine

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"

SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": EMAIL,
    "admin_password": PASSWORD,
}

DISCOUNTS = "/api/admin/discounts"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async with get_owner_engine().begin() as owner:
        await owner.execute(text("DELETE FROM audit_events"))
    async with session_scope() as db:
        for table in (
            "discount_eligible_items",
            "retail_sale_discounts",  # review T1, 0065
            "discounts",
            "password_reset_tokens",
            "staff",
            "users",
            "businesses",
            "setup_token",
        ):
            await db.execute(text(f"DELETE FROM {table}"))
        await db.execute(text("DELETE FROM roles WHERE NOT is_system"))
        await db.commit()

    from auth import setup

    async with session_scope() as db:
        token = await setup.bootstrap_setup_token(db)
    resp = await client.post("/api/setup", json={**SETUP, "token": token})
    assert resp.status_code == 201, resp.text
    async with session_scope() as db:
        await db.execute(text("UPDATE businesses SET mfa_required_for_admin = false"))
        await db.commit()
    client.cookies.clear()
    yield


async def as_admin(client, email=EMAIL, password=PASSWORD):
    login = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    resp = await client.post("/api/auth/mode", json={"mode": "admin", "password": password})
    assert resp.status_code == 200, resp.text


def percentage(**overrides) -> dict:
    body = {
        "name": "Autumn 10%",
        "kind": "percentage",
        "percentage_bp": 1000,
        "commission_basis": "reduces",
    }
    body.update(overrides)
    return body


def fixed(**overrides) -> dict:
    body = {
        "name": "$5 off",
        "kind": "fixed",
        "amount_cents": 500,
        "commission_basis": "absorbed",
    }
    body.update(overrides)
    return body


async def make(client, **overrides) -> dict:
    resp = await client.post(DISCOUNTS, json=percentage(**overrides))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def events() -> list[str]:
    async with session_scope() as db:
        rows = await db.execute(
            text("SELECT event_type FROM audit_events ORDER BY occurred_at, id")
        )
        return [row[0] for row in rows.all()]


# --- creating -------------------------------------------------------------------------------


async def test_creating_a_percentage_discount(client):
    await as_admin(client)

    created = await make(client)

    assert created["kind"] == "percentage"
    assert created["percentage_bp"] == 1000
    assert created["amount_cents"] is None
    assert created["eligibility_scope"] == "all"
    assert created["stackable"] is False
    assert created["commission_basis"] == "reduces"
    assert created["enabled"] is True
    assert created["eligible_items"] == []
    assert "discount.created" in await events()


async def test_creating_a_fixed_discount(client):
    await as_admin(client)

    resp = await client.post(DISCOUNTS, json=fixed())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "fixed"
    assert body["amount_cents"] == 500
    assert body["percentage_bp"] is None


async def test_a_percentage_discount_may_not_also_carry_amount_cents(client):
    await as_admin(client)

    resp = await client.post(DISCOUNTS, json=percentage(amount_cents=500))
    assert resp.status_code == 422, resp.text


async def test_a_fixed_discount_needs_amount_cents(client):
    await as_admin(client)

    resp = await client.post(
        DISCOUNTS, json={"name": "x", "kind": "fixed", "commission_basis": "absorbed"}
    )
    assert resp.status_code == 422, resp.text


async def test_percentage_bp_is_capped_at_10000(client):
    await as_admin(client)

    resp = await client.post(DISCOUNTS, json=percentage(percentage_bp=10_001))
    assert resp.status_code == 422, resp.text


# --- reading ---------------------------------------------------------------------------------


async def test_listing_hides_disabled_discounts_by_default(client):
    await as_admin(client)
    created = await make(client)
    await client.post(f"{DISCOUNTS}/{created['id']}/disable", json={})

    resp = await client.get(DISCOUNTS)
    assert resp.json()["discounts"] == []

    resp = await client.get(DISCOUNTS, params={"include_disabled": True})
    assert len(resp.json()["discounts"]) == 1


# --- editing ------------------------------------------------------------------------------


async def test_editing_a_discount(client):
    await as_admin(client)
    created = await make(client)

    resp = await client.patch(f"{DISCOUNTS}/{created['id']}", json={"stackable": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["stackable"] is True
    assert "discount.updated" in await events()


async def test_editing_a_discount_to_an_inconsistent_amount_is_refused(client):
    await as_admin(client)
    created = await make(client)  # percentage

    # amount_cents on a discount whose kind is still "percentage" violates the DB's own
    # either/or CHECK — proves the database is the backstop, not merely the API layer.
    resp = await client.patch(f"{DISCOUNTS}/{created['id']}", json={"amount_cents": 500})
    assert resp.status_code == 422, resp.text


# --- eligibility -----------------------------------------------------------------------------


async def test_replacing_the_eligible_item_set(client):
    await as_admin(client)
    created = await client.post(DISCOUNTS, json=percentage(eligibility_scope="selected"))
    created = created.json()

    service_id = "11111111-1111-1111-1111-111111111111"
    resp = await client.put(
        f"{DISCOUNTS}/{created['id']}/eligibility",
        json={"items": [{"item_type": "service", "item_id": service_id}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["eligible_items"] == [{"item_type": "service", "item_id": service_id}]
    assert "discount.eligibility_replaced" in await events()

    # Replacing again with an empty set clears it — a whole-set save, never additive.
    resp = await client.put(f"{DISCOUNTS}/{created['id']}/eligibility", json={"items": []})
    assert resp.json()["eligible_items"] == []


# --- enabling, and disabling ------------------------------------------------------------------


async def test_disabling_and_reenabling_a_discount(client):
    await as_admin(client)
    created = await make(client)

    resp = await client.post(f"{DISCOUNTS}/{created['id']}/disable", json={})
    assert resp.json()["enabled"] is False

    resp = await client.post(f"{DISCOUNTS}/{created['id']}/enable", json={})
    assert resp.json()["enabled"] is True

    assert "discount.disabled" in await events()
    assert "discount.enabled" in await events()


# --- authorization ---------------------------------------------------------------------------


async def test_billing_manage_is_required(client):
    resp = await client.get(DISCOUNTS)
    assert resp.status_code == 401, resp.text  # not signed in at all

    await as_admin(client)
    await client.post("/api/auth/mode", json={"mode": "staff"})
    resp = await client.get(DISCOUNTS)
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "admin_mode_required"

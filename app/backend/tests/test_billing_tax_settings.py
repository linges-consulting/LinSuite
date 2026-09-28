"""S1: Settings → Billing → Tax — the admin CRUD over tax components and their effective-dated
rates (#57).

`billing/tax.py`'s own arithmetic is proven pure at S2 (`tests/test_billing_tax.py`); this
file only proves the HTTP surface around `billing/models.py`: creation with a first rate,
uniqueness on `code`, the effective-dating on `POST .../rates` (closing the previous open
rate, refusing a rate that would not actually come after it), the `billing.manage` capability
gate, and `applicable_to_business` reading the business's own `province`.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import text

from core.db import get_purge_engine, session_scope

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"

SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": EMAIL,
    "admin_password": PASSWORD,
}

TAX_COMPONENTS = "/api/admin/billing/tax-components"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async with get_purge_engine().begin() as purge:
        await purge.execute(text("DELETE FROM audit_events"))
    async with session_scope() as db:
        for table in (
            "tax_component_rates",
            "tax_components",
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


# --- helpers --------------------------------------------------------------------------------


async def as_admin(client, email=EMAIL, password=PASSWORD):
    login = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    resp = await client.post("/api/auth/mode", json={"mode": "admin", "password": password})
    assert resp.status_code == 200, resp.text


async def set_province(client, province: str) -> None:
    resp = await client.put(
        "/api/admin/business", json={"name": "Cedar Lane Clinic", "province": province}
    )
    assert resp.status_code == 200, resp.text


def draft(**overrides) -> dict:
    body = {
        "code": "gst",
        "name": "GST",
        "province": None,
        "rate_bp": 500,
        "effective_from": date(2024, 1, 1).isoformat(),
    }
    body.update(overrides)
    return body


async def create(client, **overrides):
    return await client.post(TAX_COMPONENTS, json=draft(**overrides))


async def make(client, **overrides) -> dict:
    resp = await create(client, **overrides)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def make_role(client, name: str, capabilities: list[str]) -> dict:
    resp = await client.post("/api/admin/roles", json={"name": name, "capabilities": capabilities})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def events() -> list[str]:
    async with session_scope() as db:
        rows = await db.execute(text("SELECT event_type FROM audit_events ORDER BY occurred_at"))
        return [row[0] for row in rows]


# --- creating -------------------------------------------------------------------------------


async def test_creating_a_federal_component_with_its_first_rate(client):
    await as_admin(client)

    created = await make(client, code="gst", name="GST", province=None, rate_bp=500)

    assert created["code"] == "GST"  # normalised upper
    assert created["province"] is None
    assert created["active"] is True
    assert len(created["rates"]) == 1
    assert created["rates"][0]["rate_bp"] == 500
    assert created["rates"][0]["effective_to"] is None
    assert created["current_rate_bp"] == 500
    assert "billing.tax_component_created" in await events()


async def test_code_is_unique_case_insensitively(client):
    await as_admin(client)
    await make(client, code="gst")

    dupe = await create(client, code="GST")

    assert dupe.status_code == 409, dupe.text


async def test_unknown_province_is_refused(client):
    await as_admin(client)

    resp = await create(client, code="pst", province="ZZ")

    assert resp.status_code == 422, resp.text


async def test_rate_above_one_hundred_percent_is_refused(client):
    await as_admin(client)

    resp = await create(client, code="pst", rate_bp=10_001)

    assert resp.status_code == 422, resp.text


async def test_a_future_dated_rate_has_no_current_rate_yet(client):
    await as_admin(client)
    future = (date.today() + timedelta(days=30)).isoformat()

    created = await make(client, code="gst", effective_from=future)

    assert created["current_rate_bp"] is None


# --- jurisdiction -----------------------------------------------------------------------


async def test_a_federal_component_is_always_applicable(client):
    await as_admin(client)
    await set_province(client, "ON")

    created = await make(client, code="gst", province=None)

    assert created["applicable_to_business"] is True


async def test_a_provincial_component_is_applicable_only_to_its_own_province(client):
    await as_admin(client)
    await set_province(client, "BC")

    bc_pst = await make(client, code="pst", province="BC")
    ab_component = await make(client, code="ab_lct", province="AB")

    assert bc_pst["applicable_to_business"] is True
    assert ab_component["applicable_to_business"] is False


# --- updating ---------------------------------------------------------------------------


async def test_renaming_and_clearing_province(client):
    await as_admin(client)
    component = await make(client, code="pst", name="PST", province="BC")

    resp = await client.patch(
        f"{TAX_COMPONENTS}/{component['id']}",
        json={"name": "Provincial Sales Tax", "province": None},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Provincial Sales Tax"
    assert body["province"] is None
    assert "billing.tax_component_updated" in await events()


async def test_deactivating_a_component(client):
    await as_admin(client)
    component = await make(client, code="pst")

    resp = await client.patch(f"{TAX_COMPONENTS}/{component['id']}", json={"active": False})

    assert resp.status_code == 200, resp.text
    assert resp.json()["active"] is False


async def test_updating_an_unknown_component_is_a_404(client):
    await as_admin(client)

    resp = await client.patch(
        f"{TAX_COMPONENTS}/00000000-0000-0000-0000-000000000000", json={"name": "x"}
    )

    assert resp.status_code == 404, resp.text


# --- rates: effective-dating --------------------------------------------------------------


async def test_adding_a_rate_closes_the_previously_open_one(client):
    await as_admin(client)
    component = await make(
        client, code="gst", rate_bp=500, effective_from=date(2024, 1, 1).isoformat()
    )

    resp = await client.post(
        f"{TAX_COMPONENTS}/{component['id']}/rates",
        json={"rate_bp": 600, "effective_from": date(2025, 1, 1).isoformat()},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["rates"]) == 2
    old, new = body["rates"]
    assert old["rate_bp"] == 500
    assert old["effective_to"] == "2025-01-01"
    assert new["rate_bp"] == 600
    assert new["effective_to"] is None
    assert "billing.tax_component_rate_added" in await events()


async def test_a_rate_that_does_not_start_after_the_open_one_is_refused(client):
    await as_admin(client)
    component = await make(
        client, code="gst", rate_bp=500, effective_from=date(2024, 6, 1).isoformat()
    )

    resp = await client.post(
        f"{TAX_COMPONENTS}/{component['id']}/rates",
        json={"rate_bp": 600, "effective_from": date(2024, 1, 1).isoformat()},
    )

    assert resp.status_code == 422, resp.text
    # Left untouched: still one open rate at the original value.
    listed = await client.get(TAX_COMPONENTS)
    [only] = [c for c in listed.json()["tax_components"] if c["id"] == component["id"]]
    assert len(only["rates"]) == 1
    assert only["rates"][0]["rate_bp"] == 500


# --- capability gate ----------------------------------------------------------------------


async def test_capability_required_refuses_a_role_without_billing_manage(client):
    """`admin` alone (Admin Mode, but no `billing.manage`) is not enough — the registry's own
    rule that a route asks for its own capability, never inferred from being an administrator
    at all (`auth/capabilities.py`)."""
    await as_admin(client)
    role = await make_role(client, "Front desk", ["admin"])
    async with session_scope() as db:
        await db.execute(
            text("UPDATE users SET role_id = :role_id WHERE email = :email"),
            {"role_id": role["id"], "email": EMAIL},
        )
        await db.commit()
    client.cookies.clear()
    await as_admin(client)

    resp = await client.get(TAX_COMPONENTS)

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "capability_required"

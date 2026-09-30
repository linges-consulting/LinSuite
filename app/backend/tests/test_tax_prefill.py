"""S1: tax pre-fill (#118, spec #113 "Tax pre-fill") — saving the business address's province
creates that province's built-in components exactly once, and the confirmation timestamp that
flips the onboarding checklist's tax step.

Same fixture shape as `test_billing_tax_settings.py` (a freshly claimed instance, admin
already signed in and switched to Admin Mode); `billing/tax_table.py`'s own values are proven
at S2 in `test_billing_tax_table.py`, so this file only proves the HTTP-and-database behaviour
around them.
"""

from datetime import date

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

TAX_COMPONENTS = "/api/admin/billing/tax-components"
TAX_STATUS = "/api/admin/billing/tax-status"
TAX_CONFIRMATION = "/api/admin/billing/tax-confirmation"
ONBOARDING = "/api/admin/onboarding"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async with get_owner_engine().begin() as owner:
        await owner.execute(text("DELETE FROM audit_events"))
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


async def as_admin(client, email=EMAIL, password=PASSWORD):
    login = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    resp = await client.post("/api/auth/mode", json={"mode": "admin", "password": password})
    assert resp.status_code == 200, resp.text


async def set_province(client, province: str):
    resp = await client.put(
        "/api/admin/business", json={"name": "Cedar Lane Clinic", "province": province}
    )
    assert resp.status_code == 200, resp.text
    return resp


async def components(client) -> list[dict]:
    resp = await client.get(TAX_COMPONENTS)
    assert resp.status_code == 200, resp.text
    return resp.json()["tax_components"]


async def create_component(client, **overrides) -> dict:
    body = {
        "code": "custom",
        "name": "My Own Tax",
        "province": None,
        "rate_ppm": 10_000,
        "effective_from": date(2020, 1, 1).isoformat(),
    }
    body.update(overrides)
    resp = await client.post(TAX_COMPONENTS, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def events() -> list[str]:
    async with session_scope() as db:
        rows = await db.execute(text("SELECT event_type FROM audit_events ORDER BY occurred_at"))
        return [row[0] for row in rows]


# --- creating on first save -----------------------------------------------------------------


async def test_saving_a_province_creates_exactly_its_components(client):
    await as_admin(client)

    await set_province(client, "BC")

    by_code = {c["code"]: c for c in await components(client)}
    assert set(by_code) == {"GST", "PST"}
    assert by_code["GST"]["rates"][0]["rate_ppm"] == 50_000
    assert by_code["GST"]["rates"][0]["effective_from"] == "2008-01-01"
    assert by_code["PST"]["rates"][0]["rate_ppm"] == 70_000
    assert by_code["PST"]["rates"][0]["effective_from"] == "2013-04-01"
    assert by_code["GST"]["province"] == "BC"
    assert by_code["PST"]["province"] == "BC"
    assert by_code["GST"]["origin"] == "prefill"
    assert by_code["PST"]["origin"] == "prefill"
    assert "billing.tax_prefilled" in await events()


async def test_ontario_gets_only_hst(client):
    await as_admin(client)

    await set_province(client, "ON")

    created = await components(client)
    assert [c["code"] for c in created] == ["HST"]
    assert created[0]["rates"][0]["rate_ppm"] == 130_000


async def test_quebec_gets_gst_and_qst(client):
    await as_admin(client)

    await set_province(client, "QC")

    by_code = {c["code"]: c for c in await components(client)}
    assert set(by_code) == {"GST", "QST"}
    assert by_code["QST"]["rates"][0]["rate_ppm"] == 99_750  # exact, #119


# --- idempotence: a second save, or a later province change, creates nothing ----------------


async def test_a_second_save_of_the_same_province_creates_nothing_more(client):
    await as_admin(client)
    await set_province(client, "BC")
    first = await components(client)

    await set_province(client, "BC")
    second = await components(client)

    assert len(first) == 2
    assert {(c["id"], c["code"]) for c in second} == {(c["id"], c["code"]) for c in first}


async def test_a_later_province_change_creates_nothing(client):
    await as_admin(client)
    await set_province(client, "BC")

    await set_province(client, "ON")
    after = await components(client)

    # Still the two BC components — a province change never creates ON's HST, and never
    # touches the existing rows (spec: "nothing changes automatically").
    assert {c["code"] for c in after} == {"GST", "PST"}
    trail = await events()
    assert trail.count("billing.tax_prefilled") == 1


# --- an existing component suppresses pre-fill entirely -------------------------------------


async def test_an_existing_component_suppresses_prefill_entirely(client):
    await as_admin(client)
    await create_component(client, code="custom")

    await set_province(client, "BC")

    after = await components(client)
    assert [c["code"] for c in after] == ["CUSTOM"]
    assert "billing.tax_prefilled" not in await events()


# --- confirmation -----------------------------------------------------------------------


async def test_confirmation_sets_the_timestamp_and_flips_the_checklist_tax_step(client):
    await as_admin(client)
    await set_province(client, "BC")

    status_before = await client.get(TAX_STATUS)
    assert status_before.status_code == 200, status_before.text
    assert status_before.json()["prefilled"] is True
    assert status_before.json()["confirmed_at"] is None

    onboarding_before = await client.get(ONBOARDING)
    tax_step_before = next(s for s in onboarding_before.json()["steps"] if s["key"] == "tax")
    assert tax_step_before["done"] is False

    confirm = await client.post(TAX_CONFIRMATION, json={})
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["confirmed_at"] is not None
    assert "billing.tax_confirmed" in await events()

    onboarding_after = await client.get(ONBOARDING)
    tax_step_after = next(s for s in onboarding_after.json()["steps"] if s["key"] == "tax")
    assert tax_step_after["done"] is True


async def test_an_owner_configured_component_flips_the_checklist_without_confirming(client):
    """Story 25/onboarding rule: a business with its own (`origin='manual'`) active component
    is done without ever calling the confirmation endpoint."""
    await as_admin(client)
    await create_component(client, code="gst", province=None)

    onboarding = await client.get(ONBOARDING)
    tax_step = next(s for s in onboarding.json()["steps"] if s["key"] == "tax")

    assert tax_step["done"] is True


async def test_unconfirmed_prefill_alone_does_not_flip_the_checklist(client):
    await as_admin(client)
    await set_province(client, "BC")

    onboarding = await client.get(ONBOARDING)
    tax_step = next(s for s in onboarding.json()["steps"] if s["key"] == "tax")

    assert tax_step["done"] is False


async def test_confirmation_requires_billing_manage_capability(client):
    await as_admin(client)
    role_resp = await client.post(
        "/api/admin/roles", json={"name": "Front desk", "capabilities": ["admin"]}
    )
    assert role_resp.status_code == 201, role_resp.text
    async with session_scope() as db:
        await db.execute(
            text("UPDATE users SET role_id = :role_id WHERE email = :email"),
            {"role_id": role_resp.json()["id"], "email": EMAIL},
        )
        await db.commit()
    client.cookies.clear()
    await as_admin(client)

    resp = await client.post(TAX_CONFIRMATION, json={})

    assert resp.status_code == 403, resp.text


# --- province-change / newer-rate prompt -----------------------------------------------------


async def test_province_change_after_prefill_is_flagged_in_status(client):
    await as_admin(client)
    await set_province(client, "BC")

    await set_province(client, "ON")

    status = await client.get(TAX_STATUS)
    assert status.json()["province_changed"] is True
    assert status.json()["newer_rate_available"] is False


async def test_no_prompt_when_province_and_rates_are_unchanged(client):
    await as_admin(client)
    await set_province(client, "BC")

    status = await client.get(TAX_STATUS)

    assert status.json()["province_changed"] is False
    assert status.json()["newer_rate_available"] is False


async def test_manual_components_never_trigger_the_prompt(client):
    """A deliberate manual component in another province (`test_billing_tax_settings.py`'s
    `ab_lct` pattern) is not pre-fill going stale."""
    await as_admin(client)
    await set_province(client, "BC")
    await create_component(client, code="ab_lct", province="AB")

    status = await client.get(TAX_STATUS)

    assert status.json()["province_changed"] is False
    assert status.json()["newer_rate_available"] is False

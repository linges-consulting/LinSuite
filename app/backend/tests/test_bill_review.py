"""S1: the staff-facing draft-bill review screen (#63) — #59's draft bill, #58's discounts and
#57's tax coming together over real appointments, a real discount and a real tax component.

The resolver math itself is already S2-proven (`test_discount_resolver.py`, `test_billing_
tax.py`); this file proves the HTTP surface: what a completed appointment's draft bill looks
like with nothing applied, that an eligible stackable combination recomputes the total via
`resolve_stacked_discounts`, that tax honours the business's own province and today's rate,
that a rejected combination surfaces its specific reason rather than a silent no-op, and that
an applied selection survives a fresh `GET` (persisted, not merely echoed back).
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import text

from core.db import get_purge_engine, session_scope
from tests.conftest import wipe_document_keys

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"
STAFF_EMAIL = "desk@cedar.example"
STAFF_PASSWORD = "correct horse battery 2"

SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": EMAIL,
    "admin_password": PASSWORD,
}

STAFF = "/api/admin/staff"
SERVICES = "/api/admin/services"
APPOINTMENTS = "/api/appointments"
CUSTOMERS = "/api/customers"
DISCOUNTS = "/api/admin/discounts"
TAX_COMPONENTS = "/api/admin/billing/tax-components"
BILLS = "/api/bills"

CUSTOMER = {"first_name": "Priya", "last_name": "Nair", "phone": "416-555-0199"}


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
                "service_bill_discounts",
                "retail_sale_discounts",  # review T1, 0065
                "discount_eligible_items",
                "discounts",
                "tax_component_rates",
                "tax_components",
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

    await get_redis().delete(*(throttle.delay_key(e) for e in (EMAIL, STAFF_EMAIL)))

    async with session_scope() as db:
        token = await setup.bootstrap_setup_token(db)
    resp = await client.post("/api/setup", json={**SETUP, "token": token})
    assert resp.status_code == 201, resp.text
    async with session_scope() as db:
        await db.execute(text("UPDATE businesses SET mfa_required_for_admin = false"))
        await db.commit()
    client.cookies.clear()
    yield
    await wipe()


# --- helpers --------------------------------------------------------------------------------


async def as_admin(client, email=EMAIL, password=PASSWORD):
    login = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    resp = await client.post("/api/auth/mode", json={"mode": "admin", "password": password})
    assert resp.status_code == 200, resp.text


async def as_staff(client, email=STAFF_EMAIL, password=STAFF_PASSWORD):
    login = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text


async def add_front_desk_account() -> str:
    """A plain Staff-role account — `billing.view` is seeded to it by migration 0051, no
    escalation needed, the same call `queue.manage` already makes for front-desk work."""
    from core.security import hash_password
    from tests.conftest import add_account

    async with session_scope() as db:
        role_id = str(await db.scalar(text("SELECT id FROM roles WHERE name = 'Staff'")))
    return await add_account(STAFF_EMAIL, await hash_password(STAFF_PASSWORD), role=role_id)


async def me_staff_id(client) -> str:
    roster = await client.get(STAFF)
    assert roster.status_code == 200, roster.text
    return next(row["id"] for row in roster.json()["staff"] if row["email"] == EMAIL)


async def make_service(client, staff_ids: list[str], **overrides) -> str:
    body = {"name": "Swedish Massage", "duration_minutes": 60, "price_cents": 12000}
    body.update(overrides)
    created = await client.post(SERVICES, json=body)
    assert created.status_code == 201, created.text
    service_id = created.json()["id"]
    linked = await client.put(f"{SERVICES}/{service_id}/staff", json={"staff_ids": staff_ids})
    assert linked.status_code == 200, linked.text
    return service_id


async def make_customer(client) -> str:
    resp = await client.post(CUSTOMERS, json=CUSTOMER)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def complete_a_visit(
    client, *, price_cents: int = 12000, service_name: str = "Swedish Massage", **service
) -> tuple[str, str]:
    """Books, confirms and completes one appointment end to end — the only way #59's hook
    ever creates a draft bill — and returns `(bill_id, service_id)`. `service_name` only
    matters when calling this more than once in the same test (#65's concurrency test needs
    two distinct bills): `ux_services_name` is unique, so a second default-named call would
    otherwise collide."""
    me = await me_staff_id(client)
    resp = await client.put(
        f"{STAFF}/{me}/hours",
        json={"blocks": [{"weekday": 0, "start_minute": 540, "end_minute": 1020}]},
    )
    assert resp.status_code == 200, resp.text
    service_id = await make_service(
        client, [me], price_cents=price_cents, name=service_name, **service
    )
    customer_id = await make_customer(client)

    monday = date.today() + timedelta(days=7 + (7 - date.today().weekday()) % 7)
    slots = await client.get(
        "/api/availability",
        params={"service_id": service_id, "from": monday.isoformat(), "to": monday.isoformat()},
    )
    assert slots.status_code == 200, slots.text
    starts_at = slots.json()["days"][0]["slots"][0]["starts_at"]

    booked = await client.post(
        APPOINTMENTS,
        json={
            "service_id": service_id,
            "staff_id": me,
            "starts_at": starts_at,
            "customer_id": customer_id,
        },
    )
    assert booked.status_code == 201, booked.text
    appointment_id = booked.json()["id"]

    completed = await client.post(f"{APPOINTMENTS}/{appointment_id}/complete", json={})
    assert completed.status_code == 200, completed.text

    async with session_scope() as db:
        bill_id = await db.scalar(
            text("SELECT bill_id FROM service_bill_lines WHERE appointment_id = :a"),
            {"a": appointment_id},
        )
    return str(bill_id), service_id


async def make_discount(client, **overrides) -> dict:
    body = {
        "name": "Autumn 10%",
        "kind": "percentage",
        "percentage_bp": 1000,
        "commission_basis": "reduces",
    }
    body.update(overrides)
    resp = await client.post(DISCOUNTS, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def make_tax_component(client, **overrides) -> dict:
    body = {
        "code": "gst",
        "name": "GST",
        "province": None,
        "rate_bp": 500,
        "effective_from": date(2024, 1, 1).isoformat(),
    }
    body.update(overrides)
    resp = await client.post(TAX_COMPONENTS, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- viewing --------------------------------------------------------------------------------


async def test_staff_sees_the_draft_bill_with_no_discounts_or_tax(client):
    await as_admin(client)
    bill_id, service_id = await complete_a_visit(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)

    resp = await client.get(f"{BILLS}/{bill_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "draft"
    assert len(body["lines"]) == 1
    line = body["lines"][0]
    assert line["service"]["id"] == service_id
    assert line["price_cents"] == 12000
    assert line["discounted_cents"] == 12000
    assert line["applied_discount_ids"] == []
    assert line["tax"]["tax_cents"] == 0
    assert line["line_total_cents"] == 12000
    assert body["subtotal_cents"] == 12000
    assert body["grand_total_cents"] == 12000
    assert body["eligible_discounts"] == []


async def test_draft_bills_are_listed(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)

    resp = await client.get(BILLS)

    assert resp.status_code == 200, resp.text
    ids = [b["id"] for b in resp.json()["bills"]]
    assert bill_id in ids


async def test_billing_view_is_reachable_in_staff_mode_with_no_admin_window(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)  # no /api/auth/mode call — Staff Mode only

    resp = await client.get(f"{BILLS}/{bill_id}")

    assert resp.status_code == 200, resp.text


async def test_a_role_without_billing_view_is_refused(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    role = await client.post("/api/admin/roles", json={"name": "Bare", "capabilities": []})
    assert role.status_code == 201, role.text
    from core.security import hash_password
    from tests.conftest import add_account

    await add_account(STAFF_EMAIL, await hash_password(STAFF_PASSWORD), role=role.json()["id"])
    client.cookies.clear()
    await as_staff(client)

    resp = await client.get(f"{BILLS}/{bill_id}")

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "capability_required"


# --- discounts --------------------------------------------------------------------------------


async def test_applying_an_eligible_percentage_discount_recomputes_the_total(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    discount = await make_discount(client, percentage_bp=1000)  # 10%, eligibility_scope "all"

    resp = await client.put(f"{BILLS}/{bill_id}/discounts", json={"discount_ids": [discount["id"]]})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    line = body["lines"][0]
    # 12000 - 10% = 10800, half-up — matches `resolve_stacked_discounts`'s own math, not a
    # second computation of it.
    assert line["discounted_cents"] == 10800
    assert line["applied_discount_ids"] == [discount["id"]]
    assert body["discount_total_cents"] == 1200
    assert body["grand_total_cents"] == 10800
    assert (
        next(d for d in body["eligible_discounts"] if d["id"] == discount["id"])["applied"] is True
    )


async def test_the_applied_selection_survives_a_fresh_read(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    discount = await make_discount(client, percentage_bp=1000)
    applied = await client.put(
        f"{BILLS}/{bill_id}/discounts", json={"discount_ids": [discount["id"]]}
    )
    assert applied.status_code == 200, applied.text

    resp = await client.get(f"{BILLS}/{bill_id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["lines"][0]["discounted_cents"] == 10800


async def test_removing_a_discount_restores_the_full_price(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    discount = await make_discount(client, percentage_bp=1000)
    applied = await client.put(
        f"{BILLS}/{bill_id}/discounts", json={"discount_ids": [discount["id"]]}
    )
    assert applied.status_code == 200, applied.text

    resp = await client.put(f"{BILLS}/{bill_id}/discounts", json={"discount_ids": []})

    assert resp.status_code == 200, resp.text
    assert resp.json()["lines"][0]["discounted_cents"] == 12000


async def test_a_discount_ineligible_for_the_line_is_not_offered(client):
    await as_admin(client)
    bill_id, other_service_id = await complete_a_visit(client)
    discount = await make_discount(client, eligibility_scope="selected")
    replaced = await client.put(
        f"{DISCOUNTS}/{discount['id']}/eligibility",
        json={
            "items": [{"item_type": "service", "item_id": "00000000-0000-0000-0000-000000000000"}]
        },
    )
    assert replaced.status_code == 200, replaced.text

    resp = await client.get(f"{BILLS}/{bill_id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["eligible_discounts"] == []


async def test_two_non_stackable_discounts_together_are_rejected_with_the_specific_reason(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    first = await make_discount(client, name="A", percentage_bp=1000, stackable=False)
    second = await make_discount(client, name="B", percentage_bp=500, stackable=False)

    resp = await client.put(
        f"{BILLS}/{bill_id}/discounts", json={"discount_ids": [first["id"], second["id"]]}
    )

    assert resp.status_code == 422, resp.text
    assert "not stackable" in resp.json()["detail"]


async def test_an_excessive_fixed_discount_is_rejected_not_clamped(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client, price_cents=1000)
    too_much = await make_discount(client, kind="fixed", amount_cents=5000, percentage_bp=None)

    resp = await client.put(f"{BILLS}/{bill_id}/discounts", json={"discount_ids": [too_much["id"]]})

    assert resp.status_code == 422, resp.text
    assert "exceed" in resp.json()["detail"]

    # Rejected — not silently applied, not silently left as it was and hidden.
    unchanged = await client.get(f"{BILLS}/{bill_id}")
    assert unchanged.json()["lines"][0]["discounted_cents"] == 1000


async def test_an_unknown_discount_id_is_refused(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)

    resp = await client.put(
        f"{BILLS}/{bill_id}/discounts",
        json={"discount_ids": ["00000000-0000-0000-0000-000000000000"]},
    )

    assert resp.status_code == 422, resp.text


async def test_a_disabled_discount_cannot_be_applied(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    discount = await make_discount(client)
    disabled = await client.post(f"{DISCOUNTS}/{discount['id']}/disable", json={})
    assert disabled.status_code == 200, disabled.text

    resp = await client.put(f"{BILLS}/{bill_id}/discounts", json={"discount_ids": [discount["id"]]})

    assert resp.status_code == 422, resp.text


# --- tax ----------------------------------------------------------------------------------


async def test_tax_is_included_using_the_business_own_province_and_todays_rate(client):
    await as_admin(client)
    business = await client.put(
        "/api/admin/business", json={"name": "Cedar Lane Clinic", "province": "ON"}
    )
    assert business.status_code == 200, business.text
    await make_tax_component(client, code="gst", province=None, rate_bp=500)  # federal, 5%
    await make_tax_component(client, code="qst", province="QC", rate_bp=975)  # a different province
    bill_id, _ = await complete_a_visit(
        client, price_cents=10000, tax_component_keys=["gst", "qst"]
    )

    resp = await client.get(f"{BILLS}/{bill_id}")

    assert resp.status_code == 200, resp.text
    line = resp.json()["lines"][0]
    # Federal GST applies regardless of province; QC's QST does not, since the business is ON.
    assert line["tax"]["component_cents"] == {"GST": 500}
    assert line["tax"]["tax_cents"] == 500
    assert line["tax"]["total_cents"] == 10500
    assert resp.json()["tax_totals_by_component"] == {"GST": 500}
    assert resp.json()["grand_total_cents"] == 10500


async def test_tax_is_computed_on_the_discounted_amount(client):
    await as_admin(client)
    await make_tax_component(client, code="gst", province=None, rate_bp=500)
    bill_id, _ = await complete_a_visit(client, price_cents=10000, tax_component_keys=["GST"])
    discount = await make_discount(client, percentage_bp=1000)  # 10% off -> 9000

    resp = await client.put(f"{BILLS}/{bill_id}/discounts", json={"discount_ids": [discount["id"]]})

    assert resp.status_code == 200, resp.text
    line = resp.json()["lines"][0]
    assert line["discounted_cents"] == 9000
    assert line["tax"]["tax_cents"] == 450  # 5% of 9000
    assert line["line_total_cents"] == 9450


async def test_a_tax_component_with_no_rate_covering_today_is_skipped(client):
    await as_admin(client)
    await make_tax_component(
        client,
        code="future",
        province=None,
        rate_bp=500,
        effective_from=(date.today() + timedelta(days=30)).isoformat(),
    )
    bill_id, _ = await complete_a_visit(client, price_cents=10000)

    resp = await client.get(f"{BILLS}/{bill_id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["lines"][0]["tax"]["tax_cents"] == 0

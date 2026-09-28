"""S1(+S2): package/bundle purchase (#71) — buying a `PackageDefinition` (#60) issues an
`Invoice` through the exact same machinery #65 built, over a real PostgreSQL.

Covers: a single-service package vs. a multi-service bundle (allocation math), the frozen-
snapshot-never-recomputed guarantee (`test_bill_review.py`/`test_invoice_issue.py`'s own
"mutate the live row afterward, assert nothing changes" style), the shared invoice-numbering
counter (no second counter forked for packages), and the ticket's central rule — credits never
activate on purchase, "a partial payment activates nothing" — proven both as "purchase alone
never activates" and as a direct unit test of `activate_credits` itself (the one hook point
#66 will eventually call).
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from billing.allocation import allocate_bundle_price
from billing.models import PackagePurchase
from billing.package_purchase import activate_credits
from core.db import get_purge_engine, session_scope
from tests.conftest import add_account, wipe_document_keys

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

SERVICES = "/api/admin/services"
CUSTOMERS = "/api/customers"
PACKAGES = "/api/admin/packages"
TAX_COMPONENTS = "/api/admin/billing/tax-components"

CUSTOMER = {"first_name": "Priya", "last_name": "Nair", "phone": "416-555-0199"}


def purchase_url(definition_id: str) -> str:
    return f"{PACKAGES.replace('/admin', '')}/{definition_id}/purchase"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async def wipe():
        async with get_purge_engine().begin() as purge:
            await purge.execute(text("DELETE FROM audit_events"))
            await purge.execute(text("DELETE FROM erasure_requests"))
            await purge.execute(text("DELETE FROM form_links"))
            # #71's own guarded tables, purge-role-bypassed (migration 0055) — must go before
            # `invoices`/`package_definitions`/`customers`/`services` below, all of which they
            # reference with `ON DELETE RESTRICT`.
            await purge.execute(text("DELETE FROM package_credit_redemptions"))  # #72
            await purge.execute(text("DELETE FROM package_credit_voids"))  # #73
            await purge.execute(text("DELETE FROM package_purchase_credits"))
            await purge.execute(text("DELETE FROM invoice_payment_transfers"))  # #68
            await purge.execute(text("DELETE FROM commission_postings"))  # #69
            await purge.execute(text("DELETE FROM invoice_line_taxes"))
            await purge.execute(text("DELETE FROM invoice_line_discounts"))
            await purge.execute(text("DELETE FROM invoice_lines"))
            await purge.execute(text("DELETE FROM retail_return_lines"))  # #76
            await purge.execute(text("DELETE FROM retail_returns"))  # #76
            await purge.execute(text("DELETE FROM invoice_refunds"))  # #67
            await purge.execute(text("DELETE FROM invoices"))
            await purge.execute(text("DELETE FROM package_purchases"))
            await purge.execute(text("DELETE FROM business_invoice_counters"))
        await wipe_document_keys()
        async with session_scope() as db:
            for table in (
                "bill_override_requests",
                "service_bill_discounts",
                "discount_eligible_items",
                "discounts",
                "tax_component_rates",
                "tax_components",
                "service_bill_lines",
                "service_bills",
                "package_definition_services",
                "package_definitions",
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
    """A plain Staff-role account — `billing.view` is seeded to it (migration 0051), no
    escalation needed, the same call #65's own tests already make."""
    from core.security import hash_password

    async with session_scope() as db:
        role_id = str(await db.scalar(text("SELECT id FROM roles WHERE name = 'Staff'")))
    return await add_account(STAFF_EMAIL, await hash_password(STAFF_PASSWORD), role=role_id)


async def make_service(client, **overrides) -> dict:
    body = {"name": "Massage", "duration_minutes": 60, "price_cents": 12000}
    body.update(overrides)
    resp = await client.post(SERVICES, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def make_customer(client) -> str:
    resp = await client.post(CUSTOMERS, json=CUSTOMER)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def make_package(client, services: list[dict], **overrides) -> dict:
    body = {
        "name": "10-Session Massage Pack",
        "description": "Ten sessions.",
        "price_cents": 96000,
        "services": services,
    }
    body.update(overrides)
    resp = await client.post(PACKAGES, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def make_tax_component(client, **overrides) -> dict:
    body = {
        "code": "gst",
        "name": "GST",
        "province": None,
        "rate_bp": 500,
        "effective_from": "2024-01-01",
    }
    body.update(overrides)
    resp = await client.post(TAX_COMPONENTS, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def purchase(client, definition_id: str, customer_id: str) -> dict:
    resp = await client.post(purchase_url(definition_id), json={"customer_id": customer_id})
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- purchasing: a single-service package ------------------------------------------------


async def test_purchasing_a_single_service_package_issues_an_invoice(client):
    await as_admin(client)
    massage = await make_service(client, price_cents=12000)
    package = await make_package(
        client, [{"service_id": massage["id"], "credits": 10}], price_cents=96000
    )
    customer_id = await make_customer(client)

    body = await purchase(client, package["id"], customer_id)

    assert body["invoice_number"] == 1
    assert body["name"] == "10-Session Massage Pack"
    assert body["price_cents"] == 96000
    assert body["grand_total_cents"] == 96000  # no tax component defined
    assert body["credits_activated"] is False
    assert body["activated_at"] is None
    assert body["expires_after_days"] is None
    assert body["expires_at"] is None
    assert body["credits"] == [
        {
            "service_id": massage["id"],
            "credits_total": 10,
            "allocated_price_cents": 96000,
        }
    ]

    async with session_scope() as db:
        purchase_count = await db.scalar(text("SELECT count(*) FROM package_purchases"))
        invoice_count = await db.scalar(text("SELECT count(*) FROM invoices"))
    assert purchase_count == 1
    assert invoice_count == 1


async def test_purchase_needs_billing_view(client):
    await as_admin(client)
    massage = await make_service(client)
    package = await make_package(client, [{"service_id": massage["id"], "credits": 5}])
    customer_id = await make_customer(client)
    role = await client.post("/api/admin/roles", json={"name": "No Billing", "capabilities": []})
    assert role.status_code == 201, role.text
    from core.security import hash_password

    await add_account(
        "nocap@cedar.example", await hash_password(STAFF_PASSWORD), role=role.json()["id"]
    )
    client.cookies.clear()
    await as_staff(client, "nocap@cedar.example", STAFF_PASSWORD)

    resp = await client.post(purchase_url(package["id"]), json={"customer_id": customer_id})

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "capability_required"


async def test_front_desk_staff_can_purchase_a_package(client):
    """`billing.view` alone, Staff Mode, no Admin escalation — checkout, same as #65's own
    invoice issue."""
    await as_admin(client)
    massage = await make_service(client)
    package = await make_package(client, [{"service_id": massage["id"], "credits": 5}])
    customer_id = await make_customer(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)

    resp = await client.post(purchase_url(package["id"]), json={"customer_id": customer_id})

    assert resp.status_code == 201, resp.text


async def test_purchasing_an_inactive_package_is_refused(client):
    await as_admin(client)
    massage = await make_service(client)
    package = await make_package(client, [{"service_id": massage["id"], "credits": 5}])
    customer_id = await make_customer(client)
    deactivated = await client.post(f"{PACKAGES}/{package['id']}/deactivate", json={})
    assert deactivated.status_code == 200, deactivated.text

    resp = await client.post(purchase_url(package["id"]), json={"customer_id": customer_id})

    assert resp.status_code == 422, resp.text


async def test_purchasing_for_a_nonexistent_customer_is_refused(client):
    await as_admin(client)
    massage = await make_service(client)
    package = await make_package(client, [{"service_id": massage["id"], "credits": 5}])

    resp = await client.post(
        purchase_url(package["id"]),
        json={"customer_id": "00000000-0000-0000-0000-000000000000"},
    )

    assert resp.status_code == 404, resp.text


async def test_purchasing_a_nonexistent_package_is_refused(client):
    await as_admin(client)
    customer_id = await make_customer(client)

    resp = await client.post(
        purchase_url("00000000-0000-0000-0000-000000000000"),
        json={"customer_id": customer_id},
    )

    assert resp.status_code == 404, resp.text


# --- purchasing: a multi-service bundle (allocation math) ------------------------------------


async def test_purchasing_a_bundle_allocates_price_across_services(client):
    """The worked example from `billing/allocation.py`'s own docstring (#60's acceptance
    criteria): regular prices 120/60/60, purchase price 200 -> 100/50/50."""
    await as_admin(client)
    a = await make_service(client, name="Service A", price_cents=12000)
    b = await make_service(client, name="Service B", price_cents=6000)
    c = await make_service(client, name="Service C", price_cents=6000)
    package = await make_package(
        client,
        [
            {"service_id": a["id"], "credits": 1},
            {"service_id": b["id"], "credits": 2},
            {"service_id": c["id"], "credits": 3},
        ],
        name="Mixed Bundle",
        price_cents=20000,
    )
    customer_id = await make_customer(client)

    body = await purchase(client, package["id"], customer_id)

    expected = allocate_bundle_price([12000, 6000, 6000], 20000)
    assert expected == [10000, 5000, 5000]
    by_service = {row["service_id"]: row for row in body["credits"]}
    assert by_service[a["id"]]["allocated_price_cents"] == expected[0]
    assert by_service[a["id"]]["credits_total"] == 1
    assert by_service[b["id"]]["allocated_price_cents"] == expected[1]
    assert by_service[b["id"]]["credits_total"] == 2
    assert by_service[c["id"]]["allocated_price_cents"] == expected[2]
    assert by_service[c["id"]]["credits_total"] == 3
    # The allocated shares always sum exactly to the price paid — the one property #60's own
    # module docstring guarantees.
    assert sum(row["allocated_price_cents"] for row in body["credits"]) == body["price_cents"]


# --- expiry, frozen ------------------------------------------------------------------------


async def test_purchase_freezes_the_computed_expiry_date(client):
    await as_admin(client)
    massage = await make_service(client)
    package = await make_package(
        client, [{"service_id": massage["id"], "credits": 5}], expires_after_days=30
    )
    customer_id = await make_customer(client)

    body = await purchase(client, package["id"], customer_id)

    assert body["expires_after_days"] == 30
    assert body["expires_at"] is not None


# --- tax ------------------------------------------------------------------------------------


async def test_purchase_is_taxed_the_same_way_a_service_line_is(client):
    await as_admin(client)
    massage = await make_service(client)
    package = await make_package(
        client, [{"service_id": massage["id"], "credits": 5}], price_cents=10000
    )
    customer_id = await make_customer(client)
    await make_tax_component(client, rate_bp=500)

    body = await purchase(client, package["id"], customer_id)

    assert body["computed_subtotal_cents"] == 10000
    assert body["computed_tax_total_cents"] == 500  # 5% of 10000, exclusive convention
    assert body["grand_total_cents"] == 10500
    assert body["tax_totals_by_component"] == {"GST": 500}


# --- the frozen-snapshot guarantee: never re-joins live definitions afterward ------------------


async def test_a_purchase_never_changes_when_the_live_definition_or_service_changes_afterward(
    client,
):
    await as_admin(client)
    massage = await make_service(client, price_cents=12000)
    package = await make_package(
        client, [{"service_id": massage["id"], "credits": 10}], price_cents=96000
    )
    customer_id = await make_customer(client)

    before = await purchase(client, package["id"], customer_id)

    async with session_scope() as db:
        await db.execute(
            text("UPDATE package_definitions SET price_cents = 1, name = 'Renamed' WHERE id = :id"),
            {"id": package["id"]},
        )
        await db.execute(
            text("UPDATE services SET price_cents = 1 WHERE id = :id"), {"id": massage["id"]}
        )
        await db.commit()

    async with session_scope() as db:
        after = await db.get(PackagePurchase, before["id"])
    assert after.name == "10-Session Massage Pack"
    assert after.price_cents == 96000
    assert after.credits[0].allocated_price_cents == 96000
    assert after.credits[0].credits_total == 10


# --- shared invoice numbering: the same counter, not a second one ----------------------------


async def test_two_purchases_share_the_same_gapless_counter_as_service_invoices(client):
    """The ticket's own explicit words: "the same numbering... reused, not reimplemented for
    packages." Purchases two packages back to back and asserts sequential numbers *and* that
    only one `business_invoice_counters` row exists — there is no second counter to check."""
    await as_admin(client)
    massage = await make_service(client)
    package = await make_package(client, [{"service_id": massage["id"], "credits": 5}])
    customer_id = await make_customer(client)

    first = await purchase(client, package["id"], customer_id)
    second = await purchase(client, package["id"], customer_id)

    assert sorted([first["invoice_number"], second["invoice_number"]]) == [1, 2]
    async with session_scope() as db:
        counter_rows = await db.scalar(text("SELECT count(*) FROM business_invoice_counters"))
        counter_value = await db.scalar(
            text("SELECT next_number FROM business_invoice_counters WHERE business_id = 1")
        )
    assert counter_rows == 1
    assert counter_value == 3


# --- reading ---------------------------------------------------------------------------------


async def test_reading_a_purchase_back(client):
    await as_admin(client)
    massage = await make_service(client)
    package = await make_package(client, [{"service_id": massage["id"], "credits": 5}])
    customer_id = await make_customer(client)
    created = await purchase(client, package["id"], customer_id)

    resp = await client.get(f"/api/packages/purchases/{created['id']}")

    assert resp.status_code == 200, resp.text
    assert resp.json() == created


async def test_the_invoice_reads_back_with_a_null_service_bill_id(client):
    await as_admin(client)
    massage = await make_service(client)
    package = await make_package(client, [{"service_id": massage["id"], "credits": 5}])
    customer_id = await make_customer(client)
    created = await purchase(client, package["id"], customer_id)

    resp = await client.get(f"/api/invoices/{created['invoice_id']}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["service_bill_id"] is None
    assert body["package_purchase_id"] == created["id"]
    assert body["lines"] == []


# --- credit activation: the ticket's central rule ---------------------------------------------


async def test_a_purchase_never_activates_credits_on_its_own(client):
    """No payment ledger exists yet (#66 is a sibling, not-yet-merged ticket) — a purchase
    alone, issued or not, must never itself flip usable credits on."""
    await as_admin(client)
    massage = await make_service(client)
    package = await make_package(client, [{"service_id": massage["id"], "credits": 5}])
    customer_id = await make_customer(client)

    body = await purchase(client, package["id"], customer_id)

    assert body["credits_activated"] is False
    assert body["activated_at"] is None
    async with session_scope() as db:
        activated = await db.scalar(
            text("SELECT credits_activated FROM package_purchases WHERE id = :id"),
            {"id": body["id"]},
        )
    assert activated is False


async def test_activate_credits_flips_the_flag_given_a_fully_paid_input(client):
    """The one hook point #66 will eventually call. Exercised directly here — nothing in this
    ticket's own routes ever calls it (module docstring, `billing/package_purchase.py`)."""
    await as_admin(client)
    massage = await make_service(client)
    package = await make_package(client, [{"service_id": massage["id"], "credits": 5}])
    customer_id = await make_customer(client)
    created = await purchase(client, package["id"], customer_id)

    async with session_scope() as db:
        row = await db.get(PackagePurchase, created["id"])
        await activate_credits(db, row)
        await db.commit()

    async with session_scope() as db:
        after = await db.get(PackagePurchase, created["id"])
    assert after.credits_activated is True
    assert after.activated_at is not None


async def test_activate_credits_is_idempotent(client):
    await as_admin(client)
    massage = await make_service(client)
    package = await make_package(client, [{"service_id": massage["id"], "credits": 5}])
    customer_id = await make_customer(client)
    created = await purchase(client, package["id"], customer_id)

    async with session_scope() as db:
        row = await db.get(PackagePurchase, created["id"])
        await activate_credits(db, row)
        await db.commit()
        first_activated_at = row.activated_at

    async with session_scope() as db:
        row = await db.get(PackagePurchase, created["id"])
        await activate_credits(db, row)
        await db.commit()

    async with session_scope() as db:
        after = await db.get(PackagePurchase, created["id"])
    assert after.activated_at == first_activated_at


# --- immutability: append-only / activation-guarded, by grant and trigger ---------------------


async def test_the_app_role_may_not_delete_a_package_purchase(client):
    await as_admin(client)
    massage = await make_service(client)
    package = await make_package(client, [{"service_id": massage["id"], "credits": 5}])
    customer_id = await make_customer(client)
    created = await purchase(client, package["id"], customer_id)

    async with session_scope() as db:
        with pytest.raises(DBAPIError) as refused:
            await db.execute(
                text("DELETE FROM package_purchases WHERE id = :id"), {"id": created["id"]}
            )
        await db.rollback()
    assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value


async def test_the_app_role_may_not_rewrite_a_package_purchases_frozen_columns(client):
    await as_admin(client)
    massage = await make_service(client)
    package = await make_package(client, [{"service_id": massage["id"], "credits": 5}])
    customer_id = await make_customer(client)
    created = await purchase(client, package["id"], customer_id)

    async with session_scope() as db:
        with pytest.raises(DBAPIError) as refused:
            await db.execute(
                text("UPDATE package_purchases SET price_cents = 1 WHERE id = :id"),
                {"id": created["id"]},
            )
        await db.rollback()
    assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value


async def test_the_app_role_may_perform_the_one_permitted_activation_transition(client):
    await as_admin(client)
    massage = await make_service(client)
    package = await make_package(client, [{"service_id": massage["id"], "credits": 5}])
    customer_id = await make_customer(client)
    created = await purchase(client, package["id"], customer_id)

    async with session_scope() as db:
        await db.execute(
            text(
                "UPDATE package_purchases SET credits_activated = true, activated_at = now() "
                "WHERE id = :id"
            ),
            {"id": created["id"]},
        )
        await db.commit()
        activated = await db.scalar(
            text("SELECT credits_activated FROM package_purchases WHERE id = :id"),
            {"id": created["id"]},
        )
    assert activated is True


async def test_the_app_role_may_neither_update_nor_delete_a_package_purchase_credit(client):
    await as_admin(client)
    massage = await make_service(client)
    package = await make_package(client, [{"service_id": massage["id"], "credits": 5}])
    customer_id = await make_customer(client)
    await purchase(client, package["id"], customer_id)

    async with session_scope() as db:
        for statement in (
            "UPDATE package_purchase_credits SET credits_total = credits_total",
            "DELETE FROM package_purchase_credits",
        ):
            with pytest.raises(DBAPIError) as refused:
                await db.execute(text(statement))
            await db.rollback()
            assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value

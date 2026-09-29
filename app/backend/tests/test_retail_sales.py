"""S1(+S5): retail sale — draft cart -> atomic, stock-deducting issue (#75), over a real
PostgreSQL.

A retail sale is always its own invoice, never combined with a service bill/invoice (CLAUDE.md
"Domain rules"). `record_movement` (#61) is the one writer of `product_variants.
quantity_on_hand`; this file proves that a draft never calls it at all, that issuing calls it
once per line inside the one transaction that also inserts the invoice, and that a whole issue
either lands completely or not at all — the same genuine two-ASGI-client race
`test_stock_movements.py`/`test_invoice_issue.py` already use to prove their own atomic
guarantees, applied here across more than one line.

Reuses `tests/test_inventory.py`'s catalog helpers and `tests/test_bill_review.py`'s
staff/customer helpers — the established cross-file test-import pattern (#61's own ledger
entry).
"""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.db import get_purge_engine, session_scope
from tests.conftest import wipe_document_keys
from tests.test_bill_review import (
    EMAIL,
    SETUP,
    STAFF,
    STAFF_EMAIL,
    add_front_desk_account,
    as_admin,
    as_staff,
    make_customer,
)
from tests.test_inventory import make_product, make_variant

RETAIL_SALES = "/api/retail-sales"
RETAIL_INVOICES = "/api/retail-invoices"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async with get_purge_engine().begin() as purge:
        await purge.execute(text("DELETE FROM audit_events"))
        await purge.execute(text("DELETE FROM erasure_requests"))
        await purge.execute(text("DELETE FROM form_links"))
        # #75's own append-only/voidable tables (purge-role-bypassed, migration 0056), #65's
        # own (0054) — one test here issues a *service* invoice too, to prove the shared
        # numbering series — and #61's own ledger (0050). All must go before the rows they
        # reference below ("children before parents", `test_invoice_issue.py`'s own ordering).
        await purge.execute(text("DELETE FROM retail_return_lines"))  # #76
        await purge.execute(text("DELETE FROM retail_returns"))  # #76
        await purge.execute(text("DELETE FROM invoice_payments"))  # #66/#76
        await purge.execute(text("DELETE FROM invoice_balance_authorizations"))  # R8
        await purge.execute(text("DELETE FROM invoice_payment_transfers"))  # #68, R15
        await purge.execute(text("DELETE FROM retail_invoice_lines"))
        await purge.execute(text("DELETE FROM retail_invoices"))
        await purge.execute(text("DELETE FROM commission_postings"))  # #69
        await purge.execute(text("DELETE FROM invoice_line_taxes"))
        await purge.execute(text("DELETE FROM invoice_line_discounts"))
        await purge.execute(text("DELETE FROM invoice_lines"))
        await purge.execute(text("DELETE FROM invoice_refunds"))  # #67
        await purge.execute(text("DELETE FROM invoices"))
        await purge.execute(text("DELETE FROM business_invoice_counters"))
        await purge.execute(text("DELETE FROM stock_movements"))
    await wipe_document_keys()
    async with session_scope() as db:
        for table in (
            "retail_sale_lines",
            "retail_sales",
            "product_variants",
            "products",
            "bill_override_requests",
            "service_bill_discounts",
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


# --- helpers ------------------------------------------------------------------------------


async def new_variant(client, **overrides) -> dict:
    # A fresh product per call — `ux_products_name` is unique, and several tests need more
    # than one variant in the same test.
    product = await make_product(client, name=f"Product {uuid.uuid4().hex[:8]}")
    variant = await make_variant(client, product["id"], **overrides)
    return variant["variants"][0]


async def quantity_on_hand(variant_id: str) -> int:
    async with session_scope() as db:
        return await db.scalar(
            text("SELECT quantity_on_hand FROM product_variants WHERE id = :id"),
            {"id": variant_id},
        )


async def movements(variant_id: str) -> list[dict]:
    async with session_scope() as db:
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT kind, quantity_delta FROM stock_movements "
                        "WHERE variant_id = :v ORDER BY created_at"
                    ),
                    {"v": variant_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def staff_id_for(client, email: str) -> str:
    roster = await client.get(STAFF)
    assert roster.status_code == 200, roster.text
    return next(row["id"] for row in roster.json()["staff"] if row["email"] == email)


async def make_staff(client, **overrides) -> dict:
    async with session_scope() as db:
        role_id = str(await db.scalar(text("SELECT id FROM roles WHERE name = 'Staff'")))
    body = {
        "first_name": "Sam",
        "last_name": "Lead",
        "email": f"{uuid.uuid4().hex[:8]}@cedar.example",
        "role_id": role_id,
        "commission_rate_retail_bp": 1500,
    }
    body.update(overrides)
    resp = await client.post(STAFF, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def start_sale(client, **overrides) -> dict:
    body: dict = {}
    body.update(overrides)
    resp = await client.post(RETAIL_SALES, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def add_line(client, sale_id: str, variant_id: str, quantity: int = 1) -> dict:
    resp = await client.post(
        f"{RETAIL_SALES}/{sale_id}/lines", json={"variant_id": variant_id, "quantity": quantity}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def issue(client, sale_id: str):
    # Every mutating request is JSON (main.py's CSRF guard), a body-less issue included.
    return await client.post(f"{RETAIL_SALES}/{sale_id}/issue", json={})


# --- draft: never touches stock ----------------------------------------------------------------


async def test_starting_a_draft_defaults_sold_by_to_the_acting_staff_member(client):
    await as_admin(client)
    me = await staff_id_for(client, EMAIL)

    sale = await start_sale(client)

    assert sale["sold_by_staff_id"] == me
    assert sale["status"] == "draft"
    assert sale["customer_id"] is None
    assert sale["payment_collector_staff_id"] is None
    assert sale["lines"] == []


async def test_sold_by_is_staff_selectable_at_creation(client):
    await as_admin(client)
    colleague = await make_staff(client)

    sale = await start_sale(client, sold_by_staff_id=colleague["id"])

    assert sale["sold_by_staff_id"] == colleague["id"]


async def test_a_draft_may_be_anonymous_or_linked_to_a_customer(client):
    await as_admin(client)

    anonymous = await start_sale(client)
    assert anonymous["customer_id"] is None

    customer_id = await make_customer(client)
    linked = await start_sale(client, customer_id=customer_id)
    assert linked["customer_id"] == customer_id


async def test_building_a_multi_line_cart_never_touches_stock(client):
    await as_admin(client)
    a = await new_variant(client, sku="SKU-A", barcode="1000000000001", quantity_on_hand=10)
    b = await new_variant(
        client, name="1L", sku="SKU-B", barcode="1000000000002", quantity_on_hand=20
    )

    sale = await start_sale(client)
    sale = await add_line(client, sale["id"], a["id"], quantity=3)
    sale = await add_line(client, sale["id"], b["id"], quantity=5)

    assert len(sale["lines"]) == 2
    assert await quantity_on_hand(a["id"]) == 10
    assert await quantity_on_hand(b["id"]) == 20
    assert await movements(a["id"]) == []
    assert await movements(b["id"]) == []


async def test_a_line_snapshots_the_variants_price_at_add_time(client):
    await as_admin(client)
    variant = await new_variant(client, price_cents=2499, quantity_on_hand=10)

    sale = await start_sale(client)
    sale = await add_line(client, sale["id"], variant["id"], quantity=2)

    assert sale["lines"][0]["unit_price_cents"] == 2499
    assert sale["lines"][0]["line_total_cents"] == 4998
    assert sale["subtotal_cents"] == 4998


async def test_a_line_may_be_removed_from_the_draft(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10)
    sale = await start_sale(client)
    sale = await add_line(client, sale["id"], variant["id"], quantity=1)
    line_id = sale["lines"][0]["id"]

    # Every mutating request is JSON (main.py's CSRF guard), DELETE included.
    resp = await client.delete(
        f"{RETAIL_SALES}/{sale['id']}/lines/{line_id}", headers={"Content-Type": "application/json"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["lines"] == []


async def test_adding_an_inactive_variant_is_refused(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10)
    product_id = variant["product_id"]
    deactivate = await client.post(
        f"/api/admin/products/{product_id}/variants/{variant['id']}/deactivate", json={}
    )
    assert deactivate.status_code == 200, deactivate.text
    sale = await start_sale(client)

    resp = await client.post(
        f"{RETAIL_SALES}/{sale['id']}/lines", json={"variant_id": variant["id"], "quantity": 1}
    )

    assert resp.status_code == 422, resp.text


async def test_adding_an_unknown_variant_404s(client):
    await as_admin(client)
    sale = await start_sale(client)

    resp = await client.post(
        f"{RETAIL_SALES}/{sale['id']}/lines", json={"variant_id": str(uuid.uuid4()), "quantity": 1}
    )

    assert resp.status_code == 404, resp.text


# --- "sold by" vs. the payment collector: two distinct attributions ---------------------------


async def test_sold_by_is_reassignable_while_still_a_draft(client):
    await as_admin(client)
    colleague = await make_staff(client)
    sale = await start_sale(client)

    resp = await client.patch(
        f"{RETAIL_SALES}/{sale['id']}", json={"sold_by_staff_id": colleague["id"]}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["sold_by_staff_id"] == colleague["id"]


async def test_setting_the_payment_collector_never_changes_sold_by(client):
    await as_admin(client)
    me = await staff_id_for(client, EMAIL)
    collector = await make_staff(client)
    sale = await start_sale(client)

    resp = await client.patch(
        f"{RETAIL_SALES}/{sale['id']}", json={"payment_collector_staff_id": collector["id"]}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["payment_collector_staff_id"] == collector["id"]
    assert body["sold_by_staff_id"] == me


async def test_reassigning_sold_by_never_changes_the_payment_collector(client):
    await as_admin(client)
    collector = await make_staff(client)
    new_seller = await make_staff(client, email="second@cedar.example")
    sale = await start_sale(client)
    await client.patch(
        f"{RETAIL_SALES}/{sale['id']}", json={"payment_collector_staff_id": collector["id"]}
    )

    resp = await client.patch(
        f"{RETAIL_SALES}/{sale['id']}", json={"sold_by_staff_id": new_seller["id"]}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sold_by_staff_id"] == new_seller["id"]
    assert body["payment_collector_staff_id"] == collector["id"]


# --- issue: the atomic, stock-deducting moment -------------------------------------------------


async def test_issuing_deducts_stock_and_writes_one_movement_per_line(client):
    await as_admin(client)
    a = await new_variant(client, sku="SKU-A", barcode="2000000000001", quantity_on_hand=10)
    b = await new_variant(
        client, name="1L", sku="SKU-B", barcode="2000000000002", quantity_on_hand=5
    )
    sale = await start_sale(client)
    sale = await add_line(client, sale["id"], a["id"], quantity=3)
    sale = await add_line(client, sale["id"], b["id"], quantity=2)

    resp = await issue(client, sale["id"])

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "issued"
    assert body["invoice_number"] == 1
    assert body["subtotal_cents"] == body["grand_total_cents"]
    assert {line["variant_id"] for line in body["lines"]} == {a["id"], b["id"]}
    assert await quantity_on_hand(a["id"]) == 7
    assert await quantity_on_hand(b["id"]) == 3
    assert [m["kind"] for m in await movements(a["id"])] == ["sale"]
    assert [m["quantity_delta"] for m in await movements(a["id"])] == [-3]
    assert [m["quantity_delta"] for m in await movements(b["id"])] == [-2]


async def test_a_second_retail_invoice_gets_the_next_sequential_number(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10)
    first = await start_sale(client)
    first = await add_line(client, first["id"], variant["id"], quantity=1)
    second = await start_sale(client)
    second = await add_line(client, second["id"], variant["id"], quantity=1)

    r1 = await issue(client, first["id"])
    r2 = await issue(client, second["id"])

    assert r1.json()["invoice_number"] == 1
    assert r2.json()["invoice_number"] == 2


async def test_the_retail_and_service_invoice_series_share_one_counter(client):
    """A service invoice and a retail invoice for the same business draw from the same
    `business_invoice_counters` row (`billing/models.py`'s own module section) — numbers never
    collide across the two document types."""
    await as_admin(client)
    from tests.test_bill_review import BILLS, complete_a_visit

    bill_id, _ = await complete_a_visit(client)
    service_invoice = await client.post(f"{BILLS}/{bill_id}/issue", json={})
    assert service_invoice.status_code == 201, service_invoice.text

    variant = await new_variant(client, quantity_on_hand=10)
    sale = await start_sale(client)
    sale = await add_line(client, sale["id"], variant["id"], quantity=1)
    retail_invoice = await issue(client, sale["id"])

    assert service_invoice.json()["invoice_number"] == 1
    assert retail_invoice.json()["invoice_number"] == 2


async def test_the_commission_rate_is_snapshotted_from_sold_by_at_issue(client):
    await as_admin(client)
    seller = await make_staff(client, commission_rate_retail_bp=2500)
    variant = await new_variant(client, quantity_on_hand=10)
    sale = await start_sale(client, sold_by_staff_id=seller["id"])
    sale = await add_line(client, sale["id"], variant["id"], quantity=1)

    resp = await issue(client, sale["id"])

    assert resp.status_code == 201, resp.text
    line = resp.json()["lines"][0]
    assert line["staff_id"] == seller["id"]
    assert line["commission_rate_bp"] == 2500


async def test_a_linked_sale_stays_visible_on_the_invoice(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    variant = await new_variant(client, quantity_on_hand=10)
    sale = await start_sale(client, customer_id=customer_id)
    sale = await add_line(client, sale["id"], variant["id"], quantity=1)

    resp = await issue(client, sale["id"])

    assert resp.status_code == 201, resp.text
    assert resp.json()["customer_id"] == customer_id

    anonymous = await start_sale(client)
    await add_line(client, anonymous["id"], variant["id"], quantity=1)
    assert (await issue(client, anonymous["id"])).status_code == 201

    history = await client.get(RETAIL_INVOICES, params={"customer_id": customer_id})
    assert history.status_code == 200, history.text
    assert [row["id"] for row in history.json()["retail_invoices"]] == [resp.json()["id"]]


async def test_an_anonymous_sale_issues_with_no_customer(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10)
    sale = await start_sale(client)
    sale = await add_line(client, sale["id"], variant["id"], quantity=1)

    resp = await issue(client, sale["id"])

    assert resp.status_code == 201, resp.text
    assert resp.json()["customer_id"] is None


async def test_an_empty_sale_cannot_be_issued(client):
    await as_admin(client)
    sale = await start_sale(client)

    resp = await issue(client, sale["id"])

    assert resp.status_code == 422, resp.text


async def test_issuing_an_already_issued_sale_is_refused(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10)
    sale = await start_sale(client)
    sale = await add_line(client, sale["id"], variant["id"], quantity=1)
    first = await issue(client, sale["id"])
    assert first.status_code == 201, first.text

    second = await issue(client, sale["id"])

    assert second.status_code == 422, second.text


async def test_lines_cannot_be_added_to_an_issued_sale(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10)
    sale = await start_sale(client)
    sale = await add_line(client, sale["id"], variant["id"], quantity=1)
    issued = await issue(client, sale["id"])
    assert issued.status_code == 201, issued.text

    resp = await client.post(
        f"{RETAIL_SALES}/{sale['id']}/lines", json={"variant_id": variant["id"], "quantity": 1}
    )

    assert resp.status_code == 422, resp.text


# --- issue refuses insufficient stock, atomically, with a full rollback -----------------------


async def test_issuing_with_insufficient_stock_is_refused_and_deducts_nothing(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=2)
    sale = await start_sale(client)
    sale = await add_line(client, sale["id"], variant["id"], quantity=5)

    resp = await issue(client, sale["id"])

    assert resp.status_code == 409, resp.text
    assert variant["name"] in resp.text
    assert await quantity_on_hand(variant["id"]) == 2
    assert await movements(variant["id"]) == []
    async with session_scope() as db:
        count = await db.scalar(text("SELECT count(*) FROM retail_invoices"))
    assert count == 0


async def test_a_second_lines_shortfall_rolls_back_the_first_lines_deduction_too(client):
    """The whole-transaction guarantee (#75's own central acceptance criterion): if any one
    line's stock is unavailable, the *entire* issue attempt is undone — including whatever a
    line processed before it already, atomically, deducted within this same not-yet-committed
    transaction."""
    await as_admin(client)
    plenty = await new_variant(client, sku="SKU-P", barcode="3000000000001", quantity_on_hand=100)
    scarce = await new_variant(
        client, name="1L", sku="SKU-S", barcode="3000000000002", quantity_on_hand=1
    )
    sale = await start_sale(client)
    # `plenty`'s variant id sorts before `scarce`'s far more often than not is irrelevant here —
    # whichever line the atomic loop reaches first, the other's shortfall must still roll it
    # back; the quantities are chosen so *one* of the two lines always fails.
    sale = await add_line(client, sale["id"], plenty["id"], quantity=1)
    sale = await add_line(client, sale["id"], scarce["id"], quantity=5)

    resp = await issue(client, sale["id"])

    assert resp.status_code == 409, resp.text
    assert await quantity_on_hand(plenty["id"]) == 100
    assert await quantity_on_hand(scarce["id"]) == 1
    assert await movements(plenty["id"]) == []
    assert await movements(scarce["id"]) == []
    async with session_scope() as db:
        count = await db.scalar(text("SELECT count(*) FROM retail_invoices"))
    assert count == 0


# `VariantNotFound` at issue (a line's variant missing outright) is unreachable through this
# schema: `retail_sale_lines.variant_id` is a real FK, so even a direct SQL rewrite of a line
# refuses rather than pointing it at nothing — belt-and-braces error handling in `issue_retail_
# sale`, proven by the same route-level 404 branch `inventory/stock_routes.py` already has for
# `record_movement`'s own `VariantNotFound`, not by a test that would require an impossible row.


# --- the concurrency proof: exactly one of two simultaneous issues wins ------------------------


async def test_two_concurrent_issues_for_the_last_unit_yield_one_201_and_one_409(
    client, monkeypatch
):
    """Two ASGI clients race to issue two *different* retail sales, each buying the last unit
    of the same variant. Nothing in the app locks — `inventory/stock.py::record_movement`'s own
    atomic `UPDATE ... WHERE quantity_on_hand + :delta >= 0` inside the one transaction each
    issue attempt holds is the lock (CLAUDE.md "Concurrency": stock). The sleep tacked onto the
    real call keeps the first transaction open long enough that the second's own `UPDATE`
    genuinely blocks on Postgres's row lock rather than racing in application code — the same
    interleaving `test_stock_movements.py`'s own concurrency test forces."""
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=1)
    sale_a = await start_sale(client)
    sale_a = await add_line(client, sale_a["id"], variant["id"], quantity=1)
    sale_b = await start_sale(client)
    sale_b = await add_line(client, sale_b["id"], variant["id"], quantity=1)
    cookie = client.cookies["linsuite_session"]

    from billing import retail_sales as retail_sales_mod
    from main import app as main_app

    real_record_movement = retail_sales_mod.record_movement

    async def slow_record_movement(*args, **kwargs):
        result = await real_record_movement(*args, **kwargs)
        await asyncio.sleep(0.25)
        return result

    monkeypatch.setattr(retail_sales_mod, "record_movement", slow_record_movement)

    async def attempt(sale_id: str):
        async with AsyncClient(transport=ASGITransport(app=main_app), base_url="http://test") as c:
            c.cookies.set("linsuite_session", cookie)
            return await issue(c, sale_id)

    results = await asyncio.gather(attempt(sale_a["id"]), attempt(sale_b["id"]))

    assert sorted(r.status_code for r in results) == [201, 409], [r.text for r in results]
    failed = next(r for r in results if r.status_code == 409)
    assert variant["name"] in failed.text
    assert await quantity_on_hand(variant["id"]) == 0
    assert len(await movements(variant["id"])) == 1


# --- reading ------------------------------------------------------------------------------------


async def test_issued_retail_invoices_are_listed_and_readable(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10)
    sale = await start_sale(client)
    sale = await add_line(client, sale["id"], variant["id"], quantity=1)
    issued = await issue(client, sale["id"])
    invoice_id = issued.json()["id"]

    listing = await client.get(RETAIL_INVOICES)
    assert listing.status_code == 200, listing.text
    assert [row["id"] for row in listing.json()["retail_invoices"]] == [invoice_id]

    single = await client.get(f"{RETAIL_INVOICES}/{invoice_id}")
    assert single.status_code == 200, single.text
    assert single.json()["id"] == invoice_id


# --- capability --------------------------------------------------------------------------------


async def test_retail_sale_routes_need_billing_view(client):
    await as_admin(client)
    role = await client.post(
        "/api/admin/roles",
        json={"name": "No Billing", "description": "x", "capabilities": []},
    )
    assert role.status_code == 201, role.text

    from core.security import hash_password
    from tests.conftest import add_account

    await add_account(
        "stranger@cedar.example",
        await hash_password("correct horse battery 3"),
        role=role.json()["id"],
    )
    client.cookies.clear()
    login = await client.post(
        "/api/auth/login",
        json={"email": "stranger@cedar.example", "password": "correct horse battery 3"},
    )
    assert login.status_code == 200, login.text

    resp = await client.post(RETAIL_SALES, json={})

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "capability_required"


async def test_billing_view_is_reachable_in_staff_mode_with_no_admin_window(client):
    await add_front_desk_account()
    await as_staff(client)

    resp = await client.post(RETAIL_SALES, json={})

    assert resp.status_code == 201, resp.text


# --- immutability: append-only / voidable, by grant and trigger -------------------------------


async def _issue_one(client) -> dict:
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10)
    sale = await start_sale(client)
    sale = await add_line(client, sale["id"], variant["id"], quantity=1)
    resp = await issue(client, sale["id"])
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_the_app_role_may_not_delete_a_retail_invoice(client):
    invoice = await _issue_one(client)
    async with session_scope() as db:
        with pytest.raises(DBAPIError) as refused:
            await db.execute(text(f"DELETE FROM retail_invoices WHERE id = '{invoice['id']}'"))
        await db.rollback()
    assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value


async def test_the_app_role_may_not_update_a_retail_invoices_money_columns(client):
    invoice = await _issue_one(client)
    async with session_scope() as db:
        with pytest.raises(DBAPIError) as refused:
            await db.execute(
                text(
                    f"UPDATE retail_invoices SET grand_total_cents = 1 WHERE id = '{invoice['id']}'"
                )
            )
        await db.rollback()
    assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value


async def test_the_app_role_may_perform_the_one_permitted_retail_cancel_transition(client):
    invoice = await _issue_one(client)
    async with session_scope() as db:
        await db.execute(
            text(
                "UPDATE retail_invoices SET status = 'cancelled', cancelled_at = now(), "
                "cancelled_by = issued_by, cancel_reason = 'test' WHERE id = :id"
            ),
            {"id": invoice["id"]},
        )
        await db.commit()
    async with session_scope() as db:
        status = await db.scalar(
            text("SELECT status FROM retail_invoices WHERE id = :id"), {"id": invoice["id"]}
        )
    assert status == "cancelled"


@pytest.mark.parametrize(
    "statement",
    ["UPDATE retail_invoice_lines SET quantity = 99", "DELETE FROM retail_invoice_lines"],
)
async def test_the_app_role_may_neither_update_nor_delete_a_retail_invoice_line(client, statement):
    await _issue_one(client)
    async with session_scope() as db:
        with pytest.raises(DBAPIError) as refused:
            await db.execute(text(statement))
        await db.rollback()
    assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value


@pytest.mark.parametrize(
    "table,trigger",
    [
        ("retail_invoices", "retail_invoices_voidable_guard"),
        ("retail_invoice_lines", "retail_invoice_lines_no_rewrite"),
    ],
)
async def test_the_guard_trigger_is_attached_and_enabled(client, table, trigger):
    async with session_scope() as db:
        enabled = await db.scalar(
            text(
                "SELECT tgenabled::text FROM pg_trigger "
                f"WHERE tgrelid = '{table}'::regclass AND tgname = '{trigger}'"
            )
        )
    assert enabled == "O"

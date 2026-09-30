"""M4 review T1 (R1-R6): per-item tax components, inclusive pricing, retail tax/discounts, frozen
discount rules, and an admin override that reconciles to the cent.

S2 for the pure pricing functions; S1 (real PostgreSQL, app role) for everything a client sees.
Reuses `tests/test_retail_sales.py`'s `claimed_instance` (it wipes both the service and retail
billing tables) and the bill/retail helpers.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from billing.commission import CommissionDiscountInput
from billing.discount_resolver import DiscountInput, resolve_discount_amounts
from billing.pricing import OverrideConflict, distribute_override, price_line
from billing.tax import ComponentRate, compute_line_tax
from core.db import session_scope
from tests.conftest import get_owner_engine
from tests.test_bill_review import (
    BILLS,
    as_admin,
    complete_a_visit,
    make_discount,
    make_tax_component,
)
from tests.test_retail_sales import (  # noqa: F401 — autouse fixture
    RETAIL_INVOICES,
    RETAIL_SALES,
    add_line,
    claimed_instance,
    issue,
    new_variant,
    start_sale,
)


@pytest.fixture(autouse=True)
async def wipe_issued_rows_after(claimed_instance):  # noqa: F811 — runs after its setup
    """Issued rows are immutable to the app role; later test files' app-role wipes cannot
    remove them, and none of these are among the purge role's four permitted tables (#83), so
    this reset runs as the schema owner."""
    yield
    async with get_owner_engine().begin() as owner:
        for table in (
            "retail_return_lines",
            "retail_returns",
            "package_credit_voids",
            "package_credit_redemptions",
            "package_purchase_credits",
            "invoice_payment_transfers",
            "invoice_refunds",
            "invoice_payments",
            "invoice_balance_authorizations",
            "commission_postings",
            "retail_invoice_line_taxes",
            "retail_invoice_line_discounts",
            "retail_invoice_lines",
            "retail_invoices",
            "invoice_line_taxes",
            "invoice_line_discounts",
            "invoice_lines",
            "invoices",
            "business_invoice_counters",
            "stock_movements",
        ):
            await owner.execute(text(f"DELETE FROM {table}"))


GST = ComponentRate("GST", 50_000)
PST = ComponentRate("PST", 70_000)
LOW, HIGH = uuid.UUID(int=1), uuid.UUID(int=2)


# --- S2: the pure functions -------------------------------------------------------------------


def test_resolved_discount_amounts_are_sequential_and_sum_to_the_stacked_result():
    ten = DiscountInput(id=LOW, kind="percentage", stackable=True, percentage_bp=1000)
    twenty = DiscountInput(id=HIGH, kind="percentage", stackable=True, percentage_bp=2000)
    five = DiscountInput(id=uuid.UUID(int=3), kind="fixed", stackable=True, amount_cents=500)

    # 10000 -> 10% off (1000) -> 20% off 9000 (1800) = 7200, then 5.00 fixed.
    assert resolve_discount_amounts(10000, [twenty, five, ten]) == {
        LOW: 1000,
        HIGH: 1800,
        five.id: 500,
    }
    # Odd cents: the last percentage absorbs the rounding so the parts still sum exactly.
    amounts = resolve_discount_amounts(333, [ten, twenty])
    assert sum(amounts.values()) == 333 - 240  # round(333 * 0.72) = 240


def test_an_inclusive_price_backs_tax_out_and_reconciles_exactly():
    line = price_line(11300, "inclusive", [GST, PST], [])

    assert line.tax.total_cents == 11300
    assert line.tax.pretax_cents == 10089  # 11300 / 1.12, half-up
    assert line.tax.component_cents == {"GST": 505, "PST": 706}
    assert line.tax.pretax_cents + line.tax.tax_cents == 11300
    assert line.commission_basis_cents == 10089  # tax excluded from commission


def test_commission_basis_ignores_absorbed_discounts_and_tax():
    absorbed = CommissionDiscountInput(
        id=LOW,
        kind="fixed",
        stackable=True,
        percentage_bp=None,
        amount_cents=1000,
        commission_basis="absorbed",
    )
    reduces = CommissionDiscountInput(
        id=HIGH,
        kind="fixed",
        stackable=True,
        percentage_bp=None,
        amount_cents=2000,
        commission_basis="reduces",
    )

    line = price_line(10000, "exclusive", [GST], [absorbed, reduces])

    assert line.discounted_cents == 7000  # the client pays 70 + tax
    assert line.commission_basis_cents == 8000  # spec §142's worked example
    assert line.tax.tax_cents == 350


def test_an_override_is_distributed_so_every_line_reconciles():
    a = compute_line_tax(10000, [GST, PST], "exclusive")  # 11200
    b = compute_line_tax(5000, [GST], "exclusive")  # 5250
    prepaid = compute_line_tax(3000, [], "exclusive")

    billed = distribute_override(
        18000, "inclusive", [(a, [GST, PST], False), (b, [GST], False), (prepaid, [], True)]
    )

    assert sum(t.total_cents for t in billed) == 18000
    assert billed[2] == prepaid  # a prepaid line keeps its frozen value
    for tax in billed:
        assert tax.pretax_cents + tax.tax_cents == tax.total_cents
        assert sum(tax.component_cents.values()) == tax.tax_cents
    with pytest.raises(OverrideConflict):
        distribute_override(2999, "inclusive", [(a, [GST, PST], False), (prepaid, [], True)])


# --- S1 helpers -------------------------------------------------------------------------------


async def bc_business_with_gst_and_pst(client) -> None:
    await as_admin(client)
    resp = await client.put("/api/admin/business", json={"name": "Cedar Lane", "province": "BC"})
    assert resp.status_code == 200, resp.text
    # #118 pre-fills BC's own GST/PST the moment the province is saved on a business with no
    # components yet — this file wants its own hand-built rates instead, so clear whatever
    # pre-fill just created before building the exact fixture these tests expect.
    async with session_scope() as db:
        await db.execute(text("DELETE FROM tax_component_rates"))
        await db.execute(text("DELETE FROM tax_components"))
        await db.commit()
    await make_tax_component(client, code="gst", name="GST", province=None, rate_ppm=50_000)
    await make_tax_component(client, code="pst", name="PST", province="BC", rate_ppm=70_000)


async def commission_bases(invoice_id: str) -> list[int]:
    async with session_scope() as db:
        return list(
            await db.scalars(
                text(
                    "SELECT c.basis_cents FROM commission_postings c "
                    "JOIN invoice_lines l ON l.id = c.invoice_line_id "
                    "WHERE c.invoice_id = :i ORDER BY l.price_cents DESC"
                ),
                {"i": invoice_id},
            )
        )


async def issue_bill(client, bill_id: str) -> dict:
    resp = await client.post(f"{BILLS}/{bill_id}/issue", json={})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def override(client, bill_id: str, total_cents: int, **extra):
    request = await client.post(
        f"{BILLS}/{bill_id}/override-requests",
        json={"kind": "price_override", "requested_total_cents": total_cents, "reason": "Loyal"}
        | extra,
    )
    assert request.status_code == 201, request.text
    return await client.post(
        f"{BILLS}/{bill_id}/override-requests/{request.json()['id']}/decision",
        json={"decision": "approved"},
    )


async def two_line_bill(client) -> str:
    """A 100.00 GST+PST massage and a 50.00 GST-only consult on one draft bill."""
    bill_id, _ = await complete_a_visit(
        client, price_cents=10000, tax_component_keys=["GST", "PST"]
    )
    other, _ = await complete_a_visit(
        client, price_cents=5000, service_name="Consult", tax_component_keys=["GST"]
    )
    async with session_scope() as db:
        await db.execute(
            text("UPDATE service_bill_lines SET bill_id = :a WHERE bill_id = :b"),
            {"a": bill_id, "b": other},
        )
        await db.commit()
    return bill_id


# --- R1: each item toggles its own components ---------------------------------------------------


async def test_gst_yes_pst_no_on_a_service_is_honoured_and_frozen(client):
    await bc_business_with_gst_and_pst(client)
    bill_id, _ = await complete_a_visit(client, price_cents=10000, tax_component_keys=["GST"])

    invoice = await issue_bill(client, bill_id)

    [line] = invoice["lines"]
    assert [(t["component_code"], t["rate_ppm"], t["amount_cents"]) for t in line["taxes"]] == [
        ("GST", 50_000, 500)
    ]
    assert invoice["tax_totals_by_component"] == {"GST": 500}
    assert invoice["grand_total_cents"] == 10500
    assert invoice["tax_rates_by_component"] == {"GST": 50_000, "PST": 70_000}


async def test_a_catalog_item_cannot_toggle_an_unknown_component(client):
    await as_admin(client)
    resp = await client.post(
        "/api/admin/services",
        json={"name": "Facial", "duration_minutes": 60, "tax_component_keys": ["VAT"]},
    )
    assert resp.status_code == 422, resp.text
    assert "VAT" in resp.json()["detail"]


# --- R2: tax-inclusive catalog pricing ----------------------------------------------------------


async def test_an_inclusive_service_price_is_the_total_and_commission_excludes_tax(client):
    await bc_business_with_gst_and_pst(client)
    bill_id, _ = await complete_a_visit(
        client, price_cents=11300, tax_component_keys=["GST", "PST"], tax_convention="inclusive"
    )

    invoice = await issue_bill(client, bill_id)

    [line] = invoice["lines"]
    assert line["tax_convention"] == "inclusive"
    assert (line["line_total_cents"], line["pretax_cents"], line["tax_cents"]) == (
        11300,
        10089,
        1211,
    )
    assert {t["component_code"]: t["amount_cents"] for t in line["taxes"]} == {
        "GST": 505,
        "PST": 706,
    }
    assert invoice["grand_total_cents"] == 11300
    assert await commission_bases(invoice["id"]) == [10089]


# --- R3/R4: retail is taxed and discounted, and both are frozen --------------------------------


async def test_a_retail_sale_is_taxed_and_discounted_and_frozen_at_issue(client):
    await bc_business_with_gst_and_pst(client)
    variant = await new_variant(
        client, price_cents=2000, quantity_on_hand=10, tax_component_keys=["gst", "pst"]
    )
    discount = await make_discount(client, name="Retail 10%", percentage_bp=1000)
    sale = await start_sale(client)
    await add_line(client, sale["id"], variant["id"], quantity=2)

    draft = await client.put(
        f"{RETAIL_SALES}/{sale['id']}/discounts", json={"discount_ids": [discount["id"]]}
    )
    assert draft.status_code == 200, draft.text
    # 2 x 20.00 = 40.00, 10% off = 36.00, GST 1.80 + PST 2.52 = 40.32.
    assert (
        draft.json()["subtotal_cents"],
        draft.json()["discount_total_cents"],
        draft.json()["tax_total_cents"],
        draft.json()["grand_total_cents"],
    ) == (4000, 400, 432, 4032)

    resp = await issue(client, sale["id"])
    assert resp.status_code == 201, resp.text
    invoice = resp.json()
    [line] = invoice["lines"]
    assert (line["discount_cents"], line["tax_cents"], line["line_total_cents"]) == (400, 432, 4032)
    [frozen] = line["discounts"]
    assert (frozen["percentage_bp"], frozen["resolved_amount_cents"]) == (1000, 400)
    assert {t["component_code"]: (t["rate_ppm"], t["amount_cents"]) for t in line["taxes"]} == {
        "GST": (50_000, 180),
        "PST": (70_000, 252),
    }
    assert invoice["grand_total_cents"] == 4032
    assert invoice["outstanding_cents"] == 4032

    # Later definition/rate edits never reach the issued invoice.
    async with session_scope() as db:
        await db.execute(text("UPDATE discounts SET percentage_bp = 5000"))
        await db.execute(text("UPDATE tax_component_rates SET rate_ppm = 100000"))
        await db.execute(text("UPDATE product_variants SET tax_component_keys = '[]'"))
        await db.commit()
    again = await client.get(f"{RETAIL_INVOICES}/{invoice['id']}")
    assert again.json() == invoice


async def test_the_frozen_retail_tax_rows_are_append_only(client):
    await bc_business_with_gst_and_pst(client)
    variant = await new_variant(client, price_cents=1000, tax_component_keys=["GST"])
    sale = await start_sale(client)
    await add_line(client, sale["id"], variant["id"])
    assert (await issue(client, sale["id"])).status_code == 201

    for statement in (
        "UPDATE retail_invoice_line_taxes SET amount_cents = 0",
        # The one transition the voidable guard permits, smuggling a tax rewrite along —
        # only `retail_invoices_tax_snapshot_frozen` stands in the way.
        "UPDATE retail_invoices SET status = 'cancelled', cancelled_at = now(), "
        "cancelled_by = issued_by, cancel_reason = 'x', tax_total_cents = 0",
    ):
        async with session_scope() as db:
            with pytest.raises(DBAPIError) as refused:
                await db.execute(text(statement))
            await db.rollback()
        assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value


async def test_an_ineligible_retail_discount_does_not_apply(client):
    await as_admin(client)
    variant = await new_variant(client, price_cents=1000)
    discount = await make_discount(client, eligibility_scope="selected")
    sale = await start_sale(client)
    await add_line(client, sale["id"], variant["id"])

    resp = await client.put(
        f"{RETAIL_SALES}/{sale['id']}/discounts", json={"discount_ids": [discount["id"]]}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["discount_total_cents"] == 0


async def test_a_service_invoice_freezes_the_discount_rule_and_amount(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client, price_cents=10000)
    ten = await make_discount(client, name="Ten", percentage_bp=1000, stackable=True)
    five = await make_discount(
        client, name="Five", kind="fixed", percentage_bp=None, amount_cents=500, stackable=True
    )
    applied = await client.put(
        f"{BILLS}/{bill_id}/discounts", json={"discount_ids": [ten["id"], five["id"]]}
    )
    assert applied.status_code == 200, applied.text

    invoice = await issue_bill(client, bill_id)

    frozen = {d["discount_name"]: d for d in invoice["lines"][0]["discounts"]}
    assert (frozen["Ten"]["percentage_bp"], frozen["Ten"]["resolved_amount_cents"]) == (1000, 1000)
    assert (frozen["Five"]["amount_cents"], frozen["Five"]["resolved_amount_cents"]) == (500, 500)
    assert invoice["lines"][0]["discounted_cents"] == 8500


# --- R5/R6: an override reconciles to the cent ---------------------------------------------------


async def test_an_override_is_spread_over_lines_tax_rows_and_commission(client):
    await bc_business_with_gst_and_pst(client)
    bill_id = await two_line_bill(client)  # computed 112.00 + 52.50 = 164.50

    decided = await override(client, bill_id, 15000)
    assert decided.status_code == 200, decided.text
    bill = (await client.get(f"{BILLS}/{bill_id}")).json()
    assert bill["override_grand_total_cents"] == 15000

    invoice = await issue_bill(client, bill_id)

    lines = sorted(invoice["lines"], key=lambda line: -line["price_cents"])
    # 150.00 split 11200:5250 -> 102.13 / 47.87, each re-taxed inclusive.
    assert [line["line_total_cents"] for line in lines] == [10213, 4787]
    assert [line["pretax_cents"] for line in lines] == [9119, 4559]
    assert {t["component_code"]: t["amount_cents"] for t in lines[0]["taxes"]} == {
        "GST": 456,
        "PST": 638,
    }
    assert {t["component_code"]: t["amount_cents"] for t in lines[1]["taxes"]} == {"GST": 228}
    for line in lines:
        assert sum(t["amount_cents"] for t in line["taxes"]) == line["tax_cents"]
        assert line["price_cents"] - line["discounted_cents"] == -line["override_adjustment_cents"]
    assert invoice["grand_total_cents"] == 15000
    assert sum(invoice["tax_totals_by_component"].values()) == sum(
        line["tax_cents"] for line in lines
    )
    assert invoice["outstanding_cents"] == 15000
    # Commission follows the revised, pre-tax price by default (spec §142).
    assert await commission_bases(invoice["id"]) == [9119, 4559]


async def test_an_absorbed_pre_tax_override_keeps_commission_on_the_original_price(client):
    await bc_business_with_gst_and_pst(client)
    bill_id, _ = await complete_a_visit(client, price_cents=10000, tax_component_keys=["GST"])

    decided = await override(
        client, bill_id, 8000, tax_convention="exclusive", commission_basis="absorbed"
    )
    assert decided.status_code == 200, decided.text
    invoice = await issue_bill(client, bill_id)

    [line] = invoice["lines"]
    assert (line["pretax_cents"], line["tax_cents"], line["line_total_cents"]) == (8000, 400, 8400)
    assert invoice["grand_total_cents"] == 8400
    assert invoice["override_tax_convention"] == "exclusive"
    assert await commission_bases(invoice["id"]) == [10000]


async def test_prepaid_lines_keep_their_value_under_an_override(client):
    await bc_business_with_gst_and_pst(client)
    bill_id = await two_line_bill(client)
    async with session_scope() as db:  # the 50.00 consult was settled by a package credit
        await db.execute(
            text(
                "UPDATE service_bill_lines SET prepaid_cents = price_cents "
                "WHERE bill_id = :b AND price_cents = 5000"
            ),
            {"b": bill_id},
        )
        await db.commit()

    refused = await override(client, bill_id, 4000)  # below the 50.00 already prepaid
    assert refused.status_code == 422, refused.text
    [pending] = (await client.get(f"{BILLS}/{bill_id}/override-requests")).json()["requests"]
    rejected = await client.post(
        f"{BILLS}/{bill_id}/override-requests/{pending['id']}/decision",
        json={"decision": "rejected"},
    )
    assert rejected.status_code == 200, rejected.text

    assert (await override(client, bill_id, 12000)).status_code == 200
    invoice = await issue_bill(client, bill_id)

    totals = sorted(line["line_total_cents"] for line in invoice["lines"])
    assert totals == [5000, 7000]
    assert invoice["prepaid_cents"] == 5000
    assert invoice["outstanding_cents"] == 7000

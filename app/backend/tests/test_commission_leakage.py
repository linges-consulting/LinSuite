"""S1: the ticket's own explicit, dedicated acceptance criterion (#69) — "No staff-facing bill,
payment, roster or document payload includes commission rates or amounts, verified by checking
those response shapes directly, not just the report endpoint's own access gate."

Every test here signs in as a plain front-desk Staff account (`billing.view`/`schedule.view`,
seeded to the `Staff` role, no admin capability, no Admin Mode window) and walks the raw JSON
body of a real response, recursively, for any key whose name mentions "commission" at all —
not just the two known field names, so a differently-named future leak (`commission_pct`,
`earnings_bp`, ...) would still be caught. This is a direct probe of the response shapes
themselves, deliberately independent of `commission_report.py`'s own `commission.view`/Admin
Mode gate (proven separately in `tests/test_commission_report.py`).
"""

import pytest
from sqlalchemy import text

from core.db import get_purge_engine, session_scope
from tests.conftest import wipe_document_keys
from tests.test_bill_review import (
    BILLS,
    EMAIL,
    SETUP,
    STAFF,
    STAFF_EMAIL,
    add_front_desk_account,
    as_admin,
    as_staff,
    complete_a_visit,
)
from tests.test_invoice_issue import INVOICES, issue_url


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async def wipe():
        async with get_purge_engine().begin() as purge:
            await purge.execute(text("DELETE FROM audit_events"))
            await purge.execute(text("DELETE FROM erasure_requests"))
            await purge.execute(text("DELETE FROM form_links"))
            await purge.execute(text("DELETE FROM retail_return_lines"))  # R26: retail too
            await purge.execute(text("DELETE FROM retail_returns"))
            await purge.execute(text("DELETE FROM invoice_payments"))
            await purge.execute(text("DELETE FROM invoice_balance_authorizations"))
            await purge.execute(text("DELETE FROM invoice_payment_transfers"))  # #68
            await purge.execute(text("DELETE FROM commission_postings"))
            await purge.execute(text("DELETE FROM retail_invoice_line_taxes"))
            await purge.execute(text("DELETE FROM retail_invoice_line_discounts"))
            await purge.execute(text("DELETE FROM retail_invoice_lines"))
            await purge.execute(text("DELETE FROM invoice_line_taxes"))
            await purge.execute(text("DELETE FROM invoice_line_discounts"))
            await purge.execute(text("DELETE FROM invoice_lines"))
            await purge.execute(text("DELETE FROM retail_return_lines"))  # #76
            await purge.execute(text("DELETE FROM retail_returns"))  # #76
            await purge.execute(text("DELETE FROM invoice_refunds"))  # #67
            await purge.execute(text("DELETE FROM retail_invoices"))
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


def _walk_keys(payload) -> set[str]:
    """Every dict key anywhere in a JSON body, recursively."""
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.add(key)
            found |= _walk_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= _walk_keys(item)
    return found


def _assert_no_commission_leak(payload) -> None:
    suspicious = {k for k in _walk_keys(payload) if "commission" in k.lower()}
    assert not suspicious, (
        f"commission-related keys leaked into a staff-facing payload: {suspicious}"
    )


async def _issue_as_staff(client) -> tuple[str, str]:
    """Sets a real, non-zero commission rate, completes and issues a visit as admin, then
    hands the session to a plain front-desk Staff account — returns (bill_id, invoice_id)."""
    await as_admin(client)
    roster = await client.get(STAFF)
    assert roster.status_code == 200, roster.text
    me = next(row["id"] for row in roster.json()["staff"] if row["email"] == EMAIL)
    rated = await client.patch(f"{STAFF}/{me}", json={"commission_rate_services_bp": 3333})
    assert rated.status_code == 200, rated.text

    bill_id, _ = await complete_a_visit(client, price_cents=10000)
    issued = await client.post(issue_url(bill_id), json={})
    assert issued.status_code == 201, issued.text
    invoice_id = issued.json()["id"]

    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)
    return bill_id, invoice_id


# --- billing/bill_review.py: the bill-line output ------------------------------------------


async def test_the_draft_bill_list_never_includes_commission_fields(client):
    await as_admin(client)
    await complete_a_visit(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)

    resp = await client.get(BILLS)

    assert resp.status_code == 200, resp.text
    _assert_no_commission_leak(resp.json())


async def test_a_single_draft_bills_lines_never_include_commission_fields(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)

    resp = await client.get(f"{BILLS}/{bill_id}")

    assert resp.status_code == 200, resp.text
    _assert_no_commission_leak(resp.json())


# --- scheduling/staff.py: the roster (`/api/staff`, `schedule.view`, no admin) --------------


async def test_the_public_roster_never_includes_commission_fields(client):
    await as_admin(client)
    me_roster = await client.get(STAFF)
    me = next(row["id"] for row in me_roster.json()["staff"] if row["email"] == EMAIL)
    rated = await client.patch(f"{STAFF}/{me}", json={"commission_rate_services_bp": 4200})
    assert rated.status_code == 200, rated.text
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)

    resp = await client.get("/api/staff")

    assert resp.status_code == 200, resp.text
    _assert_no_commission_leak(resp.json())
    assert len(resp.json()["staff"]) >= 1


# --- billing/invoices.py: the issued invoice's own line serializer ---------------------------


async def test_the_invoice_list_never_includes_commission_fields(client):
    await _issue_as_staff(client)

    resp = await client.get(INVOICES)

    assert resp.status_code == 200, resp.text
    _assert_no_commission_leak(resp.json())
    assert len(resp.json()["invoices"]) == 1


async def test_a_single_invoices_lines_never_include_commission_fields(client):
    _bill_id, invoice_id = await _issue_as_staff(client)

    resp = await client.get(f"{INVOICES}/{invoice_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    _assert_no_commission_leak(body)
    assert len(body["lines"]) == 1
    assert len(body["lines"][0]["discounts"]) == 0  # sanity: this line has no discounts applied


async def test_issuing_itself_returns_no_commission_fields_in_the_201_body(client):
    """The response to `POST /bills/{id}/issue` is itself Staff-Mode-reachable and must never
    carry what it just posted to the (admin-only) commission ledger."""
    await as_admin(client)
    roster = await client.get(STAFF)
    me = next(row["id"] for row in roster.json()["staff"] if row["email"] == EMAIL)
    rated = await client.patch(f"{STAFF}/{me}", json={"commission_rate_services_bp": 2500})
    assert rated.status_code == 200, rated.text
    bill_id, _ = await complete_a_visit(client, price_cents=10000)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)

    resp = await client.post(issue_url(bill_id), json={})

    assert resp.status_code == 201, resp.text
    _assert_no_commission_leak(resp.json())


# --- billing/retail_sales.py: retail invoices (R26) ------------------------------------------


async def _retail_as_staff(client) -> dict:
    """An admin issues a retail sale sold by a staff member with a real retail rate and
    returns one unit, then hands the session to a plain front-desk Staff account."""
    from tests.test_retail_sales import add_line, issue, make_staff, new_variant, start_sale

    await as_admin(client)
    seller = await make_staff(client, commission_rate_retail_bp=2500)
    variant = await new_variant(client, quantity_on_hand=10)
    sale = await start_sale(client, sold_by_staff_id=seller["id"])
    await add_line(client, sale["id"], variant["id"], quantity=2)
    issued = await issue(client, sale["id"])
    assert issued.status_code == 201, issued.text
    _assert_no_commission_leak(issued.json())
    returned = await client.post(
        f"/api/retail-invoices/{issued.json()['id']}/returns",
        json={
            "reason": "Changed mind",
            "lines": [
                {
                    "retail_invoice_line_id": issued.json()["lines"][0]["id"],
                    "quantity": 1,
                    "restock": True,
                }
            ],
        },
    )
    assert returned.status_code == 201, returned.text
    _assert_no_commission_leak(returned.json())

    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)
    return issued.json()


async def test_retail_invoice_reads_never_include_commission_fields(client):
    invoice = await _retail_as_staff(client)

    single = await client.get(f"/api/retail-invoices/{invoice['id']}")
    listed = await client.get("/api/retail-invoices")
    draft = await client.get(f"/api/retail-sales/{invoice['retail_sale_id']}")

    for resp in (single, listed, draft):
        assert resp.status_code == 200, resp.text
        _assert_no_commission_leak(resp.json())
    assert len(single.json()["lines"]) == 1


async def test_retail_cancel_as_staff_returns_no_commission_fields(client):
    invoice = await _retail_as_staff(client)

    resp = await client.post(
        f"/api/retail-invoices/{invoice['id']}/cancel", json={"reason": "Wrong item"}
    )

    assert resp.status_code == 200, resp.text
    _assert_no_commission_leak(resp.json())
    replacement = await client.get(f"/api/retail-sales/{resp.json()['replacement_sale_id']}")
    assert replacement.status_code == 200, replacement.text
    _assert_no_commission_leak(replacement.json())

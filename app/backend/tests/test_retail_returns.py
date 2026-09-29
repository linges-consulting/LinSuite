"""S1(+S5): retail returns (#76) and retail money on the shared payment ledger, over a real
PostgreSQL.

Reuses `tests/test_retail_sales.py`'s `claimed_instance` (imported — autouse applies here too)
and its sale/stock helpers.
"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.db import session_scope
from tests.test_bill_review import add_front_desk_account, as_admin, as_staff, make_customer
from tests.test_retail_sales import (  # noqa: F401 — autouse fixture
    RETAIL_INVOICES,
    add_line,
    claimed_instance,
    issue,
    movements,
    new_variant,
    quantity_on_hand,
    start_sale,
)


async def sell(client, quantity: int = 2, price_cents: int = 1500, **sale) -> tuple[dict, dict]:
    """An issued retail invoice for `quantity` units of a fresh variant, paid in full."""
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10, price_cents=price_cents)
    draft = await start_sale(client, **sale)
    await add_line(client, draft["id"], variant["id"], quantity=quantity)
    resp = await issue(client, draft["id"])
    assert resp.status_code == 201, resp.text
    invoice = resp.json()
    paid = await client.post(
        f"{RETAIL_INVOICES}/{invoice['id']}/payments",
        json={"payer_type": "client", "method": "cash", "amount_cents": price_cents * quantity},
    )
    assert paid.status_code == 201, paid.text
    return invoice, variant


def returns_url(invoice_id: str) -> str:
    return f"{RETAIL_INVOICES}/{invoice_id}/returns"


def a_return(invoice: dict, quantity: int = 1, restock: bool = True, **extra) -> dict:
    line_id = invoice["lines"][0]["id"]
    return {
        "reason": "Changed mind",
        "lines": [{"retail_invoice_line_id": line_id, "quantity": quantity, "restock": restock}],
        **extra,
    }


async def read(client, invoice_id: str) -> dict:
    resp = await client.get(f"{RETAIL_INVOICES}/{invoice_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- the shared ledger ----------------------------------------------------------------------------


async def test_retail_payments_land_on_the_shared_ledger(client):
    invoice, _ = await sell(client, quantity=2, price_cents=1500)

    body = await read(client, invoice["id"])
    listed = (await client.get(f"{RETAIL_INVOICES}/{invoice['id']}/payments")).json()

    assert (body["outstanding_cents"], body["refunded_cents"]) == (0, 0)
    [payment] = listed["payments"]
    assert payment["retail_invoice_id"] == invoice["id"]
    assert payment["invoice_id"] is None


async def test_a_ledger_row_references_exactly_one_invoice_kind(client):
    invoice, _ = await sell(client)
    async with session_scope() as db:
        with pytest.raises(DBAPIError):
            await db.execute(
                text(
                    "INSERT INTO invoice_refunds (amount_cents, reason, approved_by) "
                    "SELECT 1, 'r', issued_by FROM retail_invoices WHERE id = :id"
                ),
                {"id": invoice["id"]},
            )
        await db.rollback()


# --- the two independent choices ------------------------------------------------------------------


async def test_a_suitable_return_with_refund_restocks_and_refunds(client):
    invoice, variant = await sell(client, quantity=2, price_cents=1500)

    resp = await client.post(returns_url(invoice["id"]), json=a_return(invoice, refund_cents=1500))

    assert resp.status_code == 201, resp.text
    assert resp.json()["refund_id"] is not None
    assert resp.json()["lines"][0]["restocked"] is True
    assert await quantity_on_hand(variant["id"]) == 9
    assert [m["kind"] for m in await movements(variant["id"])][-1] == "return"
    after = await read(client, invoice["id"])
    assert (after["refunded_cents"], after["outstanding_cents"]) == (1500, 1500)
    refunds = (await client.get(f"{RETAIL_INVOICES}/{invoice['id']}/refunds")).json()["refunds"]
    assert [r["retail_invoice_id"] for r in refunds] == [invoice["id"]]


async def test_an_opened_item_is_refunded_without_restocking(client):
    invoice, variant = await sell(client)

    resp = await client.post(
        returns_url(invoice["id"]), json=a_return(invoice, restock=False, refund_cents=1500)
    )

    assert resp.status_code == 201, resp.text
    assert await quantity_on_hand(variant["id"]) == 8
    assert "return" not in [m["kind"] for m in await movements(variant["id"])]
    assert (await read(client, invoice["id"]))["refunded_cents"] == 1500


async def test_a_restock_never_implies_a_refund(client):
    invoice, variant = await sell(client)

    resp = await client.post(returns_url(invoice["id"]), json=a_return(invoice, quantity=2))

    assert resp.status_code == 201, resp.text
    assert resp.json()["refund_id"] is None
    assert await quantity_on_hand(variant["id"]) == 10
    assert (await read(client, invoice["id"]))["refunded_cents"] == 0


# --- the money side goes through #67's admin-approved, capped refund ------------------------------


async def test_the_refund_is_capped_by_money_received(client):
    invoice, variant = await sell(client, quantity=2, price_cents=1500)

    resp = await client.post(returns_url(invoice["id"]), json=a_return(invoice, refund_cents=3001))

    assert resp.status_code == 422, resp.text
    assert await quantity_on_hand(variant["id"]) == 8  # nothing restocked either


async def test_a_return_needs_billing_manage_in_admin_mode(client):
    invoice, _ = await sell(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)

    resp = await client.post(returns_url(invoice["id"]), json=a_return(invoice))

    assert resp.status_code == 403, resp.text


# --- the invoice read exposes what remains returnable (#105) --------------------------------------


async def test_the_invoice_read_exposes_returned_quantity_per_line(client):
    invoice, _ = await sell(client, quantity=3)

    resp = await client.post(returns_url(invoice["id"]), json=a_return(invoice, quantity=2))
    assert resp.status_code == 201, resp.text

    after = await read(client, invoice["id"])
    assert after["lines"][0]["returned_quantity"] == 2
    assert after["lines"][0]["quantity"] == 3


# --- never over-returning a line ------------------------------------------------------------------


async def test_a_line_cannot_be_returned_past_what_was_sold(client):
    invoice, variant = await sell(client, quantity=2)

    first = await client.post(returns_url(invoice["id"]), json=a_return(invoice, restock=False))
    second = await client.post(returns_url(invoice["id"]), json=a_return(invoice, quantity=2))
    third = await client.post(returns_url(invoice["id"]), json=a_return(invoice))

    assert first.status_code == 201, first.text
    assert second.status_code == 422, second.text
    assert third.status_code == 201, third.text
    assert await quantity_on_hand(variant["id"]) == 9


async def test_two_concurrent_returns_of_the_last_unit_land_once(client, monkeypatch):
    invoice, variant = await sell(client, quantity=1)

    from billing import retail_sales as retail_sales_mod
    from main import app as main_app

    real = retail_sales_mod.record_movement

    async def slow(*args, **kwargs):
        result = await real(*args, **kwargs)
        await asyncio.sleep(0.25)
        return result

    # Separate Admin Mode sessions, so nothing but the invoice row lock serializes them.
    clients = [
        AsyncClient(transport=ASGITransport(app=main_app), base_url="http://test") for _ in "ab"
    ]
    try:
        for c in clients:
            await as_admin(c)
        monkeypatch.setattr(retail_sales_mod, "record_movement", slow)
        results = await asyncio.gather(
            *(c.post(returns_url(invoice["id"]), json=a_return(invoice)) for c in clients)
        )
    finally:
        for c in clients:
            await c.aclose()

    assert sorted(r.status_code for r in results) == [201, 422], [r.text for r in results]
    assert await quantity_on_hand(variant["id"]) == 10
    assert [m["kind"] for m in await movements(variant["id"])].count("return") == 1


# --- anonymous and client-linked sales behave the same --------------------------------------------


@pytest.mark.parametrize("linked", [False, True])
async def test_a_return_works_for_anonymous_and_linked_sales(client, linked):
    await as_admin(client)
    sale = {"customer_id": await make_customer(client)} if linked else {}
    invoice, variant = await sell(client, **sale)
    assert (invoice["customer_id"] is not None) is linked

    resp = await client.post(returns_url(invoice["id"]), json=a_return(invoice, refund_cents=500))

    assert resp.status_code == 201, resp.text
    assert await quantity_on_hand(variant["id"]) == 9
    assert (await read(client, invoice["id"]))["refunded_cents"] == 500


# --- append-only ----------------------------------------------------------------------------------


@pytest.mark.parametrize("table", ["retail_returns", "retail_return_lines"])
async def test_returns_are_append_only(client, table):
    invoice, _ = await sell(client)
    await client.post(returns_url(invoice["id"]), json=a_return(invoice))

    async with session_scope() as db:
        for statement in (f"UPDATE {table} SET id = id", f"DELETE FROM {table}"):
            with pytest.raises(DBAPIError) as refused:
                await db.execute(text(statement))
            await db.rollback()
            assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value

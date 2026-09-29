"""S1(+S5): M4 review T2 on retail invoices — the balance exception (R8), a concurrent double
issue answered 422 not 500 (R11), and cancel & replace (R15), mirroring #68: the voidable
issued -> cancelled transition, a replacement draft, money held then carried, and stock moved
only for real changes (cancellation alone moves none).

Reuses `tests/test_retail_sales.py`'s autouse `claimed_instance` and
`tests/test_retail_returns.py`'s `sell` helper.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.db import session_scope
from tests.test_bill_review import add_front_desk_account, as_admin, as_staff
from tests.test_ledger_guards import race
from tests.test_retail_returns import a_return, read, returns_url, sell
from tests.test_retail_sales import (  # noqa: F401 — autouse fixture
    RETAIL_INVOICES,
    RETAIL_SALES,
    add_line,
    claimed_instance,
    issue,
    movements,
    new_variant,
    quantity_on_hand,
    start_sale,
)


def cancel_url(invoice_id: str) -> str:
    return f"{RETAIL_INVOICES}/{invoice_id}/cancel"


def exceptions_url(invoice_id: str) -> str:
    return f"{RETAIL_INVOICES}/{invoice_id}/balance-exceptions"


async def cancel(client, invoice_id: str, reason: str = "Wrong item rung up") -> dict:
    resp = await client.post(cancel_url(invoice_id), json={"reason": reason})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def drop_first_line(client, sale_id: str) -> None:
    line_id = (await client.get(f"{RETAIL_SALES}/{sale_id}")).json()["lines"][0]["id"]
    resp = await client.delete(
        f"{RETAIL_SALES}/{sale_id}/lines/{line_id}", headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 200, resp.text


async def scalar(sql: str, **params):
    async with session_scope() as db:
        return await db.scalar(text(sql), params)


def unique() -> dict:
    """A second variant in one test needs its own SKU and barcode."""
    n = uuid.uuid4().int % 10**12
    return {"sku": f"SKU-{n}", "barcode": f"9{n:012d}"}


async def unpaid_sale(client, quantity: int = 2, price_cents: int = 1500) -> tuple[dict, dict]:
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10, price_cents=price_cents, **unique())
    draft = await start_sale(client)
    await add_line(client, draft["id"], variant["id"], quantity=quantity)
    resp = await issue(client, draft["id"])
    assert resp.status_code == 201, resp.text
    return resp.json(), variant


# --- R8: the outstanding-balance exception for retail ---------------------------------------------


async def test_an_admin_authorizes_a_retail_balance_and_checkout_completes(client):
    invoice, _ = await unpaid_sale(client)
    assert (await read(client, invoice["id"]))["checkout_complete"] is False

    resp = await client.post(exceptions_url(invoice["id"]), json={"reason": "Pays Friday"})

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["retail_invoice_id"], body["invoice_id"]) == (invoice["id"], None)
    assert body["outstanding_cents_at_authorization"] == 3000
    after = await read(client, invoice["id"])
    assert (after["checkout_complete"], after["outstanding_cents"]) == (True, 3000)
    listed = (await client.get(exceptions_url(invoice["id"]))).json()["exceptions"]
    assert [e["id"] for e in listed] == [body["id"]]


async def test_a_retail_exception_is_refused_when_nothing_is_owed_or_by_staff(client):
    invoice, _ = await sell(client)  # paid in full
    paid = await client.post(exceptions_url(invoice["id"]), json={"reason": "r"})
    assert paid.status_code == 422, paid.text

    owing, _ = await unpaid_sale(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)
    staff = await client.post(exceptions_url(owing["id"]), json={"reason": "r"})
    assert staff.status_code == 403, staff.text
    assert await scalar("SELECT count(*) FROM invoice_balance_authorizations") == 0


# --- R11: two concurrent issues of one retail draft -----------------------------------------------


async def test_a_concurrent_double_retail_issue_gets_one_invoice_and_a_422(client, monkeypatch):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10)
    draft = await start_sale(client)
    await add_line(client, draft["id"], variant["id"], quantity=2)
    from billing import retail_sales as retail_sales_mod

    results = await race(
        monkeypatch,
        retail_sales_mod,
        "_load_staff",  # runs after the status check, under the sale lock
        lambda c: issue(c, draft["id"]),
        lambda c: issue(c, draft["id"]),
    )

    assert sorted(r.status_code for r in results) == [201, 422], [r.text for r in results]
    assert await scalar("SELECT count(*) FROM retail_invoices") == 1
    assert await quantity_on_hand(variant["id"]) == 8


# --- R15: cancel ----------------------------------------------------------------------------------


async def test_cancel_retains_the_original_and_opens_a_replacement_draft(client):
    invoice, variant = await sell(client, quantity=2, price_cents=1500)
    before = await movements(variant["id"])

    body = await cancel(client, invoice["id"])

    cancelled = body["invoice"]
    assert cancelled["status"] == "cancelled"
    assert cancelled["invoice_number"] == invoice["invoice_number"]
    assert cancelled["cancel_reason"] == "Wrong item rung up"
    assert cancelled["lines"] == invoice["lines"]
    # Money stays on the original, shown explicitly — never a negative balance (R12).
    assert (cancelled["outstanding_cents"], cancelled["held_credit_cents"]) == (0, 3000)
    # Cancellation alone moves no stock: the client still has the goods.
    assert await movements(variant["id"]) == before
    assert await quantity_on_hand(variant["id"]) == 8

    draft = (await client.get(f"{RETAIL_SALES}/{body['replacement_sale_id']}")).json()
    assert draft["status"] == "draft"
    assert draft["replaces_retail_invoice_id"] == invoice["id"]
    assert draft["sold_by_staff_id"] == invoice["sold_by_staff_id"]
    assert [
        (ln["variant_id"], ln["quantity"], ln["unit_price_cents"]) for ln in draft["lines"]
    ] == [(variant["id"], 2, 1500)]


async def test_cancel_requires_a_reason_and_billing_view(client):
    invoice, _ = await sell(client)
    assert (await client.post(cancel_url(invoice["id"]), json={"reason": ""})).status_code == 422
    client.cookies.clear()
    assert (await client.post(cancel_url(invoice["id"]), json={"reason": "x"})).status_code in (
        401,
        403,
    )


async def test_a_cancelled_retail_invoice_refuses_payments_returns_and_exceptions(client):
    invoice, _ = await unpaid_sale(client)
    await cancel(client, invoice["id"])

    for url, body in (
        (
            f"{RETAIL_INVOICES}/{invoice['id']}/payments",
            {"payer_type": "client", "method": "cash", "amount_cents": 100},
        ),
        (returns_url(invoice["id"]), a_return(invoice)),
        (exceptions_url(invoice["id"]), {"reason": "r"}),
    ):
        resp = await client.post(url, json=body)
        assert resp.status_code == 422, (url, resp.text)


async def test_a_retried_cancel_returns_the_same_replacement_once(client):
    invoice, _ = await sell(client)

    first = await cancel(client, invoice["id"])
    again = await cancel(client, invoice["id"], reason="different reason")

    assert again["replacement_sale_id"] == first["replacement_sale_id"]
    assert again["invoice"]["cancel_reason"] == "Wrong item rung up"
    assert await scalar("SELECT count(*) FROM retail_sales") == 2
    event = "SELECT count(*) FROM audit_events WHERE event_type = 'retail_invoice.cancelled'"
    assert await scalar(event) == 1


async def test_the_database_refuses_a_payment_row_on_a_cancelled_retail_invoice(client):
    invoice, _ = await sell(client)
    await cancel(client, invoice["id"])

    async with session_scope() as db:
        with pytest.raises(DBAPIError, match="not issued"):
            await db.execute(
                text(
                    "INSERT INTO invoice_payments "
                    "(retail_invoice_id, payer_type, method, amount_cents, collected_by) "
                    "SELECT id, 'client', 'cash', 100, issued_by FROM retail_invoices "
                    "WHERE id = :id"
                ),
                {"id": invoice["id"]},
            )
        await db.rollback()


# --- R15: replace ---------------------------------------------------------------------------------


async def test_an_unchanged_replacement_moves_no_stock_and_carries_the_money(client):
    invoice, variant = await sell(client, quantity=2, price_cents=1500)
    before = await movements(variant["id"])
    sale_id = (await cancel(client, invoice["id"]))["replacement_sale_id"]

    resp = await issue(client, sale_id)

    assert resp.status_code == 201, resp.text
    replacement = resp.json()
    assert replacement["replaces_invoice_id"] == invoice["id"]
    assert replacement["invoice_number"] > invoice["invoice_number"]
    assert (replacement["outstanding_cents"], replacement["checkout_complete"]) == (0, True)
    assert await movements(variant["id"]) == before
    assert await quantity_on_hand(variant["id"]) == 8
    original = await read(client, invoice["id"])
    assert original["replaced_by_invoice_id"] == replacement["id"]
    assert original["held_credit_cents"] == 0
    history = (await client.get(f"{RETAIL_INVOICES}/{replacement['id']}/payments")).json()
    assert history["payments"] == []
    [transfer] = history["transfers"]
    assert (transfer["from_invoice_id"], transfer["received_cents"]) == (invoice["id"], 3000)


async def test_a_dropped_item_returns_to_stock_and_an_added_one_is_sold(client):
    invoice, kept = await sell(client, quantity=2, price_cents=1500)
    added = await new_variant(client, quantity_on_hand=5, price_cents=500, **unique())
    sale_id = (await cancel(client, invoice["id"]))["replacement_sale_id"]
    await drop_first_line(client, sale_id)
    await add_line(client, sale_id, kept["id"], quantity=1)
    await add_line(client, sale_id, added["id"], quantity=3)

    resp = await issue(client, sale_id)

    assert resp.status_code == 201, resp.text
    assert await quantity_on_hand(kept["id"]) == 9  # one of the two came back
    assert (await movements(kept["id"]))[-1] == {"kind": "return", "quantity_delta": 1}
    assert await quantity_on_hand(added["id"]) == 2
    assert await movements(added["id"]) == [{"kind": "sale", "quantity_delta": -3}]
    # 3000 carried against a 1500 + 1500 replacement.
    assert resp.json()["outstanding_cents"] == 0


async def test_a_price_increase_is_collected_as_an_ordinary_payment(client):
    invoice, variant = await sell(client, quantity=2, price_cents=1500)
    sale_id = (await cancel(client, invoice["id"]))["replacement_sale_id"]
    await add_line(client, sale_id, variant["id"], quantity=1)

    replacement = (await issue(client, sale_id)).json()

    assert replacement["outstanding_cents"] == 1500
    assert await quantity_on_hand(variant["id"]) == 7


async def test_units_already_returned_are_not_carried_into_the_replacement(client):
    invoice, variant = await sell(client, quantity=2, price_cents=1500)
    returned = await client.post(returns_url(invoice["id"]), json=a_return(invoice, quantity=1))
    assert returned.status_code == 201, returned.text
    before = await movements(variant["id"])

    sale_id = (await cancel(client, invoice["id"]))["replacement_sale_id"]
    draft = (await client.get(f"{RETAIL_SALES}/{sale_id}")).json()
    assert [ln["quantity"] for ln in draft["lines"]] == [1]
    assert (await issue(client, sale_id)).status_code == 201

    assert await movements(variant["id"]) == before
    assert await quantity_on_hand(variant["id"]) == 9


async def test_refunds_go_to_the_live_end_of_a_retail_lineage(client):
    invoice, _ = await sell(client, quantity=2, price_cents=1500)
    sale_id = (await cancel(client, invoice["id"]))["replacement_sale_id"]
    await drop_first_line(client, sale_id)
    variant = await new_variant(client, quantity_on_hand=5, price_cents=1000, **unique())
    await add_line(client, sale_id, variant["id"], quantity=1)
    replacement = (await issue(client, sale_id)).json()
    assert replacement["outstanding_cents"] == -2000

    refunds = f"{RETAIL_INVOICES}/{{}}/refunds"
    on_original = await client.post(
        refunds.format(invoice["id"]), json={"amount_cents": 2000, "reason": "Credit"}
    )
    on_replacement = await client.post(
        refunds.format(replacement["id"]), json={"amount_cents": 2000, "reason": "Credit"}
    )

    assert on_original.status_code == 422, on_original.text
    assert on_replacement.status_code == 201, on_replacement.text
    assert (await read(client, replacement["id"]))["outstanding_cents"] == 0

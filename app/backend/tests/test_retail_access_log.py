"""S1: opening a client-linked retail invoice is an audited read (ADR-0002, review R30) — the
retail half of `test_financial_access_log.py`, here for `test_retail_sales.py`'s fixture."""

import pytest

from tests.test_bill_review import as_admin, make_customer
from tests.test_financial_access_log import access_rows
from tests.test_retail_sales import (  # noqa: F401 — claimed_instance is an autouse fixture
    RETAIL_INVOICES,
    add_line,
    claimed_instance,
    issue,
    new_variant,
    start_sale,
)


async def issued(client, customer_id: str | None) -> str:
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=5)
    sale = await start_sale(client, **({"customer_id": customer_id} if customer_id else {}))
    await add_line(client, sale["id"], variant["id"])
    resp = await issue(client, sale["id"])
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.parametrize(
    ("suffix", "resource_type"),
    [
        ("", "retail_invoice"),
        ("/payments", "retail_invoice_payments"),
        ("/refunds", "retail_invoice_refunds"),
        ("/balance-exceptions", "retail_invoice_balance_exceptions"),
    ],
)
async def test_opening_a_linked_retail_invoice_logs_one_row_for_its_client(
    client, suffix, resource_type
):
    await as_admin(client)
    customer_id = await make_customer(client)
    invoice_id = await issued(client, customer_id)

    resp = await client.get(f"{RETAIL_INVOICES}/{invoice_id}{suffix}")

    assert resp.status_code == 200, resp.text
    assert await access_rows(resource_type, invoice_id) == [customer_id]


async def test_an_anonymous_retail_invoice_names_no_client_and_logs_nothing(client):
    invoice_id = await issued(client, None)

    resp = await client.get(f"{RETAIL_INVOICES}/{invoice_id}")

    assert resp.status_code == 200, resp.text
    assert await access_rows("retail_invoice", invoice_id) == []


async def test_one_clients_retail_history_logs_but_the_list_render_does_not(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    await issued(client, customer_id)

    everyone = await client.get(RETAIL_INVOICES)
    assert everyone.status_code == 200, everyone.text
    assert await access_rows("retail_invoice_history", customer_id) == []

    theirs = await client.get(RETAIL_INVOICES, params={"customer_id": customer_id})
    assert theirs.status_code == 200, theirs.text
    assert await access_rows("retail_invoice_history", customer_id) == [customer_id]

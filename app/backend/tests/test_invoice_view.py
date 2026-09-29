"""S1: the names the invoice view (#102) needs on top of what #99's list already joins in.

`GET /invoices/{id}` and `GET /retail-invoices/{id}` used to carry only ids for a line's
service/staff (service side) or variant (retail side), and no customer name at all — fine for
#65/#75's own issue-time assertions, but useless for a screen that has to explain a line to a
client without a second, admin-only catalog fetch a Staff-Mode/`billing.view` account cannot
make (`lib/nav.ts`'s own `/api/admin/services`/`/api/admin/staff` gate). This file proves the
joined names land on both invoice kinds, and that an anonymous retail sale still reads as
`None` (the client-side "Walk-in" label) rather than a fabricated name.

Reuses `test_retail_sales.py`'s `claimed_instance` fixture (wipes both invoice kinds) and
`test_bill_review.py`'s `complete_a_visit`/customer helpers — the established cross-file
import pattern `test_invoice_list.py` already uses.
"""

from tests.test_bill_review import CUSTOMER, STAFF, as_admin, complete_a_visit, make_customer
from tests.test_retail_sales import (  # noqa: F401 — claimed_instance is an autouse fixture
    add_line,
    claimed_instance,
    issue,
    new_variant,
    start_sale,
)

INVOICES = "/api/invoices"
RETAIL_INVOICES = "/api/retail-invoices"


async def test_get_invoice_carries_customer_and_line_names(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    issued = await client.post(f"/api/bills/{bill_id}/issue", json={})
    assert issued.status_code == 201, issued.text
    invoice_id = issued.json()["id"]

    resp = await client.get(f"{INVOICES}/{invoice_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["customer_name"] == f"{CUSTOMER['first_name']} {CUSTOMER['last_name']}"
    assert len(body["lines"]) == 1
    line = body["lines"][0]
    assert line["service_name"] == "Swedish Massage"

    roster = await client.get(STAFF)
    assert roster.status_code == 200, roster.text
    expected_staff_name = next(
        row["display_name"] for row in roster.json()["staff"] if row["id"] == line["staff_id"]
    )
    assert line["staff_name"] == expected_staff_name


async def test_get_retail_invoice_carries_customer_and_variant_names(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    variant = await new_variant(client, name="Shampoo 250ml", quantity_on_hand=10)
    sale = await start_sale(client, customer_id=customer_id)
    await add_line(client, sale["id"], variant["id"])
    issued = await issue(client, sale["id"])
    assert issued.status_code == 201, issued.text
    invoice_id = issued.json()["id"]

    resp = await client.get(f"{RETAIL_INVOICES}/{invoice_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["customer_name"] == f"{CUSTOMER['first_name']} {CUSTOMER['last_name']}"
    assert len(body["lines"]) == 1
    assert body["lines"][0]["variant_name"] == "Shampoo 250ml"


async def test_an_anonymous_retail_invoice_has_no_customer_name(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10)
    sale = await start_sale(client)
    await add_line(client, sale["id"], variant["id"])
    issued = await issue(client, sale["id"])
    assert issued.status_code == 201, issued.text
    invoice_id = issued.json()["id"]

    resp = await client.get(f"{RETAIL_INVOICES}/{invoice_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["customer_name"] is None

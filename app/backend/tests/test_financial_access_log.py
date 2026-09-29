"""S1: opening a client's financial record is an audited read (ADR-0002, review R30).

Every record-open of client-linked billing data writes exactly one `audit_access_log` row
naming that client and the record; the unfiltered invoice list is a list render and writes
none (ADR-0002 §4), while the whole package-liability report logs one read per client it
names (owner decision). The routes are registered in
`test_access_log.py::LOGGED`; this file proves what each one writes. Retail equivalents live
in `test_retail_access_log.py`, beside the fixture that can wipe retail tables.
"""

import pytest
from sqlalchemy import text

from core.db import session_scope
from tests.test_bill_review import complete_a_visit
from tests.test_credit_redemption import (  # noqa: F401 — claimed_instance is an autouse fixture
    claimed_instance,
    world,
)


async def access_rows(resource_type: str, resource_id: str) -> list[str]:
    """The `customer_id` of every row for this one resource — ids are fresh per test, so no
    wipe of the (append-only, partitioned) log is needed."""
    async with session_scope() as db:
        rows = await db.execute(
            text(
                "SELECT customer_id FROM audit_access_log "
                "WHERE resource_type = :t AND resource_id = :r AND action = 'view'"
            ),
            {"t": resource_type, "r": resource_id},
        )
        return [str(r[0]) for r in rows]


@pytest.mark.parametrize(
    ("suffix", "resource_type"),
    [
        ("", "invoice"),
        ("/payments", "invoice_payments"),
        ("/refunds", "invoice_refunds"),
        ("/balance-exceptions", "invoice_balance_exceptions"),
    ],
)
async def test_opening_an_invoice_or_its_ledger_logs_one_row_for_its_client(
    client, suffix, resource_type
):
    w = await world(client)
    invoice_id = w.purchase["invoice_id"]

    resp = await client.get(f"/api/invoices/{invoice_id}{suffix}")

    assert resp.status_code == 200, resp.text
    assert await access_rows(resource_type, invoice_id) == [w.customer_id]


async def test_opening_a_package_purchase_logs_one_row(client):
    w = await world(client)

    resp = await client.get(f"/api/packages/purchases/{w.purchase['id']}")

    assert resp.status_code == 200, resp.text
    assert await access_rows("package_purchase", w.purchase["id"]) == [w.customer_id]


async def test_opening_a_clients_package_purchases_logs_one_row_per_open(client):
    # #108: the client Packages tab — opening it is the same kind of financial-history read as
    # an invoice's, logged even though nothing in the response is in `PHI_FIELDS`.
    w = await world(client)

    for _ in range(2):
        resp = await client.get(f"/api/customers/{w.customer_id}/package-purchases")
        assert resp.status_code == 200, resp.text

    assert await access_rows("package_purchases", w.customer_id) == [w.customer_id, w.customer_id]


async def test_opening_a_service_bill_logs_one_row(client):
    w = await world(client)  # signs in as admin; the visit below is a second, billable one
    bill_id, _ = await complete_a_visit(client, service_name="Deep Tissue")
    async with session_scope() as db:
        customer_id = str(
            await db.scalar(
                text("SELECT customer_id FROM service_bills WHERE id = :b"), {"b": bill_id}
            )
        )
    assert customer_id != w.customer_id

    resp = await client.get(f"/api/bills/{bill_id}")

    assert resp.status_code == 200, resp.text
    assert await access_rows("service_bill", bill_id) == [customer_id]


async def test_one_clients_invoice_history_logs_but_the_list_render_does_not(client):
    w = await world(client)

    everyone = await client.get("/api/invoices")
    assert everyone.status_code == 200, everyone.text
    assert await access_rows("invoice_history", w.customer_id) == []

    theirs = await client.get("/api/invoices", params={"customer_id": w.customer_id})
    assert theirs.status_code == 200, theirs.text
    assert await access_rows("invoice_history", w.customer_id) == [w.customer_id]


async def test_the_liability_report_logs_one_read_per_client_it_names(client):
    # Owner decision: unlike a list render, the whole report names every client holding
    # credits, so each one shown gets one audited read; filtered, exactly the one client.
    w = await world(client)

    everyone = await client.get("/api/admin/reports/package-liability")
    assert everyone.status_code == 200, everyone.text
    named = {c["customer_id"] for c in everyone.json()["customers"]}
    assert named == {w.customer_id}
    assert await access_rows("package_liability", w.customer_id) == [w.customer_id]

    theirs = await client.get(
        "/api/admin/reports/package-liability", params={"customer_id": w.customer_id}
    )
    assert theirs.status_code == 200, theirs.text
    assert await access_rows("package_liability", w.customer_id) == [w.customer_id] * 2


async def test_an_unknown_invoice_404s_and_logs_nothing_there_is_no_client_to_name(client):
    await world(client)
    missing = "00000000-0000-4000-8000-000000000000"

    resp = await client.get(f"/api/invoices/{missing}")

    assert resp.status_code == 404, resp.text
    assert await access_rows("invoice", missing) == []


async def test_a_refused_read_is_not_an_access(client):
    w = await world(client)
    invoice_id = w.purchase["invoice_id"]
    client.cookies.clear()

    resp = await client.get(f"/api/invoices/{invoice_id}")

    assert resp.status_code == 401, resp.text
    assert await access_rows("invoice", invoice_id) == []

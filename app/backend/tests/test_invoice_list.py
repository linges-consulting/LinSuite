"""S1: the Invoices list (#99, spec #95 stories 6-14) — date default, date range, status
derivation and pagination totals, for both the service and retail invoice lists.

The client filter and its access logging are already proven in `test_financial_access_log.py`
(service) and `test_retail_access_log.py` (retail) and are unchanged by this ticket; this file
is about the mechanics `billing/bill_review.py::invoice_list_window` and `billing/payments.py::
invoice_list_status` add on top of them. Reuses `test_retail_sales.py`'s `claimed_instance`
fixture (it wipes both the service and retail invoice tables) and `test_bill_review.py`'s
`complete_a_visit` helper — the established cross-file test-import pattern.
"""

import uuid
from datetime import date, timedelta

from sqlalchemy import text

from tests.conftest import get_owner_engine
from tests.test_bill_review import as_admin, complete_a_visit
from tests.test_retail_sales import (  # noqa: F401 — claimed_instance is an autouse fixture
    RETAIL_INVOICES,
    add_line,
    claimed_instance,
    issue,
    new_variant,
    start_sale,
)

INVOICES = "/api/invoices"


async def _issue_service_invoice(client, **kwargs) -> tuple[str, int]:
    bill_id, _ = await complete_a_visit(client, **kwargs)
    resp = await client.post(f"/api/bills/{bill_id}/issue", json={})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["id"], body["grand_total_cents"]


async def _issue_retail_invoice(client) -> tuple[str, int]:
    unique = uuid.uuid4().hex[:8]
    barcode = f"{int(unique, 16) % 10**13:013d}"
    variant = await new_variant(client, sku=f"SKU-{unique}", barcode=barcode, quantity_on_hand=100)
    sale = await start_sale(client)
    await add_line(client, sale["id"], variant["id"])
    resp = await issue(client, sale["id"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["id"], body["grand_total_cents"]


async def _backdate(table: str, invoice_id: str, days: int) -> None:
    """`invoices_voidable_guard`/`retail_invoices_voidable_guard` block the app role from
    touching `issued_at` at all (only the owner, or the exact issued -> cancelled transition,
    may write these rows) — the owner engine is the one way a test can move the clock back."""
    async with get_owner_engine().begin() as owner:
        await owner.execute(
            text(  # noqa: S608
                f"UPDATE {table} SET issued_at = now() - make_interval(days => :d) WHERE id = :id"
            ),
            {"d": days, "id": invoice_id},
        )


async def _pay(client, path: str, amount_cents: int) -> None:
    resp = await client.post(
        path,
        json={
            "payer_type": "client",
            "method": "cash",
            "amount_cents": amount_cents,
            "status": "received",
        },
    )
    assert resp.status_code == 201, resp.text


async def _cancel(client, path: str) -> None:
    resp = await client.post(path, json={"reason": "test cancel"})
    assert resp.status_code == 200, resp.text


# --- service invoices --------------------------------------------------------------------------


async def test_service_list_defaults_to_the_last_30_days(client):
    await as_admin(client)
    old_id, _ = await _issue_service_invoice(client, service_name="Old Visit")
    recent_id, _ = await _issue_service_invoice(client, service_name="Recent Visit")
    await _backdate("invoices", old_id, 40)

    resp = await client.get(INVOICES)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {i["id"] for i in body["invoices"]} == {recent_id}
    assert body["total"] == 1
    assert date.fromisoformat(body["to"]) - date.fromisoformat(body["from"]) == timedelta(days=30)


async def test_service_list_date_range_reaches_further_back(client):
    await as_admin(client)
    old_id, _ = await _issue_service_invoice(client, service_name="Old Visit")
    await _backdate("invoices", old_id, 40)
    from_date = (date.today() - timedelta(days=45)).isoformat()

    resp = await client.get(INVOICES, params={"from": from_date})

    assert resp.status_code == 200, resp.text
    assert old_id in {i["id"] for i in resp.json()["invoices"]}


async def test_service_list_status_derives_from_the_balance(client):
    await as_admin(client)
    paid_id, total = await _issue_service_invoice(client, service_name="Paid Visit")
    await _pay(client, f"{INVOICES}/{paid_id}/payments", total)
    outstanding_id, _ = await _issue_service_invoice(client, service_name="Outstanding Visit")
    cancelled_id, _ = await _issue_service_invoice(client, service_name="Cancelled Visit")
    await _cancel(client, f"{INVOICES}/{cancelled_id}/cancel")

    resp = await client.get(INVOICES)

    assert resp.status_code == 200, resp.text
    by_id = {i["id"]: i["list_status"] for i in resp.json()["invoices"]}
    assert by_id[paid_id] == "paid"
    assert by_id[outstanding_id] == "outstanding"
    assert by_id[cancelled_id] == "cancelled"

    for wanted in ("outstanding", "paid", "cancelled"):
        filtered = await client.get(INVOICES, params={"status": wanted})
        assert filtered.status_code == 200, filtered.text
        statuses = {i["list_status"] for i in filtered.json()["invoices"]}
        assert statuses <= {wanted}


async def test_service_list_paginates_with_a_total(client):
    await as_admin(client)
    ids = [(await _issue_service_invoice(client, service_name=f"Visit {i}"))[0] for i in range(3)]

    first = await client.get(INVOICES, params={"page": 1, "page_size": 2})
    second = await client.get(INVOICES, params={"page": 2, "page_size": 2})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["total"] == 3
    assert second.json()["total"] == 3
    assert len(first.json()["invoices"]) == 2
    assert len(second.json()["invoices"]) == 1
    seen = {i["id"] for i in first.json()["invoices"]}
    seen |= {i["id"] for i in second.json()["invoices"]}
    assert seen == set(ids)


# --- retail invoices, the same shape -------------------------------------------------------------


async def test_retail_list_defaults_to_the_last_30_days(client):
    await as_admin(client)
    old_id, _ = await _issue_retail_invoice(client)
    recent_id, _ = await _issue_retail_invoice(client)
    await _backdate("retail_invoices", old_id, 40)

    resp = await client.get(RETAIL_INVOICES)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {i["id"] for i in body["retail_invoices"]} == {recent_id}
    assert body["total"] == 1


async def test_retail_list_date_range_reaches_further_back(client):
    await as_admin(client)
    old_id, _ = await _issue_retail_invoice(client)
    await _backdate("retail_invoices", old_id, 40)

    resp = await client.get(
        RETAIL_INVOICES, params={"from": (date.today() - timedelta(days=45)).isoformat()}
    )

    assert resp.status_code == 200, resp.text
    assert old_id in {i["id"] for i in resp.json()["retail_invoices"]}


async def test_retail_list_status_derives_from_the_balance(client):
    await as_admin(client)
    paid_id, total = await _issue_retail_invoice(client)
    await _pay(client, f"{RETAIL_INVOICES}/{paid_id}/payments", total)
    outstanding_id, _ = await _issue_retail_invoice(client)
    cancelled_id, _ = await _issue_retail_invoice(client)
    await _cancel(client, f"{RETAIL_INVOICES}/{cancelled_id}/cancel")

    resp = await client.get(RETAIL_INVOICES)

    assert resp.status_code == 200, resp.text
    by_id = {i["id"]: i["list_status"] for i in resp.json()["retail_invoices"]}
    assert by_id[paid_id] == "paid"
    assert by_id[outstanding_id] == "outstanding"
    assert by_id[cancelled_id] == "cancelled"


async def test_retail_list_paginates_with_a_total(client):
    await as_admin(client)
    ids = [(await _issue_retail_invoice(client))[0] for _ in range(3)]

    first = await client.get(RETAIL_INVOICES, params={"page": 1, "page_size": 2})
    second = await client.get(RETAIL_INVOICES, params={"page": 2, "page_size": 2})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["total"] == 3
    assert second.json()["total"] == 3
    assert len(first.json()["retail_invoices"]) == 2
    assert len(second.json()["retail_invoices"]) == 1
    seen = {i["id"] for i in first.json()["retail_invoices"]} | {
        i["id"] for i in second.json()["retail_invoices"]
    }
    assert seen == set(ids)


async def test_a_to_date_before_from_is_refused(client):
    await as_admin(client)
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    resp = await client.get(INVOICES, params={"from": date.today().isoformat(), "to": yesterday})

    assert resp.status_code == 422, resp.text

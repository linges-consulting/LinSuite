"""S1: unused-package liability report (#74), over a real PostgreSQL. Its CSV export (#88,
delivers #81) is proven below too — through the shared mechanism of #86, the same as
`tests/test_commission_report.py` proves for `kind="commission"`."""

import csv
import io
import os
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from core.db import session_scope
from tests.test_bill_review import CUSTOMER, CUSTOMERS, EMAIL, PASSWORD
from tests.test_credit_redemption import (  # noqa: F401 — claimed_instance is an autouse fixture
    book,
    buy,
    claimed_instance,
    complete,
    world,
)
from tests.test_package_refund import refund

REPORT = "/api/admin/reports/package-liability"
EXPORTS = "/api/admin/reports/package-liability/exports"


async def report(client, **params) -> dict:
    resp = await client.get(REPORT, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def access_rows(customer_id: str) -> list[str]:
    async with session_scope() as db:
        rows = await db.execute(
            text(
                "SELECT customer_id FROM audit_access_log WHERE resource_type = "
                "'package_liability' AND customer_id = :c AND action = 'view'"
            ),
            {"c": customer_id},
        )
        return [str(r[0]) for r in rows]


async def as_owner(sql: str, **params) -> None:
    owner = create_async_engine(os.environ["DATABASE_URL_MIGRATE"])
    try:
        async with owner.begin() as conn:
            await conn.execute(text(sql), params)
    finally:
        await owner.dispose()


async def test_unused_credits_are_valued_at_the_frozen_allocation(client):
    w = await world(client, credits=10, price_cents=96000)
    assert (await complete(client, await book(client, w), w.purchase["id"])).status_code == 200
    # Live catalog prices move after the sale; the liability must not.
    async with session_scope() as db:
        await db.execute(text("UPDATE services SET price_cents = 50000"))
        await db.execute(text("UPDATE package_definitions SET price_cents = 1"))
        await db.commit()

    body = await report(client)

    [row] = body["rows"]
    assert row["package_purchase_id"] == w.purchase["id"]
    assert (row["credits_total"], row["credits_redeemed"], row["credits_remaining"]) == (10, 1, 9)
    assert row["unused_value_cents"] == 96000 - 9600
    assert body["customers"] == [
        {
            "customer_id": w.customer_id,
            "customer_name": row["customer_name"],
            "credits_remaining": 9,
            "unused_value_cents": 86400,
        }
    ]
    assert body["total_unused_value_cents"] == 86400


async def test_fully_redeemed_and_unpaid_packages_are_not_liabilities(client):
    w = await world(client, credits=1, price_cents=12000)
    assert (await complete(client, await book(client, w), w.purchase["id"])).status_code == 200
    await buy(client, w, credits=3, price_cents=30000, pay=False)

    assert (await report(client))["rows"] == []


async def test_a_standard_refund_removes_the_credits(client):
    w = await world(client)
    assert (await refund(client, w.purchase["id"])).status_code == 201

    assert (await report(client))["rows"] == []


async def test_goodwill_refund_that_keeps_credits_still_shows_them(client):
    w = await world(client, credits=10, price_cents=96000)
    assert (await complete(client, await book(client, w), w.purchase["id"])).status_code == 200
    kept = await refund(
        client,
        w.purchase["id"],
        exception={
            "amount_cents": 20000,
            "cancel_remaining_credits": False,
            "reverse_commission": False,
        },
    )
    assert kept.status_code == 201, kept.text
    assert kept.json()["invoice"]["status"] == "cancelled"

    [row] = (await report(client))["rows"]

    assert (row["credits_remaining"], row["unused_value_cents"]) == (9, 86400)


async def test_goodwill_refund_that_cancels_credits_removes_them(client):
    w = await world(client, credits=10, price_cents=96000)
    assert (await complete(client, await book(client, w), w.purchase["id"])).status_code == 200
    resp = await refund(
        client,
        w.purchase["id"],
        exception={
            "amount_cents": 20000,
            "cancel_remaining_credits": True,
            "reverse_commission": False,
        },
    )
    assert resp.status_code == 201, resp.text

    assert (await report(client))["rows"] == []


async def test_expired_credits_drop_off_after_their_expiry_day(client):
    w = await world(client)
    today = date.fromisoformat((await report(client))["as_of"])  # business timezone
    sql = "UPDATE package_purchases SET expires_after_days = 1, expires_at = :d WHERE id = :p"
    # The expiry day itself is still spendable (inclusive).
    await as_owner(sql, d=today, p=w.purchase["id"])
    assert len((await report(client))["rows"]) == 1

    await as_owner(sql, d=today - timedelta(days=1), p=w.purchase["id"])

    assert (await report(client))["rows"] == []


async def test_filters_by_purchase_date_and_customer(client):
    w = await world(client)
    other = await client.post(CUSTOMERS, json={**CUSTOMER, "first_name": "Zed"})
    assert other.status_code == 201, other.text
    w.customer_id = other.json()["id"]
    second = await buy(client, w, credits=3, price_cents=30000, pay=True)
    today = (await report(client))["as_of"]

    assert len((await report(client, **{"from": today, "to": today}))["rows"]) == 2
    tomorrow = (date.fromisoformat(today) + timedelta(days=1)).isoformat()
    assert (await report(client, **{"from": tomorrow}))["rows"] == []
    body = await report(client, customer_id=w.customer_id)
    assert [r["package_purchase_id"] for r in body["rows"]] == [second["id"]]
    assert body["total_unused_value_cents"] == 30000

    bad = await client.get(REPORT, params={"from": tomorrow, "to": today})
    assert bad.status_code == 422, bad.text


async def test_report_needs_billing_manage_in_admin_mode(client):
    await world(client)
    client.cookies.clear()
    login = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200, login.text

    assert (await client.get(REPORT)).status_code == 403


# --- #88: the CSV export, through the shared mechanism of #86 ----------------------------------


async def _queued_csv(client, **payload) -> list[dict]:
    """Requests an export, polls it and downloads it — eager Celery has already built it by
    the time the 202 is read (`test_commission_report.py`'s own pattern) — and returns the
    parsed CSV as a list of row dicts."""
    queued = await client.post(EXPORTS, json=payload)
    assert queued.status_code == 202, queued.text
    polled = await client.get(f"{EXPORTS}/{queued.json()['id']}")
    assert polled.status_code == 200, polled.text
    assert polled.json()["status"] == "ready"
    download = await client.get(polled.json()["download_url"])
    assert download.status_code == 200, download.text
    assert download.headers["content-type"].startswith("text/csv")
    assert "attachment" in download.headers["content-disposition"]
    return list(csv.DictReader(io.StringIO(download.text)))


async def test_the_csv_export_matches_the_unfiltered_report(client):
    w = await world(client, credits=10, price_cents=96000)
    assert (await complete(client, await book(client, w), w.purchase["id"])).status_code == 200

    body = await report(client)
    csv_rows = await _queued_csv(client)

    assert len(csv_rows) == len(body["rows"]) == 1
    row, expected = csv_rows[0], body["rows"][0]
    assert row["package_purchase_id"] == expected["package_purchase_id"]
    assert row["customer_id"] == expected["customer_id"]
    assert int(row["credits_total"]) == expected["credits_total"]
    assert int(row["credits_remaining"]) == expected["credits_remaining"]
    assert int(row["unused_value_cents"]) == expected["unused_value_cents"]


async def test_the_csv_export_honours_the_same_filters_as_the_report(client):
    w = await world(client)
    other = await client.post(CUSTOMERS, json={**CUSTOMER, "first_name": "Zed"})
    assert other.status_code == 201, other.text
    w.customer_id = other.json()["id"]
    second = await buy(client, w, credits=3, price_cents=30000, pay=True)

    body = await report(client, customer_id=w.customer_id)
    csv_rows = await _queued_csv(client, customer_id=w.customer_id)

    assert [r["package_purchase_id"] for r in csv_rows] == [second["id"]]
    assert [r["package_purchase_id"] for r in csv_rows] == [
        r["package_purchase_id"] for r in body["rows"]
    ]


async def test_an_export_with_an_inverted_range_is_refused_before_queueing(client):
    await world(client)
    today = date.today()
    resp = await client.post(
        EXPORTS, json={"from": today.isoformat(), "to": (today - timedelta(days=1)).isoformat()}
    )
    assert resp.status_code == 422, resp.text


async def test_the_export_routes_require_billing_manage_in_admin_mode(client):
    import uuid

    await world(client)
    client.cookies.clear()
    login = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200, login.text

    assert (await client.post(EXPORTS, json={})).status_code == 403
    assert (await client.get(f"{EXPORTS}/{uuid.uuid4()}")).status_code == 403
    assert (await client.get(f"{EXPORTS}/{uuid.uuid4()}/csv")).status_code == 403


async def test_polling_and_downloading_only_resolve_package_liability_exports(client):
    """A commission export's id must not resolve on the liability routes, or the reverse —
    `_load_export`/`_load_liability_export` both filter on `kind` (mirrors
    `test_commission_report.py`'s own `kind` isolation)."""
    await world(client)
    from tests.test_commission_report import EXPORTS as COMMISSION_EXPORTS

    foreign = await client.post(COMMISSION_EXPORTS, json={})
    assert foreign.status_code == 202, foreign.text

    assert (await client.get(f"{EXPORTS}/{foreign.json()['id']}")).status_code == 404
    assert (await client.get(f"{EXPORTS}/{foreign.json()['id']}/csv")).status_code == 404


async def test_downloading_an_expired_liability_export_is_refused_with_410(client):
    await world(client)
    queued = await client.post(EXPORTS, json={})
    assert queued.status_code == 202, queued.text
    export_id = queued.json()["id"]

    async with session_scope() as db:
        await db.execute(
            text(
                "UPDATE report_exports SET expires_at = now() - interval '1 second' WHERE id = :id"
            ),
            {"id": export_id},
        )
        await db.commit()

    refused = await client.get(f"{EXPORTS}/{export_id}/csv")
    assert refused.status_code == 410, refused.text


async def test_the_export_request_writes_one_audit_event_with_kind_and_params_no_content(client):
    w = await world(client)

    queued = await client.post(EXPORTS, json={"customer_id": w.customer_id})
    assert queued.status_code == 202, queued.text

    async with session_scope() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT target_id, metadata FROM audit_events "
                    "WHERE event_type = 'report.export_requested'"
                )
            )
        ).all()
    assert len(rows) == 1
    target_id, metadata = rows[0]
    assert target_id == queued.json()["id"]
    assert metadata["kind"] == "package_liability"
    assert metadata["params"]["customer_id"] == w.customer_id
    assert "content" not in metadata
    # The IP is no more sensitive here than what the access log itself already stores per
    # row (`core/access_log.py`) — see `billing/package_liability.py`'s own comment.
    assert metadata["params"]["_access_context"]["ip"] == "127.0.0.1"


async def test_building_the_export_logs_one_access_row_per_client_it_names(client):
    w = await world(client, credits=10, price_cents=96000)
    assert (await complete(client, await book(client, w), w.purchase["id"])).status_code == 200

    queued = await client.post(EXPORTS, json={})
    assert queued.status_code == 202, queued.text

    assert await access_rows(w.customer_id) == [w.customer_id]


async def test_a_filtered_export_logs_the_filtered_client_even_with_no_rows(client):
    w = await world(client)
    other = await client.post(CUSTOMERS, json={**CUSTOMER, "first_name": "Zed"})
    assert other.status_code == 201, other.text
    empty_customer_id = other.json()["id"]

    queued = await client.post(EXPORTS, json={"customer_id": empty_customer_id})
    assert queued.status_code == 202, queued.text

    assert await access_rows(empty_customer_id) == [empty_customer_id]
    assert await access_rows(w.customer_id) == []

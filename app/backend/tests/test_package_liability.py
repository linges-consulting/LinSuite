"""S1: unused-package liability report (#74), over a real PostgreSQL."""

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


async def report(client, **params) -> dict:
    resp = await client.get(REPORT, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


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

"""S1(+S5): M4 review T2 on service invoices — ledger writes locked against cancel (R7), the
reissue transfer locked against refunds (R9), no refunds on a replaced invoice (R10), a
concurrent double issue answered 422 not 500 (R11), and a cancelled invoice's retained money
shown as `held_credit_cents` instead of a negative balance (R12).

Races use two real ASGI clients, each with its own Admin Mode session; the first request is
slowed *while holding its row lock*, so the second genuinely waits on Postgres. Each was checked
to fail with the lock removed.

Reuses `tests/test_invoice_payments.py`'s autouse `claimed_instance`.
"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.db import session_scope
from tests.test_bill_review import as_admin, complete_a_visit
from tests.test_invoice_cancel_replace import cancel, pay, reissue, scalar
from tests.test_invoice_payments import (  # noqa: F401 — claimed_instance is an autouse fixture
    INVOICES,
    claimed_instance,
    exceptions_url,
    issue_an_invoice,
    issue_url,
    payments_url,
)


def refunds_url(invoice_id: str) -> str:
    return f"{INVOICES}/{invoice_id}/refunds"


async def read(client, invoice_id: str) -> dict:
    resp = await client.get(f"{INVOICES}/{invoice_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def race(monkeypatch, target, name: str, first, second):
    """Run `first(c)` then, 0.1s later, `second(c)` on two fresh Admin Mode clients, with
    `target.name` slowed by 0.3s *after* it runs — inside `first`'s locked transaction."""
    from main import app as main_app

    real = getattr(target, name)

    async def slow(*args, **kwargs):
        result = await real(*args, **kwargs)
        await asyncio.sleep(0.3)
        return result

    clients = [
        AsyncClient(transport=ASGITransport(app=main_app), base_url="http://test") for _ in "ab"
    ]
    try:
        for c in clients:
            await as_admin(c)
        monkeypatch.setattr(target, name, slow)

        async def later(c, call):
            await asyncio.sleep(0.1)
            return await call(c)

        return await asyncio.gather(first(clients[0]), later(clients[1], second))
    finally:
        for c in clients:
            await c.aclose()


# --- R7: payments / balance exceptions never land on an invoice being cancelled ------------------


@pytest.mark.parametrize("write", ["payment", "exception"])
async def test_a_ledger_write_racing_a_cancel_is_refused_not_recorded(client, monkeypatch, write):
    invoice_id = await issue_an_invoice(client, price_cents=12000)
    from billing import invoices as invoices_mod

    url, body = {
        "payment": (
            payments_url(invoice_id),
            {"payer_type": "client", "method": "cash", "amount_cents": 5000},
        ),
        "exception": (exceptions_url(invoice_id), {"reason": "Hardship"}),
    }[write]

    cancelled, late = await race(
        monkeypatch,
        invoices_mod,
        "reverse_commission",  # runs under the cancel's row lock
        lambda c: c.post(f"{INVOICES}/{invoice_id}/cancel", json={"reason": "Wrong"}),
        lambda c: c.post(url, json=body),
    )

    assert cancelled.status_code == 200, cancelled.text
    assert late.status_code == 422, late.text
    assert await scalar("SELECT count(*) FROM invoice_payments") == 0
    assert await scalar("SELECT count(*) FROM invoice_balance_authorizations") == 0


async def test_corrections_are_refused_on_a_cancelled_invoice(client):
    invoice_id = await issue_an_invoice(client, price_cents=12000)
    await pay(client, invoice_id, 12000)
    payment_id = (await client.get(payments_url(invoice_id))).json()["payments"][0]["id"]
    await cancel(client, invoice_id)

    resp = await client.post(
        f"{payments_url(invoice_id)}/{payment_id}/corrections",
        json={"reason": "typo", "amount_cents": 10000},
    )

    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("table", ["invoice_payments", "invoice_balance_authorizations"])
async def test_the_database_refuses_ledger_rows_on_a_cancelled_invoice(client, table):
    """S5: the 0064 trigger, as the app role, with no application code in the way."""
    invoice_id = await issue_an_invoice(client)
    await cancel(client, invoice_id)
    columns = {
        "invoice_payments": (
            "payer_type, method, amount_cents, collected_by",
            "'client', 'cash', 100, issued_by",
        ),
        "invoice_balance_authorizations": (
            "authorized_by, reason, outstanding_cents_at_authorization",
            "issued_by, 'r', 100",
        ),
    }[table]

    async with session_scope() as db:
        with pytest.raises(DBAPIError, match="not issued") as refused:
            await db.execute(
                text(
                    f"INSERT INTO {table} (invoice_id, {columns[0]}) "
                    f"SELECT id, {columns[1]} FROM invoices WHERE id = :id"
                ),
                {"id": invoice_id},
            )
        await db.rollback()
    assert getattr(refused.value.orig, "sqlstate", None) == "23514"


# --- R9 / R10: the reissue transfer and refunds against the lineage ------------------------------


async def test_a_refund_racing_a_reissue_is_carried_net_never_double_counted(client, monkeypatch):
    invoice_id = await issue_an_invoice(client, price_cents=10000)
    await pay(client, invoice_id, 12000)  # overpaid by 2000
    bill_id = (await cancel(client, invoice_id))["replacement_bill_id"]
    from billing import payments as payments_mod

    refunded, reissued = await race(
        monkeypatch,
        payments_mod,
        "refundable_cents",  # runs under the refund's lineage lock
        lambda c: c.post(refunds_url(invoice_id), json={"amount_cents": 2000, "reason": "Over"}),
        lambda c: c.post(issue_url(bill_id), json={}),
    )

    assert refunded.status_code == 201, refunded.text
    assert reissued.status_code == 201, reissued.text
    replacement = await read(client, reissued.json()["id"])
    assert replacement["outstanding_cents"] == 0
    original = await read(client, invoice_id)
    assert (original["held_credit_cents"], original["refunded_cents"]) == (0, 2000)
    transfer = (await client.get(payments_url(replacement["id"]))).json()["transfers"][0]
    assert transfer["received_cents"] == 10000


async def test_a_replaced_invoice_refuses_refunds_its_replacement_takes_them(client):
    invoice_id = await issue_an_invoice(client, price_cents=10000)
    await pay(client, invoice_id, 12000)
    replacement = await reissue(client, (await cancel(client, invoice_id))["replacement_bill_id"])

    on_original = await client.post(
        refunds_url(invoice_id), json={"amount_cents": 2000, "reason": "Overpaid"}
    )
    on_replacement = await client.post(
        refunds_url(replacement["id"]), json={"amount_cents": 2000, "reason": "Overpaid"}
    )

    assert on_original.status_code == 422, on_original.text
    assert "replacement" in on_original.json()["detail"]
    assert on_replacement.status_code == 201, on_replacement.text
    assert (await read(client, invoice_id))["held_credit_cents"] == 0
    assert (await read(client, replacement["id"]))["outstanding_cents"] == 0


# --- R11: two concurrent issues of one draft -----------------------------------------------------


@pytest.mark.parametrize("reissue_first", [False, True])
async def test_a_concurrent_double_issue_gets_one_invoice_and_a_422(
    client, monkeypatch, reissue_first
):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client, price_cents=12000)
    if reissue_first:
        first = await client.post(issue_url(bill_id), json={})
        assert first.status_code == 201, first.text
        await cancel(client, first.json()["id"])
    from billing import invoices as invoices_mod

    results = await race(
        monkeypatch,
        invoices_mod,
        "persisted_selection",  # runs after the status check, under the bill lock
        lambda c: c.post(issue_url(bill_id), json={}),
        lambda c: c.post(issue_url(bill_id), json={}),
    )

    assert sorted(r.status_code for r in results) == [201, 422], [r.text for r in results]
    live = "SELECT count(*) FROM invoices WHERE status = 'issued'"
    assert await scalar(live) == 1
    assert await scalar("SELECT count(*) FROM invoice_payment_transfers") == int(reissue_first)


# --- R12: retained money on a cancelled invoice --------------------------------------------------


async def test_a_cancelled_invoice_reports_zero_owed_and_its_held_credit(client):
    invoice_id = await issue_an_invoice(client, price_cents=12000)
    await pay(client, invoice_id, 5000)
    await cancel(client, invoice_id)

    body = await read(client, invoice_id)
    listed = (await client.get(INVOICES)).json()["invoices"][0]

    for view in (body, listed):
        assert view["status"] == "cancelled"
        assert view["outstanding_cents"] == 0
        assert view["client_outstanding_cents"] == 0
        assert view["held_credit_cents"] == 5000
        assert view["checkout_complete"] is True

"""S1(+S5): payment correction + admin-approved refund (#67) over a real PostgreSQL.

Reuses `tests/test_invoice_payments.py`'s `claimed_instance` (imported — autouse applies here
too) and its issue/pay helpers.
"""

import asyncio
import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from core.db import session_scope
from tests.test_bill_review import add_front_desk_account, as_admin, as_staff, complete_a_visit
from tests.test_invoice_payments import (  # noqa: F401 — autouse fixture
    INVOICES,
    claimed_instance,
    issue_an_invoice,
    issue_url,
    payments_url,
)


def corrections_url(invoice_id: str, payment_id: str) -> str:
    return f"{payments_url(invoice_id)}/{payment_id}/corrections"


def refunds_url(invoice_id: str) -> str:
    return f"{INVOICES}/{invoice_id}/refunds"


async def pay(client, invoice_id: str, amount_cents: int, **extra) -> str:
    body = {"payer_type": "client", "method": "cash", "amount_cents": amount_cents, **extra}
    resp = await client.post(payments_url(invoice_id), json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def invoice(client, invoice_id: str) -> dict:
    resp = await client.get(f"{INVOICES}/{invoice_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def as_front_desk(client) -> None:
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)


# --- correction ---------------------------------------------------------------------------------


async def test_staff_corrects_an_amount_the_original_is_kept_and_the_balance_recomputes(client):
    invoice_id = await issue_an_invoice(client, price_cents=12000)
    await as_front_desk(client)
    payment_id = await pay(client, invoice_id, 12000)

    resp = await client.post(
        corrections_url(invoice_id, payment_id),
        json={"reason": "Keyed 120 instead of 100", "amount_cents": 10000},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["corrects_payment_id"] == payment_id
    assert body["correction_reason"] == "Keyed 120 instead of 100"
    assert body["amount_cents"] == 10000
    assert body["method"] == "cash"
    listed = {p["id"]: p for p in (await client.get(payments_url(invoice_id))).json()["payments"]}
    assert listed[payment_id]["amount_cents"] == 12000  # original untouched
    assert listed[payment_id]["corrects_payment_id"] is None
    after = await invoice(client, invoice_id)
    assert after["outstanding_cents"] == 2000
    assert after["checkout_complete"] is False


async def test_a_correction_can_change_method_payer_and_reference(client):
    invoice_id = await issue_an_invoice(client, price_cents=5000)
    payment_id = await pay(client, invoice_id, 5000, reference="wrong")

    resp = await client.post(
        corrections_url(invoice_id, payment_id),
        json={"reason": "Was card", "method": "card", "reference": "AUTH-1"},
    )
    assert resp.status_code == 201, resp.text
    assert (resp.json()["method"], resp.json()["reference"]) == ("card", "AUTH-1")

    to_insurer = await client.post(
        corrections_url(invoice_id, resp.json()["id"]),
        json={"reason": "Insurer paid it", "payer_type": "insurer", "method": "insurer"},
    )
    assert to_insurer.status_code == 201, to_insurer.text
    assert (await invoice(client, invoice_id))["outstanding_cents"] == 0


async def test_a_correction_needs_a_reason(client):
    invoice_id = await issue_an_invoice(client, price_cents=5000)
    payment_id = await pay(client, invoice_id, 5000)

    for body in ({"amount_cents": 4000}, {"amount_cents": 4000, "reason": ""}):
        resp = await client.post(corrections_url(invoice_id, payment_id), json=body)
        assert resp.status_code == 422, resp.text


async def test_a_correction_that_changes_nothing_or_mismatches_payer_is_refused(client):
    invoice_id = await issue_an_invoice(client, price_cents=5000)
    payment_id = await pay(client, invoice_id, 5000)
    url = corrections_url(invoice_id, payment_id)

    same = await client.post(url, json={"reason": "r", "amount_cents": 5000})
    mismatch = await client.post(url, json={"reason": "r", "payer_type": "insurer"})

    assert same.status_code == 422, same.text
    assert mismatch.status_code == 422, mismatch.text


async def test_an_entry_is_corrected_once_then_the_correction_is_corrected(client):
    invoice_id = await issue_an_invoice(client, price_cents=5000)
    payment_id = await pay(client, invoice_id, 5000)
    first = await client.post(
        corrections_url(invoice_id, payment_id), json={"reason": "r1", "amount_cents": 3000}
    )
    assert first.status_code == 201, first.text

    again = await client.post(
        corrections_url(invoice_id, payment_id), json={"reason": "r2", "amount_cents": 4000}
    )
    chained = await client.post(
        corrections_url(invoice_id, first.json()["id"]), json={"reason": "r2", "amount_cents": 4000}
    )

    assert again.status_code == 409, again.text
    assert chained.status_code == 201, chained.text
    assert (await invoice(client, invoice_id))["outstanding_cents"] == 1000


async def test_a_correction_may_zero_out_a_mistaken_entry(client):
    invoice_id = await issue_an_invoice(client, price_cents=5000)
    await pay(client, invoice_id, 5000)
    duplicate = await pay(client, invoice_id, 5000)

    resp = await client.post(
        corrections_url(invoice_id, duplicate), json={"reason": "Double-entered", "amount_cents": 0}
    )

    assert resp.status_code == 201, resp.text
    assert (await invoice(client, invoice_id))["outstanding_cents"] == 0


async def test_correcting_a_payment_from_another_invoice_404s(client):
    first = await issue_an_invoice(client, price_cents=5000)
    payment_id = await pay(client, first, 5000)
    bill_id, _ = await complete_a_visit(client, service_name="Deep Tissue Massage")
    second = (await client.post(issue_url(bill_id), json={})).json()["id"]

    resp = await client.post(
        corrections_url(second, payment_id), json={"reason": "r", "amount_cents": 1}
    )

    assert resp.status_code == 404, resp.text


async def test_a_correction_never_returns_money_to_the_client(client):
    """Correcting down past the total leaves the client in credit on paper — nothing refunded."""
    invoice_id = await issue_an_invoice(client, price_cents=5000)
    payment_id = await pay(client, invoice_id, 5000)

    resp = await client.post(
        corrections_url(invoice_id, payment_id), json={"reason": "Was 80", "amount_cents": 8000}
    )

    assert resp.status_code == 201, resp.text
    after = await invoice(client, invoice_id)
    assert after["outstanding_cents"] == -3000
    assert after["refunded_cents"] == 0
    assert (await client.get(refunds_url(invoice_id))).json()["refunds"] == []


# --- refund -------------------------------------------------------------------------------------


async def test_an_admin_refund_records_amount_reason_and_approver(client):
    invoice_id = await issue_an_invoice(client, price_cents=10000)
    await pay(client, invoice_id, 12000)  # overpaid by 20

    resp = await client.post(refunds_url(invoice_id), json={"amount_cents": 2000, "reason": "Over"})

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["amount_cents"], body["reason"]) == (2000, "Over")
    async with session_scope() as db:
        admin_id = str(await db.scalar(text("SELECT id FROM users ORDER BY created_at LIMIT 1")))
    assert body["approved_by"] == admin_id
    after = await invoice(client, invoice_id)
    assert (after["outstanding_cents"], after["refunded_cents"]) == (0, 2000)
    assert after["checkout_complete"] is True
    async with session_scope() as db:
        events = await db.scalar(
            text("SELECT count(*) FROM audit_events WHERE event_type = 'invoice.refund_recorded'")
        )
    assert events == 1


async def test_staff_attempting_a_refund_is_refused(client):
    invoice_id = await issue_an_invoice(client, price_cents=5000)
    await pay(client, invoice_id, 5000)
    await as_front_desk(client)

    resp = await client.post(refunds_url(invoice_id), json={"amount_cents": 100, "reason": "r"})

    assert resp.status_code == 403, resp.text
    assert (await client.get(refunds_url(invoice_id))).json()["refunds"] == []


async def test_refunds_are_capped_at_money_received_less_prior_refunds(client):
    invoice_id = await issue_an_invoice(client, price_cents=10000)
    await pay(client, invoice_id, 6000)
    await pay(client, invoice_id, 4000, payer_type="insurer", method="insurer", status="pending")
    url = refunds_url(invoice_id)

    over = await client.post(url, json={"amount_cents": 6001, "reason": "r"})
    first = await client.post(url, json={"amount_cents": 4000, "reason": "r"})
    second_over = await client.post(url, json={"amount_cents": 2001, "reason": "r"})
    second = await client.post(url, json={"amount_cents": 2000, "reason": "r"})

    assert over.status_code == 422, over.text  # pending insurer money is not received
    assert first.status_code == 201, first.text
    assert second_over.status_code == 422, second_over.text
    assert second.status_code == 201, second.text


async def test_a_correction_cannot_drop_received_below_what_was_refunded(client):
    invoice_id = await issue_an_invoice(client, price_cents=10000)
    payment_id = await pay(client, invoice_id, 10000)
    refunded = await client.post(
        refunds_url(invoice_id), json={"amount_cents": 7000, "reason": "r"}
    )
    assert refunded.status_code == 201, refunded.text

    resp = await client.post(
        corrections_url(invoice_id, payment_id), json={"reason": "r", "amount_cents": 6999}
    )

    assert resp.status_code == 422, resp.text
    assert len((await client.get(payments_url(invoice_id))).json()["payments"]) == 1


async def test_the_cap_spans_the_replacement_lineage(client):
    original = await issue_an_invoice(client, price_cents=5000)
    await pay(client, original, 5000)
    bill_id, _ = await complete_a_visit(client, service_name="Deep Tissue Massage")
    replacement = (await client.post(issue_url(bill_id), json={})).json()["id"]
    owner = create_async_engine(os.environ["DATABASE_URL_MIGRATE"])
    try:
        async with owner.begin() as conn:
            await conn.execute(
                text("UPDATE invoices SET replaces_invoice_id = :a WHERE id = :b"),
                {"a": original, "b": replacement},
            )
    finally:
        await owner.dispose()

    on_replacement = await client.post(
        refunds_url(replacement), json={"amount_cents": 5000, "reason": "r"}
    )
    on_original = await client.post(refunds_url(original), json={"amount_cents": 1, "reason": "r"})

    assert on_replacement.status_code == 201, on_replacement.text
    assert on_original.status_code == 422, on_original.text


async def test_invoice_refunds_is_append_only(client):
    invoice_id = await issue_an_invoice(client, price_cents=5000)
    await pay(client, invoice_id, 5000)
    refund_id = (
        await client.post(refunds_url(invoice_id), json={"amount_cents": 100, "reason": "r"})
    ).json()["id"]

    async with session_scope() as db:
        for statement in (
            "UPDATE invoice_refunds SET amount_cents = 1 WHERE id = :id",
            "DELETE FROM invoice_refunds WHERE id = :id",
        ):
            with pytest.raises(DBAPIError) as refused:
                await db.execute(text(statement), {"id": refund_id})
            await db.rollback()
            assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value


# --- concurrency: two real ASGI clients; a sleep after the cap check, before commit


async def _race(client, monkeypatch, *requests):
    from billing import payments as payments_mod
    from main import app as main_app

    real_check = payments_mod.refundable_cents

    async def slow_check(*args, **kwargs):
        result = await real_check(*args, **kwargs)
        await asyncio.sleep(0.25)
        return result

    # Each racer gets its own Admin Mode session: sharing one cookie would serialize the two
    # requests on that session's own sliding-window row, hiding the race this is proving.
    clients = [
        AsyncClient(transport=ASGITransport(app=main_app), base_url="http://test") for _ in requests
    ]
    try:
        for c in clients:
            await as_admin(c)
        monkeypatch.setattr(payments_mod, "refundable_cents", slow_check)
        return await asyncio.gather(
            *(c.post(url, json=body) for c, (url, body) in zip(clients, requests, strict=True))
        )
    finally:
        for c in clients:
            await c.aclose()


async def test_two_concurrent_refunds_cannot_together_exceed_the_cap(client, monkeypatch):
    invoice_id = await issue_an_invoice(client, price_cents=10000)
    await pay(client, invoice_id, 10000)
    body = {"amount_cents": 6000, "reason": "r"}

    results = await _race(
        client, monkeypatch, (refunds_url(invoice_id), body), (refunds_url(invoice_id), body)
    )

    assert sorted(r.status_code for r in results) == [201, 422], [r.text for r in results]
    assert (await invoice(client, invoice_id))["refunded_cents"] == 6000


async def test_a_concurrent_correction_and_refund_cannot_together_break_the_cap(
    client, monkeypatch
):
    """Either order alone is legal; together they'd leave 5000 received against 6000 refunded."""
    invoice_id = await issue_an_invoice(client, price_cents=10000)
    payment_id = await pay(client, invoice_id, 10000)

    results = await _race(
        client,
        monkeypatch,
        (refunds_url(invoice_id), {"amount_cents": 6000, "reason": "r"}),
        (corrections_url(invoice_id, payment_id), {"reason": "r", "amount_cents": 5000}),
    )

    assert sorted(r.status_code for r in results) == [201, 422], [r.text for r in results]
    async with session_scope() as db:
        received = await db.scalar(
            text(
                "SELECT sum(amount_cents) FROM invoice_payments p WHERE NOT EXISTS "
                "(SELECT 1 FROM invoice_payments c WHERE c.corrects_payment_id = p.id)"
            )
        )
        refunded = await db.scalar(
            text("SELECT coalesce(sum(amount_cents), 0) FROM invoice_refunds")
        )
    assert received >= refunded


async def test_two_concurrent_corrections_of_one_entry_land_once(client, monkeypatch):
    invoice_id = await issue_an_invoice(client, price_cents=10000)
    payment_id = await pay(client, invoice_id, 10000)
    url = corrections_url(invoice_id, payment_id)

    results = await _race(
        client,
        monkeypatch,
        (url, {"reason": "a", "amount_cents": 9000}),
        (url, {"reason": "b", "amount_cents": 8000}),
    )

    assert sorted(r.status_code for r in results) == [201, 409], [r.text for r in results]

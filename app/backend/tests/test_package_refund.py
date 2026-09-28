"""S1: package/bundle refunds (#73), over a real PostgreSQL.

Zero-use standard refund (cancel + void + refund in one transaction), the standard path's
refusal once a credit is used, the manual admin exception's explicit choices, the #67 cap,
the refund-vs-redemption race, and the void table's own DB enforcement.
"""

import asyncio
import pathlib

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.db import session_scope
from tests.test_bill_review import EMAIL, PASSWORD
from tests.test_credit_redemption import (  # noqa: F401 — claimed_instance is an autouse fixture
    bill_of,
    book,
    claimed_instance,
    complete,
    credits_left,
    pay_invoice,
    redemption_count,
    status_of,
    world,
)


def refund_url(purchase_id: str) -> str:
    return f"/api/packages/purchases/{purchase_id}/refund"


async def refund(client, purchase_id: str, **body):
    return await client.post(refund_url(purchase_id), json={"reason": "Client moved", **body})


async def scalar(sql: str, **params):
    async with session_scope() as db:
        return await db.scalar(text(sql), params)


async def redeem_and_issue(client, w, slot: int = 0) -> dict:
    appointment_id = await book(client, w, slot=slot)
    assert (await complete(client, appointment_id, w.purchase["id"])).status_code == 200
    bill = await bill_of(client, appointment_id)
    issued = await client.post(f"/api/bills/{bill['id']}/issue", json={})
    assert issued.status_code == 201, issued.text
    return issued.json()


# --- the standard (zero-use) path -------------------------------------------------------------


async def test_zero_use_refund_returns_what_was_paid_and_cancels_the_entitlement(client):
    w = await world(client, credits=10, price_cents=96000)
    appointment_id = await book(client, w)
    assert len(await credits_left(client, appointment_id)) == 1

    resp = await refund(client, w.purchase["id"])

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["refund"]["amount_cents"] == w.purchase["grand_total_cents"]
    assert body["credits_voided"] is True
    invoice = body["invoice"]
    assert invoice["status"] == "cancelled"
    # A cancelled invoice bills nothing, so a full refund leaves nothing owed and no credit.
    assert (invoice["outstanding_cents"], invoice["refunded_cents"]) == (0, 96000)
    assert await credits_left(client, appointment_id) == []
    purchase = (await client.get(f"/api/packages/purchases/{w.purchase['id']}")).json()
    assert purchase["credits_voided_at"] is not None
    assert (await complete(client, appointment_id, w.purchase["id"])).status_code == 422
    assert await status_of(appointment_id) == "confirmed"
    assert (
        await scalar(
            "SELECT count(*) FROM audit_events WHERE event_type = 'package_purchase.refunded'"
        )
        == 1
    )


async def test_zero_use_refund_deducts_prior_refunds(client):
    w = await world(client, credits=10, price_cents=96000)
    prior = await client.post(
        f"/api/invoices/{w.purchase['invoice_id']}/refunds",
        json={"amount_cents": 1000, "reason": "Overcharged"},
    )
    assert prior.status_code == 201, prior.text

    resp = await refund(client, w.purchase["id"])

    assert resp.status_code == 201, resp.text
    assert resp.json()["refund"]["amount_cents"] == 96000 - 1000
    assert resp.json()["invoice"]["outstanding_cents"] == 0


async def test_an_unpaid_zero_use_package_is_cancelled_with_no_refund_row(client):
    w = await world(client, pay=False)

    resp = await refund(client, w.purchase["id"])

    assert resp.status_code == 201, resp.text
    assert resp.json()["refund"] is None
    assert resp.json()["invoice"]["status"] == "cancelled"
    assert await scalar("SELECT count(*) FROM invoice_refunds") == 0
    # A cancelled invoice takes no further payment, so the credits can never activate.
    late = await client.post(
        f"/api/invoices/{w.purchase['invoice_id']}/payments",
        json={"payer_type": "client", "method": "cash", "amount_cents": 1000},
    )
    assert late.status_code == 422


async def test_a_second_standard_refund_is_refused(client):
    w = await world(client)
    assert (await refund(client, w.purchase["id"])).status_code == 201

    again = await refund(client, w.purchase["id"])

    assert again.status_code == 409, again.text
    assert await scalar("SELECT count(*) FROM invoice_refunds") == 1


async def test_standard_path_refuses_once_a_credit_is_used(client):
    w = await world(client, credits=10, price_cents=96000)
    appointment_id = await book(client, w)
    assert (await complete(client, appointment_id, w.purchase["id"])).status_code == 200

    resp = await refund(client, w.purchase["id"])

    assert resp.status_code == 422, resp.text
    assert "exception" in resp.json()["detail"]
    invoice = (await client.get(f"/api/invoices/{w.purchase['invoice_id']}")).json()
    assert invoice["status"] == "issued"
    assert await scalar("SELECT count(*) FROM invoice_refunds") == 0
    assert await scalar("SELECT count(*) FROM package_credit_voids") == 0


async def test_refunds_need_billing_manage_in_admin_mode(client):
    w = await world(client)
    client.cookies.clear()
    login = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200, login.text

    resp = await refund(client, w.purchase["id"])

    assert resp.status_code == 403, resp.text
    assert await scalar("SELECT count(*) FROM invoice_refunds") == 0


async def test_an_unknown_purchase_is_404(client):
    w = await world(client)
    assert (await refund(client, w.customer_id)).status_code == 404


# --- the manual admin/owner exception ---------------------------------------------------------


async def test_the_exception_requires_every_explicit_choice(client):
    w = await world(client)
    full = {"amount_cents": 100, "cancel_remaining_credits": True, "reverse_commission": False}
    for missing in full:
        body = {k: v for k, v in full.items() if k != missing}
        resp = await refund(client, w.purchase["id"], exception=body)
        assert resp.status_code == 422, (missing, resp.text)
    no_reason = await client.post(refund_url(w.purchase["id"]), json={"exception": full})
    assert no_reason.status_code == 422
    assert await scalar("SELECT count(*) FROM invoice_refunds") == 0


async def test_goodwill_refund_after_use_keeps_credits_and_preserves_commission(client):
    w = await world(client, credits=10, price_cents=96000)
    await redeem_and_issue(client, w)

    resp = await refund(
        client,
        w.purchase["id"],
        exception={
            "amount_cents": 20000,
            "cancel_remaining_credits": False,
            "reverse_commission": False,
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["refund"]["amount_cents"], body["credits_voided"]) == (20000, False)
    assert body["commission_reversals"] == 0
    assert body["invoice"]["status"] == "cancelled"
    assert await scalar("SELECT count(*) FROM commission_postings WHERE kind = 'reversal'") == 0
    second = await book(client, w, slot=1)
    assert (await credits_left(client, second))[0]["credits_remaining"] == 9


async def test_exception_can_cancel_credits_and_reverse_commission(client):
    w = await world(client, credits=10, price_cents=96000)
    await redeem_and_issue(client, w)
    earned = await scalar("SELECT amount_cents FROM commission_postings WHERE kind = 'earned'")

    resp = await refund(
        client,
        w.purchase["id"],
        exception={
            "amount_cents": 86400,
            "cancel_remaining_credits": True,
            "reverse_commission": True,
        },
    )

    assert resp.status_code == 201, resp.text
    assert (resp.json()["credits_voided"], resp.json()["commission_reversals"]) == (True, 1)
    assert (
        await scalar("SELECT amount_cents FROM commission_postings WHERE kind = 'reversal'")
        == -earned
    )
    second = await book(client, w, slot=1)
    assert await credits_left(client, second) == []


async def test_a_reversed_posting_is_not_reversed_again_when_its_invoice_is_cancelled(client):
    w = await world(client, credits=10, price_cents=96000)
    service_invoice = await redeem_and_issue(client, w)
    resp = await refund(
        client,
        w.purchase["id"],
        exception={"amount_cents": 1, "cancel_remaining_credits": True, "reverse_commission": True},
    )
    assert resp.status_code == 201, resp.text

    cancelled = await client.post(
        f"/api/invoices/{service_invoice['id']}/cancel", json={"reason": "Wrong date"}
    )

    assert cancelled.status_code == 200, cancelled.text
    assert await scalar("SELECT count(*) FROM commission_postings WHERE kind = 'reversal'") == 1


async def test_the_exception_is_capped_at_money_received_less_prior_refunds(client):
    w = await world(client, credits=10, price_cents=96000)
    await redeem_and_issue(client, w)
    first = await refund(
        client,
        w.purchase["id"],
        exception={
            "amount_cents": 90000,
            "cancel_remaining_credits": False,
            "reverse_commission": False,
        },
    )
    assert first.status_code == 201, first.text

    over = await refund(
        client,
        w.purchase["id"],
        exception={
            "amount_cents": 6001,
            "cancel_remaining_credits": True,
            "reverse_commission": False,
        },
    )

    assert over.status_code == 422, over.text
    assert "6000" in over.json()["detail"]
    # Nothing from the refused call landed — the void rolled back with it.
    assert await scalar("SELECT count(*) FROM package_credit_voids") == 0
    assert await scalar("SELECT sum(amount_cents) FROM invoice_refunds") == 90000


async def test_no_partial_use_formula_exists_anywhere():
    """#54 replaced #14's "paid minus sessions used x regular price" formula with the manual
    exception. Nothing in billing multiplies redeemed sessions by a price."""
    src = pathlib.Path(__file__).parents[1] / "src" / "billing"
    for path in src.glob("*.py"):
        code = path.read_text().lower()
        assert "sessions_used" not in code, path
        assert "used * " not in code and "redeemed *" not in code, path


# --- race safety --------------------------------------------------------------------------------


@pytest.mark.parametrize("first", ["redeem", "refund"])
async def test_a_refund_racing_a_redemption_never_lets_both_land(client, monkeypatch, first):
    """Two ASGI clients: one completes an appointment against the package, the other asks for
    the standard refund. Whichever takes the purchase-row lock first is slowed while holding it,
    so the other genuinely waits on Postgres — the loser is refused, never both."""
    w = await world(client, credits=10, price_cents=96000)
    appointment_id = await book(client, w)
    cookie = client.cookies["linsuite_session"]

    from billing import package_refund, redemption
    from main import app as main_app

    target, name = (
        (redemption, "_eligible") if first == "redeem" else (package_refund, "refundable_cents")
    )
    real = getattr(target, name)

    async def slow(*args, **kwargs):
        result = await real(*args, **kwargs)
        await asyncio.sleep(0.3)
        return result

    monkeypatch.setattr(target, name, slow)

    async def call(kind: str, delay: float):
        await asyncio.sleep(delay)
        async with AsyncClient(transport=ASGITransport(app=main_app), base_url="http://test") as c:
            c.cookies.set("linsuite_session", cookie)
            if kind == "redeem":
                return await complete(c, appointment_id, w.purchase["id"])
            return await refund(c, w.purchase["id"])

    other = "refund" if first == "redeem" else "redeem"
    won, lost = await asyncio.gather(call(first, 0), call(other, 0.1))

    assert won.status_code in (200, 201), won.text
    assert lost.status_code == 422, lost.text
    refunds = await scalar("SELECT count(*) FROM invoice_refunds")
    if first == "redeem":
        assert (await redemption_count(), refunds) == (1, 0)
        assert await status_of(appointment_id) == "completed"
    else:
        assert (await redemption_count(), refunds) == (0, 1)
        assert await status_of(appointment_id) == "confirmed"


# --- database enforcement -----------------------------------------------------------------------


async def test_the_database_refuses_redeeming_a_voided_purchase_and_rewriting_a_void(client):
    w = await world(client)
    assert (await refund(client, w.purchase["id"])).status_code == 201
    appointment_id = await book(client, w)
    user_id = await scalar("SELECT id FROM users LIMIT 1")

    with pytest.raises(DBAPIError, match="voided"):
        async with session_scope() as db:
            await db.execute(
                text(
                    "INSERT INTO package_credit_redemptions (package_purchase_id, service_id, "
                    "sequence, appointment_id, value_cents, redeemed_by) "
                    "VALUES (:p, :s, 1, :a, 0, :u)"
                ),
                {"p": w.purchase["id"], "s": w.service_id, "a": appointment_id, "u": user_id},
            )
            await db.commit()
    for statement in (
        "UPDATE package_credit_voids SET reason = 'x'",
        "DELETE FROM package_credit_voids",
    ):
        with pytest.raises(DBAPIError):
            async with session_scope() as db:
                await db.execute(text(statement))
                await db.commit()
    assert await scalar("SELECT count(*) FROM package_credit_voids") == 1


async def test_paying_then_refunding_leaves_the_purchase_invoice_settled(client):
    # A partial payment, then a zero-use refund: exactly what was received goes back.
    w = await world(client, pay=False)
    await pay_invoice(client, w.purchase["invoice_id"], 5000)

    resp = await refund(client, w.purchase["id"])

    assert resp.status_code == 201, resp.text
    assert resp.json()["refund"]["amount_cents"] == 5000
    assert resp.json()["invoice"]["outstanding_cents"] == 0

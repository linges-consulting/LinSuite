"""S1(+S5): package transfer (#111, spec #96) — an admin, Admin Mode, `billing.manage`, moves
a package purchase's whole remaining balance to another client, over a real PostgreSQL.

Covers: a transferable package transfers without an override; a non-transferable package is
refused without one and succeeds with it (audit records `override: true`); every refusal case
(not active in each of its four shapes, target is the holder/suppressed/missing, `from` is not
the current holder); a concurrent second transfer is refused (the purchase row lock); checkout
offers a transferred purchase's credits to the new holder and not the old; a chain A -> B -> C
ends with C; the liability report names the current holder; a refund after transfer still
refunds the purchaser and voids the credits regardless of who holds them; the audit event
carries no reason text; the client package-purchases list shows both a received and a
transferred-out purchase, each with its chain; a transferred holder's treatment receipt names
them, never the purchaser; and the append-only guard on `package_transfers` itself.
"""

import asyncio
import os
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from core.db import get_purge_engine, session_scope
from scheduling.clock import today_in
from tests.test_bill_review import CUSTOMERS, EMAIL, PASSWORD
from tests.test_credit_redemption import (  # noqa: F401 — claimed_instance is an autouse fixture
    PACKAGES,
    book,
    claimed_instance,
    complete,
    credits_left,
    pay_invoice,
    world,
)


def transfer_url(purchase_id: str) -> str:
    return f"/api/packages/purchases/{purchase_id}/transfer"


def refund_url(purchase_id: str) -> str:
    return f"/api/packages/purchases/{purchase_id}/refund"


async def scalar(sql: str, **params):
    async with session_scope() as db:
        return await db.scalar(text(sql), params)


async def make_second_customer(client, first="Sam", last="Lee", phone="416-555-0100"):
    resp = await client.post(
        CUSTOMERS, json={"first_name": first, "last_name": last, "phone": phone}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def buy_package(client, w, *, transferable: bool, credits=5, price_cents=50000, pay=True):
    package = await client.post(
        PACKAGES,
        json={
            "name": "Reiki Pack",
            "description": "",
            "price_cents": price_cents,
            "transferable": transferable,
            "services": [{"service_id": w.service_id, "credits": credits}],
        },
    )
    assert package.status_code == 201, package.text
    bought = await client.post(
        f"/api/packages/{package.json()['id']}/purchase", json={"customer_id": w.customer_id}
    )
    assert bought.status_code == 201, bought.text
    body = bought.json()
    if pay:
        await pay_invoice(client, body["invoice_id"], body["grand_total_cents"])
    return body


async def transfer(
    client,
    purchase_id: str,
    *,
    to_customer_id: str,
    from_customer_id: str,
    override=True,
    reason="Client moved away",
):
    """`override=True` by default: `world()`'s own package is non-transferable (the default),
    and most of this file's tests are proving something other than the override rule itself —
    the two override-specific tests below pass it explicitly, `False` first."""
    return await client.post(
        transfer_url(purchase_id),
        json={
            "to_customer_id": to_customer_id,
            "from_customer_id": from_customer_id,
            "reason": reason,
            "override": override,
        },
    )


async def refund(client, purchase_id: str, **body):
    return await client.post(refund_url(purchase_id), json={"reason": "Refund", **body})


async def package_purchases_of(client, customer_id: str) -> list[dict]:
    resp = await client.get(f"/api/customers/{customer_id}/package-purchases")
    assert resp.status_code == 200, resp.text
    return resp.json()["purchases"]


# --- transferable vs. non-transferable, override ------------------------------------------


async def test_a_transferable_package_transfers_without_an_override(client):
    w = await world(client)
    purchase = await buy_package(w=w, client=client, transferable=True)
    other = await make_second_customer(client)

    resp = await transfer(
        client,
        purchase["id"],
        to_customer_id=other,
        from_customer_id=w.customer_id,
        override=False,
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["current_holder_id"] == other
    assert len(body["transfers"]) == 1
    assert body["transfers"][0]["override"] is False


async def test_a_non_transferable_package_is_refused_without_override_and_succeeds_with_it(
    client,
):
    w = await world(client)  # w.purchase is non-transferable by default
    other = await make_second_customer(client)

    refused = await transfer(
        client,
        w.purchase["id"],
        to_customer_id=other,
        from_customer_id=w.customer_id,
        override=False,
    )
    assert refused.status_code == 422, refused.text
    assert await scalar("SELECT count(*) FROM package_transfers") == 0

    resp = await transfer(
        client,
        w.purchase["id"],
        to_customer_id=other,
        from_customer_id=w.customer_id,
        override=True,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["transfers"][0]["override"] is True
    logged = await scalar(
        "SELECT metadata->>'override' FROM audit_events WHERE event_type = 'package.transferred'"
    )
    assert logged == "true"


# --- refusals -------------------------------------------------------------------------------


async def test_an_unpaid_purchase_cannot_be_transferred(client):
    w = await world(client, pay=False)
    other = await make_second_customer(client)

    resp = await transfer(
        client, w.purchase["id"], to_customer_id=other, from_customer_id=w.customer_id
    )

    assert resp.status_code == 422, resp.text


async def test_a_fully_used_purchase_cannot_be_transferred(client):
    w = await world(client, credits=1, price_cents=5000)
    appointment_id = await book(client, w)
    assert (await complete(client, appointment_id, w.purchase["id"])).status_code == 200
    other = await make_second_customer(client)

    resp = await transfer(
        client, w.purchase["id"], to_customer_id=other, from_customer_id=w.customer_id
    )

    assert resp.status_code == 422, resp.text


async def test_a_refunded_purchase_cannot_be_transferred(client):
    w = await world(client)
    assert (await refund(client, w.purchase["id"])).status_code == 201
    other = await make_second_customer(client)

    resp = await transfer(
        client, w.purchase["id"], to_customer_id=other, from_customer_id=w.customer_id
    )

    assert resp.status_code == 422, resp.text


async def test_a_goodwill_refund_that_kept_credits_still_blocks_transfer(client):
    """The invoice is cancelled either way (`billing/package_refund.py`'s own rule) — even a
    goodwill refund that explicitly kept the remaining credits leaves nothing active to
    transfer, since the purchase invoice itself is no longer issued."""
    w = await world(client, credits=10, price_cents=96000)
    appointment_id = await book(client, w)
    assert (await complete(client, appointment_id, w.purchase["id"])).status_code == 200
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
    other = await make_second_customer(client)

    resp = await transfer(
        client, w.purchase["id"], to_customer_id=other, from_customer_id=w.customer_id
    )

    assert resp.status_code == 422, resp.text


async def test_an_expired_purchase_cannot_be_transferred(client):
    w = await world(client)
    owner = create_async_engine(os.environ["DATABASE_URL_MIGRATE"])
    try:
        async with owner.begin() as conn:
            zone = await conn.scalar(text("SELECT timezone FROM businesses"))
            await conn.execute(
                text(
                    "UPDATE package_purchases SET expires_after_days = 1, "
                    "expires_at = :expired WHERE id = :p"
                ),
                {"p": w.purchase["id"], "expired": today_in(zone) - timedelta(days=1)},
            )
    finally:
        await owner.dispose()
    other = await make_second_customer(client)

    resp = await transfer(
        client, w.purchase["id"], to_customer_id=other, from_customer_id=w.customer_id
    )

    assert resp.status_code == 422, resp.text


async def test_transferring_to_the_current_holder_is_refused(client):
    w = await world(client)

    resp = await transfer(
        client, w.purchase["id"], to_customer_id=w.customer_id, from_customer_id=w.customer_id
    )

    assert resp.status_code == 422, resp.text


async def test_a_suppressed_target_is_refused(client):
    w = await world(client)
    other = await make_second_customer(client)
    erased = await client.post(f"/api/customers/{other}/erasure", json={})
    assert erased.status_code == 201, erased.text

    resp = await transfer(
        client, w.purchase["id"], to_customer_id=other, from_customer_id=w.customer_id
    )

    assert resp.status_code == 422, resp.text


async def test_a_missing_target_is_refused(client):
    w = await world(client)
    missing = "00000000-0000-0000-0000-000000000000"

    resp = await transfer(
        client, w.purchase["id"], to_customer_id=missing, from_customer_id=w.customer_id
    )

    assert resp.status_code == 404, resp.text


async def test_a_stale_from_customer_is_refused(client):
    """A second admin's request naming the *pre-transfer* holder, after the first transfer
    already landed — the same 409 a genuinely concurrent race produces, on the sequential
    path (spec #96 story 17)."""
    w = await world(client)
    b = await make_second_customer(client)
    first = await transfer(
        client, w.purchase["id"], to_customer_id=b, from_customer_id=w.customer_id
    )
    assert first.status_code == 201, first.text
    c = await make_second_customer(client, first="Ana", last="Woo", phone="416-555-0111")

    stale = await transfer(
        client, w.purchase["id"], to_customer_id=c, from_customer_id=w.customer_id
    )

    assert stale.status_code == 409, stale.text
    assert await scalar("SELECT count(*) FROM package_transfers") == 1


async def test_an_unknown_purchase_is_404(client):
    w = await world(client)
    other = await make_second_customer(client)

    resp = await transfer(
        client, w.customer_id, to_customer_id=other, from_customer_id=w.customer_id
    )

    assert resp.status_code == 404, resp.text


async def test_transfer_needs_billing_manage_in_admin_mode(client):
    w = await world(client)
    other = await make_second_customer(client)
    client.cookies.clear()
    login = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200, login.text

    resp = await transfer(
        client, w.purchase["id"], to_customer_id=other, from_customer_id=w.customer_id
    )

    assert resp.status_code == 403, resp.text
    assert await scalar("SELECT count(*) FROM package_transfers") == 0


# --- concurrency: the purchase row lock serialises two transfers ----------------------------


async def test_a_genuinely_concurrent_second_transfer_is_refused(client, monkeypatch):
    w = await world(client)
    b = await make_second_customer(client)
    c = await make_second_customer(client, first="Ana", last="Woo", phone="416-555-0111")
    cookie = client.cookies["linsuite_session"]

    from billing import package_transfer
    from main import app as main_app

    real = package_transfer.holder_id_for

    async def slow(*args, **kwargs):
        result = await real(*args, **kwargs)
        await asyncio.sleep(0.3)
        return result

    monkeypatch.setattr(package_transfer, "holder_id_for", slow)

    async def call(to_customer_id: str, delay: float):
        await asyncio.sleep(delay)
        async with AsyncClient(transport=ASGITransport(app=main_app), base_url="http://test") as c2:
            c2.cookies.set("linsuite_session", cookie)
            return await transfer(
                c2, w.purchase["id"], to_customer_id=to_customer_id, from_customer_id=w.customer_id
            )

    first, second = await asyncio.gather(call(b, 0), call(c, 0.05))

    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [201, 409], (first.text, second.text)
    assert await scalar("SELECT count(*) FROM package_transfers") == 1


# --- checkout follows the current holder; chains end at the last hop ------------------------


async def test_after_transfer_checkout_offers_the_new_holder_and_not_the_old(client):
    w = await world(client, credits=5, price_cents=50000)
    b = await make_second_customer(client)
    assert (
        await transfer(client, w.purchase["id"], to_customer_id=b, from_customer_id=w.customer_id)
    ).status_code == 201

    old_holder_appt = await book(client, w, slot=0, customer_id=w.customer_id)
    assert await credits_left(client, old_holder_appt) == []

    new_holder_appt = await book(client, w, slot=1, customer_id=b)
    offered = await credits_left(client, new_holder_appt)
    assert len(offered) == 1 and offered[0]["package_purchase_id"] == w.purchase["id"]
    assert (await complete(client, new_holder_appt, w.purchase["id"])).status_code == 200


async def test_a_chain_a_to_b_to_c_ends_with_c(client):
    w = await world(client, credits=5, price_cents=50000)
    b = await make_second_customer(client)
    c = await make_second_customer(client, first="Ana", last="Woo", phone="416-555-0111")
    assert (
        await transfer(client, w.purchase["id"], to_customer_id=b, from_customer_id=w.customer_id)
    ).status_code == 201
    second = await transfer(client, w.purchase["id"], to_customer_id=c, from_customer_id=b)
    assert second.status_code == 201, second.text
    assert len(second.json()["transfers"]) == 2

    b_appt = await book(client, w, slot=0, customer_id=b)
    assert await credits_left(client, b_appt) == []

    c_appt = await book(client, w, slot=1, customer_id=c)
    assert len(await credits_left(client, c_appt)) == 1


# --- liability report -------------------------------------------------------------------------


async def test_the_liability_report_names_the_current_holder(client):
    w = await world(client, credits=5, price_cents=50000)
    b = await make_second_customer(client)
    assert (
        await transfer(client, w.purchase["id"], to_customer_id=b, from_customer_id=w.customer_id)
    ).status_code == 201

    report = await client.get("/api/admin/reports/package-liability")
    assert report.status_code == 200, report.text
    body = report.json()
    holder_ids = {row["customer_id"] for row in body["rows"]}
    assert holder_ids == {b}
    assert w.customer_id not in holder_ids


# --- refund after transfer: still the purchaser, still voids the credits --------------------


async def test_a_refund_after_transfer_refunds_the_purchaser_and_voids_credits(client):
    w = await world(client, credits=5, price_cents=50000)
    b = await make_second_customer(client)
    assert (
        await transfer(client, w.purchase["id"], to_customer_id=b, from_customer_id=w.customer_id)
    ).status_code == 201

    resp = await refund(client, w.purchase["id"])

    assert resp.status_code == 201, resp.text
    assert resp.json()["credits_voided"] is True
    b_appt = await book(client, w, slot=0, customer_id=b)
    assert await credits_left(client, b_appt) == []


# --- audit: no reason text -------------------------------------------------------------------


async def test_the_audit_event_carries_no_reason_text(client):
    w = await world(client)
    other = await make_second_customer(client)
    secret_reason = "Client's cousin will use the remaining sessions instead"

    resp = await transfer(
        client,
        w.purchase["id"],
        to_customer_id=other,
        from_customer_id=w.customer_id,
        reason=secret_reason,
    )
    assert resp.status_code == 201, resp.text

    async with session_scope() as db:
        row = (
            await db.execute(
                text("SELECT metadata FROM audit_events WHERE event_type = 'package.transferred'")
            )
        ).one()
    metadata = row[0]
    assert set(metadata) == {"purchase_id", "from_customer_id", "to_customer_id", "override"}
    assert secret_reason not in str(metadata)
    stored_reason = await scalar("SELECT reason FROM package_transfers LIMIT 1")
    assert stored_reason == secret_reason


# --- client package-purchases list: received / transferred-out, with the chain --------------


async def test_the_client_list_shows_received_and_transferred_out_purchases(client):
    w = await world(client, credits=5, price_cents=50000)
    b = await make_second_customer(client)
    assert (
        await transfer(
            client,
            w.purchase["id"],
            to_customer_id=b,
            from_customer_id=w.customer_id,
            reason="Client moved",
        )
    ).status_code == 201

    a_list = await package_purchases_of(client, w.customer_id)
    assert len(a_list) == 1
    a_row = a_list[0]
    assert a_row["held_by_viewer"] is False
    assert a_row["current_holder_id"] == b
    assert a_row["customer_id"] == w.customer_id  # the purchaser, unchanged
    assert len(a_row["transfers"]) == 1
    assert a_row["transfers"][0]["to_customer_id"] == b

    b_list = await package_purchases_of(client, b)
    assert len(b_list) == 1
    b_row = b_list[0]
    assert b_row["held_by_viewer"] is True
    assert b_row["current_holder_id"] == b
    assert b_row["purchaser_name"] != b_row["current_holder_name"]


# --- receipt: the holder is named, never the purchaser ---------------------------------------


async def test_the_treatment_receipt_names_the_holder_never_the_purchaser(client):
    w = await world(client, credits=5, price_cents=50000)
    b = await make_second_customer(client, first="Sam", last="Lee", phone="416-555-0100")
    assert (
        await transfer(client, w.purchase["id"], to_customer_id=b, from_customer_id=w.customer_id)
    ).status_code == 201

    appointment_id = await book(client, w, slot=0, customer_id=b)
    assert (await complete(client, appointment_id, w.purchase["id"])).status_code == 200

    import uuid

    from billing.documents import receipt_status, render_receipt_html
    from billing.models import Invoice
    from billing.payments import balance
    from core.models import Business
    from scheduling.models import Appointment

    async with session_scope() as db:
        bill_id = await db.scalar(
            text("SELECT bill_id FROM service_bill_lines WHERE appointment_id = :a"),
            {"a": appointment_id},
        )
    issued = await client.post(f"/api/bills/{bill_id}/issue", json={})
    assert issued.status_code == 201, issued.text

    async with session_scope() as db:
        loaded = await db.get(Invoice, uuid.UUID(issued.json()["id"]))
        appointment = await db.get(Appointment, uuid.UUID(appointment_id))
        business = await db.get(Business, 1)
        status = receipt_status(loaded, loaded.lines[0], await balance(db, loaded))
        html = render_receipt_html(
            loaded, loaded.lines[0], appointment, business, 1, payment_status=status
        )

    assert "Sam Lee" in html  # the holder
    assert "Priya" not in html and "Nair" not in html  # the purchaser (world()'s CUSTOMER)


# --- database enforcement: append-only, and the purge role's default-deny -------------------


async def test_the_database_refuses_any_rewrite_of_package_transfers(client):
    w = await world(client)
    other = await make_second_customer(client)
    assert (
        await transfer(
            client, w.purchase["id"], to_customer_id=other, from_customer_id=w.customer_id
        )
    ).status_code == 201

    for statement in (
        "UPDATE package_transfers SET reason = 'x'",
        "DELETE FROM package_transfers",
    ):
        with pytest.raises(DBAPIError):
            async with session_scope() as db:
                await db.execute(text(statement))
                await db.commit()
    assert await scalar("SELECT count(*) FROM package_transfers") == 1


async def test_the_app_role_is_refused_even_with_the_grant_restored(client):
    w = await world(client)
    other = await make_second_customer(client)
    assert (
        await transfer(
            client, w.purchase["id"], to_customer_id=other, from_customer_id=w.customer_id
        )
    ).status_code == 201

    owner = create_async_engine(os.environ["DATABASE_URL_MIGRATE"])
    try:
        async with owner.connect() as conn:
            await conn.begin()
            try:
                await conn.execute(
                    text("GRANT UPDATE, DELETE ON package_transfers TO linsuite_app")
                )
                await conn.execute(text("SET ROLE linsuite_app"))
                with pytest.raises(DBAPIError) as refused:
                    await conn.execute(text("UPDATE package_transfers SET reason = 'x'"))
                assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value
            finally:
                await conn.rollback()
    finally:
        await owner.dispose()

    async with session_scope() as db:
        with pytest.raises(DBAPIError) as still_refused:
            await db.execute(text("UPDATE package_transfers SET reason = 'x'"))
        await db.rollback()
    assert getattr(still_refused.value.orig, "sqlstate", None) == "42501"


async def test_the_purge_role_holds_select_only_on_package_transfers(client):
    w = await world(client)
    other = await make_second_customer(client)
    assert (
        await transfer(
            client, w.purchase["id"], to_customer_id=other, from_customer_id=w.customer_id
        )
    ).status_code == 201

    with pytest.raises(DBAPIError) as refused:
        async with get_purge_engine().begin() as purge:
            await purge.execute(text("DELETE FROM package_transfers"))
    assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value
    assert await scalar("SELECT count(*) FROM package_transfers") == 1

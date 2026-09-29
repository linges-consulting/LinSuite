"""S1: package credit redemption at appointment completion (#72), over a real PostgreSQL.

Selection before completion, one credit redeemed in the completion transaction, the draft line
shown as prepaid (not a new charge), booking/cancel never deducting, the last-credit race, the
database's own refusal to overspend, and the prepaid treatment receipt.
"""

import asyncio
import os
from datetime import date, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from core.db import session_scope
from scheduling.clock import today_in
from tests.conftest import get_owner_engine, wipe_document_keys
from tests.test_bill_review import (
    APPOINTMENTS,
    EMAIL,
    SETUP,
    STAFF,
    STAFF_EMAIL,
    as_admin,
    make_customer,
    make_service,
    me_staff_id,
)

PACKAGES = "/api/admin/packages"
TAX_COMPONENTS = "/api/admin/billing/tax-components"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async def wipe():
        # #83: none of these are among the purge role's four permitted tables — reset as the
        # schema owner instead.
        async with get_owner_engine().begin() as owner:
            for table in (
                "audit_events",
                "erasure_requests",
                "form_links",
                "package_credit_redemptions",
                "package_credit_voids",  # #73
                "package_purchase_credits",
                "invoice_payment_transfers",  # #68
                "retail_return_lines",  # #76
                "retail_returns",  # #76
                "invoice_refunds",  # #67
                "invoice_payments",
                "invoice_balance_authorizations",
                "commission_postings",
                "invoice_line_taxes",
                "invoice_line_discounts",
                "invoice_lines",
                "invoices",
                "package_purchases",
                "business_invoice_counters",
            ):
                await owner.execute(text(f"DELETE FROM {table}"))
        await wipe_document_keys()
        async with session_scope() as db:
            for table in (
                "bill_override_requests",
                "service_bill_discounts",
                "tax_component_rates",
                "tax_components",
                "service_bill_lines",
                "service_bills",
                "package_definition_services",
                "package_definitions",
                "queue_entries",
                "appointment_resources",
                "appointments",
                "customers",
                "service_requirements",
                "service_staff",
                "services",
                "working_hours",
                "time_off",
                "closures",
                "resources",
                "staff",
                "password_reset_tokens",
                "users",
                "businesses",
                "setup_token",
            ):
                await db.execute(text(f"DELETE FROM {table}"))
            await db.execute(text("DELETE FROM roles WHERE NOT is_system"))
            await db.commit()

    await wipe()
    from auth import setup, throttle
    from core.redis import get_redis

    await get_redis().delete(*(throttle.delay_key(e) for e in (EMAIL, STAFF_EMAIL)))
    async with session_scope() as db:
        token = await setup.bootstrap_setup_token(db)
    resp = await client.post("/api/setup", json={**SETUP, "token": token})
    assert resp.status_code == 201, resp.text
    async with session_scope() as db:
        await db.execute(text("UPDATE businesses SET mfa_required_for_admin = false"))
        await db.commit()
    client.cookies.clear()
    yield
    await wipe()


# --- helpers --------------------------------------------------------------------------------


class World:
    """One practitioner, one service, one client, and a purchased package of that service."""

    staff_id: str
    service_id: str
    customer_id: str
    purchase: dict
    slots: list[str]


async def world(client, *, credits: int = 10, price_cents: int = 96000, pay: bool = True):
    await as_admin(client)
    w = World()
    w.staff_id = await me_staff_id(client)
    resp = await client.put(
        f"{STAFF}/{w.staff_id}/hours",
        json={"blocks": [{"weekday": 0, "start_minute": 540, "end_minute": 1020}]},
    )
    assert resp.status_code == 200, resp.text
    w.service_id = await make_service(client, [w.staff_id], price_cents=12000)
    w.customer_id = await make_customer(client)
    monday = date.today() + timedelta(days=7 + (7 - date.today().weekday()) % 7)
    slots = await client.get(
        "/api/availability",
        params={"service_id": w.service_id, "from": monday.isoformat(), "to": monday.isoformat()},
    )
    assert slots.status_code == 200, slots.text
    # Every other hour, so no two booked slots ever overlap for the one practitioner.
    starts = [s["starts_at"] for s in slots.json()["days"][0]["slots"]]
    w.slots = [s for s in starts if datetime.fromisoformat(s).minute == 0][::2]
    w.purchase = await buy(client, w, credits=credits, price_cents=price_cents, pay=pay)
    return w


async def buy(client, w: World, *, credits: int, price_cents: int, pay: bool) -> dict:
    package = await client.post(
        PACKAGES,
        json={
            "name": f"{credits}-Session Pack",
            "description": "",
            "price_cents": price_cents,
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


async def pay_invoice(client, invoice_id: str, amount_cents: int) -> None:
    resp = await client.post(
        f"/api/invoices/{invoice_id}/payments",
        json={"payer_type": "client", "method": "cash", "amount_cents": amount_cents},
    )
    assert resp.status_code == 201, resp.text


async def book(client, w: World, slot: int = 0, customer_id: str | None = None) -> str:
    resp = await client.post(
        APPOINTMENTS,
        json={
            "service_id": w.service_id,
            "staff_id": w.staff_id,
            "starts_at": w.slots[slot],
            "customer_id": customer_id or w.customer_id,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def complete(client, appointment_id: str, purchase_id: str | None):
    return await client.post(
        f"{APPOINTMENTS}/{appointment_id}/complete", json={"package_purchase_id": purchase_id}
    )


async def credits_left(client, appointment_id: str) -> list[dict]:
    resp = await client.get(f"{APPOINTMENTS}/{appointment_id}/package-credits")
    assert resp.status_code == 200, resp.text
    return resp.json()["credits"]


async def redemption_count() -> int:
    async with session_scope() as db:
        return await db.scalar(text("SELECT count(*) FROM package_credit_redemptions"))


async def status_of(appointment_id: str) -> str:
    async with session_scope() as db:
        return await db.scalar(
            text("SELECT status FROM appointments WHERE id = :a"), {"a": appointment_id}
        )


async def bill_of(client, appointment_id: str) -> dict:
    async with session_scope() as db:
        bill_id = await db.scalar(
            text("SELECT bill_id FROM service_bill_lines WHERE appointment_id = :a"),
            {"a": appointment_id},
        )
    resp = await client.get(f"/api/bills/{bill_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- selection and redemption ---------------------------------------------------------------


async def test_an_eligible_paid_package_is_offered_before_completion(client):
    w = await world(client, credits=10, price_cents=96000)
    appointment_id = await book(client, w)

    offered = await credits_left(client, appointment_id)

    assert [
        (c["package_purchase_id"], c["credits_remaining"], c["value_cents"]) for c in offered
    ] == [(w.purchase["id"], 10, 9600)]


async def test_completion_redeems_one_credit_and_the_draft_line_is_prepaid(client):
    w = await world(client, credits=10, price_cents=96000)
    # A tax component exists: the prepaid line must not be taxed a second time.
    tax = await client.post(
        TAX_COMPONENTS,
        json={
            "code": "gst",
            "name": "GST",
            "province": None,
            "rate_bp": 500,
            "effective_from": "2024-01-01",
        },
    )
    assert tax.status_code == 201, tax.text
    appointment_id = await book(client, w)

    resp = await complete(client, appointment_id, w.purchase["id"])

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"
    assert await redemption_count() == 1
    bill = await bill_of(client, appointment_id)
    [line] = bill["lines"]
    assert line["price_cents"] == 9600  # the frozen session value, not the 12000 list price
    assert line["prepaid_cents"] == 9600
    assert line["tax"]["tax_cents"] == 0
    assert line["applied_discount_ids"] == []
    assert bill["prepaid_total_cents"] == bill["grand_total_cents"] == 9600

    second = await book(client, w, slot=1)
    assert (await credits_left(client, second))[0]["credits_remaining"] == 9

    async with session_scope() as db:
        event = await db.scalar(
            text("SELECT count(*) FROM audit_events WHERE event_type = 'package_credit.redeemed'")
        )
    assert event == 1


async def test_the_issued_invoice_owes_nothing_and_commission_goes_to_the_practitioner(client):
    w = await world(client, credits=10, price_cents=96000)
    appointment_id = await book(client, w)
    assert (await complete(client, appointment_id, w.purchase["id"])).status_code == 200
    bill = await bill_of(client, appointment_id)

    issued = await client.post(f"/api/bills/{bill['id']}/issue", json={})

    assert issued.status_code == 201, issued.text
    invoice = issued.json()
    assert invoice["grand_total_cents"] == 9600
    assert invoice["lines"][0]["prepaid_cents"] == 9600
    assert invoice["prepaid_cents"] == 9600
    assert invoice["outstanding_cents"] == 0
    assert invoice["client_outstanding_cents"] == 0
    assert invoice["checkout_complete"] is True
    async with session_scope() as db:
        staff_id, basis = (
            await db.execute(text("SELECT staff_id::text, basis_cents FROM commission_postings"))
        ).one()
    assert (staff_id, basis) == (w.staff_id, 9600)


async def test_a_cancelled_and_reissued_prepaid_invoice_still_owes_nothing(client):
    # #68 x #72: the reopened bill keeps its prepaid line, so the replacement freezes the same
    # prepaid share; the cancelled original bills nothing — neither goes negative or owes.
    w = await world(client, credits=10, price_cents=96000)
    appointment_id = await book(client, w)
    assert (await complete(client, appointment_id, w.purchase["id"])).status_code == 200
    bill = await bill_of(client, appointment_id)
    original = (await client.post(f"/api/bills/{bill['id']}/issue", json={})).json()
    cancelled = await client.post(
        f"/api/invoices/{original['id']}/cancel", json={"reason": "Wrong practitioner"}
    )
    assert cancelled.status_code == 200, cancelled.text

    replacement = await client.post(f"/api/bills/{bill['id']}/issue", json={})

    assert replacement.status_code == 201, replacement.text
    body = replacement.json()
    assert (body["prepaid_cents"], body["outstanding_cents"]) == (9600, 0)
    assert body["checkout_complete"] is True
    after = (await client.get(f"/api/invoices/{original['id']}")).json()
    assert after["outstanding_cents"] == 0
    assert await redemption_count() == 1


async def test_completion_without_a_selection_charges_normally(client):
    w = await world(client)
    appointment_id = await book(client, w)

    assert (await complete(client, appointment_id, None)).status_code == 200

    assert await redemption_count() == 0
    [line] = (await bill_of(client, appointment_id))["lines"]
    assert (line["price_cents"], line["prepaid_cents"]) == (12000, 0)


async def test_session_values_sum_exactly_to_the_allocation(client):
    w = await world(client, credits=3, price_cents=10000)
    values = []
    for slot in (0, 1, 2):
        appointment_id = await book(client, w, slot=slot)
        assert (await complete(client, appointment_id, w.purchase["id"])).status_code == 200
        values.append((await bill_of(client, appointment_id))["lines"][0]["prepaid_cents"])

    assert values == [3334, 3333, 3333]


async def test_a_bundle_values_each_session_by_price_times_credits(client):
    """Spec §152 end to end: assessment 120 ×1 + follow-up 60 ×2, bundle 200 -> the three
    redeemed sessions carry 100 / 50 / 50."""
    w = await world(client, credits=1, pay=False)  # assessment = w.service_id at 120
    follow_up = await make_service(client, [w.staff_id], name="Follow-up", price_cents=6000)
    package = await client.post(
        PACKAGES,
        json={
            "name": "Assessment Bundle",
            "description": "",
            "price_cents": 20000,
            "services": [
                {"service_id": w.service_id, "credits": 1},
                {"service_id": follow_up, "credits": 2},
            ],
        },
    )
    assert package.status_code == 201, package.text
    bought = await client.post(
        f"/api/packages/{package.json()['id']}/purchase", json={"customer_id": w.customer_id}
    )
    assert bought.status_code == 201, bought.text
    await pay_invoice(client, bought.json()["invoice_id"], bought.json()["grand_total_cents"])

    values = []
    for slot, service_id in enumerate((w.service_id, follow_up, follow_up)):
        resp = await client.post(
            APPOINTMENTS,
            json={
                "service_id": service_id,
                "staff_id": w.staff_id,
                "starts_at": w.slots[slot],
                "customer_id": w.customer_id,
            },
        )
        assert resp.status_code == 201, resp.text
        appointment_id = resp.json()["id"]
        done = await complete(client, appointment_id, bought.json()["id"])
        assert done.status_code == 200, done.text
        values.append((await bill_of(client, appointment_id))["lines"][0]["prepaid_cents"])

    assert values == [10000, 5000, 5000]


# --- only completion deducts -----------------------------------------------------------------


async def test_booking_and_cancelling_never_deduct_a_credit(client):
    w = await world(client, credits=2)
    appointment_id = await book(client, w)
    cancelled = await client.post(f"{APPOINTMENTS}/{appointment_id}/cancel", json={})
    assert cancelled.status_code == 200, cancelled.text
    another = await book(client, w, slot=1)

    assert await redemption_count() == 0
    assert (await credits_left(client, another))[0]["credits_remaining"] == 2


# --- eligibility refusals leave the appointment untouched -----------------------------------


async def assert_refused(client, appointment_id: str, purchase_id: str, status: int) -> None:
    resp = await complete(client, appointment_id, purchase_id)
    assert resp.status_code == status, resp.text
    assert await status_of(appointment_id) == "confirmed"
    assert await redemption_count() == 0
    async with session_scope() as db:
        assert await db.scalar(text("SELECT count(*) FROM service_bill_lines")) == 0


async def test_a_partially_paid_package_is_not_offered_or_redeemable(client):
    w = await world(client, pay=False)
    await pay_invoice(client, w.purchase["invoice_id"], w.purchase["grand_total_cents"] - 1)
    appointment_id = await book(client, w)

    assert await credits_left(client, appointment_id) == []
    await assert_refused(client, appointment_id, w.purchase["id"], 422)


async def test_full_payment_activates_the_credits(client):
    w = await world(client, pay=False)
    appointment_id = await book(client, w)
    await pay_invoice(client, w.purchase["invoice_id"], 1000)
    assert await credits_left(client, appointment_id) == []

    await pay_invoice(client, w.purchase["invoice_id"], w.purchase["grand_total_cents"] - 1000)

    assert len(await credits_left(client, appointment_id)) == 1


async def test_another_clients_package_is_refused(client):
    w = await world(client)
    other = await client.post(
        "/api/customers", json={"first_name": "Sam", "last_name": "Lee", "phone": "416-555-0100"}
    )
    assert other.status_code == 201, other.text
    appointment_id = await book(client, w, customer_id=other.json()["id"])

    assert await credits_left(client, appointment_id) == []
    await assert_refused(client, appointment_id, w.purchase["id"], 422)


async def test_a_package_for_another_service_is_refused(client):
    w = await world(client)
    w.service_id = await make_service(client, [w.staff_id], name="Reflexology")
    appointment_id = await book(client, w)

    await assert_refused(client, appointment_id, w.purchase["id"], 422)


async def test_an_expired_package_is_refused(client):
    w = await world(client)
    owner = create_async_engine(os.environ["DATABASE_URL_MIGRATE"])
    try:
        async with owner.begin() as conn:
            # Yesterday on the business's own calendar — never Postgres's UTC `current_date`,
            # which is a day ahead every evening and made this test flake by the hour.
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
    appointment_id = await book(client, w)

    await assert_refused(client, appointment_id, w.purchase["id"], 422)


async def test_an_unknown_purchase_is_404(client):
    w = await world(client)
    appointment_id = await book(client, w)

    await assert_refused(client, appointment_id, w.customer_id, 404)


async def test_the_last_credit_cannot_be_spent_twice(client):
    w = await world(client, credits=1, price_cents=5000)
    first, second = await book(client, w, slot=0), await book(client, w, slot=1)
    assert (await complete(client, first, w.purchase["id"])).status_code == 200

    resp = await complete(client, second, w.purchase["id"])

    assert resp.status_code == 409, resp.text
    assert await status_of(second) == "confirmed"
    assert await redemption_count() == 1


# --- the last-credit race ---------------------------------------------------------------------


async def test_two_concurrent_completions_race_for_the_last_credit(client, monkeypatch):
    """Two ASGI clients complete two appointments against a one-credit package at once. The
    sleep inside the real eligibility count keeps the first transaction's purchase-row lock
    held while the second arrives, so the second genuinely waits on Postgres, recounts, and
    loses — never a negative balance."""
    w = await world(client, credits=1, price_cents=5000)
    first, second = await book(client, w, slot=0), await book(client, w, slot=1)
    cookie = client.cookies["linsuite_session"]

    from billing import redemption
    from main import app as main_app

    real_eligible = redemption._eligible

    async def slow_eligible(*args, **kwargs):
        result = await real_eligible(*args, **kwargs)
        await asyncio.sleep(0.25)
        return result

    monkeypatch.setattr(redemption, "_eligible", slow_eligible)

    async def attempt(appointment_id: str):
        async with AsyncClient(transport=ASGITransport(app=main_app), base_url="http://test") as c:
            c.cookies.set("linsuite_session", cookie)
            return await complete(c, appointment_id, w.purchase["id"])

    results = await asyncio.gather(attempt(first), attempt(second))

    assert sorted(r.status_code for r in results) == [200, 409], [r.text for r in results]
    assert await redemption_count() == 1
    assert sorted([await status_of(first), await status_of(second)]) == ["completed", "confirmed"]


# --- database enforcement ---------------------------------------------------------------------


async def test_the_database_refuses_an_overspend_and_any_rewrite(client):
    w = await world(client, credits=1, price_cents=5000)
    first, second = await book(client, w, slot=0), await book(client, w, slot=1)
    assert (await complete(client, first, w.purchase["id"])).status_code == 200

    insert = (
        "INSERT INTO package_credit_redemptions (package_purchase_id, service_id, sequence, "
        "appointment_id, value_cents, redeemed_by) SELECT package_purchase_id, service_id, "
        ":seq, :a, 0, redeemed_by FROM package_credit_redemptions"
    )
    for seq in (1, 2):  # the same credit again; a credit past `credits_total`
        with pytest.raises(DBAPIError):
            async with session_scope() as db:
                await db.execute(text(insert), {"seq": seq, "a": second})
                await db.commit()
    for statement in (
        "UPDATE package_credit_redemptions SET value_cents = 1",
        "DELETE FROM package_credit_redemptions",
    ):
        with pytest.raises(DBAPIError):
            async with session_scope() as db:
                await db.execute(text(statement))
                await db.commit()
    assert await redemption_count() == 1


async def test_the_database_refuses_redeeming_an_unactivated_purchase(client):
    w = await world(client, pay=False)
    appointment_id = await book(client, w)
    async with session_scope() as db:
        user_id = await db.scalar(text("SELECT id FROM users LIMIT 1"))
    with pytest.raises(DBAPIError, match="not activated"):
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


# --- the prepaid treatment receipt -----------------------------------------------------------


async def test_the_treatment_receipt_shows_the_prepaid_value_and_service_date(client):
    w = await world(client, credits=10, price_cents=96000)
    appointment_id = await book(client, w)
    assert (await complete(client, appointment_id, w.purchase["id"])).status_code == 200
    bill = await bill_of(client, appointment_id)
    invoice = (await client.post(f"/api/bills/{bill['id']}/issue", json={})).json()

    import uuid
    from zoneinfo import ZoneInfo

    from billing.documents import receipt_status, render_receipt_html
    from billing.models import Invoice
    from billing.payments import balance
    from core.models import Business
    from scheduling.models import Appointment

    async with session_scope() as db:
        loaded = await db.get(Invoice, uuid.UUID(invoice["id"]))
        appointment = await db.get(Appointment, uuid.UUID(appointment_id))
        business = await db.get(Business, 1)
        status = receipt_status(loaded, loaded.lines[0], await balance(db, loaded))
        html = render_receipt_html(
            loaded, loaded.lines[0], appointment, business, 1, payment_status=status
        )
        # Fully prepaid means checkout is complete at issue: the receipt is already stored.
        stored = await db.scalar(
            text("SELECT kind FROM documents WHERE source_id = :l"), {"l": loaded.lines[0].id}
        )
        service_date = appointment.starts_at.astimezone(ZoneInfo(business.timezone)).date()

    assert stored == "treatment_receipt:prepaid"

    assert "$96.00" in html
    assert "Prepaid (package credit)" in html
    assert "Paid<" not in html
    assert f"Date of service: {service_date.isoformat()}" in html

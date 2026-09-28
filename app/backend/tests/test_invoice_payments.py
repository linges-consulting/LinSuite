"""S1(+S5): manual payment ledger + checkout gate (#66) — recording split payments against a
real issued invoice, the derived outstanding balance / checkout-complete predicate, the
insurer pending-vs-received distinction, and the admin/owner outstanding-balance exception,
over a real PostgreSQL.

Reuses `tests/test_invoice_issue.py`'s own fixture/helper chain (the only way to get a real
issued invoice is to book, confirm, complete and issue a real appointment) — this file defines
its own `claimed_instance` for the same reason that one does: the two new append-only tables
(migration 0058) must be wiped, purge-role-bypassed, before `invoices`/`service_bills` and
everything under them.
"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.db import get_purge_engine, session_scope
from tests.conftest import add_account, wipe_document_keys
from tests.test_bill_review import (
    EMAIL,
    SETUP,
    STAFF_EMAIL,
    STAFF_PASSWORD,
    add_front_desk_account,
    as_admin,
    as_staff,
    complete_a_visit,
)

INVOICES = "/api/invoices"


def issue_url(bill_id: str) -> str:
    return f"/api/bills/{bill_id}/issue"


def payments_url(invoice_id: str) -> str:
    return f"{INVOICES}/{invoice_id}/payments"


def exceptions_url(invoice_id: str) -> str:
    return f"{INVOICES}/{invoice_id}/balance-exceptions"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async def wipe():
        async with get_purge_engine().begin() as purge:
            await purge.execute(text("DELETE FROM audit_events"))
            await purge.execute(text("DELETE FROM erasure_requests"))
            await purge.execute(text("DELETE FROM form_links"))
            # #66's own append-only tables — must go before `invoices` (`ON DELETE CASCADE`
            # would handle it, but the purge role deletes explicitly the same way #65's own
            # fixture does for its four tables).
            await purge.execute(text("DELETE FROM invoice_balance_authorizations"))
            await purge.execute(text("DELETE FROM invoice_payments"))
            await purge.execute(text("DELETE FROM commission_postings"))  # #69
            await purge.execute(text("DELETE FROM invoice_line_taxes"))
            await purge.execute(text("DELETE FROM invoice_line_discounts"))
            await purge.execute(text("DELETE FROM invoice_lines"))
            await purge.execute(text("DELETE FROM invoice_refunds"))  # #67
            await purge.execute(text("DELETE FROM invoices"))
            await purge.execute(text("DELETE FROM business_invoice_counters"))
        await wipe_document_keys()
        async with session_scope() as db:
            for table in (
                "bill_override_requests",
                "service_bill_discounts",
                "discount_eligible_items",
                "discounts",
                "tax_component_rates",
                "tax_components",
                "service_bill_lines",
                "service_bills",
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


async def issue_an_invoice(client, *, price_cents: int = 12000) -> str:
    """Books, confirms, completes and issues one appointment end to end, and returns the
    invoice id — as an admin, so this can be reused whether the test then wants to keep acting
    as admin or switch to front-desk staff."""
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client, price_cents=price_cents)
    resp = await client.post(issue_url(bill_id), json={})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# --- recording payments, the happy paths ------------------------------------------------------


async def test_a_single_cash_payment_settles_the_balance_and_completes_checkout(client):
    invoice_id = await issue_an_invoice(client, price_cents=12000)

    resp = await client.post(
        payments_url(invoice_id),
        json={"payer_type": "client", "method": "cash", "amount_cents": 12000},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["payer_type"] == "client"
    assert body["method"] == "cash"
    assert body["status"] == "received"
    assert body["amount_cents"] == 12000

    invoice = await client.get(f"{INVOICES}/{invoice_id}")
    assert invoice.status_code == 200, invoice.text
    assert invoice.json()["outstanding_cents"] == 0
    assert invoice.json()["checkout_complete"] is True


async def test_split_payments_across_methods_sum_to_full_settlement(client):
    invoice_id = await issue_an_invoice(client, price_cents=10000)

    first = await client.post(
        payments_url(invoice_id),
        json={"payer_type": "client", "method": "cash", "amount_cents": 4000},
    )
    second = await client.post(
        payments_url(invoice_id),
        json={
            "payer_type": "client",
            "method": "e_transfer",
            "amount_cents": 3000,
            "reference": "ETRF-001",
        },
    )
    third = await client.post(
        payments_url(invoice_id),
        json={"payer_type": "client", "method": "card", "amount_cents": 3000},
    )
    assert [r.status_code for r in (first, second, third)] == [201, 201, 201]

    invoice = await client.get(f"{INVOICES}/{invoice_id}")
    assert invoice.json()["outstanding_cents"] == 0
    assert invoice.json()["checkout_complete"] is True

    listed = await client.get(payments_url(invoice_id))
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["payments"]) == 3
    assert listed.json()["payments"][1]["reference"] == "ETRF-001"


async def test_a_partial_payment_leaves_checkout_incomplete(client):
    invoice_id = await issue_an_invoice(client, price_cents=10000)

    resp = await client.post(
        payments_url(invoice_id),
        json={"payer_type": "client", "method": "cash", "amount_cents": 4000},
    )
    assert resp.status_code == 201, resp.text

    invoice = await client.get(f"{INVOICES}/{invoice_id}")
    assert invoice.json()["outstanding_cents"] == 6000
    assert invoice.json()["checkout_complete"] is False


async def test_recording_a_payment_is_front_desk_reachable(client):
    """`billing.view`, the same capability #65's own issue route uses — no Admin Mode."""
    invoice_id = await issue_an_invoice(client, price_cents=5000)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)

    resp = await client.post(
        payments_url(invoice_id),
        json={"payer_type": "client", "method": "cash", "amount_cents": 5000},
    )

    assert resp.status_code == 201, resp.text


async def test_recording_a_payment_needs_billing_view(client):
    invoice_id = await issue_an_invoice(client, price_cents=5000)
    role = await client.post("/api/admin/roles", json={"name": "No Billing", "capabilities": []})
    assert role.status_code == 201, role.text
    from core.security import hash_password

    await add_account(
        "nocap@cedar.example", await hash_password(STAFF_PASSWORD), role=role.json()["id"]
    )
    client.cookies.clear()
    await as_staff(client, "nocap@cedar.example", STAFF_PASSWORD)

    resp = await client.post(
        payments_url(invoice_id),
        json={"payer_type": "client", "method": "cash", "amount_cents": 5000},
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "capability_required"


async def test_recording_a_payment_against_a_nonexistent_invoice_404s(client):
    await as_admin(client)

    resp = await client.post(
        payments_url("00000000-0000-0000-0000-000000000000"),
        json={"payer_type": "client", "method": "cash", "amount_cents": 100},
    )

    assert resp.status_code == 404, resp.text


# --- the insurer pending-vs-received distinction -----------------------------------------------


async def test_an_insurer_pending_payment_never_counts_as_received(client):
    invoice_id = await issue_an_invoice(client, price_cents=8000)

    resp = await client.post(
        payments_url(invoice_id),
        json={
            "payer_type": "insurer",
            "method": "insurer",
            "amount_cents": 8000,
            "status": "pending",
            "reference": "CLAIM-42",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "pending"

    invoice = await client.get(f"{INVOICES}/{invoice_id}")
    assert invoice.json()["outstanding_cents"] == 8000  # still visibly unpaid
    assert invoice.json()["pending_insurer_cents"] == 8000
    assert invoice.json()["client_outstanding_cents"] == 0
    # Spec #54 "Checkout gate": pending approved insurer money does not by itself block
    # checkout once the client portion is settled.
    assert invoice.json()["checkout_complete"] is True


async def test_checkout_needs_the_client_portion_settled_beside_a_pending_insurer_amount(client):
    invoice_id = await issue_an_invoice(client, price_cents=8000)
    for payload in (
        {"payer_type": "insurer", "method": "insurer", "amount_cents": 5000, "status": "pending"},
        {"payer_type": "client", "method": "card", "amount_cents": 2000},
    ):
        resp = await client.post(payments_url(invoice_id), json=payload)
        assert resp.status_code == 201, resp.text

    invoice = (await client.get(f"{INVOICES}/{invoice_id}")).json()
    assert invoice["client_outstanding_cents"] == 1000
    assert invoice["checkout_complete"] is False

    resp = await client.post(
        payments_url(invoice_id),
        json={"payer_type": "client", "method": "cash", "amount_cents": 1000},
    )
    assert resp.status_code == 201, resp.text
    invoice = (await client.get(f"{INVOICES}/{invoice_id}")).json()
    assert invoice["checkout_complete"] is True
    assert invoice["outstanding_cents"] == 5000
    assert invoice["pending_insurer_cents"] == 5000

    # Nothing left on the client's side, so there is no client balance to authorize.
    refused = await client.post(exceptions_url(invoice_id), json={"reason": "n/a"})
    assert refused.status_code == 422, refused.text

    # The insurer money arriving later settles the whole invoice.
    resp = await client.post(
        payments_url(invoice_id),
        json={"payer_type": "insurer", "method": "insurer", "amount_cents": 5000},
    )
    assert resp.status_code == 201, resp.text
    invoice = (await client.get(f"{INVOICES}/{invoice_id}")).json()
    assert (invoice["outstanding_cents"], invoice["pending_insurer_cents"]) == (0, 0)


async def test_a_later_received_insurer_entry_settles_the_balance_the_pending_row_never_did(
    client,
):
    invoice_id = await issue_an_invoice(client, price_cents=8000)
    pending = await client.post(
        payments_url(invoice_id),
        json={
            "payer_type": "insurer",
            "method": "insurer",
            "amount_cents": 8000,
            "status": "pending",
        },
    )
    assert pending.status_code == 201, pending.text

    received = await client.post(
        payments_url(invoice_id),
        json={
            "payer_type": "insurer",
            "method": "insurer",
            "amount_cents": 8000,
            "status": "received",
            "reference": "EFT-9001",
        },
    )
    assert received.status_code == 201, received.text

    invoice = await client.get(f"{INVOICES}/{invoice_id}")
    assert invoice.json()["outstanding_cents"] == 0
    assert invoice.json()["checkout_complete"] is True
    listed = await client.get(payments_url(invoice_id))
    assert [p["status"] for p in listed.json()["payments"]] == ["pending", "received"]


async def test_a_client_payment_cannot_be_recorded_pending(client):
    invoice_id = await issue_an_invoice(client, price_cents=5000)

    resp = await client.post(
        payments_url(invoice_id),
        json={
            "payer_type": "client",
            "method": "cash",
            "amount_cents": 5000,
            "status": "pending",
        },
    )

    assert resp.status_code == 422, resp.text


async def test_payer_and_method_must_agree_on_insurer(client):
    invoice_id = await issue_an_invoice(client, price_cents=5000)

    mismatched_a = await client.post(
        payments_url(invoice_id),
        json={"payer_type": "insurer", "method": "cash", "amount_cents": 5000},
    )
    mismatched_b = await client.post(
        payments_url(invoice_id),
        json={"payer_type": "client", "method": "insurer", "amount_cents": 5000},
    )

    assert mismatched_a.status_code == 422, mismatched_a.text
    assert mismatched_b.status_code == 422, mismatched_b.text


# --- the admin/owner outstanding-balance exception ----------------------------------------------


async def test_admin_can_authorize_an_outstanding_balance_and_complete_checkout(client):
    invoice_id = await issue_an_invoice(client, price_cents=10000)
    partial = await client.post(
        payments_url(invoice_id),
        json={"payer_type": "client", "method": "cash", "amount_cents": 4000},
    )
    assert partial.status_code == 201, partial.text

    resp = await client.post(
        exceptions_url(invoice_id), json={"reason": "Client leaving town, will e-transfer later."}
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["outstanding_cents_at_authorization"] == 6000
    assert body["reason"] == "Client leaving town, will e-transfer later."

    invoice = await client.get(f"{INVOICES}/{invoice_id}")
    assert invoice.json()["outstanding_cents"] == 6000
    assert invoice.json()["checkout_complete"] is True


async def test_authorizing_an_exception_needs_billing_manage_admin_mode(client):
    invoice_id = await issue_an_invoice(client, price_cents=10000)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)

    resp = await client.post(exceptions_url(invoice_id), json={"reason": "Trying anyway"})

    assert resp.status_code == 403, resp.text


async def test_authorizing_an_exception_with_nothing_outstanding_is_refused(client):
    invoice_id = await issue_an_invoice(client, price_cents=5000)
    paid = await client.post(
        payments_url(invoice_id),
        json={"payer_type": "client", "method": "cash", "amount_cents": 5000},
    )
    assert paid.status_code == 201, paid.text

    resp = await client.post(exceptions_url(invoice_id), json={"reason": "Nothing to see here"})

    assert resp.status_code == 422, resp.text


async def test_exceptions_are_listed_for_manual_follow_up(client):
    invoice_id = await issue_an_invoice(client, price_cents=10000)
    authorized = await client.post(
        exceptions_url(invoice_id), json={"reason": "Insurer claim in progress"}
    )
    assert authorized.status_code == 201, authorized.text

    listed = await client.get(exceptions_url(invoice_id))

    assert listed.status_code == 200, listed.text
    assert len(listed.json()["exceptions"]) == 1
    assert listed.json()["exceptions"][0]["reason"] == "Insurer claim in progress"


# --- the billing list / client history ----------------------------------------------------------


async def test_the_invoice_list_shows_outstanding_balance_for_manual_follow_up(client):
    invoice_id = await issue_an_invoice(client, price_cents=10000)
    partial = await client.post(
        payments_url(invoice_id),
        json={"payer_type": "client", "method": "cash", "amount_cents": 3000},
    )
    assert partial.status_code == 201, partial.text

    listed = await client.get(INVOICES)

    assert listed.status_code == 200, listed.text
    row = next(i for i in listed.json()["invoices"] if i["id"] == invoice_id)
    assert row["outstanding_cents"] == 7000
    assert row["checkout_complete"] is False


async def test_the_invoice_list_filters_by_customer_for_the_clients_own_history(client):
    invoice_id = await issue_an_invoice(client, price_cents=5000)
    invoice = await client.get(f"{INVOICES}/{invoice_id}")
    customer_id = invoice.json()["customer_id"]

    filtered = await client.get(INVOICES, params={"customer_id": customer_id})
    other = await client.get(
        INVOICES, params={"customer_id": "00000000-0000-0000-0000-000000000000"}
    )

    assert filtered.status_code == 200, filtered.text
    assert [i["id"] for i in filtered.json()["invoices"]] == [invoice_id]
    assert other.json()["invoices"] == []


# --- append-only, by grant and trigger ---------------------------------------------------------


TRIGGERS = {
    "invoice_payments": "invoice_payments_no_rewrite",
    "invoice_balance_authorizations": "invoice_balance_authorizations_no_rewrite",
}


async def test_invoice_payments_is_append_only(client):
    """`REVOKE UPDATE, DELETE` denies the app role outright (`42501`, `insufficient_privilege`)
    before the trigger ever runs — the same precedent `test_invoice_issue.py`'s own `test_the_
    app_role_may_neither_update_nor_delete_an_invoice_line_child_row` checks against, rather
    than matching the trigger's own message text, which the grant denial never reaches."""
    invoice_id = await issue_an_invoice(client, price_cents=5000)
    paid = await client.post(
        payments_url(invoice_id),
        json={"payer_type": "client", "method": "cash", "amount_cents": 5000},
    )
    assert paid.status_code == 201, paid.text
    payment_id = paid.json()["id"]

    async with session_scope() as db:
        with pytest.raises(DBAPIError) as refused:
            await db.execute(
                text("UPDATE invoice_payments SET amount_cents = 1 WHERE id = :id"),
                {"id": payment_id},
            )
        await db.rollback()
        assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value
        with pytest.raises(DBAPIError) as refused:
            await db.execute(
                text("DELETE FROM invoice_payments WHERE id = :id"), {"id": payment_id}
            )
        await db.rollback()
        assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value


async def test_invoice_balance_authorizations_is_append_only(client):
    invoice_id = await issue_an_invoice(client, price_cents=5000)
    authorized = await client.post(exceptions_url(invoice_id), json={"reason": "reasons"})
    assert authorized.status_code == 201, authorized.text
    exception_id = authorized.json()["id"]

    async with session_scope() as db:
        with pytest.raises(DBAPIError) as refused:
            await db.execute(
                text("UPDATE invoice_balance_authorizations SET reason = 'x' WHERE id = :id"),
                {"id": exception_id},
            )
        await db.rollback()
        assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value


@pytest.mark.parametrize("table", ["invoice_payments", "invoice_balance_authorizations"])
async def test_the_guard_trigger_is_attached_and_enabled(client, table):
    async with session_scope() as db:
        enabled = await db.scalar(
            text(
                "SELECT tgenabled::text FROM pg_trigger "
                f"WHERE tgrelid = '{table}'::regclass AND tgname = '{TRIGGERS[table]}'"
            )
        )
    assert enabled == "O"


# --- a genuine concurrency case: two split payments racing on the same invoice -----------------


async def test_two_concurrent_split_payments_on_the_same_invoice_both_land(client):
    """Unlike #65's gapless invoice numbering, nothing here needs a row lock: payments are an
    append-only ledger with no shared mutable counter, so two concurrent inserts are simply two
    independent rows, and `outstanding_cents` re-sums the ledger fresh on every read — there is
    no in-memory total to race on. Two real ASGI clients race to record two different partial
    payments against the same invoice; this proves both land and the read-time `SUM` afterward
    is exactly their total, with no lost row."""
    invoice_id = await issue_an_invoice(client, price_cents=10000)
    cookie = client.cookies["linsuite_session"]

    from main import app as main_app

    async def attempt(amount_cents: int):
        async with AsyncClient(transport=ASGITransport(app=main_app), base_url="http://test") as c:
            c.cookies.set("linsuite_session", cookie)
            return await c.post(
                payments_url(invoice_id),
                json={"payer_type": "client", "method": "cash", "amount_cents": amount_cents},
            )

    results = await asyncio.gather(attempt(4000), attempt(6000))

    assert [r.status_code for r in results] == [201, 201], [r.text for r in results]
    invoice = await client.get(f"{INVOICES}/{invoice_id}")
    assert invoice.json()["outstanding_cents"] == 0
    assert invoice.json()["checkout_complete"] is True
    listed = await client.get(payments_url(invoice_id))
    assert sorted(p["amount_cents"] for p in listed.json()["payments"]) == [4000, 6000]

"""S1: commission posting at invoice issue (#69) — over a real PostgreSQL, reusing `tests/
test_bill_review.py`'s fixture/helpers (the only way to get a real, completed appointment) and
`tests/test_invoice_issue.py`'s own `claimed_instance` shape (the four #65 tables plus this
ticket's `commission_postings` are `ON DELETE RESTRICT` against tables the generic wipe would
otherwise refuse to clear).

The pure composition formula is proven directly, with no database, in `tests/test_billing_
commission.py`; this file proves the wiring — the rate really is the one snapshotted at
completion (not re-read live at issue), the basis really is discount-aware, one row is posted
per invoice line, and the ledger is genuinely append-only.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.db import get_purge_engine, session_scope
from tests.conftest import wipe_document_keys
from tests.test_bill_review import (
    BILLS,
    EMAIL,
    SETUP,
    STAFF,
    STAFF_EMAIL,
    add_front_desk_account,
    as_admin,
    as_staff,
    complete_a_visit,
    make_discount,
    me_staff_id,
)
from tests.test_invoice_issue import INVOICES, issue_url


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async def wipe():
        async with get_purge_engine().begin() as purge:
            await purge.execute(text("DELETE FROM audit_events"))
            await purge.execute(text("DELETE FROM erasure_requests"))
            await purge.execute(text("DELETE FROM form_links"))
            # Children before parents (`ON DELETE RESTRICT` all the way down): the commission
            # ledger references `invoice_lines`/`invoices`, both of which #65's own tables
            # already reference `service_bills`/`appointments`/etc.
            await purge.execute(text("DELETE FROM invoice_payment_transfers"))  # #68
            await purge.execute(text("DELETE FROM commission_postings"))
            await purge.execute(text("DELETE FROM invoice_line_taxes"))
            await purge.execute(text("DELETE FROM invoice_line_discounts"))
            await purge.execute(text("DELETE FROM invoice_lines"))
            await purge.execute(text("DELETE FROM retail_return_lines"))  # #76
            await purge.execute(text("DELETE FROM retail_returns"))  # #76
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


async def set_commission_rate(client, staff_id: str, rate_bp: int) -> None:
    resp = await client.patch(f"{STAFF}/{staff_id}", json={"commission_rate_services_bp": rate_bp})
    assert resp.status_code == 200, resp.text


async def commission_postings(invoice_id: str | None = None) -> list[dict]:
    async with session_scope() as db:
        where = "WHERE invoice_id = :invoice_id" if invoice_id else ""
        rows = (
            await db.execute(
                text(
                    "SELECT id::text, invoice_line_id::text, invoice_id::text, staff_id::text, "
                    "kind, commission_rate_bp, basis_cents, amount_cents, "
                    "reverses_posting_id::text, posted_at "
                    f"FROM commission_postings {where}"
                ),
                {"invoice_id": invoice_id} if invoice_id else {},
            )
        ).mappings()
        return [dict(r) for r in rows]


# --- the rate is the one snapshotted at completion, never re-read live ------------------------


async def test_commission_is_posted_using_the_rate_snapshotted_at_completion(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await set_commission_rate(client, me, 2000)  # 20%, set before completion

    bill_id, service_id = await complete_a_visit(client, price_cents=10000)

    issued = await client.post(issue_url(bill_id), json={})
    assert issued.status_code == 201, issued.text
    invoice_id = issued.json()["id"]

    postings = await commission_postings(invoice_id)
    assert len(postings) == 1
    posting = postings[0]
    assert posting["staff_id"] == me
    assert posting["kind"] == "earned"
    assert posting["commission_rate_bp"] == 2000
    assert posting["basis_cents"] == 10000
    assert posting["amount_cents"] == 2000  # 20% of 10000
    assert posting["reverses_posting_id"] is None


async def test_a_rate_change_after_completion_but_before_issue_does_not_move_the_posting(client):
    """The whole point of #59's M4 reversal: the rate is frozen at completion, not issue.
    Changing it in between must not touch what gets posted."""
    await as_admin(client)
    me = await me_staff_id(client)
    await set_commission_rate(client, me, 1000)  # 10% at completion time

    bill_id, _ = await complete_a_visit(client, price_cents=10000)

    await set_commission_rate(client, me, 9999)  # changed after completion, before issue

    issued = await client.post(issue_url(bill_id), json={})
    assert issued.status_code == 201, issued.text

    posting = (await commission_postings(issued.json()["id"]))[0]
    assert posting["commission_rate_bp"] == 1000
    assert posting["amount_cents"] == 1000


async def test_a_later_rate_change_after_issue_does_not_rewrite_an_already_posted_commission(
    client,
):
    await as_admin(client)
    me = await me_staff_id(client)
    await set_commission_rate(client, me, 1000)
    bill_id, _ = await complete_a_visit(client, price_cents=10000)
    issued = await client.post(issue_url(bill_id), json={})
    assert issued.status_code == 201, issued.text
    invoice_id = issued.json()["id"]

    await set_commission_rate(client, me, 5000)

    posting = (await commission_postings(invoice_id))[0]
    assert posting["commission_rate_bp"] == 1000
    assert posting["amount_cents"] == 1000


# --- commission-basis composition, over the real discount-application flow ---------------------


async def test_a_reduces_discount_lowers_the_commission_basis(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await set_commission_rate(client, me, 1000)  # 10%
    bill_id, _ = await complete_a_visit(client, price_cents=10000)
    discount = await make_discount(
        client, name="Reduces 10%", percentage_bp=1000, commission_basis="reduces"
    )
    applied = await client.put(
        f"{BILLS}/{bill_id}/discounts", json={"discount_ids": [discount["id"]]}
    )
    assert applied.status_code == 200, applied.text

    issued = await client.post(issue_url(bill_id), json={})
    assert issued.status_code == 201, issued.text

    posting = (await commission_postings(issued.json()["id"]))[0]
    # 10000 - 10% = 9000 basis; 10% commission of 9000 = 900.
    assert posting["basis_cents"] == 9000
    assert posting["amount_cents"] == 900


async def test_an_absorbed_discount_leaves_the_commission_basis_at_full_price(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await set_commission_rate(client, me, 1000)  # 10%
    bill_id, _ = await complete_a_visit(client, price_cents=10000)
    discount = await make_discount(
        client, name="Absorbed 10%", percentage_bp=1000, commission_basis="absorbed"
    )
    applied = await client.put(
        f"{BILLS}/{bill_id}/discounts", json={"discount_ids": [discount["id"]]}
    )
    assert applied.status_code == 200, applied.text

    issued = await client.post(issue_url(bill_id), json={})
    assert issued.status_code == 201, issued.text
    # The client was actually charged 9000 (the discount really did apply to the invoice) --
    # but commission is unaffected by it.
    assert issued.json()["grand_total_cents"] == 9000

    posting = (await commission_postings(issued.json()["id"]))[0]
    assert posting["basis_cents"] == 10000
    assert posting["amount_cents"] == 1000


# --- append-only, `invoice_lines`'s exact shape -------------------------------------------------


async def _post_one(client) -> dict:
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    issued = await client.post(issue_url(bill_id), json={})
    assert issued.status_code == 201, issued.text
    return (await commission_postings(issued.json()["id"]))[0]


async def test_the_app_role_may_neither_update_nor_delete_a_commission_posting(client):
    await _post_one(client)
    async with session_scope() as db:
        for statement in (
            "UPDATE commission_postings SET amount_cents = amount_cents",
            "DELETE FROM commission_postings",
        ):
            with pytest.raises(DBAPIError) as refused:
                await db.execute(text(statement))
            await db.rollback()
            assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value


async def test_the_commission_postings_trigger_is_attached_and_enabled(client):
    await _post_one(client)
    async with session_scope() as db:
        enabled = await db.scalar(
            text(
                "SELECT tgenabled::text FROM pg_trigger WHERE tgrelid = "
                "'commission_postings'::regclass AND tgname = 'commission_postings_no_rewrite'"
            )
        )
    assert enabled == "O"


# --- commission is never leaked through the issue response itself ------------------------------


async def test_issuing_as_staff_returns_no_commission_fields_at_all(client):
    """The direct proof for this file's own hook: the same response that confirms a posting
    happened must not itself carry the rate or amount. `tests/test_commission_leakage.py` does
    the exhaustive sweep across every staff-facing endpoint; this is the one this file's own
    flow produces."""
    await as_admin(client)
    me = await me_staff_id(client)
    await set_commission_rate(client, me, 4200)
    bill_id, _ = await complete_a_visit(client, price_cents=10000)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)

    resp = await client.post(issue_url(bill_id), json={})

    assert resp.status_code == 201, resp.text
    body_text = resp.text
    assert "commission" not in body_text.lower()

    # And a subsequent read of the same invoice, also as Staff Mode.
    read = await client.get(f"{INVOICES}/{resp.json()['id']}")
    assert read.status_code == 200, read.text
    assert "commission" not in read.text.lower()

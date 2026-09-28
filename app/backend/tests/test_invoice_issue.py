"""S1(+S5): invoice issue (#65) — turning a reviewed, approved draft `ServiceBill` into an
issued, immutable `Invoice`, over a real PostgreSQL.

Reuses `tests/test_bill_review.py`'s fixture/helpers (the only way to get a real draft bill is
to book, confirm and complete a real appointment) and `tests/test_bill_authority.py`'s
staff-request helpers (the pending/stale-override refusal tests need a real approved request).

This file defines its own `claimed_instance` rather than importing `test_bill_review.py`'s:
`invoices.service_bill_id` is `ON DELETE RESTRICT`, so the generic wipe's `DELETE FROM
service_bills` would itself be refused if a leftover `Invoice` row from a previous test still
pointed at one — the four new tables (plus their purge-role-bypassed triggers, migration 0054)
have to go first, the same "children before parents" ordering `wipe_document_keys` already
uses for `business_document_keys`.
"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.db import get_purge_engine, session_scope
from tests.conftest import add_account, wipe_document_keys
from tests.test_bill_authority import decision_url, submit_request
from tests.test_bill_review import (
    BILLS,
    EMAIL,
    SETUP,
    STAFF_EMAIL,
    STAFF_PASSWORD,
    add_front_desk_account,
    as_admin,
    as_staff,
    complete_a_visit,
    make_discount,
    make_tax_component,
)

INVOICES = "/api/invoices"


def issue_url(bill_id: str) -> str:
    return f"{BILLS}/{bill_id}/issue"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async def wipe():
        async with get_purge_engine().begin() as purge:
            await purge.execute(text("DELETE FROM audit_events"))
            await purge.execute(text("DELETE FROM erasure_requests"))
            await purge.execute(text("DELETE FROM form_links"))
            # #69's commission ledger references `invoice_lines`/`invoices` with `ON DELETE
            # RESTRICT` too — must go before them, same "children before parents" rule.
            await purge.execute(text("DELETE FROM invoice_payment_transfers"))  # #68
            await purge.execute(text("DELETE FROM commission_postings"))
            # #65's own append-only/voidable tables — purge-role-bypassed (migration 0054),
            # and must go before `service_bills`/`appointments`/`services`/`staff`/`customers`
            # below (`invoices`/`invoice_lines` reference all of them with `ON DELETE
            # RESTRICT`).
            await purge.execute(text("DELETE FROM invoice_line_taxes"))
            await purge.execute(text("DELETE FROM invoice_line_discounts"))
            await purge.execute(text("DELETE FROM invoice_lines"))
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


# --- issuing, the happy path -----------------------------------------------------------------


async def test_issuing_allocates_the_first_number_and_freezes_the_bill(client):
    await as_admin(client)
    bill_id, service_id = await complete_a_visit(client, price_cents=12000)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)

    resp = await client.post(issue_url(bill_id), json={})

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["invoice_number"] == 1
    assert body["status"] == "issued"
    assert body["service_bill_id"] == bill_id
    assert body["computed_grand_total_cents"] == 12000
    assert body["grand_total_cents"] == 12000
    assert body["override_applied_cents"] is None
    assert len(body["lines"]) == 1
    line = body["lines"][0]
    assert line["service_id"] == service_id
    assert line["price_cents"] == 12000
    assert line["line_total_cents"] == 12000
    assert line["discounts"] == []
    assert line["taxes"] == []

    async with session_scope() as db:
        status = await db.scalar(
            text("SELECT status FROM service_bills WHERE id = :id"), {"id": bill_id}
        )
        line_count = await db.scalar(
            text("SELECT count(*) FROM invoice_lines WHERE invoice_id = :id"), {"id": body["id"]}
        )
    assert status == "issued"
    assert line_count == 1


async def test_a_second_bill_gets_the_next_sequential_number(client):
    await as_admin(client)
    bill_a, _ = await complete_a_visit(client, service_name="Swedish Massage")
    bill_b, _ = await complete_a_visit(client, service_name="Deep Tissue Massage")

    first = await client.post(issue_url(bill_a), json={})
    second = await client.post(issue_url(bill_b), json={})

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert sorted([first.json()["invoice_number"], second.json()["invoice_number"]]) == [1, 2]


async def test_issuing_an_already_issued_bill_is_refused(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)

    first = await client.post(issue_url(bill_id), json={})
    second = await client.post(issue_url(bill_id), json={})

    assert first.status_code == 201, first.text
    assert second.status_code == 422, second.text


async def test_an_empty_bill_cannot_be_issued(client):
    """Unreachable through the ordinary API (#59's completion hook never creates a bill with
    no lines) — the guard is exercised directly, the same "insert past the API to prove a
    defensive check" shape `test_bill_review.py`'s own fixture setup uses for schema-level
    invariants."""
    await as_admin(client)
    async with session_scope() as db:
        customer_id = await db.scalar(text("SELECT id FROM customers LIMIT 1"))
    if customer_id is None:
        made = await client.post(
            "/api/customers", json={"first_name": "A", "last_name": "B", "phone": "1"}
        )
        assert made.status_code == 201, made.text
        customer_id = made.json()["id"]
    async with session_scope() as db:
        bill_id = await db.scalar(
            text(
                "INSERT INTO service_bills (customer_id, status) VALUES (:c, 'draft') RETURNING id"
            ),
            {"c": customer_id},
        )
        await db.commit()

    resp = await client.post(issue_url(str(bill_id)), json={})

    assert resp.status_code == 422, resp.text


async def test_issuing_needs_billing_view(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    role = await client.post("/api/admin/roles", json={"name": "No Billing", "capabilities": []})
    assert role.status_code == 201, role.text
    from core.security import hash_password

    await add_account(
        "nocap@cedar.example", await hash_password(STAFF_PASSWORD), role=role.json()["id"]
    )
    client.cookies.clear()
    await as_staff(client, "nocap@cedar.example", STAFF_PASSWORD)

    resp = await client.post(issue_url(bill_id), json={})

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "capability_required"


# --- refusal: pending / stale / unauthorized override ------------------------------------


async def test_issuing_with_a_pending_override_request_is_refused(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    await submit_request(client, bill_id)

    resp = await client.post(issue_url(bill_id), json={})

    assert resp.status_code == 409, resp.text
    assert "pending" in resp.json()["detail"]


async def test_issuing_with_a_stale_approved_override_is_refused(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client, price_cents=12000)
    request = await submit_request(client, bill_id, requested_total_cents=9000)
    decided = await client.post(decision_url(bill_id, request["id"]), json={"decision": "approved"})
    assert decided.status_code == 200, decided.text

    # The bill moves on after the override was authorized — a sibling appointment completing
    # into it (`billing/completion.py`) bumps `updated_at` without touching the override
    # fields at all. Simulated directly rather than via a second linked appointment: this
    # proves #65's own refusal logic, not #64's already-tested bump behaviour.
    async with session_scope() as db:
        await db.execute(
            text("UPDATE service_bills SET updated_at = now() WHERE id = :id"), {"id": bill_id}
        )
        await db.commit()

    resp = await client.post(issue_url(bill_id), json={})

    assert resp.status_code == 409, resp.text
    assert "no longer current" in resp.json()["detail"]


async def test_a_manual_override_without_an_authorization_checkpoint_refuses_issue(client):
    """`override_applied_revision IS NULL` while `manual_override_cents` is set — the
    "required admin authorization... hasn't actually happened" case. Unreachable through
    either of #64's own write paths (both stamp the checkpoint in the same statement); proven
    here by writing past them directly."""
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    async with session_scope() as db:
        await db.execute(
            text(
                "UPDATE service_bills SET manual_override_cents = 5000, "
                "manual_override_reason = 'no checkpoint' WHERE id = :id"
            ),
            {"id": bill_id},
        )
        await db.commit()

    resp = await client.post(issue_url(bill_id), json={})

    assert resp.status_code == 409, resp.text


async def test_issuing_with_a_current_approved_override_uses_the_override_total(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client, price_cents=12000)
    request = await submit_request(
        client, bill_id, requested_total_cents=9000, reason="Loyal client"
    )
    decided = await client.post(decision_url(bill_id, request["id"]), json={"decision": "approved"})
    assert decided.status_code == 200, decided.text

    resp = await client.post(issue_url(bill_id), json={})

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["computed_grand_total_cents"] == 12000
    assert body["override_applied_cents"] == 9000
    assert body["override_reason"] == "Loyal client"
    assert body["grand_total_cents"] == 9000


# --- the snapshot contract: never re-joins live definitions -----------------------------------


async def test_an_issued_invoice_never_changes_when_live_definitions_change_afterward(client):
    await as_admin(client)
    bill_id, service_id = await complete_a_visit(client, price_cents=10000)
    discount = await make_discount(client, percentage_bp=1000)
    tax = await make_tax_component(client, rate_bp=500)
    applied = await client.put(
        f"{BILLS}/{bill_id}/discounts", json={"discount_ids": [discount["id"]]}
    )
    assert applied.status_code == 200, applied.text

    issued = await client.post(issue_url(bill_id), json={})
    assert issued.status_code == 201, issued.text
    invoice_id = issued.json()["id"]
    before = issued.json()
    assert before["computed_discount_total_cents"] == 1000  # 10% of 10000
    assert before["computed_tax_total_cents"] == 450  # 5% of the discounted 9000

    async with session_scope() as db:
        await db.execute(
            text("UPDATE services SET price_cents = 99999 WHERE id = :id"), {"id": service_id}
        )
        await db.execute(
            text("UPDATE discounts SET percentage_bp = 9000 WHERE id = :id"),
            {"id": discount["id"]},
        )
        await db.execute(
            text("UPDATE tax_component_rates SET rate_bp = 9999 WHERE component_id = :id"),
            {"id": tax["id"]},
        )
        await db.commit()

    after = await client.get(f"{INVOICES}/{invoice_id}")

    assert after.status_code == 200, after.text
    assert after.json() == before


# --- gapless numbering under real concurrency --------------------------------------------


async def test_two_concurrent_issues_on_different_bills_get_sequential_numbers_no_gap(
    client, monkeypatch
):
    """Two ASGI clients race to issue two *different* bills for the same business. Nothing in
    the app locks — `billing/invoice_numbering.py::allocate_invoice_number`'s own
    `SELECT ... FOR UPDATE` on the one counter row is the lock (CLAUDE.md "Concurrency"). The
    sleep tacked onto the real allocator, before the route's own commit, keeps the first
    transaction's row lock held long enough that the second's own `UPDATE` genuinely blocks on
    Postgres rather than racing in application code — the same interleaving `test_stock_
    movements.py`'s own concurrency test forces."""
    await as_admin(client)
    bill_a, _ = await complete_a_visit(client, service_name="Swedish Massage")
    bill_b, _ = await complete_a_visit(client, service_name="Deep Tissue Massage")
    cookie = client.cookies["linsuite_session"]

    from billing import invoices as invoices_mod
    from main import app as main_app

    real_allocate = invoices_mod.allocate_invoice_number

    async def slow_allocate(*args, **kwargs):
        result = await real_allocate(*args, **kwargs)
        await asyncio.sleep(0.25)
        return result

    monkeypatch.setattr(invoices_mod, "allocate_invoice_number", slow_allocate)

    async def attempt(bill_id: str):
        async with AsyncClient(transport=ASGITransport(app=main_app), base_url="http://test") as c:
            c.cookies.set("linsuite_session", cookie)
            return await c.post(issue_url(bill_id), json={})

    results = await asyncio.gather(attempt(bill_a), attempt(bill_b))

    assert [r.status_code for r in results] == [201, 201], [r.text for r in results]
    numbers = sorted(r.json()["invoice_number"] for r in results)
    assert numbers == [1, 2]
    async with session_scope() as db:
        rows = list(
            await db.scalars(text("SELECT invoice_number FROM invoices ORDER BY invoice_number"))
        )
        counter = await db.scalar(
            text("SELECT next_number FROM business_invoice_counters WHERE business_id = 1")
        )
    assert rows == [1, 2]
    assert counter == 3


# --- immutability: append-only / voidable, by grant and trigger -------------------------------


TABLES = ("invoice_lines", "invoice_line_discounts", "invoice_line_taxes")
TRIGGERS = {
    "invoices": "invoices_voidable_guard",
    "invoice_lines": "invoice_lines_no_rewrite",
    "invoice_line_discounts": "invoice_line_discounts_no_rewrite",
    "invoice_line_taxes": "invoice_line_taxes_no_rewrite",
}
# A harmless self-referential SET per table — each has a different own-column shape (the two
# child tables key off `invoice_line_id`, `invoice_lines` off its own `id`).
_NOOP_SET = {
    "invoice_lines": "price_cents = price_cents",
    "invoice_line_discounts": "discount_name = discount_name",
    "invoice_line_taxes": "rate_bp = rate_bp",
}


async def _issue_one(client) -> dict:
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    resp = await client.post(issue_url(bill_id), json={})
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.parametrize("table", TABLES)
async def test_the_app_role_may_neither_update_nor_delete_an_invoice_line_child_row(client, table):
    await _issue_one(client)
    async with session_scope() as db:
        for statement in (
            f"UPDATE {table} SET {_NOOP_SET[table]}",
            f"DELETE FROM {table}",
        ):
            with pytest.raises(DBAPIError) as refused:
                await db.execute(text(statement))
            await db.rollback()
            assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value


async def test_the_app_role_may_not_delete_an_invoice(client):
    await _issue_one(client)
    async with session_scope() as db:
        with pytest.raises(DBAPIError) as refused:
            await db.execute(text("DELETE FROM invoices"))
        await db.rollback()
    assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value


async def test_the_app_role_may_not_update_an_invoices_money_columns(client):
    invoice = await _issue_one(client)
    async with session_scope() as db:
        with pytest.raises(DBAPIError) as refused:
            await db.execute(
                text("UPDATE invoices SET grand_total_cents = 1 WHERE id = :id"),
                {"id": invoice["id"]},
            )
        await db.rollback()
    assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value


async def test_the_app_role_may_perform_the_one_permitted_cancel_transition(client):
    """The narrow exception `invoices_voidable_guard` exists to allow (module docstring,
    `billing/models.py`): `status` flips `issued` -> `cancelled` with the cancel fields newly
    set, everything else identical. No `/cancel` route is built by this ticket — this proves
    the database itself already allows exactly this shape, for whichever later ticket adds
    one."""
    invoice = await _issue_one(client)
    async with session_scope() as db:
        actor_id = await db.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": EMAIL})
        await db.execute(
            text(
                "UPDATE invoices SET status = 'cancelled', cancelled_at = now(), "
                "cancelled_by = :actor, cancel_reason = 'test cancel' WHERE id = :id"
            ),
            {"actor": actor_id, "id": invoice["id"]},
        )
        await db.commit()
        status = await db.scalar(
            text("SELECT status FROM invoices WHERE id = :id"), {"id": invoice["id"]}
        )
    assert status == "cancelled"


@pytest.mark.parametrize("table", ["invoices", *TABLES])
async def test_the_guard_trigger_is_attached_and_enabled(client, table):
    await _issue_one(client)
    async with session_scope() as db:
        enabled = await db.scalar(
            text(
                "SELECT tgenabled::text FROM pg_trigger "
                f"WHERE tgrelid = '{table}'::regclass AND tgname = '{TRIGGERS[table]}'"
            )
        )
    assert enabled == "O"

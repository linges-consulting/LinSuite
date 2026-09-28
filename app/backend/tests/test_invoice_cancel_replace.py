"""S1(+S5): cancel & replace an issued invoice (#68) over a real PostgreSQL — the original is
retained and marked cancelled, its bill reopens as the replacement draft, reissuing it links
the lineage both ways, payments transfer (never re-charged, never double-counted), commission
is reversed then re-earned once, and a retried cancel produces one set of effects.

Reuses `tests/test_invoice_payments.py`'s autouse `claimed_instance` fixture (imported below)
and its helpers — it already wipes every invoice child table in the right order.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.db import session_scope
from tests.test_bill_review import as_admin, complete_a_visit
from tests.test_invoice_payments import (  # noqa: F401 — claimed_instance is an autouse fixture
    INVOICES,
    claimed_instance,
    issue_an_invoice,
    issue_url,
    payments_url,
)


def cancel_url(invoice_id: str) -> str:
    return f"{INVOICES}/{invoice_id}/cancel"


async def pay(client, invoice_id: str, cents: int) -> None:
    resp = await client.post(
        payments_url(invoice_id),
        json={"payer_type": "client", "method": "cash", "amount_cents": cents},
    )
    assert resp.status_code == 201, resp.text


async def cancel(client, invoice_id: str, reason: str = "Wrong service recorded") -> dict:
    resp = await client.post(cancel_url(invoice_id), json={"reason": reason})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def reissue(client, bill_id: str) -> dict:
    resp = await client.post(issue_url(bill_id), json={})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def set_override(bill_id: str, cents: int) -> None:
    """An authorized override, written the way #65's tests do (checkpoint == updated_at)."""
    async with session_scope() as db:
        await db.execute(
            text(
                "UPDATE service_bills SET manual_override_cents = :c, "
                "manual_override_reason = 'corrected', override_applied_revision = updated_at "
                "WHERE id = :b"
            ),
            {"c": cents, "b": bill_id},
        )
        await db.commit()


async def scalar(sql: str, **params):
    async with session_scope() as db:
        return await db.scalar(text(sql), params)


# --- cancellation ------------------------------------------------------------------------------


async def test_cancel_requires_a_reason(client):
    invoice_id = await issue_an_invoice(client)

    assert (await client.post(cancel_url(invoice_id), json={})).status_code == 422
    assert (await client.post(cancel_url(invoice_id), json={"reason": ""})).status_code == 422
    assert (await client.get(f"{INVOICES}/{invoice_id}")).json()["status"] == "issued"


async def test_cancel_retains_the_original_and_reopens_its_bill_as_the_replacement_draft(client):
    invoice_id = await issue_an_invoice(client, price_cents=12000)
    original = (await client.get(f"{INVOICES}/{invoice_id}")).json()

    body = await cancel(client, invoice_id)

    cancelled = body["invoice"]
    assert cancelled["status"] == "cancelled"
    assert cancelled["invoice_number"] == original["invoice_number"]
    assert cancelled["cancel_reason"] == "Wrong service recorded"
    assert cancelled["cancelled_at"] is not None
    assert cancelled["grand_total_cents"] == original["grand_total_cents"]
    assert cancelled["lines"] == original["lines"]
    assert body["replacement_bill_id"] == original["service_bill_id"]

    draft = await client.get(f"/api/bills/{body['replacement_bill_id']}")
    assert draft.status_code == 200, draft.text
    assert draft.json()["status"] == "draft"
    assert len(draft.json()["lines"]) == len(original["lines"])


async def test_a_still_valid_override_carries_onto_the_replacement_draft(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client, price_cents=12000)
    await set_override(bill_id, 9000)
    original = await reissue(client, bill_id)
    assert original["grand_total_cents"] == 9000

    body = await cancel(client, original["id"])
    replacement = await reissue(client, body["replacement_bill_id"])

    assert replacement["grand_total_cents"] == 9000


async def test_reissuing_the_draft_links_the_lineage_both_ways_with_a_new_number(client):
    invoice_id = await issue_an_invoice(client)
    body = await cancel(client, invoice_id)

    replacement = await reissue(client, body["replacement_bill_id"])

    assert replacement["replaces_invoice_id"] == invoice_id
    assert replacement["invoice_number"] == body["invoice"]["invoice_number"] + 1
    assert replacement["status"] == "issued"
    original = (await client.get(f"{INVOICES}/{invoice_id}")).json()
    assert original["replaced_by_invoice_id"] == replacement["id"]
    assert original["status"] == "cancelled"
    listed = (await client.get(INVOICES)).json()["invoices"]
    assert [i["status"] for i in listed] == ["cancelled", "issued"]


async def test_cancelling_an_unknown_invoice_404s(client):
    await issue_an_invoice(client)
    missing = await client.post(
        cancel_url("00000000-0000-0000-0000-000000000000"), json={"reason": "x"}
    )
    assert missing.status_code == 404, missing.text


async def test_cancel_needs_billing_view(client):
    invoice_id = await issue_an_invoice(client)
    client.cookies.clear()

    resp = await client.post(cancel_url(invoice_id), json={"reason": "x"})

    assert resp.status_code in (401, 403), resp.text


# --- payments ----------------------------------------------------------------------------------


async def test_payments_transfer_to_the_replacement_and_are_never_charged_again(client):
    invoice_id = await issue_an_invoice(client, price_cents=12000)
    await pay(client, invoice_id, 12000)
    body = await cancel(client, invoice_id)

    # Held as a credit on the cancelled original until the replacement exists.
    held = (await client.get(f"{INVOICES}/{invoice_id}")).json()
    assert held["outstanding_cents"] == -12000
    # Payments are refused on a cancelled invoice (#66).
    refused = await client.post(
        payments_url(invoice_id),
        json={"payer_type": "client", "method": "cash", "amount_cents": 100},
    )
    assert refused.status_code == 422

    replacement = await reissue(client, body["replacement_bill_id"])

    assert replacement["outstanding_cents"] == 0
    assert replacement["checkout_complete"] is True
    original = (await client.get(f"{INVOICES}/{invoice_id}")).json()
    assert original["outstanding_cents"] == 0
    history = (await client.get(payments_url(replacement["id"]))).json()
    assert history["payments"] == []
    assert len(history["transfers"]) == 1
    assert history["transfers"][0]["from_invoice_id"] == invoice_id
    assert history["transfers"][0]["received_cents"] == 12000
    # The original payment row is still there, untouched, on the original.
    original_history = (await client.get(payments_url(invoice_id))).json()
    assert [p["amount_cents"] for p in original_history["payments"]] == [12000]


async def test_a_price_increase_is_collected_as_an_ordinary_new_payment(client):
    invoice_id = await issue_an_invoice(client, price_cents=12000)
    await pay(client, invoice_id, 12000)
    body = await cancel(client, invoice_id)
    await set_override(body["replacement_bill_id"], 15000)

    replacement = await reissue(client, body["replacement_bill_id"])

    assert replacement["grand_total_cents"] == 15000
    assert replacement["outstanding_cents"] == 3000
    assert replacement["checkout_complete"] is False
    await pay(client, replacement["id"], 3000)
    after = (await client.get(f"{INVOICES}/{replacement['id']}")).json()
    assert after["outstanding_cents"] == 0
    assert after["checkout_complete"] is True


async def test_a_price_decrease_leaves_a_credit_and_never_refunds_automatically(client):
    invoice_id = await issue_an_invoice(client, price_cents=12000)
    await pay(client, invoice_id, 12000)
    body = await cancel(client, invoice_id)
    await set_override(body["replacement_bill_id"], 10000)

    replacement = await reissue(client, body["replacement_bill_id"])

    assert replacement["outstanding_cents"] == -2000
    assert await scalar("SELECT count(*) FROM invoice_payments") == 1


async def test_a_second_cancel_carries_the_inherited_money_down_the_chain(client):
    invoice_id = await issue_an_invoice(client, price_cents=12000)
    await pay(client, invoice_id, 5000)
    bill_id = (await cancel(client, invoice_id))["replacement_bill_id"]
    second = await reissue(client, bill_id)
    await pay(client, second["id"], 2000)
    await cancel(client, second["id"])

    third = await reissue(client, bill_id)

    assert third["replaces_invoice_id"] == second["id"]
    assert third["outstanding_cents"] == 12000 - 7000
    for earlier in (invoice_id, second["id"]):
        assert (await client.get(f"{INVOICES}/{earlier}")).json()["outstanding_cents"] == 0


# --- commission, retries -----------------------------------------------------------------------


async def test_commission_is_reversed_on_cancel_and_earned_once_by_the_replacement(client):
    invoice_id = await issue_an_invoice(client, price_cents=12000)
    earned = await scalar("SELECT sum(amount_cents) FROM commission_postings")
    body = await cancel(client, invoice_id)

    assert await scalar("SELECT sum(amount_cents) FROM commission_postings") == 0
    assert (
        await scalar(
            "SELECT count(*) FROM commission_postings r JOIN commission_postings e "
            "ON r.reverses_posting_id = e.id WHERE r.kind = 'reversal' AND e.invoice_id = :i",
            i=invoice_id,
        )
        == 1
    )

    replacement = await reissue(client, body["replacement_bill_id"])
    assert await scalar("SELECT sum(amount_cents) FROM commission_postings") == earned
    assert (
        await scalar(
            "SELECT sum(amount_cents) FROM commission_postings WHERE invoice_id = :i",
            i=replacement["id"],
        )
        == earned
    )


async def test_a_retried_cancel_and_reissue_produce_one_lineage_and_one_set_of_effects(client):
    invoice_id = await issue_an_invoice(client, price_cents=12000)
    await pay(client, invoice_id, 12000)

    first = await cancel(client, invoice_id)
    retried = await cancel(client, invoice_id, reason="different reason on retry")
    assert retried["invoice"]["cancelled_at"] == first["invoice"]["cancelled_at"]
    assert retried["invoice"]["cancel_reason"] == "Wrong service recorded"

    replacement = await reissue(client, first["replacement_bill_id"])
    again = await client.post(issue_url(first["replacement_bill_id"]), json={})
    assert again.status_code == 422, again.text
    after_replacement = await cancel(client, invoice_id)
    assert after_replacement["invoice"]["replaced_by_invoice_id"] == replacement["id"]

    counts = {
        sql: await scalar(sql)
        for sql in (
            "SELECT count(*) FROM invoices",
            "SELECT count(*) FROM invoice_payment_transfers",
            "SELECT count(*) FROM commission_postings WHERE kind = 'reversal'",
            "SELECT count(*) FROM audit_events WHERE event_type = 'invoice.cancelled'",
            "SELECT count(*) FROM service_bill_lines",
        )
    }
    assert list(counts.values()) == [2, 1, 1, 1, 1]
    assert (await client.get(f"{INVOICES}/{replacement['id']}")).json()["outstanding_cents"] == 0


async def test_the_replacement_gets_its_own_rendered_documents(client):
    invoice_id = await issue_an_invoice(client)
    replacement = await reissue(client, (await cancel(client, invoice_id))["replacement_bill_id"])

    for source in (invoice_id, replacement["id"]):
        assert (
            await scalar(
                "SELECT count(*) FROM documents WHERE kind = 'invoice' AND source_id = :i",
                i=source,
            )
            == 1
        )


# --- S5: the transfer history is append-only ----------------------------------------------------


async def test_the_app_role_may_neither_update_nor_delete_a_payment_transfer(client):
    invoice_id = await issue_an_invoice(client)
    await reissue(client, (await cancel(client, invoice_id))["replacement_bill_id"])

    async with session_scope() as db:
        for statement in (
            "UPDATE invoice_payment_transfers SET received_cents = received_cents",
            "DELETE FROM invoice_payment_transfers",
        ):
            with pytest.raises(DBAPIError) as refused:
                await db.execute(text(statement))
            await db.rollback()
            assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value

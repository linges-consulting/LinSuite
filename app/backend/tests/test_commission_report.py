"""S1: the admin-only commission report (#69) — `GET /api/admin/reports/commission` (JSON) and
the queued CSV export (`/exports`, R29), `commission.view`, Administrator-only, Admin
Mode. `tests/test_commission_leakage.py` proves the flip side (no staff-facing payload leaks
this data); this file proves the report itself: the access gate, the date-range/staff filters,
and the totals.
"""

import csv
import io

import pytest
from sqlalchemy import text

from core.db import session_scope
from core.security import hash_password
from scheduling.clock import today_in
from tests.conftest import add_account, get_owner_engine, wipe_document_keys
from tests.test_bill_review import EMAIL, SETUP, STAFF, STAFF_EMAIL, as_admin, complete_a_visit
from tests.test_invoice_issue import issue_url

REPORT = "/api/admin/reports/commission"
EXPORTS = "/api/admin/reports/commission/exports"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async def wipe():
        # #83: none of these are among the purge role's four permitted tables — reset as the
        # schema owner instead.
        async with get_owner_engine().begin() as owner:
            await owner.execute(text("DELETE FROM audit_events"))
            await owner.execute(text("DELETE FROM erasure_requests"))
            await owner.execute(text("DELETE FROM form_links"))
            await owner.execute(text("DELETE FROM retail_return_lines"))  # R27: retail too
            await owner.execute(text("DELETE FROM retail_returns"))
            await owner.execute(text("DELETE FROM invoice_payments"))  # R25: the ledger
            await owner.execute(text("DELETE FROM invoice_balance_authorizations"))
            await owner.execute(text("DELETE FROM invoice_payment_transfers"))  # #68
            await owner.execute(text("DELETE FROM commission_postings"))
            await owner.execute(text("DELETE FROM retail_invoice_line_taxes"))
            await owner.execute(text("DELETE FROM retail_invoice_line_discounts"))
            await owner.execute(text("DELETE FROM retail_invoice_lines"))
            await owner.execute(text("DELETE FROM invoice_line_taxes"))
            await owner.execute(text("DELETE FROM invoice_line_discounts"))
            await owner.execute(text("DELETE FROM invoice_lines"))
            await owner.execute(text("DELETE FROM retail_return_lines"))  # #76
            await owner.execute(text("DELETE FROM retail_returns"))  # #76
            await owner.execute(text("DELETE FROM invoice_refunds"))  # #67
            await owner.execute(text("DELETE FROM retail_invoices"))
            await owner.execute(text("DELETE FROM invoices"))
            await owner.execute(text("DELETE FROM business_invoice_counters"))
            await owner.execute(text("DELETE FROM stock_movements"))
        await wipe_document_keys()
        async with session_scope() as db:
            for table in (
                "commission_exports",  # R29
                "retail_sale_lines",
                "retail_sales",
                "product_variants",
                "products",
                "bill_override_requests",
                "service_bill_discounts",
                "retail_sale_discounts",  # review T1, 0065
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


async def _issue_one(client, *, rate_bp: int = 1000, price_cents: int = 10000) -> tuple[str, str]:
    """Admin, in Admin Mode, sets their own staff rate, completes and issues a visit —
    returns `(staff_id, invoice_id)`."""
    roster = await client.get(STAFF)
    assert roster.status_code == 200, roster.text
    me = next(row["id"] for row in roster.json()["staff"] if row["email"] == EMAIL)
    rated = await client.patch(f"{STAFF}/{me}", json={"commission_rate_services_bp": rate_bp})
    assert rated.status_code == 200, rated.text
    bill_id, _ = await complete_a_visit(client, price_cents=price_cents)
    issued = await client.post(issue_url(bill_id), json={})
    assert issued.status_code == 201, issued.text
    return me, issued.json()["id"]


# --- the access gate ---------------------------------------------------------------------------


async def test_the_report_requires_admin_mode_even_for_an_administrator(client):
    """`commission.view` is `requires_admin_mode=True` — a Staff Mode session for an account
    that holds the capability is still refused, `AdminUser`'s own rule."""
    login = await client.post(
        "/api/auth/login", json={"email": EMAIL, "password": "correct horse battery"}
    )
    assert login.status_code == 200, login.text

    resp = await client.get(REPORT)

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "admin_mode_required"


async def test_the_report_requires_the_commission_view_capability(client):
    await as_admin(client)
    role = await client.post(
        "/api/admin/roles", json={"name": "Admin-lite", "capabilities": ["admin"]}
    )
    assert role.status_code == 201, role.text
    await add_account(
        "noreport@cedar.example",
        await hash_password("correct horse battery 3"),
        role=role.json()["id"],
    )
    client.cookies.clear()
    login = await client.post(
        "/api/auth/login",
        json={"email": "noreport@cedar.example", "password": "correct horse battery 3"},
    )
    assert login.status_code == 200, login.text
    mode = await client.post(
        "/api/auth/mode", json={"mode": "admin", "password": "correct horse battery 3"}
    )
    assert mode.status_code == 200, mode.text

    resp = await client.get(REPORT)

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "capability_required"


async def test_a_plain_staff_account_with_billing_view_is_still_refused(client):
    from tests.test_bill_review import add_front_desk_account, as_staff

    await as_admin(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)

    resp = await client.get(REPORT)

    assert resp.status_code == 403, resp.text


# --- the report itself --------------------------------------------------------------------------


async def test_an_unpaid_invoices_commission_is_all_pending(client):
    await as_admin(client)
    staff_id, invoice_id = await _issue_one(client, rate_bp=1000, price_cents=10000)

    resp = await client.get(REPORT)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert (row["staff_id"], row["invoice_id"], row["source"]) == (staff_id, invoice_id, "service")
    assert row["kind"] == "earned"
    assert row["commission_rate_bp"] == 1000
    assert row["basis_cents"] == 10000
    assert row["amount_cents"] == 1000
    assert row["payment_status"] == "pending"
    assert (row["commission_received_cents"], row["commission_pending_cents"]) == (0, 1000)
    assert (body["total_earned_cents"], body["total_received_cents"]) == (1000, 0)
    assert body["total_pending_cents"] == 1000
    assert body["service"]["revenue_cents"] == 10000
    assert body["retail"] == {
        "revenue_cents": 0,
        "commission_cents": 0,
        "commission_received_cents": 0,
        "commission_pending_cents": 0,
    }


async def test_the_report_filters_by_staff_id(client):
    await as_admin(client)
    staff_id, _invoice_id = await _issue_one(client)

    matching = await client.get(REPORT, params={"staff_id": staff_id})
    assert matching.status_code == 200, matching.text
    assert len(matching.json()["rows"]) == 1

    import uuid

    other = await client.get(REPORT, params={"staff_id": str(uuid.uuid4())})
    assert other.status_code == 200, other.text
    assert other.json()["rows"] == []
    assert other.json()["total_earned_cents"] == 0


async def test_the_report_filters_by_date_range(client):
    await as_admin(client)
    await _issue_one(client)

    from datetime import date, timedelta

    future = date.today() + timedelta(days=3)
    empty = await client.get(
        REPORT, params={"from": future.isoformat(), "to": (future + timedelta(days=1)).isoformat()}
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["rows"] == []

    # The report's calendar is the business's, not this machine's (UTC on CI): today there.
    async with session_scope() as db:
        zone = await db.scalar(text("SELECT timezone FROM businesses"))
    today = today_in(zone).isoformat()
    present = await client.get(REPORT, params={"from": today, "to": today})
    assert present.status_code == 200, present.text
    assert len(present.json()["rows"]) == 1


async def test_an_inverted_range_is_refused(client):
    await as_admin(client)
    from datetime import date, timedelta

    today = date.today()
    resp = await client.get(
        REPORT, params={"from": today.isoformat(), "to": (today - timedelta(days=1)).isoformat()}
    )
    assert resp.status_code == 422, resp.text


# --- R25: received/pending from the payment ledger ------------------------------------------


async def pay(client, invoice_id: str, amount_cents: int, *, retail: bool = False, **extra):
    kind = "retail-invoices" if retail else "invoices"
    resp = await client.post(
        f"/api/{kind}/{invoice_id}/payments",
        json={"payer_type": "client", "method": "cash", "amount_cents": amount_cents, **extra},
    )
    assert resp.status_code == 201, resp.text


async def test_received_and_pending_follow_the_payment_ledger(client):
    await as_admin(client)
    _staff, invoice_id = await _issue_one(client, rate_bp=1000, price_cents=10000)
    total = (await client.get(f"/api/invoices/{invoice_id}")).json()["grand_total_cents"]
    half = total // 2

    await pay(client, invoice_id, half)
    body = (await client.get(REPORT)).json()
    [row] = body["rows"]
    assert row["payment_status"] == "partial"
    assert (row["invoice_received_cents"], row["invoice_pending_cents"]) == (half, total - half)
    assert row["commission_received_cents"] + row["commission_pending_cents"] == 1000
    assert row["commission_received_cents"] == round(1000 * half / total)
    assert (body["payments_received_cents"], body["payments_pending_cents"]) == (
        half,
        total - half,
    )

    # Approved-but-unpaid insurer money is still pending, never received.
    await pay(
        client, invoice_id, total - half, payer_type="insurer", method="insurer", status="pending"
    )
    assert (await client.get(REPORT)).json()["rows"][0]["payment_status"] == "partial"

    await pay(client, invoice_id, total - half)
    full = (await client.get(REPORT)).json()
    assert full["rows"][0]["payment_status"] == "received"
    assert (full["total_received_cents"], full["total_pending_cents"]) == (1000, 0)
    assert (full["payments_received_cents"], full["payments_pending_cents"]) == (total, 0)


async def test_a_cancel_and_reissue_is_one_earning_and_carried_money_is_not_new_cash(client):
    await as_admin(client)
    _staff, invoice_id = await _issue_one(client, rate_bp=1000, price_cents=10000)
    total = (await client.get(f"/api/invoices/{invoice_id}")).json()["grand_total_cents"]
    await pay(client, invoice_id, total)
    cancelled = await client.post(f"/api/invoices/{invoice_id}/cancel", json={"reason": "Typo"})
    assert cancelled.status_code == 200, cancelled.text
    reissued = await client.post(issue_url(cancelled.json()["replacement_bill_id"]), json={})
    assert reissued.status_code == 201, reissued.text

    body = (await client.get(REPORT)).json()

    assert sorted(r["kind"] for r in body["rows"]) == ["earned", "earned", "reversal"]
    assert body["total_earned_cents"] == 1000
    assert (body["total_received_cents"], body["total_pending_cents"]) == (1000, 0)
    # The carried payment counts once, on the replacement — a transfer is not a receipt.
    assert body["payments_received_cents"] == total
    voided = [r for r in body["rows"] if r["invoice_id"] == invoice_id]
    assert {r["payment_status"] for r in voided} == {"voided"}
    assert sum(r["amount_cents"] for r in voided) == 0


# --- R27: retail commission, split from service ----------------------------------------------


async def _retail(client, *, quantity: int, price_cents: int = 1500, rate_bp: int = 2000):
    from tests.test_retail_sales import add_line, issue, make_staff, new_variant, start_sale

    seller = await make_staff(client, commission_rate_retail_bp=rate_bp)
    variant = await new_variant(client, quantity_on_hand=10, price_cents=price_cents)
    sale = await start_sale(client, sold_by_staff_id=seller["id"])
    await add_line(client, sale["id"], variant["id"], quantity=quantity)
    resp = await issue(client, sale["id"])
    assert resp.status_code == 201, resp.text
    return seller, resp.json()


async def _return(client, invoice: dict, quantity: int):
    line_id = invoice["lines"][0]["id"]
    resp = await client.post(
        f"/api/retail-invoices/{invoice['id']}/returns",
        json={
            "reason": "Changed mind",
            "lines": [{"retail_invoice_line_id": line_id, "quantity": quantity, "restock": True}],
        },
    )
    assert resp.status_code == 201, resp.text


async def test_retail_commission_posts_at_issue_for_the_seller_and_reports_separately(client):
    await as_admin(client)
    await _issue_one(client, rate_bp=1000, price_cents=10000)
    seller, invoice = await _retail(client, quantity=2, price_cents=1500, rate_bp=2000)
    await pay(client, invoice["id"], invoice["grand_total_cents"], retail=True)

    body = (await client.get(REPORT)).json()

    [row] = [r for r in body["rows"] if r["source"] == "retail"]
    assert (row["staff_id"], row["invoice_id"]) == (seller["id"], invoice["id"])
    assert (row["variant_id"], row["service_id"]) == (invoice["lines"][0]["variant_id"], None)
    assert (row["commission_rate_bp"], row["basis_cents"], row["amount_cents"]) == (2000, 3000, 600)
    assert row["payment_status"] == "received"
    assert body["retail"] == {
        "revenue_cents": 3000,
        "commission_cents": 600,
        "commission_received_cents": 600,
        "commission_pending_cents": 0,
    }
    assert body["service"]["commission_cents"] == 1000
    assert body["total_earned_cents"] == 1600


async def test_retail_returns_reverse_proportionally_and_a_full_return_nets_to_zero(client):
    await as_admin(client)
    _seller, invoice = await _retail(client, quantity=3, price_cents=1000, rate_bp=1000)  # 300

    await _return(client, invoice, 1)
    one = (await client.get(REPORT)).json()["retail"]
    assert (one["commission_cents"], one["revenue_cents"]) == (200, 2000)

    await _return(client, invoice, 2)
    body = (await client.get(REPORT)).json()
    assert (body["retail"]["commission_cents"], body["retail"]["revenue_cents"]) == (0, 0)
    assert [r["kind"] for r in body["rows"]].count("reversal") == 2


async def test_retail_cancel_and_replace_nets_to_one_earning_on_what_was_kept(client):
    await as_admin(client)
    _seller, invoice = await _retail(client, quantity=2, price_cents=1500, rate_bp=2000)  # 600
    await _return(client, invoice, 1)  # -300
    cancel_url = f"/api/retail-invoices/{invoice['id']}/cancel"
    cancelled = await client.post(cancel_url, json={"reason": "Wrong seller"})
    assert cancelled.status_code == 200, cancelled.text  # -300: the original nets to zero
    sale_id = cancelled.json()["replacement_sale_id"]
    replacement = await client.post(f"/api/retail-sales/{sale_id}/issue", json={})
    assert replacement.status_code == 201, replacement.text  # +300 for the one unit kept

    body = (await client.get(REPORT)).json()

    assert body["retail"]["commission_cents"] == 300
    original = [r for r in body["rows"] if r["invoice_id"] == invoice["id"]]
    assert sum(r["amount_cents"] for r in original) == 0
    # A retried cancel reverses nothing more.
    again = await client.post(cancel_url, json={"reason": "Wrong seller"})
    assert again.status_code == 200, again.text
    assert (await client.get(REPORT)).json()["retail"]["commission_cents"] == 300


# --- R29: the CSV export is a Celery job ------------------------------------------------------


async def test_the_csv_export_is_queued_then_downloaded(client):
    await as_admin(client)
    staff_id, invoice_id = await _issue_one(client, rate_bp=1500, price_cents=8000)

    queued = await client.post(EXPORTS, json={})

    assert queued.status_code == 202, queued.text
    # Eager Celery (the suite) has already run the job by the time the 202 is read.
    polled = await client.get(f"{EXPORTS}/{queued.json()['id']}")
    assert polled.status_code == 200, polled.text
    assert polled.json()["status"] == "ready"
    download = await client.get(polled.json()["download_url"])
    assert download.status_code == 200, download.text
    assert download.headers["content-type"].startswith("text/csv")
    assert "attachment" in download.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(download.text)))
    assert rows[0][:5] == ["posted_at", "kind", "source", "staff_id", "staff_name"]
    assert "commission_received_cents" in rows[0]
    assert len(rows) == 2
    data = dict(zip(rows[0], rows[1], strict=True))
    assert (data["staff_id"], data["invoice_id"], data["source"]) == (
        staff_id,
        invoice_id,
        "service",
    )
    assert (data["commission_rate_bp"], data["basis_cents"]) == ("1500", "8000")
    assert data["amount_cents"] == "1200"  # 15% of 8000
    assert (data["payment_status"], data["variant_id"]) == ("pending", "")


async def test_the_request_only_enqueues_the_export(client, monkeypatch):
    """The handler never builds the file: with the job not yet run, the export is pending and
    its download is refused."""
    from billing import commission_report

    queued_ids: list[str] = []
    monkeypatch.setattr(commission_report.build_commission_export, "delay", queued_ids.append)
    await as_admin(client)

    queued = await client.post(EXPORTS, json={})

    assert queued.status_code == 202, queued.text
    assert queued_ids == [queued.json()["id"]]
    assert (queued.json()["status"], queued.json()["download_url"]) == ("pending", None)
    refused = await client.get(f"{EXPORTS}/{queued_ids[0]}/csv")
    assert refused.status_code == 409, refused.text


async def test_an_export_with_an_inverted_range_is_refused_before_queueing(client):
    await as_admin(client)
    from datetime import date, timedelta

    today = date.today()
    resp = await client.post(
        EXPORTS, json={"from": today.isoformat(), "to": (today - timedelta(days=1)).isoformat()}
    )
    assert resp.status_code == 422, resp.text


async def test_the_export_routes_require_commission_view_and_admin_mode(client):
    import uuid

    assert (await client.post(EXPORTS, json={})).status_code in (401, 403)
    assert (await client.get(f"{EXPORTS}/{uuid.uuid4()}")).status_code in (401, 403)
    assert (await client.get(f"{EXPORTS}/{uuid.uuid4()}/csv")).status_code in (401, 403)


# --- S2: the received share -------------------------------------------------------------------


def test_received_share_is_clamped_and_symmetric():
    from billing.commission_report import received_share_cents

    assert received_share_cents(1000, 0, 10000) == 0
    assert received_share_cents(1000, 10000, 10000) == 1000
    assert received_share_cents(1000, 12000, 10000) == 1000  # overpaid
    assert received_share_cents(1000, 5000, 0) == 1000  # nothing to collect
    assert received_share_cents(333, 5000, 10000) == 167  # 166.5, half-up
    assert received_share_cents(-333, 5000, 10000) == -167  # a reversal splits the same way

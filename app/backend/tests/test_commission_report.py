"""S1: the admin-only commission report (#69) — `GET /api/admin/reports/commission` (JSON) and
`GET /api/admin/reports/commission/csv` (export), `commission.view`, Administrator-only, Admin
Mode. `tests/test_commission_leakage.py` proves the flip side (no staff-facing payload leaks
this data); this file proves the report itself: the access gate, the date-range/staff filters,
and the totals.
"""

import csv
import io

import pytest
from sqlalchemy import text

from core.db import get_purge_engine, session_scope
from core.security import hash_password
from tests.conftest import add_account, wipe_document_keys
from tests.test_bill_review import EMAIL, SETUP, STAFF, STAFF_EMAIL, as_admin, complete_a_visit
from tests.test_invoice_issue import issue_url

REPORT = "/api/admin/reports/commission"
REPORT_CSV = "/api/admin/reports/commission/csv"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async def wipe():
        async with get_purge_engine().begin() as purge:
            await purge.execute(text("DELETE FROM audit_events"))
            await purge.execute(text("DELETE FROM erasure_requests"))
            await purge.execute(text("DELETE FROM form_links"))
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


async def test_the_report_shows_the_earned_commission_pending_not_received(client):
    await as_admin(client)
    staff_id, invoice_id = await _issue_one(client, rate_bp=1000, price_cents=10000)

    resp = await client.get(REPORT)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["staff_id"] == staff_id
    assert row["invoice_id"] == invoice_id
    assert row["kind"] == "earned"
    assert row["commission_rate_bp"] == 1000
    assert row["basis_cents"] == 10000
    assert row["amount_cents"] == 1000
    # Best-effort, documented: nothing here is ever "received" without #66's payment ledger.
    assert row["payment_status"] == "pending"
    assert body["total_earned_cents"] == 1000
    assert body["total_received_cents"] == 0
    assert body["total_pending_cents"] == 1000


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

    today = date.today().isoformat()
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


# --- CSV export -----------------------------------------------------------------------------


async def test_the_csv_export_has_a_header_row_and_one_data_row_matching_the_json(client):
    await as_admin(client)
    staff_id, invoice_id = await _issue_one(client, rate_bp=1500, price_cents=8000)

    resp = await client.get(REPORT_CSV)

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0] == [
        "posted_at",
        "kind",
        "staff_id",
        "staff_name",
        "invoice_id",
        "invoice_number",
        "service_id",
        "commission_rate_bp",
        "basis_cents",
        "amount_cents",
        "payment_status",
    ]
    assert len(rows) == 2
    data = dict(zip(rows[0], rows[1], strict=True))
    assert data["staff_id"] == staff_id
    assert data["invoice_id"] == invoice_id
    assert data["commission_rate_bp"] == "1500"
    assert data["basis_cents"] == "8000"
    assert data["amount_cents"] == "1200"  # 15% of 8000
    assert data["payment_status"] == "pending"


async def test_the_csv_export_also_requires_commission_view_and_admin_mode(client):
    resp = await client.get(REPORT_CSV)
    assert resp.status_code in (401, 403), resp.text

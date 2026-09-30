"""S1(+S2): treatment receipts + invoice rendering, print/email (#70).

S1 proves the real pipeline over a real PostgreSQL: issuing an invoice queues
`billing.documents.render_invoice_documents` (eager Celery, `tests/conftest.py`), which stores
one `invoice` document and one `treatment_receipt` document per appointment line; both are
downloadable, both log an `audit_access_log` row on open (`core.access_log.LogAccess`, reused
unmodified); emailing either requires a verified sender and attaches the exact stored bytes
through the extended `NotificationProvider.send_email`; a resend never re-renders or re-touches
the invoice.

S2 (`tests/test_notification_email_providers.py` has the adapter-level attachment tests; this
file's own S2 section covers `billing/documents.py`'s pure content-shape logic instead —
`show_clinical_fields`, `payment_status_label`, `receipt_number`, `_plus_years`, and
`render_receipt_html`'s clinical-field gating) — reuses the same real render function rather
than reimplementing its branching, over plain transient ORM objects with no database at all.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from billing.documents import (
    _money,
    _plus_years,
    _rate,
    receipt_number,
    receipt_status,
    render_invoice_html,
    render_receipt_html,
    show_clinical_fields,
)
from billing.models import Invoice, InvoiceLine, InvoiceLineTax
from billing.payments import Balance
from core.db import session_scope
from core.models import Business
from customers.models import Customer
from scheduling.models import Appointment, Service, Staff
from tests.test_bill_review import (
    CUSTOMERS,
    STAFF,
    add_front_desk_account,
    as_admin,
    as_staff,
    complete_a_visit,
    me_staff_id,
)
from tests.test_invoice_issue import claimed_instance, issue_url  # noqa: F401 — autouse fixture

INVOICES = "/api/invoices"


async def _make_practitioner(client) -> str:
    me = await me_staff_id(client)
    resp = await client.patch(
        f"{STAFF}/{me}",
        json={"is_practitioner": True, "designation": "RMT", "licence_number": "12345"},
    )
    assert resp.status_code == 200, resp.text
    return me


async def _make_email_ready() -> None:
    async with session_scope() as db:
        await db.execute(
            text(
                "UPDATE businesses SET email_sender='resend', "
                "resend_api_key_encrypted='sealed', resend_from_address='hello@cedar.example', "
                "resend_domain_verified_at=now()"
            )
        )
        await db.commit()


async def _pay(client, invoice_id: str, amount_cents: int, **overrides) -> None:
    body = {"payer_type": "client", "method": "card", "amount_cents": amount_cents}
    body.update(overrides)
    resp = await client.post(f"{INVOICES}/{invoice_id}/payments", json=body)
    assert resp.status_code == 201, resp.text


async def _issued_invoice_with_emailed_customer(
    client, *, pay: bool = True, **overrides
) -> tuple[str, str, str]:
    """`(invoice_id, customer_id, invoice_number)` for a visit whose client has an email on
    file — `complete_a_visit`'s own `CUSTOMER` fixture has none. `pay` settles it in full, so
    checkout is complete and the treatment receipt is released (R20)."""
    bill_id, _service_id = await complete_a_visit(client, **overrides)
    async with session_scope() as db:
        customer_id = await db.scalar(
            text("SELECT customer_id FROM service_bills WHERE id = :b"), {"b": bill_id}
        )
    patched = await client.patch(f"{CUSTOMERS}/{customer_id}", json={"email": "priya@example.com"})
    assert patched.status_code == 200, patched.text

    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)
    issued = await client.post(issue_url(bill_id), json={})
    assert issued.status_code == 201, issued.text
    if pay:
        await _pay(client, issued.json()["id"], issued.json()["grand_total_cents"])
    return issued.json()["id"], str(customer_id), issued.json()["invoice_number"]


def _pdf_url(customer_id: str, invoice_id: str) -> str:
    return f"{CUSTOMERS}/{customer_id}/invoices/{invoice_id}/pdf"


def _receipt_pdf_url(customer_id: str, invoice_id: str, line_id: str) -> str:
    return f"{CUSTOMERS}/{customer_id}/invoices/{invoice_id}/receipts/{line_id}/pdf"


async def _first_line_id(invoice_id: str) -> str:
    async with session_scope() as db:
        line_id = await db.scalar(
            text("SELECT id FROM invoice_lines WHERE invoice_id = :i"), {"i": invoice_id}
        )
    return str(line_id)


# --- S1: issuing renders both documents --------------------------------------------------------


async def test_issuing_renders_the_invoice_pdf_and_one_receipt_per_appointment(client):
    await as_admin(client)
    invoice_id, _customer_id, _number = await _issued_invoice_with_emailed_customer(client)

    async with session_scope() as db:
        counts = dict(
            (
                await db.execute(
                    text(
                        "SELECT kind, count(*) FROM documents WHERE source_id = :i "
                        "OR source_id IN (SELECT id FROM invoice_lines WHERE invoice_id = :i) "
                        "GROUP BY kind"
                    ),
                    {"i": invoice_id},
                )
            ).all()
        )
    assert counts == {"invoice": 1, "treatment_receipt:paid": 1}


async def test_invoice_pdf_downloads_and_logs_an_audited_read(client):
    await as_admin(client)
    invoice_id, customer_id, number = await _issued_invoice_with_emailed_customer(client)

    resp = await client.get(_pdf_url(customer_id, invoice_id))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")
    assert f"invoice-{number}.pdf" in resp.headers["content-disposition"]

    async with session_scope() as db:
        logs = (
            await db.execute(
                text(
                    "SELECT resource_type, resource_id FROM audit_access_log "
                    "WHERE resource_type = 'invoice_document' AND resource_id = :i"
                ),
                {"i": invoice_id},
            )
        ).all()
    assert logs == [("invoice_document", invoice_id)]


async def test_receipt_pdf_downloads_and_logs_an_audited_read(client):
    await as_admin(client)
    invoice_id, customer_id, number = await _issued_invoice_with_emailed_customer(client)
    line_id = await _first_line_id(invoice_id)

    resp = await client.get(_receipt_pdf_url(customer_id, invoice_id, line_id))
    assert resp.status_code == 200, resp.text
    assert resp.content.startswith(b"%PDF")
    assert f"receipt-{number}.pdf" in resp.headers["content-disposition"]

    async with session_scope() as db:
        logs = (
            await db.execute(
                text(
                    "SELECT resource_type, resource_id FROM audit_access_log "
                    "WHERE resource_type = 'treatment_receipt' AND resource_id = :i"
                ),
                {"i": line_id},
            )
        ).all()
    assert logs == [("treatment_receipt", line_id)]


async def test_a_document_cannot_be_opened_under_another_customer_path(client):
    await as_admin(client)
    invoice_id, _customer_id, _number = await _issued_invoice_with_emailed_customer(client)
    other = await client.post(CUSTOMERS, json={"first_name": "Someone", "last_name": "Else"})
    assert other.status_code == 201, other.text

    resp = await client.get(_pdf_url(other.json()["id"], invoice_id))
    assert resp.status_code == 404


async def test_an_unrendered_document_reports_pending_not_an_error(client):
    from tests.test_form_links import as_owner

    await as_admin(client)
    invoice_id, customer_id, _number = await _issued_invoice_with_emailed_customer(client)
    # A business-keyed document (#55) has no customer to check a retention hold against, so
    # the purge role's own trigger refuses to delete it — only the schema owner can, the same
    # "tests only" escape hatch `tests/conftest.py::wipe_document_keys` already uses.
    await as_owner("DELETE FROM documents WHERE source_id = :i", i=invoice_id)

    resp = await client.get(_pdf_url(customer_id, invoice_id))
    assert resp.status_code == 202
    assert resp.json() == {"status": "rendering"}
    assert resp.headers["cache-control"] == "no-store"


# --- S1: email ------------------------------------------------------------------------------


async def test_emailing_requires_a_verified_sender(client):
    await as_admin(client)
    invoice_id, customer_id, _number = await _issued_invoice_with_emailed_customer(client)

    resp = await client.post(f"{CUSTOMERS}/{customer_id}/invoices/{invoice_id}/email", json={})
    assert resp.status_code == 422, resp.text


async def test_emailing_requires_the_client_have_an_email_on_file(client):
    await as_admin(client)
    bill_id, _service_id = await complete_a_visit(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)
    issued = await client.post(issue_url(bill_id), json={})
    assert issued.status_code == 201, issued.text
    invoice_id, customer_id = issued.json()["id"], issued.json()["customer_id"]
    await _make_email_ready()

    resp = await client.post(f"{CUSTOMERS}/{customer_id}/invoices/{invoice_id}/email", json={})
    assert resp.status_code == 422, resp.text


async def test_emailing_the_invoice_accepts_a_typed_address_when_the_client_has_none(
    client, sent_emails
):
    """#104: the frontend's fallback when `_customer_email` would 422 — a typed `to` in the
    body (the same optional-override shape `email_linked_retail_invoice` already carries)
    goes through instead, exactly like it does for a retail invoice."""
    await as_admin(client)
    bill_id, _service_id = await complete_a_visit(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)
    issued = await client.post(issue_url(bill_id), json={})
    assert issued.status_code == 201, issued.text
    invoice_id, customer_id = issued.json()["id"], issued.json()["customer_id"]
    await _make_email_ready()

    resp = await client.post(
        f"{CUSTOMERS}/{customer_id}/invoices/{invoice_id}/email",
        json={"to": "typed-in@example.com"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "queued", "to": "typed-in@example.com"}
    assert len(sent_emails) == 1
    assert sent_emails[0].to == "typed-in@example.com"


async def test_emailing_the_receipt_accepts_a_typed_address_when_the_client_has_none(
    client, sent_emails
):
    await as_admin(client)
    bill_id, _service_id = await complete_a_visit(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)
    issued = await client.post(issue_url(bill_id), json={})
    assert issued.status_code == 201, issued.text
    invoice_id, customer_id = issued.json()["id"], issued.json()["customer_id"]
    await _pay(client, invoice_id, issued.json()["grand_total_cents"])
    line_id = await _first_line_id(invoice_id)
    await _make_email_ready()

    resp = await client.post(
        f"{CUSTOMERS}/{customer_id}/invoices/{invoice_id}/receipts/{line_id}/email",
        json={"to": "typed-in@example.com"},
    )
    assert resp.status_code == 200, resp.text
    assert len(sent_emails) == 1
    assert sent_emails[0].to == "typed-in@example.com"


async def test_emailing_the_invoice_attaches_the_exact_stored_pdf_bytes(client, sent_emails):
    await as_admin(client)
    invoice_id, customer_id, number = await _issued_invoice_with_emailed_customer(client)
    await _make_email_ready()
    stored = (await client.get(_pdf_url(customer_id, invoice_id))).content

    resp = await client.post(f"{CUSTOMERS}/{customer_id}/invoices/{invoice_id}/email", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "queued", "to": "priya@example.com"}

    assert len(sent_emails) == 1
    [attachment] = sent_emails[0].attachments
    assert attachment.content == stored
    assert attachment.filename == f"invoice-{number}.pdf"
    assert attachment.content_type == "application/pdf"

    async with session_scope() as db:
        logged = await db.scalar(
            text("SELECT count(*) FROM audit_events WHERE event_type = 'invoice.emailed'")
        )
    assert logged == 1


async def test_emailing_the_receipt_attaches_the_exact_stored_pdf_bytes(client, sent_emails):
    await as_admin(client)
    invoice_id, customer_id, number = await _issued_invoice_with_emailed_customer(client)
    await _make_email_ready()
    line_id = await _first_line_id(invoice_id)
    stored = (await client.get(_receipt_pdf_url(customer_id, invoice_id, line_id))).content

    resp = await client.post(
        f"{CUSTOMERS}/{customer_id}/invoices/{invoice_id}/receipts/{line_id}/email", json={}
    )
    assert resp.status_code == 200, resp.text

    assert len(sent_emails) == 1
    [attachment] = sent_emails[0].attachments
    assert attachment.content == stored
    assert attachment.filename == f"receipt-{number}.pdf"


async def test_a_resend_never_rerenders_or_retouches_the_invoice(client, sent_emails):
    await as_admin(client)
    invoice_id, customer_id, _number = await _issued_invoice_with_emailed_customer(client)
    await _make_email_ready()

    first = await client.post(f"{CUSTOMERS}/{customer_id}/invoices/{invoice_id}/email", json={})
    assert first.status_code == 200, first.text
    async with session_scope() as db:
        before_total = await db.scalar(
            text("SELECT grand_total_cents FROM invoices WHERE id = :i"), {"i": invoice_id}
        )

    second = await client.post(f"{CUSTOMERS}/{customer_id}/invoices/{invoice_id}/email", json={})
    assert second.status_code == 200, second.text

    async with session_scope() as db:
        after_total = await db.scalar(
            text("SELECT grand_total_cents FROM invoices WHERE id = :i"), {"i": invoice_id}
        )
        document_count = await db.scalar(
            text("SELECT count(*) FROM documents WHERE source_id = :i"), {"i": invoice_id}
        )
    assert before_total == after_total
    assert document_count == 1  # never a second render — the same stored bytes, resent
    assert len(sent_emails) == 2  # the send itself is not idempotent, only the money is


# --- S1: receipt release and labels follow checkout (R20) --------------------------------------


async def _receipt_kinds(line_id: str) -> list[str]:
    async with session_scope() as db:
        rows = await db.scalars(
            text("SELECT kind FROM documents WHERE source_id = :l ORDER BY created_at"),
            {"l": line_id},
        )
        return list(rows)


async def test_an_unpaid_invoice_releases_no_receipt(client):
    await as_admin(client)
    invoice_id, customer_id, _number = await _issued_invoice_with_emailed_customer(
        client, pay=False
    )
    line_id = await _first_line_id(invoice_id)

    resp = await client.get(_receipt_pdf_url(customer_id, invoice_id, line_id))
    assert resp.status_code == 409, resp.text
    assert await _receipt_kinds(line_id) == []
    # The invoice itself is still printable — only the receipt waits for checkout.
    assert (await client.get(_pdf_url(customer_id, invoice_id))).status_code == 200


async def test_pending_insurer_money_is_labelled_pending_then_paid_on_arrival(client):
    await as_admin(client)
    invoice_id, customer_id, _number = await _issued_invoice_with_emailed_customer(
        client, pay=False
    )
    line_id = await _first_line_id(invoice_id)
    total = (await client.get(f"{INVOICES}/{invoice_id}")).json()["grand_total_cents"]
    insurer = {"payer_type": "insurer", "method": "insurer"}

    await _pay(client, invoice_id, total - 4000)
    assert await _receipt_kinds(line_id) == []  # client portion not yet settled
    await _pay(client, invoice_id, 4000, status="pending", **insurer)
    assert await _receipt_kinds(line_id) == ["treatment_receipt:pending_insurer"]
    assert (await client.get(_receipt_pdf_url(customer_id, invoice_id, line_id))).status_code == 200

    await _pay(client, invoice_id, 4000, **insurer)
    # A new document for the new status; the earlier (immutable) one is kept as history.
    assert await _receipt_kinds(line_id) == [
        "treatment_receipt:pending_insurer",
        "treatment_receipt:paid",
    ]


async def test_an_authorized_balance_exception_releases_a_receipt_that_does_not_say_paid(client):
    await as_admin(client)
    invoice_id, _customer_id, _number = await _issued_invoice_with_emailed_customer(
        client, pay=False
    )
    line_id = await _first_line_id(invoice_id)
    client.cookies.clear()
    await as_admin(client)

    resp = await client.post(
        f"{INVOICES}/{invoice_id}/balance-exceptions", json={"reason": "hardship"}
    )
    assert resp.status_code == 201, resp.text
    assert await _receipt_kinds(line_id) == ["treatment_receipt:authorized_balance"]


async def test_the_email_queue_carries_the_document_id_never_its_bytes(
    client, sent_emails, monkeypatch
):
    from billing.documents import email_document

    await as_admin(client)
    invoice_id, customer_id, _number = await _issued_invoice_with_emailed_customer(client)
    await _make_email_ready()
    queued: list[tuple] = []
    real_delay = email_document.delay

    def spy(*args, **kwargs):
        queued.append((args, kwargs))
        return real_delay(*args, **kwargs)

    monkeypatch.setattr(email_document, "delay", spy)
    resp = await client.post(f"{CUSTOMERS}/{customer_id}/invoices/{invoice_id}/email", json={})
    assert resp.status_code == 200, resp.text

    async with session_scope() as db:
        document_id = await db.scalar(
            text("SELECT id FROM documents WHERE kind = 'invoice' AND source_id = :i"),
            {"i": invoice_id},
        )
    [(args, kwargs)] = queued
    assert args[0] == str(document_id)
    assert all(isinstance(a, str) and len(a) < 200 for a in args)
    assert "attachments" not in kwargs
    assert sent_emails[0].attachments[0].content.startswith(b"%PDF")


# --- S1: the beat reconciler (R23) -------------------------------------------------------------


async def test_the_reconciler_re_renders_documents_that_never_landed(client):
    from billing.documents import reconcile_renders
    from tests.test_form_links import as_owner

    await as_admin(client)
    invoice_id, customer_id, _number = await _issued_invoice_with_emailed_customer(client)
    line_id = await _first_line_id(invoice_id)
    await as_owner("DELETE FROM documents WHERE source_id IN (:i, :l)", i=invoice_id, l=line_id)
    assert (await client.get(_pdf_url(customer_id, invoice_id))).status_code == 202

    reconcile_renders()

    assert (await client.get(_pdf_url(customer_id, invoice_id))).status_code == 200
    assert await _receipt_kinds(line_id) == ["treatment_receipt:paid"]


async def test_the_reconciler_leaves_unreleased_receipts_alone(client):
    from billing.documents import reconcile_renders

    await as_admin(client)
    invoice_id, _customer_id, _number = await _issued_invoice_with_emailed_customer(
        client, pay=False
    )
    line_id = await _first_line_id(invoice_id)

    reconcile_renders()

    assert await _receipt_kinds(line_id) == []


# --- S1: clinical field gating, over the real HTTP surface -----------------------------------


async def test_a_regulated_health_receipt_shows_the_practitioners_credentials(client):
    await as_admin(client)
    await _make_practitioner(client)
    invoice_id, customer_id, _number = await _issued_invoice_with_emailed_customer(client)
    line_id = await _first_line_id(invoice_id)

    pdf = (await client.get(_receipt_pdf_url(customer_id, invoice_id, line_id))).content
    assert pdf.startswith(b"%PDF")  # designation text is rendered into the PDF stream itself;
    # the exact clinical-gating *content* is proven directly against `render_receipt_html`
    # below (S2) rather than by decoding a compressed PDF byte stream here.


# --- S2: pure content-shape logic, no database ------------------------------------------------


def _business(**overrides) -> Business:
    return Business(
        id=1,
        name="Cedar Lane Clinic",
        timezone="America/Toronto",
        currency_symbol="$",
        **overrides,
    )


def test_show_clinical_fields_is_true_only_for_regulated_health():
    assert show_clinical_fields(_business(retention_profile="regulated_health")) is True
    assert show_clinical_fields(_business(retention_profile="general_business")) is False


def _bal(**overrides) -> Balance:
    fields = {
        "outstanding_cents": 0,
        "pending_insurer_cents": 0,
        "client_outstanding_cents": 0,
        "checkout_complete": True,
        "refunded_cents": 0,
    }
    fields.update(overrides)
    return Balance(**fields)


def test_receipt_status_follows_the_ledger_and_never_says_paid_for_pending_money():
    issued = Invoice(status="issued")
    line = InvoiceLine(line_total_cents=10000, prepaid_cents=0)
    assert receipt_status(issued, line, _bal(checkout_complete=False)) is None
    assert receipt_status(Invoice(status="cancelled"), line, _bal()) is None
    assert receipt_status(issued, line, _bal()) == "Paid"
    pending = _bal(outstanding_cents=4000, pending_insurer_cents=4000)
    assert receipt_status(issued, line, pending) == "Pending insurer"
    assert receipt_status(issued, line, _bal(outstanding_cents=500)) == (
        "Balance outstanding (authorized)"
    )
    prepaid = InvoiceLine(line_total_cents=10000, prepaid_cents=10000)
    assert receipt_status(issued, prepaid, pending) == "Prepaid (package credit)"


def test_money_is_formatted_from_integer_cents():
    assert _money(0, "$") == "$0.00"
    assert _money(5, "$") == "$0.05"
    assert _money(-123456, "$") == "-$1,234.56"
    assert _money(10**15 + 1, "$") == "$10,000,000,000,000.01"  # no float rounding


def test_rate_is_formatted_from_ppm_with_trailing_zeros_trimmed():
    """#119: a receipt's own tax-rate display, in parts per million now, not basis points."""
    assert _rate(50_000) == "5"  # not "5.00" or "5.000"
    assert _rate(70_000) == "7"
    assert _rate(130_000) == "13"
    assert _rate(99_750) == "9.975"  # QST, exact
    assert _rate(0) == "0"
    # Whatever an administrator stored is what the receipt prints: every ppm is a whole
    # ten-thousandth of a percent, so four decimals show any rate exactly, never truncated.
    assert _rate(12_345) == "1.2345"
    assert _rate(1) == "0.0001"


def test_an_overridden_invoice_prints_the_billed_tax_not_the_computed_one():
    invoice = Invoice(
        invoice_number=3,
        status="issued",
        issued_at=datetime(2026, 1, 5, 15, 0, tzinfo=UTC),
        computed_subtotal_cents=10000,
        computed_discount_total_cents=0,
        computed_tax_total_cents=999,
        tax_totals_by_component={"GST": 450},
        override_applied_cents=9450,
        override_reason="goodwill",
        grand_total_cents=9450,
        lines=[
            InvoiceLine(
                id=uuid.uuid4(),
                price_cents=10000,
                discounted_cents=9000,
                override_adjustment_cents=-1000,
                tax_cents=450,
                line_total_cents=9450,
                created_at=datetime(2026, 1, 5, tzinfo=UTC),
            )
        ],
    )
    customer = Customer(first_name="Priya", last_name="Nair")
    html = render_invoice_html(invoice, _business(), customer, {})
    assert "$4.50" in html
    assert "$9.99" not in html
    assert "-$10.00" in html  # the authorized adjustment, so the totals add up to $94.50


def test_receipt_number_is_the_invoice_number_and_the_lines_ordinal():
    invoice = Invoice(invoice_number=7)
    assert receipt_number(invoice, 1) == "7-1"
    assert receipt_number(invoice, 2) == "7-2"


def test_plus_years_handles_the_leap_day_edge_case():
    assert _plus_years(datetime(2024, 2, 29, tzinfo=UTC), 6) == datetime(2030, 3, 1, tzinfo=UTC)
    assert _plus_years(datetime(2026, 6, 15, tzinfo=UTC), 6) == datetime(2032, 6, 15, tzinfo=UTC)


def _transient_receipt_fixtures(*, retention_profile: str):
    business = _business(retention_profile=retention_profile, receipt_footer=None)
    customer = Customer(id=uuid.uuid4(), first_name="Priya", last_name="Nair")
    staff = Staff(
        id=uuid.uuid4(),
        display_name="Ana Rivera",
        is_practitioner=True,
        designation="RMT",
        licence_number="12345",
        colour="teal",
    )
    service = Service(id=uuid.uuid4(), name="Swedish Massage", duration_minutes=60)
    appointment = Appointment(
        id=uuid.uuid4(),
        starts_at=datetime(2026, 1, 5, 15, 0, tzinfo=UTC),
        ends_at=datetime(2026, 1, 5, 16, 0, tzinfo=UTC),
        staff=staff,
        customer=customer,
        service=service,
    )
    invoice = Invoice(id=uuid.uuid4(), invoice_number=1, status="issued")
    line = InvoiceLine(
        id=uuid.uuid4(),
        appointment_id=appointment.id,
        price_cents=12000,
        discounted_cents=0,
        pretax_cents=12000,
        tax_cents=1560,
        line_total_cents=13560,
        taxes=[
            InvoiceLineTax(
                invoice_line_id=uuid.uuid4(),
                component_code="GST",
                rate_ppm=50_000,
                amount_cents=600,
            ),
        ],
    )
    return invoice, line, appointment, business


def test_render_receipt_html_includes_credentials_only_for_regulated_health():
    invoice, line, appointment, regulated = _transient_receipt_fixtures(
        retention_profile="regulated_health"
    )
    html = render_receipt_html(invoice, line, appointment, regulated, 1, payment_status="Paid")
    assert "RMT" in html
    assert "12345" in html

    invoice2, line2, appointment2, general = _transient_receipt_fixtures(
        retention_profile="general_business"
    )
    html2 = render_receipt_html(invoice2, line2, appointment2, general, 1, payment_status="Paid")
    assert "RMT" not in html2
    assert "12345" not in html2


def test_render_receipt_html_shows_the_frozen_attributable_value_never_a_live_price():
    invoice, line, appointment, business = _transient_receipt_fixtures(
        retention_profile="general_business"
    )
    html = render_receipt_html(invoice, line, appointment, business, 1, payment_status="Paid")
    assert "135.60" in html  # line.line_total_cents, not the service's live price
    assert "Paid" in html

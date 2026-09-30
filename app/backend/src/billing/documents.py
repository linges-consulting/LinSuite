"""Invoice & treatment-receipt PDF rendering, delivery and reconciliation (#70, M4 review T4).

Three stored document kinds, all sealed under the business key (#55/ADR-0003, `key_owner=
"business"`, `linked_customer_id` = the client, NULL for an anonymous retail sale, `retain_until`
= issue + six years for CRA):

- **`invoice`** — one per issued `Invoice`, service *or* package purchase (`source_id=invoice.id`).
- **`retail_invoice`** — one per issued `RetailInvoice` (M4 review R22).
- **`treatment_receipt:<status>`** — one per `InvoiceLine` (one appointment) *per payment
  status* (`source_id=line.id`). A receipt is released only once the invoice is issued and
  checkout is complete (`billing.payments` `Balance.checkout_complete`, review R20), and its
  label is derived from the ledger: Prepaid (package credit) / Pending insurer / Paid / an
  authorized outstanding balance — never "Paid" for money not yet received. `documents` rows
  are immutable (0026), so a label-changing event (payment, correction, refund, balance
  exception) never rewrites a stored receipt: the status is part of the document's identity,
  a new status is a new row, and the earlier one stays as history. `store_document`'s
  `ON CONFLICT DO NOTHING` on `(kind, source_id)` keeps every render and retry idempotent.

**Rendered in Celery, never inline** (CLAUDE.md). Issue and every ledger write queue
`render_invoice_documents`/`render_retail_invoice_document` *after* their own commit, so a
render failure never unwinds a committed checkout. `reconcile_renders` (beat, every 5 min —
the `forms/tasks.py::reconcile_archives` precedent) repairs a lost enqueue: issued invoices with
no stored document, and checkout-complete receipts whose current-status document never landed.

**Email carries identifiers, not bytes** (review R21, spec §169): `email_document` takes a
document id and fetches/decrypts inside the worker.

**Display-only joins, never a money source.** Appointment/service/staff/product rows supply
names and dates only; every figure comes from the frozen invoice/line/tax columns.
"""

import base64
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from jinja2 import Environment
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from billing.keys import business_key
from billing.models import (
    Invoice,
    InvoiceBalanceAuthorization,
    InvoiceLine,
    InvoicePayment,
    InvoicePaymentTransfer,
    InvoiceRefund,
    RetailInvoice,
)
from billing.payments import Balance, balance, balances
from core.celery_app import celery_app
from core.config import get_settings
from core.db import run_task
from core.documents import fetch_document, store_document
from core.models import Business, Document
from customers.models import Customer
from forms.render import html_to_pdf
from inventory.models import Product, ProductVariant
from notifications.providers import EmailAttachment
from notifications.tasks import deliver_email
from scheduling.models import Appointment
from settings.models import LOGO, BrandingAsset

REGULATED_HEALTH = "regulated_health"

# Receipt payment statuses (R20) and the document kind each is stored under.
PAID = "Paid"
PREPAID = "Prepaid (package credit)"
PENDING_INSURER = "Pending insurer"
AUTHORIZED_BALANCE = "Balance outstanding (authorized)"
_RECEIPT_KIND = {
    PAID: "treatment_receipt:paid",
    PREPAID: "treatment_receipt:prepaid",
    PENDING_INSURER: "treatment_receipt:pending_insurer",
    AUTHORIZED_BALANCE: "treatment_receipt:authorized_balance",
}
RECEIPT_KINDS = tuple(_RECEIPT_KIND.values())

_ENV = Environment(autoescape=True)

_STYLE = """
@page { size: Letter; margin: 20mm; }
body { font: 11pt sans-serif; color: #18212f; line-height: 1.5; }
header img { max-height: 50px; max-width: 200px; }
h1 { font-size: 20pt; } h2 { font-size: 13pt; margin: 4px 0; } h3 { font-size: 11pt; }
table { width: 100%; border-collapse: collapse; margin-top: 10px; }
th, td { text-align: left; padding: 6px 4px; border-bottom: 1px solid #ddd; font-size: 10pt; }
th { border-bottom: 2px solid #18212f; }
.right { text-align: right; }
.totals td { border: none; padding: 2px 4px; }
.grand { font-weight: bold; font-size: 12pt; }
.muted { color: #556; font-size: 9pt; }
.status { display: inline-block; padding: 2px 8px; border: 1px solid #18212f; border-radius: 3px; }
footer { margin-top: 30px; font-size: 9pt; border-top: 1px solid #ccc; padding-top: 10px; }
"""

_INVOICE_TEMPLATE = _ENV.from_string(
    """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{{ title }}</title>
<style>"""
    + _STYLE
    + """</style></head><body>
<header>{% if logo_uri %}<img src="{{ logo_uri }}" alt="">{% endif %}
<h2>{{ business.name }}</h2>
<p class="muted">{{ address_lines|join(', ') }}<br>
{% if business.gst_hst_number %}GST/HST {{ business.gst_hst_number }} {% endif %}
{% if business.pst_qst_number %}PST/QST {{ business.pst_qst_number }}{% endif %}</p>
</header>
<h1>{{ title }}</h1>
<p>Billed to: {{ billed_to }}<br>
Issued: {{ issued_local }} ({{ business.timezone }})</p>
<table><thead><tr><th>Item</th><th></th><th class="right">Price</th>
<th class="right">Discount</th><th class="right">Tax</th><th class="right">Total</th></tr></thead>
<tbody>{% for row in rows %}
<tr><td>{{ row.description }}</td><td>{{ row.detail }}</td>
<td class="right">{{ money(row.price_cents) }}</td>
<td class="right">{{ money(-row.discount_cents) if row.discount_cents else "" }}</td>
<td class="right">{{ money(row.tax_cents) }}</td>
<td class="right">{{ money(row.total_cents) }}</td></tr>
{% endfor %}</tbody></table>
<table class="totals">
{% for label, cents in totals %}
<tr><td>{{ label }}</td><td class="right">{{ money(cents) }}</td></tr>
{% endfor %}
<tr class="grand"><td>Total</td><td class="right">{{ money(grand_total_cents) }}</td></tr>
</table>
<footer>{% if business.receipt_footer %}<div>{{ business.receipt_footer }}</div>{% endif %}
<div>{{ title }} &middot; {{ business.name }}</div></footer>
</body></html>"""
)

_RECEIPT_TEMPLATE = _ENV.from_string(
    """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Receipt {{ receipt_number }}</title>
<style>"""
    + _STYLE
    + """</style></head><body>
<header>{% if logo_uri %}<img src="{{ logo_uri }}" alt="">{% endif %}
<h2>{{ business.name }}</h2>
<p class="muted">{{ address_lines|join(', ') }}</p>
</header>
<h1>Treatment Receipt</h1>
<p>Receipt #{{ receipt_number }} &middot; Invoice #{{ invoice.invoice_number }}<br>
Client: {{ customer.first_name }} {{ customer.last_name }}<br>
Date of service: {{ service_date }}</p>
<h3>{{ appointment.service.name }}</h3>
<p>Provided by {{ staff.display_name }}
{% if show_clinical and staff.designation %}
&mdash; {{ staff.designation }}
{% if staff.licence_number %}(licence {{ staff.licence_number }}){% endif %}
{% endif %}</p>
<table><thead><tr><th>Component</th><th class="right">Rate</th>
<th class="right">Amount</th></tr></thead>
<tbody>{% for tax in taxes %}
<tr><td>{{ tax.code }}</td><td class="right">{{ tax.rate }}%</td>
<td class="right">{{ money(tax.amount_cents) }}</td></tr>
{% endfor %}</tbody></table>
<table class="totals">
<tr class="grand"><td>Amount attributable to this visit</td>
<td class="right">{{ money(line.line_total_cents) }}</td></tr>
<tr><td>Payment status</td>
<td class="right"><span class="status">{{ payment_status }}</span></td></tr>
</table>
<footer>{% if business.receipt_footer %}<div>{{ business.receipt_footer }}</div>{% endif %}
<div>Receipt #{{ receipt_number }} &middot; {{ business.name }}</div></footer>
</body></html>"""
)


def _address_lines(business: Business) -> list[str]:
    city_line = " ".join(
        part for part in (business.city, business.province, business.postal_code) if part
    )
    return [part for part in (business.address_line1, business.address_line2, city_line) if part]


def _plus_years(when: datetime, years: int) -> datetime:
    """A financial document's CRA retention hold, six years out from `issued_at`. 29 Feb has
    no anniversary in a non-leap target year; the later reading is safer, so 1 Mar."""
    try:
        return when.replace(year=when.year + years)
    except ValueError:
        return when.replace(month=3, day=1, year=when.year + years)


def _money(cents: int, symbol: str) -> str:
    """Integer cents only, never through a float (CLAUDE.md: money is integer cents)."""
    dollars, rem = divmod(abs(cents), 100)
    return f"{'-' if cents < 0 else ''}{symbol}{dollars:,}.{rem:02d}"


def _rate(ppm: int) -> str:
    """A tax rate's percent, exactly as stored, trailing zeros trimmed (5, not 5.0000; 9.975
    for QST) — `rate_ppm` is parts per million, `10_000` ppm to a percentage point, so four
    decimals show any stored rate exactly and nothing is ever truncated or rounded away."""
    whole, remainder = divmod(ppm, 10_000)
    return f"{whole}.{remainder:04d}".rstrip("0").rstrip(".")


def show_clinical_fields(business: Business) -> bool:
    """A practitioner's designation/licence belongs only on a `regulated_health` business's
    receipts — a salon or retail shop is never forced to carry it (#70)."""
    return business.retention_profile == REGULATED_HEALTH


def receipt_status(invoice: Invoice, line: InvoiceLine, bal: Balance) -> str | None:
    """The payment status a treatment receipt shows, or None while it is not yet released
    (R20): not issued (cancelled — its replacement carries the visit) or checkout incomplete.
    A fully prepaid visit is Prepaid; otherwise any approved-but-unarrived insurer money makes
    it Pending insurer; an admin-excepted unpaid balance says so; only a settled invoice is
    Paid."""
    if invoice.status != "issued" or not bal.checkout_complete:
        return None
    if line.prepaid_cents and line.prepaid_cents >= line.line_total_cents:
        return PREPAID
    if bal.pending_insurer_cents > 0:
        return PENDING_INSURER
    if bal.outstanding_cents > 0:
        return AUTHORIZED_BALANCE
    return PAID


def receipt_kind(status: str) -> str:
    return _RECEIPT_KIND[status]


def receipt_number(invoice: Invoice, ordinal: int) -> str:
    """The invoice's gapless number plus the line's 1-based position: "1-1", "1-2"."""
    return f"{invoice.invoice_number}-{ordinal}"


def _render_page(
    business: Business,
    *,
    title: str,
    billed_to: str,
    issued_at: datetime,
    rows: list[dict],
    totals: list[tuple[str, int]],
    grand_total_cents: int,
    logo_uri: str | None,
) -> str:
    return _INVOICE_TEMPLATE.render(
        business=business,
        title=title,
        billed_to=billed_to,
        issued_local=issued_at.astimezone(ZoneInfo(business.timezone)).strftime("%Y-%m-%d %H:%M"),
        rows=rows,
        totals=totals,
        grand_total_cents=grand_total_cents,
        logo_uri=logo_uri,
        address_lines=_address_lines(business),
        money=lambda cents: _money(cents, business.currency_symbol),
    )


def render_invoice_html(
    invoice: Invoice,
    business: Business,
    customer: Customer,
    appointments: dict[uuid.UUID, Appointment],
    *,
    logo_uri: str | None = None,
) -> str:
    """A service invoice (one row per appointment line) or a package-purchase invoice (#71:
    one row, the package). Tax is the *billed* tax (`tax_totals_by_component`, review R1) —
    under an admin override that is the distributed per-line tax, so the totals match the
    lines; the override's own adjustment gets its own row."""
    lines = sorted(invoice.lines, key=lambda line: line.created_at)
    billed_tax = sum(invoice.tax_totals_by_component.values())
    if invoice.package_purchase is not None:
        purchase = invoice.package_purchase
        rows = [
            {
                "description": purchase.name,
                "detail": "Package",
                "price_cents": purchase.price_cents,
                "discount_cents": 0,
                "tax_cents": billed_tax,
                "total_cents": invoice.grand_total_cents,
            }
        ]
    else:
        rows = []
        for line in lines:
            appointment = appointments.get(line.id)
            rows.append(
                {
                    "description": appointment.service.name if appointment else "",
                    "detail": appointment.staff.display_name if appointment else "",
                    "price_cents": line.price_cents,
                    "discount_cents": (
                        line.price_cents + line.override_adjustment_cents - line.discounted_cents
                    ),
                    "tax_cents": line.tax_cents,
                    "total_cents": line.line_total_cents,
                }
            )
    totals = [
        ("Subtotal", invoice.computed_subtotal_cents),
        ("Discounts", -invoice.computed_discount_total_cents),
    ]
    adjustment = sum(line.override_adjustment_cents for line in lines)
    if invoice.override_applied_cents is not None and adjustment:
        totals.append(
            (f"Admin/owner-authorized adjustment ({invoice.override_reason})", adjustment)
        )
    totals += [
        (f"Tax {code}", cents) for code, cents in sorted(invoice.tax_totals_by_component.items())
    ]
    return _render_page(
        business,
        title=f"Invoice #{invoice.invoice_number}",
        billed_to=f"{customer.first_name} {customer.last_name}",
        issued_at=invoice.issued_at,
        rows=rows,
        totals=totals,
        grand_total_cents=invoice.grand_total_cents,
        logo_uri=logo_uri,
    )


def render_retail_invoice_html(
    invoice: RetailInvoice,
    business: Business,
    customer: Customer | None,
    names: dict[uuid.UUID, str],
    *,
    logo_uri: str | None = None,
) -> str:
    """A retail invoice (R22); `names` maps variant id -> "Product — Variant"."""
    rows = [
        {
            "description": names.get(line.variant_id, ""),
            "detail": f"Qty {line.quantity}",
            "price_cents": line.unit_price_cents * line.quantity,
            "discount_cents": line.discount_cents,
            "tax_cents": line.tax_cents,
            "total_cents": line.line_total_cents,
        }
        for line in sorted(invoice.lines, key=lambda line: line.created_at)
    ]
    totals = [("Subtotal", invoice.subtotal_cents), ("Discounts", -invoice.discount_total_cents)]
    totals += [
        (f"Tax {code}", cents) for code, cents in sorted(invoice.tax_totals_by_component.items())
    ]
    return _render_page(
        business,
        title=f"Retail invoice #{invoice.invoice_number}",
        billed_to=f"{customer.first_name} {customer.last_name}" if customer else "Walk-in customer",
        issued_at=invoice.issued_at,
        rows=rows,
        totals=totals,
        grand_total_cents=invoice.grand_total_cents,
        logo_uri=logo_uri,
    )


def render_receipt_html(
    invoice: Invoice,
    line: InvoiceLine,
    appointment: Appointment,
    business: Business,
    ordinal: int,
    *,
    payment_status: str,
    logo_uri: str | None = None,
) -> str:
    zone = ZoneInfo(business.timezone)
    return _RECEIPT_TEMPLATE.render(
        business=business,
        invoice=invoice,
        line=line,
        appointment=appointment,
        staff=appointment.staff,
        customer=appointment.customer,
        logo_uri=logo_uri,
        address_lines=_address_lines(business),
        receipt_number=receipt_number(invoice, ordinal),
        service_date=appointment.starts_at.astimezone(zone).strftime("%Y-%m-%d"),
        payment_status=payment_status,
        show_clinical=show_clinical_fields(business),
        taxes=[
            {"code": t.component_code, "rate": _rate(t.rate_ppm), "amount_cents": t.amount_cents}
            for t in line.taxes
        ],
        money=lambda cents: _money(cents, business.currency_symbol),
    )


# --- worker tasks -------------------------------------------------------------------------------

_RETRY = {
    "autoretry_for": (Exception,),
    "retry_backoff": True,
    "retry_jitter": True,
    "max_retries": 5,
}


async def _logo_uri(db: AsyncSession) -> str | None:
    logo = await db.get(BrandingAsset, LOGO)
    return "data:image/png;base64," + base64.b64encode(logo.data).decode() if logo else None


async def _stored(db: AsyncSession, source_ids: list[uuid.UUID]) -> set[tuple[str, uuid.UUID]]:
    rows = await db.execute(
        select(Document.kind, Document.source_id).where(Document.source_id.in_(source_ids))
    )
    return {(kind, source_id) for kind, source_id in rows}


def _task_engine():
    # A fresh engine per task event loop, application role only (`forms/tasks.py`).
    return create_async_engine(get_settings().database_url, poolclass=NullPool)


@celery_app.task(name="billing.documents.render_invoice_documents", **_RETRY)
def render_invoice_documents(invoice_id: str) -> None:
    """Queued after issue and after every ledger write on a service/package invoice. Renders
    only what is missing, so a re-queue with nothing new is a couple of cheap reads."""
    run_task(_render_invoice_documents, uuid.UUID(invoice_id))


async def _render_invoice_documents(invoice_id: uuid.UUID) -> None:
    engine = _task_engine()
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db, db.begin():
            invoice = await db.get(Invoice, invoice_id)
            if invoice is None:
                return
            lines = sorted(invoice.lines, key=lambda line: line.created_at)
            have = await _stored(db, [invoice.id, *(line.id for line in lines)])
            bal = await balance(db, invoice)
            wanted = [
                (ordinal, line, status)
                for ordinal, line in enumerate(lines, start=1)
                if (status := receipt_status(invoice, line, bal)) is not None
                and (receipt_kind(status), line.id) not in have
            ]
            need_invoice = ("invoice", invoice.id) not in have
            if not need_invoice and not wanted:
                return

            business = await db.get(Business, 1)
            customer = await db.get(Customer, invoice.customer_id)
            logo_uri = await _logo_uri(db)
            key = await business_key(db)
            appointments: dict[uuid.UUID, Appointment] = {}
            for line in lines:
                appointment = await db.get(Appointment, line.appointment_id)
                if appointment is not None:
                    appointments[line.id] = appointment
            common = {
                "key": key,
                "key_owner": "business",
                "linked_customer_id": invoice.customer_id,
                "retain_until": _plus_years(invoice.issued_at, 6),
                "content_type": "application/pdf",
            }
            if need_invoice:
                html = render_invoice_html(
                    invoice, business, customer, appointments, logo_uri=logo_uri
                )
                await store_document(
                    db, kind="invoice", source_id=invoice.id, content=html_to_pdf(html), **common
                )
            for ordinal, line, status in wanted:
                appointment = appointments.get(line.id)
                if appointment is None:
                    continue
                html = render_receipt_html(
                    invoice,
                    line,
                    appointment,
                    business,
                    ordinal,
                    payment_status=status,
                    logo_uri=logo_uri,
                )
                await store_document(
                    db,
                    kind=receipt_kind(status),
                    source_id=line.id,
                    content=html_to_pdf(html),
                    **common,
                )
    finally:
        await engine.dispose()


@celery_app.task(name="billing.documents.render_retail_invoice_document", **_RETRY)
def render_retail_invoice_document(invoice_id: str) -> None:
    run_task(_render_retail_invoice_document, uuid.UUID(invoice_id))


async def _render_retail_invoice_document(invoice_id: uuid.UUID) -> None:
    engine = _task_engine()
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db, db.begin():
            invoice = await db.get(RetailInvoice, invoice_id)
            if invoice is None or ("retail_invoice", invoice.id) in await _stored(db, [invoice.id]):
                return
            business = await db.get(Business, 1)
            customer = await db.get(Customer, invoice.customer_id) if invoice.customer_id else None
            rows = await db.execute(
                select(ProductVariant.id, Product.name, ProductVariant.name)
                .join(Product, Product.id == ProductVariant.product_id)
                .where(ProductVariant.id.in_([line.variant_id for line in invoice.lines]))
            )
            names = {vid: f"{product} — {variant}" for vid, product, variant in rows}
            html = render_retail_invoice_html(
                invoice, business, customer, names, logo_uri=await _logo_uri(db)
            )
            await store_document(
                db,
                key=await business_key(db),
                key_owner="business",
                linked_customer_id=invoice.customer_id,
                retain_until=_plus_years(invoice.issued_at, 6),
                kind="retail_invoice",
                source_id=invoice.id,
                content=html_to_pdf(html),
                content_type="application/pdf",
            )
    finally:
        await engine.dispose()


_BATCH = 100


def _missing(model: type[Invoice] | type[RetailInvoice], kind: str):
    return (
        select(model.id)
        .where(~exists().where(Document.kind == kind, Document.source_id == model.id))
        .order_by(model.issued_at)
        .limit(_BATCH)
    )


def _last_ledger_event():
    """When `invoices.id`'s receipt status last could have changed: issue, or its newest
    payment, correction, exception, refund or incoming transfer. `greatest` skips NULLs."""

    def newest(column, owner):
        return select(func.max(column)).where(owner == Invoice.id).scalar_subquery()

    return func.greatest(
        Invoice.issued_at,
        newest(InvoicePayment.recorded_at, InvoicePayment.invoice_id),
        newest(InvoiceBalanceAuthorization.authorized_at, InvoiceBalanceAuthorization.invoice_id),
        newest(InvoiceRefund.refunded_at, InvoiceRefund.invoice_id),
        newest(InvoicePaymentTransfer.transferred_at, InvoicePaymentTransfer.to_invoice_id),
    )


async def _pending_renders() -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """(service/package invoice ids, retail invoice ids) whose rendering never landed."""
    engine = _task_engine()
    try:
        async with AsyncSession(engine) as db:
            invoice_ids = set(await db.scalars(_missing(Invoice, "invoice")))
            retail_ids = list(await db.scalars(_missing(RetailInvoice, "retail_invoice")))

            # Receipts: issued service invoices with a line lacking any receipt rendered since
            # the last ledger event, then narrowed in Python to those whose *current* status
            # document is missing. ponytail: unpaid invoices re-qualify every run (no receipt
            # yet); fine while the outstanding list is small — add a watermark if it isn't.
            stale = await db.scalars(
                select(Invoice).where(
                    Invoice.status == "issued",
                    Invoice.service_bill_id.is_not(None),
                    exists().where(
                        InvoiceLine.invoice_id == Invoice.id,
                        ~exists().where(
                            Document.source_id == InvoiceLine.id,
                            Document.kind.in_(RECEIPT_KINDS),
                            Document.created_at >= _last_ledger_event(),
                        ),
                    ),
                )
            )
            candidates = list(stale)
            bals = await balances(db, candidates)
            have = await _stored(db, [line.id for i in candidates for line in i.lines])
            for invoice in candidates:
                if len(invoice_ids) >= _BATCH:
                    break
                if any(
                    (status := receipt_status(invoice, line, bals[invoice.id])) is not None
                    and (receipt_kind(status), line.id) not in have
                    for line in invoice.lines
                ):
                    invoice_ids.add(invoice.id)
            return list(invoice_ids), retail_ids
    finally:
        await engine.dispose()


@celery_app.task(name="billing.documents.reconcile_renders", **_RETRY)
def reconcile_renders() -> None:
    """Beat repairs the commit/enqueue gap (`forms/tasks.py::reconcile_archives`). Bounded
    batches and the renderers' idempotence keep overlapping runs harmless."""
    invoice_ids, retail_ids = run_task(_pending_renders)
    for invoice_id in invoice_ids:
        render_invoice_documents.delay(str(invoice_id))
    for invoice_id in retail_ids:
        render_retail_invoice_document.delay(str(invoice_id))


# --- email (R21: identifiers in the queue, bytes fetched here) ---------------------------------


async def _fetch_business_document(document_id: uuid.UUID) -> bytes:
    engine = _task_engine()
    try:
        async with AsyncSession(engine) as db, db.begin():
            content, _ = await fetch_document(
                db, key=await business_key(db), key_owner="business", document_id=document_id
            )
            return content
    finally:
        await engine.dispose()


@celery_app.task(name="billing.documents.email_document", **_RETRY)
def email_document(
    document_id: str,
    to: str,
    subject: str,
    text: str,
    filename: str,
    *,
    customer_id: str | None = None,
    notification_type: str | None = None,
) -> None:
    content = run_task(_fetch_business_document, uuid.UUID(document_id))
    deliver_email(
        to,
        subject,
        text,
        attachments=[EmailAttachment(filename=filename, content=content)],
        customer_id=customer_id,
        notification_type=notification_type,
    )


def queue_invoice_render(invoice: Invoice | RetailInvoice) -> None:
    """Call after the commit that issued the invoice or changed its ledger."""
    if isinstance(invoice, RetailInvoice):
        render_retail_invoice_document.delay(str(invoice.id))
    else:
        render_invoice_documents.delay(str(invoice.id))

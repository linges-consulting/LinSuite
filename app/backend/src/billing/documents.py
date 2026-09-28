"""Invoice & treatment-receipt PDF rendering, delivery and audited reads (#70).

Two related but distinct documents render from the same frozen #65 snapshot:

- **Invoice** — one per issued `Invoice` (`kind="invoice"`, `source_id=invoice.id`): the whole
  bill, every line, the computed/override totals — CLAUDE.md's *voidable* document class.
- **Treatment receipt** — one per `InvoiceLine`, i.e. one per completed, checked-out
  appointment (`kind="treatment_receipt"`, `source_id=invoice_line.id`) — never one per
  invoice: a grouped visit's invoice can cover several appointments, and CLAUDE.md's own "why
  separate document types" paragraph is explicit that each needs its own claimable date of
  service. `InvoiceLine.appointment_id` is unique (inherited from `ServiceBillLine`'s own
  uniqueness), so "one line" and "one appointment" are the same count.

**"Checkout complete" approximation (M4 Wave 5 dependency gap, tracked for #66).** The
acceptance criterion is "released only after its bill has been reviewed and checkout
completed" — but #66 (the payment ledger, which will define what "checkout complete" actually
means: full settlement, or an admin-authorized exception) is a sibling ticket running in
parallel, not merged, and not in this ticket's own dependency graph (only #65/#55 are).
Rendering is instead triggered by `Invoice.status == "issued"` (#65, already merged): issuing
already requires the bill to be fully reviewed with no pending/stale override
(`billing/invoices.py::issue_invoice`'s own refusal conditions), and for a plain service sale
(no packages/insurer billing yet — #71/#72 are unbuilt this milestone) "issued" is the closest
available proxy for "paid enough to receipt". `payment_status_label` below is the one function
to change once #66 lands: swap its constant `"Paid"` for a read of
`billing.is_checkout_complete(invoice)` (the predicate #66 is expected to define) and let a
still-outstanding balance render "pending" instead of "paid" — never the reverse.

**Rendered in a Celery task, never inline** (CLAUDE.md: PDF generation is a worker job, never
the request path). `render_invoice_documents` is queued from
`billing/invoices.py::issue_invoice` *after* that transaction's own `db.commit()` — the same
"commit, then queue" order `scheduling/public.py` already uses for `notify_booking_confirmed`
— so a rendering or delivery failure can never unwind the invoice/bill-issue transaction that
already landed. `store_document`'s own `ON CONFLICT DO NOTHING` on `(kind, source_id)` is what
makes both this task and a resend idempotent: a retry, a duplicate enqueue, or a second
`.../pdf`/`.../email` call all resolve to the same stored bytes — never a second render, and
never a second touch of `invoices`/`service_bills`/commission/stock data.

**Business-owned document tier** (#55/ADR-0003): both documents seal under
`billing.keys.business_key`, `key_owner="business"`, `linked_customer_id=invoice.customer_id`
— a financial document must outlive any one customer's crypto-shred (CRA's six-year rule),
exactly the tier #55 built for this. `retain_until` is `issued_at` plus six years
(`_plus_years` — the same "later reading is safer" leap-day tie-break
`customers/retention.py::_years_after` already uses for its own, DOB-specific purpose;
duplicated in miniature here rather than imported, since that helper is private to that
module and reads a customer's date of birth, which has nothing to do with a financial
document's own CRA clock).

**Display-only joins, never a money source.** `InvoiceLine` carries `appointment_id`/
`service_id`/`staff_id` "for display only... never read back into a money calculation"
(`billing/models.py`'s own docstring) — this module is exactly that display reader: it loads
each line's `Appointment` (already `lazy="joined"` onto its own `service`/`staff`/`customer`)
for the service name, the provider's name/designation/licence, and the service date, while
every dollar figure rendered comes only from the frozen `InvoiceLine`/`InvoiceLineTax` columns
`billing/invoices.py::issue_invoice` already froze.
"""

import base64
import uuid
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from jinja2 import Environment
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from billing.keys import business_key
from billing.models import Invoice, InvoiceLine
from core.celery_app import celery_app
from core.config import get_settings
from core.db import run_task
from core.documents import store_document
from core.models import Business
from customers.models import Customer
from forms.render import html_to_pdf
from scheduling.models import Appointment
from settings.models import LOGO, BrandingAsset

REGULATED_HEALTH = "regulated_health"

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
<title>Invoice {{ invoice.invoice_number }}</title>
<style>"""
    + _STYLE
    + """</style></head><body>
<header>{% if logo_uri %}<img src="{{ logo_uri }}" alt="">{% endif %}
<h2>{{ business.name }}</h2>
<p class="muted">{{ address_lines|join(', ') }}<br>
{% if business.gst_hst_number %}GST/HST {{ business.gst_hst_number }} {% endif %}
{% if business.pst_qst_number %}PST/QST {{ business.pst_qst_number }}{% endif %}</p>
</header>
<h1>Invoice #{{ invoice.invoice_number }}</h1>
<p>Billed to: {{ customer.first_name }} {{ customer.last_name }}<br>
Issued: {{ issued_local }} ({{ business.timezone }})
&middot; Status: {{ invoice.status }}</p>
<table><thead><tr><th>Service</th><th>Provider</th><th class="right">Price</th>
<th class="right">Discount</th><th class="right">Tax</th><th class="right">Total</th></tr></thead>
<tbody>{% for row in rows %}
<tr><td>{{ row.service_name }}</td><td>{{ row.staff_name }}</td>
<td class="right">{{ money(row.price_cents) }}</td>
<td class="right">{{ money(row.discounted_cents) }}</td>
<td class="right">{{ money(row.tax_cents) }}</td>
<td class="right">{{ money(row.line_total_cents) }}</td></tr>
{% endfor %}</tbody></table>
<table class="totals">
<tr><td>Subtotal</td><td class="right">{{ money(invoice.computed_subtotal_cents) }}</td></tr>
<tr><td>Discounts</td>
<td class="right">-{{ money(invoice.computed_discount_total_cents) }}</td></tr>
<tr><td>Tax</td><td class="right">{{ money(invoice.computed_tax_total_cents) }}</td></tr>
{% if invoice.override_applied_cents is not none %}
<tr><td>Admin/owner-authorized total ({{ invoice.override_reason }})</td>
<td class="right">{{ money(invoice.override_applied_cents) }}</td></tr>
{% endif %}
<tr class="grand"><td>Total</td>
<td class="right">{{ money(invoice.grand_total_cents) }}</td></tr>
</table>
<footer>{% if business.receipt_footer %}<div>{{ business.receipt_footer }}</div>{% endif %}
<div>Invoice #{{ invoice.invoice_number }} &middot; {{ business.name }}</div></footer>
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
<tr><td>{{ tax.code }}</td><td class="right">{{ "%.2f"|format(tax.rate_pct) }}%</td>
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
    """Precomputed in Python rather than a longer `{% if %}` chain in the template — the same
    non-null parts `Business.address_line1`/`address_line2`/`city`/`province`/`postal_code`
    join into one line, skipping whatever a business hasn't filled in."""
    city_line = " ".join(
        part for part in (business.city, business.province, business.postal_code) if part
    )
    return [part for part in (business.address_line1, business.address_line2, city_line) if part]


def _plus_years(when: datetime, years: int) -> datetime:
    """A financial document's CRA retention hold, six years out from `issued_at`. 29 Feb has
    no anniversary in a non-leap target year; the later reading is safer (the same tie-break
    `customers/retention.py::_years_after` uses for its own purpose), so 1 Mar, never 28 Feb."""
    try:
        return when.replace(year=when.year + years)
    except ValueError:
        return when.replace(month=3, day=1, year=when.year + years)


def _money(cents: int, symbol: str) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{symbol}{abs(cents) / 100:.2f}"


def show_clinical_fields(business: Business) -> bool:
    """#70 acceptance criterion: a practitioner's designation/licence snapshot belongs only on
    a `regulated_health` business's receipts — a `general_business` tenant (a salon, a retail
    shop) is never forced to carry a field that means nothing to it. The same `retention_
    profile` column CLAUDE.md's retention rules already branch on (`customers/retention.py`)."""
    return business.retention_profile == REGULATED_HEALTH


def payment_status_label(invoice: Invoice) -> str:  # noqa: ARG001 — kept for the swap #66 makes
    """The "checkout complete" approximation, isolated to this one function (module docstring).
    Always "Paid": every receipt this ticket can produce is a plain, fully-issued service sale
    — #71/#72 package credits and #66 insurer billing are unbuilt this milestone, so "prepaid"
    and "pending" never arise yet. Once #66 exists, read `billing.is_checkout_complete(invoice)`
    (or its per-line equivalent) here instead, and never show a still-pending insurer amount
    as paid."""
    return "Paid"


def receipt_number(invoice: Invoice, ordinal: int) -> str:
    """A treatment receipt's document identity — the ticket's own allowance is "a simple unique
    id or sequential-per-business number", not gapless/audited the way `Invoice.invoice_number`
    is. Derived from the invoice's own already-gapless number plus this line's 1-based position
    on it, so no second counter table is needed: "1-1", "1-2" for the first invoice's first and
    second lines."""
    return f"{invoice.invoice_number}-{ordinal}"


def render_invoice_html(
    invoice: Invoice,
    business: Business,
    customer: Customer,
    appointments: dict[uuid.UUID, Appointment],
    *,
    logo_uri: str | None = None,
) -> str:
    zone = ZoneInfo(business.timezone)
    lines = sorted(invoice.lines, key=lambda line: line.created_at)
    money: Callable[[int], str] = lambda cents: _money(cents, business.currency_symbol)  # noqa: E731
    rows = [
        {
            "service_name": appointments[line.id].service.name if line.id in appointments else "",
            "staff_name": (
                appointments[line.id].staff.display_name if line.id in appointments else ""
            ),
            "price_cents": line.price_cents,
            "discounted_cents": line.discounted_cents,
            "tax_cents": line.tax_cents,
            "line_total_cents": line.line_total_cents,
        }
        for line in lines
    ]
    return _INVOICE_TEMPLATE.render(
        business=business,
        invoice=invoice,
        customer=customer,
        rows=rows,
        logo_uri=logo_uri,
        address_lines=_address_lines(business),
        issued_local=invoice.issued_at.astimezone(zone).strftime("%Y-%m-%d %H:%M"),
        money=money,
    )


def render_receipt_html(
    invoice: Invoice,
    line: InvoiceLine,
    appointment: Appointment,
    business: Business,
    ordinal: int,
    *,
    logo_uri: str | None = None,
) -> str:
    zone = ZoneInfo(business.timezone)
    money: Callable[[int], str] = lambda cents: _money(cents, business.currency_symbol)  # noqa: E731
    taxes = [
        {"code": t.component_code, "rate_pct": t.rate_bp / 100, "amount_cents": t.amount_cents}
        for t in line.taxes
    ]
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
        payment_status=payment_status_label(invoice),
        show_clinical=show_clinical_fields(business),
        taxes=taxes,
        money=money,
    )


@celery_app.task(
    name="billing.documents.render_invoice_documents",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def render_invoice_documents(invoice_id: str) -> None:
    """Queued once, right after `issue_invoice`'s own commit (module docstring). `run_task`
    (`core/db.py`) runs this under the worker's own event loop, or off a side thread when eager
    Celery (the test suite) calls it from inside a request handler's already-running loop —
    the same helper `notifications/tasks.py::_load_business` uses for the identical reason."""
    run_task(_render_invoice_documents, uuid.UUID(invoice_id))


async def _render_invoice_documents(invoice_id: uuid.UUID) -> None:
    # A fresh engine for this task's event loop, application role only — the same shape
    # `forms/tasks.py::_render_submission` already uses.
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db, db.begin():
            invoice = await db.get(Invoice, invoice_id)
            if invoice is None:
                return
            business = await db.get(Business, 1)
            customer = await db.get(Customer, invoice.customer_id)
            logo = await db.get(BrandingAsset, LOGO)
            logo_uri = (
                "data:image/png;base64," + base64.b64encode(logo.data).decode() if logo else None
            )
            key = await business_key(db)
            retain_until = _plus_years(invoice.issued_at, 6)

            lines = sorted(invoice.lines, key=lambda line: line.created_at)
            appointments: dict[uuid.UUID, Appointment] = {}
            for line in lines:
                appointment = await db.get(Appointment, line.appointment_id)
                if appointment is not None:
                    appointments[line.id] = appointment

            invoice_html = render_invoice_html(
                invoice, business, customer, appointments, logo_uri=logo_uri
            )
            await store_document(
                db,
                key=key,
                key_owner="business",
                linked_customer_id=invoice.customer_id,
                retain_until=retain_until,
                kind="invoice",
                source_id=invoice.id,
                content=html_to_pdf(invoice_html),
                content_type="application/pdf",
            )

            for ordinal, line in enumerate(lines, start=1):
                appointment = appointments.get(line.id)
                if appointment is None:
                    continue
                receipt_html = render_receipt_html(
                    invoice, line, appointment, business, ordinal, logo_uri=logo_uri
                )
                await store_document(
                    db,
                    key=key,
                    key_owner="business",
                    linked_customer_id=invoice.customer_id,
                    retain_until=retain_until,
                    kind="treatment_receipt",
                    source_id=line.id,
                    content=html_to_pdf(receipt_html),
                    content_type="application/pdf",
                )
    finally:
        await engine.dispose()

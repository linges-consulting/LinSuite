"""Invoice issue (#65): turning a reviewed, approved draft `ServiceBill` into an issued,
immutable `Invoice`. See `billing/models.py`'s `## invoice issue (#65)` section for the full
schema, snapshot contract and refusal-condition rationale — this module is the one writer of
`invoices`/`invoice_lines`/`invoice_line_discounts`/`invoice_line_taxes`, and (bar direct SQL,
S5-tested separately) the only way any of those four tables is ever touched.

**Reuses `bill_review.py`'s own `_compute`, never reimplements the math** — the same rule that
module's own docstring states for itself, extended one level further: issue is one more reader
of the same pure functions (`discount_resolver.resolve_stacked_discounts`, `billing/tax.py`'s
three functions), called one final time, with the result frozen rather than merely rendered.

**Capability: `billing.view`, reused.** Issuing is checkout, not an admin action — the same
front-desk-reachable call #63 already made for the rest of this screen. Reading an issued
invoice is gated the same way. See the module section in `billing/models.py` for the full
reasoning.
"""

import base64
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import select

from auth.capabilities import Requires
from billing.bill_review import (
    BillViewer,
    _applicable_components,
    _business,
    _compute,
    _LineConflict,
    _load_bill,
    _persisted_selection,
    _today_in,
)
from billing.documents import render_invoice_documents
from billing.invoice_numbering import allocate_invoice_number
from billing.keys import business_key
from billing.models import (
    BillOverrideRequest,
    Discount,
    Invoice,
    InvoiceLine,
    InvoiceLineDiscount,
    InvoiceLineTax,
)
from core.access_log import LogAccess
from core.audit import record_event
from core.db import SessionDep
from core.documents import fetch_document
from core.models import Document
from customers.models import Customer
from notifications.providers import email_ready
from notifications.tasks import send_email as send_email_task

router = APIRouter(tags=["billing"])

# Reading an issued invoice or a treatment receipt (print/download, or opening it to email) is
# gated the same way issuing it was (`billing/invoices.py`'s own module docstring): `billing.
# view`, front-desk-reachable, no admin escalation. `LogAccess` is added per route below, not
# here, because it needs that route's own path parameters.
_ViewInvoiceDocs = Depends(Requires("billing.view"))


# --- what goes over the wire -----------------------------------------------------------------


class InvoiceLineDiscountOut(BaseModel):
    discount_id: str
    discount_name: str
    discount_kind: str
    commission_basis: str


class InvoiceLineTaxOut(BaseModel):
    component_code: str
    rate_bp: int
    amount_cents: int


class InvoiceLineOut(BaseModel):
    id: str
    appointment_id: str
    service_id: str
    staff_id: str
    price_cents: int
    commission_rate_bp: int
    discounted_cents: int
    pretax_cents: int
    tax_cents: int
    line_total_cents: int
    discounts: list[InvoiceLineDiscountOut]
    taxes: list[InvoiceLineTaxOut]


class InvoiceOut(BaseModel):
    id: str
    business_id: int
    invoice_number: int
    service_bill_id: str
    customer_id: str
    status: str
    computed_subtotal_cents: int
    computed_discount_total_cents: int
    computed_tax_total_cents: int
    computed_grand_total_cents: int
    tax_totals_by_component: dict[str, int]
    override_applied_cents: int | None
    override_reason: str | None
    grand_total_cents: int
    issued_at: datetime
    issued_by: str
    lines: list[InvoiceLineOut]


class InvoiceSummaryOut(BaseModel):
    id: str
    invoice_number: int
    customer_id: str
    status: str
    grand_total_cents: int
    issued_at: datetime


def _line_out(line: InvoiceLine) -> InvoiceLineOut:
    return InvoiceLineOut(
        id=str(line.id),
        appointment_id=str(line.appointment_id),
        service_id=str(line.service_id),
        staff_id=str(line.staff_id),
        price_cents=line.price_cents,
        commission_rate_bp=line.commission_rate_bp,
        discounted_cents=line.discounted_cents,
        pretax_cents=line.pretax_cents,
        tax_cents=line.tax_cents,
        line_total_cents=line.line_total_cents,
        discounts=[
            InvoiceLineDiscountOut(
                discount_id=str(d.discount_id),
                discount_name=d.discount_name,
                discount_kind=d.discount_kind,
                commission_basis=d.commission_basis,
            )
            for d in line.discounts
        ],
        taxes=[
            InvoiceLineTaxOut(
                component_code=t.component_code, rate_bp=t.rate_bp, amount_cents=t.amount_cents
            )
            for t in line.taxes
        ],
    )


def _invoice_out(invoice: Invoice) -> InvoiceOut:
    return InvoiceOut(
        id=str(invoice.id),
        business_id=invoice.business_id,
        invoice_number=invoice.invoice_number,
        service_bill_id=str(invoice.service_bill_id),
        customer_id=str(invoice.customer_id),
        status=invoice.status,
        computed_subtotal_cents=invoice.computed_subtotal_cents,
        computed_discount_total_cents=invoice.computed_discount_total_cents,
        computed_tax_total_cents=invoice.computed_tax_total_cents,
        computed_grand_total_cents=invoice.computed_grand_total_cents,
        tax_totals_by_component=invoice.tax_totals_by_component,
        override_applied_cents=invoice.override_applied_cents,
        override_reason=invoice.override_reason,
        grand_total_cents=invoice.grand_total_cents,
        issued_at=invoice.issued_at,
        issued_by=str(invoice.issued_by),
        lines=[_line_out(line) for line in invoice.lines],
    )


async def _load_invoice(db: SessionDep, invoice_id: uuid.UUID) -> Invoice:
    invoice = await db.get(Invoice, invoice_id, populate_existing=True)
    if invoice is None:
        raise HTTPException(status_code=404, detail="No such invoice.")
    return invoice


# --- issue --------------------------------------------------------------------------------


@router.post("/bills/{bill_id}/issue", status_code=201)
async def issue_invoice(bill_id: uuid.UUID, actor: BillViewer, db: SessionDep) -> InvoiceOut:
    business = await _business(db)
    bill = await _load_bill(db, bill_id)

    if bill.status != "draft":
        raise HTTPException(status_code=422, detail="This bill has already been issued.")
    if not bill.lines:
        raise HTTPException(status_code=422, detail="A bill with no lines cannot be issued.")

    # Refusal 1: a pending (undecided) override request.
    pending = await db.scalar(
        select(BillOverrideRequest.id).where(
            BillOverrideRequest.bill_id == bill_id, BillOverrideRequest.status == "pending"
        )
    )
    if pending is not None:
        raise HTTPException(
            status_code=409,
            detail="This bill has a pending override request awaiting an admin/owner decision.",
        )

    # Refusal 2: an unresolved or stale manual override — see `billing/models.py`'s `## invoice
    # issue (#65)` section for exactly what "stale" and "unresolved" mean here.
    if bill.manual_override_cents is not None and (
        bill.override_applied_revision is None or bill.override_applied_revision != bill.updated_at
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "This bill's admin/owner-authorized override is no longer current — the bill "
                "has changed since it was authorized. Ask an admin/owner to re-approve it "
                "(or apply a fresh inline edit) before issuing."
            ),
        )

    selected_ids = await _persisted_selection(db, bill_id)
    try:
        computed = await _compute(db, business, bill, selected_ids=selected_ids)
    except _LineConflict as error:
        raise HTTPException(status_code=409, detail=error.detail) from error

    today = _today_in(business)
    resolved_components = await _applicable_components(db, business, today)
    components_by_code = {c.code: c.rate_bp for c in resolved_components}

    override_applied_cents = computed.override_total_cents
    override_reason = computed.override_reason
    grand_total_cents = (
        override_applied_cents if override_applied_cents is not None else computed.grand_total_cents
    )

    invoice_number = await allocate_invoice_number(db, business_id=business.id)

    invoice = Invoice(
        business_id=business.id,
        invoice_number=invoice_number,
        service_bill_id=bill.id,
        customer_id=bill.customer_id,
        computed_subtotal_cents=computed.subtotal_cents,
        computed_discount_total_cents=computed.discount_total_cents,
        computed_tax_total_cents=computed.tax_total_cents,
        computed_grand_total_cents=computed.grand_total_cents,
        tax_totals_by_component=computed.tax_totals_by_component,
        override_applied_cents=override_applied_cents,
        override_reason=override_reason,
        grand_total_cents=grand_total_cents,
        issued_by=actor.id,
    )
    db.add(invoice)
    await db.flush()

    # Discount definitions frozen at this exact moment — one more query, never a live re-join
    # once this transaction commits (module section in `billing/models.py`).
    all_discount_ids = {
        uuid.UUID(discount_id)
        for line_out in computed.lines
        for discount_id in line_out.applied_discount_ids
    }
    discounts_by_id = {}
    if all_discount_ids:
        discounts_by_id = {
            d.id: d
            for d in await db.scalars(select(Discount).where(Discount.id.in_(all_discount_ids)))
        }

    for bill_line, line_out in zip(bill.lines, computed.lines, strict=True):
        invoice_line = InvoiceLine(
            invoice_id=invoice.id,
            service_bill_line_id=bill_line.id,
            appointment_id=bill_line.appointment_id,
            service_id=bill_line.service_id,
            staff_id=bill_line.staff_id,
            price_cents=bill_line.price_cents,
            commission_rate_bp=bill_line.commission_rate_bp,
            discounted_cents=line_out.discounted_cents,
            pretax_cents=line_out.tax.pretax_cents,
            tax_cents=line_out.tax.tax_cents,
            line_total_cents=line_out.line_total_cents,
        )
        db.add(invoice_line)
        await db.flush()

        for discount_id_str in line_out.applied_discount_ids:
            discount = discounts_by_id.get(uuid.UUID(discount_id_str))
            if discount is None:  # pragma: no cover — defensive; _compute already validated this
                continue
            db.add(
                InvoiceLineDiscount(
                    invoice_line_id=invoice_line.id,
                    discount_id=discount.id,
                    discount_name=discount.name,
                    discount_kind=discount.kind,
                    commission_basis=discount.commission_basis,
                )
            )

        for code, amount_cents in line_out.tax.component_cents.items():
            db.add(
                InvoiceLineTax(
                    invoice_line_id=invoice_line.id,
                    component_code=code,
                    rate_bp=components_by_code.get(code, 0),
                    amount_cents=amount_cents,
                )
            )

    bill.status = "issued"
    await db.flush()

    record_event(
        db,
        "invoice.issued",
        target_type="invoice",
        target_id=str(invoice.id),
        actor_user_id=actor.id,
        metadata={
            "service_bill_id": str(bill_id),
            "invoice_number": invoice_number,
            "grand_total_cents": grand_total_cents,
        },
    )
    await db.commit()

    # #70: render the invoice PDF and one treatment receipt per line, in a separate Celery
    # transaction, queued only *after* this one has landed — the "commit, then queue" order
    # `scheduling/public.py::book_public` already uses for `notify_booking_confirmed`, so a
    # rendering failure can never unwind the checkout/commission effects that just committed.
    render_invoice_documents.delay(str(invoice.id))

    invoice = await db.get(Invoice, invoice.id, populate_existing=True)
    assert invoice is not None
    return _invoice_out(invoice)


# --- reading --------------------------------------------------------------------------------


@router.get("/invoices")
async def list_invoices(_: BillViewer, db: SessionDep) -> dict[str, list[InvoiceSummaryOut]]:
    invoices = await db.scalars(select(Invoice).order_by(Invoice.invoice_number))
    return {
        "invoices": [
            InvoiceSummaryOut(
                id=str(i.id),
                invoice_number=i.invoice_number,
                customer_id=str(i.customer_id),
                status=i.status,
                grand_total_cents=i.grand_total_cents,
                issued_at=i.issued_at,
            )
            for i in invoices
        ]
    }


@router.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: uuid.UUID, _: BillViewer, db: SessionDep) -> InvoiceOut:
    invoice = await _load_invoice(db, invoice_id)
    return _invoice_out(invoice)


# --- PDF print/download and email (#70) ------------------------------------------------------
#
# Mounted under `/customers/{customer_id}/...` — the same shape `forms/submissions.py::
# view_pdf` already established for a client-linked document's audited read — so `LogAccess`
# (`core.access_log`) can be reused exactly, unmodified, rather than this ticket inventing a
# second audited-read mechanism for a route that has no customer id in its path otherwise.
# `billing.view` gates every route below, the same capability issuing and reading an invoice
# already use: printing/downloading needs no verified email sender (it never reaches
# `NotificationProvider`); emailing does, checked explicitly with `email_ready` before anything
# is sent — an unconfigured business gets a 422, never a silent no-op the way a background
# trigger's `dispatch()` degrades.


async def _load_invoice_for_customer(
    db: SessionDep, customer_id: uuid.UUID, invoice_id: uuid.UUID
) -> Invoice:
    invoice = await db.scalar(
        select(Invoice).where(Invoice.id == invoice_id, Invoice.customer_id == customer_id)
    )
    if invoice is None:
        raise HTTPException(status_code=404, detail="No such invoice.")
    return invoice


async def _load_line_for_customer(
    db: SessionDep, customer_id: uuid.UUID, invoice_id: uuid.UUID, line_id: uuid.UUID
) -> tuple[Invoice, InvoiceLine]:
    invoice = await _load_invoice_for_customer(db, customer_id, invoice_id)
    line = await db.scalar(
        select(InvoiceLine).where(InvoiceLine.id == line_id, InvoiceLine.invoice_id == invoice_id)
    )
    if line is None:
        raise HTTPException(status_code=404, detail="No such treatment receipt.")
    return invoice, line


async def _fetch_stored(db: SessionDep, *, kind: str, source_id: uuid.UUID) -> bytes | None:
    """`None` when the Celery render hasn't landed yet — the same "still rendering" window
    `forms/submissions.py::view_pdf` already reports as a 202, never a synchronous fallback
    render on the request path (CLAUDE.md: PDF generation is a worker job)."""
    document_id = await db.scalar(
        select(Document.id).where(Document.kind == kind, Document.source_id == source_id)
    )
    if document_id is None:
        return None
    key = await business_key(db)
    content, _ = await fetch_document(db, key=key, key_owner="business", document_id=document_id)
    return content


_PENDING = JSONResponse(
    {"status": "rendering"}, status_code=202, headers={"Cache-Control": "no-store"}
)


@router.get(
    "/customers/{customer_id}/invoices/{invoice_id}/pdf",
    dependencies=[
        _ViewInvoiceDocs,
        Depends(LogAccess("invoice_document", resource_param="invoice_id")),
    ],
)
async def invoice_pdf(customer_id: uuid.UUID, invoice_id: uuid.UUID, db: SessionDep) -> Response:
    invoice = await _load_invoice_for_customer(db, customer_id, invoice_id)
    content = await _fetch_stored(db, kind="invoice", source_id=invoice_id)
    if content is None:
        return _PENDING
    return Response(
        content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"inline; filename=invoice-{invoice.invoice_number}.pdf",
            "Cache-Control": "no-store",
        },
    )


@router.get(
    "/customers/{customer_id}/invoices/{invoice_id}/receipts/{line_id}/pdf",
    dependencies=[
        _ViewInvoiceDocs,
        Depends(LogAccess("treatment_receipt", resource_param="line_id")),
    ],
)
async def treatment_receipt_pdf(
    customer_id: uuid.UUID, invoice_id: uuid.UUID, line_id: uuid.UUID, db: SessionDep
) -> Response:
    invoice, _line = await _load_line_for_customer(db, customer_id, invoice_id, line_id)
    content = await _fetch_stored(db, kind="treatment_receipt", source_id=line_id)
    if content is None:
        return _PENDING
    return Response(
        content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"inline; filename=receipt-{invoice.invoice_number}.pdf",
            "Cache-Control": "no-store",
        },
    )


class EmailedOut(BaseModel):
    status: str
    to: str


async def _customer_email(db: SessionDep, customer_id: uuid.UUID) -> str:
    customer = await db.get(Customer, customer_id)
    if customer is None or not customer.email:
        raise HTTPException(status_code=422, detail="This client has no email address on file.")
    return customer.email


@router.post(
    "/customers/{customer_id}/invoices/{invoice_id}/email",
    dependencies=[
        _ViewInvoiceDocs,
        Depends(LogAccess("invoice_document", resource_param="invoice_id")),
    ],
)
async def email_invoice(
    customer_id: uuid.UUID, invoice_id: uuid.UUID, actor: BillViewer, db: SessionDep
) -> EmailedOut:
    business = await _business(db)
    if not email_ready(business):
        raise HTTPException(
            status_code=422, detail="Email is not configured and verified for this business."
        )
    invoice = await _load_invoice_for_customer(db, customer_id, invoice_id)
    to = await _customer_email(db, customer_id)
    content = await _fetch_stored(db, kind="invoice", source_id=invoice_id)
    if content is None:
        return _PENDING  # type: ignore[return-value]

    # An explicit staff action, audited like any other (`core/audit.py`) — distinct from the
    # `LogAccess` read above, which only records that the document was opened. Nothing here
    # touches `invoices`/`service_bills`/commission/stock, so a resend is trivially idempotent:
    # re-running this route re-sends the same already-rendered bytes and writes one more audit
    # row, never a second financial effect.
    record_event(
        db,
        "invoice.emailed",
        target_type="invoice",
        target_id=str(invoice_id),
        actor_user_id=actor.id,
        metadata={"to": to},
    )
    await db.commit()

    send_email_task.delay(
        to,
        f"Invoice #{invoice.invoice_number} — {business.name}",
        f"Your invoice #{invoice.invoice_number} from {business.name} is attached.",
        attachments=[
            {
                "filename": f"invoice-{invoice.invoice_number}.pdf",
                "content_b64": base64.b64encode(content).decode(),
                "content_type": "application/pdf",
            }
        ],
        customer_id=str(customer_id),
        notification_type="invoice_document",
    )
    return EmailedOut(status="queued", to=to)


@router.post(
    "/customers/{customer_id}/invoices/{invoice_id}/receipts/{line_id}/email",
    dependencies=[
        _ViewInvoiceDocs,
        Depends(LogAccess("treatment_receipt", resource_param="line_id")),
    ],
)
async def email_treatment_receipt(
    customer_id: uuid.UUID,
    invoice_id: uuid.UUID,
    line_id: uuid.UUID,
    actor: BillViewer,
    db: SessionDep,
) -> EmailedOut:
    business = await _business(db)
    if not email_ready(business):
        raise HTTPException(
            status_code=422, detail="Email is not configured and verified for this business."
        )
    invoice, _line = await _load_line_for_customer(db, customer_id, invoice_id, line_id)
    to = await _customer_email(db, customer_id)
    content = await _fetch_stored(db, kind="treatment_receipt", source_id=line_id)
    if content is None:
        return _PENDING  # type: ignore[return-value]

    record_event(
        db,
        "treatment_receipt.emailed",
        target_type="invoice_line",
        target_id=str(line_id),
        actor_user_id=actor.id,
        metadata={"to": to, "invoice_id": str(invoice_id)},
    )
    await db.commit()

    send_email_task.delay(
        to,
        f"Your receipt — {business.name}",
        f"Your treatment receipt from {business.name} is attached.",
        attachments=[
            {
                "filename": f"receipt-{invoice.invoice_number}.pdf",
                "content_b64": base64.b64encode(content).decode(),
                "content_type": "application/pdf",
            }
        ],
        customer_id=str(customer_id),
        notification_type="treatment_receipt",
    )
    return EmailedOut(status="queued", to=to)

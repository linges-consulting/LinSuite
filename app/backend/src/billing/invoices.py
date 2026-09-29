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

**Commission is posted here too (#69), one more thing in the same transaction before commit** —
the exact shape `billing/completion.py::record_draft_bill_line`/`scheduling/appointments.py
::_clear_queue_entry` already established. `InvoiceLineOut`/`InvoiceLineDiscountOut` deliberately
carry no `commission_rate_bp`/`commission_basis` — this route is reachable in Staff Mode with no
admin window (`billing.view`), and commission data is never staff-facing (CLAUDE.md; #69's own
acceptance criterion, checked directly in `tests/test_commission_leakage.py`). Read it from
`GET /admin/reports/commission` instead (`billing/commission_report.py`, `commission.view`,
Administrator-only, Admin Mode).
"""

import base64
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from auth.capabilities import Requires
from billing.bill_review import (
    BillViewer,
    _business,
    _compute,
    _LineConflict,
    _load_bill,
    _persisted_selection,
    price_bill,
)
from billing.commission import commission_amount_cents
from billing.documents import render_invoice_documents
from billing.invoice_numbering import allocate_invoice_number
from billing.keys import business_key
from billing.models import (
    BillOverrideRequest,
    CommissionPosting,
    Invoice,
    InvoiceLine,
    InvoiceLineDiscount,
    InvoiceLineTax,
    ServiceBill,
)
from billing.payments import balance, balances, carry_payments
from billing.tax import invoice_tax_totals
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
    # Deliberately no `commission_basis` — this route is Staff-Mode-reachable (`billing.view`,
    # no admin window) and commission data is never staff-facing (CLAUDE.md; #69). Read the
    # commission-basis choice via `GET /admin/reports/commission` (`commission.view`) instead.
    discount_id: str
    discount_name: str
    discount_kind: str
    # Review R4: the rule and resolved cents, frozen at issue (NULL on pre-0065 invoices).
    percentage_bp: int | None
    amount_cents: int | None
    resolved_amount_cents: int | None


class InvoiceLineTaxOut(BaseModel):
    component_code: str
    rate_bp: int
    amount_cents: int


class InvoiceLineOut(BaseModel):
    # Deliberately no `commission_rate_bp` — see `InvoiceLineDiscountOut`'s own note above.
    id: str
    appointment_id: str
    service_id: str
    staff_id: str
    price_cents: int
    discounted_cents: int
    pretax_cents: int
    tax_cents: int
    line_total_cents: int
    prepaid_cents: int
    tax_convention: str
    override_adjustment_cents: int
    discounts: list[InvoiceLineDiscountOut]
    taxes: list[InvoiceLineTaxOut]


class InvoiceOut(BaseModel):
    id: str
    business_id: int
    invoice_number: int
    # Exactly one of these two is set (#71, `ck_invoices_source_xor`) — a service invoice has
    # `service_bill_id` and empty `lines`; a package-purchase invoice has `package_purchase_id`
    # and empty `lines` (its own frozen shape is read via `GET /packages/purchases/{id}`,
    # `billing/package_purchase.py` — kept off this endpoint so `InvoiceOut` does not have to
    # duplicate that module's response shape here).
    service_bill_id: str | None
    package_purchase_id: str | None
    customer_id: str
    status: str
    computed_subtotal_cents: int
    computed_discount_total_cents: int
    computed_tax_total_cents: int
    computed_grand_total_cents: int
    # The tax actually billed (== computed unless an override was distributed, review R5).
    tax_totals_by_component: dict[str, int]
    tax_rates_by_component: dict[str, int]
    # Package-purchase invoices only; service lines carry their own.
    tax_convention: str | None
    override_applied_cents: int | None
    override_reason: str | None
    override_tax_convention: str | None
    grand_total_cents: int
    issued_at: datetime
    issued_by: str
    # #68 lineage, queryable both ways: what this invoice replaced / what replaced it.
    replaces_invoice_id: str | None
    replaced_by_invoice_id: str | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    lines: list[InvoiceLineOut]
    # #66: derived every time from the payment ledger, never a stored flag — see
    # `billing/payments.py::Balance`.
    outstanding_cents: int
    pending_insurer_cents: int
    client_outstanding_cents: int
    checkout_complete: bool
    refunded_cents: int
    prepaid_cents: int
    # M4 review R12: money a cancelled invoice still holds (0 while issued) — see `Balance`.
    held_credit_cents: int


class InvoiceSummaryOut(BaseModel):
    id: str
    invoice_number: int
    customer_id: str
    status: str
    grand_total_cents: int
    issued_at: datetime
    outstanding_cents: int
    pending_insurer_cents: int
    client_outstanding_cents: int
    checkout_complete: bool
    refunded_cents: int
    prepaid_cents: int
    # M4 review R12: money a cancelled invoice still holds (0 while issued) — see `Balance`.
    held_credit_cents: int


def _line_out(line: InvoiceLine) -> InvoiceLineOut:
    return InvoiceLineOut(
        id=str(line.id),
        appointment_id=str(line.appointment_id),
        service_id=str(line.service_id),
        staff_id=str(line.staff_id),
        price_cents=line.price_cents,
        discounted_cents=line.discounted_cents,
        pretax_cents=line.pretax_cents,
        tax_cents=line.tax_cents,
        line_total_cents=line.line_total_cents,
        prepaid_cents=line.prepaid_cents,
        tax_convention=line.tax_convention,
        override_adjustment_cents=line.override_adjustment_cents,
        discounts=[
            InvoiceLineDiscountOut(
                discount_id=str(d.discount_id),
                discount_name=d.discount_name,
                discount_kind=d.discount_kind,
                percentage_bp=d.percentage_bp,
                amount_cents=d.amount_cents,
                resolved_amount_cents=d.resolved_amount_cents,
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


def _str_or_none(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


async def _invoice_out(db: SessionDep, invoice: Invoice) -> InvoiceOut:
    return InvoiceOut(
        id=str(invoice.id),
        business_id=invoice.business_id,
        invoice_number=invoice.invoice_number,
        service_bill_id=str(invoice.service_bill_id) if invoice.service_bill_id else None,
        package_purchase_id=(
            str(invoice.package_purchase_id) if invoice.package_purchase_id else None
        ),
        customer_id=str(invoice.customer_id),
        status=invoice.status,
        computed_subtotal_cents=invoice.computed_subtotal_cents,
        computed_discount_total_cents=invoice.computed_discount_total_cents,
        computed_tax_total_cents=invoice.computed_tax_total_cents,
        computed_grand_total_cents=invoice.computed_grand_total_cents,
        tax_totals_by_component=invoice.tax_totals_by_component,
        tax_rates_by_component=invoice.tax_rates_by_component,
        tax_convention=invoice.tax_convention,
        override_applied_cents=invoice.override_applied_cents,
        override_reason=invoice.override_reason,
        override_tax_convention=invoice.override_tax_convention,
        grand_total_cents=invoice.grand_total_cents,
        issued_at=invoice.issued_at,
        issued_by=str(invoice.issued_by),
        replaces_invoice_id=_str_or_none(invoice.replaces_invoice_id),
        replaced_by_invoice_id=_str_or_none(
            await db.scalar(select(Invoice.id).where(Invoice.replaces_invoice_id == invoice.id))
        ),
        cancelled_at=invoice.cancelled_at,
        cancel_reason=invoice.cancel_reason,
        lines=[_line_out(line) for line in invoice.lines],
        **asdict(await balance(db, invoice)),
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
    # R11: the bill's row lock serializes concurrent issues of one draft — the loser waits,
    # then reads `issued` below and gets the ordinary 422, never a unique-index 500.
    await db.execute(select(ServiceBill.id).where(ServiceBill.id == bill_id).with_for_update())
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
        priced_bill = await price_bill(db, business, bill, selected_ids=selected_ids)
    except _LineConflict as error:
        raise HTTPException(status_code=409, detail=error.detail) from error

    components_by_code = {c.code: c.rate_bp for c in priced_bill.pool}
    overridden = priced_bill.overridden
    # Review R5: under an override the lines carry the distributed amounts, so the invoice's
    # billed tax and total are those lines summed — reconciling to the cent.
    billed_tax_totals = invoice_tax_totals([p.billed for p in priced_bill.lines])
    grand_total_cents = sum(p.billed.total_cents for p in priced_bill.lines)

    # #68: a draft reopened by cancelling its invoice replaces that (not-yet-replaced) invoice.
    successor = aliased(Invoice)
    replaces = await db.scalar(
        select(Invoice).where(
            Invoice.service_bill_id == bill.id,
            Invoice.status == "cancelled",
            ~exists().where(successor.replaces_invoice_id == Invoice.id),
        )
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
        tax_totals_by_component=billed_tax_totals,
        tax_rates_by_component=components_by_code,
        override_applied_cents=computed.override_total_cents,
        override_reason=computed.override_reason,
        override_tax_convention=computed.override_tax_convention,
        override_commission_basis=bill.override_commission_basis if overridden else None,
        grand_total_cents=grand_total_cents,
        issued_by=actor.id,
        replaces_invoice_id=replaces.id if replaces is not None else None,
    )
    db.add(invoice)
    await db.flush()

    if replaces is not None:
        # Under the lineage lock (R9) — see `carry_payments`.
        await carry_payments(db, replaces, invoice, actor.id)

    for p in priced_bill.lines:
        bill_line, priced, billed = p.bill_line, p.priced, p.billed
        # The billed amount in the line's own convention: pre-tax for an exclusive line, the
        # tax-included total for an inclusive one (== priced.discounted_cents, no override).
        discounted = billed.pretax_cents if priced.convention == "exclusive" else billed.total_cents
        if not overridden:
            discounted = priced.discounted_cents
        invoice_line = InvoiceLine(
            invoice_id=invoice.id,
            service_bill_line_id=bill_line.id,
            appointment_id=bill_line.appointment_id,
            service_id=bill_line.service_id,
            staff_id=bill_line.staff_id,
            price_cents=bill_line.price_cents,
            commission_rate_bp=bill_line.commission_rate_bp,
            discounted_cents=discounted,
            pretax_cents=billed.pretax_cents,
            tax_cents=billed.tax_cents,
            line_total_cents=billed.total_cents,
            prepaid_cents=bill_line.prepaid_cents,
            tax_convention=priced.convention,
            override_adjustment_cents=discounted - priced.discounted_cents,
        )
        db.add(invoice_line)
        await db.flush()

        # The rule and the cents it took off, frozen at this exact moment (review R4) — never
        # a live re-join once this transaction commits.
        for discount in p.discounts:
            db.add(
                InvoiceLineDiscount(
                    invoice_line_id=invoice_line.id,
                    discount_id=discount.id,
                    discount_name=discount.name,
                    discount_kind=discount.kind,
                    commission_basis=discount.commission_basis,
                    percentage_bp=discount.percentage_bp,
                    amount_cents=discount.amount_cents,
                    stackable=discount.stackable,
                    resolved_amount_cents=priced.discount_amounts[discount.id],
                )
            )

        # #69: commission, posted in this same transaction — the rate is always
        # `bill_line.commission_rate_bp` (snapshotted at completion, #59, never re-read live).
        # The basis is pre-tax (`pricing.price_line`: "absorbed" discounts left out); under an
        # override whose commission basis is "reduces" (the spec §142 default) it is the
        # revised line's own pre-tax amount instead. `staff_id` is always the delivering staff
        # member, never inferred from a package sale (#72's own constraint).
        basis_cents = priced.commission_basis_cents
        if overridden and bill.override_commission_basis == "reduces":
            basis_cents = billed.pretax_cents
        db.add(
            CommissionPosting(
                invoice_line_id=invoice_line.id,
                invoice_id=invoice.id,
                staff_id=bill_line.staff_id,
                commission_rate_bp=bill_line.commission_rate_bp,
                basis_cents=basis_cents,
                amount_cents=commission_amount_cents(basis_cents, bill_line.commission_rate_bp),
            )
        )

        for code, amount_cents in billed.component_cents.items():
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
            "replaces_invoice_id": str(replaces.id) if replaces is not None else None,
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
    return await _invoice_out(db, invoice)


# --- cancel & replace (#68) -------------------------------------------------------------------


class CancelInvoiceIn(BaseModel):
    reason: Annotated[str, Field(min_length=1, max_length=2000)]


class CancelledOut(BaseModel):
    invoice: InvoiceOut
    # The replacement draft: the original's own bill, reopened with its lines, discount
    # selection and (still-valid) override intact. Edit it on the bill review screen, then
    # `POST /bills/{id}/issue` issues the replacement.
    replacement_bill_id: str


def cancel_issued(db: AsyncSession, invoice: Invoice, actor_id: uuid.UUID, reason: str) -> datetime:
    """The one issued -> cancelled write `invoices_voidable_guard` permits, plus its audit
    event. The caller holds the invoice's row lock, checked `status == "issued"`, and commits.
    Shared by the #68 cancel route and #73's package refund."""
    now = datetime.now(UTC)
    invoice.status = "cancelled"
    invoice.cancelled_at = now
    invoice.cancelled_by = actor_id
    invoice.cancel_reason = reason
    record_event(
        db,
        "invoice.cancelled",
        target_type="invoice",
        target_id=str(invoice.id),
        actor_user_id=actor_id,
        metadata={
            "invoice_number": invoice.invoice_number,
            "reason": reason,
            "service_bill_id": str(invoice.service_bill_id) if invoice.service_bill_id else None,
            "package_purchase_id": (
                str(invoice.package_purchase_id) if invoice.package_purchase_id else None
            ),
        },
    )
    return now


async def reverse_commission(db: AsyncSession, *where: ColumnElement[bool]) -> int:
    """Post a `reversal` for every matching `earned` posting not already reversed (a posting is
    reversed at most once, whichever of #68's cancel or #73's refund gets there first). Dated
    today, never backdated (#69). Returns how many were reversed; does not commit."""
    reversal = aliased(CommissionPosting)
    earned = list(
        await db.scalars(
            select(CommissionPosting).where(
                CommissionPosting.kind == "earned",
                ~exists().where(reversal.reverses_posting_id == CommissionPosting.id),
                *where,
            )
        )
    )
    for posting in earned:
        db.add(
            CommissionPosting(
                invoice_line_id=posting.invoice_line_id,
                invoice_id=posting.invoice_id,
                staff_id=posting.staff_id,
                kind="reversal",
                commission_rate_bp=posting.commission_rate_bp,
                basis_cents=posting.basis_cents,
                amount_cents=-posting.amount_cents,
                reverses_posting_id=posting.id,
            )
        )
    return len(earned)


@router.post("/invoices/{invoice_id}/cancel")
async def cancel_invoice(
    invoice_id: uuid.UUID, payload: CancelInvoiceIn, actor: BillViewer, db: SessionDep
) -> CancelledOut:
    """Voidable cancel (CLAUDE.md): the original keeps its number and every frozen row; only
    the issued -> cancelled transition `invoices_voidable_guard` permits is written. The row
    lock makes a retry wait for, then observe, the first call — which it returns unchanged, so
    one lineage and one set of effects either way."""
    invoice = await db.scalar(
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if invoice is None:
        raise HTTPException(status_code=404, detail="No such invoice.")
    if invoice.service_bill_id is None:
        raise HTTPException(
            status_code=422, detail="Only a service invoice can be cancelled and replaced."
        )

    if invoice.status == "issued":
        now = cancel_issued(db, invoice, actor.id, payload.reason)
        # Commission: reverse what the original earned, dated today (#69's own shape); the
        # replacement posts its own at issue, so the two are never counted together.
        await reverse_commission(db, CommissionPosting.invoice_id == invoice.id)

        # Reopen the bill as the replacement draft. Completion is not re-run, so no stock or
        # package credit moves. An override on an issued bill was valid at issue (issue refuses
        # a stale one) and nothing edits an issued bill, so it stays authorized for the same
        # content; any later edit to the draft invalidates it the usual way.
        bill = await _load_bill(db, invoice.service_bill_id)
        bill.status = "draft"
        bill.updated_at = now
        if bill.manual_override_cents is not None:
            bill.override_applied_revision = now
        await db.commit()
        invoice = await _load_invoice(db, invoice_id)

    return CancelledOut(
        invoice=await _invoice_out(db, invoice), replacement_bill_id=str(invoice.service_bill_id)
    )


# --- reading --------------------------------------------------------------------------------


@router.get("/invoices")
async def list_invoices(
    _: BillViewer, db: SessionDep, customer_id: uuid.UUID | None = None
) -> dict[str, list[InvoiceSummaryOut]]:
    """The billing list for manual outstanding-balance follow-up (#66's own acceptance
    criterion); `customer_id` narrows it to one client's own invoice history — the same
    endpoint, not a second one, since the shape a client's history needs is identical."""
    stmt = select(Invoice).order_by(Invoice.invoice_number)
    if customer_id is not None:
        stmt = stmt.where(Invoice.customer_id == customer_id)
    invoices = list(await db.scalars(stmt))
    by_id = await balances(db, invoices)
    return {
        "invoices": [
            InvoiceSummaryOut(
                id=str(i.id),
                invoice_number=i.invoice_number,
                customer_id=str(i.customer_id),
                status=i.status,
                grand_total_cents=i.grand_total_cents,
                issued_at=i.issued_at,
                **asdict(by_id[i.id]),
            )
            for i in invoices
        ]
    }


@router.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: uuid.UUID, _: BillViewer, db: SessionDep) -> InvoiceOut:
    invoice = await _load_invoice(db, invoice_id)
    return await _invoice_out(db, invoice)


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

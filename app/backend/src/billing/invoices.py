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

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

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
from billing.commission import (
    CommissionDiscountInput,
    commission_amount_cents,
    commission_basis_cents,
)
from billing.invoice_numbering import allocate_invoice_number
from billing.models import (
    BillOverrideRequest,
    CommissionPosting,
    Discount,
    Invoice,
    InvoiceLine,
    InvoiceLineDiscount,
    InvoiceLineTax,
)
from core.audit import record_event
from core.db import SessionDep

router = APIRouter(tags=["billing"])


# --- what goes over the wire -----------------------------------------------------------------


class InvoiceLineDiscountOut(BaseModel):
    # Deliberately no `commission_basis` — this route is Staff-Mode-reachable (`billing.view`,
    # no admin window) and commission data is never staff-facing (CLAUDE.md; #69). Read the
    # commission-basis choice via `GET /admin/reports/commission` (`commission.view`) instead.
    discount_id: str
    discount_name: str
    discount_kind: str


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
        discounted_cents=line.discounted_cents,
        pretax_cents=line.pretax_cents,
        tax_cents=line.tax_cents,
        line_total_cents=line.line_total_cents,
        discounts=[
            InvoiceLineDiscountOut(
                discount_id=str(d.discount_id),
                discount_name=d.discount_name,
                discount_kind=d.discount_kind,
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

        applied_discounts = []
        for discount_id_str in line_out.applied_discount_ids:
            discount = discounts_by_id.get(uuid.UUID(discount_id_str))
            if discount is None:  # pragma: no cover — defensive; _compute already validated this
                continue
            applied_discounts.append(discount)
            db.add(
                InvoiceLineDiscount(
                    invoice_line_id=invoice_line.id,
                    discount_id=discount.id,
                    discount_name=discount.name,
                    discount_kind=discount.kind,
                    commission_basis=discount.commission_basis,
                )
            )

        # #69: commission, posted in this same transaction — the rate is always
        # `bill_line.commission_rate_bp` (snapshotted at completion, #59, never re-read live),
        # the basis is `billing/commission.py`'s own formula against the *live* `Discount`
        # rows fetched above (still live at this exact moment, before they are frozen onto
        # `InvoiceLineDiscount` a few lines up) — see that module's docstring for why the live
        # rows, not the frozen ones, are what the formula needs. `staff_id` is always
        # `bill_line.staff_id` — the delivering staff member, never inferred from a package
        # sale (module docstring; #72's own constraint).
        commission_discounts = [
            CommissionDiscountInput(
                id=d.id,
                kind=d.kind,
                stackable=d.stackable,
                percentage_bp=d.percentage_bp,
                amount_cents=d.amount_cents,
                commission_basis=d.commission_basis,
            )
            for d in applied_discounts
        ]
        basis_cents = commission_basis_cents(bill_line.price_cents, commission_discounts)
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

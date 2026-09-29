"""Retail sale: draft -> atomic stock-deducting issue (#75). See `billing/models.py`'s
`## retail sale` section for the full design (schema, the "sold by"/payment-collector split,
the commission-field decision) — this module is the one writer of `retail_sales`/
`retail_sale_lines`/`retail_invoices`/`retail_invoice_lines`.

**Capability: `billing.view`, reused — not a new key.** Starting a sale, building its cart and
issuing it are all front-desk checkout work, the same call #63/#65 already made for the
service side.

**The atomic multi-line issue is the point of this ticket.** `issue_retail_sale` calls
`inventory/stock.py::record_movement` once per line inside the one transaction that also
inserts the `RetailInvoice`/`RetailInvoiceLine` rows, and does not commit until every line has
succeeded — see the module section in `billing/models.py` for exactly how that composes #61's
own atomic `UPDATE` across more than one variant with a full rollback on any single line's
failure.

**Tax and discounts (review R3/R4)** — the service side's rules, applied per line: each
variant's own `tax_component_keys`/`tax_convention`, and a sale-level discount selection
(`PUT /retail-sales/{id}/discounts`, eligibility `("product", product_id)`) priced by
`billing/pricing.py::price_line`. Draft reads recompute live; issue freezes every line's
resolved discounts (rule + cents) and tax components (rate + cents).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from auth.capabilities import Requires
from auth.models import User
from billing.bill_review import (
    LineTaxOut,
    _applicable_components,
    _today_in,
    components_for,
    discount_input,
    eligible_discounts,
    tax_out,
)
from billing.commission import commission_amount_cents
from billing.commission_ledger import reverse_commission
from billing.discount_resolver import DiscountConflict
from billing.invoice_numbering import allocate_invoice_number
from billing.models import (
    CommissionPosting,
    Discount,
    RetailInvoice,
    RetailInvoiceLine,
    RetailInvoiceLineDiscount,
    RetailInvoiceLineTax,
    RetailReturn,
    RetailReturnLine,
    RetailSale,
    RetailSaleDiscount,
    RetailSaleLine,
)
from billing.payments import (
    AdminReviewer,
    balance,
    carry_payments,
    lock_issued,
    lock_lineage,
    record_refund,
)
from billing.pricing import PricedLine, price_line
from billing.tax import ComponentRate, invoice_tax_totals
from core.audit import record_event
from core.db import SessionDep
from core.models import Business
from customers.models import Customer
from inventory.models import ProductVariant
from inventory.stock import InsufficientStock, VariantNotFound, record_movement
from notifications.triggers import notify_low_stock
from scheduling.models import Staff

router = APIRouter(tags=["billing"])

RetailSeller = Annotated[User, Depends(Requires("billing.view"))]


# --- what goes over the wire -----------------------------------------------------------------


class StartRetailSaleIn(BaseModel):
    customer_id: uuid.UUID | None = None
    # Defaults to the acting staff member (`_acting_staff_id`) when omitted — staff-selectable
    # for an admin/manager ringing up a sale on behalf of a colleague.
    sold_by_staff_id: uuid.UUID | None = None


class PatchRetailSaleIn(BaseModel):
    """Every field optional and applied only when sent (`exclude_unset`) — the same shape
    `scheduling/staff.py::StaffPatch` already establishes for a partial update."""

    customer_id: uuid.UUID | None = None
    sold_by_staff_id: uuid.UUID | None = None
    payment_collector_staff_id: uuid.UUID | None = None


class AddRetailSaleLineIn(BaseModel):
    variant_id: uuid.UUID
    quantity: Annotated[int, Field(ge=1, le=1_000_000)]


class RetailSaleLineOut(BaseModel):
    id: str
    variant_id: str
    quantity: int
    unit_price_cents: int
    # Review R3: the variant's convention, the discounts that apply (id -> cents off) and the
    # line's tax. `line_total_cents` is tax-included.
    tax_convention: str
    discount_amounts: dict[str, int]
    discount_cents: int
    tax: LineTaxOut
    line_total_cents: int


class RetailSaleOut(BaseModel):
    id: str
    status: str
    customer_id: str | None
    sold_by_staff_id: str
    payment_collector_staff_id: str | None
    # R15: set on the replacement draft a retail cancel opened.
    replaces_retail_invoice_id: str | None = None
    lines: list[RetailSaleLineOut]
    # The applied selection; `subtotal - discount_total + tax_total == grand_total`.
    discount_ids: list[str]
    subtotal_cents: int
    discount_total_cents: int
    tax_totals_by_component: dict[str, int]
    tax_total_cents: int
    grand_total_cents: int
    created_at: datetime
    updated_at: datetime


class ApplyRetailDiscountsIn(BaseModel):
    discount_ids: list[uuid.UUID] = []


class RetailInvoiceLineDiscountOut(BaseModel):
    discount_id: str
    discount_name: str
    discount_kind: str
    percentage_bp: int | None
    amount_cents: int | None
    resolved_amount_cents: int


class RetailInvoiceLineTaxOut(BaseModel):
    component_code: str
    rate_bp: int
    amount_cents: int


class RetailInvoiceLineOut(BaseModel):
    id: str
    variant_id: str
    quantity: int
    unit_price_cents: int
    discount_cents: int
    tax_cents: int
    tax_convention: str
    line_total_cents: int
    discounts: list[RetailInvoiceLineDiscountOut]
    taxes: list[RetailInvoiceLineTaxOut]
    # R26: no commission rate or basis — this payload is staff-facing (`billing.view`); the
    # admin-only commission report (`commission.view`) is where retail commission is read.
    staff_id: str


class RetailInvoiceOut(BaseModel):
    id: str
    business_id: int
    invoice_number: int
    retail_sale_id: str
    customer_id: str | None
    status: str
    subtotal_cents: int
    discount_total_cents: int
    tax_total_cents: int
    tax_totals_by_component: dict[str, int]
    grand_total_cents: int
    sold_by_staff_id: str
    payment_collector_staff_id: str | None
    issued_at: datetime
    issued_by: str
    lines: list[RetailInvoiceLineOut]
    # #76: from the shared payment ledger (`billing/payments.py::balances`).
    outstanding_cents: int
    refunded_cents: int
    checkout_complete: bool
    # R12: money a cancelled retail invoice still holds (0 while issued).
    held_credit_cents: int
    # R15 lineage, both ways, as `InvoiceOut` carries it.
    replaces_invoice_id: str | None
    replaced_by_invoice_id: str | None
    cancelled_at: datetime | None
    cancel_reason: str | None


class RetailInvoiceSummaryOut(BaseModel):
    id: str
    invoice_number: int
    customer_id: str | None
    status: str
    grand_total_cents: int
    issued_at: datetime


@dataclass
class _PricedRetailLine:
    line: RetailSaleLine
    discounts: list[Discount]
    components: list[ComponentRate]
    priced: PricedLine


async def _selection(db: SessionDep, sale_id: uuid.UUID) -> set[uuid.UUID]:
    return set(
        await db.scalars(
            select(RetailSaleDiscount.discount_id).where(RetailSaleDiscount.sale_id == sale_id)
        )
    )


async def _price_sale(
    db: SessionDep, sale: RetailSale, selected_ids: set[uuid.UUID]
) -> tuple[list[_PricedRetailLine], list[ComponentRate]]:
    """Every line priced against its variant's live tax settings and the selection — the
    one computation the draft view, the discount PUT and issue share. 422 on a conflict."""
    business = await _business(db)
    pool = await _applicable_components(db, business, _today_in(business))
    variant_ids = {line.variant_id for line in sale.lines}
    variants = (
        {
            v.id: v
            for v in await db.scalars(
                select(ProductVariant).where(ProductVariant.id.in_(variant_ids))
            )
        }
        if variant_ids
        else {}
    )
    enabled = list(await db.scalars(select(Discount).where(Discount.enabled)))
    priced_lines = []
    for line in sale.lines:
        variant = variants[line.variant_id]
        applied = eligible_discounts(enabled, selected_ids, "product", variant.product_id)
        components = components_for(pool, variant.tax_component_keys)
        try:
            priced = price_line(
                line.unit_price_cents * line.quantity,
                variant.tax_convention,
                components,
                [discount_input(d) for d in applied],
            )
        except DiscountConflict as error:
            raise HTTPException(status_code=422, detail=f"{variant.name}: {error}") from error
        priced_lines.append(_PricedRetailLine(line, applied, components, priced))
    return priced_lines, pool


async def _sale_out(db: SessionDep, sale: RetailSale) -> RetailSaleOut:
    selected = await _selection(db, sale.id)
    priced_lines, _ = await _price_sale(db, sale, selected)
    tax_totals = invoice_tax_totals([p.priced.tax for p in priced_lines])
    tax_total = sum(tax_totals.values())
    grand_total = sum(p.priced.tax.total_cents for p in priced_lines)
    discount_total = sum(sum(p.priced.discount_amounts.values()) for p in priced_lines)
    return RetailSaleOut(
        id=str(sale.id),
        status=sale.status,
        customer_id=str(sale.customer_id) if sale.customer_id is not None else None,
        sold_by_staff_id=str(sale.sold_by_staff_id),
        payment_collector_staff_id=(
            str(sale.payment_collector_staff_id)
            if sale.payment_collector_staff_id is not None
            else None
        ),
        lines=[
            RetailSaleLineOut(
                id=str(p.line.id),
                variant_id=str(p.line.variant_id),
                quantity=p.line.quantity,
                unit_price_cents=p.line.unit_price_cents,
                tax_convention=p.priced.convention,
                discount_amounts={str(k): v for k, v in p.priced.discount_amounts.items()},
                discount_cents=sum(p.priced.discount_amounts.values()),
                tax=tax_out(p.priced.tax),
                line_total_cents=p.priced.tax.total_cents,
            )
            for p in priced_lines
        ],
        discount_ids=sorted(str(i) for i in selected),
        subtotal_cents=grand_total - tax_total + discount_total,
        discount_total_cents=discount_total,
        tax_totals_by_component=tax_totals,
        tax_total_cents=tax_total,
        grand_total_cents=grand_total,
        replaces_retail_invoice_id=(
            str(sale.replaces_retail_invoice_id) if sale.replaces_retail_invoice_id else None
        ),
        created_at=sale.created_at,
        updated_at=sale.updated_at,
    )


async def _invoice_out(db: SessionDep, invoice: RetailInvoice) -> RetailInvoiceOut:
    money = await balance(db, invoice)
    replaced_by = await db.scalar(
        select(RetailInvoice.id).where(RetailInvoice.replaces_invoice_id == invoice.id)
    )
    return RetailInvoiceOut(
        outstanding_cents=money.outstanding_cents,
        refunded_cents=money.refunded_cents,
        checkout_complete=money.checkout_complete,
        held_credit_cents=money.held_credit_cents,
        replaces_invoice_id=(
            str(invoice.replaces_invoice_id) if invoice.replaces_invoice_id else None
        ),
        replaced_by_invoice_id=str(replaced_by) if replaced_by else None,
        cancelled_at=invoice.cancelled_at,
        cancel_reason=invoice.cancel_reason,
        id=str(invoice.id),
        business_id=invoice.business_id,
        invoice_number=invoice.invoice_number,
        retail_sale_id=str(invoice.retail_sale_id),
        customer_id=str(invoice.customer_id) if invoice.customer_id is not None else None,
        status=invoice.status,
        subtotal_cents=invoice.subtotal_cents,
        discount_total_cents=invoice.discount_total_cents,
        tax_total_cents=invoice.tax_total_cents,
        tax_totals_by_component=invoice.tax_totals_by_component,
        grand_total_cents=invoice.grand_total_cents,
        sold_by_staff_id=str(invoice.sold_by_staff_id),
        payment_collector_staff_id=(
            str(invoice.payment_collector_staff_id)
            if invoice.payment_collector_staff_id is not None
            else None
        ),
        issued_at=invoice.issued_at,
        issued_by=str(invoice.issued_by),
        lines=[
            RetailInvoiceLineOut(
                id=str(line.id),
                variant_id=str(line.variant_id),
                quantity=line.quantity,
                unit_price_cents=line.unit_price_cents,
                discount_cents=line.discount_cents,
                tax_cents=line.tax_cents,
                tax_convention=line.tax_convention,
                line_total_cents=line.line_total_cents,
                discounts=[
                    RetailInvoiceLineDiscountOut(
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
                    RetailInvoiceLineTaxOut(
                        component_code=t.component_code,
                        rate_bp=t.rate_bp,
                        amount_cents=t.amount_cents,
                    )
                    for t in line.taxes
                ],
                staff_id=str(line.staff_id),
            )
            for line in invoice.lines
        ],
    )


# --- loaders ----------------------------------------------------------------------------------


async def _acting_staff_id(db: SessionDep, actor: User) -> uuid.UUID:
    """The staff member behind this session (`scheduling/appointments.py`'s own `Staff.
    user_id == actor.id` lookup) — every account has exactly one (`Staff`'s own docstring:
    "one row per account, always"), so this is never unresolvable."""
    staff_id = await db.scalar(select(Staff.id).where(Staff.user_id == actor.id))
    assert staff_id is not None
    return staff_id


async def _load_sale(db: SessionDep, sale_id: uuid.UUID) -> RetailSale:
    sale = await db.get(RetailSale, sale_id, populate_existing=True)
    if sale is None:
        raise HTTPException(status_code=404, detail="No such retail sale.")
    return sale


async def _load_staff(db: SessionDep, staff_id: uuid.UUID) -> Staff:
    staff = await db.get(Staff, staff_id)
    if staff is None:
        raise HTTPException(status_code=404, detail="No such staff member.")
    return staff


async def _load_variant(db: SessionDep, variant_id: uuid.UUID) -> ProductVariant:
    variant = await db.get(ProductVariant, variant_id)
    if variant is None:
        raise HTTPException(status_code=404, detail="No such product variant.")
    return variant


async def _require_customer(db: SessionDep, customer_id: uuid.UUID) -> None:
    exists = await db.scalar(select(Customer.id).where(Customer.id == customer_id))
    if exists is None:
        raise HTTPException(status_code=404, detail="No such customer.")


async def _business(db: SessionDep) -> Business:
    business = await db.scalar(select(Business).where(Business.id == 1))
    if business is None:
        raise HTTPException(status_code=404, detail="This instance has not been set up.")
    return business


# --- the draft: starting a sale and building its cart ------------------------------------------


@router.post("/retail-sales", status_code=201)
async def start_retail_sale(
    payload: StartRetailSaleIn, actor: RetailSeller, db: SessionDep
) -> RetailSaleOut:
    if payload.customer_id is not None:
        await _require_customer(db, payload.customer_id)

    sold_by_staff_id = payload.sold_by_staff_id
    if sold_by_staff_id is not None:
        await _load_staff(db, sold_by_staff_id)
    else:
        sold_by_staff_id = await _acting_staff_id(db, actor)

    sale = RetailSale(customer_id=payload.customer_id, sold_by_staff_id=sold_by_staff_id)
    db.add(sale)
    await db.flush()

    record_event(
        db,
        "retail_sale.started",
        target_type="retail_sale",
        target_id=str(sale.id),
        actor_user_id=actor.id,
        metadata={"customer_id": str(payload.customer_id) if payload.customer_id else None},
    )
    await db.commit()
    return await _sale_out(db, await _load_sale(db, sale.id))


@router.get("/retail-sales/{sale_id}")
async def get_retail_sale(sale_id: uuid.UUID, _: RetailSeller, db: SessionDep) -> RetailSaleOut:
    return await _sale_out(db, await _load_sale(db, sale_id))


@router.patch("/retail-sales/{sale_id}")
async def patch_retail_sale(
    sale_id: uuid.UUID, payload: PatchRetailSaleIn, actor: RetailSeller, db: SessionDep
) -> RetailSaleOut:
    sale = await _load_sale(db, sale_id)
    if sale.status != "draft":
        raise HTTPException(status_code=422, detail="This retail sale has already been issued.")

    sent = payload.model_dump(exclude_unset=True)
    if sent.get("customer_id") is not None:
        await _require_customer(db, sent["customer_id"])
    if sent.get("sold_by_staff_id") is not None:
        await _load_staff(db, sent["sold_by_staff_id"])
    if sent.get("payment_collector_staff_id") is not None:
        await _load_staff(db, sent["payment_collector_staff_id"])

    for field, value in sent.items():
        setattr(sale, field, value)
    await db.flush()

    if sent:
        record_event(
            db,
            "retail_sale.updated",
            target_type="retail_sale",
            target_id=str(sale.id),
            actor_user_id=actor.id,
            metadata={"changed": sorted(sent)},
        )
    await db.commit()
    return await _sale_out(db, await _load_sale(db, sale.id))


@router.post("/retail-sales/{sale_id}/lines", status_code=201)
async def add_retail_sale_line(
    sale_id: uuid.UUID, payload: AddRetailSaleLineIn, actor: RetailSeller, db: SessionDep
) -> RetailSaleOut:
    """Adds a variant/quantity to the cart. Snapshots `ProductVariant.price_cents` onto the
    line right now (`billing/models.py`'s own "copied rather than referenced" contract) — and
    touches nothing on `product_variants` itself: no reservation, no soft-lock (#75's own first
    acceptance criterion)."""
    sale = await _load_sale(db, sale_id)
    if sale.status != "draft":
        raise HTTPException(status_code=422, detail="This retail sale has already been issued.")

    variant = await _load_variant(db, payload.variant_id)
    if not variant.active:
        raise HTTPException(
            status_code=422, detail="This product variant is inactive and cannot be sold."
        )

    db.add(
        RetailSaleLine(
            sale_id=sale.id,
            variant_id=variant.id,
            quantity=payload.quantity,
            unit_price_cents=variant.price_cents,
        )
    )
    await db.flush()

    record_event(
        db,
        "retail_sale.line_added",
        target_type="retail_sale",
        target_id=str(sale.id),
        actor_user_id=actor.id,
        metadata={"variant_id": str(variant.id), "quantity": payload.quantity},
    )
    await db.commit()
    return await _sale_out(db, await _load_sale(db, sale.id))


@router.delete("/retail-sales/{sale_id}/lines/{line_id}")
async def remove_retail_sale_line(
    sale_id: uuid.UUID, line_id: uuid.UUID, actor: RetailSeller, db: SessionDep
) -> RetailSaleOut:
    sale = await _load_sale(db, sale_id)
    if sale.status != "draft":
        raise HTTPException(status_code=422, detail="This retail sale has already been issued.")

    line = await db.get(RetailSaleLine, line_id)
    if line is None or line.sale_id != sale.id:
        raise HTTPException(status_code=404, detail="No such line on this retail sale.")

    await db.delete(line)
    await db.flush()

    record_event(
        db,
        "retail_sale.line_removed",
        target_type="retail_sale",
        target_id=str(sale.id),
        actor_user_id=actor.id,
        metadata={"line_id": str(line_id)},
    )
    await db.commit()
    return await _sale_out(db, await _load_sale(db, sale.id))


@router.put("/retail-sales/{sale_id}/discounts")
async def apply_retail_discounts(
    sale_id: uuid.UUID, payload: ApplyRetailDiscountsIn, actor: RetailSeller, db: SessionDep
) -> RetailSaleOut:
    """Replaces the draft's whole discount selection (review R3; `bill_review.
    apply_discounts`' shape). A combination that fails on any line is refused whole (422) and
    nothing is persisted."""
    sale = await _load_sale(db, sale_id)
    if sale.status != "draft":
        raise HTTPException(status_code=422, detail="This retail sale has already been issued.")
    selected = set(payload.discount_ids)
    if selected:
        known = set(
            await db.scalars(select(Discount.id).where(Discount.id.in_(selected), Discount.enabled))
        )
        if selected - known:
            raise HTTPException(
                status_code=422,
                detail="No such enabled discount: "
                + ", ".join(sorted(str(i) for i in selected - known)),
            )
    await _price_sale(db, sale, selected)  # 422 before anything is written

    await db.execute(delete(RetailSaleDiscount).where(RetailSaleDiscount.sale_id == sale_id))
    db.add_all([RetailSaleDiscount(sale_id=sale_id, discount_id=i) for i in selected])
    await db.flush()
    record_event(
        db,
        "retail_sale.discounts_applied",
        target_type="retail_sale",
        target_id=str(sale_id),
        actor_user_id=actor.id,
        metadata={"discount_ids": sorted(str(i) for i in selected)},
    )
    await db.commit()
    return await _sale_out(db, await _load_sale(db, sale_id))


# --- issue: the one atomic, stock-deducting moment ---------------------------------------------


@router.post("/retail-sales/{sale_id}/issue", status_code=201)
async def issue_retail_sale(
    sale_id: uuid.UUID, actor: RetailSeller, db: SessionDep
) -> RetailInvoiceOut:
    business = await _business(db)
    # R11: the sale's row lock serializes concurrent issues — the loser waits, then reads
    # `issued` below and gets the ordinary 422, never a unique-constraint 500.
    await db.execute(select(RetailSale.id).where(RetailSale.id == sale_id).with_for_update())
    sale = await _load_sale(db, sale_id)

    if sale.status != "draft":
        raise HTTPException(status_code=422, detail="This retail sale has already been issued.")
    if not sale.lines:
        raise HTTPException(status_code=422, detail="A retail sale with no lines cannot be issued.")

    staff = await _load_staff(db, sale.sold_by_staff_id)

    priced_lines, pool = await _price_sale(db, sale, await _selection(db, sale.id))
    priced_by_line = {p.line.id: p for p in priced_lines}
    rates = {c.code: c.rate_bp for c in pool}
    tax_totals = invoice_tax_totals([p.priced.tax for p in priced_lines])
    tax_total_cents = sum(tax_totals.values())
    grand_total_cents = sum(p.priced.tax.total_cents for p in priced_lines)
    discount_total_cents = sum(sum(p.priced.discount_amounts.values()) for p in priced_lines)

    invoice_number = await allocate_invoice_number(db, business_id=business.id)

    invoice = RetailInvoice(
        business_id=business.id,
        invoice_number=invoice_number,
        retail_sale_id=sale.id,
        customer_id=sale.customer_id,
        subtotal_cents=grand_total_cents - tax_total_cents + discount_total_cents,
        discount_total_cents=discount_total_cents,
        tax_total_cents=tax_total_cents,
        tax_totals_by_component=tax_totals,
        grand_total_cents=grand_total_cents,
        sold_by_staff_id=sale.sold_by_staff_id,
        payment_collector_staff_id=sale.payment_collector_staff_id,
        issued_by=actor.id,
        replaces_invoice_id=sale.replaces_retail_invoice_id,
    )
    db.add(invoice)
    await db.flush()

    original = None
    if sale.replaces_retail_invoice_id is not None:
        original = await db.get(RetailInvoice, sale.replaces_retail_invoice_id)
        assert original is not None and original.status == "cancelled"
        await carry_payments(db, original, invoice, actor.id)

    # One guarded movement per variant, in variant-id order (`billing/models.py`'s own module
    # section): two concurrent sales sharing more than one variant always take their row locks
    # in the same order, so neither can deadlock against the other. A replacement (R15) moves
    # only the difference from what its cancelled original already handed over: an unchanged
    # item never leaves the shelf twice, a dropped one comes back as a `return` movement, and
    # an added one is an ordinary `sale` (spec: explicit additional-sale / stock-return
    # movements for real changes; cancellation alone never moves stock).
    carried = await _still_with_client(db, original)
    wanted: dict[uuid.UUID, int] = {}
    for line in sale.lines:
        wanted[line.variant_id] = wanted.get(line.variant_id, 0) + line.quantity
    armed_variant_ids: list[uuid.UUID] = []
    for variant_id in sorted(wanted.keys() | carried.keys()):
        delta = carried.get(variant_id, 0) - wanted.get(variant_id, 0)
        if delta == 0:
            continue
        try:
            movement = await record_movement(
                db,
                variant_id=variant_id,
                kind="sale" if delta < 0 else "return",
                quantity_delta=delta,
                actor_user_id=actor.id,
                reason=(
                    f"Dropped from the replacement of retail invoice {original.invoice_number}"
                    if original is not None and delta > 0
                    else None
                ),
            )
        except VariantNotFound:
            raise HTTPException(
                status_code=404, detail=f"No such product variant: {variant_id}."
            ) from None
        except InsufficientStock as error:
            variant = await db.get(ProductVariant, variant_id)
            name = variant.name if variant is not None else str(variant_id)
            raise HTTPException(
                status_code=409,
                detail=f"Not enough stock for '{name}' to complete this sale: {error}",
            ) from None
        if movement.low_stock_alert_armed:
            armed_variant_ids.append(variant_id)

    for line in sale.lines:
        p = priced_by_line[line.id]
        invoice_line = RetailInvoiceLine(
            invoice_id=invoice.id,
            retail_sale_line_id=line.id,
            variant_id=line.variant_id,
            quantity=line.quantity,
            unit_price_cents=line.unit_price_cents,
            discount_cents=sum(p.priced.discount_amounts.values()),
            tax_cents=p.priced.tax.tax_cents,
            tax_convention=p.priced.convention,
            line_total_cents=p.priced.tax.total_cents,
            commission_basis_cents=p.priced.commission_basis_cents,
            staff_id=sale.sold_by_staff_id,
            # `Staff.commission_rate_retail_bp`, read fresh and frozen here — retail's own
            # "delivery" moment (`billing/models.py`'s own module section).
            commission_rate_bp=staff.commission_rate_retail_bp,
        )
        db.add(invoice_line)
        await db.flush()
        # R27: retail commission is earned at the sale (retail's delivery), on the pre-tax
        # basis with "absorbed" discounts left out, at the seller's rate frozen just above.
        db.add(
            CommissionPosting(
                retail_invoice_line_id=invoice_line.id,
                retail_invoice_id=invoice.id,
                staff_id=invoice_line.staff_id,
                commission_rate_bp=invoice_line.commission_rate_bp,
                basis_cents=p.priced.commission_basis_cents,
                amount_cents=commission_amount_cents(
                    p.priced.commission_basis_cents, invoice_line.commission_rate_bp
                ),
            )
        )
        for discount in p.discounts:
            db.add(
                RetailInvoiceLineDiscount(
                    retail_invoice_line_id=invoice_line.id,
                    discount_id=discount.id,
                    discount_name=discount.name,
                    discount_kind=discount.kind,
                    percentage_bp=discount.percentage_bp,
                    amount_cents=discount.amount_cents,
                    stackable=discount.stackable,
                    commission_basis=discount.commission_basis,
                    resolved_amount_cents=p.priced.discount_amounts[discount.id],
                )
            )
        for code, amount_cents in p.priced.tax.component_cents.items():
            db.add(
                RetailInvoiceLineTax(
                    retail_invoice_line_id=invoice_line.id,
                    component_code=code,
                    rate_bp=rates.get(code, 0),
                    amount_cents=amount_cents,
                )
            )

    sale.status = "issued"
    await db.flush()

    record_event(
        db,
        "retail_invoice.issued",
        target_type="retail_invoice",
        target_id=str(invoice.id),
        actor_user_id=actor.id,
        metadata={
            "retail_sale_id": str(sale_id),
            "invoice_number": invoice_number,
            "grand_total_cents": grand_total_cents,
            "replaces_invoice_id": str(original.id) if original is not None else None,
        },
    )
    await db.commit()

    # Queued after commit, never before — the same requirement `inventory/stock_routes.py`'s
    # own routes already honour for the same reason (#62; CLAUDE.md: Celery workers, never
    # inline; a task queued before commit could race a rollback).
    for variant_id in armed_variant_ids:
        await notify_low_stock(db, variant_id)

    invoice = await db.get(RetailInvoice, invoice.id, populate_existing=True)
    assert invoice is not None
    return await _invoice_out(db, invoice)


# --- reading issued retail invoices -------------------------------------------------------------


@router.get("/retail-invoices")
async def list_retail_invoices(
    _: RetailSeller, db: SessionDep, customer_id: uuid.UUID | None = None
) -> dict[str, list[RetailInvoiceSummaryOut]]:
    """`?customer_id=` narrows to one client's retail history (a linked sale's invoice)."""
    query = select(RetailInvoice).order_by(RetailInvoice.invoice_number)
    if customer_id is not None:
        query = query.where(RetailInvoice.customer_id == customer_id)
    invoices = await db.scalars(query)
    return {
        "retail_invoices": [
            RetailInvoiceSummaryOut(
                id=str(i.id),
                invoice_number=i.invoice_number,
                customer_id=str(i.customer_id) if i.customer_id is not None else None,
                status=i.status,
                grand_total_cents=i.grand_total_cents,
                issued_at=i.issued_at,
            )
            for i in invoices
        ]
    }


@router.get("/retail-invoices/{invoice_id}")
async def get_retail_invoice(
    invoice_id: uuid.UUID, _: RetailSeller, db: SessionDep
) -> RetailInvoiceOut:
    invoice = await db.get(RetailInvoice, invoice_id, populate_existing=True)
    if invoice is None:
        raise HTTPException(status_code=404, detail="No such retail invoice.")
    return await _invoice_out(db, invoice)


# --- returns (#76): goods back and money back are two independent choices ----------------------


class ReturnLineIn(BaseModel):
    retail_invoice_line_id: uuid.UUID
    quantity: Annotated[int, Field(ge=1, le=1_000_000)]
    # False for an opened/damaged item: it counts as returned, but never goes back on the shelf.
    restock: bool


class RetailReturnIn(BaseModel):
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    lines: Annotated[list[ReturnLineIn], Field(min_length=1)]
    # Omitted = no money back. Never derived from the lines: the refund is its own decision.
    refund_cents: Annotated[int, Field(gt=0)] | None = None


class RetailReturnLineOut(BaseModel):
    id: str
    retail_invoice_line_id: str
    quantity: int
    restocked: bool


class RetailReturnOut(BaseModel):
    id: str
    retail_invoice_id: str
    reason: str
    refund_id: str | None
    refund_cents: int | None
    returned_by: str
    returned_at: datetime
    lines: list[RetailReturnLineOut]


@router.post("/retail-invoices/{invoice_id}/returns", status_code=201)
async def return_retail_items(
    invoice_id: uuid.UUID, payload: RetailReturnIn, actor: AdminReviewer, db: SessionDep
) -> RetailReturnOut:
    """One return action. `billing.manage` (Admin Mode) — the same gate as #67's refund, since a
    return may carry one. Restock writes a whole-unit `kind="return"` movement per line; the
    refund goes through `record_refund` (capped at money received less prior refunds). The
    retail invoice's row lock (`lock_lineage`) serializes concurrent returns, so a line's
    returned quantity can never exceed what was sold."""
    invoice = await db.get(RetailInvoice, invoice_id, populate_existing=True)
    if invoice is None:
        raise HTTPException(status_code=404, detail="No such retail invoice.")
    requested = [item.retail_invoice_line_id for item in payload.lines]
    if len(set(requested)) != len(requested):
        raise HTTPException(status_code=422, detail="Each line may appear only once per return.")

    # Status re-read under the lock: a return racing a cancel (R15) never lands on it.
    await lock_issued(db, invoice, "This retail invoice is not issued.")
    sold = {line.id: line for line in invoice.lines}
    returned = await _returned_by_line(db, invoice.id)
    for item in payload.lines:
        line = sold.get(item.retail_invoice_line_id)
        if line is None:
            raise HTTPException(status_code=404, detail="No such line on this retail invoice.")
        remaining = line.quantity - returned.get(line.id, 0)
        if item.quantity > remaining:
            raise HTTPException(
                status_code=422,
                detail=f"Only {remaining} of this line can still be returned.",
            )

    refund = None
    if payload.refund_cents is not None:
        refund = await record_refund(
            db, invoice, amount_cents=payload.refund_cents, reason=payload.reason, approver=actor
        )
    ret = RetailReturn(
        retail_invoice_id=invoice.id,
        reason=payload.reason,
        refund_id=refund.id if refund is not None else None,
        returned_by=actor.id,
    )
    db.add(ret)
    await db.flush()
    # Variant-id order, as at issue: two concurrent restocks never take row locks crosswise.
    for item in sorted(payload.lines, key=lambda i: sold[i.retail_invoice_line_id].variant_id):
        line = sold[item.retail_invoice_line_id]
        db.add(
            RetailReturnLine(
                return_id=ret.id,
                retail_invoice_line_id=line.id,
                quantity=item.quantity,
                restocked=item.restock,
            )
        )
        if item.restock:
            await record_movement(
                db,
                variant_id=line.variant_id,
                kind="return",
                quantity_delta=item.quantity,
                actor_user_id=actor.id,
                reason=payload.reason,
            )
        # R27: returned units stop earning — the line's cumulative reversal follows the share
        # returned so far (`billing/commission_ledger.py`'s rule), restocked or not.
        await reverse_commission(
            db,
            CommissionPosting.retail_invoice_line_id == line.id,
            share=(returned.get(line.id, 0) + item.quantity, line.quantity),
        )
    await db.flush()
    record_event(
        db,
        "retail_invoice.return_recorded",
        target_type="retail_invoice",
        target_id=str(invoice.id),
        actor_user_id=actor.id,
        metadata={
            "return_id": str(ret.id),
            "refund_cents": payload.refund_cents,
            "lines": [
                {
                    "line_id": str(i.retail_invoice_line_id),
                    "quantity": i.quantity,
                    "restock": i.restock,
                }
                for i in payload.lines
            ],
        },
    )
    await db.commit()
    ret = await db.get(RetailReturn, ret.id, populate_existing=True)
    assert ret is not None
    return RetailReturnOut(
        id=str(ret.id),
        retail_invoice_id=str(ret.retail_invoice_id),
        reason=ret.reason,
        refund_id=str(ret.refund_id) if ret.refund_id is not None else None,
        refund_cents=payload.refund_cents,
        returned_by=str(ret.returned_by),
        returned_at=ret.returned_at,
        lines=[
            RetailReturnLineOut(
                id=str(r.id),
                retail_invoice_line_id=str(r.retail_invoice_line_id),
                quantity=r.quantity,
                restocked=r.restocked,
            )
            for r in ret.lines
        ],
    )


# --- cancel & replace (M4 review R15; mirrors #68) ----------------------------------------------


async def _returned_by_line(db: SessionDep, invoice_id: uuid.UUID) -> dict[uuid.UUID, int]:
    """Units already brought back per line by #76 returns (restocked or not)."""
    return dict(
        (
            await db.execute(
                select(RetailReturnLine.retail_invoice_line_id, func.sum(RetailReturnLine.quantity))
                .join(RetailReturn, RetailReturn.id == RetailReturnLine.return_id)
                .where(RetailReturn.retail_invoice_id == invoice_id)
                .group_by(RetailReturnLine.retail_invoice_line_id)
            )
        ).all()
    )


async def _still_with_client(db: SessionDep, invoice: RetailInvoice | None) -> dict[uuid.UUID, int]:
    """Per variant: units `invoice` handed over that the client still has (sold less returned).
    Empty for an ordinary (non-replacement) sale."""
    if invoice is None:
        return {}
    returned = await _returned_by_line(db, invoice.id)
    kept: dict[uuid.UUID, int] = {}
    for line in invoice.lines:
        left = line.quantity - returned.get(line.id, 0)
        kept[line.variant_id] = kept.get(line.variant_id, 0) + left
    return kept


class CancelRetailInvoiceIn(BaseModel):
    reason: Annotated[str, Field(min_length=1, max_length=2000)]


class CancelledRetailOut(BaseModel):
    invoice: RetailInvoiceOut
    # A new draft sale carrying the original's customer, seller, collector and unreturned
    # lines at their original prices. Edit it, then `POST /retail-sales/{id}/issue` issues the
    # replacement (moving only the stock difference and carrying the original's money).
    replacement_sale_id: str


@router.post("/retail-invoices/{invoice_id}/cancel")
async def cancel_retail_invoice(
    invoice_id: uuid.UUID, payload: CancelRetailInvoiceIn, actor: RetailSeller, db: SessionDep
) -> CancelledRetailOut:
    """Voidable cancel (CLAUDE.md), `billing.view` like #68's service cancel: the original
    keeps its number and every frozen line; only the issued -> cancelled transition
    `retail_invoices_voidable_guard` permits is written. Cancelling moves no stock — the client
    still has the goods until a replacement says otherwise — and no money: payments stay on the
    original (shown as `held_credit_cents`) until the replacement's issue carries them. Under
    the lineage row lock a retry waits for, then returns, the first call's result."""
    invoice = await db.get(RetailInvoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="No such retail invoice.")
    await lock_lineage(db, invoice.id, RetailInvoice)
    await db.refresh(invoice)

    if invoice.status == "issued":
        invoice.status = "cancelled"
        invoice.cancelled_at = datetime.now(UTC)
        invoice.cancelled_by = actor.id
        invoice.cancel_reason = payload.reason
        # R27: what the original still earns is reversed today; the replacement earns its own
        # at issue, so the pair nets to one earning (#68's service shape).
        await reverse_commission(db, CommissionPosting.retail_invoice_id == invoice.id)
        replacement = RetailSale(
            customer_id=invoice.customer_id,
            sold_by_staff_id=invoice.sold_by_staff_id,
            payment_collector_staff_id=invoice.payment_collector_staff_id,
            replaces_retail_invoice_id=invoice.id,
        )
        db.add(replacement)
        await db.flush()
        returned = await _returned_by_line(db, invoice.id)
        for line in invoice.lines:
            left = line.quantity - returned.get(line.id, 0)
            if left > 0:
                db.add(
                    RetailSaleLine(
                        sale_id=replacement.id,
                        variant_id=line.variant_id,
                        quantity=left,
                        unit_price_cents=line.unit_price_cents,
                    )
                )
        record_event(
            db,
            "retail_invoice.cancelled",
            target_type="retail_invoice",
            target_id=str(invoice.id),
            actor_user_id=actor.id,
            metadata={
                "invoice_number": invoice.invoice_number,
                "reason": payload.reason,
                "retail_sale_id": str(invoice.retail_sale_id),
                "replacement_sale_id": str(replacement.id),
            },
        )
        await db.commit()

    replacement_id = await db.scalar(
        select(RetailSale.id).where(RetailSale.replaces_retail_invoice_id == invoice_id)
    )
    invoice = await db.get(RetailInvoice, invoice_id, populate_existing=True)
    assert invoice is not None and replacement_id is not None
    return CancelledRetailOut(
        invoice=await _invoice_out(db, invoice), replacement_sale_id=str(replacement_id)
    )

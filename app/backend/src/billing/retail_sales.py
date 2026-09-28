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
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from auth.capabilities import Requires
from auth.models import User
from billing.invoice_numbering import allocate_invoice_number
from billing.models import RetailInvoice, RetailInvoiceLine, RetailSale, RetailSaleLine
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
    line_total_cents: int


class RetailSaleOut(BaseModel):
    id: str
    status: str
    customer_id: str | None
    sold_by_staff_id: str
    payment_collector_staff_id: str | None
    lines: list[RetailSaleLineOut]
    subtotal_cents: int
    created_at: datetime
    updated_at: datetime


class RetailInvoiceLineOut(BaseModel):
    id: str
    variant_id: str
    quantity: int
    unit_price_cents: int
    line_total_cents: int
    staff_id: str
    commission_rate_bp: int


class RetailInvoiceOut(BaseModel):
    id: str
    business_id: int
    invoice_number: int
    retail_sale_id: str
    customer_id: str | None
    status: str
    subtotal_cents: int
    grand_total_cents: int
    sold_by_staff_id: str
    payment_collector_staff_id: str | None
    issued_at: datetime
    issued_by: str
    lines: list[RetailInvoiceLineOut]


class RetailInvoiceSummaryOut(BaseModel):
    id: str
    invoice_number: int
    customer_id: str | None
    status: str
    grand_total_cents: int
    issued_at: datetime


def _sale_out(sale: RetailSale) -> RetailSaleOut:
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
                id=str(line.id),
                variant_id=str(line.variant_id),
                quantity=line.quantity,
                unit_price_cents=line.unit_price_cents,
                line_total_cents=line.unit_price_cents * line.quantity,
            )
            for line in sale.lines
        ],
        subtotal_cents=sum(line.unit_price_cents * line.quantity for line in sale.lines),
        created_at=sale.created_at,
        updated_at=sale.updated_at,
    )


def _invoice_out(invoice: RetailInvoice) -> RetailInvoiceOut:
    return RetailInvoiceOut(
        id=str(invoice.id),
        business_id=invoice.business_id,
        invoice_number=invoice.invoice_number,
        retail_sale_id=str(invoice.retail_sale_id),
        customer_id=str(invoice.customer_id) if invoice.customer_id is not None else None,
        status=invoice.status,
        subtotal_cents=invoice.subtotal_cents,
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
                line_total_cents=line.line_total_cents,
                staff_id=str(line.staff_id),
                commission_rate_bp=line.commission_rate_bp,
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
    return _sale_out(await _load_sale(db, sale.id))


@router.get("/retail-sales/{sale_id}")
async def get_retail_sale(sale_id: uuid.UUID, _: RetailSeller, db: SessionDep) -> RetailSaleOut:
    return _sale_out(await _load_sale(db, sale_id))


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
    return _sale_out(await _load_sale(db, sale.id))


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
    return _sale_out(await _load_sale(db, sale.id))


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
    return _sale_out(await _load_sale(db, sale.id))


# --- issue: the one atomic, stock-deducting moment ---------------------------------------------


@router.post("/retail-sales/{sale_id}/issue", status_code=201)
async def issue_retail_sale(
    sale_id: uuid.UUID, actor: RetailSeller, db: SessionDep
) -> RetailInvoiceOut:
    business = await _business(db)
    sale = await _load_sale(db, sale_id)

    if sale.status != "draft":
        raise HTTPException(status_code=422, detail="This retail sale has already been issued.")
    if not sale.lines:
        raise HTTPException(status_code=422, detail="A retail sale with no lines cannot be issued.")

    staff = await _load_staff(db, sale.sold_by_staff_id)

    invoice_number = await allocate_invoice_number(db, business_id=business.id)
    subtotal_cents = sum(line.unit_price_cents * line.quantity for line in sale.lines)

    invoice = RetailInvoice(
        business_id=business.id,
        invoice_number=invoice_number,
        retail_sale_id=sale.id,
        customer_id=sale.customer_id,
        subtotal_cents=subtotal_cents,
        grand_total_cents=subtotal_cents,
        sold_by_staff_id=sale.sold_by_staff_id,
        payment_collector_staff_id=sale.payment_collector_staff_id,
        issued_by=actor.id,
    )
    db.add(invoice)
    await db.flush()

    # Variant-id order, not insertion order (`billing/models.py`'s own module section): two
    # concurrent multi-line sales sharing more than one variant then always take their row
    # locks in the same order, so neither can deadlock against the other.
    armed_variant_ids: list[uuid.UUID] = []
    for line in sorted(sale.lines, key=lambda one_line: one_line.variant_id):
        try:
            movement = await record_movement(
                db,
                variant_id=line.variant_id,
                kind="sale",
                quantity_delta=-line.quantity,
                actor_user_id=actor.id,
            )
        except VariantNotFound:
            raise HTTPException(
                status_code=404, detail=f"No such product variant: {line.variant_id}."
            ) from None
        except InsufficientStock as error:
            variant = await db.get(ProductVariant, line.variant_id)
            name = variant.name if variant is not None else str(line.variant_id)
            raise HTTPException(
                status_code=409,
                detail=f"Not enough stock for '{name}' to complete this sale: {error}",
            ) from None

        if movement.low_stock_alert_armed:
            armed_variant_ids.append(line.variant_id)

        db.add(
            RetailInvoiceLine(
                invoice_id=invoice.id,
                retail_sale_line_id=line.id,
                variant_id=line.variant_id,
                quantity=line.quantity,
                unit_price_cents=line.unit_price_cents,
                line_total_cents=line.unit_price_cents * line.quantity,
                staff_id=sale.sold_by_staff_id,
                # `Staff.commission_rate_retail_bp`, read fresh and frozen here — retail's own
                # "delivery" moment (`billing/models.py`'s own module section).
                commission_rate_bp=staff.commission_rate_retail_bp,
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
            "grand_total_cents": subtotal_cents,
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
    return _invoice_out(invoice)


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
    return _invoice_out(invoice)

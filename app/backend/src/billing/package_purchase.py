"""Package/bundle purchase (#71): buying a `PackageDefinition` (#60) is its own invoice,
issued through the exact same machinery `billing/invoices.py::issue_invoice` (#65) already
built — the same `business_invoice_counters` row, the same `Invoice` table, the same
`billing.view` checkout capability. See `billing/models.py`'s `## package/bundle purchase
(#71)` section for the full schema, snapshot contract and credit-activation design.

**One atomic action, no draft stage.** Unlike a service visit (accumulates across sibling
appointments before review/issue), a package purchase has nothing to accumulate — picking a
definition and a customer *is* the whole transaction. `PackagePurchase` is created and its
`Invoice` issued in the same request, the same transaction.

**Credits never activate here.** `PackagePurchase.credits_activated` is always `false` at
insert; `activate_credits` below is the one integration point a later payment-completion
caller (#66) uses, once it can prove the invoice is fully paid. Nothing in this module calls
it — see the module section in `billing/models.py` for exactly why, and what #66 must call.
"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from billing.allocation import allocate_bundle_price
from billing.bill_review import (
    BillViewer,
    _applicable_components,
    _business,
    _today_in,
    components_for,
)
from billing.invoice_numbering import allocate_invoice_number
from billing.models import (
    Invoice,
    PackageCreditVoid,
    PackageDefinition,
    PackagePurchase,
    PackagePurchaseCredit,
)
from billing.tax import compute_line_tax
from core.audit import record_event
from core.db import SessionDep
from customers.models import Customer
from scheduling.models import Service

router = APIRouter(prefix="/packages", tags=["billing"])


# --- the one activation hook point (module docstring; `billing/models.py`'s own section) -----


async def activate_credits(db: AsyncSession, purchase: PackagePurchase) -> None:
    """Flip `credits_activated` true, once a caller (today: only a test proving this works;
    eventually: #66's payment-ledger reconciliation, once an invoice is provably fully paid)
    has determined the purchase invoice is fully paid. Idempotent — a no-op if already
    activated, so a retried reconciliation pass never double-stamps `activated_at`."""
    if purchase.credits_activated:
        return
    purchase.credits_activated = True
    purchase.activated_at = datetime.now(UTC)


# --- what goes over the wire ------------------------------------------------------------------


class PurchasePackageIn(BaseModel):
    customer_id: uuid.UUID


class PackagePurchaseCreditOut(BaseModel):
    service_id: str
    credits_total: int
    allocated_price_cents: int


class PackagePurchaseOut(BaseModel):
    id: str
    package_definition_id: str
    customer_id: str
    name: str
    price_cents: int
    expires_after_days: int | None
    expires_at: str | None
    purchased_at: datetime
    credits_activated: bool
    activated_at: datetime | None
    # #73: set once a refund voided the unspent credits; none are redeemable after it.
    credits_voided_at: datetime | None = None
    credits: list[PackagePurchaseCreditOut]
    invoice_id: str
    invoice_number: int
    computed_subtotal_cents: int
    computed_tax_total_cents: int
    tax_totals_by_component: dict[str, int]
    grand_total_cents: int


def _out(purchase: PackagePurchase, invoice: Invoice) -> PackagePurchaseOut:
    return PackagePurchaseOut(
        id=str(purchase.id),
        package_definition_id=str(purchase.package_definition_id),
        customer_id=str(purchase.customer_id),
        name=purchase.name,
        price_cents=purchase.price_cents,
        expires_after_days=purchase.expires_after_days,
        expires_at=purchase.expires_at.isoformat() if purchase.expires_at else None,
        purchased_at=purchase.purchased_at,
        credits_activated=purchase.credits_activated,
        activated_at=purchase.activated_at,
        credits=[
            PackagePurchaseCreditOut(
                service_id=str(c.service_id),
                credits_total=c.credits_total,
                allocated_price_cents=c.allocated_price_cents,
            )
            for c in purchase.credits
        ],
        invoice_id=str(invoice.id),
        invoice_number=invoice.invoice_number,
        computed_subtotal_cents=invoice.computed_subtotal_cents,
        computed_tax_total_cents=invoice.computed_tax_total_cents,
        tax_totals_by_component=invoice.tax_totals_by_component,
        grand_total_cents=invoice.grand_total_cents,
    )


# --- purchasing ---------------------------------------------------------------------------


@router.post("/{definition_id}/purchase", status_code=201)
async def purchase_package(
    definition_id: uuid.UUID, payload: PurchasePackageIn, actor: BillViewer, db: SessionDep
) -> PackagePurchaseOut:
    business = await _business(db)

    definition = await db.scalar(
        select(PackageDefinition).where(PackageDefinition.id == definition_id)
    )
    if definition is None:
        raise HTTPException(status_code=404, detail="No such package.")
    if not definition.active:
        raise HTTPException(
            status_code=422,
            detail="This package has been deactivated and can no longer be sold.",
        )

    customer = await db.get(Customer, payload.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="No such customer.")

    # Freeze the definition's *current* shape (module docstring, `PackageDefinition`'s own
    # "Snapshot contract" note) — read exactly once, here, never re-joined again.
    definition_services = list(definition.services)
    service_ids = [row.service_id for row in definition_services]
    prices_by_id = {
        row.id: row.price_cents
        for row in await db.scalars(select(Service).where(Service.id.in_(service_ids)))
    }
    regular_prices = [prices_by_id[sid] for sid in service_ids]
    # `allocate_bundle_price` called exactly once — the whole point of freezing it onto
    # `PackagePurchaseCredit.allocated_price_cents` rather than recomputing it on every read.
    allocated = allocate_bundle_price(regular_prices, definition.price_cents)

    today = _today_in(business)
    expires_at = (
        today + timedelta(days=definition.expires_after_days)
        if definition.expires_after_days is not None
        else None
    )

    purchase = PackagePurchase(
        package_definition_id=definition.id,
        customer_id=customer.id,
        name=definition.name,
        price_cents=definition.price_cents,
        expires_after_days=definition.expires_after_days,
        expires_at=expires_at,
    )
    db.add(purchase)
    await db.flush()

    for row, allocated_cents in zip(definition_services, allocated, strict=True):
        db.add(
            PackagePurchaseCredit(
                package_purchase_id=purchase.id,
                service_id=row.service_id,
                credits_total=row.credits,
                allocated_price_cents=allocated_cents,
            )
        )

    # Tax: the definition's own component toggles and price convention (review R1/R2) — one
    # "line" (the whole purchase), so `Invoice.tax_totals_by_component` already *is* this
    # purchase's own per-component breakdown, with the resolved rates and the convention
    # frozen beside it (`tax_rates_by_component`/`tax_convention`).
    resolved_components = components_for(
        await _applicable_components(db, business, today), definition.tax_component_keys
    )
    line_tax = compute_line_tax(
        definition.price_cents, resolved_components, definition.tax_convention
    )

    invoice_number = await allocate_invoice_number(db, business_id=business.id)

    invoice = Invoice(
        business_id=business.id,
        invoice_number=invoice_number,
        service_bill_id=None,
        package_purchase_id=purchase.id,
        customer_id=customer.id,
        computed_subtotal_cents=line_tax.pretax_cents,
        computed_discount_total_cents=0,
        computed_tax_total_cents=line_tax.tax_cents,
        computed_grand_total_cents=line_tax.total_cents,
        tax_totals_by_component=line_tax.component_cents,
        tax_rates_by_component={c.code: c.rate_bp for c in resolved_components},
        tax_convention=definition.tax_convention,
        grand_total_cents=line_tax.total_cents,
        issued_by=actor.id,
    )
    db.add(invoice)
    await db.flush()

    record_event(
        db,
        "package_purchase.purchased",
        target_type="package_purchase",
        target_id=str(purchase.id),
        actor_user_id=actor.id,
        metadata={
            "package_definition_id": str(definition.id),
            "customer_id": str(customer.id),
            "invoice_id": str(invoice.id),
            "invoice_number": invoice_number,
            "price_cents": definition.price_cents,
            "grand_total_cents": line_tax.total_cents,
        },
    )
    await db.commit()

    purchase = await db.get(PackagePurchase, purchase.id, populate_existing=True)
    invoice = await db.get(Invoice, invoice.id, populate_existing=True)
    assert purchase is not None
    assert invoice is not None
    return _out(purchase, invoice)


# --- reading -------------------------------------------------------------------------------


@router.get("/purchases/{purchase_id}")
async def get_package_purchase(
    purchase_id: uuid.UUID, _: BillViewer, db: SessionDep
) -> PackagePurchaseOut:
    purchase = await db.get(PackagePurchase, purchase_id, populate_existing=True)
    if purchase is None:
        raise HTTPException(status_code=404, detail="No such package purchase.")
    invoice = await db.scalar(select(Invoice).where(Invoice.package_purchase_id == purchase_id))
    assert invoice is not None  # always created in the same transaction as the purchase
    out = _out(purchase, invoice)
    out.credits_voided_at = await db.scalar(
        select(PackageCreditVoid.voided_at).where(
            PackageCreditVoid.package_purchase_id == purchase_id
        )
    )
    return out

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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from auth.capabilities import Requires
from billing.allocation import allocate_bundle_price
from billing.bill_review import (
    BillViewer,
    applicable_components,
    business_or_404,
    components_for,
)
from billing.invoice_numbering import allocate_invoice_number
from billing.models import (
    Invoice,
    PackageCreditRedemption,
    PackageCreditVoid,
    PackageDefinition,
    PackagePurchase,
    PackagePurchaseCredit,
    PackageTransfer,
)
from billing.package_holder import transfer_chain
from billing.tax import compute_line_tax
from core.access_log import LogAccess, LogAccessOf
from core.audit import record_event
from core.db import SessionDep
from customers.models import Customer
from scheduling.clock import today_in
from scheduling.models import Appointment, Service

router = APIRouter(prefix="/packages", tags=["billing"])
# #108: the client Packages tab lives under `/customers/{customer_id}/...`, `forms/
# submissions.py`'s own precedent for a domain module mounting a second router there rather
# than folding its own routes into `customers/routes.py`.
customer_router = APIRouter(prefix="/customers", tags=["billing"])


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
    business = await business_or_404(db)

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
    # Weight = regular price × credits (spec §152): a credit row's allocation covers all its
    # sessions, so assessment 120 ×1 + follow-up 60 ×2 at 200 -> 100 / 100 (100 + 50 + 50 per
    # session, split by `redemption.py`). Pricing by price alone would value the two follow-ups
    # at the same total as the one assessment.
    regular_prices = [prices_by_id[row.service_id] * row.credits for row in definition_services]
    # `allocate_bundle_price` called exactly once — the whole point of freezing it onto
    # `PackagePurchaseCredit.allocated_price_cents` rather than recomputing it on every read.
    allocated = allocate_bundle_price(regular_prices, definition.price_cents)

    today = today_in(business.timezone)
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
        await applicable_components(db, business, today), definition.tax_component_keys
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
        tax_rates_by_component={c.code: c.rate_ppm for c in resolved_components},
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
    # R22: the package invoice PDF renders in the worker, queued after commit.
    from billing.documents import render_invoice_documents  # documents -> payments -> here

    render_invoice_documents.delay(str(invoice.id))

    purchase = await db.get(PackagePurchase, purchase.id, populate_existing=True)
    invoice = await db.get(Invoice, invoice.id, populate_existing=True)
    assert purchase is not None
    assert invoice is not None
    return _out(purchase, invoice)


# --- reading -------------------------------------------------------------------------------


@router.get(
    "/purchases/{purchase_id}",
    dependencies=[
        Depends(Requires("billing.view")),
        Depends(LogAccessOf("package_purchase", "purchase_id", PackagePurchase.customer_id)),
    ],
)
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


# --- client Packages tab (#108, spec #95 user story 54) ------------------------------------


class ClientPackagePurchaseCreditOut(BaseModel):
    service_id: str
    service_name: str
    credits_total: int
    credits_used: int
    credits_remaining: int
    # #111: this credit's redemptions by the client whose tab this row is on, specifically —
    # not every holder's combined usage. Meaningful on a transferred-out purchase, where it is
    # the old holder's own history; identical to `credits_used` when the purchase never moved.
    used_by_you: int


class TransferHopOut(BaseModel):
    id: str
    from_customer_id: str
    from_customer_name: str
    to_customer_id: str
    to_customer_name: str
    reason: str
    override: bool
    transferred_at: datetime


class ClientPackagePurchaseOut(BaseModel):
    id: str
    package_definition_id: str
    name: str
    price_cents: int
    # The purchaser, permanently (CLAUDE.md/#96 §"Data model") — invoice, payments and refunds
    # keep pointing here regardless of who holds the credits.
    customer_id: str
    purchaser_name: str
    # #111: derived — the `to` of the purchase's latest transfer, else the purchaser
    # (`billing/package_holder.py`).
    current_holder_id: str
    current_holder_name: str
    held_by_viewer: bool
    transferable: bool
    transfers: list[TransferHopOut]
    purchased_at: datetime
    expires_at: str | None
    credits_activated: bool
    credits_voided_at: datetime | None
    invoice_id: str
    invoice_number: int
    credits: list[ClientPackagePurchaseCreditOut]


class ClientPackagePurchases(BaseModel):
    purchases: list[ClientPackagePurchaseOut]


@customer_router.get(
    "/{customer_id}/package-purchases",
    dependencies=[
        Depends(Requires("billing.view")),
        # A client's package purchases are their financial history the same way an invoice is
        # (review R30's own reasoning) — logged like `GET /api/customers/{id}` and every other
        # client-scoped read, even though nothing here is in `core.access_log.PHI_FIELDS`.
        Depends(LogAccess("package_purchases")),
    ],
)
async def list_customer_package_purchases(
    customer_id: uuid.UUID, _: BillViewer, db: SessionDep
) -> ClientPackagePurchases:
    """Newest first: what this client currently holds, plus what they held earlier and later
    transferred away (#111, spec #96 API section) — everything the Packages tab (#108/#111)
    shows without a second request per row. A purchase belongs on this list if `customer_id`
    is the purchaser *or* was ever a transfer's `to` — the union below — never only "currently
    holds", or a transferred-out purchase would vanish from the old holder's own history
    (spec #96 story 22). No customer-existence check (`forms/submissions.py::list_submissions`'s
    own precedent): an unknown id simply lists nothing."""
    purchase_ids = list(
        await db.scalars(
            union(
                select(PackagePurchase.id).where(PackagePurchase.customer_id == customer_id),
                select(PackageTransfer.package_purchase_id).where(
                    PackageTransfer.to_customer_id == customer_id
                ),
            )
        )
    )
    if not purchase_ids:
        return ClientPackagePurchases(purchases=[])

    purchases = list(
        await db.scalars(
            select(PackagePurchase)
            .where(PackagePurchase.id.in_(purchase_ids))
            .order_by(PackagePurchase.purchased_at.desc())
        )
    )
    purchase_ids = [p.id for p in purchases]

    chains = await transfer_chain(db, purchase_ids)

    service_ids = {c.service_id for p in purchases for c in p.credits}
    names = (
        {
            row.id: row.name
            for row in await db.scalars(select(Service).where(Service.id.in_(service_ids)))
        }
        if service_ids
        else {}
    )
    transferable = {
        row.id: row.transferable
        for row in await db.scalars(
            select(PackageDefinition).where(
                PackageDefinition.id.in_([p.package_definition_id for p in purchases])
            )
        )
    }
    used = {
        (row.package_purchase_id, row.service_id): row.n
        for row in await db.execute(
            select(
                PackageCreditRedemption.package_purchase_id,
                PackageCreditRedemption.service_id,
                func.count().label("n"),
            )
            .where(PackageCreditRedemption.package_purchase_id.in_(purchase_ids))
            .group_by(
                PackageCreditRedemption.package_purchase_id,
                PackageCreditRedemption.service_id,
            )
        )
    }
    used_by_viewer = {
        (row.package_purchase_id, row.service_id): row.n
        for row in await db.execute(
            select(
                PackageCreditRedemption.package_purchase_id,
                PackageCreditRedemption.service_id,
                func.count().label("n"),
            )
            .join(Appointment, Appointment.id == PackageCreditRedemption.appointment_id)
            .where(
                PackageCreditRedemption.package_purchase_id.in_(purchase_ids),
                Appointment.customer_id == customer_id,
            )
            .group_by(
                PackageCreditRedemption.package_purchase_id,
                PackageCreditRedemption.service_id,
            )
        )
    }
    voided_at = {
        row.package_purchase_id: row.voided_at
        for row in await db.execute(
            select(PackageCreditVoid.package_purchase_id, PackageCreditVoid.voided_at).where(
                PackageCreditVoid.package_purchase_id.in_(purchase_ids)
            )
        )
    }
    invoices = {
        row.package_purchase_id: (row.id, row.invoice_number)
        for row in await db.execute(
            select(Invoice.package_purchase_id, Invoice.id, Invoice.invoice_number).where(
                Invoice.package_purchase_id.in_(purchase_ids)
            )
        )
    }

    customer_ids = {p.customer_id for p in purchases}
    for hops in chains.values():
        for hop in hops:
            customer_ids.add(hop.from_customer_id)
            customer_ids.add(hop.to_customer_id)
    customer_names = {
        row.id: f"{row.first_name} {row.last_name}"
        for row in await db.scalars(select(Customer).where(Customer.id.in_(customer_ids)))
    }

    def name_of(cid: uuid.UUID) -> str:
        return customer_names.get(cid, "—")

    out = []
    for p in purchases:
        invoice_id, invoice_number = invoices[p.id]  # always created in the same transaction
        hops = chains.get(p.id, [])
        holder_id = hops[-1].to_customer_id if hops else p.customer_id
        out.append(
            ClientPackagePurchaseOut(
                id=str(p.id),
                package_definition_id=str(p.package_definition_id),
                name=p.name,
                price_cents=p.price_cents,
                customer_id=str(p.customer_id),
                purchaser_name=name_of(p.customer_id),
                current_holder_id=str(holder_id),
                current_holder_name=name_of(holder_id),
                held_by_viewer=holder_id == customer_id,
                transferable=transferable.get(p.package_definition_id, False),
                transfers=[
                    TransferHopOut(
                        id=str(hop.id),
                        from_customer_id=str(hop.from_customer_id),
                        from_customer_name=name_of(hop.from_customer_id),
                        to_customer_id=str(hop.to_customer_id),
                        to_customer_name=name_of(hop.to_customer_id),
                        reason=hop.reason,
                        override=hop.override,
                        transferred_at=hop.transferred_at,
                    )
                    for hop in hops
                ],
                purchased_at=p.purchased_at,
                expires_at=p.expires_at.isoformat() if p.expires_at else None,
                credits_activated=p.credits_activated,
                credits_voided_at=voided_at.get(p.id),
                invoice_id=str(invoice_id),
                invoice_number=invoice_number,
                credits=[
                    ClientPackagePurchaseCreditOut(
                        service_id=str(c.service_id),
                        service_name=names.get(c.service_id, "—"),
                        credits_total=c.credits_total,
                        credits_used=used.get((p.id, c.service_id), 0),
                        credits_remaining=c.credits_total - used.get((p.id, c.service_id), 0),
                        used_by_you=used_by_viewer.get((p.id, c.service_id), 0),
                    )
                    for c in p.credits
                ],
            )
        )
    return ClientPackagePurchases(purchases=out)


# --- sellable packages: staff-facing, Staff-Mode-reachable (#108, spec #95 story 51) -------


class SellablePackageServiceOut(BaseModel):
    service_id: str
    service_name: str
    credits: int


class SellablePackageOut(BaseModel):
    id: str
    name: str
    price_cents: int
    expires_after_days: int | None
    tax_convention: str
    services: list[SellablePackageServiceOut]


class SellablePackages(BaseModel):
    packages: list[SellablePackageOut]


@router.get("")
async def list_sellable_packages(_: BillViewer, db: SessionDep) -> SellablePackages:
    """What front-desk staff sees to sell (spec #95 story 51): active packages only, `billing.
    view`, reachable in Staff Mode — unlike `GET /admin/packages` (`billing.manage`, Admin
    Mode, includes inactive), which stays exactly as it is. Only what selling one needs: name,
    price, credits per service, expiry, tax convention — not `description` or `transferable`,
    which the admin screen already owns."""
    definitions = list(
        await db.scalars(
            select(PackageDefinition)
            .where(PackageDefinition.active)
            .order_by(PackageDefinition.name)
        )
    )
    service_ids = {row.service_id for d in definitions for row in d.services}
    names = (
        {
            row.id: row.name
            for row in await db.scalars(select(Service).where(Service.id.in_(service_ids)))
        }
        if service_ids
        else {}
    )
    return SellablePackages(
        packages=[
            SellablePackageOut(
                id=str(d.id),
                name=d.name,
                price_cents=d.price_cents,
                expires_after_days=d.expires_after_days,
                tax_convention=d.tax_convention,
                services=[
                    SellablePackageServiceOut(
                        service_id=str(row.service_id),
                        service_name=names.get(row.service_id, "—"),
                        credits=row.credits,
                    )
                    for row in d.services
                ],
            )
            for d in definitions
        ]
    )

"""The staff-facing draft-bill review screen (#63): where #59's draft `ServiceBill`, #58's
discounts and #57's tax components first come together into one demoable screen. `billing.
view` (Staff Mode, no admin window) gates every route here — nothing on this screen needs
admin/owner approval yet, that is #64's job.

**Calls the existing pure functions, never reimplements their math.** One line's discounts
and tax are `billing/pricing.py::price_line`; an admin/owner override is spread over the lines
by `pricing.distribute_override`. This module's only job is resolving *which* discounts and
tax components apply to *this* bill's lines and gluing the results into one response.

**Tax per item (review R1/R2).** A line is taxed by the business-applicable components
(`province is None or province == business.province`, with a rate covering today) that its
own `Service.tax_component_keys` selects, in the service's own `tax_convention` (inclusive or
exclusive). Both are read live while the bill is a draft; issue freezes them.

**Discount application is bill-level, not line-level** (`billing/models.py::
ServiceBillDiscount`'s own docstring): staff picks a combination once, and it is intersected
with each line's own eligibility fresh on every read — so a line added later (a sibling
appointment completing after discounts were already applied) is considered automatically,
with no reapply step.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import date as Date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from auth.capabilities import Requires
from auth.models import User
from billing.commission import CommissionDiscountInput
from billing.discount_resolver import DiscountConflict, DiscountEligibility, is_eligible
from billing.models import (
    Discount,
    ServiceBill,
    ServiceBillDiscount,
    ServiceBillLine,
    TaxComponent,
)
from billing.pricing import OverrideConflict, PricedLine, distribute_override, price_line
from billing.tax import ComponentRate, LineTax, invoice_tax_totals, resolve_rate_bp
from core.access_log import LogAccessOf
from core.audit import record_event
from core.db import SessionDep
from core.models import Business
from customers.models import Customer
from scheduling.clock import today_in
from scheduling.models import Service, Staff

router = APIRouter(prefix="/bills", tags=["billing"])

BillViewer = Annotated[User, Depends(Requires("billing.view"))]


# --- what goes over the wire -----------------------------------------------------------------


class Ref(BaseModel):
    id: str
    name: str


class LineTaxOut(BaseModel):
    pretax_cents: int
    component_cents: dict[str, int]
    tax_cents: int
    total_cents: int


def tax_out(tax: LineTax) -> LineTaxOut:
    return LineTaxOut(
        pretax_cents=tax.pretax_cents,
        component_cents=tax.component_cents,
        tax_cents=tax.tax_cents,
        total_cents=tax.total_cents,
    )


class BillLineOut(BaseModel):
    id: str
    appointment_id: str
    service: Ref
    staff: Ref
    price_cents: int
    # The convention `price_cents`/`discounted_cents` are stated in (the service's own).
    tax_convention: str
    applied_discount_ids: list[str]
    # Cents each applied discount took off this line (id -> cents).
    discount_amounts: dict[str, int]
    discounted_cents: int
    tax: LineTaxOut
    line_total_cents: int
    # #72: a redeemed package credit's frozen session value; the line is already settled by
    # it (no discount, no second tax), so it is never a new amount to collect.
    prepaid_cents: int
    # What this line bills once an admin/owner override is spread across the bill (review
    # R5) — equal to `tax` when there is no override.
    billed: LineTaxOut


class DiscountChoiceOut(BaseModel):
    id: str
    name: str
    kind: str
    percentage_bp: int | None
    amount_cents: int | None
    stackable: bool
    applied: bool


class BillOut(BaseModel):
    id: str
    status: str
    customer: Ref
    booking_group_id: str | None
    created_at: datetime
    updated_at: datetime
    lines: list[BillLineOut]
    eligible_discounts: list[DiscountChoiceOut]
    # `subtotal - discount_total + tax_total == grand_total`, whatever mix of conventions.
    subtotal_cents: int
    discount_total_cents: int
    tax_totals_by_component: dict[str, int]
    tax_total_cents: int
    grand_total_cents: int
    # #72: the part of `grand_total_cents` package credits already settled.
    prepaid_total_cents: int
    # #64: an admin/owner-authorized exception (staff-request approval or inline admin edit),
    # reported alongside the ordinarily-computed total rather than replacing it. As entered —
    # a tax-inclusive total or a pre-tax amount per `override_tax_convention`.
    override_total_cents: int | None
    override_reason: str | None
    override_tax_convention: str | None
    # What the bill actually charges under the override: the lines' `billed` totals summed.
    override_grand_total_cents: int | None
    # The two business-setting toggles (#64), read fresh on every bill view so the screen can
    # decide whether to offer either override path without a second request.
    bill_override_requests_enabled: bool
    inline_admin_bill_edit_enabled: bool


class BillSummaryOut(BaseModel):
    id: str
    customer: Ref
    booking_group_id: str | None
    created_at: datetime
    line_count: int
    subtotal_cents: int


class ApplyDiscountsIn(BaseModel):
    discount_ids: list[uuid.UUID] = []


# --- loading -----------------------------------------------------------------------------------


async def business_or_404(db: SessionDep) -> Business:
    business = await db.scalar(select(Business).where(Business.id == 1))
    if business is None:
        raise HTTPException(status_code=404, detail="This instance has not been set up.")
    return business


async def load_bill(db: SessionDep, bill_id: uuid.UUID) -> ServiceBill:
    bill = await db.scalar(
        select(ServiceBill)
        .where(ServiceBill.id == bill_id)
        .execution_options(populate_existing=True)
    )
    if bill is None:
        raise HTTPException(status_code=404, detail="No such service bill.")
    return bill


async def applicable_components(
    db: SessionDep, business: Business, today: Date
) -> list[ComponentRate]:
    """Every active, business-applicable component with a rate covering `today` — the pool an
    item's own `tax_component_keys` selects from (`components_for`). A component with no rate
    covering today contributes nothing — there is no number to charge, not an error."""
    components = await db.scalars(
        select(TaxComponent).options(selectinload(TaxComponent.rates)).where(TaxComponent.active)
    )
    resolved: list[ComponentRate] = []
    for component in components:
        if not (component.province is None or component.province == business.province):
            continue
        history = [(r.effective_from, r.effective_to, r.rate_bp) for r in component.rates]
        rate_bp = resolve_rate_bp(history, today)
        if rate_bp is not None:
            resolved.append(ComponentRate(code=component.code, rate_bp=rate_bp))
    return resolved


def components_for(pool: list[ComponentRate], keys: list[str]) -> list[ComponentRate]:
    """The components one catalog item toggles on (review R1: "GST yes, PST no")."""
    wanted = set(keys)
    return [c for c in pool if c.code in wanted]


def _eligibility_of(discount: Discount) -> DiscountEligibility:
    return DiscountEligibility(
        applies_to_all=discount.eligibility_scope == "all",
        eligible_items=frozenset((i.item_type, i.item_id) for i in discount.eligible_items),
    )


def discount_input(discount: Discount) -> CommissionDiscountInput:
    return CommissionDiscountInput(
        id=discount.id,
        kind=discount.kind,
        stackable=discount.stackable,
        percentage_bp=discount.percentage_bp,
        amount_cents=discount.amount_cents,
        commission_basis=discount.commission_basis,
    )


def eligible_discounts(
    enabled: list[Discount], selected_ids: set[uuid.UUID], item_type: str, item_id: uuid.UUID
) -> list[Discount]:
    return [
        d
        for d in enabled
        if d.id in selected_ids and is_eligible(_eligibility_of(d), item_type, item_id)
    ]


class LineConflict(Exception):
    """A selected combination fails `resolve_stacked_discounts` on one specific line —
    surfaced verbatim (acceptance criterion: "shows the specific reason, not a silent
    no-op"), with the service name so staff knows which line it was about."""

    def __init__(self, service_name: str, reason: str) -> None:
        self.detail = f"{service_name}: {reason}"
        super().__init__(self.detail)


@dataclass
class PricedBillLine:
    bill_line: ServiceBillLine
    service: Service | None
    discounts: list[Discount]
    components: list[ComponentRate]
    priced: PricedLine
    billed: LineTax  # == priced.tax unless an override is distributed


@dataclass
class PricedBill:
    lines: list[PricedBillLine]
    pool: list[ComponentRate]  # every applicable component with today's rate
    overridden: bool


async def price_bill(
    db: SessionDep, business: Business, bill: ServiceBill, *, selected_ids: set[uuid.UUID]
) -> PricedBill:
    """Every line priced, plus the override (if any) distributed — the one computation the
    review screen, the authority routes and issue all share. Raises `LineConflict`."""
    service_ids = {line.service_id for line in bill.lines}
    services = (
        {s.id: s for s in await db.scalars(select(Service).where(Service.id.in_(service_ids)))}
        if service_ids
        else {}
    )
    enabled = list(await db.scalars(select(Discount).where(Discount.enabled)))
    pool = await applicable_components(db, business, today_in(business.timezone))

    lines: list[PricedBillLine] = []
    for line in bill.lines:
        service = services.get(line.service_id)
        # #72: a prepaid line gets no discount (the purchase already set its price) and no
        # tax (the purchase invoice already carried it).
        prepaid = line.prepaid_cents > 0
        applied = (
            [] if prepaid else eligible_discounts(enabled, selected_ids, "service", line.service_id)
        )
        components = (
            [] if prepaid or service is None else components_for(pool, service.tax_component_keys)
        )
        convention = service.tax_convention if service is not None else "exclusive"
        try:
            priced = price_line(
                line.price_cents, convention, components, [discount_input(d) for d in applied]
            )
        except DiscountConflict as error:
            raise LineConflict(service.name if service else "This service", str(error)) from error
        lines.append(PricedBillLine(line, service, applied, components, priced, priced.tax))

    overridden = bill.manual_override_cents is not None
    if overridden:
        try:
            billed = distribute_override(
                bill.manual_override_cents,
                bill.override_tax_convention,
                [(p.priced.tax, p.components, p.bill_line.prepaid_cents > 0) for p in lines],
            )
        except OverrideConflict as error:
            raise LineConflict("Override", str(error)) from error
        for p, tax in zip(lines, billed, strict=True):
            p.billed = tax
    return PricedBill(lines=lines, pool=pool, overridden=overridden)


async def compute_bill(
    db: SessionDep, business: Business, bill: ServiceBill, *, selected_ids: set[uuid.UUID]
) -> BillOut:
    """The whole screen's numbers, for one candidate `selected_ids` combination — used both to
    render the currently-applied state (GET) and to validate/preview a new one before it is
    persisted (PUT), so the two never compute it two different ways."""
    priced_bill = await price_bill(db, business, bill, selected_ids=selected_ids)
    customer = await db.get(Customer, bill.customer_id)
    staff_ids = {line.staff_id for line in bill.lines}
    staff_by_id = (
        {s.id: s for s in await db.scalars(select(Staff).where(Staff.id.in_(staff_ids)))}
        if staff_ids
        else {}
    )

    line_outs: list[BillLineOut] = []
    touched_discount_ids: set[uuid.UUID] = set()
    for p in priced_bill.lines:
        line, priced = p.bill_line, p.priced
        member = staff_by_id.get(line.staff_id)
        touched_discount_ids.update(d.id for d in p.discounts)
        line_outs.append(
            BillLineOut(
                id=str(line.id),
                appointment_id=str(line.appointment_id),
                service=Ref(id=str(line.service_id), name=p.service.name if p.service else "—"),
                staff=Ref(id=str(line.staff_id), name=member.display_name if member else "—"),
                price_cents=line.price_cents,
                tax_convention=priced.convention,
                applied_discount_ids=sorted(str(d.id) for d in p.discounts),
                discount_amounts={str(k): v for k, v in priced.discount_amounts.items()},
                discounted_cents=priced.discounted_cents,
                tax=tax_out(priced.tax),
                line_total_cents=priced.tax.total_cents,
                prepaid_cents=line.prepaid_cents,
                billed=tax_out(p.billed),
            )
        )

    tax_totals = invoice_tax_totals([p.priced.tax for p in priced_bill.lines])
    tax_total_cents = sum(tax_totals.values())
    grand_total_cents = sum(o.line_total_cents for o in line_outs)
    discount_total_cents = sum(o.price_cents - o.discounted_cents for o in line_outs)

    # Every enabled discount eligible for at least one line, not just the ones selected —
    # the picker needs to offer what *could* be applied, not only what already is.
    enabled_discounts = list(await db.scalars(select(Discount).where(Discount.enabled)))
    eligible_any = [
        d
        for d in enabled_discounts
        if any(is_eligible(_eligibility_of(d), "service", line.service_id) for line in bill.lines)
    ]
    overridden = priced_bill.overridden

    return BillOut(
        id=str(bill.id),
        status=bill.status,
        customer=Ref(
            id=str(bill.customer_id),
            name=f"{customer.first_name} {customer.last_name}" if customer else "—",
        ),
        booking_group_id=str(bill.booking_group_id) if bill.booking_group_id else None,
        created_at=bill.created_at,
        updated_at=bill.updated_at,
        lines=line_outs,
        eligible_discounts=[
            DiscountChoiceOut(
                id=str(d.id),
                name=d.name,
                kind=d.kind,
                percentage_bp=d.percentage_bp,
                amount_cents=d.amount_cents,
                stackable=d.stackable,
                applied=d.id in touched_discount_ids,
            )
            for d in eligible_any
        ],
        subtotal_cents=grand_total_cents - tax_total_cents + discount_total_cents,
        discount_total_cents=discount_total_cents,
        tax_totals_by_component=tax_totals,
        tax_total_cents=tax_total_cents,
        grand_total_cents=grand_total_cents,
        prepaid_total_cents=sum(line.prepaid_cents for line in bill.lines),
        override_total_cents=bill.manual_override_cents,
        override_reason=bill.manual_override_reason,
        override_tax_convention=bill.override_tax_convention if overridden else None,
        override_grand_total_cents=(
            sum(p.billed.total_cents for p in priced_bill.lines) if overridden else None
        ),
        bill_override_requests_enabled=business.enable_bill_override_requests,
        inline_admin_bill_edit_enabled=business.enable_inline_admin_bill_edit,
    )


async def persisted_selection(db: SessionDep, bill_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await db.scalars(
        select(ServiceBillDiscount.discount_id).where(ServiceBillDiscount.bill_id == bill_id)
    )
    return set(rows)


# --- reading -----------------------------------------------------------------------------------


@router.get("")
async def list_draft_bills(_: BillViewer, db: SessionDep) -> dict[str, list[BillSummaryOut]]:
    bills = await db.scalars(
        select(ServiceBill).where(ServiceBill.status == "draft").order_by(ServiceBill.created_at)
    )
    bills = list(bills)
    names = (
        {
            c.id: f"{c.first_name} {c.last_name}"
            for c in await db.scalars(
                select(Customer).where(Customer.id.in_({b.customer_id for b in bills}))
            )
        }
        if bills
        else {}
    )
    return {
        "bills": [
            BillSummaryOut(
                id=str(b.id),
                customer=Ref(id=str(b.customer_id), name=names.get(b.customer_id, "—")),
                booking_group_id=str(b.booking_group_id) if b.booking_group_id else None,
                created_at=b.created_at,
                line_count=len(b.lines),
                subtotal_cents=sum(line.price_cents for line in b.lines),
            )
            for b in bills
        ]
    }


@router.get(
    "/{bill_id}",
    dependencies=[
        Depends(Requires("billing.view")),
        Depends(LogAccessOf("service_bill", "bill_id", ServiceBill.customer_id)),
    ],
)
async def get_bill(bill_id: uuid.UUID, _: BillViewer, db: SessionDep) -> BillOut:
    business = await business_or_404(db)
    bill = await load_bill(db, bill_id)
    selected_ids = await persisted_selection(db, bill_id)
    try:
        return await compute_bill(db, business, bill, selected_ids=selected_ids)
    except LineConflict as error:
        # A combination that was valid when applied but no longer is (a discount disabled, or
        # an eligibility set changed, since) — surfaced the same honest way a fresh apply
        # would refuse it, not hidden behind a 500.
        raise HTTPException(status_code=409, detail=error.detail) from error


# --- applying discounts --------------------------------------------------------------------


@router.put("/{bill_id}/discounts")
async def apply_discounts(
    bill_id: uuid.UUID, payload: ApplyDiscountsIn, actor: BillViewer, db: SessionDep
) -> BillOut:
    business = await business_or_404(db)
    bill = await load_bill(db, bill_id)

    selected_ids = set(payload.discount_ids)
    if selected_ids:
        known = set(
            await db.scalars(
                select(Discount.id).where(Discount.id.in_(selected_ids), Discount.enabled)
            )
        )
        unknown = selected_ids - known
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"No such enabled discount: {', '.join(sorted(str(i) for i in unknown))}",
            )

    # #64: this is "staff resumes ordinary billing on the same draft" — an admin-authorized
    # exception is a one-shot answer to a specific ask, not a standing rule, so picking up the
    # ordinary predefined-discount flow again clears it rather than leaving a stale override
    # silently shadowing whatever staff pick here next.
    bill.manual_override_cents = None
    bill.manual_override_reason = None
    # #65's stale-approval checkpoint clears with the override it belongs to — see
    # `billing/models.py`'s `## invoice issue (#65)` section.
    bill.override_applied_revision = None
    # Bumped explicitly: unlike `ServiceBill`'s own row, this route never issues an `UPDATE`
    # against it otherwise (only `service_bill_discounts` changes) — SQLAlchemy's `onupdate`
    # only fires when the mapped row itself is written. `bill_authority.py`'s stale-approval
    # guard pins exactly this column at request time, so a discount change staff make here
    # must be visible to that guard the same way a sibling appointment completing already is
    # (`billing/completion.py::record_draft_bill_line`).
    bill.updated_at = datetime.now(UTC)

    try:
        computed = await compute_bill(db, business, bill, selected_ids=selected_ids)
    except LineConflict as error:
        raise HTTPException(status_code=422, detail=error.detail) from error

    await db.execute(delete(ServiceBillDiscount).where(ServiceBillDiscount.bill_id == bill_id))
    db.add_all(
        [
            ServiceBillDiscount(bill_id=bill_id, discount_id=discount_id)
            for discount_id in selected_ids
        ]
    )
    await db.flush()

    record_event(
        db,
        "bill.discounts_applied",
        target_type="service_bill",
        target_id=str(bill_id),
        actor_user_id=actor.id,
        metadata={"discount_ids": sorted(str(i) for i in selected_ids)},
    )
    await db.commit()
    return computed

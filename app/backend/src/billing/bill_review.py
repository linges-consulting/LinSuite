"""The staff-facing draft-bill review screen (#63): where #59's draft `ServiceBill`, #58's
discounts and #57's tax components first come together into one demoable screen. `billing.
view` (Staff Mode, no admin window) gates every route here — nothing on this screen needs
admin/owner approval yet, that is #64's job.

**Calls the existing pure functions, never reimplements their math.** Discount stacking is
`discount_resolver.resolve_stacked_discounts`; tax is `tax.compute_line_tax`/
`invoice_tax_totals`/`resolve_rate_bp`. This module's only job is resolving *which* discounts
and tax components apply to *this* bill's lines and gluing the results into one response.

**Two scope decisions this ticket had to make that nothing upstream decided yet** (both
documented in `.superpowers/sdd/m4/progress.md`'s `#63 detail`):

1. **Which tax components apply to a line.** No catalog item selects its own components yet
   (`billing/models.py::TaxComponent`'s own docstring: "#63 is what actually selects a
   catalog item's applicable components and no selection endpoint exists yet"). V1: every
   `active`, business-applicable component (`province is None or province == business.
   province` — the same hint `tax_routes.py` already computes) applies to every service bill
   line. A future per-service selection narrows this without touching `billing/tax.py`.
2. **Tax convention.** No catalog item states inclusive/exclusive either. `Service.price_cents`
   is treated as the pre-tax "catalog default" `billing/tax.py`'s own docstring already calls
   exclusive pricing — so every line here is computed `"exclusive"`. A future per-service
   convention field would be read here instead of the constant.

**Discount application is bill-level, not line-level** (`billing/models.py::
ServiceBillDiscount`'s own docstring): staff picks a combination once, and it is intersected
with each line's own eligibility fresh on every read — so a line added later (a sibling
appointment completing after discounts were already applied) is considered automatically,
with no reapply step.
"""

import uuid
from datetime import UTC, datetime
from datetime import date as Date
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from auth.capabilities import Requires
from auth.models import User
from billing.discount_resolver import (
    DiscountConflict,
    DiscountEligibility,
    DiscountInput,
    is_eligible,
    resolve_stacked_discounts,
)
from billing.models import Discount, ServiceBill, ServiceBillDiscount, TaxComponent
from billing.tax import ComponentRate, compute_line_tax, invoice_tax_totals, resolve_rate_bp
from core.audit import record_event
from core.db import SessionDep
from core.models import Business
from customers.models import Customer
from scheduling.models import Service, Staff

router = APIRouter(prefix="/bills", tags=["billing"])

BillViewer = Annotated[User, Depends(Requires("billing.view"))]

TAX_CONVENTION = "exclusive"  # see the module docstring's scope decision 2.


# --- what goes over the wire -----------------------------------------------------------------


class Ref(BaseModel):
    id: str
    name: str


class LineTaxOut(BaseModel):
    pretax_cents: int
    component_cents: dict[str, int]
    tax_cents: int
    total_cents: int


class BillLineOut(BaseModel):
    id: str
    appointment_id: str
    service: Ref
    staff: Ref
    price_cents: int
    applied_discount_ids: list[str]
    discounted_cents: int
    tax: LineTaxOut
    line_total_cents: int


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
    subtotal_cents: int
    discount_total_cents: int
    tax_totals_by_component: dict[str, int]
    tax_total_cents: int
    grand_total_cents: int
    # #64: an admin/owner-authorized exception (staff-request approval or inline admin edit),
    # reported alongside the ordinarily-computed total rather than replacing it — see
    # `billing/models.py::ServiceBill`'s own "bill review authority" section for why this is a
    # separate field and not a second way to arrive at `grand_total_cents`.
    override_total_cents: int | None
    override_reason: str | None
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


async def _business(db: SessionDep) -> Business:
    business = await db.scalar(select(Business).where(Business.id == 1))
    if business is None:
        raise HTTPException(status_code=404, detail="This instance has not been set up.")
    return business


def _today_in(business: Business) -> Date:
    return datetime.now(ZoneInfo(business.timezone)).date()


async def _load_bill(db: SessionDep, bill_id: uuid.UUID) -> ServiceBill:
    bill = await db.scalar(
        select(ServiceBill)
        .where(ServiceBill.id == bill_id)
        .execution_options(populate_existing=True)
    )
    if bill is None:
        raise HTTPException(status_code=404, detail="No such service bill.")
    return bill


async def _applicable_components(
    db: SessionDep, business: Business, today: Date
) -> list[ComponentRate]:
    """Scope decision 1 (module docstring): every active, business-applicable component taxes
    every line. A component with no rate covering today contributes nothing — there is no
    number to charge, not an error."""
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


def _eligibility_of(discount: Discount) -> DiscountEligibility:
    return DiscountEligibility(
        applies_to_all=discount.eligibility_scope == "all",
        eligible_items=frozenset((i.item_type, i.item_id) for i in discount.eligible_items),
    )


class _LineConflict(Exception):
    """A selected combination fails `resolve_stacked_discounts` on one specific line —
    surfaced verbatim (acceptance criterion: "shows the specific reason, not a silent
    no-op"), with the service name so staff knows which line it was about."""

    def __init__(self, service_name: str, reason: str) -> None:
        self.detail = f"{service_name}: {reason}"
        super().__init__(self.detail)


async def _compute(
    db: SessionDep, business: Business, bill: ServiceBill, *, selected_ids: set[uuid.UUID]
) -> BillOut:
    """The whole screen's numbers, for one candidate `selected_ids` combination — used both to
    render the currently-applied state (GET) and to validate/preview a new one before it is
    persisted (PUT), so the two never compute it two different ways."""
    today = _today_in(business)
    customer = await db.get(Customer, bill.customer_id)

    service_ids = {line.service_id for line in bill.lines}
    staff_ids = {line.staff_id for line in bill.lines}
    services = (
        {s.id: s for s in await db.scalars(select(Service).where(Service.id.in_(service_ids)))}
        if service_ids
        else {}
    )
    staff_by_id = (
        {s.id: s for s in await db.scalars(select(Staff).where(Staff.id.in_(staff_ids)))}
        if staff_ids
        else {}
    )

    enabled_discounts = list(await db.scalars(select(Discount).where(Discount.enabled)))

    components = await _applicable_components(db, business, today)

    line_outs: list[BillLineOut] = []
    subtotal_cents = 0
    discount_total_cents = 0
    line_taxes = []
    touched_discount_ids: set[uuid.UUID] = set()

    for line in bill.lines:
        service = services.get(line.service_id)
        member = staff_by_id.get(line.staff_id)
        eligibility_checked = [
            d
            for d in enabled_discounts
            if d.id in selected_ids and is_eligible(_eligibility_of(d), "service", line.service_id)
        ]
        if eligibility_checked:
            inputs = [
                DiscountInput(
                    id=d.id,
                    kind=d.kind,
                    stackable=d.stackable,
                    percentage_bp=d.percentage_bp,
                    amount_cents=d.amount_cents,
                )
                for d in eligibility_checked
            ]
            try:
                discounted_cents = resolve_stacked_discounts(line.price_cents, inputs)
            except DiscountConflict as error:
                raise _LineConflict(
                    service.name if service else "This service", str(error)
                ) from error
            touched_discount_ids.update(d.id for d in eligibility_checked)
        else:
            discounted_cents = line.price_cents

        line_tax = compute_line_tax(discounted_cents, components, TAX_CONVENTION)
        line_taxes.append(line_tax)
        subtotal_cents += line.price_cents
        discount_total_cents += line.price_cents - discounted_cents

        line_outs.append(
            BillLineOut(
                id=str(line.id),
                appointment_id=str(line.appointment_id),
                service=Ref(id=str(line.service_id), name=service.name if service else "—"),
                staff=Ref(id=str(line.staff_id), name=member.display_name if member else "—"),
                price_cents=line.price_cents,
                applied_discount_ids=sorted(str(d.id) for d in eligibility_checked),
                discounted_cents=discounted_cents,
                tax=LineTaxOut(
                    pretax_cents=line_tax.pretax_cents,
                    component_cents=line_tax.component_cents,
                    tax_cents=line_tax.tax_cents,
                    total_cents=line_tax.total_cents,
                ),
                line_total_cents=line_tax.total_cents,
            )
        )

    tax_totals = invoice_tax_totals(line_taxes)
    tax_total_cents = sum(tax_totals.values())
    grand_total_cents = sum(o.line_total_cents for o in line_outs)

    # Every enabled discount eligible for at least one line, not just the ones selected —
    # the picker needs to offer what *could* be applied, not only what already is.
    eligible_any = [
        d
        for d in enabled_discounts
        if any(is_eligible(_eligibility_of(d), "service", line.service_id) for line in bill.lines)
    ]

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
        subtotal_cents=subtotal_cents,
        discount_total_cents=discount_total_cents,
        tax_totals_by_component=tax_totals,
        tax_total_cents=tax_total_cents,
        grand_total_cents=grand_total_cents,
        override_total_cents=bill.manual_override_cents,
        override_reason=bill.manual_override_reason,
        bill_override_requests_enabled=business.enable_bill_override_requests,
        inline_admin_bill_edit_enabled=business.enable_inline_admin_bill_edit,
    )


async def _persisted_selection(db: SessionDep, bill_id: uuid.UUID) -> set[uuid.UUID]:
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


@router.get("/{bill_id}")
async def get_bill(bill_id: uuid.UUID, _: BillViewer, db: SessionDep) -> BillOut:
    business = await _business(db)
    bill = await _load_bill(db, bill_id)
    selected_ids = await _persisted_selection(db, bill_id)
    try:
        return await _compute(db, business, bill, selected_ids=selected_ids)
    except _LineConflict as error:
        # A combination that was valid when applied but no longer is (a discount disabled, or
        # an eligibility set changed, since) — surfaced the same honest way a fresh apply
        # would refuse it, not hidden behind a 500.
        raise HTTPException(status_code=409, detail=error.detail) from error


# --- applying discounts --------------------------------------------------------------------


@router.put("/{bill_id}/discounts")
async def apply_discounts(
    bill_id: uuid.UUID, payload: ApplyDiscountsIn, actor: BillViewer, db: SessionDep
) -> BillOut:
    business = await _business(db)
    bill = await _load_bill(db, bill_id)

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
    # Bumped explicitly: unlike `ServiceBill`'s own row, this route never issues an `UPDATE`
    # against it otherwise (only `service_bill_discounts` changes) — SQLAlchemy's `onupdate`
    # only fires when the mapped row itself is written. `bill_authority.py`'s stale-approval
    # guard pins exactly this column at request time, so a discount change staff make here
    # must be visible to that guard the same way a sibling appointment completing already is
    # (`billing/completion.py::record_draft_bill_line`).
    bill.updated_at = datetime.now(UTC)

    try:
        computed = await _compute(db, business, bill, selected_ids=selected_ids)
    except _LineConflict as error:
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

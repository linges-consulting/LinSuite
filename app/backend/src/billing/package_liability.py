"""Unused-package liability report (#74; spec #54 story 68): every customer's unused, still
-spendable package/bundle credits, valued at the frozen purchase-time allocation.

`GET /admin/reports/package-liability?from=&to=&customer_id=` — `billing.manage`
(Administrator, Admin Mode). `from`/`to` bound the purchase date, inclusive, in the business's
timezone; both optional (omitted = every purchase). Liability is always as of today.

**Which credits count** is `redemption.py::spendable` — the same rule completion uses, so the
report can never show a credit staff couldn't redeem: activated (paid), no
`package_credit_voids` row (a standard refund or a cancel-credits exception voided it; a
goodwill refund that *kept* credits leaves no void row, so they still show even though the
purchase invoice is cancelled), and not past `expires_at`.

**Value** per purchased service = `allocated_price_cents` (frozen at purchase, #71) minus the
frozen `value_cents` of each redemption (#72) — never a live `Service`/`PackageDefinition`
price. Remaining = `credits_total` minus redemptions.
"""

import uuid
from datetime import date, datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from auth.capabilities import Requires
from auth.session import CurrentUser
from billing.models import PackageCreditRedemption, PackagePurchase, PackagePurchaseCredit
from billing.redemption import spendable
from core.access_log import LogAccessIfFiltered, log_each_named
from core.db import SessionDep
from core.forms import refuse
from customers.models import Customer
from scheduling.clock import localize, today_in
from scheduling.models import Service
from scheduling.time_off import business_zone

router = APIRouter(prefix="/admin/reports", tags=["billing"])


class LiabilityRowOut(BaseModel):
    customer_id: str
    customer_name: str
    package_purchase_id: str
    package_name: str
    purchased_at: datetime
    expires_at: date | None
    service_id: str
    service_name: str
    credits_total: int
    credits_redeemed: int
    credits_remaining: int
    unused_value_cents: int


class LiabilityCustomerOut(BaseModel):
    customer_id: str
    customer_name: str
    credits_remaining: int
    unused_value_cents: int


class LiabilityReportOut(BaseModel):
    rows: list[LiabilityRowOut]
    customers: list[LiabilityCustomerOut]
    total_unused_value_cents: int
    as_of: date
    from_: date | None = Field(serialization_alias="from")
    to: date | None
    timezone: str


@router.get(
    "/package-liability",
    dependencies=[
        Depends(Requires("billing.manage")),
        Depends(LogAccessIfFiltered("package_liability")),
    ],
)
async def package_liability_report(
    db: SessionDep,
    user: CurrentUser,
    request: Request,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
    customer_id: uuid.UUID | None = None,
) -> LiabilityReportOut:
    if from_ and to and to < from_:
        raise refuse("to", "The last day cannot come before the first.", where="query")
    zone = await business_zone(db)
    today = today_in(zone)

    used = (
        select(
            PackageCreditRedemption.package_purchase_id,
            PackageCreditRedemption.service_id,
            func.count().label("n"),
            func.sum(PackageCreditRedemption.value_cents).label("value"),
        )
        .group_by(PackageCreditRedemption.package_purchase_id, PackageCreditRedemption.service_id)
        .subquery()
    )
    redeemed = func.coalesce(used.c.n, 0)
    query = (
        select(
            PackagePurchase,
            PackagePurchaseCredit,
            Customer.first_name,
            Customer.last_name,
            Service.name,
            redeemed,
            PackagePurchaseCredit.allocated_price_cents - func.coalesce(used.c.value, 0),
        )
        .select_from(PackagePurchase)
        .join(PackagePurchaseCredit)
        .join(Customer, Customer.id == PackagePurchase.customer_id)
        .join(Service, Service.id == PackagePurchaseCredit.service_id)
        .outerjoin(
            used,
            (used.c.package_purchase_id == PackagePurchaseCredit.package_purchase_id)
            & (used.c.service_id == PackagePurchaseCredit.service_id),
        )
        .where(*spendable(today), PackagePurchaseCredit.credits_total > redeemed)
        .order_by(
            Customer.last_name, Customer.first_name, Customer.id, PackagePurchase.purchased_at
        )
    )
    if from_:
        query = query.where(
            PackagePurchase.purchased_at >= localize(datetime.combine(from_, time.min), zone)
        )
    if to:
        end = localize(datetime.combine(to + timedelta(days=1), time.min), zone)
        query = query.where(PackagePurchase.purchased_at < end)
    if customer_id:
        query = query.where(PackagePurchase.customer_id == customer_id)

    rows = [
        LiabilityRowOut(
            customer_id=str(purchase.customer_id),
            customer_name=f"{first} {last}",
            package_purchase_id=str(purchase.id),
            package_name=purchase.name,
            purchased_at=purchase.purchased_at,
            expires_at=purchase.expires_at,
            service_id=str(credit.service_id),
            service_name=service_name,
            credits_total=credit.credits_total,
            credits_redeemed=n,
            credits_remaining=credit.credits_total - n,
            unused_value_cents=value,
        )
        for purchase, credit, first, last, service_name, n, value in await db.execute(query)
    ]
    customers: dict[str, LiabilityCustomerOut] = {}
    for r in rows:
        c = customers.setdefault(
            r.customer_id,
            LiabilityCustomerOut(
                customer_id=r.customer_id,
                customer_name=r.customer_name,
                credits_remaining=0,
                unused_value_cents=0,
            ),
        )
        c.credits_remaining += r.credits_remaining
        c.unused_value_cents += r.unused_value_cents
    if customer_id is None:
        # Unfiltered, the report still names every client with credits left: one audited read
        # per client shown (owner decision). Filtered, `LogAccessIfFiltered` already logged it.
        await log_each_named(
            db, user, request, [uuid.UUID(c) for c in customers], "package_liability"
        )
    return LiabilityReportOut(
        rows=rows,
        customers=list(customers.values()),
        total_unused_value_cents=sum(r.unused_value_cents for r in rows),
        as_of=today,
        from_=from_,
        to=to,
        timezone=zone.key,
    )

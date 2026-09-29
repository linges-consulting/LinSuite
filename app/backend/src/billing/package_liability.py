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

**The CSV export (#88; delivers #81), through the shared export mechanism (#86)**: `POST
.../package-liability/exports` records the request and queues `core.exports.build_report_export`
after commit; `GET .../exports/{id}` polls it and `GET .../exports/{id}/csv` downloads it once
`ready` (410 past `expires_at`) — same shape as `billing/commission_report.py`, behind the same
`billing.manage` capability as the on-screen report above.

**Access logging follows the on-screen report's own rule, at build time.** The worker has no
request to log against, so the endpoint captures the requester's role and IP — exactly what
`core.access_log` records for every row — into `params["_access_context"]` at request time;
the builder reads it back and writes one row per client the export names, attributed to the
requester, the same as `log_each_named` does for the on-screen report. That params blob is
audited verbatim by `report.export_requested` (`core/exports.py::request_export`); the IP it
carries is no more sensitive than what the access log itself already stores per row.
"""

import csv
import io
import uuid
from datetime import date, datetime, time, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.capabilities import Requires
from auth.models import User
from auth.session import CurrentUser
from billing.models import PackageCreditRedemption, PackagePurchase, PackagePurchaseCredit
from billing.redemption import spendable
from core.access_log import LogAccessIfFiltered, log_each_named, log_each_named_as
from core.db import SessionDep
from core.exports import build_report_export, is_expired, register_builder, request_export
from core.forms import refuse
from core.models import ReportExport
from customers.models import Customer
from scheduling.clock import localize, today_in
from scheduling.models import Service
from scheduling.time_off import business_zone

router = APIRouter(prefix="/admin/reports", tags=["billing"])
LiabilityViewer = Annotated[User, Depends(Requires("billing.manage"))]


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


class LiabilityExportIn(BaseModel):
    from_: date | None = Field(default=None, alias="from")
    to: date | None = None
    customer_id: uuid.UUID | None = None


class LiabilityExportOut(BaseModel):
    id: str
    status: str
    from_: date | None = Field(serialization_alias="from")
    to: date | None
    customer_id: str | None
    created_at: datetime
    completed_at: datetime | None
    download_url: str | None


def _window(from_: date | None, to: date | None) -> None:
    if from_ and to and to < from_:
        raise refuse("to", "The last day cannot come before the first.", where="query")


async def _report(
    db: SessionDep,
    from_: date | None,
    to: date | None,
    customer_id: uuid.UUID | None,
    zone: ZoneInfo,
    today: date,
) -> LiabilityReportOut:
    """The report's own query and totals — no access logging, which its two callers (the
    on-screen route, and the CSV builder below) each do in their own way."""
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
    return LiabilityReportOut(
        rows=rows,
        customers=list(customers.values()),
        total_unused_value_cents=sum(r.unused_value_cents for r in rows),
        as_of=today,
        from_=from_,
        to=to,
        timezone=zone.key,
    )


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
    _window(from_, to)
    zone = await business_zone(db)
    report = await _report(db, from_, to, customer_id, zone, today_in(zone))
    if customer_id is None:
        # Unfiltered, the report still names every client with credits left: one audited read
        # per client shown (owner decision). Filtered, `LogAccessIfFiltered` already logged it.
        named = [uuid.UUID(c.customer_id) for c in report.customers]
        await log_each_named(db, user, request, named, "package_liability")
    return report


# --- CSV export: a Celery job, through the shared export mechanism (#86, #88) ------------------

CSV_COLUMNS = tuple(LiabilityRowOut.model_fields)


def _csv(rows: list[LiabilityRowOut]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        values = row.model_dump()
        values["purchased_at"] = row.purchased_at.isoformat()
        values["expires_at"] = row.expires_at.isoformat() if row.expires_at else ""
        writer.writerow(["" if values[c] is None else values[c] for c in CSV_COLUMNS])
    return buffer.getvalue()


def _liability_export_out(export: ReportExport) -> LiabilityExportOut:
    params = export.params
    return LiabilityExportOut(
        id=str(export.id),
        status=export.status,
        from_=date.fromisoformat(params["from_date"]) if params["from_date"] else None,
        to=date.fromisoformat(params["to_date"]) if params["to_date"] else None,
        customer_id=params["customer_id"],
        created_at=export.created_at,
        completed_at=export.completed_at,
        download_url=(
            f"/api/admin/reports/package-liability/exports/{export.id}/csv"
            if export.status == "ready"
            else None
        ),
    )


async def _build_liability_csv(db: AsyncSession, params: dict) -> str:
    """The registered builder for `kind="package_liability"` (`core/exports.py`'s one shared
    task calls this): `params` is exactly what `request_liability_export` staged, including
    `_access_context` — the requester's id, role and IP, captured at request time since the
    worker has no request of its own.

    Writes one access-log entry per client the export names, exactly as the on-screen report
    does: the one filtered customer if `customer_id` was given (even if they end up with no
    rows — `LogAccessIfFiltered`'s own rule), otherwise every client the report names."""
    zone = await business_zone(db)
    today = today_in(zone)
    from_ = date.fromisoformat(params["from_date"]) if params["from_date"] else None
    to = date.fromisoformat(params["to_date"]) if params["to_date"] else None
    customer_id = uuid.UUID(params["customer_id"]) if params["customer_id"] else None
    report = await _report(db, from_, to, customer_id, zone, today)

    ctx = params["_access_context"]
    named = (
        [customer_id]
        if customer_id is not None
        else [uuid.UUID(c.customer_id) for c in report.customers]
    )
    actor_id = uuid.UUID(ctx["actor_user_id"])
    await log_each_named_as(db, actor_id, ctx["actor_role"], ctx["ip"], named, "package_liability")
    return _csv(report.rows)


register_builder("package_liability", _build_liability_csv)


async def _load_liability_export(db: SessionDep, export_id: uuid.UUID) -> ReportExport:
    export = await db.get(ReportExport, export_id, populate_existing=True)
    if export is None or export.kind != "package_liability":
        raise HTTPException(status_code=404, detail="No such export.")
    return export


@router.post("/package-liability/exports", status_code=202)
async def request_liability_export(
    payload: LiabilityExportIn, actor: LiabilityViewer, request: Request, db: SessionDep
) -> LiabilityExportOut:
    _window(payload.from_, payload.to)
    params = {
        "from_date": payload.from_.isoformat() if payload.from_ else None,
        "to_date": payload.to.isoformat() if payload.to else None,
        "customer_id": str(payload.customer_id) if payload.customer_id else None,
        # The requester's role and IP, captured now because the worker that builds this
        # export has no request of its own to read them from — exactly what every row
        # `core.access_log` writes already carries, so nothing more sensitive is added here
        # even though `report.export_requested` audits `params` verbatim.
        "_access_context": {
            "actor_user_id": str(actor.id),
            "actor_role": actor.role.name,
            "ip": request.client.host if request.client else None,
        },
    }
    export = await request_export(
        db, kind="package_liability", params=params, requested_by=actor.id
    )
    await db.commit()
    # After commit, never before — the worker must find the row it was handed.
    build_report_export.delay(str(export.id))
    return _liability_export_out(await _load_liability_export(db, export.id))


@router.get("/package-liability/exports/{export_id}")
async def get_liability_export(
    export_id: uuid.UUID, _: LiabilityViewer, db: SessionDep
) -> LiabilityExportOut:
    return _liability_export_out(await _load_liability_export(db, export_id))


@router.get("/package-liability/exports/{export_id}/csv")
async def download_liability_export(
    export_id: uuid.UUID, _: LiabilityViewer, db: SessionDep
) -> Response:
    export = await _load_liability_export(db, export_id)
    if is_expired(export):
        raise HTTPException(status_code=410, detail="This export has expired.")
    if export.status != "ready" or export.content is None:
        raise HTTPException(status_code=409, detail=f"This export is {export.status}.")
    from_, to = export.params["from_date"] or "all", export.params["to_date"] or "all"
    filename = f"package_liability_{from_}_{to}.csv"
    return Response(
        content=export.content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

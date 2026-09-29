"""Admin-only commission report (#69, M4 spec #54 stories 97-100; M4 review T5): what every
staff member has earned in commission — service and retail separately — next to what the
underlying invoices have actually collected, filterable by date range and staff, exportable as
CSV.

**`commission.view`, Administrator-only, Admin Mode.** `audit.view`'s own shape (0022).

**Earned comes only from the frozen `commission_postings` ledger** — never recomputed from a
live staff rate. Earned rows are dated at issue, reversals at the day they happened (refund,
cancel, return), so a window shows adjustments in the period they occur.

**Received/pending come from the payment ledger (R25).** Per invoice, `billing/payments.py::
balances` gives the money collected (`received`, net of refunds and of #68 transfers, so a
carried payment is never new cash) and what is still owed (`pending`, including approved but
unpaid insurer money). A posting's `commission_received_cents` is its share of the invoice's
settled fraction — `(received + prepaid package value) / grand total`, half-up — and
`commission_pending_cents` is the rest, so the two always sum to `amount_cents`. An earning and
its reversal on the same invoice split identically, so they still net to zero. A cancelled
invoice bills nothing: its row status is `voided` and its money is what it still holds.

**The CSV export is a Celery job (R29), through the shared export mechanism (#86)**: `POST
.../commission/exports` records the request and queues `core.exports.build_report_export`
after commit; `GET .../exports/{id}` polls it and `GET .../exports/{id}/csv` downloads it
once `ready` (410 past `expires_at`). Nothing is built inside the request. The export row
itself is `core.models.ReportExport`, `kind="commission"` — `core/exports.py`'s docstring is
the interface a new report kind registers against.
"""

import csv
import io
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.capabilities import Requires
from auth.models import User
from billing.models import (
    CommissionPosting,
    Invoice,
    InvoiceLine,
    RetailInvoice,
    RetailInvoiceLine,
)
from billing.payments import balances
from core.db import SessionDep
from core.exports import build_report_export, is_expired, register_builder, request_export
from core.forms import refuse
from core.models import ReportExport
from scheduling.clock import localize, today_in
from scheduling.models import Staff
from scheduling.time_off import business_zone

router = APIRouter(prefix="/admin/reports", tags=["billing"])
CommissionViewer = Annotated[User, Depends(Requires("commission.view"))]

DEFAULT_DAYS = 90
Source = Literal["service", "retail"]
PaymentStatus = Literal["received", "partial", "pending", "voided"]


# --- what goes over the wire -----------------------------------------------------------------


class CommissionRowOut(BaseModel):
    id: str
    posted_at: datetime
    kind: str
    source: Source
    staff_id: str
    staff_name: str
    invoice_id: str
    invoice_number: int
    invoice_status: str
    service_id: str | None
    variant_id: str | None
    commission_rate_bp: int
    basis_cents: int
    amount_cents: int
    payment_status: PaymentStatus
    # The invoice's own money, from the payment ledger (the same figures on every row of it).
    invoice_received_cents: int
    invoice_pending_cents: int
    # `amount_cents` split by the invoice's settled fraction; the two always sum to it.
    commission_received_cents: int
    commission_pending_cents: int


class SourceTotalsOut(BaseModel):
    # Net commission basis (earned minus reversed) — the pre-tax revenue commission is on.
    revenue_cents: int = 0
    commission_cents: int = 0
    commission_received_cents: int = 0
    commission_pending_cents: int = 0


class CommissionReportOut(BaseModel):
    rows: list[CommissionRowOut]
    service: SourceTotalsOut
    retail: SourceTotalsOut
    # Commission, both sources: net earned, and its received/pending split.
    total_earned_cents: int
    total_received_cents: int
    total_pending_cents: int
    # Payments, once per invoice in the window — separate from commission (spec §158).
    payments_received_cents: int
    payments_pending_cents: int
    from_: date = Field(serialization_alias="from")
    to: date
    timezone: str


class CommissionExportIn(BaseModel):
    from_: date | None = Field(default=None, alias="from")
    to: date | None = None
    staff_id: uuid.UUID | None = None


class CommissionExportOut(BaseModel):
    id: str
    status: str
    from_: date = Field(serialization_alias="from")
    to: date
    staff_id: str | None
    created_at: datetime
    completed_at: datetime | None
    download_url: str | None


# --- the arithmetic (S2) -------------------------------------------------------------------------


def received_share_cents(amount_cents: int, settled_cents: int, total_cents: int) -> int:
    """`amount_cents` times the invoice's settled fraction (clamped to 0..1), half-up away from
    zero — symmetric, so an earning and its reversal on one invoice still net to zero."""
    if total_cents <= 0 or settled_cents >= total_cents:
        return amount_cents
    if settled_cents <= 0:
        return 0
    share = Decimal(amount_cents) * settled_cents / total_cents
    return int(share.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class _Money:
    status: str
    received: int
    pending: int
    settled: int
    total: int


async def _money(db: AsyncSession, service_ids: set, retail_ids: set) -> dict[uuid.UUID, _Money]:
    invoices: list[Invoice | RetailInvoice] = []
    if service_ids:
        invoices += list(await db.scalars(select(Invoice).where(Invoice.id.in_(service_ids))))
    if retail_ids:
        invoices += list(
            await db.scalars(select(RetailInvoice).where(RetailInvoice.id.in_(retail_ids)))
        )
    by_id = await balances(db, invoices)
    out = {}
    for invoice in invoices:
        b = by_id[invoice.id]
        if invoice.status == "issued":
            received = invoice.grand_total_cents - b.prepaid_cents - b.outstanding_cents
            pending = max(b.outstanding_cents, 0)
        else:
            received, pending = b.held_credit_cents, 0
        out[invoice.id] = _Money(
            status=invoice.status,
            received=received,
            pending=pending,
            settled=received + b.prepaid_cents,
            total=invoice.grand_total_cents,
        )
    return out


def _payment_status(money: _Money) -> PaymentStatus:
    if money.status != "issued":
        return "voided"
    full = received_share_cents(10_000, money.settled, money.total)
    return "received" if full == 10_000 else "pending" if full == 0 else "partial"


# --- the report ----------------------------------------------------------------------------------


async def _window(
    db: AsyncSession, from_: date | None, to: date | None
) -> tuple[date, date, ZoneInfo]:
    zone = await business_zone(db)
    to = to or today_in(zone)
    from_ = from_ or to - timedelta(days=DEFAULT_DAYS)
    if to < from_:
        raise refuse("to", "The last day cannot come before the first.", where="query")
    return from_, to, zone


async def _report(
    db: AsyncSession, from_: date, to: date, zone: ZoneInfo, staff_id: uuid.UUID | None
) -> CommissionReportOut:
    start = localize(datetime.combine(from_, time.min), zone)
    end = localize(datetime.combine(to + timedelta(days=1), time.min), zone)
    conditions = [CommissionPosting.posted_at >= start, CommissionPosting.posted_at < end]
    if staff_id is not None:
        conditions.append(CommissionPosting.staff_id == staff_id)
    result = await db.execute(
        select(
            CommissionPosting,
            Staff.display_name,
            Invoice.invoice_number,
            InvoiceLine.service_id,
            RetailInvoice.invoice_number,
            RetailInvoiceLine.variant_id,
        )
        .join(Staff, Staff.id == CommissionPosting.staff_id)
        .outerjoin(Invoice, Invoice.id == CommissionPosting.invoice_id)
        .outerjoin(InvoiceLine, InvoiceLine.id == CommissionPosting.invoice_line_id)
        .outerjoin(RetailInvoice, RetailInvoice.id == CommissionPosting.retail_invoice_id)
        .outerjoin(
            RetailInvoiceLine, RetailInvoiceLine.id == CommissionPosting.retail_invoice_line_id
        )
        .where(*conditions)
        .order_by(CommissionPosting.posted_at.desc(), CommissionPosting.id)
    )
    fetched = result.all()
    money = await _money(
        db,
        {p.invoice_id for p, *_ in fetched if p.invoice_id},
        {p.retail_invoice_id for p, *_ in fetched if p.retail_invoice_id},
    )

    rows: list[CommissionRowOut] = []
    totals = {"service": SourceTotalsOut(), "retail": SourceTotalsOut()}
    for posting, staff_name, number, service_id, retail_number, variant_id in fetched:
        source: Source = "service" if posting.invoice_id else "retail"
        invoice_id = posting.invoice_id or posting.retail_invoice_id
        m = money[invoice_id]
        received = received_share_cents(posting.amount_cents, m.settled, m.total)
        row = CommissionRowOut(
            id=str(posting.id),
            posted_at=posting.posted_at,
            kind=posting.kind,
            source=source,
            staff_id=str(posting.staff_id),
            staff_name=staff_name,
            invoice_id=str(invoice_id),
            invoice_number=number if source == "service" else retail_number,
            invoice_status=m.status,
            service_id=str(service_id) if service_id else None,
            variant_id=str(variant_id) if variant_id else None,
            commission_rate_bp=posting.commission_rate_bp,
            basis_cents=posting.basis_cents,
            amount_cents=posting.amount_cents,
            payment_status=_payment_status(m),
            invoice_received_cents=m.received,
            invoice_pending_cents=m.pending,
            commission_received_cents=received,
            commission_pending_cents=posting.amount_cents - received,
        )
        rows.append(row)
        t = totals[source]
        t.revenue_cents += posting.basis_cents if posting.kind == "earned" else -posting.basis_cents
        t.commission_cents += row.amount_cents
        t.commission_received_cents += row.commission_received_cents
        t.commission_pending_cents += row.commission_pending_cents

    both = totals.values()
    return CommissionReportOut(
        rows=rows,
        service=totals["service"],
        retail=totals["retail"],
        # Signed ledger amounts: summing them is the window's net standing.
        total_earned_cents=sum(t.commission_cents for t in both),
        total_received_cents=sum(t.commission_received_cents for t in both),
        total_pending_cents=sum(t.commission_pending_cents for t in both),
        payments_received_cents=sum(m.received for m in money.values()),
        payments_pending_cents=sum(m.pending for m in money.values()),
        from_=from_,
        to=to,
        timezone=zone.key,
    )


@router.get("/commission", dependencies=[Depends(Requires("commission.view"))])
async def commission_report(
    db: SessionDep,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
    staff_id: uuid.UUID | None = None,
) -> CommissionReportOut:
    resolved_from, resolved_to, zone = await _window(db, from_, to)
    return await _report(db, resolved_from, resolved_to, zone, staff_id)


# --- CSV export: a Celery job, never built in the request (R29) ---------------------------------

CSV_COLUMNS = tuple(name for name in CommissionRowOut.model_fields if name != "id")


def _csv(rows: list[CommissionRowOut]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        values = row.model_dump()
        values["posted_at"] = row.posted_at.isoformat()
        writer.writerow(["" if values[c] is None else values[c] for c in CSV_COLUMNS])
    return buffer.getvalue()


def _export_out(export: ReportExport) -> CommissionExportOut:
    return CommissionExportOut(
        id=str(export.id),
        status=export.status,
        from_=date.fromisoformat(export.params["from_date"]),
        to=date.fromisoformat(export.params["to_date"]),
        staff_id=export.params["staff_id"],
        created_at=export.created_at,
        completed_at=export.completed_at,
        download_url=(
            f"/api/admin/reports/commission/exports/{export.id}/csv"
            if export.status == "ready"
            else None
        ),
    )


async def _build_commission_csv(db: AsyncSession, params: dict) -> str:
    """The registered builder for `kind="commission"` (`core/exports.py`'s one shared task
    calls this): `params` is exactly what `request_commission_export` staged."""
    zone = await business_zone(db)
    from_ = date.fromisoformat(params["from_date"])
    to = date.fromisoformat(params["to_date"])
    staff_id = uuid.UUID(params["staff_id"]) if params["staff_id"] else None
    report = await _report(db, from_, to, zone, staff_id)
    return _csv(report.rows)


register_builder("commission", _build_commission_csv)


async def _load_export(db: SessionDep, export_id: uuid.UUID) -> ReportExport:
    export = await db.get(ReportExport, export_id, populate_existing=True)
    if export is None or export.kind != "commission":
        raise HTTPException(status_code=404, detail="No such export.")
    return export


@router.post("/commission/exports", status_code=202)
async def request_commission_export(
    payload: CommissionExportIn, actor: CommissionViewer, db: SessionDep
) -> CommissionExportOut:
    from_, to, _zone = await _window(db, payload.from_, payload.to)
    params = {
        "from_date": from_.isoformat(),
        "to_date": to.isoformat(),
        "staff_id": str(payload.staff_id) if payload.staff_id else None,
    }
    export = await request_export(db, kind="commission", params=params, requested_by=actor.id)
    await db.commit()
    # After commit, never before — the worker must find the row it was handed.
    build_report_export.delay(str(export.id))
    return _export_out(await _load_export(db, export.id))


@router.get("/commission/exports/{export_id}")
async def get_commission_export(
    export_id: uuid.UUID, _: CommissionViewer, db: SessionDep
) -> CommissionExportOut:
    return _export_out(await _load_export(db, export_id))


@router.get("/commission/exports/{export_id}/csv")
async def download_commission_export(
    export_id: uuid.UUID, _: CommissionViewer, db: SessionDep
) -> Response:
    export = await _load_export(db, export_id)
    if is_expired(export):
        raise HTTPException(status_code=410, detail="This export has expired.")
    if export.status != "ready" or export.content is None:
        raise HTTPException(status_code=409, detail=f"This export is {export.status}.")
    filename = f"commission_{export.params['from_date']}_{export.params['to_date']}.csv"
    return Response(
        content=export.content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

"""`/api/admin/customers/{customer_id}/access-log`: who opened this client's record (ADR-0002 §6).

**Behind `audit.view`, an Admin Mode capability.** Who looked at whom is sensitive in its own
right, so a Staff Mode session is refused even when its role holds the capability.

**Not itself an access, so it does not log** (pre-flight §8). It returns identifiers about the
chart — actor, role, resource, IP, time — never the chart, and it lives under `/api/admin`,
outside the customer-scoped paths the route-enumeration test gates. Reading it writes nothing.

**Names are resolved at read time, and a row never disappears for want of one.** The access
log has no foreign keys (it outlives what it describes), so the actor is left-joined to
`users` and `staff`: the staff display name, else the account's email, else the bare id. The
role is the name snapshotted at the moment of access, never today's.

**The range is the partition key.** Business-local dates, `to` inclusive, default the last 90
days; the bounds are `occurred_at` comparisons so Postgres prunes to the partitions in them,
and `(customer_id, occurred_at)` is the index it reads. Newest first, `id` breaking ties so a
page boundary never duplicates or drops a row.

**The CSV export (#87), through the shared export mechanism (#86).** `POST
.../access-log/exports` records the request and queues `core.exports.build_report_export`
after commit; `GET .../exports/{id}` polls it and `GET .../exports/{id}/csv` downloads it
once `ready` (410 past `expires_at`) — the same request/poll/download shape
`billing/commission_report.py` established, and the same access gate as the on-screen report
above: `audit.view`, Admin Mode only. The builder (`_build_access_log_csv`) runs the same
`window_query` this module's on-screen report runs and resolves names the same way
(`_entry_out`), so the CSV's rows are exactly the on-screen report's rows for the same range,
newest first — never a second query with its own idea of the shape. Requesting an export is
not itself an access (matching the on-screen report) and writes no row here; it is
`request_export` that writes `report.export_requested`. `params` carries `"customer_id"`
(ADR-0001 rule 14's convention — an erasure deletes any export naming a client that way) and
the resolved `"from"`/`"to"` dates; `_load_export` below refuses to resolve an export of any
other `kind`, or one whose `params` names a different customer, as 404.
"""

import csv
import io
import uuid
from datetime import date, datetime, time, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.capabilities import Requires
from auth.models import User
from core.db import SessionDep
from core.exports import build_report_export, is_expired, register_builder, request_export
from core.models import AccessLogEntry, ReportExport
from scheduling._admin_forms import refuse
from scheduling.clock import localize
from scheduling.models import Staff
from scheduling.time_off import business_zone

router = APIRouter(prefix="/admin/customers", tags=["admin"])
AuditViewer = Annotated[User, Depends(Requires("audit.view"))]

DEFAULT_DAYS = 90
PAGE_SIZE, MAX_PAGE_SIZE = 50, 100


class AccessEntryOut(BaseModel):
    id: int
    occurred_at: datetime
    actor_user_id: str
    actor_name: str
    actor_role: str
    resource_type: str
    resource_id: str
    action: str
    ip: str | None


class AccessReportOut(BaseModel):
    entries: list[AccessEntryOut]
    total: int
    # The range actually applied, so a screen can show the defaults it did not send.
    from_: date = Field(serialization_alias="from")
    to: date
    # The business's zone, to print each instant on the clock it happened on.
    timezone: str


def _in_window(customer_id: uuid.UUID, start: datetime, end: datetime):
    return (
        AccessLogEntry.customer_id == customer_id,
        AccessLogEntry.occurred_at >= start,
        AccessLogEntry.occurred_at < end,
    )


def window_query(customer_id: uuid.UUID, start: datetime, end: datetime) -> Select:
    """Every row for one client in `[start, end)`, newest first, with the actor resolved."""
    return (
        select(AccessLogEntry, Staff.display_name, User.email)
        .outerjoin(User, User.id == AccessLogEntry.actor_user_id)
        .outerjoin(Staff, Staff.user_id == AccessLogEntry.actor_user_id)
        .where(*_in_window(customer_id, start, end))
        .order_by(AccessLogEntry.occurred_at.desc(), AccessLogEntry.id.desc())
    )


def _entry_out(entry: AccessLogEntry, name: str | None, email: str | None) -> AccessEntryOut:
    """One row's wire shape, name resolution included — shared by the on-screen report and
    the CSV export (#87) so the two never grow different ideas of "the same row"."""
    return AccessEntryOut(
        id=entry.id,
        occurred_at=entry.occurred_at,
        actor_user_id=str(entry.actor_user_id),
        actor_name=name or email or str(entry.actor_user_id),
        actor_role=entry.actor_role,
        resource_type=entry.resource_type,
        resource_id=entry.resource_id,
        action=entry.action,
        ip=str(entry.ip) if entry.ip is not None else None,
    )


async def _window(
    db: AsyncSession, from_: date | None, to: date | None
) -> tuple[date, date, ZoneInfo]:
    """Business-local dates and the zone they resolve in — the on-screen report's own
    defaulting and validation, shared with the export request so a range that is refused on
    screen is refused identically when exporting."""
    zone = await business_zone(db)
    to = to or datetime.now(zone).date()
    from_ = from_ or to - timedelta(days=DEFAULT_DAYS)
    if to < from_:
        raise refuse("to", "The last day cannot come before the first.", where="query")
    return from_, to, zone


@router.get(
    "/{customer_id}/access-log",
    dependencies=[Depends(Requires("audit.view"))],
)
async def read_access_log(
    customer_id: uuid.UUID,
    db: SessionDep,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = PAGE_SIZE,
) -> AccessReportOut:
    from_, to, zone = await _window(db, from_, to)
    start = localize(datetime.combine(from_, time.min), zone)
    end = localize(datetime.combine(to + timedelta(days=1), time.min), zone)

    total = await db.scalar(select(func.count()).where(*_in_window(customer_id, start, end)))
    rows = await db.execute(
        window_query(customer_id, start, end).offset((page - 1) * page_size).limit(page_size)
    )
    return AccessReportOut(
        entries=[_entry_out(entry, name, email) for entry, name, email in rows],
        total=total or 0,
        from_=from_,
        to=to,
        timezone=zone.key,
    )


# --- CSV export (#87): a Celery job through the shared mechanism, never built in the request --

CSV_COLUMNS = tuple(AccessEntryOut.model_fields)


def _csv(entries: list[AccessEntryOut]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for entry in entries:
        values = entry.model_dump()
        values["occurred_at"] = entry.occurred_at.isoformat()
        writer.writerow(["" if values[c] is None else values[c] for c in CSV_COLUMNS])
    return buffer.getvalue()


class AccessLogExportIn(BaseModel):
    from_: date | None = Field(default=None, alias="from")
    to: date | None = None


class AccessLogExportOut(BaseModel):
    id: str
    status: str
    from_: date = Field(serialization_alias="from")
    to: date
    created_at: datetime
    completed_at: datetime | None
    download_url: str | None


def _export_out(customer_id: uuid.UUID, export: ReportExport) -> AccessLogExportOut:
    return AccessLogExportOut(
        id=str(export.id),
        status=export.status,
        from_=date.fromisoformat(export.params["from"]),
        to=date.fromisoformat(export.params["to"]),
        created_at=export.created_at,
        completed_at=export.completed_at,
        download_url=(
            f"/api/admin/customers/{customer_id}/access-log/exports/{export.id}/csv"
            if export.status == "ready"
            else None
        ),
    )


async def _build_access_log_csv(db: AsyncSession, params: dict) -> str:
    """The registered builder for `kind="access_log"`: runs the exact query and name
    resolution `read_access_log` runs above, over the full range with no pagination — a CSV
    is the whole report, not one page of it."""
    customer_id = uuid.UUID(params["customer_id"])
    zone = await business_zone(db)
    from_ = date.fromisoformat(params["from"])
    to = date.fromisoformat(params["to"])
    start = localize(datetime.combine(from_, time.min), zone)
    end = localize(datetime.combine(to + timedelta(days=1), time.min), zone)
    rows = await db.execute(window_query(customer_id, start, end))
    return _csv([_entry_out(entry, name, email) for entry, name, email in rows])


register_builder("access_log", _build_access_log_csv)


async def _load_export(
    db: SessionDep, customer_id: uuid.UUID, export_id: uuid.UUID
) -> ReportExport:
    export = await db.get(ReportExport, export_id, populate_existing=True)
    if (
        export is None
        or export.kind != "access_log"
        or export.params.get("customer_id") != str(customer_id)
    ):
        raise HTTPException(status_code=404, detail="No such export.")
    return export


@router.post("/{customer_id}/access-log/exports", status_code=202)
async def request_access_log_export(
    customer_id: uuid.UUID,
    payload: AccessLogExportIn,
    actor: AuditViewer,
    db: SessionDep,
) -> AccessLogExportOut:
    from_, to, _timezone = await _window(db, payload.from_, payload.to)
    params = {"customer_id": str(customer_id), "from": from_.isoformat(), "to": to.isoformat()}
    export = await request_export(db, kind="access_log", params=params, requested_by=actor.id)
    await db.commit()
    # After commit, never before — the worker must find the row it was handed.
    build_report_export.delay(str(export.id))
    return _export_out(customer_id, await _load_export(db, customer_id, export.id))


@router.get("/{customer_id}/access-log/exports/{export_id}")
async def get_access_log_export(
    customer_id: uuid.UUID, export_id: uuid.UUID, _: AuditViewer, db: SessionDep
) -> AccessLogExportOut:
    return _export_out(customer_id, await _load_export(db, customer_id, export_id))


@router.get("/{customer_id}/access-log/exports/{export_id}/csv")
async def download_access_log_export(
    customer_id: uuid.UUID, export_id: uuid.UUID, _: AuditViewer, db: SessionDep
) -> Response:
    export = await _load_export(db, customer_id, export_id)
    if is_expired(export):
        raise HTTPException(status_code=410, detail="This export has expired.")
    if export.status != "ready" or export.content is None:
        raise HTTPException(status_code=409, detail=f"This export is {export.status}.")
    filename = f"access_log_{customer_id}_{export.params['from']}_{export.params['to']}.csv"
    return Response(
        content=export.content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

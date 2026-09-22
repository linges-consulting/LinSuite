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
"""

import uuid
from datetime import date, datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select

from auth.capabilities import Requires
from auth.models import User
from core.db import SessionDep
from core.models import AccessLogEntry
from scheduling._admin_forms import refuse
from scheduling.clock import localize
from scheduling.models import Staff
from scheduling.time_off import business_zone

router = APIRouter(prefix="/admin/customers", tags=["admin"])

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
    zone = await business_zone(db)
    to = to or datetime.now(zone).date()
    from_ = from_ or to - timedelta(days=DEFAULT_DAYS)
    if to < from_:
        raise refuse("to", "The last day cannot come before the first.", where="query")
    start = localize(datetime.combine(from_, time.min), zone)
    end = localize(datetime.combine(to + timedelta(days=1), time.min), zone)

    total = await db.scalar(select(func.count()).where(*_in_window(customer_id, start, end)))
    rows = await db.execute(
        window_query(customer_id, start, end).offset((page - 1) * page_size).limit(page_size)
    )
    return AccessReportOut(
        entries=[
            AccessEntryOut(
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
            for entry, name, email in rows
        ],
        total=total or 0,
        from_=from_,
        to=to,
        timezone=zone.key,
    )

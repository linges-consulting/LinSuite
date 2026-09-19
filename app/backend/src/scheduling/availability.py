"""The three availability inputs: the weekly matrix, time off, and the days the doors are shut.

This module only *stores* them. Task 14 is what intersects them into bookable slots, and
tech-stack §22 is what later lets a human book over a shift end or a vacation with the
override logged — none of that lives here.

**Working hours are replaced a week at a time.** `PUT /admin/staff/{id}/hours` takes the whole
matrix, because that is what the screen is: an administrator drags Tuesday's afternoon block
and saves, and a per-block API would let a half-applied week exist between two requests. The
exclusion constraint still guards each row, so the endpoint never has to trust its own
arithmetic.

**Nothing here writes a timezone onto a recurring rule.** A `working_hours` row is minutes
since local midnight and that is the whole of it (CLAUDE.md "Time"). `Business.timezone` is
read at *conversion* time by `scheduling/clock.py`, which is why changing it re-interprets the
week correctly instead of corrupting it.

**Time off is the opposite and is converted here.** A specific absence is an instant, entered
in local terms; this boundary is the one place that conversion happens, and the response
carries both halves so no screen re-derives one from the other.

**Who may do what.** Hours are `users.manage`, which the registry marks administrative.
Closures are `admin`. Time off is the exception: a staff member books and cancels **their
own** without any capability at all — it is their own calendar — and anyone else's needs
`users.manage` in Admin Mode. The alternative, making a therapist ask an administrator to
record a dentist appointment, is a rule that gets worked around by not recording it.
"""

import uuid
from datetime import UTC, datetime, time, timedelta
from datetime import date as Date
from typing import Annotated
from zoneinfo import ZoneInfo

import holidays
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from auth.capabilities import Requires
from auth.models import User
from auth.modes import require_admin_mode
from auth.session import ClaimsDep, CurrentUser
from core.audit import record_event
from core.db import SessionDep
from core.errors import CAPABILITY_REQUIRED, Forbidden
from core.models import Business
from scheduling._admin_forms import blank_to_none, refuse
from scheduling.models import MINUTE_STEP, MINUTES_IN_DAY, Closure, Staff, TimeOff, WorkingHours

router = APIRouter(tags=["availability"])

StaffManager = Annotated[User, Depends(Requires("users.manage"))]
Administrator = Annotated[User, Depends(Requires("admin"))]

# `holidays` only knows Canada here, and so does the address form (`country` is fixed at CA
# in `core/models.py`). The day a second country is supported, this reads that column.
COUNTRY = "CA"
Year = Annotated[int, Query(ge=2000, le=2100)]


async def _timezone(db: SessionDep) -> ZoneInfo:
    """The zone every local time on this API is read against."""
    return ZoneInfo(await db.scalar(select(Business.timezone).where(Business.id == 1)) or "UTC")


async def _staff(db: SessionDep, staff_id: uuid.UUID) -> Staff:
    staff = await db.get(Staff, staff_id)
    if staff is None:
        raise HTTPException(status_code=404, detail="No such staff member.")
    return staff


# --- the weekly matrix --------------------------------------------------------------------


class BlockIn(BaseModel):
    """One working block. ISO weekday (Monday = 0), minutes since local midnight."""

    weekday: Annotated[int, Field(ge=0, le=6)]
    start_minute: Annotated[int, Field(ge=0, le=MINUTES_IN_DAY)]
    # 1440 is a block that runs to midnight, which is why these are minutes and not `time`.
    end_minute: Annotated[int, Field(ge=0, le=MINUTES_IN_DAY)]

    @model_validator(mode="after")
    def _a_real_span(self) -> "BlockIn":
        if self.start_minute >= self.end_minute:
            raise ValueError("a block has to end after it starts")
        if self.start_minute % MINUTE_STEP or self.end_minute % MINUTE_STEP:
            raise ValueError(f"times go in {MINUTE_STEP}-minute steps")
        return self


class WeekIn(BaseModel):
    """The whole week. An empty list is somebody with no set hours, which is a real answer."""

    blocks: list[BlockIn] = []


class BlockOut(BaseModel):
    weekday: int
    start_minute: int
    end_minute: int


async def _week(db: SessionDep, staff_id: uuid.UUID) -> dict[str, list[BlockOut]]:
    rows = await db.scalars(
        select(WorkingHours)
        .where(WorkingHours.staff_id == staff_id)
        .order_by(WorkingHours.weekday, WorkingHours.start_minute)
    )
    return {
        "blocks": [
            BlockOut(weekday=r.weekday, start_minute=r.start_minute, end_minute=r.end_minute)
            for r in rows
        ]
    }


@router.get("/admin/staff/{staff_id}/hours")
async def read_hours(
    staff_id: uuid.UUID, _: StaffManager, db: SessionDep
) -> dict[str, list[BlockOut]]:
    await _staff(db, staff_id)
    return await _week(db, staff_id)


@router.put("/admin/staff/{staff_id}/hours")
async def replace_hours(
    staff_id: uuid.UUID, payload: WeekIn, admin: StaffManager, db: SessionDep
) -> dict[str, list[BlockOut]]:
    """The whole matrix, in one transaction. Nothing is half-saved.

    Overlap is left to `ex_working_hours_no_overlap` rather than checked in a loop here. One
    rule, in the one place that cannot be bypassed — and the frontend's own check before
    submit is a courtesy, not the enforcement.
    """
    staff = await _staff(db, staff_id)
    await db.execute(delete(WorkingHours).where(WorkingHours.staff_id == staff_id))
    db.add_all([WorkingHours(staff_id=staff_id, **block.model_dump()) for block in payload.blocks])
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Two blocks on the same day overlap. A break between them is the gap.",
        ) from None

    record_event(
        db,
        "staff.hours_replaced",
        target_type="staff",
        target_id=str(staff.id),
        actor_user_id=admin.id,
        # A count, not the times: the matrix is already stored, and copying it into the audit
        # log would be a second copy with a different retention horizon.
        metadata={"blocks": len(payload.blocks)},
    )
    await db.commit()
    return await _week(db, staff_id)


# --- time off and vacation ------------------------------------------------------------------


class TimeOffIn(BaseModel):
    """Either a run of whole local days, or a local datetime range. Never both.

    All-day is a *date* range and inclusive of its last day, because "away 1–5 July" means the
    fifth as well. The instant it becomes is midnight after the fifth, local.
    """

    all_day: bool = True
    start_date: Date | None = None
    end_date: Date | None = None
    # Local wall-clock, no offset — a `datetime-local` input is what sends these. An explicit
    # offset is refused rather than reinterpreted: silently treating `…Z` as local time is the
    # exact class of bug this ticket exists to prevent.
    starts_at_local: datetime | None = None
    ends_at_local: datetime | None = None
    reason: Annotated[str | None, Field(max_length=200)] = None

    @model_validator(mode="after")
    def _a_real_absence(self) -> "TimeOffIn":
        self.reason = blank_to_none(self.reason)
        if self.all_day:
            if self.start_date is None:
                raise ValueError("a date is needed")
            self.end_date = self.end_date or self.start_date
            if self.end_date < self.start_date:
                raise ValueError("the last day cannot come before the first")
            return self
        if self.starts_at_local is None or self.ends_at_local is None:
            raise ValueError("both a start and an end are needed")
        if self.starts_at_local.tzinfo or self.ends_at_local.tzinfo:
            raise ValueError("send local times without an offset")
        if self.starts_at_local >= self.ends_at_local:
            raise ValueError("an absence has to end after it starts")
        return self

    def instants(self, zone: ZoneInfo) -> tuple[datetime, datetime]:
        if self.all_day:
            midnight = datetime.combine(self.start_date, time.min)
            after = datetime.combine(self.end_date + timedelta(days=1), time.min)
            return midnight.replace(tzinfo=zone), after.replace(tzinfo=zone)
        return (
            self.starts_at_local.replace(tzinfo=zone),
            self.ends_at_local.replace(tzinfo=zone),
        )


class TimeOffOut(BaseModel):
    id: str
    all_day: bool
    reason: str | None
    # The instants, which is what is stored and what the scheduler will read.
    starts_at: str
    ends_at: str
    # The same span as a person entered it. Sent rather than derived in the browser so the
    # screen and the server never disagree about which local day an instant falls on.
    starts_at_local: str
    ends_at_local: str
    start_date: str
    end_date: str


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _entry(row: TimeOff, zone: ZoneInfo) -> TimeOffOut:
    starts_local = row.starts_at.astimezone(zone)
    ends_local = row.ends_at.astimezone(zone)
    # An all-day range is stored half-open — midnight *after* the last day — so the last day
    # somebody is away is one step back from the end.
    last_day = (ends_local - timedelta(days=1)).date() if row.all_day else ends_local.date()
    return TimeOffOut(
        id=str(row.id),
        all_day=row.all_day,
        reason=row.reason,
        starts_at=_utc(row.starts_at),
        ends_at=_utc(row.ends_at),
        starts_at_local=starts_local.replace(tzinfo=None).isoformat(),
        ends_at_local=ends_local.replace(tzinfo=None).isoformat(),
        start_date=starts_local.date().isoformat(),
        end_date=last_day.isoformat(),
    )


async def _own_or_managed(db: SessionDep, claims: dict, user: User, staff_id: uuid.UUID) -> Staff:
    """Their own calendar, or somebody with `users.manage` in a live Admin Mode window.

    The admin-mode check runs before the capability one, deliberately: told "your role does
    not allow this" when the truth is "your window lapsed", an administrator goes looking at a
    role they already hold instead of typing their password (`auth/capabilities.py`).
    """
    staff = await _staff(db, staff_id)
    if staff.user_id == user.id:
        return staff
    await require_admin_mode(claims, user)
    if "users.manage" not in user.capabilities:
        raise Forbidden(
            CAPABILITY_REQUIRED, "Your role does not allow booking time off for other people."
        )
    return staff


@router.get("/staff/{staff_id}/time-off")
async def list_time_off(
    staff_id: uuid.UUID, claims: ClaimsDep, user: CurrentUser, db: SessionDep
) -> dict[str, list[TimeOffOut]]:
    await _own_or_managed(db, claims, user, staff_id)
    zone = await _timezone(db)
    rows = await db.scalars(
        select(TimeOff).where(TimeOff.staff_id == staff_id).order_by(TimeOff.starts_at)
    )
    return {"time_off": [_entry(row, zone) for row in rows]}


@router.post("/staff/{staff_id}/time-off", status_code=201)
async def create_time_off(
    staff_id: uuid.UUID,
    payload: TimeOffIn,
    claims: ClaimsDep,
    user: CurrentUser,
    db: SessionDep,
) -> TimeOffOut:
    staff = await _own_or_managed(db, claims, user, staff_id)
    zone = await _timezone(db)
    starts_at, ends_at = payload.instants(zone)
    # Read before the flush that may fail: a rolled-back session expires every attribute on
    # the object, and reading one afterwards is IO this transaction no longer has.
    display_name, resolved_id = staff.display_name, staff.id

    entry = TimeOff(
        staff_id=resolved_id,
        starts_at=starts_at,
        ends_at=ends_at,
        all_day=payload.all_day,
        reason=payload.reason,
    )
    db.add(entry)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"{display_name} already has time off overlapping that.",
        ) from None

    record_event(
        db,
        "staff.time_off_created",
        target_type="staff",
        target_id=str(resolved_id),
        actor_user_id=user.id,
        # No reason: it is a colleague's private business, and the audit question is who
        # recorded an absence for whom, not why.
        metadata={"time_off_id": str(entry.id), "all_day": payload.all_day},
    )
    await db.commit()
    return _entry(entry, zone)


@router.delete("/staff/{staff_id}/time-off/{entry_id}", status_code=204)
async def delete_time_off(
    staff_id: uuid.UUID,
    entry_id: uuid.UUID,
    claims: ClaimsDep,
    user: CurrentUser,
    db: SessionDep,
) -> None:
    staff = await _own_or_managed(db, claims, user, staff_id)
    entry = await db.get(TimeOff, entry_id)
    if entry is None or entry.staff_id != staff.id:
        raise HTTPException(status_code=404, detail="No such time off.")

    await db.delete(entry)
    record_event(
        db,
        "staff.time_off_deleted",
        target_type="staff",
        target_id=str(staff.id),
        actor_user_id=user.id,
        metadata={"time_off_id": str(entry_id)},
    )
    await db.commit()


# --- the days the business is shut --------------------------------------------------------------


class ClosureIn(BaseModel):
    date: Date
    name: Annotated[str, Field(min_length=1, max_length=200)]

    @model_validator(mode="after")
    def _a_real_name(self) -> "ClosureIn":
        if not self.name.strip():
            raise ValueError("this cannot be blank")
        self.name = self.name.strip()
        return self


class ClosureOut(BaseModel):
    id: str
    date: str
    name: str
    source: str


def _closure(row: Closure) -> ClosureOut:
    return ClosureOut(id=str(row.id), date=row.date.isoformat(), name=row.name, source=row.source)


async def _closures(db: SessionDep, year: int | None) -> list[ClosureOut]:
    clauses = []
    if year is not None:
        clauses = [Closure.date >= Date(year, 1, 1), Closure.date <= Date(year, 12, 31)]
    rows = await db.scalars(select(Closure).where(*clauses).order_by(Closure.date))
    return [_closure(row) for row in rows]


@router.get("/admin/closures")
async def list_closures(
    _: Administrator, db: SessionDep, year: Year | None = None
) -> dict[str, list[ClosureOut]]:
    return {"closures": await _closures(db, year)}


@router.post("/admin/closures", status_code=201)
async def add_closure(payload: ClosureIn, admin: Administrator, db: SessionDep) -> ClosureOut:
    closure = Closure(date=payload.date, name=payload.name, source="manual")
    db.add(closure)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail=f"{payload.date.isoformat()} is already closed."
        ) from None

    record_event(
        db,
        "business.closure_added",
        target_type="closure",
        target_id=str(closure.id),
        actor_user_id=admin.id,
        metadata={"date": closure.date.isoformat(), "name": closure.name},
    )
    await db.commit()
    return _closure(closure)


@router.post("/admin/closures/import-statutory")
async def import_statutory(year: Year, admin: Administrator, db: SessionDep) -> dict[str, object]:
    """One year of this province's statutory holidays, from the `holidays` package.

    **Dates already present are skipped, never overwritten.** A business that added "Canada
    Day — closing at noon" by hand has said something the package does not know; an import
    that replaced it would silently undo a decision. And re-importing is therefore free, which
    is what makes the button safe to press twice.

    Nothing is computed at read time and there is no overrides table: a statutory day somebody
    deletes is simply a day this business works, until somebody imports that year again. That
    last clause is the honest cost of having no tombstones — one mechanism, one answer, and a
    screen that warns rather than a second table to keep in step.
    """
    province = await db.scalar(select(Business.province).where(Business.id == 1))
    if not province:
        raise refuse(
            "province",
            "Set the business's province on the Business tab first — statutory holidays "
            "differ by province.",
        )

    found = holidays.country_holidays(COUNTRY, subdiv=province, years=year)
    already = set(await db.scalars(select(Closure.date).where(*_year_clauses(year))))
    added = 0
    for day, name in sorted(found.items()):
        if day in already:
            continue
        db.add(Closure(date=day, name=name, source="statutory"))
        added += 1

    record_event(
        db,
        "business.closures_imported",
        target_type="business",
        target_id="1",
        actor_user_id=admin.id,
        metadata={"year": year, "count": added, "province": province},
    )
    await db.commit()
    return {
        "added": added,
        "skipped": len(found) - added,
        "closures": await _closures(db, year),
    }


def _year_clauses(year: int) -> list:
    return [Closure.date >= Date(year, 1, 1), Closure.date <= Date(year, 12, 31)]


@router.delete("/admin/closures/{closure_id}", status_code=204)
async def remove_closure(closure_id: uuid.UUID, admin: Administrator, db: SessionDep) -> None:
    closure = await db.get(Closure, closure_id)
    if closure is None:
        raise HTTPException(status_code=404, detail="No such closure.")

    date, name, source = closure.date.isoformat(), closure.name, closure.source
    await db.delete(closure)
    record_event(
        db,
        "business.closure_removed",
        target_type="closure",
        target_id=str(closure_id),
        actor_user_id=admin.id,
        metadata={"date": date, "name": name, "source": source},
    )
    await db.commit()

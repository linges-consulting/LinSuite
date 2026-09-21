"""Time off and vacation: the absences that override the weekly matrix (PRD §1).

**Instants, unlike working hours — and this is the one place the conversion happens.** A
specific absence is a moment in this business's life, not a rule about a clock face, so
`timestamptz` is right here where wall-clock is right there. The API accepts local dates
(all-day, inclusive of the last day) or local datetimes carrying **no offset**, and converts
with `Business.timezone` through `scheduling/clock.py`. A datetime that arrives with an offset
is refused rather than reinterpreted: silently reading `…Z` as a local time is the exact class
of bug this ticket exists to prevent.

Responses carry both halves — the instants the scheduler reads, and the local span a person
entered — so no screen has to work out which local day a UTC instant falls on.

**Who may do what.** A staff member books and cancels **their own** without any capability at
all; it is their own calendar. Anyone else's needs `users.manage` in Admin Mode. The
alternative — making a therapist ask an administrator to record a dentist appointment — is a
rule that gets worked around by not recording it, which costs the schedule more than the
permission saves.

Advisory at booking time, absolutely (tech-stack §22): a human with
`schedule.override_availability` may book over an absence, logged. That belongs to the booking
ticket; this module only records the fact.
"""

import uuid
from datetime import UTC, datetime, time, timedelta
from datetime import date as Date
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from auth.models import User
from auth.modes import require_admin_mode
from auth.session import ClaimsDep, CurrentUser
from core.audit import record_event
from core.db import SessionDep
from core.errors import CAPABILITY_REQUIRED, Forbidden
from core.models import Business
from scheduling import cache
from scheduling._admin_forms import blank_to_none
from scheduling.clock import localize
from scheduling.models import Staff, TimeOff
from scheduling.staff import load_staff

router = APIRouter(prefix="/staff", tags=["time off"])


async def business_zone(db: SessionDep) -> ZoneInfo:
    """The zone every local time on this API is read against."""
    return ZoneInfo(await db.scalar(select(Business.timezone).where(Business.id == 1)) or "UTC")


class TimeOffIn(BaseModel):
    """Either a run of whole local days, or a local datetime range. Never both.

    All-day is a *date* range and inclusive of its last day, because "away 1–5 July" means the
    fifth as well. The instant it becomes is midnight after the fifth, local.
    """

    all_day: bool = True
    start_date: Date | None = None
    end_date: Date | None = None
    # Local wall-clock, no offset — a `datetime-local` input is what sends these.
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
        """Through `clock.localize`, not a local `.replace(tzinfo=...)`: the gap and the
        repeated hour have to be resolved the same way here as they are for the weekly matrix,
        and one implementation is what guarantees that.

        An all-day range is half-open — midnight *after* the last day — which is also what
        makes it 23 or 25 hours of instants across a DST boundary, correctly and for free.
        """
        if self.all_day:
            return (
                localize(datetime.combine(self.start_date, time.min), zone),
                localize(datetime.combine(self.end_date + timedelta(days=1), time.min), zone),
            )
        return localize(self.starts_at_local, zone), localize(self.ends_at_local, zone)


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
    staff = await load_staff(db, staff_id)
    if staff.user_id == user.id:
        return staff
    await require_admin_mode(claims, user)
    if "users.manage" not in user.capabilities:
        raise Forbidden(
            CAPABILITY_REQUIRED, "Your role does not allow booking time off for other people."
        )
    return staff


@router.get("/{staff_id}/time-off")
async def list_time_off(
    staff_id: uuid.UUID, claims: ClaimsDep, user: CurrentUser, db: SessionDep
) -> dict[str, list[TimeOffOut]]:
    await _own_or_managed(db, claims, user, staff_id)
    zone = await business_zone(db)
    rows = await db.scalars(
        select(TimeOff).where(TimeOff.staff_id == staff_id).order_by(TimeOff.starts_at)
    )
    return {"time_off": [_entry(row, zone) for row in rows]}


@router.post("/{staff_id}/time-off", status_code=201)
async def create_time_off(
    staff_id: uuid.UUID,
    payload: TimeOffIn,
    claims: ClaimsDep,
    user: CurrentUser,
    db: SessionDep,
) -> TimeOffOut:
    staff = await _own_or_managed(db, claims, user, staff_id)
    zone = await business_zone(db)
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
    await cache.bump()
    return _entry(entry, zone)


@router.delete("/{staff_id}/time-off/{entry_id}", status_code=204)
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
    await cache.bump()

"""The weekly matrix: when somebody works, as a rule rather than as a moment.

**Nothing here writes a timezone onto a row, deliberately.** A `working_hours` row is minutes
since *local* midnight and that is the whole of it (CLAUDE.md "Time", PRD §1, tech-stack §19).
"Works Mondays 09:00–12:00" is a rule about a clock face; stored as a UTC instant it would
slide by an hour at every DST boundary. `Business.timezone` is read at *conversion* time by
`scheduling/clock.py`, which is why changing that setting re-interprets the week correctly
instead of corrupting it — there is nothing on the row to reinterpret.

**The week is replaced whole.** `PUT /admin/staff/{id}/hours` takes the entire matrix, because
that is what the screen is: an administrator drags Tuesday's afternoon block and saves. A
per-block API would let a half-applied week exist between two requests, and the one thing a
scheduling input must never be is partly true.

**Overlap is the database's job.** `ex_working_hours_no_overlap` is declared on the model and
created by migration 0011; this module turns the violation into a 409 and otherwise does not
check. The frontend's own check before submit is a courtesy, not the enforcement.

Everything is `users.manage`, which the registry marks administrative — so an Admin Mode
window is required on top of the capability, and `Requires` applies that without this module
mentioning it (the same shape as `scheduling/staff.py`).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from auth.capabilities import Requires
from auth.models import User
from core.audit import record_event
from core.db import SessionDep
from scheduling import cache
from scheduling.models import MINUTE_STEP, MINUTES_IN_DAY, WorkingHours
from scheduling.staff import load_staff

router = APIRouter(prefix="/admin/staff", tags=["working hours"])

StaffManager = Annotated[User, Depends(Requires("users.manage"))]


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


@router.get("/{staff_id}/hours")
async def read_hours(
    staff_id: uuid.UUID, _: StaffManager, db: SessionDep
) -> dict[str, list[BlockOut]]:
    await load_staff(db, staff_id)
    return await _week(db, staff_id)


@router.put("/{staff_id}/hours")
async def replace_hours(
    staff_id: uuid.UUID, payload: WeekIn, admin: StaffManager, db: SessionDep
) -> dict[str, list[BlockOut]]:
    """The whole matrix, in one transaction. Nothing is half-saved.

    The DELETE and the INSERTs share a transaction, so a refused week leaves the old one
    exactly where it was — which `test_hours.py` asserts row for row rather than trusting.
    """
    staff = await load_staff(db, staff_id)
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
    await cache.bump()
    return await _week(db, staff_id)

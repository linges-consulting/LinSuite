"""The days the business is shut — statutory holidays, and the ones it chose (PRD §1).

**One table, one row per date, and `source` is a label rather than a rule.** An imported
statutory holiday and a manually added staff retreat block bookings identically; `source` only
changes what an import is allowed to skip and what the screen calls the row.

**Nothing is computed at read time, and there is no overrides table.** The alternative —
deriving the holiday list on every availability query, with a second table naming the ones this
business ignores — is two mechanisms for one answer. Importing a year writes rows; deleting one
means the business works that day.

The honest cost of having no tombstones: re-importing a year adds back a statutory day somebody
deleted, because the import's only skip rule is "a row for this date already exists". The
screen warns before the delete rather than growing a second table to prevent it, and
`test_hours.py` pins the behaviour so nobody rediscovers it in production.

Everything is `admin`, which the registry marks administrative — so an Admin Mode window is
required on top of the capability.
"""

import uuid
from datetime import date as Date
from typing import Annotated

import holidays
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from auth.capabilities import Requires
from auth.models import User
from core.audit import record_event
from core.db import SessionDep
from core.models import Business
from scheduling import cache
from scheduling._admin_forms import refuse
from scheduling.models import Closure

router = APIRouter(prefix="/admin/closures", tags=["closures"])

Administrator = Annotated[User, Depends(Requires("admin"))]

# `holidays` only knows Canada here, and so does the address form (`country` is fixed at CA
# in `core/models.py`). The day a second country is supported, this reads that column.
COUNTRY = "CA"
Year = Annotated[int, Query(ge=2000, le=2100)]


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


def _year_clauses(year: int | None) -> list:
    """The one expression of "inside this calendar year", so the list and the import agree
    about which rows an import is allowed to consider already present."""
    if year is None:
        return []
    return [Closure.date >= Date(year, 1, 1), Closure.date <= Date(year, 12, 31)]


async def _closures(db: SessionDep, year: int | None) -> list[ClosureOut]:
    rows = await db.scalars(select(Closure).where(*_year_clauses(year)).order_by(Closure.date))
    return [_closure(row) for row in rows]


@router.get("")
async def list_closures(
    _: Administrator, db: SessionDep, year: Year | None = None
) -> dict[str, list[ClosureOut]]:
    return {"closures": await _closures(db, year)}


@router.post("", status_code=201)
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
    await cache.bump()
    return _closure(closure)


@router.post("/import-statutory")
async def import_statutory(year: Year, admin: Administrator, db: SessionDep) -> dict[str, object]:
    """One year of this province's statutory holidays, from the `holidays` package.

    **Dates already present are skipped, never overwritten.** A business that added "Canada
    Day — closing at noon" by hand has said something the package does not know; an import
    that replaced it would silently undo a decision. Re-importing is therefore free, which is
    what makes the button safe to press twice.
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
    await cache.bump()
    return {"added": added, "skipped": len(found) - added, "closures": await _closures(db, year)}


@router.delete("/{closure_id}", status_code=204)
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
    await cache.bump()

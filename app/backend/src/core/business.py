"""The administrative view of this deployment's own identity.

Here rather than in a domain because the `Business` record is here: it belongs to no single
domain and every domain reads it. The import of `auth.modes` runs the other way from the
usual direction — `core` is what domains import — but the mode guard has to live in `auth`,
and nothing in `auth` imports this module, so the graph stays acyclic.

Read-only for now. Task 9 turns this into the settings surface that also writes.
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from auth.modes import AdminUser
from core.db import SessionDep
from core.models import Business

router = APIRouter(prefix="/admin", tags=["admin"])


class BusinessOut(BaseModel):
    name: str
    timezone: str
    setup_completed_at: datetime | None


@router.get("/business")
async def read_business(_: AdminUser, db: SessionDep) -> BusinessOut:
    """Admin Mode only — `AdminUser` is the whole of the guard, and it answers 403 to a
    session being served in Staff Mode however administrative the account is."""
    business = await db.scalar(select(Business).where(Business.id == 1))
    if business is None:
        # Unreachable through the UI: an unclaimed instance has no accounts to sign in with.
        raise HTTPException(status_code=404, detail="This instance has not been set up.")
    return BusinessOut(
        name=business.name,
        timezone=business.timezone,
        setup_completed_at=business.setup_completed_at,
    )

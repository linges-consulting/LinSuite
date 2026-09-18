"""The administrative view of this deployment's own identity.

Here rather than in a domain because the `Business` record is here: it belongs to no single
domain and every domain reads it. The import of `auth.modes` runs the other way from the
usual direction — `core` is what domains import — but the mode guard has to live in `auth`,
and nothing in `auth` imports this module, so the graph stays acyclic.

Read-only until Task 8, which added the one thing here that writes: the two multi-factor
switches. They live beside the business rather than in `auth/` because they are settings of
this deployment, not of a session — the same reason `password_rotation_days` is on this row.
Task 9 turns the rest of this into the full settings surface.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from auth.capabilities import Requires
from auth.models import User
from core.audit import record_event
from core.db import SessionDep
from core.models import Business

AdminCapability = Annotated[User, Depends(Requires("admin"))]

router = APIRouter(prefix="/admin", tags=["admin"])


class BusinessOut(BaseModel):
    name: str
    timezone: str
    setup_completed_at: datetime | None


@router.get("/business", dependencies=[Depends(Requires("admin"))])
async def read_business(db: SessionDep) -> BusinessOut:
    """The `admin` capability, which the registry marks administrative — so this answers 403
    to a role that does not hold it *and* to a session being served in Staff Mode, however
    administrative the account is."""
    business = await db.scalar(select(Business).where(Business.id == 1))
    if business is None:
        # Unreachable through the UI: an unclaimed instance has no accounts to sign in with.
        raise HTTPException(status_code=404, detail="This instance has not been set up.")
    return BusinessOut(
        name=business.name,
        timezone=business.timezone,
        setup_completed_at=business.setup_completed_at,
    )


class SecurityPolicy(BaseModel):
    """The two multi-factor decisions a business gets to make (PRD §1, tech-stack §14).

    One model for reading and writing, because both fields are always sent: a PATCH that
    could carry one of them would need a tri-state per field to tell "leave it" from "turn
    it off", and this screen has two switches on it.
    """

    # On by default. Off is the solo operator with one phone, who is better served by a
    # strong password than by being locked out of their own business.
    mfa_required_for_admin: bool
    # Off by default, and the settings screen says why in as many words: an emailed code is
    # lower assurance, because the inbox usually lives in the same browser an attacker has.
    mfa_email_otp_allowed: bool


@router.get("/business/security", dependencies=[Depends(Requires("admin"))])
async def read_security(db: SessionDep) -> SecurityPolicy:
    business = await _business(db)
    return SecurityPolicy(
        mfa_required_for_admin=business.mfa_required_for_admin,
        mfa_email_otp_allowed=business.mfa_email_otp_allowed,
    )


@router.patch("/business/security")
async def update_security(
    payload: SecurityPolicy, admin: AdminCapability, db: SessionDep
) -> SecurityPolicy:
    """Turning either of these off weakens the instance, so both are audited with a name."""
    business = await _business(db)
    was = (business.mfa_required_for_admin, business.mfa_email_otp_allowed)
    business.mfa_required_for_admin = payload.mfa_required_for_admin
    business.mfa_email_otp_allowed = payload.mfa_email_otp_allowed
    record_event(
        db,
        "business.security_changed",
        target_type="business",
        target_id=str(business.id),
        actor_user_id=admin.id,
        metadata={
            "mfa_required_for_admin": [was[0], payload.mfa_required_for_admin],
            "mfa_email_otp_allowed": [was[1], payload.mfa_email_otp_allowed],
        },
    )
    await db.commit()
    return payload


async def _business(db: SessionDep) -> Business:
    business = await db.scalar(select(Business).where(Business.id == 1))
    if business is None:
        raise HTTPException(status_code=404, detail="This instance has not been set up.")
    return business

"""The accounts panel: who exists, what role they hold, and the early unlock (PRD §7).

`users.manage`, which the registry marks administrative — so an Admin Mode window is
required on top of the capability.

The unlock endpoint lived in `auth/throttle.py` until this task, next to the policy it
undoes. It moved here because it is an administrative screen's endpoint rather than part of
the throttle: `throttle.py` now holds only the functions that decide, and every route that
acts on an account is in one module. `throttle.unlock` is still the single implementation.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from auth import mfa, throttle
from auth.capabilities import Requires
from auth.models import Role, User
from auth.roles import assert_an_administrator_remains
from auth.session import revoke_all
from core.audit import record_event
from core.db import SessionDep
from core.redis import get_redis

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/users", tags=["admin"])

UsersManager = Annotated[User, Depends(Requires("users.manage"))]


class UserOut(BaseModel):
    id: str
    email: str
    role: str
    role_id: str
    # When the temporary lock lifts, or null for an account that is not locked. An instant
    # rather than "locked: true", because the only useful thing to tell somebody waiting is
    # when it ends — and the lock always ends (PRD §1: never permanent).
    locked_until: datetime | None


async def _locks(emails: list[str]) -> dict[str, datetime]:
    """The lock TTLs, in one round trip rather than one per row."""
    if not emails:
        return {}
    redis = get_redis()
    async with redis.pipeline(transaction=False) as pipe:
        for email in emails:
            pipe.ttl(throttle.lock_key(email))
        ttls = await pipe.execute()
    now = datetime.now(UTC)
    return {
        email: now + timedelta(seconds=ttl)
        for email, ttl in zip(emails, ttls, strict=True)
        if ttl and ttl > 0
    }


@router.get("")
async def list_users(_: UsersManager, db: SessionDep) -> dict[str, list[UserOut]]:
    users = list(await db.scalars(select(User).order_by(User.email)))
    locked = await _locks([u.email for u in users])
    return {
        "users": [
            UserOut(
                id=str(u.id),
                email=u.email,
                role=u.role.name,
                role_id=str(u.role_id),
                locked_until=locked.get(u.email),
            )
            for u in users
        ]
    }


class RoleAssignment(BaseModel):
    role_id: uuid.UUID


@router.patch("/{user_id}/role")
async def assign_role(
    user_id: uuid.UUID, payload: RoleAssignment, admin: UsersManager, db: SessionDep
) -> UserOut:
    """Move an account onto another role.

    The refusal that matters is the last one: moving the final administrator onto a role that
    cannot administer leaves nobody who can move them back. `assert_an_administrator_remains`
    asks that question of the state this change would produce, not the one it started from.
    """
    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="No such user.")
    role = await db.scalar(select(Role).where(Role.id == payload.role_id))
    if role is None:
        raise HTTPException(status_code=404, detail="No such role.")

    was = user.role.name
    user.role_id = role.id
    user.role = role
    await assert_an_administrator_remains(db)

    record_event(
        db,
        "user.role_assigned",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=admin.id,
        metadata={"email": user.email, "from": was, "to": role.name},
    )
    await db.commit()
    locked = await _locks([user.email])
    return UserOut(
        id=str(user.id),
        email=user.email,
        role=role.name,
        role_id=str(role.id),
        locked_until=locked.get(user.email),
    )


@router.post("/{user_id}/unlock", status_code=204)
async def unlock_account(user_id: uuid.UUID, admin: UsersManager, db: SessionDep) -> None:
    """Reopen an account before its lock expires.

    The offence tier goes with it. Leaving it would mean the next lockout of an account an
    administrator just forgave opened at the escalated tier, which is half a forgiveness.
    """
    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="No such user.")

    await throttle.unlock(user.email)
    record_event(
        db,
        "account.unlocked",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=admin.id,
        metadata={"email": user.email},
    )
    await db.commit()
    log.info("auth: %s unlocked the account %s", admin.email, user.email)


@router.post("/{user_id}/mfa/reset", status_code=204)
async def reset_mfa(user_id: uuid.UUID, admin: UsersManager, db: SessionDep) -> None:
    """The lost-phone-and-lost-codes path, and the only one that does not need the account.

    Everything goes: the secret, the method, and every recovery code live or spent — a set
    that outlived the enrolment it belonged to would be a way back in that nobody is
    tracking. `sessions_revoked_at` goes with it, because a session opened with the factor
    being removed must not survive the removal (tech-stack §14); that is the same column a
    password change uses, so there is one revocation mechanism rather than two.

    Not undoable and not gentle, which is why it is audited with the administrator's name
    and the owner is told: this is a protection being taken off somebody's account, and
    "which administrator, when" is the question asked afterwards.
    """
    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="No such user.")

    was = user.mfa_method
    user.mfa_secret = None
    user.mfa_method = None
    user.mfa_enrolled_at = None
    await mfa.forget_recovery_codes(db, user.id)
    revoke_all(user)
    record_event(
        db,
        "mfa.reset",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=admin.id,
        metadata={"email": user.email, "method": was},
    )
    await db.commit()
    mfa.notify(user.email, mfa.RESET_SUBJECT, mfa.RESET_MESSAGE)
    log.info("auth: %s reset the second factor on %s", admin.email, user.email)

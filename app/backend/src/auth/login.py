"""Password login, logout, and the endpoint that answers "who am I".

Every failure here answers the same way and takes the same time, so that neither the body
nor a stopwatch says whether an address has an account. The detail that *is* recorded —
which address was tried, and why it failed — goes to the audit log, where an administrator
can read it and an attacker cannot.
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select

from auth.models import User
from auth.session import (
    COOKIE_NAME,
    CurrentUser,
    clear_session_cookie,
    decode_token,
    is_revoked,
    issue_token,
    revoke,
    set_session_cookie,
)
from core.audit import record_event
from core.db import SessionDep
from core.security import verify_password

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# One answer for a wrong password and for an address nobody has registered.
_REFUSED = HTTPException(status_code=401, detail="Incorrect email or password")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    """What the frontend is told about the signed-in account. The hash is not in here, and
    must never be: this model is the boundary that guarantees it."""

    id: str
    email: str
    is_admin: bool

    @classmethod
    def of(cls, user: User) -> "UserOut":
        return cls(id=str(user.id), email=user.email, is_admin=user.is_admin)


@router.post("/login")
async def login(payload: LoginRequest, response: Response, db: SessionDep) -> UserOut:
    email = payload.email.lower()
    user = await db.scalar(select(User).where(func.lower(User.email) == email))

    # `None` verifies against a dummy hash: an unknown address costs the same milliseconds
    # as a known one, so the response time is not an account-enumeration oracle.
    if not await verify_password(user.password_hash if user else None, payload.password):
        record_event(
            db,
            "login.failed",
            target_type="user",
            target_id=str(user.id) if user else None,
            actor_user_id=user.id if user else None,
            metadata={"email": email, "reason": "bad_password" if user else "unknown_email"},
        )
        await db.commit()
        log.warning("auth: failed login for %s", email)
        raise _REFUSED

    issued = issue_token(user.id)
    set_session_cookie(response, issued)
    record_event(
        db,
        "login.succeeded",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=user.id,
        metadata={"email": email, "jti": issued.jti},
    )
    await db.commit()
    return UserOut.of(user)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, db: SessionDep) -> None:
    """Idempotent on purpose: "log me out" must succeed even from a session already gone."""
    token = request.cookies.get(COOKIE_NAME)
    claims = decode_token(token) if token else None
    if claims and not await is_revoked(claims["jti"]):
        await revoke(claims)
        record_event(
            db,
            "logout",
            target_type="user",
            target_id=claims["sub"],
            actor_user_id=uuid.UUID(claims["sub"]),
            metadata={"jti": claims["jti"]},
        )
        await db.commit()
    clear_session_cookie(response)


@router.get("/me")
async def me(user: CurrentUser) -> UserOut:
    """The protected endpoint. Anonymous, expired and logged-out requests all get a 401."""
    return UserOut.of(user)

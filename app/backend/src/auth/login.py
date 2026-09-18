"""Password login, logout, the endpoint that answers "who am I", and the mode switch.

Every failure here answers the same way and takes the same time, so that neither the body
nor a stopwatch says whether an address has an account. The detail that *is* recorded —
which address was tried, and why it failed — goes to the audit log, where an administrator
can read it and an attacker cannot.

All four endpoints live together because they are one thing: the lifecycle of a session.
The rule the mode switch adds to that lifecycle is in `auth/modes.py`; this module is only
its HTTP surface.
"""

import logging
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select

from auth import modes
from auth.models import User
from auth.session import (
    COOKIE_NAME,
    ClaimsDep,
    CurrentUser,
    UnrestrictedUser,
    change_required,
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
    """What the frontend is told about the signed-in account and the session serving it. The
    hash is not in here, and must never be: this model is the boundary that guarantees it.

    The mode fields are what the context switcher is drawn from — including the two instants
    behind the countdown, so the window running out is never a surprise. One shape for
    login, `/me` and `/mode`, so the frontend has one cache entry and one type.
    """

    id: str
    email: str
    is_admin: bool
    mode: str
    can_switch_modes: bool
    admin_grant_expires_at: datetime | None
    admin_hard_limit_at: datetime | None
    # True and the frontend routes to the change-password screen and nowhere else. The
    # session is real either way — this is a forced change, not a refused login, so the
    # account can still be told what it has to do.
    must_change_password: bool

    @classmethod
    def of(
        cls,
        user: User,
        state: modes.ModeState | None = None,
        *,
        must_change_password: bool = False,
    ) -> "UserOut":
        return cls(
            id=str(user.id),
            email=user.email,
            is_admin=user.is_admin,
            mode=state.mode if state else modes.STAFF_MODE,
            can_switch_modes=modes.can_switch_modes(user),
            admin_grant_expires_at=state.grant_expires_at if state else None,
            admin_hard_limit_at=state.hard_limit_at if state else None,
            must_change_password=must_change_password,
        )


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
    return UserOut.of(user, must_change_password=await change_required(db, user))


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, db: SessionDep) -> None:
    """Idempotent on purpose: "log me out" must succeed even from a session already gone."""
    token = request.cookies.get(COOKIE_NAME)
    claims = decode_token(token) if token else None
    if claims and not await is_revoked(claims["jti"]):
        await revoke(claims)
        await modes.forget(claims["jti"])
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
async def me(user: UnrestrictedUser, claims: ClaimsDep, db: SessionDep) -> UserOut:
    """The protected endpoint. Anonymous, expired and logged-out requests all get a 401.

    One of the three endpoints a session owing a password change may still reach, and the
    reason the other two are reachable: this is where the frontend learns it owes one.

    Reading the mode deliberately does not slide the admin window: the frontend polls this
    to keep the countdown honest, and a poll that counted as activity would hold an idle
    browser tab in Admin Mode indefinitely.
    """
    return UserOut.of(
        user,
        await modes.read_state(claims),
        must_change_password=await change_required(db, user),
    )


class ModeRequest(BaseModel):
    mode: Literal["staff", "admin"]
    # Required to *open* an admin window, and only then. Switching back and forth inside a
    # live window is free (PRD §1), and returning to Staff Mode never needs it.
    password: str | None = None


@router.post("/mode")
async def switch_mode(
    payload: ModeRequest, user: CurrentUser, claims: ClaimsDep, db: SessionDep
) -> UserOut:
    """Enter or leave Admin Mode.

    Refusals here are 403, never 401. The session is valid — it is the elevation that is
    being refused — and the frontend treats a 401 as "this session is gone, go to /login",
    which is precisely the wrong thing to do to someone who just mistyped a password.
    """
    if payload.mode == modes.ADMIN_MODE:
        if not modes.can_switch_modes(user):
            raise modes.ADMIN_MODE_REQUIRED
        if (await modes.read_state(claims)).grant_expires_at is None:
            await _reauthenticate(payload.password, user, claims, db)

    await modes.set_mode(claims, payload.mode)
    record_event(
        db,
        "mode.switched",
        target_type="session",
        target_id=claims["jti"],
        actor_user_id=user.id,
        metadata={"mode": payload.mode},
    )
    await db.commit()
    return UserOut.of(
        user,
        await modes.read_state(claims),
        must_change_password=await change_required(db, user),
    )


_REAUTH_REQUIRED = HTTPException(
    status_code=403, detail="Enter your password to switch to Admin Mode."
)


async def _reauthenticate(password: str | None, user: User, claims: dict, db: SessionDep) -> None:
    """No live window, so the password is the price of a new one.

    **A request carrying no password is not a failed authentication.** It is the expected
    race: the frontend offers the free switch from the grant it last saw, and the window can
    lapse between that poll and the click. Recording `login.failed` for it would put an
    accusation nobody earned into an append-only log, and hand the escalating lockout a count
    a user could trip by clicking once. It is refused before the hash is touched, which also
    stops an unthrottled endpoint burning an Argon2 verify on a request with no credential.

    There is no dummy-hash timing equalisation here either, deliberately. `/login` pads an
    unknown address so the response time cannot confirm an account exists; by this point the
    caller is authenticated and named, and there is nothing left to enumerate.
    """
    if password is None:
        raise _REAUTH_REQUIRED

    if not await verify_password(user.password_hash, password):
        record_event(
            db,
            "login.failed",
            target_type="user",
            target_id=str(user.id),
            actor_user_id=user.id,
            metadata={"email": user.email, "reason": "reauth"},
        )
        await db.commit()
        log.warning("auth: failed Admin Mode re-authentication for %s", user.email)
        raise HTTPException(status_code=403, detail="Incorrect password")

    # Audited, and committed, before the window opens. If Redis then refuses, the log says an
    # administrator re-authenticated and no window exists — which is the harmless way round.
    record_event(
        db,
        "admin.reauth",
        target_type="session",
        target_id=claims["jti"],
        actor_user_id=user.id,
        metadata={"email": user.email},
    )
    await db.commit()
    await modes.grant_admin(claims)

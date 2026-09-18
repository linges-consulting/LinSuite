"""Forgotten-password reset, and changing a password while signed in.

**Every session dies, not just the stolen one.** A reset exists because somebody may already
hold a credential they should not. Ending only the session that performed the reset would
leave a twelve-hour Staff token alive for exactly the attacker the reset was meant to evict,
so both endpoints here set `users.sessions_revoked_at` (`auth/session.py`) and every token
issued before that instant stops being a session.

**The request endpoint answers 202 whether or not the address has an account.** Status, body
and headers are identical — this is the one place in the product where "we sent you a link"
would otherwise be a membership oracle anybody can query. No password hashing happens on
either path, so there is no Argon2 asymmetry to pad; the known-address path does a little
more work (one INSERT, one enqueue), which is microseconds of Postgres and Redis against a
network round trip.

**Only the digest is stored.** The token itself exists in the message that was sent and
nowhere else, so a database dump is not a set of working reset links.
"""

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select, update

from auth import modes
from auth.login import UserOut
from auth.models import PasswordResetToken, User
from auth.session import (
    ClaimsDep,
    CurrentUser,
    issue_token,
    revoke,
    revoke_all,
    set_session_cookie,
)
from core.audit import record_event
from core.config import get_settings
from core.db import SessionDep
from core.security import check_password_policy, hash_password, verify_password
from notifications.tasks import send_email

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# One answer for an expired link, a spent link and a token nobody ever issued. Which of the
# three it was is in the audit log; the caller only needs to know to ask for another.
_BAD_TOKEN = HTTPException(
    status_code=400, detail="This reset link has expired or has already been used."
)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _refuse_password(reason: str) -> HTTPException:
    """Shaped like FastAPI's own 422 — and like `auth/setup.py`'s — so the frontend reads one
    error format, and carrying no trace of what was submitted."""
    return HTTPException(
        status_code=422,
        detail=[{"type": "value_error", "loc": ["body", "new_password"], "msg": reason}],
    )


# --- requesting a link --------------------------------------------------------------------


class ResetRequest(BaseModel):
    email: EmailStr


@router.post("/password-reset/request", status_code=202)
async def request_reset(payload: ResetRequest, db: SessionDep) -> dict[str, str]:
    email = payload.email.lower()
    user = await db.scalar(select(User).where(func.lower(User.email) == email))
    if user is None:
        # Nothing is recorded. An audit row naming an address with no account would store a
        # non-user's email — the trail is for administrators, not a list of who was typed in.
        log.info("auth: password reset requested for an address with no account")
        return _ACCEPTED

    token = secrets.token_urlsafe(32)
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=digest(token),
            expires_at=datetime.now(UTC) + timedelta(minutes=get_settings().password_reset_minutes),
        )
    )
    record_event(
        db,
        "password.reset_requested",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=user.id,
        metadata={"email": user.email},
    )
    await db.commit()

    # Enqueued after the commit, deliberately: a worker fast enough to beat the transaction
    # would send a link the confirm endpoint cannot find yet.
    send_email.delay(user.email, "Reset your LinSuite password", _reset_message(token))
    return _ACCEPTED


# The same object every time, so the known and unknown paths cannot drift apart in the body.
_ACCEPTED = {"status": "accepted"}


def _reset_message(token: str) -> str:
    settings = get_settings()
    return (
        "Someone asked to reset the password for your LinSuite account.\n\n"
        f"{settings.app_base_url}/reset-password?token={token}\n\n"
        f"The link works once and expires in {settings.password_reset_minutes} minutes. "
        "If this was not you, no action is needed — your password has not changed."
    )


# --- completing a reset -------------------------------------------------------------------


class ResetConfirm(BaseModel):
    token: str = Field(min_length=1)
    new_password: str


@router.post("/password-reset/confirm", status_code=204)
async def confirm_reset(payload: ResetConfirm, db: SessionDep) -> None:
    now = datetime.now(UTC)
    row = await db.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == digest(payload.token))
    )
    if row is None or row.used_at is not None or row.expires_at <= now:
        log.warning("auth: rejected a password reset with an invalid, spent or expired link")
        raise _BAD_TOKEN

    rejected = await check_password_policy(payload.new_password)
    if rejected:
        # The link is left live: refusing a weak password must not cost the user their one
        # chance to use it.
        raise _refuse_password(rejected)

    user = await db.get(User, row.user_id)
    if user is None:
        raise _BAD_TOKEN
    user.password_hash = await hash_password(payload.new_password)
    user.password_changed_at = now
    user.must_change_password = False
    revoke_all(user)

    # Every outstanding link for this account, not just the one presented: the others were
    # issued against a password that no longer exists, and the reset just revoked sessions
    # in the same breath. Leaving them usable would keep a door open behind the one we shut.
    await db.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))
        .values(used_at=now)
    )
    record_event(
        db,
        "password.reset_completed",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=user.id,
        metadata={"email": user.email},
    )
    await db.commit()
    log.info("auth: password reset completed for %s", user.email)


# --- changing a password while signed in ----------------------------------------------------


class ChangeRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/password/change")
async def change_password(
    payload: ChangeRequest,
    response: Response,
    user: CurrentUser,
    claims: ClaimsDep,
    db: SessionDep,
) -> UserOut:
    """Change it, kill every other session, and hand this one a replacement cookie.

    A 403 for a wrong current password, never a 401: the session is fine, and the frontend
    treats a 401 as "this session is gone, go to /login" — which is the wrong thing to do to
    someone who mistyped. The same rule as the Admin Mode re-authentication in `login.py`.
    """
    if not await verify_password(user.password_hash, payload.current_password):
        record_event(
            db,
            "login.failed",
            target_type="user",
            target_id=str(user.id),
            actor_user_id=user.id,
            metadata={"email": user.email, "reason": "password_change"},
        )
        await db.commit()
        log.warning("auth: failed password change for %s", user.email)
        raise HTTPException(status_code=403, detail="Incorrect password")

    rejected = await check_password_policy(payload.new_password)
    if rejected:
        raise _refuse_password(rejected)

    user.password_hash = await hash_password(payload.new_password)
    user.password_changed_at = datetime.now(UTC)
    user.must_change_password = False
    revoke_all(user)
    record_event(
        db,
        "password.changed",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=user.id,
        metadata={"email": user.email},
    )
    await db.commit()

    # The caller's own token is now older than the cut-off, so it is dead like the rest. It
    # is also denylisted by `jti` and its mode state dropped, because it is the one token we
    # can name and an Admin Mode window must not survive a credential change.
    await revoke(claims)
    await modes.forget(claims["jti"])

    # Issued after the commit, so its `iat` is later than the cut-off that was just written.
    issued = issue_token(user.id)
    set_session_cookie(response, issued)
    log.info("auth: password changed for %s", user.email)
    return UserOut.of(user, await modes.read_state(claims), must_change_password=False)

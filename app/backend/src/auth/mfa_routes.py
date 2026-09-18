"""The HTTP surface of the second factor: enrolling, verifying, and the recovery codes.

Separate from `auth/mfa.py` for one structural reason. The gate has to live inside
`CurrentUser` so no route can forget it, which means `auth/session.py` imports the rule —
so the rule cannot import `UnrestrictedUser` back. Everything here needs that dependency, so
here is where it lives.

**Which dependency each endpoint asks for is the security decision on this page.**

| Endpoint | Depends on | Reachable while |
|---|---|---|
| `POST /mfa/verify`, `/mfa/email-otp/request` | `UnrestrictedUser` | pending — that is the point |
| `POST /mfa/enrol*` | `EnrollingUser` | a change or an enrolment is owed, but not a code |
| `GET /mfa`, `POST /mfa/recovery-codes` | `CurrentUser` | nothing is owed |

`UnrestrictedUser` on the verify endpoints is deliberate and narrow: a session that owes a
code can reach exactly these, `GET /auth/me` and logout, and nothing else in the product.
"""

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from auth import mfa, modes, throttle
from auth.login import UserOut
from auth.models import User
from auth.session import (
    PASSWORD_CHANGE_REQUIRED,
    ClaimsDep,
    CurrentUser,
    UnrestrictedUser,
    change_required,
)
from core.audit import record_event
from core.db import SessionDep
from core.models import Business

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/mfa", tags=["auth"])


class CodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class RecoveryCodesOut(BaseModel):
    """Shown exactly once. Only digests are kept, so there is nothing to show a second time."""

    recovery_codes: list[str]


async def enrolling_user(user: UnrestrictedUser, claims: ClaimsDep, db: SessionDep) -> User:
    """The session allowed to set a second factor up.

    Not `CurrentUser`, because an account under the enrolment gate would be refused by the
    very rule enrolling is how you satisfy. Not `UnrestrictedUser` either: a forced password
    change still comes first, and a session that owes a code has to present it before it may
    change how the account is protected — otherwise a stolen password alone could enrol a
    new authenticator over the real one.
    """
    if await change_required(db, user):
        raise PASSWORD_CHANGE_REQUIRED
    await mfa.assert_verified(claims)
    return user


EnrollingUser = Annotated[User, Depends(enrolling_user)]


async def _out(claims: dict, db: SessionDep, user: User) -> UserOut:
    return UserOut.of(
        user,
        await modes.read_state(claims),
        must_change_password=await change_required(db, user),
        mfa=await mfa.snapshot(claims, db, user),
    )


# --- enrolling with an authenticator app ----------------------------------------------------


class EnrolmentOut(BaseModel):
    # The base32 secret, so somebody whose phone cannot scan a QR code can type it in. It
    # leaves the server exactly once, in this response, over the same TLS the session rides.
    secret: str
    # What the QR code encodes. Rendered in the browser rather than as an image from here:
    # an image endpoint would be a second place the secret is served from, cacheable by
    # anything between us and the screen.
    provisioning_uri: str


@router.post("/enrol")
async def start_enrolment(user: EnrollingUser, db: SessionDep) -> EnrolmentOut:
    """Mint a secret and hand back what an authenticator app needs to hold it.

    The candidate waits in Redis and the account is *not* enrolled: nothing on `users`
    changes until a code proves the app actually has the secret. Writing it to the column
    here would be worse than useless for somebody already enrolled — it would overwrite the
    secret their authenticator is using, so abandoning a second enrolment would leave them
    locked out of their own account by a QR code they never scanned.

    Called again, it replaces the candidate. That is the common case — the first code did
    not scan and the page was reloaded — not an attack.
    """
    secret = mfa.new_secret()
    await mfa.stage_secret(user, secret)
    business = await db.scalar(select(Business).where(Business.id == 1))
    return EnrolmentOut(
        secret=secret,
        provisioning_uri=mfa.provisioning_uri(
            secret, user.email, business.name if business else "LinSuite"
        ),
    )


@router.post("/enrol/confirm")
async def confirm_enrolment(
    payload: CodeRequest, user: EnrollingUser, claims: ClaimsDep, db: SessionDep
) -> RecoveryCodesOut:
    """A valid code is what makes the enrolment real, and what earns the recovery codes."""
    secret = await mfa.staged_secret(user)
    if secret is None or not mfa.check_totp(secret, payload.code):
        # Not counted against the lockout: this is somebody setting up their own account with
        # a code they can read off their own screen, and there is nothing here to guess at —
        # the secret was just handed to them.
        raise mfa.BAD_CODE
    user.mfa_secret = mfa.encrypt_secret(secret)
    return await _complete_enrolment(db, claims, user, mfa.TOTP)


# --- enrolling with emailed codes -------------------------------------------------------------


@router.post("/enrol/email", status_code=202)
async def start_email_enrolment(user: EnrollingUser, db: SessionDep) -> dict[str, str]:
    """The lower-assurance factor, for staff who will not install an authenticator app.

    Only where the business has said so. It is not a fallback here — it is the factor itself
    — so it is the business's decision to accept the weaker one, stated plainly in the
    settings screen rather than buried (tech-stack §14).
    """
    if not (await mfa.read_policy(db)).email_otp_allowed:
        raise mfa.EMAIL_OTP_NOT_ALLOWED
    await mfa.send_otp(db, user, purpose="enrolment")
    return {"status": "sent"}


@router.post("/enrol/email/confirm")
async def confirm_email_enrolment(
    payload: CodeRequest, user: EnrollingUser, claims: ClaimsDep, db: SessionDep
) -> RecoveryCodesOut:
    if not (await mfa.read_policy(db)).email_otp_allowed:
        raise mfa.EMAIL_OTP_NOT_ALLOWED
    if not await mfa.spend_otp(user, payload.code):
        raise mfa.BAD_CODE
    return await _complete_enrolment(db, claims, user, mfa.EMAIL)


async def _complete_enrolment(
    db: SessionDep, claims: dict, user: User, method: str
) -> RecoveryCodesOut:
    """Confirming *is* presenting a code, so this session is verified by having done it.

    Without that, somebody who enrolled a minute ago would be challenged again on their way
    into Admin Mode — twice inside a minute, which is the unusable policy PRD §1 rejects.
    """
    user.mfa_method = method
    user.mfa_enrolled_at = datetime.now(UTC)
    if method == mfa.EMAIL:
        # An email enrolment has no secret, and leaving a half-scanned TOTP one behind would
        # be a credential nothing checks and nobody knows is there.
        user.mfa_secret = None
    codes = await mfa.issue_recovery_codes(db, user)
    record_event(
        db,
        "mfa.enrolled",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=user.id,
        metadata={"email": user.email, "method": method},
    )
    await db.commit()
    await mfa.forget_staged_secret(user)
    await mfa.mark_verified(claims)
    mfa.notify(user.email, mfa.ENROLLED_SUBJECT, mfa.ENROLLED_MESSAGE)
    log.info("auth: %s enrolled a second factor (%s)", user.email, method)
    return RecoveryCodesOut(recovery_codes=codes)


# --- presenting a code ---------------------------------------------------------------------------


@router.post("/verify")
async def verify(
    payload: CodeRequest, user: UnrestrictedUser, claims: ClaimsDep, db: SessionDep
) -> UserOut:
    """Finish signing in. One field takes all three kinds of code (`mfa.check_any`).

    `UnrestrictedUser`, because a pending session is refused everywhere else — this is one of
    the three doors it has, and the only one that leads anywhere.
    """
    await mfa.accept_code(db, user, payload.code, reason="login")
    await mfa.mark_verified(claims)
    return await _out(claims, db, user)


@router.post("/email-otp/request", status_code=202)
async def request_email_otp(user: UnrestrictedUser, db: SessionDep) -> dict[str, str]:
    """Send a one-time code to the account's own address.

    Its own limit, not the failure counter: asking for a code is not a guess at one, and
    counting it would let anyone who reached a pending session lock the account out. The
    same reasoning, and the same limiter, as the forgotten-password form (Task 6).
    """
    if not await mfa.may_use_email_otp(db, user):
        raise mfa.EMAIL_OTP_NOT_ALLOWED
    await throttle.guard_request(user.email, kind="mfaotp")
    await mfa.send_otp(db, user, purpose="login")
    return {"status": "sent"}


# --- the account's own security page ----------------------------------------------------------


class StatusOut(BaseModel):
    enrolled: bool
    method: str | None
    # A count, never the codes. They were shown once; there is only a digest left to count.
    recovery_codes_remaining: int
    email_otp_allowed: bool
    required_for_admin: bool


@router.get("")
async def status(user: CurrentUser, db: SessionDep) -> StatusOut:
    policy = await mfa.read_policy(db)
    return StatusOut(
        enrolled=user.mfa_method is not None,
        method=user.mfa_method,
        recovery_codes_remaining=await mfa.remaining_recovery_codes(db, user),
        email_otp_allowed=policy.email_otp_allowed,
        required_for_admin=policy.required_for_admin,
    )


@router.post("/recovery-codes")
async def regenerate_recovery_codes(user: CurrentUser, db: SessionDep) -> RecoveryCodesOut:
    """A fresh set, and the old set stops working the moment this returns.

    No code is asked for. The session already carries a verified second factor — that is
    what `CurrentUser` means for an enrolled account — and asking again would only mean
    somebody whose authenticator is working has to prove it twice to replace the paper they
    have lost.
    """
    if user.mfa_method is None:
        raise mfa.BAD_CODE
    codes = await mfa.issue_recovery_codes(db, user)
    record_event(
        db,
        "mfa.recovery_codes_regenerated",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=user.id,
        metadata={"email": user.email},
    )
    await db.commit()
    return RecoveryCodesOut(recovery_codes=codes)

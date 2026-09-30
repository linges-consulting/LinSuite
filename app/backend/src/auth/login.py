"""Password login, logout, the endpoint that answers "who am I", and the mode switch.

Every failure here answers the same way and takes the same time, so that neither the body
nor a stopwatch says whether an address has an account. The detail that *is* recorded —
which address was tried, and why it failed — goes to the audit log, where an administrator
can read it and an attacker cannot.

All four endpoints live together because they are one thing: the lifecycle of a session.
The rule the mode switch adds to that lifecycle is in `auth/modes.py`; this module is only
its HTTP surface.

**Two refusals that are not about the credential.** An account whose invitation has not been
accepted has no password to be wrong about, and a deactivated staff member's password is
right and beside the point. Both are coded 403s rather than the uniform 401, because both
have an action attached — open your invitation link; ask an administrator — and "incorrect
email or password" sends somebody looking for the wrong thing. The second reads
`staff.active`, which is why this module knows about `scheduling.models`: whether a person
still works here is a fact about the person, and the sign-in door is the last one left open
once `revoke_all` has closed the others.
"""

import logging
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select

from auth import capabilities, modes, throttle
from auth import mfa as mfa_mod
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
from core.errors import (
    ACCOUNT_INACTIVE,
    ADMIN_MODE_REQUIRED,
    INVALID_PASSWORD,
    PASSWORD_NOT_SET,
    Forbidden,
)
from core.security import verify_password
from scheduling.models import Staff

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
    # The role's name, for display, and the capability keys, so the interface can leave out
    # what this account cannot use. Neither is the authority: the server re-reads the role on
    # every request, and a frontend that has these is still refused without them.
    role: str
    capabilities: list[str]
    mode: str
    can_switch_modes: bool
    admin_grant_expires_at: datetime | None
    admin_hard_limit_at: datetime | None
    # True and the frontend routes to the change-password screen and nowhere else. The
    # session is real either way — this is a forced change, not a refused login, so the
    # account can still be told what it has to do.
    must_change_password: bool
    # The second factor, as this session stands: whether the account has one, whether this
    # session still owes a code, and whether the business is asking for an enrolment. The
    # same shape of fact as `must_change_password`, and routed on the same way (`App.tsx`).
    mfa: mfa_mod.MfaOut

    @classmethod
    def of(
        cls,
        user: User,
        state: modes.ModeState | None = None,
        *,
        must_change_password: bool = False,
        mfa: mfa_mod.MfaOut,
    ) -> "UserOut":
        return cls(
            mfa=mfa,
            id=str(user.id),
            email=user.email,
            role=user.role.name,
            capabilities=sorted(user.capabilities),
            mode=state.mode if state else modes.STAFF_MODE,
            can_switch_modes=capabilities.can_switch_modes(user),
            admin_grant_expires_at=state.grant_expires_at if state else None,
            admin_hard_limit_at=state.hard_limit_at if state else None,
            must_change_password=must_change_password,
        )


@router.post("/login")
async def login(payload: LoginRequest, response: Response, db: SessionDep) -> UserOut:
    email = payload.email.lower()
    # Before the lookup and before the hash: a throttled attempt costs this server nothing,
    # and is keyed by what was typed rather than by an account, so being refused early says
    # nothing about whether the address exists.
    await throttle.guard(email)
    user = await db.scalar(select(User).where(func.lower(User.email) == email))

    if user is not None and user.password_hash is None:
        # An account an administrator created, whose invitation nobody has accepted. Refused
        # before the hash is touched, because there is no hash: Argon2 must never be handed
        # this column's null, and the dummy-hash padding would only buy a slower way to say
        # the wrong thing. It says the true thing instead, and yes, that confirms the address
        # exists — to somebody who already knows it was invited. The alternative is telling a
        # new colleague their password is wrong when they have never had one.
        #
        # Recorded and counted like any other refused attempt. Without that this would be the
        # one door in the product that costs an attacker nothing and leaves no trace — and it
        # is the door that answers "does this address exist", so it is exactly the one worth
        # probing. The lockout is no obstacle to the person it belongs to: spending the
        # invitation link clears it (`auth/passwords.py`).
        record_event(
            db,
            "login.refused_no_password",
            target_type="user",
            target_id=str(user.id),
            actor_user_id=None,
            metadata={"email": email},
        )
        locked = await throttle.record_failure(db, email, user)
        await db.commit()
        log.info("auth: sign-in refused for %s — the invitation has not been accepted", email)
        raise locked or Forbidden(
            PASSWORD_NOT_SET,
            "This account has not been set up yet. Open the link in your invitation email to "
            "choose a password, or ask an administrator to send another.",
        )

    # `None` verifies against a dummy hash: an unknown address costs the same milliseconds
    # as a known one, so the response time is not an account-enumeration oracle.
    if not await verify_password(user.password_hash if user else None, payload.password):
        record_event(
            db,
            "login.failed",
            target_type="user",
            # No actor: whoever typed this is exactly who we do not know. The account is named
            # as the target, which is what it is — the thing an attempt was made against.
            target_id=str(user.id) if user else None,
            actor_user_id=None,
            metadata={"email": email, "reason": "bad_password" if user else "unknown_email"},
        )
        locked = await throttle.record_failure(db, email, user)
        await db.commit()
        log.warning("auth: failed login for %s", email)
        raise locked or _REFUSED

    # After the password, not before: a deactivated account must not be a way for somebody
    # holding only an address to learn that it exists. The credential was right and the
    # account is closed, which is a different fact from a wrong password and gets its own
    # code so the sign-in screen can say so instead of offering a password reset.
    #
    # And before `throttle.clear`, deliberately: a refused sign-in must not reset the failure
    # run and the offence tier. Clearing here would hand anybody holding a deactivated
    # account's password an unlimited way to wipe the lockout state off that address.
    if not await db.scalar(select(Staff.active).where(Staff.user_id == user.id)):
        record_event(
            db,
            "login.refused_inactive",
            target_type="user",
            target_id=str(user.id),
            actor_user_id=None,
            metadata={"email": email},
        )
        await db.commit()
        log.warning("auth: sign-in refused for the deactivated account %s", email)
        raise Forbidden(
            ACCOUNT_INACTIVE,
            "This account has been deactivated. Ask an administrator to restore it.",
        )

    await throttle.clear(email)
    issued = issue_token(user.id)
    set_session_cookie(response, issued)
    claims = decode_token(issued.token)
    # An enrolled account's session is born owing a code, and `CurrentUser` refuses it
    # everywhere until one is presented. The password alone is not a session.
    if user.mfa_method is not None:
        await mfa_mod.begin_pending(claims)
    # The one fact `modes.try_first_run_admin_grant` (#117, spec #113) is ever allowed to
    # trust: a password was typed in this exact request. Marked unconditionally — whether the
    # first-run grant ever spends it depends on what happens afterwards (`_complete_enrolment`
    # is the only place that reads it), never on anything decided here.
    await modes.mark_fresh_login(issued.jti)
    record_event(
        db,
        "login.succeeded",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=user.id,
        metadata={"email": email, "jti": issued.jti, "mfa_pending": user.mfa_method is not None},
    )
    await db.commit()
    return UserOut.of(
        user,
        must_change_password=await change_required(db, user),
        mfa=await mfa_mod.snapshot(claims, db, user),
    )


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, db: SessionDep) -> None:
    """Idempotent on purpose: "log me out" must succeed even from a session already gone."""
    token = request.cookies.get(COOKIE_NAME)
    claims = decode_token(token) if token else None
    if claims and not await is_revoked(claims["jti"]):
        await revoke(claims)
        await modes.forget(claims["jti"])
        await mfa_mod.forget(claims["jti"])
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
        mfa=await mfa_mod.snapshot(claims, db, user),
    )


class ModeRequest(BaseModel):
    mode: Literal["staff", "admin"]
    # Required to *open* an admin window, and only then. Switching back and forth inside a
    # live window is free (PRD §1), and returning to Staff Mode never needs it.
    password: str | None = None
    # Required once per `ADMIN_MFA_INTERVAL_HOURS` for an enrolled account, independently of
    # the password: a live window does not excuse a verification that has gone stale, and a
    # fresh verification does not excuse a window that has lapsed.
    totp: str | None = None


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
        # No `admin` capability, no second mode to be in — the same rule that keeps the
        # switcher off the screen, enforced here for anyone calling the endpoint directly.
        if not capabilities.can_switch_modes(user):
            raise modes.ADMIN_MODE_REQUIRED
        # Both are checked before either is honoured, so a correct password never opens a
        # window that a missing code should have refused.
        if await mfa_mod.admin_challenge_due(claims, user):
            await _verify_second_factor(payload.totp, user, claims, db)
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
        mfa=await mfa_mod.snapshot(claims, db, user),
    )


# Genuinely the Admin Mode kind: there is no live window, and the way out is to open one.
_REAUTH_REQUIRED = Forbidden(ADMIN_MODE_REQUIRED, "Enter your password to switch to Admin Mode.")


async def _verify_second_factor(code: str | None, user: User, claims: dict, db: SessionDep) -> None:
    """The twelve-hourly challenge on the way into Admin Mode (PRD §1).

    A request carrying no code is refused before anything is checked, like a request
    carrying no password: the frontend cannot know the interval has elapsed until the server
    says so, and treating that first refusal as a failed attempt would charge somebody the
    lockout counter for a round trip they could not have avoided.

    Verifying writes `verified_at` even though the window may still be refused afterwards
    for a wrong password. That is the honest record — this session did present a valid
    second factor at this instant — and it is the harmless way round: possession was proved,
    and the password is a separate debt.
    """
    if code is None:
        raise mfa_mod.TOTP_REQUIRED
    await mfa_mod.accept_code(db, user, code, reason="admin_mode")
    await mfa_mod.mark_verified(claims)
    record_event(
        db,
        "admin.mfa_verified",
        target_type="session",
        target_id=claims["jti"],
        actor_user_id=user.id,
        metadata={"email": user.email},
    )
    await db.commit()


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

    # Only now — a request carrying no password never reached the counter, so it must not be
    # refused by it either.
    await throttle.guard(user.email)

    if not await verify_password(user.password_hash, password):
        record_event(
            db,
            "login.failed",
            target_type="user",
            target_id=str(user.id),
            # Null like every other `login.failed`, so one event type means one thing. The
            # session that made the attempt is named in `jti` instead — more precise than an
            # actor column would be, since what is known here is the session, not the person.
            actor_user_id=None,
            metadata={"email": user.email, "reason": "reauth", "jti": claims["jti"]},
        )
        locked = await throttle.record_failure(db, user.email, user)
        await db.commit()
        log.warning("auth: failed Admin Mode re-authentication for %s", user.email)
        # Not an Admin Mode refusal, however much it looks like one from the endpoint it
        # came from: the window is not the problem, the typing was. A global handler that
        # treated this as a lapsed window would tell somebody who mistyped that their Admin
        # Mode expired, and send them to do again the thing they were already doing.
        raise locked or Forbidden(INVALID_PASSWORD, "Incorrect password")

    await throttle.clear(user.email)

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

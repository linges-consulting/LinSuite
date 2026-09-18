"""The second factor: what it is, where its state lives, and the gates it puts on a session.

**TOTP only. There is no SMS path and there never will be** (tech-stack §14): a text message
costs money per send and is defeated by a SIM swap, which is the attack this feature exists
to stop. Email OTP is here instead, and is labelled for what it is — lower assurance, because
NIST does not recognise email as an out-of-band channel when the inbox usually lives in the
same browser session an attacker has already taken.

**Three pieces of state, in three places, because they expire differently.**

| Fact | Where | Ends when |
|---|---|---|
| the enrolment (secret, method, recovery codes) | PostgreSQL | an administrator resets it |
| this session's MFA status | Redis, beside the mode and the admin grant | the session does |
| a live email OTP | Redis, keyed by the user | its ten minutes are up |

The session hash `session:mfa:<jti>` carries two fields. `pending` is set at login for an
enrolled account and is the gate that makes a session useless until a code is presented;
`verified_at` is written when one is, and is what the twelve-hourly Admin Mode challenge is
measured against. Both are keyed by the `jti` and never outlive the token, for the same
reason the admin grant is not: a later session that reused the `jti` must not inherit a
verification it never performed.

**Why this module imports nothing from `auth/session.py`.** The gate has to live inside
`CurrentUser`, so that a route written next year inherits it without knowing this file
exists — which means `session.py` imports this. Everything here is therefore the rule and
the state; the HTTP surface that needs `UnrestrictedUser` is in `auth/mfa_routes.py`.

**A failed second factor is a failed authentication.** Every wrong TOTP, recovery code and
email OTP goes onto the same per-account counter a wrong password does (`auth/throttle.py`)
and is refused by the same lock. Anything else would leave the second factor as the one door
onto an account that can be guessed at without limit — and six digits is a smaller space
than a password.
"""

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pyotp
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update

from auth import throttle
from auth.models import ADMIN_CAPABILITY, MfaRecoveryCode, User
from core import crypto
from core.audit import record_event
from core.config import get_settings
from core.db import SessionDep
from core.errors import (
    INVALID_MFA_CODE,
    MFA_EMAIL_OTP_NOT_ALLOWED,
    MFA_ENROLMENT_REQUIRED,
    MFA_REQUIRED,
    MFA_VERIFICATION_REQUIRED,
    Forbidden,
)
from core.models import Business
from core.redis import get_redis
from notifications.tasks import send_email

log = logging.getLogger(__name__)

TOTP = "totp"
EMAIL = "email"
RECOVERY_CODE = "recovery_code"

# 30-second step, ±1 window: a phone whose clock drifts a few seconds, or a person typing
# slowly, still gets in. Two windows would double the guessing surface for no real gain.
TOTP_INTERVAL_SECONDS = 30
TOTP_WINDOW = 1

_SESSION_PREFIX = "session:mfa:"
_OTP_PREFIX = "mfa:otp:"
_ENROLMENT_PREFIX = "mfa:enrolling:"

# How long a started enrolment waits for the code that confirms it. Long enough to find a
# phone, short enough that an abandoned one is not sitting around tomorrow.
ENROLMENT_MINUTES = 15

PENDING_FIELD = "pending"
VERIFIED_FIELD = "verified_at"


# --- the refusals -----------------------------------------------------------------------------

VERIFICATION_REQUIRED = Forbidden(
    MFA_VERIFICATION_REQUIRED, "Enter the code from your authenticator to finish signing in."
)
ENROLMENT_REQUIRED = Forbidden(
    MFA_ENROLMENT_REQUIRED,
    "This business requires a second factor on accounts that can administer it. "
    "Set one up to continue.",
)
TOTP_REQUIRED = Forbidden(
    MFA_REQUIRED, "Enter the code from your authenticator to switch to Admin Mode."
)
BAD_CODE = Forbidden(INVALID_MFA_CODE, "That code is not right, or has already been used.")
EMAIL_OTP_NOT_ALLOWED = Forbidden(
    MFA_EMAIL_OTP_NOT_ALLOWED,
    "Emailed codes are not available on this account. Use your authenticator or a recovery code.",
)


# --- the secret at rest ------------------------------------------------------------------------


def new_secret() -> str:
    return pyotp.random_base32()


def encrypt_secret(secret: str) -> str:
    return crypto.encrypt(secret, get_settings().mfa_encryption_key)


def decrypt_secret(sealed: str) -> str:
    return crypto.decrypt(sealed, get_settings().mfa_encryption_key)


def provisioning_uri(secret: str, email: str, issuer: str) -> str:
    """What the QR code encodes. The issuer is the business, so somebody carrying two
    clinics on one phone can tell the two entries apart."""
    return pyotp.TOTP(secret, interval=TOTP_INTERVAL_SECONDS).provisioning_uri(
        name=email, issuer_name=issuer
    )


def check_totp(secret: str, code: str) -> bool:
    return pyotp.TOTP(secret, interval=TOTP_INTERVAL_SECONDS).verify(
        code.strip(), valid_window=TOTP_WINDOW
    )


# --- an enrolment in progress -------------------------------------------------------------
#
# The candidate secret waits in Redis rather than in `users.mfa_secret`, and that is not a
# storage preference: writing it to the column would destroy the secret the account is
# *currently* authenticating with, so somebody who started a second enrolment and then closed
# the tab would find their working authenticator dead and their account unreachable. The
# column is only written by a code that proved the app has it.
#
# Sealed in Redis too. It is the same secret wherever it is sitting.


def enrolment_key(user_id: str | uuid.UUID) -> str:
    return _ENROLMENT_PREFIX + str(user_id)


async def stage_secret(user: User, secret: str) -> None:
    await get_redis().set(enrolment_key(user.id), encrypt_secret(secret), ex=ENROLMENT_MINUTES * 60)


async def staged_secret(user: User) -> str | None:
    sealed = await get_redis().get(enrolment_key(user.id))
    return decrypt_secret(sealed) if sealed else None


async def forget_staged_secret(user: User) -> None:
    await get_redis().delete(enrolment_key(user.id))


# --- this session's MFA state -------------------------------------------------------------------


def session_key(jti: str) -> str:
    return _SESSION_PREFIX + jti


def _session_seconds(claims: dict) -> int:
    return max(1, int(claims["exp"] - datetime.now(UTC).timestamp()))


@dataclass(frozen=True)
class SessionMfa:
    pending: bool
    verified_at: datetime | None


async def read_session(claims: dict) -> SessionMfa:
    stored = await get_redis().hgetall(session_key(claims["jti"]))
    verified = stored.get(VERIFIED_FIELD)
    return SessionMfa(
        pending=stored.get(PENDING_FIELD) == "1",
        verified_at=datetime.fromtimestamp(float(verified), UTC) if verified else None,
    )


async def begin_pending(claims: dict) -> None:
    """Mark a fresh session as owing a code. Called at login for an enrolled account."""
    redis = get_redis()
    key = session_key(claims["jti"])
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(key, PENDING_FIELD, "1")
        pipe.expire(key, _session_seconds(claims))
        await pipe.execute()


async def mark_verified(claims: dict) -> None:
    redis = get_redis()
    key = session_key(claims["jti"])
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(key, VERIFIED_FIELD, str(datetime.now(UTC).timestamp()))
        pipe.hdel(key, PENDING_FIELD)
        pipe.expire(key, _session_seconds(claims))
        await pipe.execute()


async def forget(jti: str) -> None:
    await get_redis().delete(session_key(jti))


async def age_verification(jti: str, when: datetime) -> None:
    """Move this session's verification into the past. For tests: the twelve-hour interval
    cannot be exercised by waiting twelve hours, and the rule under test is the comparison,
    not the clock. `issue_token(ttl=...)` exists for the same reason."""
    await get_redis().hset(session_key(jti), VERIFIED_FIELD, str(when.timestamp()))


# --- the per-business policy ----------------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    required_for_admin: bool
    email_otp_allowed: bool


async def read_policy(db: SessionDep) -> Policy:
    """A single-row lookup on a table that changes about never.

    ponytail: re-read per request, like `change_required`. Cache it if a profile says so.
    """
    row = (
        await db.execute(
            select(Business.mfa_required_for_admin, Business.mfa_email_otp_allowed).where(
                Business.id == 1
            )
        )
    ).first()
    if row is None:
        # An unclaimed instance has no business and no accounts; nothing to require it of.
        return Policy(required_for_admin=False, email_otp_allowed=False)
    return Policy(required_for_admin=row[0], email_otp_allowed=row[1])


async def enrolment_required(db: SessionDep, user: User) -> bool:
    """Whether this account must set up a second factor before it can do anything else.

    Only accounts that can administer the business, and only where the business asked for it.
    A staff account is never pushed into it by this policy — the PRD scopes the default to
    the Admin capability, and forcing a practitioner who only opens the calendar into an
    authenticator app is how a clinic ends up sharing one phone.
    """
    if user.mfa_method is not None or ADMIN_CAPABILITY not in user.capabilities:
        return False
    return (await read_policy(db)).required_for_admin


# --- the gate every protected route inherits ------------------------------------------------------


async def assert_verified(claims: dict) -> None:
    """Refuse a session that has presented a password and nothing else.

    Named separately from `assert_cleared` because one endpoint needs exactly this half:
    `POST /auth/password/change` runs on `UnrestrictedUser` (it is how a forced change is
    paid) and must still be closed to a pending session — otherwise somebody holding only a
    stolen password could rewrite the credential without ever meeting the second factor,
    which is the whole attack this feature exists to stop.
    """
    if (await read_session(claims)).pending:
        raise VERIFICATION_REQUIRED


async def assert_cleared(claims: dict, db: SessionDep, user: User) -> None:
    """Raised from inside `CurrentUser`, so no route has to remember either rule.

    403 and not 401 in both cases, for the reason the forced password change is: the session
    is real and the password was right. A 401 would send the browser to `/login`, where
    signing in again would produce another session owing the same thing.
    """
    await assert_verified(claims)
    if await enrolment_required(db, user):
        raise ENROLMENT_REQUIRED


class MfaOut(BaseModel):
    """What `/auth/me` says about the second factor, and what the frontend routes on."""

    enrolled: bool
    method: str | None
    # This session owes a code. The only reachable endpoints are the verify screen's.
    pending: bool
    # The business requires one and this account has none. The only reachable endpoints are
    # enrolment's.
    enrolment_required: bool
    verified_at: datetime | None
    # Whether the business allows emailed codes as a factor, so the verify screen knows
    # whether to offer one. Not authorisation — the server decides that per request.
    email_otp_allowed: bool


async def snapshot(claims: dict, db: SessionDep, user: User) -> MfaOut:
    state = await read_session(claims)
    policy = await read_policy(db)
    return MfaOut(
        enrolled=user.mfa_method is not None,
        method=user.mfa_method,
        pending=state.pending,
        enrolment_required=(
            user.mfa_method is None
            and ADMIN_CAPABILITY in user.capabilities
            and policy.required_for_admin
        ),
        verified_at=state.verified_at,
        email_otp_allowed=policy.email_otp_allowed,
    )


async def admin_challenge_due(claims: dict, user: User) -> bool:
    """Whether entering Admin Mode has to ask for a code right now.

    PRD §1: at the initial login and once per twelve hours, never on every mode switch. The
    login challenge *is* the twelve-hourly one — it writes `verified_at` — so an
    administrator who signed in five minutes ago is not asked again, and one who has been in
    the same Staff Mode session since this morning is.
    """
    if user.mfa_method is None:
        return False
    state = await read_session(claims)
    if state.verified_at is None:
        return True
    interval = timedelta(hours=get_settings().admin_mfa_interval_hours)
    return state.verified_at < datetime.now(UTC) - interval


# --- recovery codes ---------------------------------------------------------------------------


def digest(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _format(raw: str) -> str:
    return f"{raw[:5]}-{raw[5:]}"


def normalise(code: str) -> str:
    """What the user typed, as the digest was taken. People retype these off paper, so the
    dash and the casing are ours to put back rather than theirs to get right."""
    hex_only = "".join(c for c in code.lower() if c in "0123456789abcdef")
    return _format(hex_only) if len(hex_only) == 10 else code.strip().lower()


async def issue_recovery_codes(db: SessionDep, user: User) -> list[str]:
    """A fresh set, replacing whatever the account had. Returned once and never again —
    only the digests are kept, so there is nothing left to show a second time."""
    await forget_recovery_codes(db, user.id)
    codes = [_format(secrets.token_hex(5)) for _ in range(get_settings().mfa_recovery_code_count)]
    for code in codes:
        db.add(MfaRecoveryCode(user_id=user.id, code_hash=digest(code)))
    return codes


async def forget_recovery_codes(db: SessionDep, user_id: uuid.UUID) -> None:
    """Spent rows go too. They are kept only while the set they belong to is live — once it
    has been replaced, "this code was used" is a fact about codes nothing will accept."""
    await db.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user_id))


async def remaining_recovery_codes(db: SessionDep, user: User) -> int:
    return await db.scalar(
        select(func.count())
        .select_from(MfaRecoveryCode)
        .where(MfaRecoveryCode.user_id == user.id, MfaRecoveryCode.used_at.is_(None))
    )


async def spend_recovery_code(db: SessionDep, user: User, code: str) -> bool:
    """Single-use, enforced by the UPDATE rather than by a read-then-write.

    `WHERE used_at IS NULL` and a rowcount is the same trick the stock decrement uses
    (CLAUDE.md): the statement is the lock, so two requests arriving with the same code in
    the same millisecond cannot both find it unspent.
    """
    result = await db.execute(
        update(MfaRecoveryCode)
        .where(
            MfaRecoveryCode.user_id == user.id,
            MfaRecoveryCode.code_hash == digest(normalise(code)),
            MfaRecoveryCode.used_at.is_(None),
        )
        .values(used_at=datetime.now(UTC))
    )
    return result.rowcount == 1


# --- email OTP --------------------------------------------------------------------------------


def otp_key(user_id: str | uuid.UUID) -> str:
    return _OTP_PREFIX + str(user_id)


async def may_use_email_otp(db: SessionDep, user: User) -> bool:
    """Three ways in, and the third is the one that matters.

    The business may have chosen email as a factor it allows; the account may already be
    using it; or the recovery codes are all gone, which is precisely the situation
    tech-stack §14 keeps this path for — the lost-device path when the lost-device path has
    also been lost. Outside those, an emailed code is a weaker factor offered for no reason,
    and offering it would let anybody who reached a pending session downgrade the challenge.
    """
    if user.mfa_method == EMAIL:
        return True
    if (await read_policy(db)).email_otp_allowed:
        return True
    return await remaining_recovery_codes(db, user) == 0


async def send_otp(db: SessionDep, user: User, *, purpose: str) -> None:
    """Mint a six-digit code, keep only its digest, and hand the plaintext to the queue."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    minutes = get_settings().mfa_email_otp_minutes
    await get_redis().set(otp_key(user.id), digest(code), ex=minutes * 60)
    record_event(
        db,
        "mfa.email_otp_requested",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=user.id,
        metadata={"email": user.email, "purpose": purpose},
    )
    await db.commit()
    send_email.delay(user.email, "Your LinSuite sign-in code", _otp_message(code, minutes))


def _otp_message(code: str, minutes: int) -> str:
    return (
        f"Your LinSuite sign-in code is {code}.\n\n"
        f"It works once and expires in {minutes} minutes.\n\n"
        "If you did not ask for it, somebody has your password. Change it as soon as you "
        "can, and tell an administrator."
    )


async def spend_otp(user: User, code: str) -> bool:
    """Single-use: the key goes the moment a code matches it."""
    redis = get_redis()
    stored = await redis.get(otp_key(user.id))
    if stored is None or stored != digest(code.strip()):
        return False
    await redis.delete(otp_key(user.id))
    return True


# --- verifying, whichever kind of code it was -------------------------------------------------


@dataclass(frozen=True)
class Accepted:
    """Which factor let this request through — the audit trail's business, not the caller's."""

    kind: str


async def accept_code(db: SessionDep, user: User, code: str, *, reason: str) -> Accepted:
    """The one path a second factor is ever checked on, whichever door asked for it.

    Login's verify screen and the Admin Mode challenge both come through here, so a failure
    costs the same on both and neither can be the unthrottled one. `guard` runs before any
    code is compared, for the same reason it runs before a password is hashed.
    """
    await throttle.guard(user.email)

    accepted = await check_any(db, user, code)
    if accepted is None:
        record_event(
            db,
            "mfa.failed",
            target_type="user",
            target_id=str(user.id),
            # Null like `login.failed`: what is known is the session, not the person. And no
            # code value — an audit row is not a place to write down what somebody typed at
            # an authentication prompt.
            actor_user_id=None,
            metadata={"email": user.email, "reason": reason},
        )
        locked = await throttle.record_failure(db, user.email, user)
        await db.commit()
        log.warning("auth: failed second factor for %s (%s)", user.email, reason)
        raise locked or BAD_CODE

    await throttle.clear(user.email)
    if accepted.kind == RECOVERY_CODE:
        record_event(
            db,
            "mfa.recovery_code_used",
            target_type="user",
            target_id=str(user.id),
            actor_user_id=user.id,
            metadata={"email": user.email, "remaining": await remaining_recovery_codes(db, user)},
        )
    await db.commit()
    if accepted.kind == RECOVERY_CODE:
        # A spent recovery code usually means a lost device — which is either the user's own
        # bad afternoon or somebody else's good one, and only the owner can tell which.
        notify(user.email, RECOVERY_USED_SUBJECT, RECOVERY_USED_MESSAGE)
    return accepted


async def check_any(db: SessionDep, user: User, code: str) -> Accepted | None:
    """A TOTP, an emailed code or a recovery code, in that order.

    One field on one screen rather than three, because the person typing knows which of them
    they are holding and the server can tell them apart by trying. The two six-digit kinds
    can collide in principle and it does not matter: either one is a factor this account is
    entitled to use, and only one of them is consumed.
    """
    if (
        user.mfa_method == TOTP
        and user.mfa_secret
        and check_totp(decrypt_secret(user.mfa_secret), code)
    ):
        return Accepted(TOTP)
    if await spend_otp(user, code):
        return Accepted(EMAIL)
    if await spend_recovery_code(db, user, code):
        return Accepted(RECOVERY_CODE)
    return None


# --- what the owner is told -------------------------------------------------------------------

ENROLLED_SUBJECT = "A second factor was added to your LinSuite account"
ENROLLED_MESSAGE = (
    "A second factor is now required to sign in to your LinSuite account, and a set of "
    "recovery codes was issued.\n\n"
    "If that was you, nothing else is needed. If it was not, somebody else can sign in as "
    "you — contact an administrator immediately."
)

RECOVERY_USED_SUBJECT = "A recovery code was used on your LinSuite account"
RECOVERY_USED_MESSAGE = (
    "Somebody signed in to your LinSuite account with one of your recovery codes, which "
    "usually means the authenticator app is gone.\n\n"
    "If that was you, set up your authenticator again and regenerate your recovery codes "
    "from Security. If it was not, contact an administrator immediately."
)

RESET_SUBJECT = "The second factor on your LinSuite account was reset"
RESET_MESSAGE = (
    "An administrator reset the second factor on your LinSuite account. Your recovery "
    "codes no longer work, every signed-in device has been signed out, and you will be "
    "asked to set one up again the next time you sign in.\n\n"
    "If you did not ask for this, contact your administrator — somebody removed a "
    "protection from your account."
)


def notify(email: str, subject: str, message: str) -> None:
    """tech-stack §14: the owner hears about every change to how their account is protected.
    Enqueued rather than sent — delivery is the worker's job, never a request handler's."""
    send_email.delay(email, subject, message)

"""Per-account brute-force throttling: a progressive delay, then a temporary lock.

**One counter for every password check this product makes.** Signing in, re-authenticating
into Admin Mode and confirming a current password before changing it are three doors onto the
same credential, and an attacker who found one of them unthrottled would use that one. So all
three call `guard` before the hash is touched and `record_failure` after it refuses, and any
one of them succeeding clears the count.

**Keyed by the digest of the address, not by the account.** The key exists whether or not
anybody registered that address, so a throttled response cannot answer the question the 401
body deliberately refuses. It also means the counter survives an account being renamed or
deleted mid-attack, which is the moment it matters.

**The delay is an instant, never a sleep.** Holding the request open for 128 seconds would
tie up a worker per attempt and hand an attacker a cheap way to exhaust the pool — the
refusal is immediate, and `Retry-After` says when to come back.

**The lock is a Redis key whose TTL is the auto-unlock.** There is nothing to schedule and
nothing that can fail to run; the account reopens because the key stopped existing. Repeat
lockouts of the same address escalate through `lockout_tier_minutes` and stop at its last
entry, which is a day. Never `forever`: a permanent lock turns a known username into a way to
shut a clinic's front desk down mid-shift (PRD §1), which is the attack, not the defence.

| Key | Holds | TTL |
|---|---|---|
| `auth:fail:<digest>` | consecutive failures | the failure window, pushed out by each failure |
| `auth:delay:<digest>` | that the delay is running | what is left of it — and so `Retry-After` |
| `auth:lock:<digest>` | which tier locked it | the lock, and so the auto-unlock |
| `auth:tier:<digest>` | lockouts so far | the decay window, pushed out by each lockout |
| `auth:reset:<digest>` | reset links asked for | the reset-request window |
"""

import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from auth.models import User
from auth.modes import AdminUser
from core.audit import record_event
from core.config import get_settings
from core.db import SessionDep
from core.redis import get_redis
from notifications.tasks import send_email

log = logging.getLogger(__name__)


def _digest(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def fail_key(email: str) -> str:
    return "auth:fail:" + _digest(email)


def delay_key(email: str) -> str:
    return "auth:delay:" + _digest(email)


def lock_key(email: str) -> str:
    return "auth:lock:" + _digest(email)


def tier_key(email: str) -> str:
    return "auth:tier:" + _digest(email)


def _reset_key(email: str) -> str:
    return "auth:reset:" + _digest(email)


def _too_many(seconds: int, detail: str, *, locked: bool) -> HTTPException:
    """429 with the one header a client can act on without parsing prose.

    `X-Account-Locked` is what lets the frontend tell "wait 32 seconds" from "locked until
    2:47pm" without inferring it from the size of `Retry-After` — an inference that a change
    to either setting would quietly break.
    """
    headers = {"Retry-After": str(max(1, seconds))}
    if locked:
        headers["X-Account-Locked"] = "1"
    return HTTPException(status_code=429, detail=detail, headers=headers)


def _delay_seconds(failures: int) -> int:
    """1, 2, 4, 8 … capped. The delay after `failures` consecutive failures — so the first one
    already costs a second, and the tenth is unreachable inside a scripted burst."""
    if failures < 1:
        return 0
    return min(2 ** (failures - 1), get_settings().lockout_delay_cap_seconds)


async def guard(email: str) -> None:
    """Refuse an attempt that is too soon, before any password is verified."""
    redis = get_redis()
    async with redis.pipeline(transaction=False) as pipe:
        pipe.ttl(lock_key(email))
        pipe.ttl(delay_key(email))
        lock_ttl, delay_ttl = await pipe.execute()

    if lock_ttl > 0:
        raise _too_many(
            lock_ttl,
            "This account is temporarily locked after too many failed attempts. "
            f"It unlocks itself in {_humanise(lock_ttl)}.",
            locked=True,
        )
    if delay_ttl > 0:
        raise _too_many(
            delay_ttl, f"Too many attempts. Try again in {delay_ttl} seconds.", locked=False
        )


async def clear(email: str) -> None:
    """A verified password ends the run. The offence tier is deliberately left alone — it
    decays on its own, and one success is not evidence the earlier lockout was a mistake."""
    await get_redis().delete(fail_key(email), delay_key(email))


async def record_failure(db: SessionDep, email: str, user: User | None) -> HTTPException | None:
    """Count a failed password check, and lock the account if that was the last one.

    Returns the refusal to raise *instead of* the caller's own 401/403 when this failure
    locked the account, or None to leave the caller's answer alone. It returns rather than
    raises because the caller has an audit row staged in the same transaction as ours, and
    unwinding out of here would take both rows with it.
    """
    settings = get_settings()
    redis = get_redis()
    # ponytail: INCR is atomic, the read-and-branch below is not, so two failures landing in
    # the same millisecond can both cross the threshold and lock twice — one extra audit row,
    # one extra notice, one tier skipped. Move the whole sequence into a Lua script if a real
    # deployment ever shows it happening; a scripted attacker gains nothing from it.
    failures = await redis.incr(fail_key(email))
    await redis.expire(fail_key(email), settings.lockout_failure_window_minutes * 60)

    if failures < settings.lockout_threshold:
        await redis.set(delay_key(email), "1", ex=_delay_seconds(failures))
        return None

    return await _lock(db, email, user)


async def _lock(db: SessionDep, email: str, user: User | None) -> HTTPException:
    settings = get_settings()
    redis = get_redis()
    tier = await redis.incr(tier_key(email))
    await redis.expire(tier_key(email), settings.lockout_tier_decay_hours * 3600)
    minutes = settings.lockout_tier_minutes[min(tier, len(settings.lockout_tier_minutes)) - 1]
    unlock_at = datetime.now(UTC) + timedelta(minutes=minutes)

    await redis.set(lock_key(email), str(tier), ex=minutes * 60)
    # The run is over: the lock has taken its place, and a stale delay key would otherwise
    # outlive an early unlock.
    await redis.delete(fail_key(email), delay_key(email))

    if user is not None:
        record_event(
            db,
            "account.locked",
            target_type="user",
            target_id=str(user.id),
            actor_user_id=user.id,
            metadata={"email": user.email, "tier": tier, "unlock_at": unlock_at.isoformat()},
        )
        # Enqueued here rather than after the caller's commit: the lock is already real in
        # Redis, so the message is true whatever the transaction does next, and the owner
        # hearing about an attack promptly is the point of sending it at all.
        send_email.delay(
            user.email,
            "Your LinSuite account is temporarily locked",
            _lockout_message(minutes, unlock_at),
        )
    else:
        # No audit row: an address nobody registered is not a user, and the trail is for
        # administrators rather than a list of what an attacker typed. The lock is identical.
        log.warning("auth: locked an address with no account after %d failures", tier)

    return _too_many(
        minutes * 60,
        "This account is temporarily locked after too many failed attempts. "
        f"It unlocks itself in {_humanise(minutes * 60)}.",
        locked=True,
    )


def _humanise(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = round(minutes / 60)
    return f"{hours} hour{'s' if hours != 1 else ''}"


def _lockout_message(minutes: int, unlock_at: datetime) -> str:
    return (
        "Too many failed sign-in attempts locked your LinSuite account.\n\n"
        f"It unlocks itself in {_humanise(minutes * 60)}, at "
        f"{unlock_at.strftime('%H:%M')} UTC — nobody has to do anything.\n\n"
        "If this was not you, someone is guessing your password. Sign in once it reopens and "
        "change it, or ask an administrator to unlock the account sooner."
    )


PASSWORD_CHANGED_SUBJECT = "Your LinSuite password was changed"

PASSWORD_CHANGED_MESSAGE = (
    "The password on your LinSuite account was just changed, and every other signed-in "
    "device was signed out.\n\n"
    "If that was you, nothing else is needed. If it was not, contact an administrator "
    "immediately — somebody else can currently sign in as you."
)


def notify_password_changed(email: str) -> None:
    """tech-stack §14: the owner hears about every password change. It is the one signal that
    reaches someone whose account was taken over by a reset they did not ask for."""
    send_email.delay(email, PASSWORD_CHANGED_SUBJECT, PASSWORD_CHANGED_MESSAGE)


async def guard_reset_request(email: str) -> None:
    """A separate, simpler limit: how many links one address may ask for.

    Not part of the failure counter, and it must never be — asking for a reset link is not a
    guess at a password, and feeding it into the lockout would let anybody lock any account
    whose address they know.
    """
    settings = get_settings()
    redis = get_redis()
    asked = await redis.incr(_reset_key(email))
    # `nx` matters more than it looks: without it a counter whose `expire` was lost — a crash
    # between the two calls — would keep its value forever, and this address could never ask
    # for a link again. Nothing here is allowed to be permanent.
    await redis.expire(_reset_key(email), settings.reset_request_window_minutes * 60, nx=True)
    if asked > settings.reset_request_limit:
        ttl = await redis.ttl(_reset_key(email))
        raise _too_many(
            max(1, ttl),
            "Too many reset links have been requested for this address. Try again later.",
            locked=False,
        )


# --- the administrator's early unlock --------------------------------------------------------

router = APIRouter(prefix="/admin/users", tags=["admin"])


@router.post("/{user_id}/unlock", status_code=204)
async def unlock_account(user_id: uuid.UUID, admin: AdminUser, db: SessionDep) -> None:
    """Reopen an account before its lock expires. Admin Mode only.

    The offence tier goes with it. Leaving it would mean the next lockout of an account an
    administrator just forgave opened at the escalated tier, which is half a forgiveness.
    """
    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="No such user.")

    await get_redis().delete(
        lock_key(user.email), tier_key(user.email), fail_key(user.email), delay_key(user.email)
    )
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

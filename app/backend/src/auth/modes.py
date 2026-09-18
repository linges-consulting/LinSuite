"""Admin Mode: a short, sliding window held in Redis beside the session's `jti`.

**Mode is server state, not a token claim.** A JWT is a bearer statement the server cannot
retract between issue and expiry, and Admin Mode has to end three ways it cannot predict at
issue time — on idle, at a hard limit, and the moment the user switches back. So the mode
lives where it can be ended, and the token stays identity-only (`sub`, `jti`, `exp`). The
side benefit is that entering and leaving Admin Mode re-issues nothing: no cookie churn on
every admin request, and no window where two tokens for one session are both valid.

Two keys per session, both keyed by the session's `jti` and never outliving the token:

| Key | Value | TTL |
|---|---|---|
| `session:mode:<jti>` | the mode being served | the rest of the session |
| `session:admin:<jti>` | the hard-limit instant | the idle window |

The *grant* is the authority; the mode key only says whether it is currently in use. So
switching to Staff Mode leaves the grant alone — which is exactly what makes coming back
free while the window lasts (PRD §1). Each admin request slides the grant's TTL forward but
never past the hard limit in its value, so an administrator who never pauses is still asked
for a password at the hard limit rather than never.

Nothing here refreshes the window for `/auth/me`: the frontend polls it to draw the
countdown, and a poll that slid the window would keep an idle tab in Admin Mode forever.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends

from auth.models import User
from auth.session import ClaimsDep, CurrentUser
from core.config import get_settings
from core.errors import ADMIN_MODE_REQUIRED as _ADMIN_MODE_REQUIRED_CODE
from core.errors import Forbidden
from core.redis import get_redis

STAFF_MODE = "staff"
ADMIN_MODE = "admin"
MODES = (STAFF_MODE, ADMIN_MODE)

_MODE_PREFIX = "session:mode:"
_GRANT_PREFIX = "session:admin:"

ADMIN_MODE_REQUIRED = Forbidden(_ADMIN_MODE_REQUIRED_CODE, "Switch to Admin Mode to do this.")


def grant_key(jti: str) -> str:
    return _GRANT_PREFIX + jti


def mode_key(jti: str) -> str:
    return _MODE_PREFIX + jti


async def forget(jti: str) -> None:
    """Drop a session's mode state. Logout revokes the token anyway, so nothing could use the
    grant — but leaving it to time out keeps a live admin window in Redis for a session that
    is over, and the next reader of that key would have to know why it does not count."""
    await get_redis().delete(mode_key(jti), grant_key(jti))


@dataclass(frozen=True)
class ModeState:
    """What mode this session is being served in, and how long Admin Mode has left."""

    mode: str
    grant_expires_at: datetime | None
    hard_limit_at: datetime | None

    @property
    def in_admin_mode(self) -> bool:
        return self.mode == ADMIN_MODE and self.grant_expires_at is not None


def _session_seconds(claims: dict) -> int:
    """What is left of the session token. Nothing keyed by its `jti` may outlive it, or a
    later session that reused the `jti` would inherit a window it never asked for."""
    return max(1, int(claims["exp"] - datetime.now(UTC).timestamp()))


async def read_state(claims: dict) -> ModeState:
    jti = claims["jti"]
    redis = get_redis()
    async with redis.pipeline(transaction=False) as pipe:
        pipe.get(mode_key(jti))
        pipe.get(grant_key(jti))
        pipe.ttl(grant_key(jti))
        stored_mode, hard_limit, ttl = await pipe.execute()

    grant_expires_at = hard_limit_at = None
    if hard_limit is not None and ttl > 0:
        now = datetime.now(UTC)
        hard = datetime.fromtimestamp(float(hard_limit), UTC)
        # Whichever comes first: the idle window running out, or the hard limit arriving.
        expires = min(now + timedelta(seconds=ttl), hard)
        if expires > now:
            grant_expires_at, hard_limit_at = expires, hard

    # Without a live grant there is no Admin Mode to be in, whatever the mode key still says.
    mode = ADMIN_MODE if stored_mode == ADMIN_MODE and grant_expires_at else STAFF_MODE
    return ModeState(mode=mode, grant_expires_at=grant_expires_at, hard_limit_at=hard_limit_at)


async def grant_admin(claims: dict) -> None:
    """Open a fresh admin window. Only a verified password reaches here."""
    settings = get_settings()
    hard_limit = datetime.now(UTC) + timedelta(minutes=settings.admin_hard_limit_minutes)
    ttl = min(
        settings.admin_idle_minutes * 60,
        settings.admin_hard_limit_minutes * 60,
        _session_seconds(claims),
    )
    await get_redis().set(grant_key(claims["jti"]), str(hard_limit.timestamp()), ex=max(1, ttl))


async def set_mode(claims: dict, mode: str) -> None:
    await get_redis().set(mode_key(claims["jti"]), mode, ex=_session_seconds(claims))


async def slide(claims: dict, hard_limit_at: datetime) -> None:
    """Push the idle window out again — never past the hard limit, which does not move."""
    remaining = int((hard_limit_at - datetime.now(UTC)).total_seconds())
    ttl = min(get_settings().admin_idle_minutes * 60, remaining, _session_seconds(claims))
    if ttl > 0:
        await get_redis().expire(grant_key(claims["jti"]), ttl)


async def require_admin_mode(claims: ClaimsDep, user: CurrentUser) -> User:
    """The guard on every administrative endpoint.

    401 when there is no session; 403 when the session is being served in Staff Mode — and
    that 403 is the whole point of the feature: holding the admin capability is not the same
    as having it active. Serving the request is activity, so the window slides.
    """
    state = await read_state(claims)
    if not state.in_admin_mode:
        raise ADMIN_MODE_REQUIRED
    await slide(claims, state.hard_limit_at)
    return user


AdminUser = Annotated[User, Depends(require_admin_mode)]

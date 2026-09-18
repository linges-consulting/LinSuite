"""The staff session: an HS256 JWT in an httpOnly cookie, revocable through Redis.

**Why a cookie and not a bearer token.** The token never reaches JavaScript, so an XSS bug
in any dependency cannot read it. The price is CSRF, paid for twice: `SameSite=Lax` keeps
the cookie off cross-site POSTs, and `main.py` refuses any mutating request that is not
`application/json`, which is the one content type an HTML form cannot produce.

**Why Redis.** A JWT is valid until it expires; nothing about verifying a signature can
know the holder pressed Log out. Logout writes the token's `jti` to a denylist that expires
exactly when the token would have, so the list stays the size of "sessions ended early"
rather than growing forever. Password and MFA changes will revoke through the same list.

**Why there is no mode in here.** The token is identity only — `sub`, `jti`, `exp`. Which
mode a session is being served in changes several times within one token's life and must be
revocable on idle, so it lives in Redis against the `jti` (`auth/modes.py`) rather than in a
signed statement nothing can retract.

**Why the forced password change is enforced here.** `CurrentUser` is what every protected
route already depends on, so putting the check inside it is the only placement a route added
next year cannot forget. The three endpoints a flagged session must still reach ask for
`UnrestrictedUser` instead, and having to name it is the point: exemption is a decision
somebody writes down, not a default.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select

from auth.models import User
from core.config import get_settings
from core.db import SessionDep
from core.models import Business
from core.redis import get_redis

COOKIE_NAME = "linsuite_session"
ALGORITHM = "HS256"

_DENYLIST_PREFIX = "session:denied:"


@dataclass(frozen=True)
class IssuedToken:
    token: str
    jti: str
    expires_at: datetime


def issue_token(user_id: uuid.UUID | str, *, ttl: timedelta | None = None) -> IssuedToken:
    """Mint a session token. `ttl` is for tests; production uses the setting."""
    if ttl is None:
        ttl = timedelta(hours=get_settings().staff_session_hours)
    now = datetime.now(UTC)
    expires_at = now + ttl
    jti = uuid.uuid4().hex
    token = jwt.encode(
        # `iat` is a float, not the whole second PyJWT would encode a datetime as. It is
        # compared against `users.sessions_revoked_at` to decide whether this token survived
        # a password change, and at one-second resolution the replacement cookie issued by
        # `/auth/password/change` shares its second with the revocation that just happened —
        # so either the new session is born dead or the old one outlives the change.
        {"sub": str(user_id), "jti": jti, "iat": now.timestamp(), "exp": expires_at},
        get_settings().jwt_secret,
        algorithm=ALGORITHM,
    )
    return IssuedToken(token=token, jti=jti, expires_at=expires_at)


def decode_token(token: str) -> dict | None:
    """The claims, or None for anything we would not act on — bad signature, expired, forged."""
    try:
        return jwt.decode(
            token,
            get_settings().jwt_secret,
            algorithms=[ALGORITHM],
            options={"require": ["exp", "sub", "jti", "iat"]},
        )
    except jwt.InvalidTokenError:
        return None


async def revoke(claims: dict) -> None:
    """Deny this `jti` until the moment the token would have expired anyway."""
    remaining = int(claims["exp"] - datetime.now(UTC).timestamp())
    if remaining > 0:
        await get_redis().setex(_DENYLIST_PREFIX + claims["jti"], remaining, "1")


async def is_revoked(jti: str) -> bool:
    return bool(await get_redis().exists(_DENYLIST_PREFIX + jti))


def set_session_cookie(response: Response, issued: IssuedToken) -> None:
    response.set_cookie(
        COOKIE_NAME,
        issued.token,
        max_age=int((issued.expires_at - datetime.now(UTC)).total_seconds()),
        httponly=True,
        samesite="lax",
        secure=get_settings().cookie_secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        COOKIE_NAME, httponly=True, samesite="lax", secure=get_settings().cookie_secure, path="/"
    )


UNAUTHENTICATED = HTTPException(status_code=401, detail="Not authenticated")


def revoke_all(user: User) -> None:
    """End every session this user holds, including ones nobody is tracking.

    The `jti` denylist can only refuse a token somebody names, and no list of a user's live
    tokens exists — so the cut-off is an instant on the user instead, and `session_claims`
    refuses anything issued before it. Staged, not committed: the caller's transaction is
    what makes the revocation and the reason for it land together.
    """
    user.sessions_revoked_at = datetime.now(UTC)


async def session_claims(request: Request, db: SessionDep) -> dict:
    """The claims of a live session, or 401.

    Three ways a signed, unexpired token is still not a session: its `jti` is denylisted
    (logout), the account is gone (`current_user`), or it predates a password change. The
    last one costs a lookup on every authenticated request, and is why this dependency now
    touches the database at all — `logout` deliberately does not use it, so revoking a token
    still needs no round trip.
    """
    token = request.cookies.get(COOKIE_NAME)
    claims = decode_token(token) if token else None
    if claims is None or await is_revoked(claims["jti"]):
        raise UNAUTHENTICATED
    if await _predates_a_revocation(claims, db):
        raise UNAUTHENTICATED
    return claims


async def _predates_a_revocation(claims: dict, db: SessionDep) -> bool:
    try:
        user_id = uuid.UUID(claims["sub"])
    except (ValueError, TypeError):
        raise UNAUTHENTICATED from None
    cut_off = await db.scalar(select(User.sessions_revoked_at).where(User.id == user_id))
    return cut_off is not None and claims["iat"] < cut_off.timestamp()


ClaimsDep = Annotated[dict, Depends(session_claims)]


PASSWORD_CHANGE_REQUIRED = HTTPException(
    status_code=403, detail="Set a new password before you continue."
)


async def change_required(db: SessionDep, user: User) -> bool:
    """Whether this account must set a new password before it can do anything else.

    Two ways in: the flag an administrator or a suspected compromise sets (tech-stack §14),
    and — only where a business has opted into rotation at all — a password older than the
    configured interval. Rotation is evaluated per request rather than written into the flag
    by a nightly job, so turning the setting off takes effect at once instead of leaving the
    flag set on accounts nobody has touched since.

    The flag is checked first so the common answer costs no query at all; the rotation
    setting is a single-row lookup on a table that changes about never.
    ponytail: re-read per request, cache it if a profile ever says this matters.
    """
    if user.must_change_password:
        return True
    days = await db.scalar(select(Business.password_rotation_days).where(Business.id == 1))
    if not days:
        return False
    return user.password_changed_at < datetime.now(UTC) - timedelta(days=days)


async def unrestricted_user(claims: ClaimsDep, db: SessionDep) -> User:
    """The signed-in user, forced password change and all.

    Only three endpoints may use this, and each is one a flagged session has to reach to
    stop being flagged: `GET /auth/me` (which is how the frontend learns it must change),
    `POST /auth/password/change`, and logout — which needs no user at all and so never
    arrives here.
    """
    try:
        user_id = uuid.UUID(claims["sub"])
    except (ValueError, TypeError):
        raise UNAUTHENTICATED from None
    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        # A deleted account's token is still signed and still unexpired. It is not a session.
        raise UNAUTHENTICATED
    return user


UnrestrictedUser = Annotated[User, Depends(unrestricted_user)]


async def current_user(user: UnrestrictedUser, db: SessionDep) -> User:
    """The signed-in user of a session that is allowed to do things. Every protected route
    depends on this, directly or through a capability check layered on top of it.

    403 and not 401 while a change is owed: the session is real and the credential was
    correct. A 401 would send the frontend to `/login`, where signing in again would produce
    another session owing the same change — a loop, for someone who has done nothing wrong.
    """
    if await change_required(db, user):
        raise PASSWORD_CHANGE_REQUIRED
    return user


CurrentUser = Annotated[User, Depends(current_user)]

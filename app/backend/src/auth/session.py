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
        {"sub": str(user_id), "jti": jti, "iat": now, "exp": expires_at},
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
            options={"require": ["exp", "sub", "jti"]},
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


async def session_claims(request: Request) -> dict:
    """The claims of a live session, or 401. Separate from `current_user` so logout can
    revoke a token without a round trip to the database."""
    token = request.cookies.get(COOKIE_NAME)
    claims = decode_token(token) if token else None
    if claims is None or await is_revoked(claims["jti"]):
        raise UNAUTHENTICATED
    return claims


ClaimsDep = Annotated[dict, Depends(session_claims)]


async def current_user(claims: ClaimsDep, db: SessionDep) -> User:
    """The signed-in user. Every protected route depends on this, directly or through a
    capability check layered on top of it."""
    try:
        user_id = uuid.UUID(claims["sub"])
    except (ValueError, TypeError):
        raise UNAUTHENTICATED from None
    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        # A deleted account's token is still signed and still unexpired. It is not a session.
        raise UNAUTHENTICATED
    return user


CurrentUser = Annotated[User, Depends(current_user)]

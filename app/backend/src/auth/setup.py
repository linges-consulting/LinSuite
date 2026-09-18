"""First-run setup wizard (tech-stack §17).

A freshly deployed instance is unclaimed. Every boot mints a one-time token, logs it to
stdout (`docker compose logs app`) and writes it `0600` to disk; the wizard will not act
without it. Whoever finds the URL before the operator meets a prompt they cannot answer.

Completion writes `businesses.setup_completed_at`, and that database flag — not an
in-memory one — is what disables every route here, permanently and across restarts.
"""

import logging
import os
import secrets
from datetime import UTC, datetime
from functools import lru_cache
from zoneinfo import available_timezones

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.models import User
from core.config import get_settings
from core.db import SessionDep
from core.models import Business
from core.security import MIN_PASSWORD_LENGTH, hash_password

log = logging.getLogger(__name__)

# This boot's token. Deliberately process-local and never persisted: a restart invalidates
# it, and the operator reads the new one from the logs.
# Ceiling: one app process. Under `uvicorn --workers N` each worker would mint its own and
# only one would accept a given token; move it to a row in `businesses` if that day comes.
_token: str | None = None


async def setup_is_complete(session: AsyncSession) -> bool:
    return bool(
        await session.scalar(select(exists().where(Business.setup_completed_at.is_not(None))))
    )


async def bootstrap_setup_token(session: AsyncSession) -> str | None:
    """Called once per boot. Returns the new token, or None once setup has completed."""
    global _token
    if await setup_is_complete(session):
        _token = None
        return None

    _token = secrets.token_urlsafe(32)
    log.info("setup: this instance is unclaimed. Setup token: %s", _token)
    _write_token_file(_token)
    return _token


def forget_setup_token() -> None:
    global _token
    _token = None
    path = get_settings().setup_token_file
    try:
        os.remove(path)
    except OSError:
        pass


def _write_token_file(token: str) -> None:
    path = get_settings().setup_token_file
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # O_CREAT with mode 0600, then chmod in case the file already existed.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
        os.chmod(path, 0o600)
        log.info("setup: token also written to %s", path)
    except OSError:
        # A read-only or unwritable path must not stop the app booting — the token is in
        # the log either way, which is the documented retrieval path.
        log.warning("setup: could not write the token file at %s", path, exc_info=True)


@lru_cache
def _iana_timezones() -> list[str]:
    return sorted(available_timezones())


class SetupRequest(BaseModel):
    token: str = Field(min_length=1)
    business_name: str = Field(min_length=1, max_length=200)
    timezone: str
    admin_email: EmailStr
    admin_password: str = Field(min_length=MIN_PASSWORD_LENGTH)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        if value not in _iana_timezones():
            raise ValueError("not an IANA timezone name")
        return value


async def _setup_still_open(session: SessionDep) -> None:
    """Once setup is done this whole router stops existing, for good."""
    if await setup_is_complete(session):
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(prefix="/setup", tags=["setup"], dependencies=[Depends(_setup_still_open)])


@router.get("/status")
async def setup_status() -> dict[str, bool]:
    """Reachable only while setup is pending; afterwards this 404s, which means "done"."""
    return {"required": True}


@router.get("/timezones")
async def setup_timezones() -> dict[str, list[str]]:
    return {"timezones": _iana_timezones()}


@router.post("", status_code=201)
async def complete_setup(payload: SetupRequest, session: SessionDep) -> dict[str, str]:
    if _token is None or not secrets.compare_digest(payload.token, _token):
        log.warning("setup: rejected an attempt with an invalid token")
        raise HTTPException(status_code=403, detail="Invalid setup token")

    session.add(
        Business(
            id=1,
            name=payload.business_name.strip(),
            timezone=payload.timezone,
            setup_completed_at=datetime.now(UTC),
        )
    )
    session.add(
        User(
            email=payload.admin_email.lower(),
            password_hash=hash_password(payload.admin_password),
            is_admin=True,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        # The single-row CHECK/PK: a concurrent request got there first.
        await session.rollback()
        raise HTTPException(status_code=409, detail="Setup has already been completed") from None

    forget_setup_token()
    log.info("setup: completed for %r; the setup routes are now disabled", payload.business_name)
    return {"status": "complete"}

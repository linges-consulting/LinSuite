"""First-run setup wizard (tech-stack §17).

A freshly deployed instance is unclaimed. The first boot mints a token, logs it to stdout
(`docker compose logs app`) and writes it `0600` to disk; the wizard will not act without
it. Whoever finds the URL before the operator meets a prompt they cannot answer.

Only the token's digest is persisted, in `setup_token`. That is what makes every worker of
every restart agree on which token is valid, and it means a restart does not invalidate the
token the operator already copied out. A later boot re-mints only when the file has gone
missing, because a token nobody can read is a token nobody can finish setup with.

Completion writes `businesses.setup_completed_at`, and that database flag — not an
in-memory one — is what closes `POST /api/setup`, permanently and across restarts.
"""

import hashlib
import logging
import os
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import delete, exists, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.models import Role, SetupToken, User
from core.config import get_settings
from core.db import SessionDep
from core.errors import INVALID_SETUP_TOKEN, Forbidden
from core.models import Business
from core.security import check_password_policy, hash_password
from settings.timezones import canonical_timezones, is_canonical

log = logging.getLogger(__name__)


def _digest(token: str) -> str:
    # The token is 256 bits of urandom; there is no low-entropy secret here for a slow hash
    # to protect, and a plain digest can be compared in constant time.
    return hashlib.sha256(token.encode()).hexdigest()


async def setup_is_complete(session: AsyncSession) -> bool:
    return bool(
        await session.scalar(select(exists().where(Business.setup_completed_at.is_not(None))))
    )


async def bootstrap_setup_token(session: AsyncSession) -> str | None:
    """Called once per boot. Returns a newly minted token, or None if none was needed."""
    if await setup_is_complete(session):
        return None

    # Exactly one process may end up owning the token, so each branch lets the database
    # pick the winner and every loser returns without touching the file. The stored digest
    # is the authority: a file with no digest behind it is a leftover, not a token.
    stored = await session.scalar(select(SetupToken.token_hash))
    token = secrets.token_urlsafe(32)

    if stored is not None:
        if _token_file_exists():
            # Already minted and still readable. Restarting must not invalidate the token
            # the operator is part way through using.
            return _announce_existing_token()
        # The token is unusable now that nobody can read it, so replace it — but only if
        # this is still the digest we read. A sibling doing the same replacement wins here
        # and we keep our hands off the file.
        replaced = await session.execute(
            update(SetupToken)
            .values(token_hash=_digest(token))
            .where(SetupToken.token_hash == stored)
        )
        if replaced.rowcount == 0:
            await session.rollback()
            return _announce_existing_token()
        log.warning("setup: the token file was missing, so the setup token has been replaced")
    else:
        claimed = await session.execute(
            insert(SetupToken)
            .values(id=1, token_hash=_digest(token))
            .on_conflict_do_nothing(index_elements=["id"])
        )
        if claimed.rowcount == 0:
            # A sibling worker minted first; its token is the valid one and it writes the file.
            await session.rollback()
            return _announce_existing_token()

    await session.commit()
    log.info("setup: this instance is unclaimed. Setup token: %s", token)
    _write_token_file(token)
    return token


def _announce_existing_token() -> None:
    log.info(
        "setup: this instance is unclaimed. The setup token is in %s",
        get_settings().setup_token_file,
    )
    return None


# Every filesystem touch sits in a sync helper: these run at boot, before the server takes
# a connection, and on the one request that finishes setup.
def _token_file_exists() -> bool:
    return os.path.exists(get_settings().setup_token_file)


def _remove_token_file() -> None:
    try:
        os.remove(get_settings().setup_token_file)
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


class SetupRequest(BaseModel):
    token: str = Field(min_length=1)
    business_name: str = Field(min_length=1, max_length=200)
    timezone: str
    admin_email: EmailStr
    # Not validated here: the policy includes a breach lookup, which is async and belongs
    # in the handler. `core.security.check_password_policy` is the one authority, and this
    # wizard uses it exactly as the account screens will.
    admin_password: str

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        if not is_canonical(value):
            raise ValueError("not a current IANA timezone name")
        return value


async def _setup_still_open(session: SessionDep) -> None:
    """Completion closes this door for good; the read-only routes stay open."""
    if await setup_is_complete(session):
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(prefix="/setup", tags=["setup"])


@router.get("/status")
async def setup_status(session: SessionDep) -> dict[str, bool]:
    """Always answers: the frontend uses it to decide whether to show the wizard."""
    return {"required": not await setup_is_complete(session)}


@router.get("/timezones")
async def setup_timezones() -> dict[str, list[str]]:
    """Stays available after setup — changing the business timezone reuses this list.

    Canonical zones only (`settings/timezones.py`). The wizard and the Business screen offer
    the same list, because a zone one of them would refuse has no business being in the other.
    """
    return {"timezones": list(canonical_timezones())}


@router.post("", status_code=201, dependencies=[Depends(_setup_still_open)])
async def complete_setup(payload: SetupRequest, session: SessionDep) -> dict[str, str]:
    stored = await session.scalar(select(SetupToken.token_hash))
    if stored is None or not secrets.compare_digest(_digest(payload.token), stored):
        log.warning("setup: rejected an attempt with an invalid token")
        # Its own code rather than one of the session kinds: nobody is signed in here, so
        # "your session changed" is not a thing this can mean. The wizard shows it inline.
        raise Forbidden(INVALID_SETUP_TOKEN, "Invalid setup token")

    rejected = await check_password_policy(payload.admin_password)
    if rejected:
        # Shaped like FastAPI's own 422 so the frontend reads one error format, and — like
        # main.py's handler — carrying no trace of what was submitted.
        raise HTTPException(
            status_code=422,
            detail=[{"type": "value_error", "loc": ["body", "admin_password"], "msg": rejected}],
        )

    session.add(
        Business(
            id=1,
            name=payload.business_name.strip(),
            timezone=payload.timezone,
            setup_completed_at=datetime.now(UTC),
        )
    )
    # The Administrator role is seeded by migration 0006, so it exists before any instance
    # boots. Looked up by name rather than pinned to an id: the id is generated per database.
    administrator = await session.scalar(select(Role.id).where(Role.name == "Administrator"))
    session.add(
        User(
            email=payload.admin_email.lower(),
            password_hash=await hash_password(payload.admin_password),
            role_id=administrator,
        )
    )
    # The token dies with the request that used it, in the same transaction as the business.
    await session.execute(delete(SetupToken))
    try:
        await session.commit()
    except IntegrityError:
        # The single-row CHECK/PK: a concurrent request got there first.
        await session.rollback()
        raise HTTPException(status_code=409, detail="Setup has already been completed") from None

    _remove_token_file()
    log.info("setup: completed for %r; the setup routes are now disabled", payload.business_name)
    return {"status": "complete"}

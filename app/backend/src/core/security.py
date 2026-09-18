"""Password hashing and the one password policy (tech-stack §14).

Argon2id with argon2-cffi's defaults, which track the RFC 9106 recommendations. The
parameters live in the hash string, so raising them later only affects new hashes and
`PasswordHasher.check_needs_rehash` handles the rest — no migration.

Policy: 12 characters minimum, no composition rules (NIST SP 800-63B found forced
complexity produces predictable passwords), then breach screening. Every entry point that
accepts a new password calls `check_password_policy`; there is deliberately only one.

Hashing and verification are CPU-bound by design — that is the point of Argon2 — so both
run in a threadpool. Doing them inline would stall the event loop for every other request
for the duration of each login.
"""

import functools
import hashlib
import logging
from pathlib import Path

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from starlette.concurrency import run_in_threadpool

from core.config import get_settings

log = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 12

HIBP_RANGE_URL = "https://api.pwnedpasswords.com/range"
_HIBP_TIMEOUT = 3.0

_COMMON_PASSWORDS_FILE = Path(__file__).with_name("common_passwords.txt")

_hasher = PasswordHasher()

# Verified on every login attempt for an address that has no account, so that the response
# takes as long as a real rejection does. Without it the 401 for an unknown email returns
# in microseconds and the 401 for a wrong password takes ~50ms, which is an account
# enumeration oracle you can read over the network.
_DUMMY_HASH = _hasher.hash("timing-equalisation-only")


async def hash_password(password: str) -> str:
    return await run_in_threadpool(_hasher.hash, password)


async def verify_password(password_hash: str | None, password: str) -> bool:
    """False for a mismatch. Pass `None` to burn the same time on a nonexistent account."""
    return await run_in_threadpool(_verify, password_hash or _DUMMY_HASH, password)


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError):
        return False


async def check_password_policy(
    password: str, *, client: httpx.AsyncClient | None = None
) -> str | None:
    """The reason this password is unacceptable, or None. `client` is for tests only."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if await _is_breached(password, client):
        return (
            "This password has appeared in a known data breach. "
            "Choose one you have not used anywhere else."
        )
    return None


async def _is_breached(password: str, client: httpx.AsyncClient | None) -> bool:
    if get_settings().breach_check_enabled:
        breached = await _ask_hibp(password, client)
        if breached is not None:
            return breached
    return password.casefold() in _common_passwords()


async def _ask_hibp(password: str, client: httpx.AsyncClient | None) -> bool | None:
    """k-anonymity range query: only the first 5 hex characters of the hash leave here.

    None when the answer could not be obtained — an outage must not reject every password,
    nor accept every password, so the caller falls back to the bundled list.
    """
    digest = hashlib.sha1(password.encode()).hexdigest().upper()  # noqa: S324 — HIBP's format
    prefix, suffix = digest[:5], digest[5:]
    try:
        if client is not None:
            resp = await client.get(f"{HIBP_RANGE_URL}/{prefix}", timeout=_HIBP_TIMEOUT)
        else:
            async with httpx.AsyncClient(timeout=_HIBP_TIMEOUT) as owned:
                resp = await owned.get(f"{HIBP_RANGE_URL}/{prefix}")
        resp.raise_for_status()
    except httpx.HTTPError:
        log.warning("security: HaveIBeenPwned is unreachable; using the bundled list")
        return None
    return any(line.partition(":")[0].strip() == suffix for line in resp.text.splitlines())


@functools.lru_cache
def _common_passwords() -> frozenset[str]:
    """The offline fallback. Only entries of at least the minimum length earn their place —
    anything shorter is already refused on length."""
    lines = _COMMON_PASSWORDS_FILE.read_text(encoding="utf-8").splitlines()
    return frozenset(
        line.strip().casefold()
        for line in lines
        if line.strip() and not line.startswith("#") and len(line.strip()) >= MIN_PASSWORD_LENGTH
    )

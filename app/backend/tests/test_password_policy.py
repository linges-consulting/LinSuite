"""S2: the one password policy every entry point shares (tech-stack §14).

The HaveIBeenPwned client is the only external boundary in the codebase, so it is the only
thing here that is stubbed — with `httpx.MockTransport`, at the transport layer. The real
API is never called from a test.
"""

import hashlib

import httpx
import pytest

from core.config import get_settings
from core.security import check_password_policy

PASSPHRASE = "correct horse battery staple"
BREACHED = "passwordpassword"


def hibp(*plaintexts: str, status: int = 200) -> httpx.AsyncClient:
    """A stand-in for the range endpoint, answering with the suffixes of `plaintexts`."""
    by_prefix: dict[str, list[str]] = {}
    for plaintext in plaintexts:
        digest = hashlib.sha1(plaintext.encode()).hexdigest().upper()  # noqa: S324 — HIBP's format
        by_prefix.setdefault(digest[:5], []).append(digest[5:])

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status != 200:
            return httpx.Response(status)
        prefix = request.url.path.rsplit("/", 1)[-1]
        body = "\n".join(f"{s}:42" for s in by_prefix.get(prefix, ["0" * 35]))
        return httpx.Response(200, text=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.seen = seen  # type: ignore[attr-defined]
    return client


@pytest.fixture
def offline(monkeypatch):
    """Outbound checks disabled: the deployment posture of an on-premise install."""
    monkeypatch.setattr(get_settings(), "breach_check_enabled", False)


@pytest.fixture(autouse=True)
def outbound_allowed(database, monkeypatch):
    # `database` only for the environment it configures: `get_settings()` is cached for the
    # whole session, so it must be built from the same environment the S1 tests use.
    monkeypatch.setattr(get_settings(), "breach_check_enabled", True)


# --- length -----------------------------------------------------------------------------


async def test_a_password_under_twelve_characters_is_rejected():
    async with hibp() as client:
        assert await check_password_policy("eleven char", client=client)
    assert len("eleven char") == 11


async def test_a_long_passphrase_without_symbols_is_accepted():
    async with hibp() as client:
        assert await check_password_policy(PASSPHRASE, client=client) is None


async def test_length_is_counted_in_characters_not_bytes():
    async with hibp() as client:
        assert await check_password_policy("ααααααααααα", client=client)  # 11
        assert await check_password_policy("αααααααααααα", client=client) is None  # 12


# --- breach screening -------------------------------------------------------------------


async def test_a_known_breached_password_is_rejected():
    async with hibp(BREACHED) as client:
        reason = await check_password_policy(BREACHED, client=client)

    assert reason and "breach" in reason.lower()


async def test_only_the_first_five_characters_of_the_hash_leave_the_server():
    async with hibp(BREACHED) as client:
        await check_password_policy(BREACHED, client=client)
        (request,) = client.seen

    digest = hashlib.sha1(BREACHED.encode()).hexdigest().upper()  # noqa: S324
    url = str(request.url)
    assert url.endswith(f"/range/{digest[:5]}")
    assert digest[5:] not in url
    assert BREACHED not in url


async def test_a_short_password_is_rejected_without_asking_hibp_about_it():
    async with hibp() as client:
        await check_password_policy("short", client=client)
        assert client.seen == []


# --- the offline fallback ---------------------------------------------------------------


async def test_the_bundled_list_rejects_a_breached_password_with_outbound_checks_disabled(offline):
    async with hibp() as client:
        reason = await check_password_policy(BREACHED, client=client)

        assert reason and "breach" in reason.lower()
        # Disabled means disabled: nothing was sent.
        assert client.seen == []


async def test_a_good_passphrase_still_passes_with_outbound_checks_disabled(offline):
    assert await check_password_policy(PASSPHRASE) is None


async def test_the_bundled_list_ignores_casing(offline):
    assert await check_password_policy(BREACHED.upper()) is not None


async def test_an_unreachable_hibp_falls_back_to_the_bundled_list():
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unreachable)) as client:
        assert await check_password_policy(BREACHED, client=client) is not None
        # An outage must not lock out a password the bundled list does not know.
        assert await check_password_policy(PASSPHRASE, client=client) is None


async def test_an_erroring_hibp_falls_back_to_the_bundled_list():
    async with hibp(BREACHED, status=503) as client:
        assert await check_password_policy(BREACHED, client=client) is not None
        assert await check_password_policy(PASSPHRASE, client=client) is None

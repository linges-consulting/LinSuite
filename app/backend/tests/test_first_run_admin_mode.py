"""S1: the narrow first-run exception that lets a freshly-enrolled owner land in Admin Mode
without a second password prompt (#117, spec #113) — over a real PostgreSQL and Redis.

The instance this suite sets up is left on the *default* policy — `mfa_required_for_admin`
is `true` out of the box (`core/models.py`) — because that default, on an instance nothing
has touched yet, is exactly the scenario the feature exists for. `tests/test_modes.py` and
`tests/test_mfa.py` are where the ordinary "Admin Mode costs a password" rule is proven.
"""

from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from sqlalchemy import text

from auth import modes
from auth import session as session_mod
from core.db import session_scope
from core.redis import get_redis
from core.security import hash_password
from tests.conftest import add_account, get_owner_engine

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"
STAFF_EMAIL = "tech@cedar.example"

SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": "Owner@Cedar.example",
    "admin_password": PASSWORD,
}

COOKIE = "linsuite_session"
ADMIN_ENDPOINT = "/api/admin/business"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    """A freshly set-up instance, on the default policy nothing has changed yet."""
    async with get_owner_engine().begin() as owner:
        await owner.execute(text("DELETE FROM audit_events"))
    async with session_scope() as db:
        for table in ("mfa_recovery_codes", "users", "businesses", "setup_token"):
            await db.execute(text(f"DELETE FROM {table}"))
        await db.commit()
    await get_redis().flushdb()

    from auth import setup

    async with session_scope() as db:
        token = await setup.bootstrap_setup_token(db)
    resp = await client.post("/api/setup", json={**SETUP, "token": token})
    assert resp.status_code == 201, resp.text
    client.cookies.clear()
    yield


# --- helpers ------------------------------------------------------------------------------


async def login(client, email=EMAIL, password=PASSWORD):
    return await client.post("/api/auth/login", json={"email": email, "password": password})


async def add_staff_user(email=STAFF_EMAIL):
    await add_account(email, await hash_password(PASSWORD))


def jti(client) -> str:
    return session_mod.decode_token(client.cookies[COOKIE])["jti"]


async def me(client):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 200, resp.text
    return resp.json()


def code(secret: str, step: int = 0) -> str:
    return pyotp.TOTP(secret).at(datetime.now(UTC) + timedelta(seconds=step * 30))


async def enrol(client) -> str:
    """Start and confirm a TOTP enrolment with a real code. Returns the secret."""
    started = await client.post("/api/auth/mfa/enrol", json={})
    assert started.status_code == 200, started.text
    secret = started.json()["secret"]
    confirmed = await client.post("/api/auth/mfa/enrol/confirm", json={"code": code(secret)})
    assert confirmed.status_code == 200, confirmed.text
    return secret


async def audit() -> list[str]:
    async with session_scope() as db:
        rows = await db.execute(
            text("SELECT event_type FROM audit_events ORDER BY occurred_at, id")
        )
        return [row[0] for row in rows.all()]


async def dismiss_onboarding_directly() -> None:
    """The effect of `POST /admin/onboarding/dismiss`, reached without an open Admin Mode
    window — which is exactly the thing under test in this suite."""
    async with session_scope() as db:
        await db.execute(text("UPDATE businesses SET onboarding_dismissed_at = now()"))
        await db.commit()


# --- the grant fires exactly where the spec describes --------------------------------------


async def test_a_fresh_owner_lands_in_admin_mode_right_after_the_forced_enrolment(client):
    resp = await login(client)
    assert resp.json()["mfa"]["enrolment_required"] is True
    assert resp.json()["mode"] == "staff"

    await enrol(client)

    body = await me(client)
    assert body["mode"] == "admin"
    assert body["admin_grant_expires_at"] is not None
    assert body["admin_hard_limit_at"] is not None
    # And it behaves like any other admin window from here — no further password inside it.
    assert (await client.get(ADMIN_ENDPOINT)).status_code == 200


async def test_the_grant_is_recorded_in_the_audit_log(client):
    await login(client)

    await enrol(client)

    assert "mode.auto_admin_first_run" in await audit()


# --- the safety rules: old, stolen or already-settled sessions get nothing free ------------


async def test_a_session_past_the_freshness_window_gets_no_free_grant(client):
    """The shape "stolen cookie, used later": the password was typed minutes ago and the key
    that proves it has already expired — dropped directly here rather than waiting out
    `FRESH_LOGIN_MINUTES` for real."""
    await login(client)
    await get_redis().delete(modes.fresh_login_key(jti(client)))

    await enrol(client)

    body = await me(client)
    assert body["mode"] == "staff"
    assert body["admin_grant_expires_at"] is None
    # Admin Mode still costs exactly what it always does.
    assert (await client.post("/api/auth/mode", json={"mode": "admin"})).status_code == 403
    assert (
        await client.post("/api/auth/mode", json={"mode": "admin", "password": PASSWORD})
    ).status_code == 200


async def test_the_freshness_key_is_spent_at_most_once(client):
    """`GETDEL` is the whole mechanism: the second read of the same login's key finds nothing,
    however many places might ask."""
    await login(client)
    key = modes.fresh_login_key(jti(client))

    assert await get_redis().getdel(key) == "1"
    assert await get_redis().getdel(key) is None


async def test_a_business_that_has_dismissed_onboarding_never_grants_it(client):
    await login(client)
    await dismiss_onboarding_directly()

    await enrol(client)

    body = await me(client)
    assert body["mode"] == "staff"
    assert body["admin_grant_expires_at"] is None


async def test_a_voluntary_enrolment_never_grants_admin_mode(client):
    """With the policy off, nothing forces this enrolment — an administrator choosing a second
    factor from Security is not the fresh-install owner this feature exists for, and the
    session stays in whichever mode it was already using."""
    async with session_scope() as db:
        await db.execute(text("UPDATE businesses SET mfa_required_for_admin = false"))
        await db.commit()
    await login(client)
    assert (await me(client))["mfa"]["enrolment_required"] is False

    await enrol(client)

    body = await me(client)
    assert body["mode"] == "staff"
    assert body["admin_grant_expires_at"] is None


async def test_a_staff_only_account_is_never_pushed_into_it(client):
    """No `admin` capability, so the policy never forces this account to enrol at all — there
    is no forced enrolment for the grant to ride in on, whatever it later chooses to do."""
    await add_staff_user()
    await login(client, email=STAFF_EMAIL)

    assert (await me(client))["mfa"]["enrolment_required"] is False

    await enrol(client)  # a voluntary choice, same as any account may make

    body = await me(client)
    assert body["mode"] == "staff"
    assert body["can_switch_modes"] is False

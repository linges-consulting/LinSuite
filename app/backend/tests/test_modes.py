"""S1: the context switch between Staff Mode and Admin Mode, over a real PostgreSQL and Redis.

The admin window is state in Redis, not a claim in the token, so these tests reach into
Redis to move time rather than waiting for it — a 15-minute idle window cannot be tested by
being idle for 15 minutes. Expiry itself is proven once against a real TTL, in
`test_an_idle_window_really_expires_when_redis_drops_the_key`; everything else manipulates
the key so the assertions stay about the rule, not about the clock.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from auth import modes
from auth import session as session_mod
from core.config import get_settings
from core.db import get_purge_engine, session_scope
from core.redis import get_redis
from core.security import hash_password
from tests.conftest import add_account

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
    """Every test starts on a set-up instance, with nothing left in Redis from the last one."""
    async with get_purge_engine().begin() as purge:
        await purge.execute(text("DELETE FROM audit_events"))
    async with session_scope() as db:
        for table in ("users", "businesses", "setup_token"):
            await db.execute(text(f"DELETE FROM {table}"))
        await db.commit()
    await get_redis().flushdb()

    from auth import setup

    async with session_scope() as db:
        token = await setup.bootstrap_setup_token(db)
    resp = await client.post("/api/setup", json={**SETUP, "token": token})
    assert resp.status_code == 201, resp.text
    # These suites predate the second factor and are about other rules, so the instance they
    # set up has the MFA policy off. Task 8 defaults it on, which would otherwise put every
    # administrator here behind the enrolment gate before the rule under test is reached;
    # `tests/test_mfa.py` is where the policy itself is exercised.
    async with session_scope() as db:
        await db.execute(text("UPDATE businesses SET mfa_required_for_admin = false"))
        await db.commit()
    client.cookies.clear()
    yield


# --- helpers ------------------------------------------------------------------------------


async def login(client, email=EMAIL, password=PASSWORD):
    resp = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp


async def add_staff_user(email=STAFF_EMAIL):
    """A user on the seeded `Staff` role, which does not hold the `admin` capability."""
    await add_account(email, await hash_password(PASSWORD))


def jti(client) -> str:
    return session_mod.decode_token(client.cookies[COOKIE])["jti"]


def grant_key(client) -> str:
    return modes.grant_key(jti(client))


async def switch(client, mode, password=None):
    body = {"mode": mode}
    if password is not None:
        body["password"] = password
    return await client.post("/api/auth/mode", json=body)


async def enter_admin(client, password=PASSWORD):
    resp = await switch(client, "admin", password)
    assert resp.status_code == 200, resp.text
    return resp


async def me(client):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def audit() -> list[tuple]:
    async with session_scope() as db:
        return list(
            (
                await db.execute(
                    text(
                        "SELECT event_type, metadata::text FROM audit_events "
                        "ORDER BY occurred_at, id"
                    )
                )
            ).all()
        )


# --- what the switcher is drawn from ------------------------------------------------------


async def test_a_new_session_starts_in_staff_mode_with_no_admin_window(client):
    await login(client)

    body = await me(client)
    assert body["mode"] == "staff"
    assert body["admin_grant_expires_at"] is None
    assert body["admin_hard_limit_at"] is None


async def test_a_user_whose_role_holds_the_admin_capability_may_switch(client):
    await login(client)

    assert (await me(client))["can_switch_modes"] is True


async def test_a_staff_only_user_may_not_switch(client):
    await add_staff_user()

    await login(client, email=STAFF_EMAIL)

    body = await me(client)
    assert body["role"] == "Staff"
    assert "admin" not in body["capabilities"]
    assert body["can_switch_modes"] is False


async def test_login_answers_in_the_same_shape_as_me(client):
    resp = await login(client)

    assert resp.json() == await me(client)


# --- the admin endpoint -------------------------------------------------------------------


async def test_the_admin_endpoint_refuses_an_anonymous_request(client):
    assert (await client.get(ADMIN_ENDPOINT)).status_code == 401


async def test_the_admin_endpoint_refuses_staff_mode_even_for_an_administrator(client):
    await login(client)

    resp = await client.get(ADMIN_ENDPOINT)

    assert resp.status_code == 403
    assert PASSWORD not in resp.text


async def test_the_admin_endpoint_answers_in_admin_mode(client):
    await login(client)
    await enter_admin(client)

    resp = await client.get(ADMIN_ENDPOINT)

    assert resp.status_code == 200
    assert resp.json()["name"] == SETUP["business_name"]
    assert resp.json()["timezone"] == SETUP["timezone"]
    assert resp.json()["setup_completed_at"] is not None


# --- entering Admin Mode ------------------------------------------------------------------


async def test_entering_admin_mode_without_a_password_is_refused(client):
    await login(client)

    resp = await switch(client, "admin")

    # Not a 401: the session is perfectly valid, it is the elevation that is refused. A 401
    # here would log the browser out of a session it still holds.
    assert resp.status_code == 403
    assert (await me(client))["mode"] == "staff"


async def test_a_request_with_no_password_is_not_recorded_as_a_failed_login(client):
    """The expected race, not an attack: the frontend offers the free switch from the grant it
    last saw, and the window can lapse between that poll and the click. Writing `login.failed`
    for it would put an accusation nobody earned into an append-only log, and hand the
    escalating lockout a count that lets a user lock themselves out with one click."""
    await login(client)

    assert (await switch(client, "admin")).status_code == 403

    assert [event for event, _ in await audit()] == ["login.succeeded"]


async def test_only_a_password_that_was_actually_tried_records_a_failure(client):
    await login(client)

    await switch(client, "admin")  # nothing tried
    await switch(client, "admin", "not the password")  # tried, and wrong

    assert [event for event, _ in await audit()].count("login.failed") == 1


async def test_entering_admin_mode_with_the_wrong_password_is_refused(client):
    await login(client)

    resp = await switch(client, "admin", "not the password")

    assert resp.status_code == 403
    assert (await client.get(ADMIN_ENDPOINT)).status_code == 403


async def test_the_right_password_opens_the_window(client):
    await login(client)

    body = (await enter_admin(client)).json()

    assert body["mode"] == "admin"
    grant = datetime.fromisoformat(body["admin_grant_expires_at"])
    hard = datetime.fromisoformat(body["admin_hard_limit_at"])
    settings = get_settings()
    now = datetime.now(UTC)
    assert timedelta(minutes=settings.admin_idle_minutes) - (grant - now) < timedelta(seconds=30)
    assert timedelta(minutes=settings.admin_hard_limit_minutes) - (hard - now) < timedelta(
        seconds=30
    )


async def test_a_staff_only_user_cannot_enter_admin_mode_with_a_correct_password(client):
    await add_staff_user()
    await login(client, email=STAFF_EMAIL)

    resp = await switch(client, "admin", PASSWORD)

    assert resp.status_code == 403
    assert (await me(client))["mode"] == "staff"


async def test_the_password_is_never_echoed_back(client):
    await login(client)

    resp = await switch(client, "admin", "not the password")

    assert "not the password" not in resp.text


# --- the window slides, and stops ---------------------------------------------------------


async def test_switching_to_staff_and_back_inside_the_window_needs_no_password(client):
    await login(client)
    await enter_admin(client)

    back_to_staff = await switch(client, "staff")
    assert back_to_staff.status_code == 200
    assert back_to_staff.json()["mode"] == "staff"
    # The grant is untouched by leaving Admin Mode; that is what makes coming back free.
    assert back_to_staff.json()["admin_grant_expires_at"] is not None
    assert (await client.get(ADMIN_ENDPOINT)).status_code == 403

    again = await switch(client, "admin")

    assert again.status_code == 200
    assert again.json()["mode"] == "admin"
    assert (await client.get(ADMIN_ENDPOINT)).status_code == 200


async def test_an_admin_request_slides_the_window_forward(client):
    await login(client)
    await enter_admin(client)
    redis = get_redis()
    await redis.expire(grant_key(client), 30)  # 30 seconds left

    assert (await client.get(ADMIN_ENDPOINT)).status_code == 200

    assert await redis.ttl(grant_key(client)) > 30


async def test_the_window_never_slides_past_the_hard_limit(client):
    await login(client)
    await enter_admin(client)
    redis = get_redis()
    # A hard limit ten seconds away, with an idle window that would otherwise run for
    # fifteen minutes.
    hard = datetime.now(UTC) + timedelta(seconds=10)
    await redis.set(grant_key(client), str(hard.timestamp()), ex=900)

    assert (await client.get(ADMIN_ENDPOINT)).status_code == 200

    assert 0 < await redis.ttl(grant_key(client)) <= 10


async def test_the_hard_limit_ends_the_window_however_busy_the_session_is(client):
    await login(client)
    await enter_admin(client)
    # The idle window is nowhere near out — the hard limit is simply behind us.
    past = datetime.now(UTC) - timedelta(seconds=1)
    await get_redis().set(grant_key(client), str(past.timestamp()), ex=900)

    assert (await client.get(ADMIN_ENDPOINT)).status_code == 403
    assert (await me(client))["mode"] == "staff"
    assert (await switch(client, "admin")).status_code == 403
    assert (await switch(client, "admin", PASSWORD)).status_code == 200


async def test_an_idle_window_really_expires_when_redis_drops_the_key(client):
    await login(client)
    await enter_admin(client)
    await get_redis().pexpire(grant_key(client), 50)

    await asyncio.sleep(0.2)

    assert (await client.get(ADMIN_ENDPOINT)).status_code == 403
    assert (await me(client))["mode"] == "staff"
    assert (await me(client))["admin_grant_expires_at"] is None


async def test_an_expired_window_demands_the_password_again(client):
    await login(client)
    await enter_admin(client)
    await get_redis().delete(grant_key(client))

    assert (await switch(client, "admin")).status_code == 403

    assert (await switch(client, "admin", PASSWORD)).status_code == 200
    assert (await client.get(ADMIN_ENDPOINT)).status_code == 200


async def test_polling_me_in_admin_mode_does_not_hold_the_window_open(client):
    """The countdown is drawn from /me, refreshed every few seconds. If that refresh slid the
    window, an open browser tab would keep Admin Mode alive forever."""
    await login(client)
    await enter_admin(client)
    await get_redis().expire(grant_key(client), 30)

    for _ in range(3):
        await me(client)

    assert await get_redis().ttl(grant_key(client)) <= 30


# --- the staff session is the one that survives -------------------------------------------


async def test_the_staff_session_outlives_the_admin_window(client):
    await login(client)
    await enter_admin(client)
    await get_redis().delete(grant_key(client))

    body = await me(client)

    assert body["mode"] == "staff"
    assert body["email"] == EMAIL
    assert (await client.get("/api/health")).status_code == 200


async def test_logging_out_forgets_the_mode_state(client):
    await login(client)
    await enter_admin(client)
    keys = (grant_key(client), modes.mode_key(jti(client)))

    await client.post("/api/auth/logout", json={})

    # The token is revoked either way, but leaving the grant to time out on its own would
    # keep a live admin window in Redis for a session that ended.
    assert await get_redis().exists(*keys) == 0


async def test_a_second_login_starts_in_staff_mode(client):
    await login(client)
    await enter_admin(client)
    await client.post("/api/auth/logout", json={})
    client.cookies.clear()

    await login(client)

    assert (await me(client))["mode"] == "staff"
    assert (await client.get(ADMIN_ENDPOINT)).status_code == 403


async def test_one_sessions_admin_window_is_not_anothers(client):
    await login(client)
    await enter_admin(client)
    elevated = client.cookies[COOKIE]
    client.cookies.clear()
    await login(client)

    assert (await client.get(ADMIN_ENDPOINT)).status_code == 403

    client.cookies.set(COOKIE, elevated)
    assert (await client.get(ADMIN_ENDPOINT)).status_code == 200


# --- the audit log ------------------------------------------------------------------------


async def test_mode_switches_are_written_to_the_audit_log(client):
    await login(client)
    await enter_admin(client)
    await switch(client, "staff")

    assert [event for event, _ in await audit()] == [
        "login.succeeded",
        "admin.reauth",
        "mode.switched",
        "mode.switched",
    ]
    assert json.loads((await audit())[2][1])["mode"] == "admin"
    assert json.loads((await audit())[3][1])["mode"] == "staff"


async def test_a_free_switch_back_records_the_switch_but_not_a_reauth(client):
    await login(client)
    await enter_admin(client)
    await switch(client, "staff")
    await switch(client, "admin")

    assert [event for event, _ in await audit()].count("admin.reauth") == 1
    assert [event for event, _ in await audit()].count("mode.switched") == 3


async def test_a_failed_reauth_is_written_to_the_audit_log(client):
    await login(client)

    await switch(client, "admin", "hunter2 hunter2")

    event, metadata = (await audit())[-1]
    assert event == "login.failed"
    assert json.loads(metadata)["reason"] == "reauth"
    assert "hunter2" not in metadata

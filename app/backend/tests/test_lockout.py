"""S1 + S3: the progressive delay, the escalating lock, and who hears about it.

Over a real PostgreSQL and Redis. The lock *is* a Redis key with a TTL, so these tests move
the clock by moving the key — deleting the delay key is "the user waited", and `pexpire` on
the lock is the one place a real expiry is proven against a real clock. Nothing here waits
fifteen minutes for a fifteen-minute lock.

The settings are shrunk to a threshold of four and one-minute tiers by `short_settings`, so
an assertion is about the rule rather than about the shipped numbers.
"""

import asyncio
import json

import pytest
from sqlalchemy import text

from auth import throttle
from core.config import get_settings
from core.db import get_purge_engine, session_scope
from core.redis import get_redis
from core.security import hash_password

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"
NEW_PASSWORD = "a different long passphrase"
STAFF_EMAIL = "tech@cedar.example"
UNKNOWN = "nobody@cedar.example"

SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": "Owner@Cedar.example",
    "admin_password": PASSWORD,
}

COOKIE = "linsuite_session"

# What `short_settings` puts in place: four failures lock, the delay doubles 1, 2, 4 and the
# three offence tiers are a minute each apart.
THRESHOLD = 4
DELAY_CAP = 8
TIERS = [1, 2, 3]


@pytest.fixture(autouse=True)
def short_settings():
    settings = get_settings()
    original = {
        field: getattr(settings, field)
        for field in (
            "lockout_threshold",
            "lockout_delay_cap_seconds",
            "lockout_tier_minutes",
            "reset_request_limit",
        )
    }
    settings.lockout_threshold = THRESHOLD
    settings.lockout_delay_cap_seconds = DELAY_CAP
    settings.lockout_tier_minutes = TIERS
    settings.reset_request_limit = 3
    yield
    for field, value in original.items():
        setattr(settings, field, value)


@pytest.fixture(autouse=True)
async def claimed_instance(client, sent_emails):
    async with get_purge_engine().begin() as purge:
        await purge.execute(text("DELETE FROM audit_events"))
    async with session_scope() as db:
        for table in ("password_reset_tokens", "users", "businesses", "setup_token"):
            await db.execute(text(f"DELETE FROM {table}"))
        await db.commit()
    await get_redis().flushdb()

    from auth import setup

    async with session_scope() as db:
        token = await setup.bootstrap_setup_token(db)
    resp = await client.post("/api/setup", json={**SETUP, "token": token})
    assert resp.status_code == 201, resp.text
    client.cookies.clear()
    sent_emails.clear()
    yield


# --- helpers ------------------------------------------------------------------------------


async def login(client, password=PASSWORD, email=EMAIL):
    return await client.post("/api/auth/login", json={"email": email, "password": password})


async def change(client, current=PASSWORD, new=NEW_PASSWORD):
    return await client.post(
        "/api/auth/password/change", json={"current_password": current, "new_password": new}
    )


async def reauth(client, password):
    return await client.post("/api/auth/mode", json={"mode": "admin", "password": password})


async def waited(email=EMAIL) -> None:
    """The user came back later: the progressive delay has run out, nothing else has."""
    await get_redis().delete(throttle.delay_key(email))


async def burn(client, count: int, email=EMAIL):
    """`count` failed logins with the delay waited out between them."""
    resp = None
    for _ in range(count):
        await waited(email)
        resp = await login(client, password="not the password", email=email)
    return resp


async def add_staff_user(email=STAFF_EMAIL) -> str:
    digest = await hash_password(PASSWORD)
    async with session_scope() as db:
        row = await db.execute(
            text(
                "INSERT INTO users (email, password_hash, is_admin) "
                "VALUES (:e, :h, false) RETURNING id"
            ),
            {"e": email, "h": digest},
        )
        user_id = row.scalar_one()
        await db.commit()
    return str(user_id)


async def audit() -> list[tuple]:
    async with session_scope() as db:
        return list(
            (
                await db.execute(
                    text(
                        "SELECT event_type, metadata::text, actor_user_id FROM audit_events "
                        "ORDER BY occurred_at, id"
                    )
                )
            ).all()
        )


def retry_after(resp) -> int:
    assert "retry-after" in resp.headers, resp.headers
    return int(resp.headers["retry-after"])


# --- the progressive delay ------------------------------------------------------------------


async def test_a_second_attempt_straight_after_a_failure_is_refused(client):
    assert (await login(client, password="not the password")).status_code == 401

    resp = await login(client, password="not the password")

    assert resp.status_code == 429
    assert retry_after(resp) == 1
    assert "Too many attempts" in resp.json()["detail"]


async def test_the_delay_doubles_with_each_failure(client):
    delays = []
    for _ in range(THRESHOLD - 1):
        await waited()
        await login(client, password="not the password")
        delays.append(retry_after(await login(client, password="not the password")))

    assert delays == [1, 2, 4]


async def test_the_delay_refuses_the_right_password_too(client):
    """The delay is about the account, not about this guess. Letting a correct password
    through would turn the delay into an oracle for the one attempt that matters."""
    await login(client, password="not the password")

    resp = await login(client)

    assert resp.status_code == 429


async def test_a_successful_sign_in_clears_the_counter(client):
    await burn(client, THRESHOLD - 1)
    await waited()

    assert (await login(client)).status_code == 200

    await waited()
    assert (await login(client, password="not the password")).status_code == 401  # count is 1
    assert retry_after(await login(client, password="not the password")) == 1


async def test_a_delayed_attempt_is_not_written_to_the_audit_log(client):
    """A 429 costs nothing to produce and would otherwise be the cheapest way to fill an
    append-only table. The failure that caused it is recorded; the refusals are not."""
    await login(client, password="not the password")
    for _ in range(5):
        await login(client, password="not the password")

    assert [event for event, _, _ in await audit()].count("login.failed") == 1


# --- one counter for every password check ----------------------------------------------------


async def test_failures_on_login_mode_and_change_share_one_counter(client):
    assert (await login(client)).status_code == 200

    assert (await change(client, current="not the password")).status_code == 403
    await waited()
    assert (await reauth(client, "not the password")).status_code == 403
    await waited()
    assert (await login(client, password="not the password")).status_code == 401
    await waited()

    # The fourth failure is the threshold, wherever it came from.
    locked = await login(client, password="not the password")
    assert locked.status_code == 429
    assert "locked" in locked.json()["detail"]


async def test_a_signed_in_password_check_is_delayed_like_a_login(client):
    await login(client)

    assert (await change(client, current="not the password")).status_code == 403

    resp = await change(client, current="not the password")
    assert resp.status_code == 429
    assert retry_after(resp) == 1


async def test_a_reauth_with_no_password_is_neither_counted_nor_delayed(client):
    """The lapsed-window race, not an attempt: it never reaches a hash, so it must not reach
    the counter either — one click would otherwise cost a user a delay they never earned."""
    await login(client)

    for _ in range(THRESHOLD + 2):
        resp = await client.post("/api/auth/mode", json={"mode": "admin"})
        assert resp.status_code == 403

    assert (await login(client)).status_code == 200


# --- the lock -------------------------------------------------------------------------------


async def test_the_threshold_locks_the_account(client, sent_emails):
    resp = await burn(client, THRESHOLD)

    assert resp.status_code == 429
    assert "locked" in resp.json()["detail"]
    # Even the real password is refused, and refused the same way.
    await waited()
    right = await login(client)
    assert right.status_code == 429
    assert 0 < retry_after(right) <= TIERS[0] * 60


async def test_a_locked_account_unlocks_itself_when_the_key_expires(client):
    await burn(client, THRESHOLD)
    assert (await login(client)).status_code == 429

    await get_redis().pexpire(throttle.lock_key(EMAIL), 50)
    await asyncio.sleep(0.2)

    assert (await login(client)).status_code == 200


async def test_a_repeat_lockout_escalates_the_tier(client):
    ttls = []
    for _ in range(len(TIERS) + 1):
        await burn(client, THRESHOLD)
        ttls.append(await get_redis().ttl(throttle.lock_key(EMAIL)))
        await get_redis().delete(throttle.lock_key(EMAIL))  # the lock elapsed

    # 1, 2, 3 minutes — and the last tier is the ceiling, not a step to another one.
    assert [round(ttl / 60) for ttl in ttls] == [*TIERS, TIERS[-1]]


async def test_the_offence_tier_decays(client):
    await burn(client, THRESHOLD)
    await get_redis().delete(throttle.lock_key(EMAIL), throttle.tier_key(EMAIL))

    await burn(client, THRESHOLD)

    assert round(await get_redis().ttl(throttle.lock_key(EMAIL)) / 60) == TIERS[0]


async def test_the_tier_key_expires_on_its_own(client):
    await burn(client, THRESHOLD)

    assert (
        0
        < await get_redis().ttl(throttle.tier_key(EMAIL))
        <= get_settings().lockout_tier_decay_hours * 3600
    )


# --- per account, and only per account -------------------------------------------------------


async def test_one_account_is_not_locked_out_by_another(client):
    """Two people at one front desk, one NAT, one IP. Locking one must not touch the other."""
    await add_staff_user()
    await burn(client, THRESHOLD, email=STAFF_EMAIL)

    assert (await login(client, email=STAFF_EMAIL)).status_code == 429
    assert (await login(client)).status_code == 200


async def test_an_unknown_address_is_throttled_exactly_like_a_known_one(client):
    """The throttle is keyed by the digest of what was typed, so its behaviour cannot answer
    the question the 401 body refuses to."""
    known = await login(client, password="not the password")
    unknown = await login(client, email=UNKNOWN, password="not the password")
    assert (known.status_code, known.text) == (unknown.status_code, unknown.text)

    delayed_known = await login(client, password="not the password")
    delayed_unknown = await login(client, email=UNKNOWN, password="not the password")

    assert (delayed_known.status_code, delayed_known.text) == (
        delayed_unknown.status_code,
        delayed_unknown.text,
    )

    locked_known = await burn(client, THRESHOLD - 1)
    locked_unknown = await burn(client, THRESHOLD - 1, email=UNKNOWN)
    assert (locked_known.status_code, locked_known.text) == (
        locked_unknown.status_code,
        locked_unknown.text,
    )


# --- the administrator's early unlock --------------------------------------------------------


async def unlock(client, user_id: str):
    return await client.post(f"/api/admin/users/{user_id}/unlock", json={})


async def enter_admin(client):
    resp = await client.post("/api/auth/mode", json={"mode": "admin", "password": PASSWORD})
    assert resp.status_code == 200, resp.text


async def test_an_administrator_can_unlock_an_account_early(client):
    user_id = await add_staff_user()
    await burn(client, THRESHOLD, email=STAFF_EMAIL)
    assert (await login(client, email=STAFF_EMAIL)).status_code == 429

    await login(client)
    await enter_admin(client)
    assert (await unlock(client, user_id)).status_code == 204

    client.cookies.clear()
    assert (await login(client, email=STAFF_EMAIL)).status_code == 200


async def test_an_early_unlock_also_clears_the_offence_tier(client):
    """Otherwise the next lockout of an account an administrator just forgave would open at
    the escalated tier, and the forgiveness would be half a gesture."""
    user_id = await add_staff_user()
    await burn(client, THRESHOLD, email=STAFF_EMAIL)
    await login(client)
    await enter_admin(client)
    await unlock(client, user_id)
    client.cookies.clear()

    await burn(client, THRESHOLD, email=STAFF_EMAIL)

    assert round(await get_redis().ttl(throttle.lock_key(STAFF_EMAIL)) / 60) == TIERS[0]


async def test_the_unlock_is_audited_with_the_administrator_who_did_it(client):
    user_id = await add_staff_user()
    await burn(client, THRESHOLD, email=STAFF_EMAIL)
    await login(client)
    await enter_admin(client)

    await unlock(client, user_id)

    event, metadata, actor = (await audit())[-1]
    assert event == "account.unlocked"
    assert json.loads(metadata)["email"] == STAFF_EMAIL
    assert actor is not None and str(actor) != user_id


async def test_unlocking_needs_admin_mode(client):
    user_id = await add_staff_user()

    assert (await unlock(client, user_id)).status_code == 401

    await login(client)
    assert (await unlock(client, user_id)).status_code == 403

    await enter_admin(client)
    assert (await unlock(client, user_id)).status_code == 204


async def test_unlocking_an_account_that_is_not_locked_is_harmless(client):
    user_id = await add_staff_user()
    await login(client)
    await enter_admin(client)

    assert (await unlock(client, user_id)).status_code == 204


async def test_unlocking_an_unknown_user_is_a_404(client):
    await login(client)
    await enter_admin(client)

    resp = await unlock(client, "00000000-0000-0000-0000-000000000000")

    assert resp.status_code == 404


# --- what the account owner is told (S3) -----------------------------------------------------


async def test_a_lockout_notifies_the_account_owner(client, sent_emails):
    await burn(client, THRESHOLD)

    assert len(sent_emails) == 1
    message = sent_emails[0]
    assert message.to == EMAIL
    assert "locked" in message.text.lower()
    # The reopening time is the point of the message: it says the wait is finite.
    assert "minute" in message.text


async def test_a_lockout_on_an_unknown_address_sends_nothing(client, sent_emails):
    await burn(client, THRESHOLD, email=UNKNOWN)

    assert sent_emails == []


async def test_only_one_notice_per_lockout(client, sent_emails):
    await burn(client, THRESHOLD)
    for _ in range(3):
        await login(client, password="not the password")

    assert len(sent_emails) == 1


async def test_the_lockout_is_written_to_the_audit_log(client):
    await burn(client, THRESHOLD)

    locked = [e for e in await audit() if e[0] == "account.locked"]
    assert len(locked) == 1
    metadata = json.loads(locked[0][1])
    assert metadata["email"] == EMAIL
    assert metadata["tier"] == 1
    assert metadata["unlock_at"]


async def test_a_password_change_notifies_the_owner(client, sent_emails):
    await login(client)

    assert (await change(client)).status_code == 200

    assert [m.to for m in sent_emails] == [EMAIL]
    assert "password" in sent_emails[0].subject.lower()


async def test_a_completed_reset_notifies_the_owner(client, sent_emails):
    import re

    await client.post("/api/auth/password-reset/request", json={"email": EMAIL})
    token = re.search(r"token=([A-Za-z0-9_-]+)", sent_emails[0].text).group(1)
    sent_emails.clear()

    resp = await client.post(
        "/api/auth/password-reset/confirm", json={"token": token, "new_password": NEW_PASSWORD}
    )

    assert resp.status_code == 204
    assert [m.to for m in sent_emails] == [EMAIL]
    assert "changed" in sent_emails[0].subject.lower()


# --- the reset-request limit -----------------------------------------------------------------


async def request_reset(client, email=EMAIL):
    return await client.post("/api/auth/password-reset/request", json={"email": email})


async def test_reset_requests_are_limited_per_address(client, sent_emails):
    for _ in range(get_settings().reset_request_limit):
        assert (await request_reset(client)).status_code == 202

    resp = await request_reset(client)

    assert resp.status_code == 429
    assert retry_after(resp) > 0
    assert len(sent_emails) == get_settings().reset_request_limit


async def test_the_reset_limit_answers_an_unknown_address_identically(client):
    for _ in range(get_settings().reset_request_limit):
        await request_reset(client, email=UNKNOWN)

    over = await request_reset(client, email=UNKNOWN)
    known_over = [
        await request_reset(client) for _ in range(get_settings().reset_request_limit + 1)
    ][-1]

    assert (over.status_code, over.text) == (known_over.status_code, known_over.text)


async def test_one_mailbox_is_not_limited_by_another(client, sent_emails):
    await add_staff_user()
    for _ in range(get_settings().reset_request_limit + 1):
        await request_reset(client)

    assert (await request_reset(client, email=STAFF_EMAIL)).status_code == 202


async def test_a_reset_request_does_not_count_as_a_failed_attempt(client):
    """Asking for a link is not a guess at a password. Letting it feed the failure counter
    would let anyone lock any account they know the address of — the DoS the whole design
    refuses."""
    for _ in range(get_settings().reset_request_limit):
        await request_reset(client)

    assert (await login(client)).status_code == 200

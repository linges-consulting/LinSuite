"""S1: the second factor — enrolment, verification, recovery codes, email OTP and reset.

Over a real PostgreSQL and Redis, like every other auth suite. Two things here are reached
into directly rather than waited for: the session's MFA state, which lives in Redis beside
the mode and the admin grant, and the email OTP's TTL. Both are server state with a clock
on them, and a test that waited would be a test of the clock.

The TOTP codes are computed with `pyotp` against the secret the enrolment endpoint returned
— the same library the server verifies with, used the way an authenticator app would.
"""

import hashlib
import logging
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from sqlalchemy import text

from auth import mfa, throttle
from auth import session as session_mod
from core import crypto
from core.config import Settings, get_settings
from core.db import get_purge_engine, session_scope
from core.redis import get_redis
from core.security import hash_password

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
    """A set-up instance with the MFA policy off, so each test turns on only what it is about."""
    async with get_purge_engine().begin() as purge:
        await purge.execute(text("DELETE FROM audit_events"))
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
    await set_policy(required_for_admin=False)
    client.cookies.clear()
    yield


# --- helpers ------------------------------------------------------------------------------


async def set_policy(*, required_for_admin=None, email_otp_allowed=None):
    async with session_scope() as db:
        if required_for_admin is not None:
            await db.execute(
                text("UPDATE businesses SET mfa_required_for_admin = :v"),
                {"v": required_for_admin},
            )
        if email_otp_allowed is not None:
            await db.execute(
                text("UPDATE businesses SET mfa_email_otp_allowed = :v"), {"v": email_otp_allowed}
            )
        await db.commit()


async def login(client, email=EMAIL, password=PASSWORD):
    resp = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp


async def add_staff_user(email=STAFF_EMAIL):
    digest = await hash_password(PASSWORD)
    async with session_scope() as db:
        await db.execute(
            text(
                "INSERT INTO users (email, password_hash, role_id) "
                "VALUES (:e, :h, (SELECT id FROM roles WHERE name = 'Staff'))"
            ),
            {"e": email, "h": digest},
        )
        await db.commit()


def jti(client) -> str:
    return session_mod.decode_token(client.cookies[COOKIE])["jti"]


async def me(client):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 200, resp.text
    return resp.json()


def code(secret: str, step: int = 0) -> str:
    """The code for now, or for `step` steps either side of it.

    A test that needs a *second* code asks for the next step rather than the same one twice,
    which is what a person does when the last code has been used: they wait for the display
    to tick over. The replay rule that makes this necessary has its own tests below.
    """
    return pyotp.TOTP(secret).at(datetime.now(UTC) + timedelta(seconds=step * 30))


async def forget_spent_steps(email=EMAIL):
    """Forget which TOTP steps this account has spent — the effect of a minute passing,
    without the minute. Reached into for the same reason the admin window is: the rule under
    test is elsewhere, and only two steps either side of now are ever offerable."""
    async with session_scope() as db:
        uid = await db.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": email})
    await mfa.forget_used_steps(uid)


async def enrol(client, replacing: str | None = None) -> tuple[str, list[str]]:
    """Walk the whole enrolment: start, confirm with a real code, keep the recovery codes.

    `replacing` is a current code, which the endpoint requires when there is already a factor
    to replace. The spent steps are dropped at the end: confirming spends one, and a test
    that goes on to sign in is a test about signing in — in life, minutes pass in between.
    """
    started = await client.post(
        "/api/auth/mfa/enrol", json={"code": replacing} if replacing else {}
    )
    assert started.status_code == 200, started.text
    secret = started.json()["secret"]
    confirmed = await client.post("/api/auth/mfa/enrol/confirm", json={"code": code(secret)})
    assert confirmed.status_code == 200, confirmed.text
    await forget_spent_steps()
    return secret, confirmed.json()["recovery_codes"]


async def verify(client, code):
    return await client.post("/api/auth/mfa/verify", json={"code": code})


async def user_id() -> str:
    async with session_scope() as db:
        return str(await db.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": EMAIL}))


async def stored_secret() -> str | None:
    async with session_scope() as db:
        return await db.scalar(text("SELECT mfa_secret FROM users WHERE email = :e"), {"e": EMAIL})


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


async def event_types() -> list[str]:
    return [row[0] for row in await audit()]


# --- enrolment ------------------------------------------------------------------------------


async def test_enrolment_hands_back_a_provisioning_uri_issued_by_the_business(client):
    await login(client)

    started = await client.post("/api/auth/mfa/enrol", json={})

    body = started.json()
    assert started.status_code == 200, started.text
    uri = body["provisioning_uri"]
    assert uri.startswith("otpauth://totp/")
    # The issuer is what an authenticator app labels the entry with: this business, not a
    # product name, so somebody with two clinics on one phone can tell them apart.
    assert "issuer=Cedar%20Lane%20Clinic" in uri
    assert body["secret"] in uri


async def test_enrolment_does_not_take_effect_until_a_code_proves_the_app_has_it(client):
    await login(client)
    started = await client.post("/api/auth/mfa/enrol", json={})
    secret = started.json()["secret"]

    # Nothing has been proved yet: an account enrolled here would be locked out of itself
    # by a mis-scanned QR code.
    assert (await me(client))["mfa"]["enrolled"] is False

    wrong = await client.post("/api/auth/mfa/enrol/confirm", json={"code": "000000"})
    assert wrong.status_code == 403, wrong.text
    assert wrong.json()["code"] == "invalid_mfa_code"
    assert (await me(client))["mfa"]["enrolled"] is False

    right = await client.post("/api/auth/mfa/enrol/confirm", json={"code": code(secret)})
    assert right.status_code == 200, right.text
    assert len(right.json()["recovery_codes"]) == get_settings().mfa_recovery_code_count
    assert (await me(client))["mfa"]["enrolled"] is True
    assert "mfa.enrolled" in await event_types()


async def test_starting_a_second_enrolment_leaves_the_live_one_working(client):
    """The candidate secret waits in Redis, so abandoning an enrolment costs nothing.

    Writing it straight to `users.mfa_secret` would overwrite the secret the authenticator
    on somebody's phone is using — and a person who opened the enrolment screen, thought
    better of it and closed the tab would be locked out of their own account.
    """
    await login(client)
    secret, _ = await enrol(client)

    # Started with a current code, and then abandoned.
    started = await client.post("/api/auth/mfa/enrol", json={"code": code(secret)})
    assert started.status_code == 200, started.text

    client.cookies.clear()
    await login(client)
    assert (await verify(client, code(secret, 1))).status_code == 200


async def test_the_secret_is_encrypted_at_rest_and_round_trips(client):
    await login(client)
    secret, _ = await enrol(client)

    at_rest = await stored_secret()

    assert at_rest is not None
    # A database dump is not a set of working authenticators.
    assert secret not in at_rest
    assert at_rest != secret
    assert mfa.decrypt_secret(at_rest) == secret


async def test_enrolment_tells_the_owner_it_happened(client, sent_emails):
    await login(client)
    await enrol(client)

    assert [m.to for m in sent_emails] == [EMAIL]
    assert "second factor" in sent_emails[0].text.lower()


# --- the pending-session gate -----------------------------------------------------------------


async def test_a_login_by_an_enrolled_account_is_pending_until_it_verifies(client):
    await login(client)
    secret, _ = await enrol(client)
    client.cookies.clear()

    body = (await login(client)).json()

    assert body["mfa"]["pending"] is True
    assert (await me(client))["mfa"]["pending"] is True


async def test_a_pending_session_reaches_only_verify_me_and_logout(client):
    await login(client)
    secret, _ = await enrol(client)
    client.cookies.clear()
    await login(client)

    refused = [
        await client.get(ADMIN_ENDPOINT),
        await client.post("/api/auth/mode", json={"mode": "admin", "password": PASSWORD}),
        await client.post(
            "/api/auth/password/change",
            json={"current_password": PASSWORD, "new_password": "another good passphrase"},
        ),
        await client.post("/api/auth/mfa/enrol", json={}),
        await client.get("/api/auth/mfa"),
    ]
    for resp in refused:
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "mfa_verification_required", resp.text

    # The three that must stay open, or the session could never stop being pending.
    assert (await client.get("/api/auth/me")).status_code == 200
    assert (await verify(client, code(secret))).status_code == 200
    assert (await client.post("/api/auth/logout", json={})).status_code == 204


async def test_a_valid_code_clears_pending_and_records_when(client):
    await login(client)
    secret, _ = await enrol(client)
    client.cookies.clear()
    await login(client)

    verified = await verify(client, code(secret))

    assert verified.status_code == 200, verified.text
    assert verified.json()["mfa"]["pending"] is False
    assert verified.json()["mfa"]["verified_at"] is not None
    assert (await client.get(ADMIN_ENDPOINT)).status_code in (200, 403)
    assert (await me(client))["mfa"]["pending"] is False


async def test_a_code_from_the_previous_step_is_still_accepted(client):
    """±1 window: a phone a few seconds out of step, or a person typing slowly."""
    await login(client)
    secret, _ = await enrol(client)
    client.cookies.clear()
    await login(client)

    previous = code(secret, -1)

    assert (await verify(client, previous)).status_code == 200


async def test_a_code_two_windows_old_is_not(client):
    await login(client)
    secret, _ = await enrol(client)
    client.cookies.clear()
    await login(client)

    stale = code(secret, -4)

    refused = await verify(client, stale)
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "invalid_mfa_code"


# --- one code, once (RFC 6238 §5.2) ---------------------------------------------------------


async def test_a_code_is_accepted_once_and_never_again(client):
    """A code is good for ninety seconds, and this product checks second factors at two
    different doors. Without spending the step, a code seen over a shoulder at the Admin Mode
    dialog is still good at the verify endpoint, on somebody else's session, for the rest of
    its window."""
    await login(client)
    secret, _ = await enrol(client)
    client.cookies.clear()
    await login(client)
    used = code(secret)

    assert (await verify(client, used)).status_code == 200

    # The same session, immediately.
    await get_redis().delete(throttle.delay_key(EMAIL))
    again = await verify(client, used)
    assert again.status_code == 403, again.text
    assert again.json()["code"] == "invalid_mfa_code"


async def test_a_spent_code_is_refused_on_a_different_session_too(client):
    """The step belongs to the account, not to the session that spent it — which is the whole
    point, since the replay would arrive on a session the attacker controls."""
    await login(client)
    secret, _ = await enrol(client)
    client.cookies.clear()
    await login(client)
    used = code(secret)
    assert (await verify(client, used)).status_code == 200

    # A second sign-in, as the attacker holding the code they watched being typed.
    client.cookies.clear()
    await login(client)
    await get_redis().delete(throttle.delay_key(EMAIL))

    replayed = await verify(client, used)

    assert replayed.status_code == 403, replayed.text
    assert replayed.json()["code"] == "invalid_mfa_code"
    # Refused like any other bad code, so it costs the attacker the lockout counter.
    assert "mfa.failed" in await event_types()
    assert await get_redis().exists(throttle.fail_key(EMAIL))


async def test_the_next_code_still_works_after_one_is_spent(client):
    """Spending a step must not lock the account out of the next one — the display ticks
    over and the person types what it now says."""
    await login(client)
    secret, _ = await enrol(client)
    client.cookies.clear()
    await login(client)
    assert (await verify(client, code(secret))).status_code == 200

    client.cookies.clear()
    await login(client)

    assert (await verify(client, code(secret, 1))).status_code == 200


async def test_the_code_that_confirmed_an_enrolment_cannot_then_sign_in(client):
    """The confirming code is a code for the secret that goes live one line later."""
    await login(client)
    started = await client.post("/api/auth/mfa/enrol", json={})
    secret = started.json()["secret"]
    confirming = code(secret)
    assert (
        await client.post("/api/auth/mfa/enrol/confirm", json={"code": confirming})
    ).status_code == 200
    client.cookies.clear()
    await login(client)

    replayed = await verify(client, confirming)

    assert replayed.status_code == 403, replayed.text


# --- a failed second factor is a failed authentication ----------------------------------------


async def test_a_wrong_code_feeds_the_one_failure_counter_and_locks_the_account(client):
    await login(client)
    await enrol(client)
    client.cookies.clear()
    await login(client)

    threshold = get_settings().lockout_threshold
    for _ in range(threshold - 1):
        refused = await verify(client, "000000")
        assert refused.status_code in (403, 429), refused.text
        # The progressive delay is the lockout suite's subject; clear it so this test is
        # about the count, not about waiting.
        await get_redis().delete(throttle.delay_key(EMAIL))

    locked = await verify(client, "000000")

    assert locked.status_code == 429, locked.text
    assert locked.headers["X-Account-Locked"] == "1"
    assert await get_redis().exists(throttle.lock_key(EMAIL))
    assert "account.locked" in await event_types()


async def test_a_failure_is_audited_without_the_code_that_was_tried(client):
    await login(client)
    await enrol(client)
    client.cookies.clear()
    await login(client)

    await verify(client, "123456")

    failures = [meta for kind, meta in await audit() if kind == "mfa.failed"]
    assert failures, await event_types()
    assert "123456" not in failures[-1]


async def test_a_verified_code_clears_the_failure_run(client):
    await login(client)
    secret, _ = await enrol(client)
    client.cookies.clear()
    await login(client)
    await verify(client, "000000")
    await get_redis().delete(throttle.delay_key(EMAIL))

    assert (await verify(client, code(secret))).status_code == 200
    assert not await get_redis().exists(throttle.fail_key(EMAIL))


async def test_a_locked_account_is_refused_before_the_code_is_checked(client):
    await login(client)
    secret, _ = await enrol(client)
    client.cookies.clear()
    await login(client)
    await get_redis().set(throttle.lock_key(EMAIL), "1", ex=900)

    refused = await verify(client, code(secret))

    assert refused.status_code == 429, refused.text


# --- replacing a factor that already exists ---------------------------------------------------


async def test_replacing_a_live_factor_costs_a_current_code(client):
    """Everything else behind the second factor is protected by it; swapping the factor
    itself was the one place a hijacked live session could quietly move the account onto
    somebody else's phone. Having verified this session at some point is not the same as
    holding the device now."""
    await login(client)
    secret, _ = await enrol(client)

    without = await client.post("/api/auth/mfa/enrol", json={})
    assert without.status_code == 403, without.text
    assert without.json()["code"] == "mfa_required"

    wrong = await client.post("/api/auth/mfa/enrol", json={"code": "000000"})
    assert wrong.status_code == 403, wrong.text
    assert wrong.json()["code"] == "invalid_mfa_code"

    await get_redis().delete(throttle.delay_key(EMAIL))
    with_code = await client.post("/api/auth/mfa/enrol", json={"code": code(secret)})
    assert with_code.status_code == 200, with_code.text


async def test_a_recovery_code_is_enough_to_replace_a_lost_authenticator(client):
    """The phone is the thing that is gone, so the code it produces cannot be the only way
    to replace it."""
    await login(client)
    _, codes = await enrol(client)

    replaced = await client.post("/api/auth/mfa/enrol", json={"code": codes[0]})

    assert replaced.status_code == 200, replaced.text


async def test_a_first_enrolment_asks_for_nothing(client):
    await login(client)

    started = await client.post("/api/auth/mfa/enrol", json={})

    assert started.status_code == 200, started.text


async def test_moving_onto_emailed_codes_costs_a_current_code_too(client):
    await set_policy(email_otp_allowed=True)
    await login(client)
    secret, _ = await enrol(client)

    without = await client.post("/api/auth/mfa/enrol/email", json={})
    assert without.status_code == 403, without.text
    assert without.json()["code"] == "mfa_required"

    with_code = await client.post("/api/auth/mfa/enrol/email", json={"code": code(secret)})
    assert with_code.status_code == 202, with_code.text


# --- recovery codes ---------------------------------------------------------------------------


async def test_a_recovery_code_authenticates_once_and_is_then_spent(client, sent_emails):
    await login(client)
    _, codes = await enrol(client)
    client.cookies.clear()
    sent_emails.clear()
    await login(client)

    first = await verify(client, codes[0])
    assert first.status_code == 200, first.text
    assert first.json()["mfa"]["pending"] is False
    assert "mfa.recovery_code_used" in await event_types()
    # Spending one is exactly the event somebody whose device was stolen should hear about.
    assert [m.to for m in sent_emails] == [EMAIL]

    client.cookies.clear()
    await login(client)
    await get_redis().delete(throttle.delay_key(EMAIL))
    again = await verify(client, codes[0])

    assert again.status_code == 403, again.text
    assert again.json()["code"] == "invalid_mfa_code"


async def test_only_digests_of_recovery_codes_are_stored(client):
    await login(client)
    _, codes = await enrol(client)

    async with session_scope() as db:
        stored = list(await db.scalars(text("SELECT code_hash FROM mfa_recovery_codes")))

    assert codes[0] not in stored
    assert hashlib.sha256(codes[0].encode()).hexdigest() in stored


async def test_the_security_page_counts_what_is_left_rather_than_showing_the_codes(client):
    await login(client)
    _, codes = await enrol(client)
    client.cookies.clear()
    await login(client)
    await verify(client, codes[0])

    status = (await client.get("/api/auth/mfa")).json()

    assert status["recovery_codes_remaining"] == len(codes) - 1
    assert "recovery_codes" not in status


async def test_regenerating_invalidates_every_earlier_code(client):
    await login(client)
    _, codes = await enrol(client)

    regenerated = await client.post("/api/auth/mfa/recovery-codes", json={})
    assert regenerated.status_code == 200, regenerated.text
    fresh = regenerated.json()["recovery_codes"]
    assert set(fresh).isdisjoint(codes)
    assert "mfa.recovery_codes_regenerated" in await event_types()

    client.cookies.clear()
    await login(client)
    stale = await verify(client, codes[0])
    assert stale.status_code == 403, stale.text

    await get_redis().delete(throttle.delay_key(EMAIL))
    assert (await verify(client, fresh[0])).status_code == 200


# --- Admin Mode ---------------------------------------------------------------------------------


async def test_admin_mode_needs_no_code_right_after_the_login_challenge(client):
    """The login prompt *is* the twelve-hourly one; asking twice inside a minute is the
    unusable policy the PRD rejects."""
    await login(client)
    secret, _ = await enrol(client)
    client.cookies.clear()
    await login(client)
    await verify(client, code(secret))

    entered = await client.post("/api/auth/mode", json={"mode": "admin", "password": PASSWORD})

    assert entered.status_code == 200, entered.text
    assert entered.json()["mode"] == "admin"


async def test_switching_back_and_forth_never_asks_again(client):
    await login(client)
    secret, _ = await enrol(client)
    client.cookies.clear()
    await login(client)
    await verify(client, code(secret))
    await client.post("/api/auth/mode", json={"mode": "admin", "password": PASSWORD})

    await client.post("/api/auth/mode", json={"mode": "staff"})
    back = await client.post("/api/auth/mode", json={"mode": "admin"})

    assert back.status_code == 200, back.text
    assert back.json()["mode"] == "admin"


async def test_admin_mode_demands_a_code_once_the_twelve_hours_have_passed(client):
    await login(client)
    secret, _ = await enrol(client)
    client.cookies.clear()
    await login(client)
    await verify(client, code(secret))
    # Reach in and age the verification, rather than waiting twelve hours for it.
    await mfa.age_verification(
        jti(client),
        datetime.now(UTC) - timedelta(hours=get_settings().admin_mfa_interval_hours + 1),
    )

    without = await client.post("/api/auth/mode", json={"mode": "admin", "password": PASSWORD})
    assert without.status_code == 403, without.text
    assert without.json()["code"] == "mfa_required"

    await get_redis().delete(throttle.delay_key(EMAIL))
    with_code = await client.post(
        "/api/auth/mode",
        json={"mode": "admin", "password": PASSWORD, "totp": code(secret, 1)},
    )
    assert with_code.status_code == 200, with_code.text
    assert with_code.json()["mode"] == "admin"


async def test_a_wrong_code_on_the_mode_switch_is_refused_and_counted(client):
    await login(client)
    secret, _ = await enrol(client)
    client.cookies.clear()
    await login(client)
    await verify(client, code(secret))
    await mfa.age_verification(jti(client), datetime.now(UTC) - timedelta(hours=24))

    refused = await client.post(
        "/api/auth/mode", json={"mode": "admin", "password": PASSWORD, "totp": "000000"}
    )

    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "invalid_mfa_code"
    assert await get_redis().exists(throttle.fail_key(EMAIL))
    assert (await me(client))["mode"] == "staff"


async def test_an_unenrolled_account_is_never_asked_for_a_code(client):
    await login(client)

    entered = await client.post("/api/auth/mode", json={"mode": "admin", "password": PASSWORD})

    assert entered.status_code == 200, entered.text


# --- the per-business policy ----------------------------------------------------------------------


async def test_with_the_policy_off_an_unenrolled_administrator_is_not_pushed_into_it(client):
    await set_policy(required_for_admin=False)
    await login(client)

    assert (await me(client))["mfa"]["enrolment_required"] is False
    assert (
        await client.post("/api/auth/mode", json={"mode": "admin", "password": PASSWORD})
    ).status_code == 200


async def test_with_the_policy_on_an_unenrolled_administrator_reaches_only_enrolment(client):
    await set_policy(required_for_admin=True)
    await login(client)

    assert (await me(client))["mfa"]["enrolment_required"] is True
    refused = await client.get(ADMIN_ENDPOINT)
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "mfa_enrolment_required"

    # Enrolling is the way out, so the enrolment endpoints stay open. Confirming counts as
    # presenting a code, so opening an admin window straight afterwards asks only for the
    # password — being challenged twice inside a minute is the policy the PRD rejects.
    await enrol(client)
    assert (await me(client))["mfa"]["enrolment_required"] is False
    await client.post("/api/auth/mode", json={"mode": "admin", "password": PASSWORD})
    assert (await client.get(ADMIN_ENDPOINT)).status_code == 200


async def test_the_policy_does_not_reach_staff_who_cannot_administer(client):
    await set_policy(required_for_admin=True)
    await add_staff_user()
    await login(client, email=STAFF_EMAIL)

    assert (await me(client))["mfa"]["enrolment_required"] is False
    # A staff account is refused the admin endpoint for the ordinary reason, not this one.
    refused = await client.get(ADMIN_ENDPOINT)
    assert refused.json()["code"] != "mfa_enrolment_required"


async def test_the_policy_is_read_and_written_from_the_security_settings(client):
    await login(client)
    await client.post("/api/auth/mode", json={"mode": "admin", "password": PASSWORD})

    read = await client.get("/api/admin/business/security")
    assert read.status_code == 200, read.text
    assert read.json() == {"mfa_required_for_admin": False, "mfa_email_otp_allowed": False}

    written = await client.patch(
        "/api/admin/business/security",
        json={"mfa_required_for_admin": True, "mfa_email_otp_allowed": True},
    )
    assert written.status_code == 200, written.text
    assert written.json()["mfa_email_otp_allowed"] is True
    assert "business.security_changed" in await event_types()


async def test_the_security_settings_need_admin_mode(client):
    await login(client)

    refused = await client.get("/api/admin/business/security")

    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "admin_mode_required"


# --- email OTP --------------------------------------------------------------------------------


async def test_email_otp_is_refused_while_recovery_codes_remain_and_the_policy_is_off(client):
    await login(client)
    await enrol(client)
    client.cookies.clear()
    await login(client)

    refused = await client.post("/api/auth/mfa/email-otp/request", json={})

    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "mfa_email_otp_not_allowed"


async def test_email_otp_is_the_fallback_once_every_recovery_code_is_spent(client, sent_emails):
    await login(client)
    _, codes = await enrol(client)
    async with session_scope() as db:
        await db.execute(text("UPDATE mfa_recovery_codes SET used_at = now()"))
        await db.commit()
    client.cookies.clear()
    sent_emails.clear()
    await login(client)

    asked = await client.post("/api/auth/mfa/email-otp/request", json={})
    assert asked.status_code == 202, asked.text
    assert [m.to for m in sent_emails] == [EMAIL]
    code = _code_from(sent_emails[-1].text)

    verified = await verify(client, code)
    assert verified.status_code == 200, verified.text
    assert verified.json()["mfa"]["pending"] is False
    assert "mfa.email_otp_requested" in await event_types()


async def test_an_email_code_works_once(client, sent_emails):
    await set_policy(email_otp_allowed=True)
    await login(client)
    await enrol(client)
    client.cookies.clear()
    sent_emails.clear()
    await login(client)
    await client.post("/api/auth/mfa/email-otp/request", json={})
    code = _code_from(sent_emails[-1].text)
    await verify(client, code)

    client.cookies.clear()
    await login(client)
    await get_redis().delete(throttle.delay_key(EMAIL))
    again = await verify(client, code)

    assert again.status_code == 403, again.text


async def test_an_email_code_expires(client, sent_emails):
    await set_policy(email_otp_allowed=True)
    await login(client)
    await enrol(client)
    client.cookies.clear()
    sent_emails.clear()
    await login(client)
    await client.post("/api/auth/mfa/email-otp/request", json={})
    code = _code_from(sent_emails[-1].text)
    # The expiry is a Redis TTL, so dropping the key is exactly what running out does.
    await get_redis().delete(mfa.otp_key(await user_id()))

    assert (await verify(client, code)).status_code == 403


async def test_asking_for_too_many_codes_is_refused_without_locking_the_account(client):
    await set_policy(email_otp_allowed=True)
    await login(client)
    await enrol(client)
    client.cookies.clear()
    await login(client)

    for _ in range(get_settings().reset_request_limit):
        assert (await client.post("/api/auth/mfa/email-otp/request", json={})).status_code == 202
    refused = await client.post("/api/auth/mfa/email-otp/request", json={})

    assert refused.status_code == 429, refused.text
    # Asking is not guessing: it must never be able to lock somebody out.
    assert not await get_redis().exists(throttle.lock_key(EMAIL))


async def test_a_business_that_allows_it_can_enrol_email_as_the_second_factor(client, sent_emails):
    await set_policy(email_otp_allowed=True)
    await login(client)
    sent_emails.clear()

    started = await client.post("/api/auth/mfa/enrol/email", json={})
    assert started.status_code == 202, started.text
    code = _code_from(sent_emails[-1].text)

    confirmed = await client.post("/api/auth/mfa/enrol/email/confirm", json={"code": code})
    assert confirmed.status_code == 200, confirmed.text
    assert len(confirmed.json()["recovery_codes"]) == get_settings().mfa_recovery_code_count

    status = (await client.get("/api/auth/mfa")).json()
    assert status["method"] == "email"
    assert status["enrolled"] is True


async def test_the_enrolment_send_is_limited_like_the_sign_in_one(client):
    """It sends mail from inside a session, so it needs the limiter its sign-in twin has —
    the two being written a hundred lines apart is exactly how one of them ends up without
    it."""
    await set_policy(email_otp_allowed=True)
    await login(client)

    for _ in range(get_settings().reset_request_limit):
        assert (await client.post("/api/auth/mfa/enrol/email", json={})).status_code == 202
    refused = await client.post("/api/auth/mfa/enrol/email", json={})

    assert refused.status_code == 429, refused.text
    assert not await get_redis().exists(throttle.lock_key(EMAIL))


async def test_the_two_kinds_of_send_share_one_bucket_and_not_the_reset_one(client):
    """One bucket for the codes, because they are the same message from one address's point
    of view — and a separate one from password reset, so a forgotten password cannot use up
    the codes somebody needs to get back into an account whose authenticator is gone."""
    await set_policy(email_otp_allowed=True)
    await login(client)
    for _ in range(get_settings().reset_request_limit):
        await client.post("/api/auth/mfa/enrol/email", json={})

    assert (await client.post("/api/auth/mfa/enrol/email", json={})).status_code == 429
    # The reset form still answers, because it counts separately.
    asked = await client.post("/api/auth/password-reset/request", json={"email": EMAIL})
    assert asked.status_code == 202, asked.text


async def test_email_enrolment_is_refused_where_the_business_has_not_allowed_it(client):
    await login(client)

    refused = await client.post("/api/auth/mfa/enrol/email", json={})

    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "mfa_email_otp_not_allowed"


def _code_from(message: str) -> str:
    """The six digits out of the message the fake provider recorded."""
    import re

    match = re.search(r"\b(\d{6})\b", message)
    assert match, message
    return match.group(1)


# --- administrative reset ---------------------------------------------------------------------


async def test_an_administrator_reset_clears_enrolment_and_ends_every_session(client):
    await login(client)
    secret, codes = await enrol(client)
    client.cookies.clear()
    await login(client)
    await verify(client, code(secret))
    await client.post("/api/auth/mode", json={"mode": "admin", "password": PASSWORD})
    target = await user_id()

    reset = await client.post(f"/api/admin/users/{target}/mfa/reset", json={})
    assert reset.status_code == 204, reset.text

    # The session that performed it is gone too: `sessions_revoked_at` cuts every token.
    assert (await client.get("/api/auth/me")).status_code == 401

    async with session_scope() as db:
        row = (
            await db.execute(
                text("SELECT mfa_secret, mfa_method, sessions_revoked_at FROM users WHERE id = :i"),
                {"i": target},
            )
        ).one()
    assert row[0] is None and row[1] is None and row[2] is not None
    async with session_scope() as db:
        assert await db.scalar(text("SELECT count(*) FROM mfa_recovery_codes")) == 0

    kinds = await event_types()
    assert "mfa.reset" in kinds
    # And the codes it destroyed are worthless.
    client.cookies.clear()
    await login(client)
    assert (await me(client))["mfa"]["enrolled"] is False


async def test_the_reset_takes_the_redis_state_with_it(client):
    """A staged candidate outliving a reset would be an enrolment somebody could still
    confirm on an account an administrator has just taken the factor off."""
    await login(client)
    secret, _ = await enrol(client)
    target = await user_id()
    started = await client.post("/api/auth/mfa/enrol", json={"code": code(secret)})
    assert started.status_code == 200, started.text
    assert await get_redis().exists(mfa.enrolment_key(target))
    await client.post("/api/auth/mode", json={"mode": "admin", "password": PASSWORD})

    await client.post(f"/api/admin/users/{target}/mfa/reset", json={})

    assert not await get_redis().exists(mfa.enrolment_key(target))
    assert [key async for key in get_redis().scan_iter(match=f"mfa:used:{target}:*")] == []


async def test_two_accounts_may_hold_the_same_recovery_code_digest(client):
    """Unique per account, not globally. A global constraint turns a 1-in-2^40 collision
    into an INSERT that fails in the middle of somebody's enrolment for no stated reason."""
    await add_staff_user()
    async with session_scope() as db:
        ids = list(await db.scalars(text("SELECT id FROM users ORDER BY email")))
        for user in ids:
            await db.execute(
                text(
                    "INSERT INTO mfa_recovery_codes (user_id, code_hash) "
                    "VALUES (:u, 'the same digest')"
                ),
                {"u": user},
            )
        await db.commit()

    async with session_scope() as db:
        assert await db.scalar(text("SELECT count(*) FROM mfa_recovery_codes")) == len(ids)


async def test_the_reset_names_the_administrator_who_did_it_and_tells_the_owner(
    client, sent_emails
):
    await login(client)
    admin_secret, _ = await enrol(client)
    await add_staff_user()
    async with session_scope() as db:
        target = str(
            await db.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": STAFF_EMAIL})
        )
    client.cookies.clear()
    await login(client)
    await verify(client, code(admin_secret))
    await client.post("/api/auth/mode", json={"mode": "admin", "password": PASSWORD})
    sent_emails.clear()

    await client.post(f"/api/admin/users/{target}/mfa/reset", json={})

    async with session_scope() as db:
        actor = await db.scalar(
            text("SELECT actor_user_id FROM audit_events WHERE event_type = 'mfa.reset'")
        )
        admin_id = await db.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": EMAIL})
    assert actor == admin_id
    assert [m.to for m in sent_emails] == [STAFF_EMAIL]


async def test_the_reset_needs_the_capability_and_admin_mode(client):
    await login(client)
    target = await user_id()

    refused = await client.post(f"/api/admin/users/{target}/mfa/reset", json={})

    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "admin_mode_required"


# --- the coded-403 invariant ------------------------------------------------------------------


async def test_every_403_this_feature_emits_names_its_kind(client):
    await set_policy(required_for_admin=True)
    await login(client)

    enrolment_gate = await client.get(ADMIN_ENDPOINT)
    started = await client.post("/api/auth/mfa/enrol", json={})
    assert started.status_code == 200
    secret = started.json()["secret"]
    bad_enrolment_code = await client.post("/api/auth/mfa/enrol/confirm", json={"code": "000000"})
    email_not_allowed = await client.post("/api/auth/mfa/enrol/email", json={})

    await client.post("/api/auth/mfa/enrol/confirm", json={"code": code(secret)})
    await forget_spent_steps()
    client.cookies.clear()
    await login(client)
    pending_gate = await client.get(ADMIN_ENDPOINT)
    await get_redis().delete(throttle.delay_key(EMAIL))
    bad_verify = await verify(client, "000000")
    await get_redis().delete(throttle.delay_key(EMAIL))
    await verify(client, code(secret))
    await mfa.age_verification(jti(client), datetime.now(UTC) - timedelta(hours=24))
    totp_demanded = await client.post(
        "/api/auth/mode", json={"mode": "admin", "password": PASSWORD}
    )

    for resp, expected in (
        (enrolment_gate, "mfa_enrolment_required"),
        (bad_enrolment_code, "invalid_mfa_code"),
        (email_not_allowed, "mfa_email_otp_not_allowed"),
        (pending_gate, "mfa_verification_required"),
        (bad_verify, "invalid_mfa_code"),
        (totp_demanded, "mfa_required"),
    ):
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == expected, resp.text
        assert isinstance(resp.json()["detail"], str)


async def test_there_is_no_sms_path_anywhere_in_the_product(client):
    """tech-stack §14 and CLAUDE.md: SMS MFA is never implemented.

    Prose about *not* implementing it is exactly what the docstrings here say, so this looks
    for the shapes an implementation would take instead — an identifier, a settings field, a
    route — rather than for the three letters.
    """
    from main import app
    from notifications.providers import PROVIDERS, NotificationProvider

    def named(names) -> list[str]:
        return [name for name in names if "sms" in name.lower()]

    assert named(path for path in app.openapi()["paths"]) == []
    assert named(dir(NotificationProvider)) == []
    assert named(Settings.model_fields) == []
    for factory in PROVIDERS.values():
        assert named(dir(factory)) == []


async def test_an_unreadable_secret_is_refused_rather_than_raised(client, caplog):
    """The escrow failure, made diagnosable.

    A wrong or rotated `MFA_ENCRYPTION_KEY` means GCM will not authenticate the stored
    ciphertext. Raising there is a 500 on every sign-in that says nothing about the cause;
    the refusal plus a log line naming it is what tells an administrator to restore the key
    or reset every enrolment.
    """
    await login(client)
    secret, _ = await enrol(client)
    client.cookies.clear()
    await login(client)
    # The column, rewritten under a key this deployment does not have.
    async with session_scope() as db:
        await db.execute(
            text("UPDATE users SET mfa_secret = :s WHERE email = :e"),
            {"s": crypto.encrypt(secret, "ab" * 32), "e": EMAIL},
        )
        await db.commit()

    with caplog.at_level(logging.ERROR):
        refused = await verify(client, code(secret))

    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "invalid_mfa_code"
    assert "MFA_ENCRYPTION_KEY" in caplog.text

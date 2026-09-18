"""S1 + S3: self-service password reset, the forced change, and the notification seam.

Over a real PostgreSQL and Redis, with outbound mail captured at the provider protocol
(`sent_emails`) rather than at an HTTP client — the sending path has no idea what transport
is underneath it, which is the whole point of the seam.
"""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from auth import throttle
from core.db import get_purge_engine, session_scope
from core.redis import get_redis
from core.security import verify_password

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"
NEW_PASSWORD = "a different long passphrase"

SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": "Owner@Cedar.example",
    "admin_password": PASSWORD,
}

COOKIE = "linsuite_session"


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


async def rows(sql: str, **params) -> list[tuple]:
    async with session_scope() as db:
        return list((await db.execute(text(sql), params)).all())


async def audit() -> list[tuple]:
    return await rows(
        "SELECT event_type, target_type, metadata::text FROM audit_events ORDER BY occurred_at, id"
    )


async def login(client, password=PASSWORD, email=EMAIL):
    return await client.post("/api/auth/login", json={"email": email, "password": password})


async def request_reset(client, email=EMAIL):
    return await client.post("/api/auth/password-reset/request", json={"email": email})


def link_token(sent_emails) -> str:
    """The token as a user would get it: pulled out of the link in the message."""
    assert len(sent_emails) == 1, sent_emails
    match = re.search(r"/reset-password\?token=([A-Za-z0-9_-]+)", sent_emails[0].text)
    assert match, sent_emails[0].text
    return match.group(1)


async def confirm(client, token, password=NEW_PASSWORD):
    return await client.post(
        "/api/auth/password-reset/confirm", json={"token": token, "new_password": password}
    )


async def set_flag(column: str, value) -> None:
    async with session_scope() as db:
        await db.execute(text(f"UPDATE users SET {column} = :v"), {"v": value})
        await db.commit()


# --- requesting a reset ---------------------------------------------------------------------


async def test_a_request_for_a_known_address_sends_exactly_one_link(client, sent_emails):
    resp = await request_reset(client)

    assert resp.status_code == 202
    assert len(sent_emails) == 1
    message = sent_emails[0]
    assert message.to == EMAIL
    assert "http://test.linsuite.example/reset-password?token=" in message.text
    # The link is usable: it carries the token the confirm endpoint will accept.
    assert await confirm(client, link_token(sent_emails)) is not None


async def test_only_the_digest_of_the_token_is_stored(client, sent_emails):
    await request_reset(client)
    token = link_token(sent_emails)

    stored = await rows("SELECT token_hash, used_at FROM password_reset_tokens")
    assert len(stored) == 1
    assert stored[0][0] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in stored[0][0]
    assert stored[0][1] is None


async def test_an_unknown_address_answers_the_same_and_sends_nothing(client, sent_emails):
    unknown = await request_reset(client, email="nobody@cedar.example")

    assert unknown.status_code == 202
    assert sent_emails == []
    assert await rows("SELECT id FROM password_reset_tokens") == []
    # Never an audit row naming an address nobody registered.
    assert [e for e in await audit() if e[0] == "password.reset_requested"] == []

    # And the answer is indistinguishable from the one a real account gets.
    known = await request_reset(client)
    assert (unknown.status_code, unknown.text) == (known.status_code, known.text)


async def test_the_request_is_audited_for_a_known_account(client, sent_emails):
    await request_reset(client)

    requested = [e for e in await audit() if e[0] == "password.reset_requested"]
    assert len(requested) == 1
    assert json.loads(requested[0][2])["email"] == EMAIL


# --- completing a reset -----------------------------------------------------------------


async def test_a_completed_reset_sets_the_new_password(client, sent_emails):
    await request_reset(client)

    resp = await confirm(client, link_token(sent_emails))

    assert resp.status_code == 204, resp.text
    assert (await login(client, password=NEW_PASSWORD)).status_code == 200
    assert (await login(client, password=PASSWORD)).status_code == 401
    assert [e[0] for e in await audit()].count("password.reset_completed") == 1


async def test_a_completed_reset_revokes_every_outstanding_session(client, sent_emails):
    assert (await login(client)).status_code == 200
    stolen = client.cookies[COOKIE]
    assert (await client.get("/api/auth/me")).status_code == 200

    await request_reset(client)
    assert (await confirm(client, link_token(sent_emails))).status_code == 204

    client.cookies.clear()
    client.cookies.set(COOKIE, stolen)
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_an_expired_token_is_refused(client, sent_emails):
    await request_reset(client)
    token = link_token(sent_emails)
    async with session_scope() as db:
        await db.execute(
            text("UPDATE password_reset_tokens SET expires_at = :t"),
            {"t": datetime.now(UTC) - timedelta(seconds=1)},
        )
        await db.commit()

    resp = await confirm(client, token)

    assert resp.status_code == 400
    assert (await login(client, password=NEW_PASSWORD)).status_code == 401


async def test_a_token_cannot_be_used_twice(client, sent_emails):
    await request_reset(client)
    token = link_token(sent_emails)
    assert (await confirm(client, token)).status_code == 204

    again = await confirm(client, token, password="yet another long passphrase")

    assert again.status_code == 400
    assert (await login(client, password="yet another long passphrase")).status_code == 401


async def test_an_older_outstanding_link_dies_with_the_reset(client, sent_emails):
    await request_reset(client)
    first = link_token(sent_emails)
    sent_emails.clear()
    await request_reset(client)
    second = link_token(sent_emails)
    assert (await confirm(client, second)).status_code == 204

    assert (await confirm(client, first, password="a third long passphrase")).status_code == 400


async def test_a_reset_applies_the_password_policy(client, sent_emails):
    await request_reset(client)

    resp = await confirm(client, link_token(sent_emails), password="short")

    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "new_password"]
    # Refused, so the token is still live for a second try.
    assert (await confirm(client, link_token(sent_emails))).status_code == 204


async def test_an_unknown_token_is_refused(client):
    assert (await confirm(client, "not-a-token-anyone-issued")).status_code == 400


# --- changing a password while signed in ---------------------------------------------------


async def change(client, current=PASSWORD, new=NEW_PASSWORD):
    return await client.post(
        "/api/auth/password/change", json={"current_password": current, "new_password": new}
    )


async def test_changing_a_password_needs_the_current_one(client):
    await login(client)

    resp = await change(client, current="not the password")

    assert resp.status_code == 403
    # That failure started the progressive delay (`tests/test_lockout.py`), so this is the
    # user coming back after it: the point here is that the new password never took.
    await get_redis().delete(throttle.delay_key(EMAIL))
    assert (await login(client, password=NEW_PASSWORD)).status_code == 401


async def test_a_failed_change_is_not_filed_as_a_failed_login(client):
    """A signed-in user fumbling their own current password did not attempt a login, and the
    trail should say which door was tried. The throttle counts it all the same — it keeps its
    own state rather than scanning these rows (`tests/test_lockout.py`)."""
    await login(client)

    await change(client, current="not the password")

    types = [e[0] for e in await audit()]
    assert types.count("password.change_failed") == 1
    assert "login.failed" not in types[types.index("login.succeeded") :]


async def test_a_change_applies_the_password_policy(client):
    await login(client)

    resp = await change(client, new="short")

    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "new_password"]


async def test_a_change_keeps_the_caller_signed_in_and_drops_the_other_sessions(client):
    await login(client)
    other_tab = client.cookies[COOKIE]
    client.cookies.clear()
    await login(client)

    resp = await change(client)

    assert resp.status_code == 200, resp.text
    assert resp.json()["must_change_password"] is False
    # The cookie was replaced in the same response, so this tab never noticed.
    assert client.cookies[COOKIE] != other_tab
    assert (await client.get("/api/auth/me")).status_code == 200

    client.cookies.clear()
    client.cookies.set(COOKIE, other_tab)
    assert (await client.get("/api/auth/me")).status_code == 401
    assert [e[0] for e in await audit()].count("password.changed") == 1


async def test_a_change_stores_a_new_hash_and_records_when(client):
    await login(client)
    before = (await rows("SELECT password_hash, password_changed_at FROM users"))[0]

    assert (await change(client)).status_code == 200

    after = (await rows("SELECT password_hash, password_changed_at FROM users"))[0]
    assert after[0] != before[0]
    assert await verify_password(after[0], NEW_PASSWORD)
    assert after[1] > before[1]


# --- the forced change ---------------------------------------------------------------------


async def test_must_change_password_surfaces_on_me_and_is_cleared_by_a_change(client):
    await set_flag("must_change_password", True)
    assert (await login(client)).json()["must_change_password"] is True
    assert (await client.get("/api/auth/me")).json()["must_change_password"] is True

    assert (await change(client)).status_code == 200

    assert (await client.get("/api/auth/me")).json()["must_change_password"] is False


async def test_a_completed_reset_clears_the_forced_change(client, sent_emails):
    await set_flag("must_change_password", True)
    await request_reset(client)
    assert (await confirm(client, link_token(sent_emails))).status_code == 204

    assert (await login(client, password=NEW_PASSWORD)).json()["must_change_password"] is False


async def test_rotation_is_off_by_default(client):
    assert (await rows("SELECT password_rotation_days FROM businesses"))[0][0] is None

    await set_flag("password_changed_at", datetime.now(UTC) - timedelta(days=3650))

    assert (await login(client)).json()["must_change_password"] is False


async def test_a_configured_rotation_forces_a_change_once_the_password_is_stale(client):
    async with session_scope() as db:
        await db.execute(text("UPDATE businesses SET password_rotation_days = 90"))
        await db.commit()

    await set_flag("password_changed_at", datetime.now(UTC) - timedelta(days=89))
    assert (await login(client)).json()["must_change_password"] is False

    await set_flag("password_changed_at", datetime.now(UTC) - timedelta(days=91))
    assert (await login(client)).json()["must_change_password"] is True

    # Changing it satisfies the rotation without an administrator touching the flag.
    assert (await change(client)).json()["must_change_password"] is False


# --- the notification seam (S3) -------------------------------------------------------------


async def test_delivery_goes_through_the_celery_task_not_the_handler(client, sent_emails):
    """The handler enqueues; the task resolves the provider and sends. Eager mode collapses
    the broker hop, so what the fake recorded is what the worker would have sent."""
    from notifications.tasks import send_email

    send_email.delay("someone@cedar.example", "Subject", "Body", None)

    assert [(m.to, m.subject, m.text) for m in sent_emails] == [
        ("someone@cedar.example", "Subject", "Body")
    ]


async def test_the_console_provider_writes_the_message_to_the_log(caplog):
    """How a developer retrieves a reset link locally: it is in `docker compose logs`."""
    from notifications.providers import ConsoleProvider

    with caplog.at_level("INFO"):
        ConsoleProvider().send_email("someone@cedar.example", "Reset", "http://x/reset?token=abc")

    assert "someone@cedar.example" in caplog.text
    assert "http://x/reset?token=abc" in caplog.text


# --- the server-side gate on a flagged session ----------------------------------------------


async def enter_admin_mode(client, password=PASSWORD):
    resp = await client.post("/api/auth/mode", json={"mode": "admin", "password": password})
    assert resp.status_code == 200, resp.text


async def test_a_flagged_session_is_refused_everywhere_but_the_three_ways_out(client):
    """The flag is not advice to the frontend. A session owing a change is refused server-side
    on every endpoint that is not one of the three it needs to stop owing it."""
    await login(client)
    await enter_admin_mode(client)
    await set_flag("must_change_password", True)

    # Blocked: an ordinary protected endpoint, and the mode switch. The detail is asserted
    # because a live admin window answers 403 too — this has to be the gate refusing, not
    # the mode guard.
    for resp in (
        await client.get("/api/admin/business"),
        await client.post("/api/auth/mode", json={"mode": "staff"}),
    ):
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Set a new password before you continue."

    # Reachable: the endpoint that reports the debt, and the one that settles it.
    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["must_change_password"] is True
    assert (await change(client)).status_code == 200


async def test_the_gate_lifts_the_moment_the_password_is_changed(client):
    await login(client)
    await set_flag("must_change_password", True)
    assert (await client.post("/api/auth/mode", json={"mode": "staff"})).status_code == 403

    assert (await change(client)).status_code == 200

    assert (await client.post("/api/auth/mode", json={"mode": "staff"})).status_code == 200
    await enter_admin_mode(client, password=NEW_PASSWORD)
    assert (await client.get("/api/admin/business")).status_code == 200


async def test_logging_out_of_a_flagged_session_still_works(client):
    """The third exemption. It takes no user at all, so it never reaches the gate — this is
    what proves nobody is trapped in a session they cannot complete or leave."""
    await login(client)
    await set_flag("must_change_password", True)

    assert (await client.post("/api/auth/logout", json={})).status_code == 204
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_an_expired_rotation_closes_the_same_gate(client):
    """The rotation setting is not a second, weaker rule — it reaches the same guard."""
    async with session_scope() as db:
        await db.execute(text("UPDATE businesses SET password_rotation_days = 90"))
        await db.commit()
    await login(client)
    assert (await client.post("/api/auth/mode", json={"mode": "staff"})).status_code == 200

    await set_flag("password_changed_at", datetime.now(UTC) - timedelta(days=91))

    refused = await client.post("/api/auth/mode", json={"mode": "staff"})
    assert refused.status_code == 403
    assert refused.json()["detail"] == "Set a new password before you continue."

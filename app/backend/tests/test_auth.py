"""S1: password login, the Staff Mode session, and logout — over a real PostgreSQL and Redis."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import text

from auth import session as session_mod
from core.config import get_settings
from core.db import get_purge_engine, session_scope
from core.redis import get_redis

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"

SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": "Owner@Cedar.example",
    "admin_password": PASSWORD,
}

COOKIE = "linsuite_session"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    """Every test starts on an instance whose setup wizard has already created the admin."""
    # Only the purge role may remove audit history — the app role is refused, which is what
    # test_the_application_role_cannot_rewrite_or_erase_an_audit_event proves.
    async with get_purge_engine().begin() as purge:
        await purge.execute(text("DELETE FROM audit_events"))
    async with session_scope() as db:
        for table in ("users", "businesses", "setup_token"):
            await db.execute(text(f"DELETE FROM {table}"))
        await db.commit()
    # Including the per-account throttle counters: a test that ends on a failed login would
    # otherwise leave the next one's first attempt already delayed.
    await get_redis().flushdb()

    from auth import setup

    async with session_scope() as db:
        token = await setup.bootstrap_setup_token(db)
    resp = await client.post("/api/setup", json={**SETUP, "token": token})
    assert resp.status_code == 201, resp.text
    client.cookies.clear()
    yield


async def rows(sql: str) -> list[tuple]:
    async with session_scope() as db:
        return list((await db.execute(text(sql))).all())


async def audit() -> list[tuple]:
    return await rows(
        "SELECT event_type, target_type, metadata::text, actor_user_id IS NOT NULL "
        "FROM audit_events ORDER BY occurred_at, id"
    )


async def login(client, **overrides):
    return await client.post(
        "/api/auth/login", json={"email": EMAIL, "password": PASSWORD, **overrides}
    )


async def logout(client):
    # `json={}` so the request carries `Content-Type: application/json`, exactly as the
    # frontend's one `post()` helper does. A bodiless POST is refused by design.
    return await client.post("/api/auth/logout", json={})


# --- login ------------------------------------------------------------------------------


async def test_correct_credentials_return_a_working_session(client):
    resp = await login(client)

    assert resp.status_code == 200
    assert resp.json()["email"] == EMAIL
    assert resp.json()["is_admin"] is True
    assert client.cookies[COOKIE]

    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == EMAIL


async def test_the_session_cookie_is_http_only_and_same_site_lax(client):
    resp = await login(client)

    (header,) = resp.headers.get_list("set-cookie")
    assert "httponly" in header.lower()
    assert "samesite=lax" in header.lower()
    assert "path=/" in header.lower()


async def test_the_session_cookie_is_secure_when_configured(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "cookie_secure", True)

    resp = await login(client)

    assert "secure" in resp.headers["set-cookie"].lower()


async def test_the_email_is_matched_without_regard_to_casing(client):
    assert (await login(client, email="OWNER@Cedar.Example")).status_code == 200


async def test_a_wrong_password_is_refused_and_starts_no_session(client):
    resp = await login(client, password="not the password")

    assert resp.status_code == 401
    assert COOKIE not in client.cookies
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_an_unknown_email_is_refused_with_the_same_answer_as_a_wrong_password(client):
    unknown = await login(client, email="nobody@cedar.example")
    wrong = await login(client, password="not the password")

    assert unknown.status_code == 401
    # Nothing in the answer says whether the account exists.
    assert unknown.json() == wrong.json()


async def test_the_password_and_its_hash_never_appear_in_any_response(client):
    ok = await login(client)
    me = await client.get("/api/auth/me")

    for resp in (ok, me):
        assert PASSWORD not in resp.text
        # `must_change_password` is a legitimate field; what must never appear is the
        # credential or anything derived from it.
        assert "password_hash" not in resp.text.lower()
        assert "current_password" not in resp.text.lower()
    ((stored,),) = await rows("SELECT password_hash FROM users")
    assert stored.startswith("$argon2id$")
    assert stored not in ok.text + me.text


async def test_a_form_encoded_login_is_refused(client):
    # The session rides in a cookie, so a cross-origin form post is the shape to refuse.
    resp = await client.post("/api/auth/login", data={"email": EMAIL, "password": PASSWORD})

    assert resp.status_code == 415
    assert COOKIE not in client.cookies


async def test_a_mutation_with_no_content_type_at_all_is_refused(client):
    await login(client)

    # The case a denylist of form encodings misses: `fetch(url, {method: 'POST',
    # credentials: 'include'})` with no body sends no Content-Type, needs no preflight, and
    # carries the session cookie. Requiring application/json is what stops it.
    resp = await client.post("/api/auth/logout", headers={})

    assert resp.status_code == 415
    assert (await client.get("/api/auth/me")).status_code == 200  # the session survived


async def test_a_mutation_with_a_charset_on_the_json_content_type_is_accepted(client):
    await login(client)

    resp = await client.post(
        "/api/auth/logout", headers={"Content-Type": "application/json; charset=utf-8"}
    )

    assert resp.status_code == 204


# --- the protected endpoint -------------------------------------------------------------


async def test_me_refuses_an_anonymous_request(client):
    resp = await client.get("/api/auth/me")

    assert resp.status_code == 401


async def test_me_refuses_an_expired_token(client):
    await login(client)
    expired = session_mod.issue_token(
        (await rows("SELECT id FROM users"))[0][0], ttl=timedelta(seconds=-1)
    )
    client.cookies.set(COOKIE, expired.token)

    assert (await client.get("/api/auth/me")).status_code == 401


async def test_me_refuses_a_token_signed_with_another_secret(client):
    import jwt

    forged = jwt.encode({"sub": "whoever", "jti": "x", "exp": 9999999999}, "w" * 64, "HS256")
    client.cookies.set(COOKIE, forged)

    assert (await client.get("/api/auth/me")).status_code == 401


async def test_me_refuses_a_token_whose_user_has_been_deleted(client):
    await login(client)
    async with session_scope() as db:
        await db.execute(text("DELETE FROM users"))
        await db.commit()

    assert (await client.get("/api/auth/me")).status_code == 401


# --- logout -----------------------------------------------------------------------------


async def test_logout_ends_the_session_for_good(client):
    await login(client)
    token = client.cookies[COOKIE]

    resp = await logout(client)

    assert resp.status_code == 204
    assert client.cookies.get(COOKIE) in (None, "")
    # The cookie being cleared is not what ends the session: the token itself is dead.
    client.cookies.set(COOKIE, token)
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_logging_out_twice_is_not_an_error(client):
    await login(client)
    assert (await logout(client)).status_code == 204
    assert (await logout(client)).status_code == 204


async def test_logout_leaves_another_session_alone(client):
    await login(client)
    first = client.cookies[COOKIE]
    client.cookies.clear()
    await login(client)

    await logout(client)

    client.cookies.set(COOKIE, first)
    assert (await client.get("/api/auth/me")).status_code == 200


# --- the audit log ----------------------------------------------------------------------


async def test_a_failed_login_is_written_to_the_audit_log(client):
    await login(client, password="not the password")

    ((event, target, metadata, has_actor),) = await audit()
    assert event == "login.failed"
    assert target == "user"
    assert json.loads(metadata) == {"email": EMAIL, "reason": "bad_password"}
    # Never an actor. Whoever typed that password is exactly who is not known, and filing the
    # account as the actor reads as an accusation of the person who was attacked. The account
    # is named as the *target* — the thing an attempt was made against.
    assert has_actor is False
    assert (await rows("SELECT target_id FROM audit_events"))[0][0] is not None


async def test_a_failed_login_for_an_unknown_account_names_no_target(client):
    """The one difference between the two rows, and it is not in the answer the caller got."""
    await login(client, email="nobody@cedar.example", password="whatever you like")

    ((event, _, metadata, has_actor),) = await audit()
    assert event == "login.failed"
    assert json.loads(metadata) == {"email": "nobody@cedar.example", "reason": "unknown_email"}
    assert has_actor is False
    assert (await rows("SELECT target_id FROM audit_events"))[0][0] is None


async def test_a_failed_login_never_records_the_attempted_password(client):
    await login(client, password="hunter2 hunter2")

    ((_, _, metadata, _),) = await audit()
    assert "hunter2" not in metadata


async def test_a_successful_login_and_the_logout_that_ends_it_are_logged(client):
    await login(client)
    await logout(client)

    assert [(e, has_actor) for e, _, _, has_actor in await audit()] == [
        ("login.succeeded", True),
        ("logout", True),
    ]


# --- S5: the audit log is append-only ----------------------------------------------------


async def test_the_application_role_cannot_rewrite_or_erase_an_audit_event(client):
    await login(client, password="not the password")
    assert len(await audit()) == 1

    for statement in (
        "UPDATE audit_events SET event_type = 'nothing.happened'",
        "DELETE FROM audit_events",
    ):
        async with session_scope() as db:
            with pytest.raises(Exception) as caught:  # noqa: B017 — permission *or* trigger
                await db.execute(text(statement))
                await db.commit()
        assert "append-only" in str(caught.value) or "permission denied" in str(caught.value)

    assert len(await audit()) == 1

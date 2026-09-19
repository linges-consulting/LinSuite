"""S1: the token-gated first-run setup wizard, over a real PostgreSQL as `linsuite_app`."""

import asyncio
import hashlib
import logging
import os
import stat

import pytest
from sqlalchemy import text

from auth import setup
from core.config import get_settings
from core.db import session_scope

PAYLOAD = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": "Owner@Cedar.example",
    "admin_password": "correct horse battery",
}


def token_file() -> str:
    return get_settings().setup_token_file


@pytest.fixture(autouse=True)
async def unclaimed(database):
    """Every test starts on a freshly deployed, unclaimed instance."""
    async with session_scope() as session:
        await session.execute(text("DELETE FROM users"))
        await session.execute(text("DELETE FROM businesses"))
        await session.execute(text("DELETE FROM setup_token"))
        await session.commit()
    if os.path.exists(token_file()):
        os.remove(token_file())
    yield


async def boot() -> str | None:
    """Start the application: what `main.lifespan` does on every container boot."""
    async with session_scope() as session:
        return await setup.bootstrap_setup_token(session)


async def rows(sql: str) -> list[tuple]:
    async with session_scope() as session:
        return list((await session.execute(text(sql))).all())


# --- the token ------------------------------------------------------------------------


async def test_boot_writes_the_token_to_stdout_and_to_a_0600_file(caplog):
    with caplog.at_level(logging.INFO, logger="auth.setup"):
        token = await boot()

    assert token
    with open(token_file()) as f:
        assert f.read().strip() == token
    assert stat.S_IMODE(os.stat(token_file()).st_mode) == 0o600
    assert any(token in r.getMessage() for r in caplog.records if r.name == "auth.setup")


async def test_a_restart_keeps_the_original_token(client):
    original = await boot()

    # A second instance against the same database: no new token, and the operator is told
    # where to read the one that still works.
    assert await boot() is None
    with open(token_file()) as f:
        assert f.read().strip() == original

    resp = await client.post("/api/setup", json={**PAYLOAD, "token": original})
    assert resp.status_code == 201


async def test_two_instances_booting_at_once_agree_on_one_token(client):
    # The loser must not overwrite the winner's digest: the operator reads one file, and
    # the token in it has to be the one the database will accept.
    first, second = await asyncio.gather(boot(), boot())

    minted = [t for t in (first, second) if t]
    assert len(minted) == 1
    with open(token_file()) as f:
        assert f.read().strip() == minted[0]
    assert await rows("SELECT token_hash FROM setup_token") == [
        (hashlib.sha256(minted[0].encode()).hexdigest(),)
    ]
    assert (
        await client.post("/api/setup", json={**PAYLOAD, "token": minted[0]})
    ).status_code == 201


async def test_two_instances_replacing_a_lost_token_agree_on_one_token(client):
    await boot()
    os.remove(token_file())

    first, second = await asyncio.gather(boot(), boot())

    minted = [t for t in (first, second) if t]
    assert len(minted) == 1
    with open(token_file()) as f:
        assert f.read().strip() == minted[0]
    assert (
        await client.post("/api/setup", json={**PAYLOAD, "token": minted[0]})
    ).status_code == 201


async def test_a_token_file_without_a_stored_digest_is_a_leftover(client):
    # e.g. a restored database, or an upgrade from a build that kept the token in memory.
    with open(token_file(), "w") as f:
        f.write("left-over\n")

    token = await boot()

    assert token and token != "left-over"
    assert (
        await client.post("/api/setup", json={**PAYLOAD, "token": "left-over"})
    ).status_code == 403


async def test_a_token_whose_file_was_lost_is_replaced_on_the_next_boot(client):
    lost = await boot()
    os.remove(token_file())

    replacement = await boot()

    assert replacement and replacement != lost
    assert (await client.post("/api/setup", json={**PAYLOAD, "token": lost})).status_code == 403
    assert (
        await client.post("/api/setup", json={**PAYLOAD, "token": replacement})
    ).status_code == 201


# --- status and timezones -------------------------------------------------------------


async def test_status_says_setup_is_required_on_a_fresh_deployment(client):
    resp = await client.get("/api/setup/status")

    assert resp.status_code == 200
    assert resp.json() == {"required": True}


async def test_timezones_are_the_sorted_canonical_iana_list(client):
    """Canonical zones only — the wizard and the Business screen offer one list, and a
    deprecated alias in it is a second spelling of a zone that is already there."""
    resp = await client.get("/api/setup/timezones")

    assert resp.status_code == 200
    zones = resp.json()["timezones"]
    assert zones == sorted(zones)
    assert "America/Toronto" in zones
    assert "US/Eastern" not in zones and "Etc/UTC" not in zones
    # `UTC` is the one exception, and `tests/test_settings.py` is where the reason lives.
    assert "UTC" in zones
    assert len(zones) > 400


# --- the token gate -------------------------------------------------------------------


async def test_setup_rejects_an_absent_token(client):
    await boot()

    resp = await client.post("/api/setup", json=PAYLOAD)

    assert resp.status_code == 422
    assert await rows("SELECT 1 FROM businesses") == []


async def test_setup_rejects_a_wrong_token(client):
    await boot()

    resp = await client.post("/api/setup", json={**PAYLOAD, "token": "not-the-token"})

    assert resp.status_code == 403
    assert await rows("SELECT 1 FROM businesses") == []


async def test_setup_rejects_any_token_when_none_has_been_minted(client):
    # No boot has run: nothing can be accepted, least of all a guess.
    resp = await client.post("/api/setup", json={**PAYLOAD, "token": "anything"})

    assert resp.status_code == 403
    assert await rows("SELECT 1 FROM businesses") == []


async def test_only_the_digest_of_the_token_is_stored(client):
    token = await boot()

    ((stored,),) = await rows("SELECT token_hash FROM setup_token")
    assert token not in stored
    assert stored == hashlib.sha256(token.encode()).hexdigest()


# --- validation -----------------------------------------------------------------------


async def test_setup_rejects_a_password_under_twelve_characters(client):
    token = await boot()

    resp = await client.post(
        "/api/setup", json={**PAYLOAD, "token": token, "admin_password": "eleven char"}
    )

    assert resp.status_code == 422
    assert await rows("SELECT 1 FROM users") == []
    # A validation error must never reflect what was submitted: this body reaches proxies,
    # HAR exports and error trackers.
    assert "eleven char" not in resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "admin_password"]


async def test_setup_rejects_a_password_from_the_breach_list(client):
    # The wizard screens with the same policy as login does; the harness has outbound
    # checks disabled, so this is the bundled list answering.
    token = await boot()

    resp = await client.post(
        "/api/setup", json={**PAYLOAD, "token": token, "admin_password": "passwordpassword"}
    )

    assert resp.status_code == 422
    assert "passwordpassword" not in resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "admin_password"]
    assert await rows("SELECT 1 FROM users") == []


async def test_a_validation_error_never_echoes_the_submitted_value(client):
    token = await boot()

    resp = await client.post(
        "/api/setup",
        json={**PAYLOAD, "token": token, "admin_password": "s3cret", "admin_email": "nope"},
    )

    assert resp.status_code == 422
    assert "s3cret" not in resp.text and "nope" not in resp.text
    for error in resp.json()["detail"]:
        assert set(error) <= {"type", "loc", "msg"}


async def test_setup_rejects_a_timezone_that_is_not_an_iana_name(client):
    token = await boot()

    resp = await client.post(
        "/api/setup", json={**PAYLOAD, "token": token, "timezone": "Mars/Olympus"}
    )

    assert resp.status_code == 422
    assert await rows("SELECT 1 FROM businesses") == []


async def test_setup_rejects_an_email_that_is_not_an_address(client):
    token = await boot()

    resp = await client.post("/api/setup", json={**PAYLOAD, "token": token, "admin_email": "owner"})

    assert resp.status_code == 422


# --- completion -----------------------------------------------------------------------


async def test_completion_creates_the_business_and_the_first_administrator(client):
    token = await boot()

    resp = await client.post("/api/setup", json={**PAYLOAD, "token": token})

    assert resp.status_code == 201
    assert await rows("SELECT name, timezone, setup_completed_at IS NOT NULL FROM businesses") == [
        ("Cedar Lane Clinic", "America/Toronto", True)
    ]
    # `is_admin` is gone (0006); the wizard's account is authorised by its role instead.
    ((email, password_hash, role),) = await rows(
        "SELECT u.email, u.password_hash, r.name FROM users u JOIN roles r ON r.id = u.role_id"
    )
    assert email == "owner@cedar.example"  # stored lowercase; the index is on lower(email)
    assert password_hash.startswith("$argon2id$")
    assert PAYLOAD["admin_password"] not in password_hash
    assert role == "Administrator"


async def test_completion_destroys_the_token_file_and_its_stored_digest(client):
    token = await boot()

    await client.post("/api/setup", json={**PAYLOAD, "token": token})

    assert not os.path.exists(token_file())
    assert await rows("SELECT 1 FROM setup_token") == []


async def test_a_second_attempt_with_the_original_token_is_rejected(client):
    token = await boot()
    assert (await client.post("/api/setup", json={**PAYLOAD, "token": token})).status_code == 201

    resp = await client.post(
        "/api/setup", json={**PAYLOAD, "token": token, "business_name": "Squatter"}
    )

    assert resp.status_code == 404
    assert await rows("SELECT name FROM businesses") == [("Cedar Lane Clinic",)]


async def test_status_and_timezones_still_answer_after_completion(client):
    token = await boot()
    assert (await client.post("/api/setup", json={**PAYLOAD, "token": token})).status_code == 201

    status = await client.get("/api/setup/status")
    assert status.status_code == 200
    assert status.json() == {"required": False}
    # Task 9 lets an administrator change the timezone, and reuses this list to do it.
    assert (await client.get("/api/setup/timezones")).status_code == 200


async def test_setup_stays_disabled_across_a_restart(client):
    token = await boot()
    assert (await client.post("/api/setup", json={**PAYLOAD, "token": token})).status_code == 201

    # Restart: a fresh instance boots against the same database. The flag is in the DB, so
    # no token is minted and the original one is dead even to a process that never saw it.
    assert await boot() is None

    assert not os.path.exists(token_file())
    assert (await client.get("/api/setup/status")).json() == {"required": False}
    assert (await client.post("/api/setup", json={**PAYLOAD, "token": token})).status_code == 404

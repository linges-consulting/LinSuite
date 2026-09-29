"""S1/S3: staff records — credentials, commission rates, colours, and the invitation.

Four rules this file exists to hold down.

**Creating a staff member creates the account, and the account has no password.** The row in
`users` is born with `password_hash` NULL and an invitation link is the only way to give it
one. Nothing here ever mints a temporary password, so there is never a credential in a log,
in a mailbox or in an administrator's head.

**Credentials are conditional and enforced by the server.** A practitioner without a
designation and a licence number is a treatment receipt an insurer rejects (tech-stack §21),
so it is a 422 at the boundary and a CHECK constraint underneath. Somebody who is not a
practitioner is asked for neither.

**Deactivation preserves the row.** It ends every session, refuses the next sign-in, and
leaves every appointment, receipt and audit entry pointing at a staff member who still
exists. The one deactivation that is refused is the one that would leave nobody able to
administer this instance.

**The colour is data, not decoration.** The calendar (Task 16) paints every appointment with
its staff member's colour, so every staff member has one from the moment they are created,
and every colour in the palette is legible with white text on it.
"""

import re
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core.db import session_scope
from core.redis import get_redis
from scheduling.palette import PALETTE
from settings.branding import WHITE, contrast
from tests.conftest import get_owner_engine

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"
INVITED_PASSWORD = "several unrelated words"

SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": EMAIL,
    "admin_password": PASSWORD,
}

STAFF = "/api/admin/staff"
COOKIE = "linsuite_session"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async with get_owner_engine().begin() as owner:
        await owner.execute(text("DELETE FROM audit_events"))
    async with session_scope() as db:
        # "queue_entries" first: a leftover row (Phase 7 Task 1, #12) FKs to staff with no
        # cascade, and would block the delete below.
        for table in (
            "queue_entries",
            "staff",
            "password_reset_tokens",
            "users",
            "businesses",
            "setup_token",
        ):
            await db.execute(text(f"DELETE FROM {table}"))
        await db.execute(text("DELETE FROM roles WHERE NOT is_system"))
        await db.commit()
    await get_redis().flushdb()

    from auth import setup

    async with session_scope() as db:
        token = await setup.bootstrap_setup_token(db)
    resp = await client.post("/api/setup", json={**SETUP, "token": token})
    assert resp.status_code == 201, resp.text
    # This suite is about staff records, not about the second factor; the enrolment gate
    # would otherwise stand between every test and the screen under test.
    async with session_scope() as db:
        await db.execute(text("UPDATE businesses SET mfa_required_for_admin = false"))
        await db.commit()
    client.cookies.clear()
    yield


# --- helpers --------------------------------------------------------------------------------


async def login(client, email=EMAIL, password=PASSWORD):
    return await client.post("/api/auth/login", json={"email": email, "password": password})


async def as_admin(client, email=EMAIL, password=PASSWORD):
    assert (await login(client, email, password)).status_code == 200
    resp = await client.post("/api/auth/mode", json={"mode": "admin", "password": password})
    assert resp.status_code == 200, resp.text


async def role_id_named(name: str) -> str:
    async with session_scope() as db:
        return str(await db.scalar(text("SELECT id FROM roles WHERE name = :n"), {"n": name}))


def draft(**overrides) -> dict:
    body = {
        "email": "ana@cedar.example",
        "first_name": "Ana",
        "last_name": "Rossi",
        "is_practitioner": True,
        "designation": "RMT",
        "licence_number": "12345",
        "commission_rate_services_bp": 4500,
        "commission_rate_retail_bp": 1000,
    }
    body.update(overrides)
    return body


async def create(client, **overrides):
    body = draft(**overrides)
    if "role_id" not in body:
        body["role_id"] = await role_id_named("Staff")
    return await client.post(STAFF, json=body)


async def make(client, **overrides) -> dict:
    resp = await create(client, **overrides)
    assert resp.status_code == 201, resp.text
    return resp.json()


def invite_link(email) -> str:
    """The token out of the one message that carried it — the only place it exists."""
    found = re.search(r"reset-password\?token=([\w-]+)", email.text)
    assert found, email.text
    return found.group(1)


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


async def events() -> list[str]:
    return [row[0] for row in await audit()]


# --- the palette ----------------------------------------------------------------------------


def test_every_palette_colour_carries_white_text():
    """S2. The calendar paints an event in the staff member's colour and writes its title on
    top (tech-stack §13), so a colour white cannot be read on is a colour that ships an
    unreadable appointment. 4.5:1 is WCAG AA for body text."""
    assert len(PALETTE) >= 12
    for colour in PALETTE:
        assert contrast(colour.hex, WHITE) >= 4.5, f"{colour.key} {colour.hex}"


def test_every_palette_colour_is_legible_on_a_dark_calendar_too():
    """The dark hex is derived rather than chosen (`settings/branding.py`), so nobody eyeballed
    it. The same title is written on it, and DESIGN.md says dark is designed rather than
    inherited — this is what stops a derivation change shipping an unreadable half."""
    for colour in PALETTE:
        ratio = contrast(colour.dark_hex, colour.dark_foreground)
        assert ratio >= 4.5, f"{colour.key} {colour.dark_hex} on {colour.dark_foreground}"


def test_palette_keys_and_hexes_are_unique():
    assert len({c.key for c in PALETTE}) == len(PALETTE)
    assert len({c.hex for c in PALETTE}) == len(PALETTE)


async def test_the_palette_is_served_with_both_theme_hexes(client):
    await as_admin(client)

    resp = await client.get(f"{STAFF}/palette")

    assert resp.status_code == 200, resp.text
    colours = resp.json()["colours"]
    assert [c["key"] for c in colours] == [c.key for c in PALETTE]
    for entry in colours:
        assert re.fullmatch(r"#[0-9a-f]{6}", entry["hex"])
        assert re.fullmatch(r"#[0-9a-f]{6}", entry["dark_hex"])
        assert entry["name"].strip()


# --- creating -------------------------------------------------------------------------------


async def test_creating_a_staff_member_creates_an_account_with_no_password(client, sent_emails):
    await as_admin(client)

    created = await make(client)

    assert created["email"] == "ana@cedar.example"
    assert created["display_name"] == "Ana Rossi"
    assert created["active"] is True
    assert created["invite_pending"] is True
    async with session_scope() as db:
        stored = await db.scalar(
            text("SELECT password_hash FROM users WHERE email = 'ana@cedar.example'")
        )
    assert stored is None

    assert len(sent_emails) == 1
    assert sent_emails[0].to == "ana@cedar.example"
    assert invite_link(sent_emails[0])


async def test_the_invitation_expires_in_seventy_two_hours(client, sent_emails):
    await as_admin(client)
    await make(client)

    async with session_scope() as db:
        expires = await db.scalar(text("SELECT expires_at FROM password_reset_tokens"))
    hours = (expires - datetime.now(UTC)).total_seconds() / 3600
    assert 71 < hours <= 72


async def test_the_invitation_sets_a_password_and_signs_in(client, sent_emails):
    await as_admin(client)
    await make(client)
    token = invite_link(sent_emails[0])
    client.cookies.clear()

    # No current password is asked for, because there is not one.
    resp = await client.post(
        "/api/auth/password-reset/confirm",
        json={"token": token, "new_password": INVITED_PASSWORD},
    )
    assert resp.status_code == 204, resp.text

    signed_in = await login(client, "ana@cedar.example", INVITED_PASSWORD)
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["email"] == "ana@cedar.example"


async def test_signing_in_before_the_invitation_is_accepted_is_refused_and_costs_no_hash(
    client, sent_emails, monkeypatch
):
    """A coded 403 rather than the usual 401: "wrong password" would send somebody looking
    for a password nobody ever gave them. Argon2 is never asked to verify against NULL."""
    await as_admin(client)
    await make(client)
    client.cookies.clear()

    from auth import login as login_module

    hashed = []
    original = login_module.verify_password

    async def counting(password_hash, password):
        hashed.append(password_hash)
        return await original(password_hash, password)

    monkeypatch.setattr(login_module, "verify_password", counting)

    resp = await login(client, "ana@cedar.example", INVITED_PASSWORD)

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "password_not_set"
    assert hashed == []
    # Recorded and counted, like any other refused attempt. This is the one answer in the
    # product that confirms an address exists, so probing it must cost something and leave a
    # trail — otherwise it is a free enumeration oracle nobody can see being used.
    assert "login.refused_no_password" in await events()


async def test_probing_an_unaccepted_invitation_costs_the_same_as_a_wrong_password(
    client, sent_emails
):
    """The progressive delay is what makes repeating it expensive: the second probe is
    refused by the throttle rather than answered, exactly as a second wrong password is."""
    await as_admin(client)
    await make(client)
    client.cookies.clear()

    first = await login(client, "ana@cedar.example", "wrong")
    second = await login(client, "ana@cedar.example", "wrong")

    assert first.status_code == 403
    assert second.status_code == 429, second.text


async def test_a_practitioner_without_credentials_is_refused(client):
    await as_admin(client)

    missing_licence = await create(client, licence_number=None)
    missing_designation = await create(client, designation="")

    assert missing_licence.status_code == 422, missing_licence.text
    assert missing_designation.status_code == 422, missing_designation.text
    async with session_scope() as db:
        assert await db.scalar(text("SELECT count(*) FROM staff")) == 1  # the administrator


async def test_somebody_who_is_not_a_practitioner_needs_neither(client, sent_emails):
    await as_admin(client)

    created = await make(
        client,
        email="desk@cedar.example",
        first_name="Theo",
        last_name="Marsh",
        is_practitioner=False,
        designation=None,
        licence_number=None,
    )

    assert created["is_practitioner"] is False
    assert created["designation"] is None
    assert created["licence_number"] is None


@pytest.mark.parametrize("rate", [-1, 10001])
async def test_commission_rates_outside_the_basis_point_range_are_refused(client, rate):
    await as_admin(client)

    services = await create(client, commission_rate_services_bp=rate)
    retail = await create(client, commission_rate_retail_bp=rate)

    assert services.status_code == 422, services.text
    assert retail.status_code == 422, retail.text


async def test_concurrency_defaults_to_one_and_refuses_less(client, sent_emails):
    await as_admin(client)

    created = await make(client)
    refused = await create(client, email="two@cedar.example", max_concurrent_appointments=0)

    assert created["max_concurrent_appointments"] == 1
    assert refused.status_code == 422, refused.text


async def test_a_new_staff_member_takes_the_first_unused_colour(client, sent_emails):
    await as_admin(client)
    # The administrator already holds the first colour, so the next two take the next two.
    listed = (await client.get(STAFF)).json()["staff"]
    assert listed[0]["colour"] == PALETTE[0].key

    second = await make(client)
    third = await make(
        client,
        email="theo@cedar.example",
        is_practitioner=False,
        designation=None,
        licence_number=None,
    )

    assert second["colour"] == PALETTE[1].key
    assert third["colour"] == PALETTE[2].key


async def test_the_palette_wraps_around_once_every_colour_is_taken(client, sent_emails):
    await as_admin(client)
    for index in range(1, len(PALETTE)):
        await make(client, email=f"staff{index}@cedar.example")

    wrapped = await make(client, email="one-more@cedar.example")

    assert wrapped["colour"] == PALETTE[0].key


async def test_a_colour_freed_by_deactivation_is_offered_again(client, sent_emails):
    await as_admin(client)
    second = await make(client)
    assert second["colour"] == PALETTE[1].key

    await client.post(f"{STAFF}/{second['id']}/deactivate", json={})
    third = await make(client, email="theo@cedar.example")

    assert third["colour"] == PALETTE[1].key


async def test_a_colour_outside_the_palette_is_refused(client):
    await as_admin(client)

    resp = await create(client, colour="chartreuse")

    assert resp.status_code == 422, resp.text


# --- editing --------------------------------------------------------------------------------


async def test_editing_records_only_the_fields_that_changed(client, sent_emails):
    await as_admin(client)
    staff = await make(client)

    resp = await client.patch(
        f"{STAFF}/{staff['id']}",
        json={"commission_rate_services_bp": 5000, "display_name": "Ana R."},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["commission_rate_services_bp"] == 5000
    assert resp.json()["display_name"] == "Ana R."
    updated = [row for row in await audit() if row[0] == "staff.updated"]
    assert len(updated) == 1
    assert "commission_rate_services_bp" in updated[0][1]
    assert "display_name" in updated[0][1]
    assert "licence_number" not in updated[0][1]


async def test_clearing_the_display_name_falls_back_to_their_name(client, sent_emails):
    """The form's hint says "leave blank to use their name", and a cleared text input sends
    null. That is the one null on this endpoint that means something other than deletion."""
    await as_admin(client)
    staff = await make(client, display_name="Dr. Rossi")

    resp = await client.patch(f"{STAFF}/{staff['id']}", json={"display_name": None})

    assert resp.status_code == 200, resp.text
    assert resp.json()["display_name"] == "Ana Rossi"


async def test_clearing_and_renaming_at_once_uses_the_new_name(client, sent_emails):
    await as_admin(client)
    staff = await make(client, display_name="Dr. Rossi")

    resp = await client.patch(
        f"{STAFF}/{staff['id']}", json={"last_name": "Moreau", "display_name": None}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["display_name"] == "Ana Moreau"


@pytest.mark.parametrize(
    "field,value",
    [
        ("first_name", None),
        ("last_name", None),
        ("colour", None),
        ("is_practitioner", None),
        ("commission_rate_services_bp", None),
        ("commission_rate_retail_bp", None),
        ("max_concurrent_appointments", None),
        ("sort_order", None),
        ("first_name", "   "),
    ],
)
async def test_emptying_a_required_field_is_refused_rather_than_a_five_hundred(
    client, sent_emails, field, value
):
    """Every field on the patch model is `… | None` so that "leave it alone" and "set it" can
    be told apart — which means a null reaches a NOT NULL column unless something stops it.
    Refused at the boundary, where the answer can name the field."""
    await as_admin(client)
    staff = await make(client)

    resp = await client.patch(f"{STAFF}/{staff['id']}", json={field: value})

    assert resp.status_code == 422, resp.text
    async with session_scope() as db:
        assert (
            await db.scalar(text("SELECT first_name FROM staff WHERE id = :i"), {"i": staff["id"]})
            == "Ana"
        )


async def test_the_two_credentials_are_the_ones_that_may_be_nulled(client, sent_emails):
    """Somebody ceasing to be a practitioner. The row keeps its shape; the credentials go."""
    await as_admin(client)
    staff = await make(client)

    resp = await client.patch(
        f"{STAFF}/{staff['id']}",
        json={"is_practitioner": False, "designation": None, "licence_number": None},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["designation"] is None
    assert resp.json()["licence_number"] is None


async def test_turning_somebody_into_a_practitioner_demands_credentials(client, sent_emails):
    await as_admin(client)
    staff = await make(
        client,
        email="desk@cedar.example",
        is_practitioner=False,
        designation=None,
        licence_number=None,
    )

    refused = await client.patch(f"{STAFF}/{staff['id']}", json={"is_practitioner": True})
    accepted = await client.patch(
        f"{STAFF}/{staff['id']}",
        json={"is_practitioner": True, "designation": "RMT", "licence_number": "98765"},
    )

    assert refused.status_code == 422, refused.text
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["licence_number"] == "98765"


# --- deactivation ---------------------------------------------------------------------------


async def test_deactivating_ends_every_session_and_refuses_the_next_sign_in(client, sent_emails):
    await as_admin(client)
    staff = await make(client)
    token = invite_link(sent_emails[0])
    await client.post(
        "/api/auth/password-reset/confirm",
        json={"token": token, "new_password": INVITED_PASSWORD},
    )
    client.cookies.clear()
    theirs = await login(client, "ana@cedar.example", INVITED_PASSWORD)
    assert theirs.status_code == 200
    their_cookie = client.cookies[COOKIE]
    client.cookies.clear()
    await as_admin(client)

    resp = await client.post(f"{STAFF}/{staff['id']}/deactivate", json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["active"] is False
    # Replaying the session they held is a 401: `revoke_all` moved the cut-off past it.
    client.cookies.clear()
    client.cookies.set(COOKIE, their_cookie)
    assert (await client.get("/api/auth/me")).status_code == 401
    # And the front door is shut, with a code that says which door.
    client.cookies.clear()
    refused = await login(client, "ana@cedar.example", INVITED_PASSWORD)
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "account_inactive"


async def test_a_refused_sign_in_does_not_wipe_the_lockout_state(client, sent_emails):
    """`throttle.clear` runs only for a sign-in that succeeds. Running it before the inactive
    check would hand anybody holding a deactivated account's password an unlimited way to
    reset the failure run and the offence tier on that address."""
    await as_admin(client)
    staff = await make(client)
    await client.post(
        "/api/auth/password-reset/confirm",
        json={"token": invite_link(sent_emails[0]), "new_password": INVITED_PASSWORD},
    )
    client.cookies.clear()
    await as_admin(client)
    await client.post(f"{STAFF}/{staff['id']}/deactivate", json={})
    client.cookies.clear()

    wrong = await login(client, "ana@cedar.example", "not the password")
    # The right password, and still refused — so if `clear` ran here the delay would be gone.
    refused = await login(client, "ana@cedar.example", INVITED_PASSWORD)
    after = await login(client, "ana@cedar.example", INVITED_PASSWORD)

    assert wrong.status_code == 401
    assert refused.status_code in (403, 429)
    assert after.status_code == 429, after.text


async def test_deactivating_preserves_the_row(client, sent_emails):
    await as_admin(client)
    staff = await make(client)

    await client.post(f"{STAFF}/{staff['id']}/deactivate", json={})

    async with session_scope() as db:
        kept = await db.execute(
            text("SELECT licence_number, colour FROM staff WHERE id = :i"), {"i": staff["id"]}
        )
    assert kept.one() == ("12345", PALETTE[1].key)


async def test_an_inactive_staff_member_is_hidden_unless_asked_for(client, sent_emails):
    await as_admin(client)
    staff = await make(client)
    await client.post(f"{STAFF}/{staff['id']}/deactivate", json={})

    default = (await client.get(STAFF)).json()["staff"]
    everybody = (await client.get(f"{STAFF}?include_inactive=true")).json()["staff"]

    assert [row["id"] for row in default] == [row["id"] for row in everybody if row["active"]]
    assert staff["id"] in [row["id"] for row in everybody]
    assert staff["id"] not in [row["id"] for row in default]


async def test_deactivating_the_last_active_administrator_is_refused(client):
    await as_admin(client)
    mine = (await client.get(f"{STAFF}?include_inactive=true")).json()["staff"]
    me = next(row for row in mine if row["email"] == EMAIL)

    resp = await client.post(f"{STAFF}/{me['id']}/deactivate", json={})

    assert resp.status_code == 409, resp.text
    assert (await login(client, EMAIL, PASSWORD)).status_code == 200


async def test_reactivating_restores_the_sign_in(client, sent_emails):
    await as_admin(client)
    staff = await make(client)
    token = invite_link(sent_emails[0])
    await client.post(
        "/api/auth/password-reset/confirm",
        json={"token": token, "new_password": INVITED_PASSWORD},
    )
    client.cookies.clear()
    await as_admin(client)
    await client.post(f"{STAFF}/{staff['id']}/deactivate", json={})

    resp = await client.post(f"{STAFF}/{staff['id']}/reactivate", json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["active"] is True
    client.cookies.clear()
    assert (await login(client, "ana@cedar.example", INVITED_PASSWORD)).status_code == 200


# --- the invitation, again ------------------------------------------------------------------


async def test_resending_an_invitation_kills_the_first_link(client, sent_emails):
    await as_admin(client)
    staff = await make(client)
    first = invite_link(sent_emails[0])

    resp = await client.post(f"{STAFF}/{staff['id']}/resend-invite", json={})

    assert resp.status_code == 202, resp.text
    assert len(sent_emails) == 2
    second = invite_link(sent_emails[1])
    assert second != first
    stale = await client.post(
        "/api/auth/password-reset/confirm",
        json={"token": first, "new_password": INVITED_PASSWORD},
    )
    assert stale.status_code == 400, stale.text
    fresh = await client.post(
        "/api/auth/password-reset/confirm",
        json={"token": second, "new_password": INVITED_PASSWORD},
    )
    assert fresh.status_code == 204, fresh.text


async def test_an_invitation_is_not_resent_to_somebody_who_already_has_a_password(
    client, sent_emails
):
    """That is a password reset, and it is theirs to ask for — not an administrator's to
    trigger on an account that is already in use."""
    await as_admin(client)
    staff = await make(client)
    await client.post(
        "/api/auth/password-reset/confirm",
        json={"token": invite_link(sent_emails[0]), "new_password": INVITED_PASSWORD},
    )
    client.cookies.clear()
    await as_admin(client)

    resp = await client.post(f"{STAFF}/{staff['id']}/resend-invite", json={})

    assert resp.status_code == 409, resp.text


# --- who may do any of this -----------------------------------------------------------------


async def test_every_staff_endpoint_needs_users_manage(client, sent_emails):
    await as_admin(client)
    staff = await make(client)
    staff_role = await role_id_named("Staff")
    # A role that can do everything *except* manage users, so the refusal is about the
    # capability and not about the Admin Mode window.
    role = await client.post(
        "/api/admin/roles",
        json={"name": "Deputy", "description": "Nearly.", "capabilities": ["admin"]},
    )
    assert role.status_code == 201, role.text
    deputy = await make(
        client,
        email="deputy@cedar.example",
        role_id=role.json()["id"],
        is_practitioner=False,
        designation=None,
        licence_number=None,
    )
    await client.post(
        "/api/auth/password-reset/confirm",
        json={"token": invite_link(sent_emails[-1]), "new_password": INVITED_PASSWORD},
    )
    client.cookies.clear()
    await as_admin(client, "deputy@cedar.example", INVITED_PASSWORD)

    refusals = [
        await client.get(STAFF),
        await client.get(f"{STAFF}/palette"),
        await client.post(STAFF, json=draft(email="nope@cedar.example", role_id=staff_role)),
        await client.patch(f"{STAFF}/{staff['id']}", json={"sort_order": 3}),
        await client.post(f"{STAFF}/{staff['id']}/deactivate", json={}),
        await client.post(f"{STAFF}/{staff['id']}/reactivate", json={}),
        await client.post(f"{STAFF}/{staff['id']}/resend-invite", json={}),
    ]

    assert [r.status_code for r in refusals] == [403] * 7
    assert {r.json()["code"] for r in refusals} == {"capability_required"}
    assert deputy["email"] == "deputy@cedar.example"


async def test_staff_are_refused_outside_admin_mode(client, sent_emails):
    await as_admin(client)
    await make(client)
    client.cookies.clear()
    assert (await login(client)).status_code == 200  # Staff Mode

    resp = await client.get(STAFF)

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "admin_mode_required"


# --- the trail ------------------------------------------------------------------------------


async def test_the_trail_records_the_whole_life_of_a_staff_member(client, sent_emails):
    await as_admin(client)
    staff = await make(client)
    await client.patch(f"{STAFF}/{staff['id']}", json={"sort_order": 2})
    await client.post(f"{STAFF}/{staff['id']}/resend-invite", json={})
    await client.post(f"{STAFF}/{staff['id']}/deactivate", json={})
    await client.post(f"{STAFF}/{staff['id']}/reactivate", json={})

    recorded = await events()

    for event in (
        "staff.created",
        "user.invited",
        "staff.updated",
        "user.invite_resent",
        "staff.deactivated",
        "staff.reactivated",
    ):
        assert event in recorded, recorded


# --- every account is a staff member --------------------------------------------------------


async def test_the_setup_wizard_gives_the_first_administrator_a_staff_row(client):
    await as_admin(client)

    listed = (await client.get(STAFF)).json()["staff"]

    assert len(listed) == 1
    assert listed[0]["email"] == EMAIL
    assert listed[0]["display_name"] == "owner"
    assert listed[0]["is_practitioner"] is False
    assert listed[0]["colour"] in {c.key for c in PALETTE}


async def test_the_migration_backfills_a_staff_row_for_an_account_that_has_none(client):
    """The backfill statement from migration 0009, run against a user inserted the way an
    instance that predates this table already has: an account and no staff record."""
    import importlib.util
    import os

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "alembic",
        "versions",
        "0009_staff.py",
    )
    spec = importlib.util.spec_from_file_location("migration_0009", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    role = await role_id_named("Staff")
    async with session_scope() as db:
        await db.execute(
            text("INSERT INTO users (email, password_hash, role_id) VALUES (:e, :h, :r)"),
            {"e": "legacy@cedar.example", "h": "argon2-placeholder", "r": role},
        )
        await db.commit()

    async with session_scope() as db:
        await db.execute(text(migration.BACKFILL))
        await db.commit()

    async with session_scope() as db:
        row = await db.execute(
            text(
                "SELECT s.display_name, s.is_practitioner, s.colour, s.active FROM staff s "
                "JOIN users u ON u.id = s.user_id WHERE u.email = 'legacy@cedar.example'"
            )
        )
        assert row.one() == ("legacy", False, PALETTE[1].key, True)

    # Idempotent: an account that already has a row does not get a second one.
    async with session_scope() as db:
        await db.execute(text(migration.BACKFILL))
        await db.commit()
        assert await db.scalar(text("SELECT count(*) FROM staff")) == 2

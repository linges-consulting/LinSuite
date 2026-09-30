"""S1: Settings → Onboarding — the "Get your business ready" checklist (#116, spec #113).

What this file pins down:

* Each of the seven steps' `done` flips exactly when its data appears, and only then.
* Tax's rule, pre-#118: an *active* component with a rate, nothing about confirmation yet.
* Email's rule is "the most recent test send succeeded" — including the gap #116 closes in
  `settings/notifications_routes.py::send_test_email`: a previously-verified sender that later
  fails a test send goes back to undone, not stuck reading the stale success.
* Branding is the one step marked `optional`.
* Only `admin` in Admin Mode may read or dismiss; dismissal sets `onboarding_dismissed_at`,
  writes a fact-only audit event, and is visible to every administrator (not per-account state).
"""

from datetime import date

import pytest
from sqlalchemy import text

from core.db import session_scope
from core.security import hash_password
from tests.conftest import add_account, get_owner_engine

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"
STAFF_PASSWORD = "several unrelated words"

SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": EMAIL,
    "admin_password": PASSWORD,
}

ONBOARDING = "/api/admin/onboarding"
DISMISS = f"{ONBOARDING}/dismiss"
NOTIFICATIONS = "/api/admin/business/notifications"
TAX_COMPONENTS = "/api/admin/billing/tax-components"
SERVICES = "/api/admin/services"
STAFF = "/api/admin/staff"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async with get_owner_engine().begin() as owner:
        await owner.execute(text("DELETE FROM audit_events"))
    async with session_scope() as db:
        for table in (
            "branding_assets",
            "tax_component_rates",
            "tax_components",
            "working_hours",
            "services",
            "password_reset_tokens",
            "staff",
            "users",
            "businesses",
            "setup_token",
        ):
            await db.execute(text(f"DELETE FROM {table}"))
        await db.execute(text("DELETE FROM roles WHERE NOT is_system"))
        await db.commit()

    from auth import setup

    async with session_scope() as db:
        token = await setup.bootstrap_setup_token(db)
    resp = await client.post("/api/setup", json={**SETUP, "token": token})
    assert resp.status_code == 201, resp.text
    async with session_scope() as db:
        await db.execute(text("UPDATE businesses SET mfa_required_for_admin = false"))
        await db.commit()
    client.cookies.clear()
    yield


async def as_admin(client, email=EMAIL, password=PASSWORD):
    resp = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    resp = await client.post("/api/auth/mode", json={"mode": "admin", "password": password})
    assert resp.status_code == 200, resp.text


async def audit_events() -> list[dict]:
    async with session_scope() as db:
        rows = await db.execute(
            text("SELECT event_type, actor_user_id FROM audit_events ORDER BY occurred_at, id")
        )
        return [dict(row._mapping) for row in rows]


async def role_id_named(name: str) -> str:
    async with session_scope() as db:
        return str(await db.scalar(text("SELECT id FROM roles WHERE name = :n"), {"n": name}))


def step(body: dict, key: str) -> dict:
    return next(s for s in body["steps"] if s["key"] == key)


# --- a fresh instance: every step undone, none optional but branding --------------------------


async def test_a_fresh_instance_has_every_step_undone_except_staff(client):
    """The wizard's own administrator already has an active `staff` row (`scheduling/
    models.py::Staff` docstring: "the setup wizard creates one for the first administrator"),
    so `staff` alone starts done on a brand-new instance."""
    await as_admin(client)

    resp = await client.get(ONBOARDING)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {s["key"] for s in body["steps"]} == {
        "business",
        "hours",
        "tax",
        "services",
        "staff",
        "email",
        "branding",
    }
    for s in body["steps"]:
        assert s["done"] is (s["key"] == "staff"), s
        assert s["optional"] is (s["key"] == "branding")
    assert body["dismissed_at"] is None


# --- business: an address with a province, and a phone number ---------------------------------


async def test_business_step_flips_once_address_province_and_phone_are_saved(client):
    await as_admin(client)

    resp = await client.put(
        "/api/admin/business",
        json={"name": "Cedar Lane Clinic", "address_line1": "1 King St", "province": "ON"},
    )
    assert resp.status_code == 200, resp.text
    assert step((await client.get(ONBOARDING)).json(), "business")["done"] is False

    resp = await client.put(
        "/api/admin/business",
        json={
            "name": "Cedar Lane Clinic",
            "address_line1": "1 King St",
            "province": "ON",
            "phone": "416-555-0100",
        },
    )
    assert resp.status_code == 200, resp.text
    assert step((await client.get(ONBOARDING)).json(), "business")["done"] is True


# --- hours: at least one open day, anywhere (hours are per-staff) -----------------------------


async def test_hours_step_flips_once_a_working_hours_block_exists(client):
    await as_admin(client)
    staff_id = await _self_staff_id()

    assert step((await client.get(ONBOARDING)).json(), "hours")["done"] is False

    resp = await client.put(
        f"{STAFF}/{staff_id}/hours",
        json={"blocks": [{"weekday": 0, "start_minute": 540, "end_minute": 1020}]},
    )
    assert resp.status_code == 200, resp.text
    assert step((await client.get(ONBOARDING)).json(), "hours")["done"] is True


async def _self_staff_id() -> str:
    async with session_scope() as db:
        return str(
            await db.scalar(
                text(
                    "SELECT staff.id FROM staff JOIN users ON users.id = staff.user_id "
                    "WHERE users.email = :e"
                ),
                {"e": EMAIL},
            )
        )


# --- tax: at least one active component with a rate (pre-#118) --------------------------------


async def test_tax_step_flips_once_an_active_component_has_a_rate(client):
    await as_admin(client)

    assert step((await client.get(ONBOARDING)).json(), "tax")["done"] is False

    resp = await client.post(
        TAX_COMPONENTS,
        json={
            "code": "gst",
            "name": "GST",
            "province": None,
            "rate_bp": 500,
            "effective_from": date(2024, 1, 1).isoformat(),
        },
    )
    assert resp.status_code == 201, resp.text
    component = resp.json()
    assert step((await client.get(ONBOARDING)).json(), "tax")["done"] is True

    resp = await client.patch(f"{TAX_COMPONENTS}/{component['id']}", json={"active": False})
    assert resp.status_code == 200, resp.text
    assert step((await client.get(ONBOARDING)).json(), "tax")["done"] is False


# --- services: at least one active service -----------------------------------------------------


async def test_services_step_flips_once_an_active_service_exists(client):
    await as_admin(client)

    assert step((await client.get(ONBOARDING)).json(), "services")["done"] is False

    resp = await client.post(
        SERVICES,
        json={
            "name": "Swedish Massage",
            "duration_minutes": 60,
            "buffer_before_minutes": 0,
            "buffer_after_minutes": 0,
            "price_cents": 12000,
        },
    )
    assert resp.status_code == 201, resp.text
    service = resp.json()
    assert step((await client.get(ONBOARDING)).json(), "services")["done"] is True

    resp = await client.post(f"{SERVICES}/{service['id']}/deactivate", json={})
    assert resp.status_code == 200, resp.text
    assert step((await client.get(ONBOARDING)).json(), "services")["done"] is False


# --- staff: at least one active staff member (the wizard's own admin already counts) -----------


async def test_staff_step_is_already_done_by_the_wizards_own_administrator(client):
    await as_admin(client)

    assert step((await client.get(ONBOARDING)).json(), "staff")["done"] is True


async def test_staff_step_goes_undone_once_the_only_staff_member_is_deactivated(client):
    """`POST .../deactivate` refuses to leave nobody who can administer the instance
    (`scheduling/staff.py`'s own guard) — a real, unrelated rule this ticket has no business
    working around through a second account, so the row is flipped directly, the same way
    `claimed_instance` fixtures across this suite flip `businesses` columns no endpoint exposes."""
    await as_admin(client)
    staff_id = await _self_staff_id()

    async with session_scope() as db:
        await db.execute(text("UPDATE staff SET active = false WHERE id = :id"), {"id": staff_id})
        await db.commit()

    assert step((await client.get(ONBOARDING)).json(), "staff")["done"] is False


# --- email: a sender configured and its *most recent* test send succeeded ----------------------


async def test_email_step_flips_only_after_a_successful_test_send(client, sent_emails):
    await as_admin(client)
    resp = await client.patch(
        NOTIFICATIONS,
        json={
            "email_sender": "resend",
            "resend_from_address": "hello@cedar.example",
            "resend_api_key": "re_live_key",
        },
    )
    assert resp.status_code == 200, resp.text
    assert step((await client.get(ONBOARDING)).json(), "email")["done"] is False

    resp = await client.post(f"{NOTIFICATIONS}/test-email", json={"to": "owner@cedar.example"})
    assert resp.status_code == 200, resp.text
    assert step((await client.get(ONBOARDING)).json(), "email")["done"] is True


async def test_email_step_goes_undone_again_after_a_later_test_send_fails(
    client, sent_emails, fail_next_send
):
    """The gap #116 closes: a sender verified once must not keep reading as done once its
    most recent test send has actually failed."""
    await as_admin(client)
    await client.patch(
        NOTIFICATIONS,
        json={
            "email_sender": "resend",
            "resend_from_address": "hello@cedar.example",
            "resend_api_key": "re_live_key",
        },
    )
    verified = await client.post(f"{NOTIFICATIONS}/test-email", json={"to": "owner@cedar.example"})
    assert verified.status_code == 200, verified.text
    assert step((await client.get(ONBOARDING)).json(), "email")["done"] is True

    fail_email_next, _ = fail_next_send
    fail_email_next.append(RuntimeError("Resend said no"))
    failed = await client.post(f"{NOTIFICATIONS}/test-email", json={"to": "owner@cedar.example"})
    assert failed.status_code == 400, failed.text

    assert step((await client.get(ONBOARDING)).json(), "email")["done"] is False


# --- branding: optional, done once a logo row exists --------------------------------------------


async def test_branding_step_is_optional_and_flips_once_a_logo_row_exists(client):
    await as_admin(client)

    assert step((await client.get(ONBOARDING)).json(), "branding")["optional"] is True
    assert step((await client.get(ONBOARDING)).json(), "branding")["done"] is False

    async with session_scope() as db:
        await db.execute(
            text(
                "INSERT INTO branding_assets (kind, content_type, data, byte_length, sha256) "
                "VALUES ('logo', 'image/png', :data, 3, 'x')"
            ),
            {"data": b"abc"},
        )
        await db.commit()

    assert step((await client.get(ONBOARDING)).json(), "branding")["done"] is True


# --- dismissal: shared, audited fact -------------------------------------------------------------


async def test_dismissing_sets_the_timestamp_and_audits_the_fact(client):
    await as_admin(client)

    resp = await client.post(DISMISS, json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dismissed_at"] is not None

    events = await audit_events()
    dismissed = [e for e in events if e["event_type"] == "business.onboarding_dismissed"]
    assert len(dismissed) == 1
    assert dismissed[0]["actor_user_id"] is not None

    # Shared: a plain GET (no further dismissal) still reports it.
    follow_up = await client.get(ONBOARDING)
    assert follow_up.json()["dismissed_at"] == body["dismissed_at"]


async def test_dismissal_is_visible_to_a_second_administrator(client):
    await as_admin(client)
    await client.post(DISMISS, json={})
    client.cookies.clear()

    second_role = await role_id_named("Administrator")
    await add_account(
        "second-admin@cedar.example", await hash_password(STAFF_PASSWORD), role=second_role
    )
    await as_admin(client, email="second-admin@cedar.example", password=STAFF_PASSWORD)

    resp = await client.get(ONBOARDING)
    assert resp.json()["dismissed_at"] is not None


# --- capability and mode gate ---------------------------------------------------------------------


async def test_read_is_refused_to_an_account_without_the_admin_capability(client):
    role = await role_id_named("Staff")
    await add_account("frontdesk@cedar.example", await hash_password(STAFF_PASSWORD), role=role)
    login = await client.post(
        "/api/auth/login", json={"email": "frontdesk@cedar.example", "password": STAFF_PASSWORD}
    )
    assert login.status_code == 200, login.text

    resp = await client.get(ONBOARDING)
    assert resp.status_code == 403, resp.text


async def test_read_is_refused_in_staff_mode_even_for_an_administrator(client):
    login = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200, login.text

    resp = await client.get(ONBOARDING)
    assert resp.status_code == 403, resp.text


async def test_dismiss_is_refused_to_an_account_without_the_admin_capability(client):
    role = await role_id_named("Staff")
    await add_account("frontdesk@cedar.example", await hash_password(STAFF_PASSWORD), role=role)
    login = await client.post(
        "/api/auth/login", json={"email": "frontdesk@cedar.example", "password": STAFF_PASSWORD}
    )
    assert login.status_code == 200, login.text

    resp = await client.post(DISMISS, json={})
    assert resp.status_code == 403, resp.text

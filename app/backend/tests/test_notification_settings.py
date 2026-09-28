"""S1: Settings → Notifications (Phase 12 Task 6, #11).

What this file pins down, beyond the ordinary read/write round trip:

* **A secret is never echoed back.** The GET response carries `*_set` booleans, never the
  plaintext or the ciphertext, for `resend_api_key`, `smtp_password` and `twilio_auth_token`.
* **A field left out of a PATCH is left alone**, secrets included — rotating one credential
  must not silently blank the others.
* **"No sender configured" is stated plainly.** `email_ready`/`sms_ready` on the GET response,
  not inferred client-side from which fields happen to be filled in.
* **The test-send actions call the real adapter**, through `NOTIFICATION_PROVIDER=recording`
  (`tests/fake_notifications.py`) — never a mocked `httpx`/`smtplib` call — and only a
  *successful* one sets `resend_domain_verified_at`/`smtp_verified_at`.
* **A credential edit un-verifies its sender.**
* Templates are their own sub-resource, still audited by field name only (never body text).
"""

import uuid

import pytest
from sqlalchemy import text

from core.db import get_purge_engine, session_scope
from core.redis import get_redis
from core.security import hash_password
from tests.conftest import add_account

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"
STAFF_PASSWORD = "several unrelated words"

SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": EMAIL,
    "admin_password": PASSWORD,
}

NOTIFICATIONS = "/api/admin/business/notifications"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    """The same shape `tests/test_settings.py` uses — a fresh claimed instance, MFA off so the
    enrolment gate never stands between a test and the screen under test."""
    async with get_purge_engine().begin() as purge:
        await purge.execute(text("DELETE FROM audit_events"))
    async with session_scope() as db:
        for table in ("branding_assets", "users", "businesses", "setup_token"):
            await db.execute(text(f"DELETE FROM {table}"))
        await db.execute(text("DELETE FROM roles WHERE NOT is_system"))
        await db.commit()
    await get_redis().flushdb()

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


async def audit(event_type: str) -> list[dict]:
    async with session_scope() as db:
        rows = await db.execute(
            text(
                "SELECT metadata FROM audit_events WHERE event_type = :t ORDER BY occurred_at, id"
            ),
            {"t": event_type},
        )
    return [row[0] for row in rows]


# --- GET: honest readiness, no secrets ------------------------------------------------------


async def test_an_unconfigured_business_says_so_plainly(client):
    await as_admin(client)

    resp = await client.get(NOTIFICATIONS)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["email_sender"] is None
    assert body["email_ready"] is False
    assert body["resend_api_key_set"] is False
    assert body["smtp_password_set"] is False
    assert body["sms_enabled"] is False
    assert body["sms_ready"] is False
    assert body["twilio_auth_token_set"] is False
    assert body["reminder_intervals_hours"] == [24, 2]
    # Booking-portal policy (Phase 6 Task 4, #10) — the migration's own defaults, unchanged
    # until an administrator edits them here.
    assert body["online_booking_enabled"] is True
    assert body["online_cancellation_enabled"] is True
    assert body["cancellation_cutoff_hours"] == 24
    assert body["booking_daily_cap_per_ip"] == 20
    assert body["booking_daily_cap_per_email"] == 5
    # Walk-in queue (Phase 7 Task 1, #12) — off by default, the migration's own default.
    assert body["enable_walk_in_queue"] is False


async def test_the_templates_list_has_all_fourteen_seeded_rows_with_their_merge_fields(client):
    await as_admin(client)

    body = (await client.get(NOTIFICATIONS)).json()

    templates = body["templates"]
    assert len(templates) == 14
    pairs = {(t["notification_type"], t["channel"]) for t in templates}
    assert pairs == {
        (t, c)
        for t in (
            "booking_confirmation",
            "reminder",
            "modification",
            "cancellation",
            "form_link",
            "package_notice",
            "low_stock",  # #62
        )
        for c in ("email", "sms")
    }
    confirmation_email = next(
        t
        for t in templates
        if t["notification_type"] == "booking_confirmation" and t["channel"] == "email"
    )
    assert "$client_name" in confirmation_email["merge_fields"]
    assert "$appointment_time" in confirmation_email["merge_fields"]
    confirmation_sms = next(
        t
        for t in templates
        if t["notification_type"] == "booking_confirmation" and t["channel"] == "sms"
    )
    assert confirmation_sms["subject_template"] is None
    form_link = next(t for t in templates if t["notification_type"] == "form_link")
    assert "$form_link" in form_link["merge_fields"]


# --- PATCH: secrets never echoed, a field left out is left alone ---------------------------


async def test_configuring_resend_sets_it_up_without_ever_returning_the_key(client):
    await as_admin(client)

    resp = await client.patch(
        NOTIFICATIONS,
        json={
            "email_sender": "resend",
            "resend_from_address": "hello@cedar.example",
            "resend_api_key": "re_live_super_secret",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["email_sender"] == "resend"
    assert body["resend_from_address"] == "hello@cedar.example"
    assert body["resend_api_key_set"] is True
    assert "resend_api_key" not in body
    assert "re_live_super_secret" not in resp.text
    # Not ready yet: configured is not verified (module docstring, #11's own criterion).
    assert body["email_ready"] is False

    events = await audit("business.notification_settings_updated")
    assert events[-1] == {
        "changed": ["email_sender", "resend_from_address", "resend_api_key_encrypted"]
    }
    assert "re_live_super_secret" not in str(events)


async def test_a_field_left_out_of_the_patch_is_left_alone(client):
    await as_admin(client)
    await client.patch(
        NOTIFICATIONS,
        json={
            "email_sender": "resend",
            "resend_from_address": "hello@cedar.example",
            "resend_api_key": "re_live_super_secret",
        },
    )

    resp = await client.patch(NOTIFICATIONS, json={"resend_from_address": "new@cedar.example"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["resend_from_address"] == "new@cedar.example"
    # The key was never resent, and is still there.
    assert body["resend_api_key_set"] is True


async def test_configuring_smtp_and_twilio_and_reminder_intervals(client):
    await as_admin(client)

    resp = await client.patch(
        NOTIFICATIONS,
        json={
            "email_sender": "smtp",
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_username": "cedar",
            "smtp_password": "s3cret",
            "smtp_from_address": "hello@cedar.example",
            "sms_enabled": True,
            "twilio_account_sid": "ACtest",
            "twilio_auth_token": "authtoken",
            "twilio_from_number": "+15551234567",
            "reminder_intervals_hours": [48, 24, 1],
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["smtp_host"] == "smtp.example.com"
    assert body["smtp_port"] == 587
    assert body["smtp_password_set"] is True
    assert body["sms_enabled"] is True
    assert body["sms_ready"] is True
    assert body["twilio_auth_token_set"] is True
    assert "s3cret" not in resp.text
    assert "authtoken" not in resp.text
    assert body["reminder_intervals_hours"] == [48, 24, 1]


async def test_choosing_none_clears_the_email_sender(client):
    await as_admin(client)
    await client.patch(
        NOTIFICATIONS,
        json={
            "email_sender": "resend",
            "resend_from_address": "hello@cedar.example",
            "resend_api_key": "re_live_key",
        },
    )

    resp = await client.patch(NOTIFICATIONS, json={"email_sender": "none"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["email_sender"] is None
    assert resp.json()["email_ready"] is False


@pytest.mark.parametrize(
    "intervals",
    [
        [],
        [24, 24],
        [0, 24],
        [-1, 24],
        [24, 900],
    ],
)
async def test_reminder_interval_validation_rejects_nonsense(client, intervals):
    await as_admin(client)

    resp = await client.patch(NOTIFICATIONS, json={"reminder_intervals_hours": intervals})

    assert resp.status_code == 422, resp.text


# --- a credential edit un-verifies its sender ------------------------------------------------


async def test_rotating_the_resend_key_resets_the_verified_timestamp(client, sent_emails):
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
    assert verified.json()["resend_domain_verified_at"] is not None

    resp = await client.patch(NOTIFICATIONS, json={"resend_api_key": "re_live_key_rotated"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["resend_domain_verified_at"] is None
    assert resp.json()["email_ready"] is False


# --- test-send: the real adapter, through the recording fake --------------------------------


async def test_sending_a_test_email_actually_sends_and_verifies(client, sent_emails):
    await as_admin(client)
    await client.patch(
        NOTIFICATIONS,
        json={
            "email_sender": "resend",
            "resend_from_address": "hello@cedar.example",
            "resend_api_key": "re_live_key",
        },
    )

    resp = await client.post(f"{NOTIFICATIONS}/test-email", json={"to": "owner@cedar.example"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["resend_domain_verified_at"] is not None
    assert body["email_ready"] is True
    assert len(sent_emails) == 1
    assert sent_emails[0].to == "owner@cedar.example"

    events = await audit("business.notification_email_verified")
    assert events[-1] == {"email_sender": "resend"}


async def test_a_failed_test_email_never_verifies(client, sent_emails, fail_next_send):
    await as_admin(client)
    await client.patch(
        NOTIFICATIONS,
        json={
            "email_sender": "resend",
            "resend_from_address": "hello@cedar.example",
            "resend_api_key": "re_live_key",
        },
    )
    fail_email_next, _ = fail_next_send
    fail_email_next.append(RuntimeError("Resend said no"))

    resp = await client.post(f"{NOTIFICATIONS}/test-email", json={"to": "owner@cedar.example"})

    assert resp.status_code == 400, resp.text
    assert sent_emails == []
    follow_up = await client.get(NOTIFICATIONS)
    assert follow_up.json()["resend_domain_verified_at"] is None
    assert follow_up.json()["email_ready"] is False


async def test_a_test_email_is_refused_with_no_sender_chosen(client):
    await as_admin(client)

    resp = await client.post(f"{NOTIFICATIONS}/test-email", json={"to": "owner@cedar.example"})

    assert resp.status_code == 400, resp.text


async def test_sending_a_test_sms_actually_sends(client, sent_sms):
    await as_admin(client)
    await client.patch(
        NOTIFICATIONS,
        json={
            "sms_enabled": True,
            "twilio_account_sid": "ACtest",
            "twilio_auth_token": "authtoken",
            "twilio_from_number": "+15551234567",
        },
    )

    resp = await client.post(f"{NOTIFICATIONS}/test-sms", json={"to": "+15559876543"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"sent": True}
    assert len(sent_sms) == 1
    assert sent_sms[0].to == "+15559876543"


async def test_a_failed_test_sms_reports_the_error(client, sent_sms, fail_next_send):
    await as_admin(client)
    await client.patch(
        NOTIFICATIONS,
        json={
            "sms_enabled": True,
            "twilio_account_sid": "ACtest",
            "twilio_auth_token": "authtoken",
            "twilio_from_number": "+15551234567",
        },
    )
    _, fail_sms_next = fail_next_send
    fail_sms_next.append(RuntimeError("Twilio said no"))

    resp = await client.post(f"{NOTIFICATIONS}/test-sms", json={"to": "+15559876543"})

    assert resp.status_code == 400, resp.text
    assert sent_sms == []


async def test_a_test_sms_is_refused_while_sms_is_disabled(client):
    await as_admin(client)

    resp = await client.post(f"{NOTIFICATIONS}/test-sms", json={"to": "+15559876543"})

    assert resp.status_code == 400, resp.text


# --- walk-in queue toggle (Phase 7 Task 1, #12) ----------------------------------------------
#
# Only the toggle's own GET/PATCH round trip: there is no queue CRUD, nav entry or display
# screen yet (Tasks 2/8) for this flag to gate, so there is nothing else to test here.


async def test_the_walk_in_queue_toggle_round_trips(client):
    await as_admin(client)

    resp = await client.patch(NOTIFICATIONS, json={"enable_walk_in_queue": True})

    assert resp.status_code == 200, resp.text
    assert resp.json()["enable_walk_in_queue"] is True

    events = await audit("business.notification_settings_updated")
    assert events[-1] == {"changed": ["enable_walk_in_queue"]}

    # And it persists across a fresh GET, not just echoed back on the PATCH response.
    again = await client.get(NOTIFICATIONS)
    assert again.json()["enable_walk_in_queue"] is True


async def test_the_walk_in_queue_toggle_is_left_alone_by_an_unrelated_patch(client):
    await as_admin(client)
    await client.patch(NOTIFICATIONS, json={"enable_walk_in_queue": True})

    resp = await client.patch(NOTIFICATIONS, json={"sms_enabled": True})

    assert resp.status_code == 200, resp.text
    assert resp.json()["enable_walk_in_queue"] is True


# --- low-stock alert opt-in (#62) --------------------------------------------------------------
#
# Only the toggle's own GET/PATCH round trip and its default — `tests/test_low_stock_alerts.py`
# is where it actually gates a queued send.


async def test_low_stock_alert_email_enabled_defaults_off(client):
    await as_admin(client)

    resp = await client.get(NOTIFICATIONS)

    assert resp.json()["low_stock_alert_email_enabled"] is False


async def test_the_low_stock_alert_toggle_round_trips(client):
    await as_admin(client)

    resp = await client.patch(NOTIFICATIONS, json={"low_stock_alert_email_enabled": True})

    assert resp.status_code == 200, resp.text
    assert resp.json()["low_stock_alert_email_enabled"] is True

    events = await audit("business.notification_settings_updated")
    assert events[-1] == {"changed": ["low_stock_alert_email_enabled"]}

    again = await client.get(NOTIFICATIONS)
    assert again.json()["low_stock_alert_email_enabled"] is True


# --- templates: their own sub-resource, audited by field name only --------------------------


async def test_editing_a_template_updates_it_and_audits_field_names_only(client):
    await as_admin(client)
    body = (await client.get(NOTIFICATIONS)).json()
    template = next(
        t
        for t in body["templates"]
        if t["notification_type"] == "booking_confirmation" and t["channel"] == "email"
    )

    resp = await client.put(
        f"{NOTIFICATIONS}/templates/{template['id']}",
        json={
            "subject_template": "See you soon, $client_name",
            "body_template": "Hi $client_name, confirmed for $appointment_time.",
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["subject_template"] == "See you soon, $client_name"

    follow_up = (await client.get(NOTIFICATIONS)).json()
    saved = next(t for t in follow_up["templates"] if t["id"] == template["id"])
    assert saved["body_template"] == "Hi $client_name, confirmed for $appointment_time."

    events = await audit("business.notification_template_updated")
    assert events[-1] == {
        "notification_type": "booking_confirmation",
        "channel": "email",
        "changed": ["subject_template", "body_template"],
    }
    assert "confirmed for" not in str(events)


async def test_an_email_template_refuses_an_empty_subject(client):
    await as_admin(client)
    body = (await client.get(NOTIFICATIONS)).json()
    template = next(t for t in body["templates"] if t["channel"] == "email")

    resp = await client.put(
        f"{NOTIFICATIONS}/templates/{template['id']}",
        json={"subject_template": "   ", "body_template": "Hi $client_name."},
    )

    assert resp.status_code == 422, resp.text


async def test_an_sms_template_never_gets_a_subject_even_if_one_is_sent(client):
    await as_admin(client)
    body = (await client.get(NOTIFICATIONS)).json()
    template = next(t for t in body["templates"] if t["channel"] == "sms")

    resp = await client.put(
        f"{NOTIFICATIONS}/templates/{template['id']}",
        json={"subject_template": "This should be ignored", "body_template": "Hi $client_name."},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["subject_template"] is None


async def test_editing_an_unknown_template_is_a_404(client):
    await as_admin(client)

    resp = await client.put(
        f"{NOTIFICATIONS}/templates/{uuid.uuid4()}",
        json={"subject_template": "s", "body_template": "b"},
    )

    assert resp.status_code == 404, resp.text


# --- who may do any of this ------------------------------------------------------------------

WRITES = [
    ("PATCH", NOTIFICATIONS, {"sms_enabled": True}),
    ("POST", f"{NOTIFICATIONS}/test-email", {"to": "owner@cedar.example"}),
    ("POST", f"{NOTIFICATIONS}/test-sms", {"to": "+15559876543"}),
]


@pytest.mark.parametrize("method,path,body", WRITES)
async def test_a_write_is_refused_outside_admin_mode(client, method, path, body):
    resp = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert resp.status_code == 200, resp.text

    answer = await client.request(method, path, json=body)

    assert answer.status_code == 403
    assert answer.json()["code"] == "admin_mode_required"


async def test_reads_are_also_refused_to_a_role_without_the_admin_capability(client):
    await as_admin(client)
    role = await client.post(
        "/api/admin/roles",
        json={"name": "Front desk", "description": "Reception.", "capabilities": ["schedule.view"]},
    )
    assert role.status_code == 201, role.text
    await add_account(
        "desk@cedar.example", await hash_password(STAFF_PASSWORD), role=role.json()["id"]
    )
    client.cookies.clear()
    resp = await client.post(
        "/api/auth/login", json={"email": "desk@cedar.example", "password": STAFF_PASSWORD}
    )
    assert resp.status_code == 200, resp.text

    answer = await client.get(NOTIFICATIONS)

    assert answer.status_code == 403

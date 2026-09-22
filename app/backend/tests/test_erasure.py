"""Erasure requests and the purge job (Task 7, #42; ADR-0001 §3-§6, pre-flight D5a/D5b).

S2: the held / not-held decision and the words the client is told. S1: the request over HTTP
as `linsuite_app`, with Celery eager so `finish_erasure` (on the purge engine) has run by the
time the response is back; the nightly `purge_expired`; suppression everywhere a client is
listed or booked. S5 for the role boundary lives in `test_schema.py`.

The two errors that matter: removing anything under a live retention hold (every held field
is asserted by name below), and a request path reaching the purge role.
"""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from core.config import get_settings
from core.db import get_purge_engine, session_scope
from customers import erasure, retention, tasks
from tests.test_access_log import access_rows, add_role, clean_access_log  # noqa: F401
from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    APPOINTMENTS,
    CUSTOMERS,
    EMAIL,
    OTHER_PASSWORD,
    PASSWORD,
    as_admin,
    as_staff,
    at,
    book,
    claimed_instance,
    make_service,
    me_staff_id,
    put_hours,
)
from tests.test_customer_profile import complete_visits, ready_customer
from tests.test_document_keys import key_rows
from tests.test_groups import book_group
from tests.test_retention_api import entry, switch, years_ago

NOW = datetime(2026, 9, 21, 12, tzinfo=UTC)
TORONTO = "America/Toronto"

# Everything D5b purges on every request, held or not.
ALWAYS_PURGED = (
    "email",
    "phone",
    "emergency_contact_name",
    "emergency_contact_phone",
    "emergency_contact_relationship",
    "secondary_contact_name",
    "secondary_contact_phone",
    "secondary_contact_email",
    "notes",
)
PROFILE = {
    "email": "priya@example.com",
    "emergency_contact_name": "Ravi Nair",
    "emergency_contact_phone": "416-555-0100",
    "emergency_contact_relationship": "Father",
    "secondary_contact_name": "Asha Nair",
    "secondary_contact_phone": "416-555-0101",
    "secondary_contact_email": "asha@example.com",
    "notes": "Prefers the corner room.",
}


# --- S2: the decision --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expires", "held"),
    [
        (None, False),
        (NOW - timedelta(seconds=1), False),
        (NOW + timedelta(seconds=1), True),
        (retention.INFINITY, True),
    ],
    ids=["no_hold", "expired", "future", "infinity"],
)
def test_held_is_a_non_null_hold_that_has_not_passed(expires, held):
    assert erasure.is_held(expires, NOW) is held


def test_a_dated_hold_is_explained_with_its_local_date():
    # 04:59Z on 15 Mar is still 14 Mar in Toronto: the date is the business's, not UTC's.
    expires = datetime(2041, 3, 15, 3, 59, 59, 999999, tzinfo=UTC)

    reason = erasure.held_reason(expires, TORONTO)

    assert reason == "Regulated health record — retained until 14 Mar 2041, then destroyed"


def test_an_indefinite_hold_says_why_there_is_no_date():
    reason = erasure.held_reason(retention.INFINITY, TORONTO)

    assert "no date of birth" in reason
    assert "retained" in reason


def test_what_is_retained_is_named_only_when_held():
    assert erasure.retained(held=True) == ["Name", "Date of birth", "Visit history"]
    assert erasure.retained(held=False) == []


# --- S1 helpers ----------------------------------------------------------------------------


def erasure_url(customer_id: str) -> str:
    return f"{CUSTOMERS}/{customer_id}/erasure"


async def erase(client, customer_id: str, **body):
    return await client.post(erasure_url(customer_id), json=body)


async def fill_profile(client, customer_id: str, dob: date | None = None) -> None:
    body = dict(PROFILE)
    if dob is not None:
        body["date_of_birth"] = dob.isoformat()
    resp = await client.patch(f"{CUSTOMERS}/{customer_id}", json=body)
    assert resp.status_code == 200, resp.text


async def row(customer_id: str) -> dict:
    async with session_scope() as db:
        found = await db.execute(
            text("SELECT * FROM customers WHERE id = :id"), {"id": customer_id}
        )
        return dict(found.one()._mapping)


async def request_row(customer_id: str) -> dict:
    async with session_scope() as db:
        found = await db.execute(
            text("SELECT * FROM erasure_requests WHERE customer_id = :id"), {"id": customer_id}
        )
        return dict(found.one()._mapping)


async def events(event_type: str) -> list[dict]:
    async with get_purge_engine().connect() as purge:
        rows = await purge.execute(
            text(
                "SELECT actor_user_id, target_id, metadata FROM audit_events "
                "WHERE event_type = :t ORDER BY id"
            ),
            {"t": event_type},
        )
        return [dict(r._mapping) for r in rows]


async def admin_id() -> uuid.UUID:
    async with session_scope() as db:
        return await db.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": EMAIL})


async def as_owner(statement: str, **params) -> None:
    """The schema owner's hand — the only way a test can move a hold into the past."""
    owner = create_async_engine(get_settings().database_url_migrate)
    try:
        async with owner.begin() as conn:
            await conn.execute(text(statement), params)
    finally:
        await owner.dispose()


async def held_minor(client) -> tuple[str, str, str]:
    """`regulated_health` (the default), a 12-year-old with a completed visit, a full profile
    and a clinical entry: held until their 28th birthday. Returns (customer, service, staff)."""
    customer_id, service, me = await ready_customer(client)
    await complete_visits(client, service, me, customer_id, 1)
    await fill_profile(client, customer_id, years_ago(12))
    await entry(customer_id)
    return customer_id, service, me


def run_nightly() -> None:
    tasks.purge_expired.delay()


# --- S1: not held ------------------------------------------------------------------------


async def test_an_unheld_request_anonymises_shreds_the_key_and_audits_both_halves(client):
    await as_admin(client)
    await switch(client, "general_business")
    customer_id, _, _ = await ready_customer(client)
    await fill_profile(client, customer_id, years_ago(30))
    assert await key_rows(customer_id) == 1

    resp = await erase(client, customer_id, note="Asked by phone.")

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["held"] is False
    assert body["retained"] == []
    assert body["held_reason"] is None and body["held_until"] is None
    assert body["purged_at"] is not None

    stored = await row(customer_id)
    assert (stored["first_name"], stored["last_name"]) == ("Erased", "Client")
    assert stored["date_of_birth"] is None
    for field in ALWAYS_PURGED:
        assert stored[field] is None, field
    assert stored["suppressed_at"] is not None

    assert await key_rows(customer_id) == 0
    request = await request_row(customer_id)
    assert request["purged_at"] is not None
    assert request["note"] == "Asked by phone."
    assert request["held_until"] is None

    requested = await events("customer.erasure_requested")
    assert len(requested) == 1
    assert requested[0]["actor_user_id"] == await admin_id()
    assert requested[0]["target_id"] == customer_id
    assert requested[0]["metadata"] == {
        "held": False,
        "held_until": None,
        "request_id": str(request["id"]),
    }
    destroyed = await events("customer.key_destroyed")
    assert destroyed == [
        {
            "actor_user_id": None,
            "target_id": customer_id,
            "metadata": {"authority": "linsuite_purge", "request_id": str(request["id"])},
        }
    ]
    # Never the erased values, in either row.
    for event in requested + destroyed:
        assert "Priya" not in str(event) and "priya@" not in str(event)


# --- S1: held ----------------------------------------------------------------------------


async def test_a_held_request_keeps_the_chart_purges_contacts_and_the_key_survives(client):
    await as_admin(client)
    customer_id, _, _ = await held_minor(client)
    before = await row(customer_id)
    assert before["retention_expires_at"] is not None

    resp = await erase(client, customer_id)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["held"] is True
    assert body["retained"] == ["Name", "Date of birth", "Visit history"]
    local_end = before["retention_expires_at"].astimezone(ZoneInfo(TORONTO)).date()
    assert body["held_until"] == local_end.isoformat()
    assert f"retained until {local_end.day} {local_end:%b %Y}" in body["held_reason"]
    assert body["purged_at"] is None

    stored = await row(customer_id)
    # Every held field, by name.
    assert stored["first_name"] == before["first_name"] == "Priya"
    assert stored["last_name"] == before["last_name"] == "Nair"
    assert stored["date_of_birth"] == before["date_of_birth"] == years_ago(12)
    assert stored["retention_expires_at"] == before["retention_expires_at"]
    assert stored["last_clinical_entry_at"] == before["last_clinical_entry_at"]
    for field in ALWAYS_PURGED:
        assert stored[field] is None, field
    assert stored["suppressed_at"] is not None

    # The purge trigger refused, and that meant "held", not an error.
    assert await key_rows(customer_id) == 1
    assert await events("customer.key_destroyed") == []
    request = await request_row(customer_id)
    assert request["purged_at"] is None
    assert request["held_until"] == before["retention_expires_at"]
    assert request["held_reason"] == body["held_reason"]

    profile = (await client.get(f"{CUSTOMERS}/{customer_id}")).json()
    assert len(profile["appointments"]) == 1
    assert profile["customer"]["first_name"] == "Priya"


async def test_a_hold_with_no_date_of_birth_is_held_indefinitely(client):
    await as_admin(client)
    customer_id, _, _ = await ready_customer(client)
    await entry(customer_id)

    body = (await erase(client, customer_id)).json()

    assert body["held"] is True and body["held_until"] is None
    assert "no date of birth" in body["held_reason"]
    assert await key_rows(customer_id) == 1
    assert (await request_row(customer_id))["held_until"] == retention.INFINITY


# --- the nightly job ---------------------------------------------------------------------


async def test_once_the_hold_passes_the_nightly_job_finishes_the_request_exactly_once(client):
    await as_admin(client)
    customer_id, _, _ = await held_minor(client)
    assert (await erase(client, customer_id)).status_code == 201
    await as_owner(
        "UPDATE customers SET retention_expires_at = now() - interval '1 day' WHERE id = :id",
        id=customer_id,
    )

    run_nightly()

    assert await key_rows(customer_id) == 0
    stored = await row(customer_id)
    assert (stored["first_name"], stored["last_name"]) == ("Erased", "Client")
    assert stored["date_of_birth"] is None
    assert (await request_row(customer_id))["purged_at"] is not None
    destroyed = await events("customer.key_destroyed")
    assert len(destroyed) == 1
    assert destroyed[0]["metadata"]["request_id"] == str((await request_row(customer_id))["id"])

    run_nightly()

    assert len(await events("customer.key_destroyed")) == 1


async def test_the_nightly_job_leaves_a_live_hold_alone(client):
    await as_admin(client)
    customer_id, _, _ = await held_minor(client)
    assert (await erase(client, customer_id)).status_code == 201

    run_nightly()

    assert await key_rows(customer_id) == 1
    assert (await row(customer_id))["first_name"] == "Priya"
    assert (await request_row(customer_id))["purged_at"] is None
    assert await events("customer.key_destroyed") == []


async def test_an_expired_hold_without_a_request_loses_its_key_and_keeps_its_profile(client):
    """Owner-confirmed: the nightly job never anonymises anybody who did not ask."""
    await as_admin(client)
    customer_id, _, _ = await held_minor(client)
    await as_owner(
        "UPDATE customers SET retention_expires_at = now() - interval '1 day' WHERE id = :id",
        id=customer_id,
    )

    run_nightly()
    run_nightly()

    assert await key_rows(customer_id) == 0
    stored = await row(customer_id)
    assert stored["first_name"] == "Priya"
    assert stored["email"] == PROFILE["email"]
    assert stored["date_of_birth"] == years_ago(12)
    assert stored["suppressed_at"] is None
    assert [e["metadata"] for e in await events("customer.key_destroyed")] == [
        {"authority": "linsuite_purge", "request_id": None}
    ]


async def test_an_unheld_client_without_a_request_is_never_touched_by_the_nightly_job(client):
    await as_admin(client)
    customer_id, _, _ = await ready_customer(client)

    run_nightly()

    assert await key_rows(customer_id) == 1
    assert await events("customer.key_destroyed") == []


async def test_a_lost_enqueue_loses_nothing_and_the_nightly_job_finishes_it(client, monkeypatch):
    await as_admin(client)
    await switch(client, "general_business")
    customer_id, _, _ = await ready_customer(client)

    def broker_down(*_, **__):
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(tasks.finish_erasure, "delay", broker_down)
    resp = await erase(client, customer_id)
    monkeypatch.undo()

    assert resp.status_code == 201, resp.text
    assert await key_rows(customer_id) == 1
    assert (await request_row(customer_id))["purged_at"] is None
    assert (await row(customer_id))["first_name"] == "Erased"

    run_nightly()

    assert await key_rows(customer_id) == 0
    assert (await request_row(customer_id))["purged_at"] is not None
    assert len(await events("customer.key_destroyed")) == 1


async def test_finish_erasure_is_idempotent(client):
    await as_admin(client)
    await switch(client, "general_business")
    customer_id, _, _ = await ready_customer(client)
    assert (await erase(client, customer_id)).status_code == 201
    request_id = str((await request_row(customer_id))["id"])

    tasks.finish_erasure.delay(request_id)
    tasks.finish_erasure.delay(request_id)

    assert len(await events("customer.key_destroyed")) == 1


async def test_a_held_client_with_no_key_row_is_never_anonymised(client):
    """A client from before 0024 has no key to shred. The missing row must not be read as
    "the shred happened", or a held chart would lose its name."""
    await as_admin(client)
    customer_id, _, _ = await held_minor(client)
    await as_owner("DELETE FROM customer_document_keys WHERE customer_id = :id", id=customer_id)

    assert (await erase(client, customer_id)).status_code == 201
    run_nightly()

    assert (await row(customer_id))["first_name"] == "Priya"
    assert (await request_row(customer_id))["purged_at"] is None


# --- suppression -------------------------------------------------------------------------


async def test_a_suppressed_client_is_unlisted_unbookable_and_still_openable(client):
    await as_admin(client)
    customer_id, service, me = await held_minor(client)
    assert (await erase(client, customer_id)).status_code == 201

    listed = (await client.get(CUSTOMERS)).json()
    assert customer_id not in {c["id"] for c in listed["customers"]}
    assert listed["total"] == 0
    for q in ("pri", "nair"):
        found = (await client.get(CUSTOMERS, params={"q": q})).json()
        assert found["customers"] == [] and found["total"] == 0

    single = await book(client, service, me, at("15:00"), customer_id=customer_id)
    assert single.status_code == 422, single.text
    assert single.json()["code"] == "customer_suppressed"
    group = await book_group(
        client, [{"service_id": service, "staff_id": me}], customer_id=customer_id
    )
    assert group.status_code == 422, group.text
    assert group.json()["code"] == "customer_suppressed"

    before = len(await access_rows())
    profile = await client.get(f"{CUSTOMERS}/{customer_id}")
    assert profile.status_code == 200, profile.text
    customer = profile.json()["customer"]
    assert customer["suppressed"] is True
    assert customer["erasure"]["held"] is True
    assert customer["erasure"]["retained"] == ["Name", "Date of birth", "Visit history"]
    assert "retained until" in customer["erasure"]["held_reason"]
    assert len(await access_rows()) == before + 1


async def test_an_unsuppressed_profile_says_so(client):
    await as_admin(client)
    customer_id, _, _ = await ready_customer(client)

    customer = (await client.get(f"{CUSTOMERS}/{customer_id}")).json()["customer"]

    assert customer["suppressed"] is False and customer["erasure"] is None


async def test_a_suppressed_profile_cannot_be_refilled_but_a_held_dob_can_be_corrected(client):
    await as_admin(client)
    held, _, _ = await held_minor(client)
    assert (await erase(client, held)).status_code == 201

    refill = await client.patch(f"{CUSTOMERS}/{held}", json={"email": "back@example.com"})
    assert refill.status_code == 409, refill.text
    assert refill.json()["code"] == "customer_suppressed"
    dob = await client.patch(f"{CUSTOMERS}/{held}", json={"date_of_birth": "2014-05-01"})
    assert dob.status_code == 200, dob.text

    await switch(client, "general_business")
    tomb = (await client.post(CUSTOMERS, json={"first_name": "Sam", "last_name": "O"})).json()
    assert (await erase(client, tomb["id"])).status_code == 201
    again = await client.patch(f"{CUSTOMERS}/{tomb['id']}", json={"date_of_birth": "1990-01-01"})
    assert again.status_code == 409, again.text


# --- who may ask ---------------------------------------------------------------------------


async def test_erasure_needs_admin_mode_and_the_capability_and_happens_once(client):
    await as_admin(client)
    customer_id, _, _ = await ready_customer(client)
    await add_role(client, "Office", ["admin", "customers.view", "customers.manage"], "d@x.io")

    client.cookies.clear()
    await as_staff(client, EMAIL, PASSWORD)
    staff_mode = await erase(client, customer_id)
    assert staff_mode.status_code == 403, staff_mode.text
    assert staff_mode.json()["code"] == "admin_mode_required"

    client.cookies.clear()
    await as_admin(client, "d@x.io", OTHER_PASSWORD)
    no_capability = await erase(client, customer_id)
    assert no_capability.status_code == 403, no_capability.text
    assert no_capability.json()["code"] == "capability_required"
    assert (await row(customer_id))["suppressed_at"] is None

    client.cookies.clear()
    await as_admin(client)
    assert (await erase(client, customer_id)).status_code == 201
    second = await erase(client, customer_id)
    assert second.status_code == 409, second.text
    assert len(await events("customer.erasure_requested")) == 1


async def test_an_unknown_client_is_404(client):
    await as_admin(client)

    assert (await erase(client, str(uuid.uuid4()))).status_code == 404


# --- fix round 1 -------------------------------------------------------------------------


async def test_a_passed_hold_reads_as_expired_and_nothing_is_promised_kept(client, monkeypatch):
    """A hold that ended last month is not a hold: the profile says so (not "held until" a
    past date), the request anonymises, and — with the enqueue lost, so `purged_at` is still
    null — the summary does not claim a name is being retained on a row already renamed."""
    await as_admin(client)
    customer_id, _, _ = await held_minor(client)
    await as_owner(
        "UPDATE customers SET retention_expires_at = now() - interval '30 days' WHERE id = :id",
        id=customer_id,
    )
    stored = (await row(customer_id))["retention_expires_at"]

    before = (await client.get(f"{CUSTOMERS}/{customer_id}")).json()["customer"]
    assert before["retention"] == {
        "status": "expired",
        "expires_on": stored.astimezone(ZoneInfo(TORONTO)).date().isoformat(),
    }

    def broker_down(*_, **__):
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(tasks.finish_erasure, "delay", broker_down)
    body = (await erase(client, customer_id)).json()
    monkeypatch.undo()

    assert body["held"] is False and body["retained"] == []
    after = (await client.get(f"{CUSTOMERS}/{customer_id}")).json()["customer"]
    assert after["first_name"] == "Erased"
    assert after["erasure"]["purged_at"] is None
    assert after["erasure"]["held"] is False
    assert after["erasure"]["retained"] == [] and after["erasure"]["held_reason"] is None


async def test_a_tombstone_that_later_gains_a_hold_still_refuses_a_dob(client):
    """The DOB exception is for a chart that was held when erasure was asked — not for an
    anonymous tombstone that a later clinical entry happened to put under a hold."""
    await as_admin(client)
    await switch(client, "general_business")
    customer_id, _, _ = await ready_customer(client)
    assert (await erase(client, customer_id)).status_code == 201
    await switch(client, "regulated_health")
    await entry(customer_id)
    assert (await row(customer_id))["retention_expires_at"] == retention.INFINITY

    resp = await client.patch(f"{CUSTOMERS}/{customer_id}", json={"date_of_birth": "1990-01-01"})

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "customer_suppressed"


async def _wait_for_a_lock_wait(booking: asyncio.Task) -> None:
    """Until some app-role backend is waiting on a row lock — the booking, queued behind the
    erasure's `FOR UPDATE`. Gives up after 10 s, or as soon as the booking finishes unblocked."""
    for _ in range(200):
        if booking.done():
            return
        async with session_scope() as db:
            waiting = await db.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE usename = 'linsuite_app' AND wait_event_type = 'Lock'"
                )
            )
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the booking never queued behind the erasure's lock")


@pytest.mark.parametrize("kind", ["single", "group"])
async def test_a_booking_racing_an_erasure_waits_for_it_and_is_refused(client, kind):
    """The erasure handler holds the customer `FOR UPDATE` until it commits; the booking
    reads the customer `FOR KEY SHARE`, so it queues, then reads `suppressed_at` as committed.
    Stood in for here by an app-role transaction doing exactly what the handler does."""
    await as_admin(client)
    customer_id, service, me = await ready_customer(client)

    async with session_scope() as eraser:
        await eraser.execute(
            text("SELECT 1 FROM customers WHERE id = :id FOR UPDATE"), {"id": customer_id}
        )
        await eraser.execute(
            text("UPDATE customers SET suppressed_at = now() WHERE id = :id"), {"id": customer_id}
        )
        if kind == "single":
            request = book(client, service, me, at("15:00"), customer_id=customer_id)
        else:
            request = book_group(
                client, [{"service_id": service, "staff_id": me}], customer_id=customer_id
            )
        booking = asyncio.create_task(request)
        await _wait_for_a_lock_wait(booking)
        await eraser.commit()
    resp = await booking

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "customer_suppressed"
    async with session_scope() as db:
        booked = await db.scalar(
            text("SELECT count(*) FROM appointments WHERE customer_id = :id"), {"id": customer_id}
        )
    assert booked == 0

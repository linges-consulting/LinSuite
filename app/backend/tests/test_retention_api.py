"""S1 + S5: retention expiry as the database holds it, driven through its only writers.

`record_clinical_entry` has no production caller until Phases 8/9 (pre-flight D4), so these
tests call it directly — through the app's own role, in a session of its own, committed the
way a form submission's handler will commit it. Everything else goes over HTTP: the DOB
correction (`PATCH /customers/{id}`), the profile switch (`PATCH /admin/business/security`),
the timezone change, and appointment completion (which must *not* touch retention).
"""

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.db import Base, session_scope
from customers import retention
from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    APPOINTMENTS,
    CUSTOMERS,
    as_admin,
    at,
    book,
    claimed_instance,
    make_customer,
)
from tests.test_customer_profile import complete_visits, ready_customer

SECURITY = "/api/admin/business/security"
TORONTO = ZoneInfo("America/Toronto")
ENTRY = datetime(2026, 6, 15, 16, tzinfo=UTC)  # 12:00 in Toronto, 15 Jun 2026


def end_of(day: date, zone: ZoneInfo = TORONTO) -> datetime:
    return datetime(day.year, day.month, day.day, 23, 59, 59, 999999, tzinfo=zone)


def years_ago(n: int) -> date:
    return date(date.today().year - n, 3, 14)


async def entry(customer_id: str, at: datetime = ENTRY) -> None:
    async with session_scope() as db:
        await retention.record_clinical_entry(db, uuid.UUID(customer_id), at)
        await db.commit()


async def stored(customer_id: str) -> tuple[datetime | None, datetime | None]:
    """(last_clinical_entry_at, retention_expires_at), straight off the row."""
    async with session_scope() as db:
        row = (
            await db.execute(
                text(
                    "SELECT last_clinical_entry_at, retention_expires_at FROM customers "
                    "WHERE id = :id"
                ),
                {"id": customer_id},
            )
        ).one()
    return row[0], row[1]


async def set_dob(client, customer_id: str, dob: date | None):
    resp = await client.patch(
        f"{CUSTOMERS}/{customer_id}", json={"date_of_birth": dob.isoformat() if dob else None}
    )
    assert resp.status_code == 200, resp.text


async def switch(client, profile: str):
    resp = await client.patch(SECURITY, json={"retention_profile": profile})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def profile_events() -> list[dict]:
    async with session_scope() as db:
        rows = await db.execute(
            text(
                "SELECT metadata::text FROM audit_events "
                "WHERE event_type = 'business.retention_profile_changed' ORDER BY id"
            )
        )
    return [json.loads(r[0]) for r in rows]


async def new_customer(client, first: str) -> str:
    resp = await client.post(CUSTOMERS, json={"first_name": first, "last_name": "Client"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# --- the writers ----------------------------------------------------------------------------


async def test_a_minor_is_held_to_the_end_of_their_28th_birthday_and_a_dob_patch_recomputes(
    client,
):
    await as_admin(client)
    customer_id = await make_customer(client)
    await set_dob(client, customer_id, years_ago(12))

    await entry(customer_id)
    born = years_ago(12)
    assert await stored(customer_id) == (ENTRY, end_of(born.replace(year=born.year + 28)))

    await set_dob(client, customer_id, years_ago(40))
    assert (await stored(customer_id))[1] == end_of(date(2036, 6, 15))

    await set_dob(client, customer_id, None)
    assert (await stored(customer_id))[1] == datetime.max  # 'infinity', read back


async def test_infinity_is_really_infinity_in_the_database(client):
    """Not a year-9999 instant: the purge predicate `< now()` must be false for it forever."""
    await as_admin(client)
    customer_id = await make_customer(client)
    await entry(customer_id)
    async with session_scope() as db:
        is_infinite = await db.scalar(
            text("SELECT retention_expires_at = 'infinity' FROM customers WHERE id = :id"),
            {"id": customer_id},
        )
    assert is_infinite is True


async def test_a_client_with_no_entry_stays_unheld_whatever_their_dob(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    for dob in (years_ago(12), years_ago(40), None):
        await set_dob(client, customer_id, dob)
        assert await stored(customer_id) == (None, None)


async def test_a_backdated_entry_never_shortens_the_hold(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    await set_dob(client, customer_id, years_ago(40))
    await entry(customer_id)

    await entry(customer_id, ENTRY - timedelta(days=400))

    assert await stored(customer_id) == (ENTRY, end_of(date(2036, 6, 15)))


async def test_a_naive_entry_time_is_refused(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    with pytest.raises(ValueError):
        await entry(customer_id, datetime(2026, 6, 15, 12))


async def test_completing_an_appointment_is_not_a_clinical_entry(client):
    """A booking is an operational record, not an entry in the chart (pre-flight D4) —
    neither for a client with no chart yet, nor for one already held."""
    customer_id, service, staff_id = await ready_customer(client)
    await set_dob(client, customer_id, years_ago(40))
    await complete_visits(client, service, staff_id, customer_id, 1)  # 09:00
    assert await stored(customer_id) == (None, None)

    await entry(customer_id)
    held = await stored(customer_id)
    made = await book(client, service, staff_id, at("11:00"), customer_id=customer_id)
    assert made.status_code == 201, made.text
    done = await client.post(f"{APPOINTMENTS}/{made.json()['id']}/complete", json={})
    assert done.status_code == 200, done.text
    assert await stored(customer_id) == held


# --- the profile switch ---------------------------------------------------------------------


async def test_switching_profiles_recomputes_every_client_and_is_audited_once_each_way(client):
    await as_admin(client)
    minor, adult, unknown, none = [
        await new_customer(client, n) for n in ("Minor", "Adult", "Unknown", "None")
    ]
    await set_dob(client, minor, years_ago(12))
    await set_dob(client, adult, years_ago(40))
    for c in (minor, adult, unknown):
        await entry(c)
    held = {c: (await stored(c))[1] for c in (minor, adult, unknown, none)}
    assert held[unknown] == datetime.max and held[none] is None

    read = await switch(client, "general_business")
    assert read["retention_profile"] == "general_business"
    for c in held:
        assert (await stored(c))[1] is None
    # The entry itself is a fact about the chart, not about the profile: it survives.
    assert (await stored(adult))[0] == ENTRY

    await switch(client, "regulated_health")
    for c, expected in held.items():
        assert (await stored(c))[1] == expected

    events = await profile_events()
    assert [e["retention_profile"] for e in events] == [
        ["regulated_health", "general_business"],
        ["general_business", "regulated_health"],
    ]
    assert events[0]["holds_released"] == 3 and events[0]["held"] == 0
    assert events[1]["held"] == 3


async def test_a_new_entry_under_general_business_holds_nothing(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    await switch(client, "general_business")
    await entry(customer_id)
    assert await stored(customer_id) == (ENTRY, None)


async def test_the_profile_is_unchosen_until_saved_and_saving_the_default_confirms_it(client):
    await as_admin(client)
    before = (await client.get(SECURITY)).json()
    assert before["retention_profile"] == "regulated_health"
    assert before["retention_profile_chosen"] is False

    after = await switch(client, "regulated_health")

    assert after["retention_profile_chosen"] is True
    assert (await client.get(SECURITY)).json()["retention_profile_chosen"] is True
    # Confirming the default is a decision somebody made, so it has a name on it too.
    assert [e["retention_profile"] for e in await profile_events()] == [
        ["regulated_health", "regulated_health"]
    ]


async def test_saving_the_mfa_switches_neither_confirms_nor_changes_the_profile(client):
    await as_admin(client)
    resp = await client.patch(
        SECURITY, json={"mfa_required_for_admin": True, "mfa_email_otp_allowed": False}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["retention_profile_chosen"] is False
    assert await profile_events() == []


@pytest.mark.parametrize("value", ["something_else", "", None, 1])
async def test_a_third_profile_is_refused_by_the_api(client, value):
    await as_admin(client)
    resp = await client.patch(SECURITY, json={"retention_profile": value})
    if value is None:
        # null means "not this field" — same as leaving it out; nothing is chosen.
        assert resp.status_code == 200, resp.text
        assert resp.json()["retention_profile_chosen"] is False
    else:
        assert resp.status_code == 422, resp.text


async def test_changing_the_timezone_recomputes_the_end_of_day(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    await set_dob(client, customer_id, years_ago(40))
    await entry(customer_id)

    resp = await client.patch(
        "/api/admin/business/timezone", json={"timezone": "America/Vancouver"}
    )
    assert resp.status_code == 200, resp.text

    assert (await stored(customer_id))[1] == end_of(
        date(2036, 6, 15), ZoneInfo("America/Vancouver")
    )


# --- what the profile shows -----------------------------------------------------------------


async def test_the_profile_reports_each_retention_state_and_the_list_never_does(client):
    await as_admin(client)
    customer_id = await make_customer(client)

    async def shown() -> dict:
        resp = await client.get(f"{CUSTOMERS}/{customer_id}")
        assert resp.status_code == 200, resp.text
        return resp.json()["customer"]["retention"]

    assert await shown() == {"status": "not_held", "expires_on": None}
    await entry(customer_id)
    assert await shown() == {"status": "needs_dob", "expires_on": None}
    await set_dob(client, customer_id, years_ago(40))
    assert await shown() == {"status": "held", "expires_on": "2036-06-15"}

    listed = (await client.get(CUSTOMERS)).json()["customers"][0]
    assert "retention" not in listed
    patched = await client.patch(f"{CUSTOMERS}/{customer_id}", json={})
    assert "retention" not in patched.json()


def test_retention_is_declared_phi():
    from core.access_log import PHI_FIELDS

    assert "retention" in PHI_FIELDS


# --- S5 -------------------------------------------------------------------------------------


async def test_s5_the_check_refuses_a_third_profile(client):
    async with session_scope() as db:
        with pytest.raises(DBAPIError, match="ck_businesses_retention_profile"):
            await db.execute(text("UPDATE businesses SET retention_profile = 'spa'"))


async def test_s5_the_expiry_index_exists_and_is_declared_on_the_model(database):
    async with session_scope() as db:
        exists = await db.scalar(
            text(
                "SELECT count(*) FROM pg_indexes WHERE tablename = 'customers' "
                "AND indexname = 'ix_customers_retention_expires_at'"
            )
        )
    assert exists == 1
    declared = {i.name for i in Base.metadata.tables["customers"].indexes}
    assert "ix_customers_retention_expires_at" in declared

"""The "who accessed this client's record" report (ADR-0002 §6, #43), at S1.

What is worth pinning: the report lists every open of one client's record, newest first, with
the actor's name resolved at read time and the role name as it was at the time of access; the
date range narrows it and is the partition key, so Postgres reads only the partitions in it; a
row whose actor no longer resolves is still shown (the log never loses a row because a person
left); it is an Admin Mode capability; and reading it writes no access row of its own — it is
identifiers about the chart, not the chart (pre-flight §8).
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import create_async_engine

from core.config import get_settings
from core.db import session_scope
from tests.test_access_log import (  # noqa: F401 — the autouse fixtures come along
    TABLE,
    access_rows,
    add_role,
    clean_access_log,
)
from tests.test_appointments import (  # noqa: F401
    CUSTOMERS,
    EMAIL,
    OTHER_PASSWORD,
    PASSWORD,
    as_admin,
    as_staff,
    claimed_instance,
    make_customer,
)

DESK = "desk@cedar.example"


def report(customer_id: str) -> str:
    return f"/api/admin/customers/{customer_id}/access-log"


async def display_name(email: str) -> str:
    async with session_scope() as db:
        return await db.scalar(
            text(
                "SELECT s.display_name FROM staff s JOIN users u ON u.id = s.user_id "
                "WHERE u.email = :e"
            ),
            {"e": email},
        )


async def insert_row(customer_id: str, occurred_at: datetime, actor: uuid.UUID | None = None):
    """A row as the schema owner writes it — the only way to date one in the past, and to
    name an actor who matches no account."""
    engine = create_async_engine(get_settings().database_url_migrate)
    try:
        async with engine.begin() as owner:
            await owner.execute(
                text(
                    f"INSERT INTO {TABLE} (occurred_at, actor_user_id, actor_role, customer_id, "
                    "resource_type, resource_id, action, ip) VALUES (:at, :u, 'Staff', :c, "
                    "'customer_profile', :r, 'view', '10.0.0.7')"
                ),
                {
                    "at": occurred_at,
                    "u": actor or uuid.uuid4(),
                    "c": uuid.UUID(customer_id),
                    "r": customer_id,
                },
            )
    finally:
        await engine.dispose()


def days_ago(n: int) -> datetime:
    return datetime.combine(date.today() - timedelta(days=n), datetime.min.time(), UTC).replace(
        hour=16
    )


# --- the report itself ------------------------------------------------------------------


async def test_two_opens_by_two_people_are_listed_newest_first_with_names_and_roles(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    assert (await client.get(f"{CUSTOMERS}/{customer_id}")).status_code == 200
    await add_role(client, "Front desk", ["customers.view"], DESK)
    client.cookies.clear()
    await as_staff(client, DESK, OTHER_PASSWORD)
    assert (await client.get(f"{CUSTOMERS}/{customer_id}")).status_code == 200
    client.cookies.clear()
    await as_admin(client)
    logged = len(await access_rows())

    resp = await client.get(report(customer_id))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    entries = body["entries"]
    assert [e["actor_name"] for e in entries] == [
        await display_name(DESK),
        await display_name(EMAIL),
    ]
    assert [e["actor_role"] for e in entries] == ["Front desk", "Administrator"]
    assert {e["resource_type"] for e in entries} == {"customer_profile"}
    assert {e["resource_id"] for e in entries} == {customer_id}
    assert {e["action"] for e in entries} == {"view"}
    assert entries[0]["occurred_at"] > entries[1]["occurred_at"]
    assert entries[0]["ip"] == "127.0.0.1"
    assert body["timezone"] == "America/Toronto"
    # Reading who looked is not itself a look (pre-flight §8): no row of its own.
    assert len(await access_rows()) == logged
    # Identifiers about the chart, never the chart: nothing of the client's own is on it.
    assert "Priya" not in resp.text and "Nair" not in resp.text


async def test_the_range_narrows_the_rows_and_defaults_to_the_last_90_days(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    for n in (5, 40, 200):
        await insert_row(customer_id, days_ago(n))
    # Somebody else's record never leaks into this one's report.
    await insert_row(str(uuid.uuid4()), days_ago(5))

    default = (await client.get(report(customer_id))).json()
    assert default["total"] == 2
    today = datetime.now(ZoneInfo("America/Toronto")).date()
    assert default["to"] == today.isoformat()
    assert default["from"] == (today - timedelta(days=90)).isoformat()

    around_40 = {
        "from": (date.today() - timedelta(days=41)).isoformat(),
        "to": (date.today() - timedelta(days=39)).isoformat(),
    }
    narrowed = (await client.get(report(customer_id), params=around_40)).json()
    assert narrowed["total"] == 1
    assert len(narrowed["entries"]) == 1

    everything = {"from": (date.today() - timedelta(days=365)).isoformat()}
    assert (await client.get(report(customer_id), params=everything)).json()["total"] == 3


async def test_an_inverted_range_is_a_422(client):
    await as_admin(client)
    customer_id = await make_customer(client)

    resp = await client.get(report(customer_id), params={"from": "2026-05-02", "to": "2026-05-01"})

    assert resp.status_code == 422, resp.text


async def test_pages_are_capped_at_100_and_total_counts_every_page(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    for n in (1, 2, 3):
        await insert_row(customer_id, days_ago(n))

    assert (await client.get(report(customer_id), params={"page_size": 101})).status_code == 422
    first = (await client.get(report(customer_id), params={"page_size": 2})).json()
    second = (await client.get(report(customer_id), params={"page_size": 2, "page": 2})).json()

    assert first["total"] == second["total"] == 3
    assert len(first["entries"]) == 2 and len(second["entries"]) == 1
    # Stable ordering: no row on both pages, none on neither.
    ids = [e["id"] for e in first["entries"] + second["entries"]]
    assert len(set(ids)) == 3


async def test_an_actor_matching_no_account_is_still_listed_by_id(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    ghost = uuid.uuid4()
    await insert_row(customer_id, days_ago(1), actor=ghost)

    entries = (await client.get(report(customer_id))).json()["entries"]

    assert [e["actor_name"] for e in entries] == [str(ghost)]
    assert entries[0]["actor_user_id"] == str(ghost)
    assert entries[0]["ip"] == "10.0.0.7"


async def test_a_date_range_prunes_to_the_partitions_in_it(client):
    """With a range, Postgres reads only the partitions it covers — the point of partitioning
    by `occurred_at` (ADR-0002 §5). Next year's partition is never touched by this year's."""
    from customers.access_report import window_query

    year = date.today().year
    start = datetime(year, 3, 1, tzinfo=UTC)
    stmt = window_query(uuid.uuid4(), start, start + timedelta(days=30))
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    async with session_scope() as db:
        plan = "\n".join(await db.scalars(text(f"EXPLAIN {sql}")))

    assert f"{TABLE}_{year}" in plan
    assert f"{TABLE}_{year + 1}" not in plan


# --- who may read it ----------------------------------------------------------------------


async def test_staff_mode_is_refused_as_admin_mode_required(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    client.cookies.clear()
    await as_staff(client, EMAIL, PASSWORD)

    resp = await client.get(report(customer_id))

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "admin_mode_required"


async def test_admin_mode_without_audit_view_is_refused_and_a_custom_role_may_hold_it(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    await add_role(client, "Office", ["admin", "customers.view"], DESK)
    await add_role(client, "Auditor", ["admin", "audit.view"], "rae@cedar.example")

    client.cookies.clear()
    await as_admin(client, DESK, OTHER_PASSWORD)
    refused = await client.get(report(customer_id))
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "capability_required"

    client.cookies.clear()
    await as_admin(client, "rae@cedar.example", OTHER_PASSWORD)
    allowed = await client.get(report(customer_id))
    assert allowed.status_code == 200, allowed.text
    assert await access_rows() == []


async def test_the_capability_is_listed_with_its_description_and_group(client):
    await as_admin(client)

    listed = (await client.get("/api/admin/capabilities")).json()["capabilities"]

    audit = next(c for c in listed if c["key"] == "audit.view")
    assert audit["description"] == "See who has accessed a client's record."
    assert audit["group"] == "Administration"
    assert audit["requires_admin_mode"] is True

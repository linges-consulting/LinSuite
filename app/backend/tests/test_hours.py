"""S1: the three availability inputs — working hours, time off, and the days the doors are shut.

This ticket stores them; Task 14 is what intersects them into bookable slots.

**Working hours are wall-clock and nothing else.** A row is `(weekday, start_minute,
end_minute)` with no zone on it, because "Mondays 09:00–12:00" is a rule about a clock face
(CLAUDE.md "Time", PRD §1). Several rows on one weekday *are* the split shift — the gap
between them is the lunch break, expressed by its absence. The week is replaced whole rather
than edited block by block: the screen is a matrix, an administrator edits it as one, and a
per-block API would let a half-applied week exist.

**Overlap is refused by the database**, not by a Python loop —
`EXCLUDE USING gist (staff_id, weekday, int4range(start_minute, end_minute))`. A code path
that forgets to check receives a constraint violation instead of quietly double-booking
somebody into two shifts at once.

**Time off is instants**, because a specific absence is a moment and not a rule. It is
entered in local terms and converted with the business timezone at write time, and the
response carries both halves so nothing has to re-derive one from the other.

**Closures are business-wide and one row per date.** Statutory holidays are imported for a
year from the `holidays` package against the business's province — no on-the-fly computation,
and a deleted statutory day is simply a day this business chose to work.
"""

from datetime import date

import pytest
from sqlalchemy import text

from core.db import get_purge_engine, session_scope

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"
OTHER_PASSWORD = "correct horse battery 2"

SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": EMAIL,
    "admin_password": PASSWORD,
}

STAFF = "/api/admin/staff"
CLOSURES = "/api/admin/closures"

# Every address this module signs in as. See `claimed_instance`.
ADDRESSES = (EMAIL, "rae@cedar.example", "deputy@cedar.example", "sched@cedar.example")


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async with get_purge_engine().begin() as purge:
        await purge.execute(text("DELETE FROM audit_events"))
    async with session_scope() as db:
        for table in (
            "working_hours",
            "time_off",
            "closures",
            "staff",
            "password_reset_tokens",
            "users",
            "businesses",
            "setup_token",
        ):
            await db.execute(text(f"DELETE FROM {table}"))
        await db.execute(text("DELETE FROM roles WHERE NOT is_system"))
        await db.commit()

    from auth import setup, throttle
    from core.redis import get_redis

    # Redis outlives the database reset, and every module in this suite signs in as the same
    # address. An earlier module's failed attempt would otherwise land this one's first login
    # inside the progressive delay — a throttle doing its job, on a test that is not about it.
    await get_redis().delete(*(throttle.delay_key(e) for e in ADDRESSES))

    async with session_scope() as db:
        token = await setup.bootstrap_setup_token(db)
    resp = await client.post("/api/setup", json={**SETUP, "token": token})
    assert resp.status_code == 201, resp.text
    async with session_scope() as db:
        await db.execute(
            text("UPDATE businesses SET mfa_required_for_admin = false, province = 'ON'")
        )
        await db.commit()
    client.cookies.clear()
    yield


# --- helpers --------------------------------------------------------------------------------


async def as_admin(client, email=EMAIL, password=PASSWORD):
    login = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    resp = await client.post("/api/auth/mode", json={"mode": "admin", "password": password})
    assert resp.status_code == 200, resp.text


async def as_staff(client, email, password):
    login = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text


async def me_staff_id(client) -> str:
    """The staff row the wizard made for the administrator."""
    roster = await client.get(STAFF)
    assert roster.status_code == 200, roster.text
    return next(row["id"] for row in roster.json()["staff"] if row["email"] == EMAIL)


async def add_colleague(client, email: str, password: str, *, role: str = "Staff") -> str:
    """A second account with its own staff row, signed out again. Returns the staff id."""
    from core.security import hash_password
    from tests.conftest import add_account

    async with session_scope() as db:
        role_id = str(await db.scalar(text("SELECT id FROM roles WHERE name = :n"), {"n": role}))
    await add_account(email, await hash_password(password), role=role_id)
    roster = await client.get(STAFF)
    return next(row["id"] for row in roster.json()["staff"] if row["email"] == email)


def block(weekday: int, start: int, end: int) -> dict:
    return {"weekday": weekday, "start_minute": start, "end_minute": end}


async def put_hours(client, staff_id: str, blocks: list[dict]):
    return await client.put(f"{STAFF}/{staff_id}/hours", json={"blocks": blocks})


async def remove(client, url: str):
    """DELETE still has to declare `application/json` — the CSRF middleware covers every
    mutating method, not just the ones with a body (`main.py`)."""
    return await client.request("DELETE", url, headers={"Content-Type": "application/json"})


def time_off_url(staff_id: str) -> str:
    return f"/api/staff/{staff_id}/time-off"


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


# --- the weekly matrix ------------------------------------------------------------------------


async def test_a_split_shift_persists_as_two_blocks_on_one_weekday(client):
    await as_admin(client)
    staff_id = await me_staff_id(client)

    # 09:00–12:00, lunch, 15:00–18:00 — the case PRD §1 names.
    resp = await put_hours(client, staff_id, [block(0, 540, 720), block(0, 900, 1080)])

    assert resp.status_code == 200, resp.text
    assert resp.json()["blocks"] == [
        {"weekday": 0, "start_minute": 540, "end_minute": 720},
        {"weekday": 0, "start_minute": 900, "end_minute": 1080},
    ]
    read_back = await client.get(f"{STAFF}/{staff_id}/hours")
    assert read_back.json()["blocks"] == resp.json()["blocks"]


async def test_the_week_is_replaced_whole(client):
    await as_admin(client)
    staff_id = await me_staff_id(client)
    await put_hours(client, staff_id, [block(0, 540, 720), block(1, 540, 720)])

    resp = await put_hours(client, staff_id, [block(2, 600, 780)])

    assert resp.status_code == 200, resp.text
    assert resp.json()["blocks"] == [{"weekday": 2, "start_minute": 600, "end_minute": 780}]


async def test_an_empty_week_clears_every_block(client):
    await as_admin(client)
    staff_id = await me_staff_id(client)
    await put_hours(client, staff_id, [block(0, 540, 720)])

    resp = await put_hours(client, staff_id, [])

    assert resp.status_code == 200, resp.text
    assert resp.json()["blocks"] == []


async def test_saving_the_same_week_again_does_not_collide_with_itself(client):
    """The replace is a DELETE and an INSERT in one transaction. If the exclusion constraint
    could still see the rows this statement just deleted, pressing Save twice would be a 409
    — which is exactly the shape of bug a passing suite of *different* weeks would miss."""
    await as_admin(client)
    staff_id = await me_staff_id(client)
    week = [block(0, 540, 720), block(0, 900, 1080)]
    await put_hours(client, staff_id, week)

    resp = await put_hours(client, staff_id, week)

    assert resp.status_code == 200, resp.text
    assert len(resp.json()["blocks"]) == 2


async def test_blocks_come_back_in_week_order_whatever_order_they_were_sent(client):
    await as_admin(client)
    staff_id = await me_staff_id(client)

    resp = await put_hours(
        client, staff_id, [block(3, 900, 1080), block(0, 540, 720), block(3, 540, 720)]
    )

    assert [(b["weekday"], b["start_minute"]) for b in resp.json()["blocks"]] == [
        (0, 540),
        (3, 540),
        (3, 900),
    ]


async def test_overlapping_blocks_on_one_weekday_are_refused(client):
    """And the week that was already there survives the refusal, row for row.

    The replace is a DELETE and then INSERTs in one transaction, so a 409 halfway through
    must roll the DELETE back too. Asserting only "the request was refused" would pass just
    as happily against an endpoint that had wiped the week on its way to failing — which is
    the worse of the two bugs, because nothing on screen would say so.
    """
    await as_admin(client)
    staff_id = await me_staff_id(client)
    await put_hours(client, staff_id, [block(0, 540, 720), block(2, 600, 780)])

    async def rows():
        async with session_scope() as db:
            return list(
                (
                    await db.execute(
                        text(
                            "SELECT id, staff_id, weekday, start_minute, end_minute "
                            "FROM working_hours ORDER BY weekday, start_minute"
                        )
                    )
                ).all()
            )

    before = await rows()
    assert len(before) == 2

    resp = await put_hours(client, staff_id, [block(0, 540, 720), block(0, 660, 1080)])

    assert resp.status_code == 409, resp.text
    assert await rows() == before


async def test_blocks_that_merely_touch_are_allowed(client):
    """09:00–12:00 and 12:00–15:00 is somebody who does not take lunch, not a conflict."""
    await as_admin(client)
    staff_id = await me_staff_id(client)

    resp = await put_hours(client, staff_id, [block(0, 540, 720), block(0, 720, 900)])

    assert resp.status_code == 200, resp.text


async def test_the_same_hours_on_two_staff_members_do_not_collide(client):
    await as_admin(client)
    mine = await me_staff_id(client)
    theirs = await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)

    assert (await put_hours(client, mine, [block(0, 540, 720)])).status_code == 200
    assert (await put_hours(client, theirs, [block(0, 540, 720)])).status_code == 200


@pytest.mark.parametrize(
    "bad",
    [
        {"weekday": 0, "start_minute": 720, "end_minute": 540},  # backwards
        {"weekday": 0, "start_minute": 540, "end_minute": 540},  # empty
        {"weekday": 0, "start_minute": 542, "end_minute": 720},  # not a five-minute step
        {"weekday": 0, "start_minute": 540, "end_minute": 1441},  # past midnight
        {"weekday": 0, "start_minute": -5, "end_minute": 720},  # before midnight
        {"weekday": 7, "start_minute": 540, "end_minute": 720},  # an eighth day
    ],
)
async def test_a_block_that_is_not_a_time_is_refused(client, bad):
    await as_admin(client)
    staff_id = await me_staff_id(client)

    resp = await put_hours(client, staff_id, [bad])

    assert resp.status_code == 422, resp.text


async def test_hours_for_somebody_who_does_not_exist_are_a_404(client):
    await as_admin(client)

    resp = await put_hours(client, "00000000-0000-0000-0000-000000000000", [])

    assert resp.status_code == 404, resp.text


async def test_replacing_the_week_is_recorded(client):
    await as_admin(client)
    staff_id = await me_staff_id(client)

    await put_hours(client, staff_id, [block(0, 540, 720), block(0, 900, 1080)])

    recorded = [row for row in await audit() if row[0] == "staff.hours_replaced"]
    assert len(recorded) == 1
    assert '"blocks": 2' in recorded[0][1]


# --- the rules are wall-clock, and stay wall-clock ------------------------------------------------


async def test_changing_the_business_timezone_leaves_every_rule_untouched(client):
    """The invariant this whole design exists for (CLAUDE.md "Time").

    The stored row never moves — it is a clock face, not a moment. What moves is the instants
    it produces, which is `scheduling/clock.py`'s job and is asserted here beside it so the
    two halves of the claim are read together.
    """
    from scheduling.clock import local_blocks_to_instants

    await as_admin(client)
    staff_id = await me_staff_id(client)
    await put_hours(client, staff_id, [block(0, 540, 720), block(0, 900, 1080)])

    async def rows():
        async with session_scope() as db:
            return list(
                (
                    await db.execute(
                        text(
                            "SELECT id, staff_id, weekday, start_minute, end_minute "
                            "FROM working_hours ORDER BY weekday, start_minute"
                        )
                    )
                ).all()
            )

    before = await rows()
    moved = await client.patch(
        "/api/admin/business/timezone", json={"timezone": "America/Vancouver"}
    )
    assert moved.status_code == 200, moved.text
    after = await rows()

    assert after == before
    monday = date(2026, 6, 15)
    assert local_blocks_to_instants([(540, 720)], monday, "America/Toronto")[0][0].isoformat() == (
        "2026-06-15T13:00:00+00:00"
    )
    assert local_blocks_to_instants([(540, 720)], monday, "America/Vancouver")[0][
        0
    ].isoformat() == ("2026-06-15T16:00:00+00:00")


# --- time off and vacation --------------------------------------------------------------------


async def test_an_all_day_range_covers_local_midnight_to_midnight(client):
    await as_admin(client)
    staff_id = await me_staff_id(client)

    resp = await client.post(
        time_off_url(staff_id),
        json={
            "all_day": True,
            "start_date": "2026-07-01",
            "end_date": "2026-07-05",
            "reason": "Vacation",
        },
    )

    assert resp.status_code == 201, resp.text
    entry = resp.json()
    # Inclusive of the last day: the absence ends at midnight *after* 5 July, local.
    assert entry["start_date"] == "2026-07-01"
    assert entry["end_date"] == "2026-07-05"
    # 00:00 EDT is 04:00 UTC.
    assert entry["starts_at"] == "2026-07-01T04:00:00Z"
    assert entry["ends_at"] == "2026-07-06T04:00:00Z"
    assert entry["all_day"] is True
    assert entry["reason"] == "Vacation"


async def test_a_single_all_day_absence_needs_no_end_date(client):
    await as_admin(client)
    staff_id = await me_staff_id(client)

    resp = await client.post(
        time_off_url(staff_id), json={"all_day": True, "start_date": "2026-07-01"}
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["end_date"] == "2026-07-01"
    assert resp.json()["ends_at"] == "2026-07-02T04:00:00Z"


async def test_a_timed_absence_is_converted_with_the_business_timezone(client):
    await as_admin(client)
    staff_id = await me_staff_id(client)

    resp = await client.post(
        time_off_url(staff_id),
        json={
            "all_day": False,
            "starts_at_local": "2026-07-01T14:00:00",
            "ends_at_local": "2026-07-01T16:30:00",
            "reason": "Dentist",
        },
    )

    assert resp.status_code == 201, resp.text
    entry = resp.json()
    assert entry["starts_at"] == "2026-07-01T18:00:00Z"  # 14:00 EDT
    assert entry["ends_at"] == "2026-07-01T20:30:00Z"
    assert entry["starts_at_local"] == "2026-07-01T14:00:00"


@pytest.mark.parametrize(
    "day,hours",
    [
        # 8 March 2026: the clocks go forward, so this calendar day is 23 hours long.
        ("2026-03-08", 23),
        # 1 November 2026: they go back, and the day is 25.
        ("2026-11-01", 25),
    ],
)
async def test_an_all_day_absence_is_as_long_as_the_day_actually_is(client, day, hours):
    """ "Away all day" is a statement about a local calendar day, not about 24 hours.

    It falls out of storing the range half-open — local midnight to local midnight *after* —
    rather than by adding a day's worth of minutes, which is exactly the shortcut that would
    leave somebody bookable for the last hour of a fall-back Sunday.
    """
    await as_admin(client)
    staff_id = await me_staff_id(client)

    resp = await client.post(time_off_url(staff_id), json={"all_day": True, "start_date": day})

    assert resp.status_code == 201, resp.text
    async with session_scope() as db:
        span = await db.scalar(
            text("SELECT EXTRACT(EPOCH FROM (ends_at - starts_at)) / 3600 FROM time_off")
        )
    assert float(span) == hours


async def test_the_zone_in_force_is_the_business_one_at_write_time(client):
    """Entered as "14:00", stored as the instant that was 14:00 in Vancouver."""
    await as_admin(client)
    staff_id = await me_staff_id(client)
    await client.patch("/api/admin/business/timezone", json={"timezone": "America/Vancouver"})

    resp = await client.post(
        time_off_url(staff_id),
        json={
            "all_day": False,
            "starts_at_local": "2026-07-01T14:00:00",
            "ends_at_local": "2026-07-01T16:00:00",
        },
    )

    assert resp.json()["starts_at"] == "2026-07-01T21:00:00Z"  # 14:00 PDT


async def test_overlapping_time_off_for_one_person_is_refused(client):
    await as_admin(client)
    staff_id = await me_staff_id(client)
    first = await client.post(
        time_off_url(staff_id),
        json={"all_day": True, "start_date": "2026-07-01", "end_date": "2026-07-05"},
    )
    assert first.status_code == 201, first.text

    clash = await client.post(
        time_off_url(staff_id),
        json={"all_day": True, "start_date": "2026-07-05", "end_date": "2026-07-08"},
    )

    assert clash.status_code == 409, clash.text


async def test_two_people_may_be_away_at_once(client):
    await as_admin(client)
    mine = await me_staff_id(client)
    theirs = await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)
    body = {"all_day": True, "start_date": "2026-07-01", "end_date": "2026-07-05"}

    assert (await client.post(time_off_url(mine), json=body)).status_code == 201
    assert (await client.post(time_off_url(theirs), json=body)).status_code == 201


@pytest.mark.parametrize(
    "bad",
    [
        {"all_day": True, "start_date": "2026-07-05", "end_date": "2026-07-01"},
        {"all_day": True},
        {"all_day": False, "starts_at_local": "2026-07-01T14:00:00"},
        {
            "all_day": False,
            "starts_at_local": "2026-07-01T16:00:00",
            "ends_at_local": "2026-07-01T14:00:00",
        },
    ],
)
async def test_an_absence_that_is_not_a_span_is_refused(client, bad):
    await as_admin(client)
    staff_id = await me_staff_id(client)

    resp = await client.post(time_off_url(staff_id), json=bad)

    assert resp.status_code == 422, resp.text


async def test_time_off_is_listed_and_deleted(client):
    await as_admin(client)
    staff_id = await me_staff_id(client)
    created = await client.post(
        time_off_url(staff_id), json={"all_day": True, "start_date": "2026-07-01"}
    )
    entry_id = created.json()["id"]

    listed = await client.get(time_off_url(staff_id))
    assert [e["id"] for e in listed.json()["time_off"]] == [entry_id]

    removed = await remove(client, f"{time_off_url(staff_id)}/{entry_id}")
    assert removed.status_code == 204, removed.text
    assert (await client.get(time_off_url(staff_id))).json()["time_off"] == []
    assert "staff.time_off_created" in await events()
    assert "staff.time_off_deleted" in await events()


# --- who may book time off for whom ---------------------------------------------------------------


async def test_a_staff_member_books_their_own_time_off_without_admin_mode(client):
    await as_admin(client)
    mine = await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)
    client.cookies.clear()
    await as_staff(client, "rae@cedar.example", OTHER_PASSWORD)

    resp = await client.post(time_off_url(mine), json={"all_day": True, "start_date": "2026-07-01"})

    assert resp.status_code == 201, resp.text
    assert (await client.get(time_off_url(mine))).status_code == 200


async def test_a_staff_member_may_not_book_somebody_elses(client):
    await as_admin(client)
    owner = await me_staff_id(client)
    await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)
    client.cookies.clear()
    await as_staff(client, "rae@cedar.example", OTHER_PASSWORD)

    resp = await client.post(
        time_off_url(owner), json={"all_day": True, "start_date": "2026-07-01"}
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] in {"capability_required", "admin_mode_required"}


async def test_a_staff_member_may_not_delete_somebody_elses(client):
    await as_admin(client)
    owner = await me_staff_id(client)
    created = await client.post(
        time_off_url(owner), json={"all_day": True, "start_date": "2026-07-01"}
    )
    entry_id = created.json()["id"]
    await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)
    client.cookies.clear()
    await as_staff(client, "rae@cedar.example", OTHER_PASSWORD)

    resp = await remove(client, f"{time_off_url(owner)}/{entry_id}")

    assert resp.status_code == 403, resp.text


async def test_an_administrator_books_time_off_for_anybody(client):
    await as_admin(client)
    theirs = await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)

    resp = await client.post(
        time_off_url(theirs), json={"all_day": True, "start_date": "2026-07-01"}
    )

    assert resp.status_code == 201, resp.text


# --- closures ---------------------------------------------------------------------------------


async def test_a_manual_closure_is_added_and_listed_by_year(client):
    await as_admin(client)

    resp = await client.post(CLOSURES, json={"date": "2026-08-05", "name": "Staff retreat"})

    assert resp.status_code == 201, resp.text
    assert resp.json()["source"] == "manual"
    listed = (await client.get(f"{CLOSURES}?year=2026")).json()["closures"]
    assert [c["date"] for c in listed] == ["2026-08-05"]
    assert (await client.get(f"{CLOSURES}?year=2025")).json()["closures"] == []


async def test_a_date_can_only_be_closed_once(client):
    await as_admin(client)
    await client.post(CLOSURES, json={"date": "2026-08-05", "name": "Staff retreat"})

    again = await client.post(CLOSURES, json={"date": "2026-08-05", "name": "Something else"})

    assert again.status_code == 409, again.text


async def test_importing_statutory_holidays_inserts_the_province_s_own(client):
    await as_admin(client)

    resp = await client.post(f"{CLOSURES}/import-statutory?year=2026", json={})

    assert resp.status_code == 200, resp.text
    import holidays

    expected = holidays.country_holidays("CA", subdiv="ON", years=2026)
    assert resp.json()["added"] == len(expected)
    listed = (await client.get(f"{CLOSURES}?year=2026")).json()["closures"]
    assert {c["date"] for c in listed} == {d.isoformat() for d in expected}
    assert {c["source"] for c in listed} == {"statutory"}
    # Canada Day, whatever else the package knows about Ontario.
    assert "2026-07-01" in {c["date"] for c in listed}


async def test_importing_twice_adds_nothing_the_second_time(client):
    await as_admin(client)
    first = await client.post(f"{CLOSURES}/import-statutory?year=2026", json={})

    again = await client.post(f"{CLOSURES}/import-statutory?year=2026", json={})

    assert again.status_code == 200, again.text
    assert again.json()["added"] == 0
    assert again.json()["skipped"] == first.json()["added"]


async def test_an_import_never_overwrites_a_manual_closure_on_the_same_date(client):
    await as_admin(client)
    await client.post(CLOSURES, json={"date": "2026-07-01", "name": "Summer party"})

    await client.post(f"{CLOSURES}/import-statutory?year=2026", json={})

    listed = (await client.get(f"{CLOSURES}?year=2026")).json()["closures"]
    kept = next(c for c in listed if c["date"] == "2026-07-01")
    assert kept["name"] == "Summer party"
    assert kept["source"] == "manual"


async def test_a_statutory_day_can_be_deleted(client):
    """A business that works Canada Day deletes it, and it is gone. No overrides table —
    the row exists or it does not."""
    await as_admin(client)
    await client.post(f"{CLOSURES}/import-statutory?year=2026", json={})
    listed = (await client.get(f"{CLOSURES}?year=2026")).json()["closures"]
    canada_day = next(c for c in listed if c["date"] == "2026-07-01")

    removed = await remove(client, f"{CLOSURES}/{canada_day['id']}")

    assert removed.status_code == 204, removed.text
    after = (await client.get(f"{CLOSURES}?year=2026")).json()["closures"]
    assert "2026-07-01" not in {c["date"] for c in after}


async def test_re_importing_a_year_adds_back_a_deleted_statutory_day(client):
    """The honest cost of keeping no tombstones, pinned rather than left to be discovered.

    The import's only skip rule is "a row for this date already exists", so a deleted one is
    not remembered. That is one mechanism with one answer rather than two tables to keep in
    step — and the screen warns before the delete, which is where the cost is paid.
    """
    await as_admin(client)
    await client.post(f"{CLOSURES}/import-statutory?year=2026", json={})
    listed = (await client.get(f"{CLOSURES}?year=2026")).json()["closures"]
    canada_day = next(c for c in listed if c["date"] == "2026-07-01")
    await remove(client, f"{CLOSURES}/{canada_day['id']}")

    again = await client.post(f"{CLOSURES}/import-statutory?year=2026", json={})

    assert again.json()["added"] == 1
    after = (await client.get(f"{CLOSURES}?year=2026")).json()["closures"]
    assert "2026-07-01" in {c["date"] for c in after}


async def test_importing_without_a_province_says_so(client):
    await as_admin(client)
    async with session_scope() as db:
        await db.execute(text("UPDATE businesses SET province = NULL"))
        await db.commit()

    resp = await client.post(f"{CLOSURES}/import-statutory?year=2026", json={})

    assert resp.status_code == 422, resp.text


async def test_the_closure_trail_records_every_change(client):
    await as_admin(client)
    created = await client.post(CLOSURES, json={"date": "2026-08-05", "name": "Staff retreat"})
    await client.post(f"{CLOSURES}/import-statutory?year=2026", json={})
    await remove(client, f"{CLOSURES}/{created.json()['id']}")

    recorded = await audit()
    assert {"business.closure_added", "business.closures_imported", "business.closure_removed"} <= {
        row[0] for row in recorded
    }
    imported = next(row for row in recorded if row[0] == "business.closures_imported")
    assert '"year": 2026' in imported[1]
    assert '"count":' in imported[1]


# --- who may do any of this ---------------------------------------------------------------------


async def test_working_hours_need_users_manage(client):
    await as_admin(client)
    staff_id = await me_staff_id(client)
    role = await client.post(
        "/api/admin/roles",
        json={"name": "Deputy", "description": "Nearly.", "capabilities": ["admin"]},
    )
    assert role.status_code == 201, role.text
    from core.security import hash_password
    from tests.conftest import add_account

    await add_account(
        "deputy@cedar.example", await hash_password(OTHER_PASSWORD), role=role.json()["id"]
    )
    client.cookies.clear()
    await as_admin(client, "deputy@cedar.example", OTHER_PASSWORD)

    refusals = [
        await client.get(f"{STAFF}/{staff_id}/hours"),
        await put_hours(client, staff_id, []),
    ]

    assert [r.status_code for r in refusals] == [403, 403]
    assert {r.json()["code"] for r in refusals} == {"capability_required"}


async def test_working_hours_are_refused_outside_admin_mode(client):
    await as_admin(client)
    staff_id = await me_staff_id(client)
    client.cookies.clear()
    await as_staff(client, EMAIL, PASSWORD)  # Staff Mode

    resp = await put_hours(client, staff_id, [block(0, 540, 720)])

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "admin_mode_required"


async def test_closures_need_admin_mode(client):
    await as_admin(client)
    client.cookies.clear()
    await as_staff(client, EMAIL, PASSWORD)

    refusals = [
        await client.get(CLOSURES),
        await client.post(CLOSURES, json={"date": "2026-08-05", "name": "Retreat"}),
        await client.post(f"{CLOSURES}/import-statutory?year=2026", json={}),
        await remove(client, f"{CLOSURES}/00000000-0000-0000-0000-000000000000"),
    ]

    assert [r.status_code for r in refusals] == [403] * 4
    assert {r.json()["code"] for r in refusals} == {"admin_mode_required"}


async def test_closures_need_the_admin_capability(client):
    await as_admin(client)
    role = await client.post(
        "/api/admin/roles",
        json={"name": "Scheduler", "description": "Books.", "capabilities": ["schedule.manage"]},
    )
    assert role.status_code == 201, role.text
    from core.security import hash_password
    from tests.conftest import add_account

    await add_account(
        "sched@cedar.example", await hash_password(OTHER_PASSWORD), role=role.json()["id"]
    )
    client.cookies.clear()
    await as_staff(client, "sched@cedar.example", OTHER_PASSWORD)

    resp = await client.post(CLOSURES, json={"date": "2026-08-05", "name": "Retreat"})

    assert resp.status_code == 403, resp.text

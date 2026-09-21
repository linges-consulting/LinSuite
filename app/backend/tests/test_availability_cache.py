"""S1: Task 19 — availability caching and invalidation, over HTTP with real Postgres and
real Redis.

`test_slots.py` and `test_groups.py` already prove the two read routes' wiring against the
database; this file proves the layer in front of it: a repeated identical read is served
from cache (`slots.compute` is not called again), every mutation class that can change an
answer makes the next read recompute, a stray cached lie is never trusted by booking, Redis
being unreachable degrades to direct computation, and the cache key does not depend on
irrelevant things (query param order) while it does depend on relevant ones (the staff
filter).

Fixtures and helpers are the same ones `test_appointments.py` already built — one customer,
one service, one staff member with hours — imported rather than re-invented.
"""

import json
from datetime import date, timedelta

from sqlalchemy import text

from core.db import session_scope
from scheduling import cache as avail_cache
from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    APPOINTMENTS,
    AVAILABILITY,
    CUSTOMER,
    MONDAY,
    SERVICES,
    STAFF,
    add_colleague,
    as_admin,
    at,
    book,
    claimed_instance,
    make_resource,
    make_service,
    me_staff_id,
    move,
    put_hours,
)


async def ask(client, service_id: str, day: date = MONDAY, **params):
    query = {"service_id": service_id, "from": day.isoformat(), "to": day.isoformat(), **params}
    return await client.get(AVAILABILITY, params=query)


async def ask_group(client, service_ids: list[str], day: date = MONDAY, **params):
    query = {
        "services": ",".join(service_ids),
        "from": day.isoformat(),
        "to": day.isoformat(),
        **params,
    }
    return await client.get(f"{AVAILABILITY}/group", params=query)


def spy_on_compute(monkeypatch):
    """A count of real calls to `scheduling.slots.compute`, the loader that reads the
    database and runs the engine — what the cache exists to skip on a hit."""
    from scheduling import slots

    calls = []
    original = slots.compute

    async def counting(*args, **kwargs):
        calls.append(1)
        return await original(*args, **kwargs)

    monkeypatch.setattr(slots, "compute", counting)
    return calls


# --- served from cache -----------------------------------------------------------------------


async def test_a_repeated_identical_read_is_served_from_cache(client, monkeypatch):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    service = await make_service(client, [me])
    calls = spy_on_compute(monkeypatch)

    first = await ask(client, service, MONDAY)
    second = await ask(client, service, MONDAY)

    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.json() == second.json()
    assert len(calls) == 1


async def test_group_availability_is_served_from_cache_too(client, monkeypatch):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    service = await make_service(client, [me])
    calls = spy_on_compute(monkeypatch)

    first = await ask_group(client, [service])
    second = await ask_group(client, [service])

    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.json() == second.json()
    assert len(calls) == 1


async def test_the_cache_entry_carries_a_ttl_of_about_sixty_seconds(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    service = await make_service(client, [me])

    resp = await ask(client, service, MONDAY)
    assert resp.status_code == 200, resp.text

    from core.redis import get_redis

    redis = get_redis()
    generation = int(await redis.get(avail_cache._GENERATION_KEY) or 0)
    parts = {
        "service_id": service,
        "staff_id": None,
        "from": MONDAY.isoformat(),
        "to": MONDAY.isoformat(),
    }
    key = avail_cache._key("avail", generation, parts)
    ttl = await redis.ttl(key)
    assert 0 < ttl <= avail_cache.TTL_SECONDS


# --- key normalisation -------------------------------------------------------------------------


async def test_query_param_order_does_not_split_the_cache_but_a_different_filter_does(
    client, monkeypatch
):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    service = await make_service(client, [me])
    calls = spy_on_compute(monkeypatch)

    # Same question, params sent in a different order on the wire.
    await client.get(
        AVAILABILITY,
        params=[
            ("to", MONDAY.isoformat()),
            ("service_id", service),
            ("from", MONDAY.isoformat()),
        ],
    )
    await client.get(
        AVAILABILITY,
        params=[
            ("service_id", service),
            ("from", MONDAY.isoformat()),
            ("to", MONDAY.isoformat()),
        ],
    )
    assert len(calls) == 1

    # A different staff filter is a different question.
    await ask(client, service, MONDAY, staff_id=me)
    assert len(calls) == 2


# --- booking never trusts the cache -------------------------------------------------------------


async def test_booking_never_trusts_a_poisoned_cache(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    service = await make_service(client, [me])
    first = await book(client, service, me, at("10:00"))
    assert first.status_code == 201, first.text

    # Poison the cache directly: a fabricated "10:00 is still free" answer, written back
    # under the exact key the read route would ask for at the *current* generation — as if
    # some invalidation had been missed.
    from core.redis import get_redis

    redis = get_redis()
    generation = int(await redis.get(avail_cache._GENERATION_KEY) or 0)
    parts = {
        "service_id": service,
        "staff_id": None,
        "from": MONDAY.isoformat(),
        "to": MONDAY.isoformat(),
    }
    key = avail_cache._key("avail", generation, parts)
    poisoned = {
        "service_id": service,
        "timezone": "America/Toronto",
        "granularity_minutes": 15,
        "horizon_ends_on": (MONDAY + timedelta(days=365)).isoformat(),
        "days": [
            {
                "date": MONDAY.isoformat(),
                "slots": [{"starts_at": at("10:00"), "ends_at": at("11:00"), "staff_ids": [me]}],
            }
        ],
    }
    await redis.set(key, json.dumps(poisoned), ex=60)

    # The read now repeats the lie...
    read = await ask(client, service, MONDAY)
    assert read.status_code == 200, read.text
    assert any(s["starts_at"] == at("10:00") for s in read.json()["days"][0]["slots"])

    # ...but booking goes straight to the database and is refused regardless — "not offered"
    # (the engine, re-run fresh, never named 10:00 at all) or "slot taken" (it did, and the
    # database's own constraint is what actually caught the race) are both a correct refusal;
    # what matters is that it is never 201.
    resp = await book(client, service, me, at("10:00"))
    assert resp.status_code in (409, 422), resp.text
    async with session_scope() as db:
        # Still exactly the one appointment from before the cache was poisoned.
        assert await db.scalar(text("SELECT count(*) FROM appointments")) == 1


# --- Redis failure degrades to direct computation -----------------------------------------------


class _DeadRedis:
    """Every call raises, the way a connection to a dead host eventually does."""

    async def get(self, *a, **kw):
        raise ConnectionError("redis is down")

    async def set(self, *a, **kw):
        raise ConnectionError("redis is down")

    async def incr(self, *a, **kw):
        raise ConnectionError("redis is down")

    async def ttl(self, *a, **kw):
        raise ConnectionError("redis is down")


async def test_redis_unreachable_degrades_to_uncached_computation(client, monkeypatch):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    service = await make_service(client, [me])

    monkeypatch.setattr(avail_cache, "get_redis", lambda: _DeadRedis())

    resp = await ask(client, service, MONDAY)

    assert resp.status_code == 200, resp.text
    assert len(resp.json()["days"][0]["slots"]) == 9


async def test_a_failed_bump_does_not_fail_the_mutation(client, monkeypatch):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    service = await make_service(client, [me])

    monkeypatch.setattr(avail_cache, "get_redis", lambda: _DeadRedis())

    resp = await book(client, service, me, at("10:00"))

    assert resp.status_code == 201, resp.text


# --- invalidation, one per mutation class --------------------------------------------------------


async def _setup(client) -> tuple[str, str]:
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])  # Monday 09:00-12:00
    service = await make_service(client, [me])
    return me, service


async def test_booking_invalidates_the_slot_it_takes(client):
    me, service = await _setup(client)
    before = await ask(client, service, MONDAY)
    assert at("10:00") in [s["starts_at"] for s in before.json()["days"][0]["slots"]]

    booked = await book(client, service, me, at("10:00"))
    assert booked.status_code == 201, booked.text

    after = await ask(client, service, MONDAY)
    assert at("10:00") not in [s["starts_at"] for s in after.json()["days"][0]["slots"]]


async def test_cancelling_invalidates_the_slot_it_frees(client):
    me, service = await _setup(client)
    booked = await book(client, service, me, at("10:00"))
    assert booked.status_code == 201, booked.text
    taken = await ask(client, service, MONDAY)
    assert at("10:00") not in [s["starts_at"] for s in taken.json()["days"][0]["slots"]]

    cancelled = await client.post(f"{APPOINTMENTS}/{booked.json()['id']}/cancel", json={})
    assert cancelled.status_code == 200, cancelled.text

    freed = await ask(client, service, MONDAY)
    assert at("10:00") in [s["starts_at"] for s in freed.json()["days"][0]["slots"]]


async def test_moving_an_appointment_invalidates_both_its_old_and_new_slot(client):
    me, service = await _setup(client)
    booked = await book(client, service, me, at("09:00"))
    assert booked.status_code == 201, booked.text
    before = await ask(client, service, MONDAY)
    starts = [s["starts_at"] for s in before.json()["days"][0]["slots"]]
    assert at("09:00") not in starts
    assert at("10:00") in starts

    moved = await move(client, booked.json()["id"], starts_at=at("10:00"))
    assert moved.status_code == 200, moved.text

    after = await ask(client, service, MONDAY)
    starts = [s["starts_at"] for s in after.json()["days"][0]["slots"]]
    assert at("09:00") in starts
    assert at("10:00") not in starts


async def test_working_hours_change_invalidates_the_grid(client):
    me, service = await _setup(client)
    before = await ask(client, service, MONDAY)
    assert len(before.json()["days"][0]["slots"]) == 9  # 09:00-12:00, sixty minute service

    await put_hours(client, me, [(0, 780, 855)])  # 13:00-14:15

    after = await ask(client, service, MONDAY)
    starts = [s["starts_at"] for s in after.json()["days"][0]["slots"]]
    assert at("09:00") not in starts
    assert at("13:00") in starts


async def test_time_off_invalidates_the_covered_slots(client):
    me, service = await _setup(client)
    before = await ask(client, service, MONDAY)
    assert at("10:00") in [s["starts_at"] for s in before.json()["days"][0]["slots"]]

    away = await client.post(
        f"/api/staff/{me}/time-off",
        json={
            "all_day": False,
            "starts_at_local": f"{MONDAY.isoformat()}T10:00",
            "ends_at_local": f"{MONDAY.isoformat()}T10:30",
        },
    )
    assert away.status_code == 201, away.text

    after = await ask(client, service, MONDAY)
    assert at("10:00") not in [s["starts_at"] for s in after.json()["days"][0]["slots"]]


async def test_a_closure_invalidates_the_whole_day(client):
    me, service = await _setup(client)
    before = await ask(client, service, MONDAY)
    assert before.json()["days"][0]["slots"] != []

    closed = await client.post(
        "/api/admin/closures", json={"date": MONDAY.isoformat(), "name": "Retreat"}
    )
    assert closed.status_code == 201, closed.text

    after = await ask(client, service, MONDAY)
    assert after.json()["days"][0]["slots"] == []


async def test_a_service_buffer_change_invalidates_the_grid_even_though_the_key_is_unchanged(
    client,
):
    """The cache key is the service id, not its buffer — only the generation bump makes this
    safe. Proves the invalidation is wired, not just that the loader would answer correctly
    if it were re-run."""
    me, service = await _setup(client)
    before = await ask(client, service, MONDAY)
    assert len(before.json()["days"][0]["slots"]) == 9

    patched = await client.patch(f"{SERVICES}/{service}", json={"buffer_after_minutes": 45})
    assert patched.status_code == 200, patched.text

    after = await ask(client, service, MONDAY)
    # Sixty minutes plus a forty-five minute turnaround is a hundred and five; the last start
    # that still clears noon, turnaround included, is 10:15 — six quarter-hour starts from
    # 09:00. 10:30 would spill the turnaround past 12:00.
    assert len(after.json()["days"][0]["slots"]) == 6


async def test_staff_concurrency_change_invalidates_slots_it_now_allows(client):
    me, service = await _setup(client)
    booked = await book(client, service, me, at("10:00"))
    assert booked.status_code == 201, booked.text
    full = await ask(client, service, MONDAY)
    assert at("10:00") not in [s["starts_at"] for s in full.json()["days"][0]["slots"]]

    raised = await client.patch(f"{STAFF}/{me}", json={"max_concurrent_appointments": 2})
    assert raised.status_code == 200, raised.text

    after = await ask(client, service, MONDAY)
    assert at("10:00") in [s["starts_at"] for s in after.json()["days"][0]["slots"]]


async def test_a_resource_change_invalidates_slots_that_needed_it(client):
    await as_admin(client)
    me = await me_staff_id(client)
    ben = await add_colleague(client, "ben@cedar.example", "correct horse battery 2")
    await put_hours(client, me, [(0, 540, 720)])
    await put_hours(client, ben, [(0, 540, 720)])
    await make_resource(client, "equipment", "Laser 1")
    service = await make_service(client, [me], requirements=[{"kind": "equipment"}])
    # A second service, delivered by a different staff member, claims the only laser — so
    # `me`'s own read below is genuinely resource-constrained, never staff-constrained.
    other = await make_service(client, [ben], name="Other", requirements=[{"kind": "equipment"}])
    booked = await book(client, other, ben, at("10:00"))
    assert booked.status_code == 201, booked.text
    full = await ask(client, service, MONDAY)
    assert at("10:00") not in [s["starts_at"] for s in full.json()["days"][0]["slots"]]

    await make_resource(client, "equipment", "Laser 2")

    after = await ask(client, service, MONDAY)
    assert at("10:00") in [s["starts_at"] for s in after.json()["days"][0]["slots"]]


async def test_a_relevant_setting_change_invalidates_the_grid(client):
    me, service = await _setup(client)
    before = await ask(client, service, MONDAY)
    assert before.json()["granularity_minutes"] == 15

    profile = await client.get("/api/admin/business")
    changed = await client.put(
        "/api/admin/business", json={**profile.json(), "slot_granularity_minutes": 30}
    )
    assert changed.status_code == 200, changed.text

    after = await ask(client, service, MONDAY)
    assert after.json()["granularity_minutes"] == 30


async def test_group_booking_invalidates_both_links(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 1020)])
    facial = await make_service(client, [me], name="Facial", duration_minutes=60, price_cents=8000)

    before = await ask_group(client, [facial])
    starts = [s["starts_at"] for s in before.json()["days"][0]["slots"]]
    assert at("09:00") in starts

    group = await client.post(
        f"{APPOINTMENTS}/group",
        json={
            "starts_at": at("09:00"),
            "links": [{"service_id": facial, "staff_id": me}],
            "customer": CUSTOMER,
        },
    )
    assert group.status_code == 201, group.text

    after = await ask_group(client, [facial])
    starts = [s["starts_at"] for s in after.json()["days"][0]["slots"]]
    assert at("09:00") not in starts

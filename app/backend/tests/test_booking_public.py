"""S1: `GET /api/public/booking/availability` — the client-facing wrapper around the same
engine `GET /availability` uses (Phase 6 Task 1, #10).

The acceptance-critical case: a start reachable only through a staff override
(`tests/test_overrides.py`) is never offered here — proving `relax_advisory=False` actually
holds for this route, not merely that nothing happens to ask it to relax. Also covered: "any
available staff" (no `staff_id`) is the same `staff_id=None` mode the staff route already
uses, and a service not opted into online booking (`bookable_online`) is the same generic 404
an unknown service already is.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    MONDAY,
    as_admin,
    at,
    book,
    claimed_instance,
    make_service,
    me_staff_id,
    put_hours,
)

PUBLIC_AVAILABILITY = "/api/public/booking/availability"
TORONTO = ZoneInfo("America/Toronto")


async def public_slots(client, service_id: str, day=MONDAY, **params) -> list[str]:
    query = {"service_id": service_id, "from": day.isoformat(), "to": day.isoformat(), **params}
    resp = await client.get(PUBLIC_AVAILABILITY, params=query)
    assert resp.status_code == 200, resp.text
    return [
        datetime.fromisoformat(slot["starts_at"]).astimezone(TORONTO).strftime("%H:%M")
        for slot in resp.json()["days"][0]["slots"]
    ]


async def test_the_public_endpoint_offers_the_same_slots_the_staff_one_does(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])  # Monday 09:00-12:00
    service = await make_service(client, [me])

    resp = await client.get(
        PUBLIC_AVAILABILITY,
        params={"service_id": service, "from": MONDAY.isoformat(), "to": MONDAY.isoformat()},
    )

    assert resp.status_code == 200, resp.text
    # The shared `/api/public/` middleware treatment (`main.py`), not a bespoke second router.
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["service_id"] == service
    times = [
        datetime.fromisoformat(s["starts_at"]).astimezone(TORONTO).strftime("%H:%M")
        for s in body["days"][0]["slots"]
    ]
    assert "09:00" in times


async def test_any_available_staff_needs_no_staff_id(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    service = await make_service(client, [me])

    any_available = await public_slots(client, service)
    named = await public_slots(client, service, staff_id=me)

    assert "09:00" in any_available
    assert any_available == named


async def test_a_service_not_opted_into_online_booking_is_a_404(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    service = await make_service(client, [me], bookable_online=False)

    resp = await client.get(
        PUBLIC_AVAILABILITY,
        params={"service_id": service, "from": MONDAY.isoformat(), "to": MONDAY.isoformat()},
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "No such service."


async def test_an_unknown_service_is_the_same_404(client):
    await as_admin(client)

    resp = await client.get(
        PUBLIC_AVAILABILITY,
        params={
            "service_id": "00000000-0000-0000-0000-000000000000",
            "from": MONDAY.isoformat(),
            "to": MONDAY.isoformat(),
        },
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "No such service."


async def test_the_public_endpoint_never_offers_a_slot_only_a_staff_override_reaches(client):
    """The acceptance-critical case (m3.md, Phase 6 Task 1): 11:30 is past the 09:00-12:00
    shift end. `tests/test_overrides.py` already establishes that the staff-side diagnosis —
    which runs the engine *relaxed* — offers this start (`override_available`, naming
    `outside_shift`), and that staff can actually book it with `override: true`. The public
    endpoint must never offer 11:30, before or after that override exists: proof that
    `relax_advisory=False` holds for a start the staff-side engine, relaxed, says yes to —
    not merely that nothing here happens to ask it to relax."""
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])  # Monday 09:00-12:00
    service = await make_service(client, [me])

    # The premise: relaxed, this start is reachable — only by a human confirming it.
    diagnosed = await book(client, service, me, at("11:30"))
    assert diagnosed.status_code == 422, diagnosed.text
    assert diagnosed.json()["code"] == "override_available"
    assert diagnosed.json()["rules"] == ["outside_shift"]

    # Before any override exists, the public endpoint already refuses it.
    assert "11:30" not in await public_slots(client, service, staff_id=me)

    # Staff actually override-books it.
    overridden = await book(
        client, service, me, at("11:30"), override=True, override_reason="Client asked"
    )
    assert overridden.status_code == 201, overridden.text

    # The override changed nothing about what the public route offers.
    assert "11:30" not in await public_slots(client, service, staff_id=me)

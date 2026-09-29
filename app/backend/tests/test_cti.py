"""S1: `/api/cti` — phone lookup, the demo-mode read, and the simulated call (Phase 14, #16).

What this file pins down: partial and formatted-number input both resolve the same customer
(`customers/phone.py`'s normalisation, exercised through a real Postgres and its functional
index); several candidates for a shared prefix behave like search (no access-log row); one
resolved match behaves like a profile open (exactly one row, `customer_phone_lookup`); an
erased client is excluded, the same as the ordinary customer search; the capability is
`customers.view`, usable in Staff Mode with no Admin Mode switch; and the whole simulate
surface 404s while `businesses.demo_mode` is off, and returns a real shape once it is on.
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import text

from core.security import hash_password
from tests.conftest import add_account, get_owner_engine
from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    APPOINTMENTS,
    CUSTOMER,
    CUSTOMERS,
    OTHER_PASSWORD,
    add_colleague,
    as_admin,
    as_staff,
    at,
    book,
    claimed_instance,
    make_service,
    me_staff_id,
    put_hours,
)

LOOKUP = "/api/cti/lookup"
DEMO_MODE = "/api/cti/demo-mode"
SIMULATE = "/api/cti/simulate-call"
NOTIFICATIONS = "/api/admin/business/notifications"

# `CUSTOMER["phone"]` is `"416-555-0199"`, stored digits-only as `"4165550199"`.
DIGITS = "4165550199"


@pytest.fixture(autouse=True)
async def clean_access_log(claimed_instance):  # noqa: F811 — the imported fixture, by name
    """`claimed_instance` (`tests/test_appointments.py`) never touches `audit_access_log` —
    nothing under it needed to until this file, the first here to assert on it — so this
    clears it the same way `tests/test_access_log.py::clean_access_log` does."""
    async with get_owner_engine().begin() as owner:
        await owner.execute(text("DELETE FROM audit_access_log"))
    yield


async def access_rows(customer_id: str | None = None) -> list[dict]:
    async with get_owner_engine().begin() as owner:
        query = "SELECT customer_id, resource_type, action FROM audit_access_log"
        params: dict = {}
        if customer_id is not None:
            query += " WHERE customer_id = :id"
            params = {"id": customer_id}
        rows = (await owner.execute(text(query), params)).all()
    return [dict(r._mapping) for r in rows]


async def make_customer(client, **overrides) -> str:
    body = {**CUSTOMER, **overrides}
    resp = await client.post(CUSTOMERS, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def add_role(client, name: str, capabilities: list[str], email: str) -> None:
    made = await client.post(
        "/api/admin/roles",
        json={"name": name, "description": "Test role.", "capabilities": capabilities},
    )
    assert made.status_code == 201, made.text
    await add_account(email, await hash_password(OTHER_PASSWORD), role=made.json()["id"])


async def enable_demo_mode(client) -> None:
    await as_admin(client)
    resp = await client.patch(NOTIFICATIONS, json={"demo_mode": True})
    assert resp.status_code == 200, resp.text


# --- phone lookup: matching -------------------------------------------------------------------


async def test_too_few_digits_is_refused(client):
    await as_admin(client)

    resp = await client.get(LOOKUP, params={"phone": "416"})

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["query", "phone"]


async def test_an_unmatched_number_reports_no_match_and_logs_nothing(client):
    await as_admin(client)

    resp = await client.get(LOOKUP, params={"phone": "6045550100"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"status": "no_match", "candidates": [], "match": None}
    assert await access_rows() == []


async def test_a_formatted_number_resolves_the_same_customer_as_the_bare_digits(client):
    await as_admin(client)
    customer_id = await make_customer(client)

    for phone in ("4165550199", "(416) 555-0199", "+1 416-555-0199", "1-416-555-0199"):
        resp = await client.get(LOOKUP, params={"phone": phone})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "match", (phone, body)
        assert body["match"]["customer"]["id"] == customer_id


async def test_a_partial_number_resolves_the_customer_it_uniquely_prefixes(client):
    await as_admin(client)
    customer_id = await make_customer(client)

    resp = await client.get(LOOKUP, params={"phone": "4165550"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "match"
    assert body["match"]["customer"]["id"] == customer_id
    assert body["match"]["customer"]["classification"] == "new"
    assert body["match"]["previous_providers"] == []


async def test_several_candidates_sharing_a_prefix_are_returned_without_being_logged(client):
    await as_admin(client)
    a = await make_customer(client, first_name="Priya", phone="416-555-0100")
    b = await make_customer(client, first_name="Femi", phone="416-555-0101")

    resp = await client.get(LOOKUP, params={"phone": "41655501"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "candidates"
    assert body["match"] is None
    assert {c["id"] for c in body["candidates"]} == {a, b}
    assert await access_rows() == []


async def test_a_single_match_writes_exactly_one_access_log_row(client):
    await as_admin(client)
    customer_id = await make_customer(client)

    resp = await client.get(LOOKUP, params={"phone": DIGITS})

    assert resp.status_code == 200, resp.text
    rows = await access_rows(customer_id)
    assert len(rows) == 1
    assert rows[0]["resource_type"] == "customer_phone_lookup"
    assert rows[0]["action"] == "view"

    # A second lookup is a second read of the same PHI — a second row, not deduplicated.
    await client.get(LOOKUP, params={"phone": DIGITS})
    assert len(await access_rows(customer_id)) == 2


async def test_an_erased_customer_is_excluded_from_lookup(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    erased = await client.post(f"{CUSTOMERS}/{customer_id}/erasure", json={})
    assert erased.status_code == 201, erased.text

    resp = await client.get(LOOKUP, params={"phone": DIGITS})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "no_match"


async def test_previous_providers_lists_only_staff_from_completed_visits(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 1260)])
    service = await make_service(client, [me])
    customer_id = await make_customer(client)

    confirmed = await book(client, service, me, at("9:00"), customer_id=customer_id)
    assert confirmed.status_code == 201, confirmed.text
    completed = await book(client, service, me, at("11:00"), customer_id=customer_id)
    assert completed.status_code == 201, completed.text
    done = await client.post(f"{APPOINTMENTS}/{completed.json()['id']}/complete", json={})
    assert done.status_code == 200, done.text

    resp = await client.get(LOOKUP, params={"phone": DIGITS})

    assert resp.status_code == 200, resp.text
    providers = resp.json()["match"]["previous_providers"]
    # The confirmed-but-not-yet-completed visit does not make this list — only the completed
    # one does, once each, however many visits that staff member has delivered.
    assert len(providers) == 1
    assert providers[0]["staff_id"] == me
    assert providers[0]["service_name"] == "Swedish Massage"


# --- capability: `customers.view`, Staff Mode, no Admin Mode required -------------------------


async def test_a_staff_mode_session_with_customers_view_can_look_up_a_number(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    await add_colleague(client, "desk@cedar.example", OTHER_PASSWORD)  # default role: Staff
    client.cookies.clear()

    await as_staff(client, "desk@cedar.example", OTHER_PASSWORD)  # no /auth/mode switch at all
    resp = await client.get(LOOKUP, params={"phone": DIGITS})

    assert resp.status_code == 200, resp.text
    assert resp.json()["match"]["customer"]["id"] == customer_id


async def test_a_role_without_customers_view_is_refused(client):
    await as_admin(client)
    await add_role(client, "Reception-only", ["schedule.view"], "narrow@cedar.example")
    client.cookies.clear()

    await as_staff(client, "narrow@cedar.example", OTHER_PASSWORD)
    resp = await client.get(LOOKUP, params={"phone": DIGITS})

    assert resp.status_code == 403, resp.text


# --- demo mode: the toggle's read side, and the whole-surface gate ----------------------------


async def test_demo_mode_is_off_by_default_and_simulate_call_404s(client):
    await as_admin(client)

    read = await client.get(DEMO_MODE)
    assert read.status_code == 200, read.text
    assert read.json() == {"enabled": False}

    simulate = await client.post(SIMULATE, json={})
    assert simulate.status_code == 404, simulate.text


async def test_turning_demo_mode_on_makes_the_read_and_the_simulate_endpoint_answer(client):
    await enable_demo_mode(client)

    read = await client.get(DEMO_MODE)
    assert read.json() == {"enabled": True}

    simulate = await client.post(SIMULATE, json={})
    assert simulate.status_code == 200, simulate.text
    body = simulate.json()
    assert uuid.UUID(body["call_id"])
    assert body["phone"].isdigit()
    assert datetime.fromisoformat(body["received_at"])


async def test_demo_mode_is_readable_in_staff_mode_without_admin(client):
    """The write is Admin-Mode-only (`test_notification_settings.py`); the read that decides
    whether to render the simulate control is not — a front-desk account running the demo for
    a walk-in prospect needs it too."""
    await enable_demo_mode(client)
    await add_colleague(client, "desk@cedar.example", OTHER_PASSWORD)
    client.cookies.clear()

    await as_staff(client, "desk@cedar.example", OTHER_PASSWORD)
    resp = await client.get(DEMO_MODE)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enabled": True}


async def test_simulate_call_usually_returns_a_real_customers_number(client, monkeypatch):
    import scheduling.cti as cti

    await enable_demo_mode(client)
    customer_id = await make_customer(client)
    monkeypatch.setattr(cti.random, "random", lambda: 0.0)  # forces the "real" branch

    resp = await client.post(SIMULATE, json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["phone"] == DIGITS

    # And the returned number resolves through the same lookup, so a screen-pop panel wired
    # to this can look the caller straight up.
    looked_up = await client.get(LOOKUP, params={"phone": resp.json()["phone"]})
    assert looked_up.json()["match"]["customer"]["id"] == customer_id


async def test_simulate_call_sometimes_returns_an_unmatched_number(client, monkeypatch):
    import scheduling.cti as cti

    await enable_demo_mode(client)
    await make_customer(client)
    monkeypatch.setattr(cti.random, "random", lambda: 0.99)  # forces the "unmatched" branch
    monkeypatch.setattr(cti.random, "randint", lambda a, b: 9_999_999_999)

    resp = await client.post(SIMULATE, json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["phone"] == "9999999999"
    looked_up = await client.get(LOOKUP, params={"phone": "9999999999"})
    assert looked_up.json()["status"] == "no_match"


async def test_simulate_call_with_no_customers_at_all_never_errors(client):
    await enable_demo_mode(client)

    resp = await client.post(SIMULATE, json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["phone"].isdigit()


async def test_nothing_is_persisted_by_a_simulated_call(client):
    await enable_demo_mode(client)
    await make_customer(client)

    await client.post(SIMULATE, json={})
    await client.post(SIMULATE, json={})

    assert await access_rows() == []

"""S1/S2: essential forms and compliance (Task 8, #51).

What these pin down:

- an essential (mandatory) template mapped to a service: booking that service without a
  submission is `missing`; submitting v1 makes it `ok`; publishing v2 *without* re-signature
  leaves it `ok` (compliance is about the template's identity, not one of its versions);
  publishing v3 *with* re-signature makes it `resign_required`; a `valid_for_months` window
  that has lapsed makes it `expired`;
- applicability: no upcoming appointment for the mapped service means the template does not
  apply unless `applies_to_all`; a retired template never counts; a cancelled appointment
  does not make a template apply; a suppressed client never appears on the dashboard;
- the dashboard lists each non-compliant client once, with their next appointment, in a fixed
  number of queries whatever the client count, refuses `days` over 60, and writes no
  access-log row;
- the 12-month window is computed in the business timezone, across a DST boundary.
"""

import uuid
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from core.db import session_scope
from forms.compliance import add_months, start_of_local_day, status_for, valid_until
from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    at,
    book,
    claimed_instance,
    make_customer,
    make_service,
    me_staff_id,
    put_hours,
)
from tests.test_form_links import BASE, as_owner, forms_wiped, issue, token_of  # noqa: F401
from tests.test_form_submissions import SIGNED, pinned_version, submit
from tests.test_forms import as_admin, audit, make, publish, save

COMPLIANCE = "/api/customers/{}/compliance"
DASHBOARD = "/api/forms/compliance"
TORONTO = "America/Toronto"

_SIG_KEY = "44444444-4444-4444-8444-444444444444"


def _schema(label: str = "Signature") -> dict:
    return {"fields": [{"key": _SIG_KEY, "type": "signature", "label": label, "required": True}]}


# --- S2: the date arithmetic -------------------------------------------------------------------


def test_add_months_clamps_to_the_shorter_month():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)  # 2026 is not a leap year
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)  # 2024 is
    assert add_months(date(2025, 10, 15), 3) == date(2026, 1, 15)


def test_valid_until_is_computed_in_the_business_timezone_across_a_dst_boundary():
    # Toronto falls back from EDT (-4) to EST (-5) on 1 Nov 2026. A submission on 1 Sep 2026
    # local, valid for 3 months, lapses at local midnight 1 Dec 2026 — the DST change in
    # between must not shift that boundary by an hour.
    submitted = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)  # 11:00 EDT
    until = valid_until(submitted, 3, TORONTO)
    assert until.astimezone(ZoneInfo(TORONTO)) == datetime(
        2026, 12, 1, 0, 0, tzinfo=ZoneInfo(TORONTO)
    )
    assert until.tzinfo is UTC


def test_valid_until_refuses_a_naive_instant():
    with pytest.raises(ValueError):
        valid_until(datetime(2026, 9, 1), 3, TORONTO)


def test_start_of_local_day_is_local_midnight_not_now():
    # 02:30 UTC on 22 Sep is 22:30 the *previous* local evening in Toronto (UTC-4 in Sep) —
    # "today" by the wall clock a receptionist reads, not by the UTC calendar date.
    now = datetime(2026, 9, 22, 2, 30, tzinfo=UTC)
    start = start_of_local_day(now, TORONTO)
    assert start.astimezone(ZoneInfo(TORONTO)) == datetime(
        2026, 9, 21, 0, 0, tzinfo=ZoneInfo(TORONTO)
    )


def test_start_of_local_day_refuses_a_naive_instant():
    with pytest.raises(ValueError):
        start_of_local_day(datetime(2026, 9, 22), TORONTO)


def test_status_for_missing_when_there_is_no_submission():
    now = datetime(2026, 9, 22, tzinfo=UTC)
    assert (
        status_for(
            now=now, zone=TORONTO, valid_for_months=None, has_submission=False, qualifying=None
        )
        == "missing"
    )


def test_status_for_ok_when_a_qualifying_submission_exists():
    now = datetime(2026, 9, 22, tzinfo=UTC)
    assert (
        status_for(
            now=now,
            zone=TORONTO,
            valid_for_months=None,
            has_submission=True,
            qualifying=(1, now),
        )
        is None
    )


def test_status_for_resign_required_when_nothing_on_file_qualifies():
    now = datetime(2026, 9, 22, tzinfo=UTC)
    # Submissions exist (has_submission=True) but none clears the version bar — the caller
    # (`_latest_qualifying_submissions`) already filtered those out, so `qualifying` is None.
    assert (
        status_for(
            now=now, zone=TORONTO, valid_for_months=None, has_submission=True, qualifying=None
        )
        == "resign_required"
    )


def test_status_for_expired_after_the_window_lapses():
    now = datetime(2026, 9, 22, tzinfo=UTC)
    old = datetime(2025, 8, 1, tzinfo=UTC)  # well over 12 months before `now`
    recent = datetime(2026, 9, 1, tzinfo=UTC)
    assert (
        status_for(
            now=now, zone=TORONTO, valid_for_months=12, has_submission=True, qualifying=(1, old)
        )
        == "expired"
    )
    assert (
        status_for(
            now=now,
            zone=TORONTO,
            valid_for_months=12,
            has_submission=True,
            qualifying=(1, recent),
        )
        is None
    )


# --- S1: the lifecycle ---------------------------------------------------------------------------


async def _essential_template(client, service_id: str | None = None, **settings) -> dict:
    """A published v1, mandatory, mapped as `settings`/`service_id` says. `applies_to_all`
    unless a `service_id` is given."""
    template = await make(client)
    await save(client, template, is_mandatory=True, schema=_schema())
    resp = await publish(client, template["id"])
    assert resp.status_code == 201, resp.text
    body = {"applies_to_all": service_id is None, "service_ids": [service_id] if service_id else []}
    body.update(settings)
    settled = await client.put(f"/api/admin/forms/{template['id']}/settings", json=body)
    assert settled.status_code == 200, settled.text
    return template


async def _sign_v1(client, customer_id: str, template_id: str) -> None:
    token = token_of((await issue(client, customer_id, template_id)).json()["url"])
    version_id = await pinned_version(token)
    resp = await submit(client, token, version_id, {_SIG_KEY: SIGNED})
    assert resp.json()["status"] == "received", resp.text


async def statuses(client, customer_id: str) -> dict[str, str]:
    resp = await client.get(COMPLIANCE.format(customer_id))
    assert resp.status_code == 200, resp.text
    return {t["template_id"]: t["status"] for t in resp.json()["templates"]}


async def test_the_essential_forms_lifecycle(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 1080)])
    service_id = await make_service(client, [me])
    template = await _essential_template(client, service_id)
    customer_id = await make_customer(client)
    booked = await book(client, service_id, me, at("10:00"), customer_id=customer_id)
    assert booked.status_code == 201, booked.text

    assert await statuses(client, customer_id) == {template["id"]: "missing"}

    await _sign_v1(client, customer_id, template["id"])
    assert await statuses(client, customer_id) == {}

    # v2, unchanged flags but a reworded label — no re-signature: still ok (identity, not
    # version).
    await save(client, template, is_mandatory=True, schema=_schema("Sign here please"))
    v2 = await publish(client, template["id"])
    assert v2.status_code == 201, v2.text
    assert await statuses(client, customer_id) == {}

    # v3 does require re-signature.
    await save(client, template, is_mandatory=True, schema=_schema("Sign here — v3"))
    v3 = await publish(client, template["id"], requires_resignature=True)
    assert v3.status_code == 201, v3.text
    assert await statuses(client, customer_id) == {template["id"]: "resign_required"}


async def test_a_lapsed_validity_window_is_expired(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 1080)])
    service_id = await make_service(client, [me])
    template = await _essential_template(client, service_id, valid_for_months=12)
    customer_id = await make_customer(client)
    booked = await book(client, service_id, me, at("10:00"), customer_id=customer_id)
    assert booked.status_code == 201, booked.text
    await _sign_v1(client, customer_id, template["id"])
    assert await statuses(client, customer_id) == {}

    # Back-date the submission 13 months, as only the schema owner can (the app role's
    # UPDATE is revoked and trigger-guarded).
    await as_owner(
        "UPDATE form_submissions SET submitted_at = now() - interval '13 months' "
        "WHERE customer_id = :c",
        c=customer_id,
    )
    assert await statuses(client, customer_id) == {template["id"]: "expired"}


async def _submission_id(customer_id: str, template_id: str, *, number: int) -> str:
    async with session_scope() as db:
        row = await db.execute(
            text(
                "SELECT s.id FROM form_submissions s "
                "JOIN form_template_versions v ON v.id = s.version_id "
                "WHERE s.customer_id = :c AND s.template_id = :t AND v.number = :n"
            ),
            {"c": customer_id, "t": template_id, "n": number},
        )
        return str(row.scalar_one())


async def test_an_older_submission_filed_later_does_not_undo_a_qualifying_newer_one(client):
    """Fix round 1's probe. A client can end up with a qualifying v2 signature on file *and*
    a v1 submission whose row is more recent than the v2 one — (b) below closes the public
    path (publishing with re-signature revokes the older open link), but the read model has
    to be correct regardless of how that table state came to exist, so this drives it
    directly rather than depending on (b) staying the only way in."""
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 1080)])
    service_id = await make_service(client, [me])
    template = await _essential_template(client, service_id)
    customer_id = await make_customer(client)
    booked = await book(client, service_id, me, at("10:00"), customer_id=customer_id)
    assert booked.status_code == 201, booked.text

    await _sign_v1(client, customer_id, template["id"])
    v1_submission_id = await _submission_id(customer_id, template["id"], number=1)

    await save(client, template, is_mandatory=True, schema=_schema("v2"))
    v2 = await publish(client, template["id"], requires_resignature=True)
    assert v2.status_code == 201, v2.text
    assert await statuses(client, customer_id) == {template["id"]: "resign_required"}

    token2 = token_of((await issue(client, customer_id, template["id"])).json()["url"])
    version2_id = await pinned_version(token2)
    resp2 = await submit(client, token2, version2_id, {_SIG_KEY: SIGNED})
    assert resp2.json()["status"] == "received", resp2.text
    assert await statuses(client, customer_id) == {}  # ok: the v2 signature qualifies

    # The exact probe shape: the *older* (v1) submission's row becomes more recent than the
    # qualifying v2 one — the old "most recent submission" rule would have regressed this
    # client back to resign_required.
    await as_owner(
        "UPDATE form_submissions SET submitted_at = now() + interval '1 hour' WHERE id = :id",
        id=v1_submission_id,
    )
    assert await statuses(client, customer_id) == {}


async def test_publishing_with_requires_resignature_revokes_older_open_links(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 1080)])
    service_id = await make_service(client, [me])
    template = await _essential_template(client, service_id)
    customer_id = await make_customer(client)

    issued = await issue(client, customer_id, template["id"])
    assert issued.status_code == 201, issued.text
    token = token_of(issued.json()["url"])

    await save(client, template, is_mandatory=True, schema=_schema("v2"))
    v2 = await publish(client, template["id"], requires_resignature=True)
    assert v2.status_code == 201, v2.text

    # The old, never-used v1 link is dead now — the same uniform 404 every dead link answers.
    lookup = await client.post(
        "/api/public/forms/lookup", json={"token": token}, headers={"Origin": BASE}
    )
    assert lookup.status_code == 404, lookup.text
    assert lookup.json()["code"] == "link_invalid"

    published = [row for row in await audit() if row[0] == "form_template.published"]
    assert published[-1] == (
        "form_template.published",
        template["id"],
        {"number": 2, "links_revoked": 1},
    )


async def test_an_appointment_already_in_progress_still_applies(client):
    """`_applicable_services` reads "from today onward" as the business's local midnight, not
    `now()` (fix round 1) — a client already in the chair for an appointment that started a
    few minutes ago must still show up, not quietly drop off because the instant has passed."""
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 1080)])
    service_id = await make_service(client, [me])
    template = await _essential_template(client, service_id)
    customer_id = await make_customer(client)
    booked = await book(client, service_id, me, at("10:00"), customer_id=customer_id)
    assert booked.status_code == 201, booked.text
    await as_owner(
        "UPDATE appointments SET starts_at = now() - interval '5 minutes', "
        "ends_at = now() + interval '55 minutes' WHERE id = :id",
        id=booked.json()["id"],
    )
    assert await statuses(client, customer_id) == {template["id"]: "missing"}


async def test_compliance_404s_for_an_unknown_or_suppressed_customer(client):
    await as_admin(client)
    unknown = await client.get(COMPLIANCE.format(str(uuid.uuid4())))
    assert unknown.status_code == 404, unknown.text

    customer_id = await make_customer(client)
    await as_owner("UPDATE customers SET suppressed_at = now() WHERE id = :c", c=customer_id)
    suppressed = await client.get(COMPLIANCE.format(customer_id))
    assert suppressed.status_code == 404, suppressed.text


async def test_applicability_service_mapping_retirement_and_cancellation(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 1080)])
    mapped_service = await make_service(client, [me], name="Mapped service")
    other_service = await make_service(client, [me], name="Other service")
    template = await _essential_template(client, mapped_service)

    # No upcoming appointment at all: does not appear.
    no_appt = await make_customer(client)
    assert await statuses(client, no_appt) == {}

    # An appointment for a *different* (unmapped) service: still does not apply.
    other_customer = await make_customer(client)
    booked_other = await book(client, other_service, me, at("10:00"), customer_id=other_customer)
    assert booked_other.status_code == 201, booked_other.text
    assert await statuses(client, other_customer) == {}

    # A cancelled appointment for the mapped service: does not make it apply.
    cancelled_customer = await make_customer(client)
    booked_cancelled = await book(
        client, mapped_service, me, at("11:00"), customer_id=cancelled_customer
    )
    assert booked_cancelled.status_code == 201, booked_cancelled.text
    cancel = await client.post(
        f"/api/appointments/{booked_cancelled.json()['id']}/cancel", json={"reason": "test"}
    )
    assert cancel.status_code == 200, cancel.text
    assert await statuses(client, cancelled_customer) == {}

    # A confirmed appointment for the mapped service: applies, and is missing.
    real_customer = await make_customer(client)
    booked_real = await book(client, mapped_service, me, at("12:00"), customer_id=real_customer)
    assert booked_real.status_code == 201, booked_real.text
    assert await statuses(client, real_customer) == {template["id"]: "missing"}

    # Retiring the template: it never counts again, appointment or not.
    retire = await client.post(f"/api/admin/forms/{template['id']}/retire", json={})
    assert retire.status_code == 200, retire.text
    assert await statuses(client, real_customer) == {}


async def test_applies_to_all_needs_no_appointment_but_the_dashboard_still_needs_one(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 1080)])
    template = await _essential_template(client)  # applies_to_all=True, no service mapping
    customer_id = await make_customer(client)

    # No appointment at all: the per-client endpoint still flags it (applies_to_all needs no
    # appointment to *apply*), but the dashboard — anchored to "coming in soon" — omits them.
    assert await statuses(client, customer_id) == {template["id"]: "missing"}
    dash = await client.get(DASHBOARD)
    assert dash.status_code == 200, dash.text
    assert customer_id not in {c["customer_id"] for c in dash.json()["clients"]}

    service_id = await make_service(client, [me], name="Any service")
    booked = await book(client, service_id, me, at("13:00"), customer_id=customer_id)
    assert booked.status_code == 201, booked.text
    dash2 = await client.get(DASHBOARD)
    rows = {c["customer_id"]: c for c in dash2.json()["clients"]}
    assert customer_id in rows
    assert rows[customer_id]["templates"] == [
        {"template_id": template["id"], "name": template["name"], "status": "missing"}
    ]


async def test_a_suppressed_client_never_appears_on_the_dashboard(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 1080)])
    service_id = await make_service(client, [me])
    await _essential_template(client, service_id)
    customer_id = await make_customer(client)
    booked = await book(client, service_id, me, at("10:00"), customer_id=customer_id)
    assert booked.status_code == 201, booked.text
    # Set directly rather than through `POST …/erasure`: that route also refuses a client with
    # an upcoming appointment, a separate rule this test has no reason to route around.
    await as_owner("UPDATE customers SET suppressed_at = now() WHERE id = :c", c=customer_id)

    dash = await client.get(DASHBOARD)
    assert customer_id not in {c["customer_id"] for c in dash.json()["clients"]}


async def test_days_over_sixty_is_refused(client):
    await as_admin(client)
    resp = await client.get(DASHBOARD, params={"days": 61})
    assert resp.status_code == 422, resp.text


async def _access_log_row_count() -> int:
    async with session_scope() as db:
        return await db.scalar(text("SELECT count(*) FROM audit_access_log"))


class _StatementCounter:
    """Counts `cursor.execute` calls across every engine while it is open — the cheapest
    proof that a handler does not loop a query per row (no ORM machinery to fight)."""

    def __init__(self) -> None:
        self.count = 0

    def _hook(self, *args, **kwargs) -> None:
        self.count += 1

    def __enter__(self) -> "_StatementCounter":
        event.listen(Engine, "before_cursor_execute", self._hook)
        return self

    def __exit__(self, *exc) -> None:
        event.remove(Engine, "before_cursor_execute", self._hook)


async def test_the_dashboard_writes_no_access_log_row_and_does_not_scale_with_client_count(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 1320)])  # 09:00–22:00: room for five 1-hour slots
    service_id = await make_service(client, [me])
    await _essential_template(client, service_id)

    hour = iter(range(14, 20))

    async def one_non_compliant_client() -> None:
        customer_id = await make_customer(client)
        booked = await book(client, service_id, me, at(f"{next(hour)}:00"), customer_id=customer_id)
        assert booked.status_code == 201, booked.text

    await one_non_compliant_client()
    before = await _access_log_row_count()
    with _StatementCounter() as counted_1:
        resp = await client.get(DASHBOARD)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["clients"]) == 1
    assert await _access_log_row_count() == before  # no new row

    for _ in range(4):
        await one_non_compliant_client()
    with _StatementCounter() as counted_5:
        resp5 = await client.get(DASHBOARD)
    assert resp5.status_code == 200, resp5.text
    assert len(resp5.json()["clients"]) == 5
    # The statements issued to answer the statuses do not grow with the client count — the
    # bulk reads in `forms.compliance._compliance` are the same handful either way. A little
    # slack for the surrounding request's own bookkeeping (session, auth), none for a loop.
    assert counted_5.count <= counted_1.count + 2, (counted_1.count, counted_5.count)


async def test_changing_only_template_flags_publishes_a_new_frozen_version(client):
    await as_admin(client)
    template = await make(client)
    await save(client, template, is_mandatory=False, is_health_form=False, schema=_schema())
    assert (await publish(client, template["id"])).status_code == 201
    await save(client, template, is_mandatory=True, is_health_form=True, schema=_schema())
    result = await publish(client, template["id"])
    assert result.status_code == 201, result.text
    versions = (await client.get(f"/api/admin/forms/{template['id']}/versions")).json()["versions"]
    assert [(v["number"], v["is_mandatory"], v["is_health_form"]) for v in versions] == [
        (2, True, True),
        (1, False, False),
    ]

"""The PHI access log (ADR-0002), at three seams.

**S1.** Opening a profile writes exactly one row — actor, role name, customer, resource,
`view`, timestamp — and nothing else does: not the list, not the booking dialog's search,
not the calendar's reads. A refusal (401, 403) leaves no row, because `Requires` runs before
`LogAccess` and the row is only ever written for somebody who is allowed in.

**S5.** The table is append-only for the application role (grant *and* trigger), yearly
partitioned, and a row dated next July lands in next year's partition.

**Route enumeration.** ADR-0002 §2 chose an explicit per-route dependency over middleware and
accepted that it can be forgotten. These two rules are what close that gap: (a) every
customer-scoped route — any method, not only GET — whose `response_model` actually carries a
PHI field (`core.access_log.PHI_FIELDS`, walked recursively through nested models, lists and
`Optional`) carries `LogAccess` unless it is named in `NOT_PHI`; (b) the set of routes
carrying it equals `LOGGED`, so adding a PHI endpoint is a visible edit to this file; (c) a
customer-scoped route with no `response_model` at all — a `JSONResponse` or a bare dict, which
rule (a) cannot see into — fails unless `UNMODELLED` names it with a reason. All three are
proven against probe routes mounted for the test, so a green build is not a rule that never
fires. Rule (a) started GET-only and missed exactly the bug a fix round found: `PATCH
/customers/{id}` returned the full profile — DOB, both contacts, notes — with no
`LogAccess` and, on a no-change request, no `audit_events` row either. It is checked by
response shape now, not by HTTP verb.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

import pytest
from fastapi import Depends
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, _iter_routes_with_context
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.access_log import declares_a_model, response_phi_fields
from core.db import get_purge_engine, session_scope
from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    CUSTOMERS,
    EMAIL,
    OTHER_PASSWORD,
    PASSWORD,
    as_admin,
    as_staff,
    at,
    book,
    claimed_instance,
    make_customer,
    make_service,
    me_staff_id,
    put_hours,
)

TABLE = "audit_access_log"
TRIGGER = "audit_access_log_no_rewrite"

# The routes that return PHI today. A new one is a conscious edit here (rule b).
LOGGED = {("GET", "/api/customers/{customer_id}")}
# Customer-scoped GETs that deliberately do not log. Empty: nothing under a customer's path
# is metadata yet (the access report itself will live under `/api/admin/...`).
NOT_PHI: set[tuple[str, str]] = set()

# Customer-scoped routes with no declared response model, each with the reason it is safe.
# Rule (a) reads what a route discloses off its `response_model`; a handler returning a
# `JSONResponse` or a bare dict has none, so rule (a) would pass it whatever it sends. Rule (c)
# refuses that blind spot: declare a model, or name the route here and say why. Empty in #7.
UNMODELLED: dict[tuple[str, str], str] = {}

PROBE = "/api/customers/{customer_id}/probe"


@pytest.fixture(autouse=True)
async def clean_access_log(claimed_instance):  # noqa: F811 — the imported fixture, by name
    async with get_purge_engine().begin() as purge:
        await purge.execute(text(f"DELETE FROM {TABLE}"))
    yield


async def access_rows() -> list[dict]:
    async with session_scope() as db:
        rows = await db.execute(
            text(
                "SELECT actor_user_id, actor_role, customer_id, resource_type, resource_id, "
                f"action, ip, occurred_at FROM {TABLE} ORDER BY id"
            )
        )
        return [dict(r._mapping) for r in rows]


async def add_role(client, name: str, capabilities: list[str], email: str) -> None:
    """A role with exactly `capabilities`, and an account holding it, via the admin API."""
    from core.security import hash_password
    from tests.conftest import add_account

    made = await client.post(
        "/api/admin/roles",
        json={"name": name, "description": "Test role.", "capabilities": capabilities},
    )
    assert made.status_code == 201, made.text
    await add_account(email, await hash_password(OTHER_PASSWORD), role=made.json()["id"])


async def booked_customer(client) -> tuple[str, str]:
    """A customer with one confirmed Monday 10:00 appointment. Returns (customer_id, appt_id)."""
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    service = await make_service(client, [me])
    customer_id = await make_customer(client)
    made = await book(client, service, me, at("10:00"), customer_id=customer_id)
    assert made.status_code == 201, made.text
    return customer_id, made.json()["id"]


# --- S1: opening a profile is one row; nothing else is any --------------------------------


async def test_opening_a_profile_returns_it_with_its_visits_and_writes_exactly_one_row(client):
    customer_id, appointment_id = await booked_customer(client)
    me = await client.get("/api/auth/me")
    user_id, role = me.json()["id"], me.json()["role"]

    resp = await client.get(f"{CUSTOMERS}/{customer_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["customer"]["id"] == customer_id
    assert body["customer"]["first_name"] == "Priya"
    assert body["customer"]["phone"] == "4165550199"
    assert body["timezone"] == "America/Toronto"
    assert [a["id"] for a in body["appointments"]] == [appointment_id]
    visit = body["appointments"][0]
    assert visit["status"] == "confirmed"
    assert visit["service"]["name"] == "Swedish Massage"
    assert visit["staff"]["display_name"]
    assert visit["starts_at"] == at("10:00")

    rows = await access_rows()
    assert len(rows) == 1
    row = rows[0]
    assert str(row["actor_user_id"]) == user_id
    assert row["actor_role"] == role == "Administrator"
    assert str(row["customer_id"]) == customer_id
    assert row["resource_type"] == "customer_profile"
    assert row["resource_id"] == customer_id
    assert row["action"] == "view"
    assert row["occurred_at"] is not None
    assert row["occurred_at"].tzinfo is not None
    # httpx's ASGITransport supplies a loopback client address by default (core/access_log.py) —
    # not the None a bare socket-less transport would give.
    assert str(row["ip"]) == "127.0.0.1"


async def test_opening_a_profile_twice_is_two_rows(client):
    customer_id, _ = await booked_customer(client)

    for _ in range(2):
        assert (await client.get(f"{CUSTOMERS}/{customer_id}")).status_code == 200

    assert len(await access_rows()) == 2


async def test_an_unknown_customer_is_a_404_that_still_records_the_attempt(client):
    """Log-before-read: the row says somebody tried to open this id, which is what an
    investigation wants to know. Documented as intended in `core/access_log.py`."""
    await as_admin(client)
    missing = str(uuid.uuid4())

    resp = await client.get(f"{CUSTOMERS}/{missing}")

    assert resp.status_code == 404, resp.text
    rows = await access_rows()
    assert [str(r["customer_id"]) for r in rows] == [missing]


async def test_lists_searches_and_calendar_reads_write_no_rows(client):
    customer_id, _ = await booked_customer(client)
    day = date.fromisoformat(at("10:00")[:10])

    for path, params in (
        (CUSTOMERS, None),
        (CUSTOMERS, {"q": "nai"}),
        (CUSTOMERS, {"q": "(416) 555"}),
        ("/api/appointments", {"from": day.isoformat(), "to": day.isoformat()}),
        ("/api/schedule", {"from": day.isoformat(), "to": day.isoformat()}),
    ):
        resp = await client.get(path, params=params)
        assert resp.status_code == 200, (path, resp.text)

    assert await access_rows() == []
    # Not by accident of an empty list: the customer really was rendered by those reads.
    listed = await client.get(CUSTOMERS, params={"q": "nai"})
    assert [c["id"] for c in listed.json()["customers"]] == [customer_id]


async def test_a_role_without_customers_view_is_refused_and_nothing_is_logged(client):
    customer_id, _ = await booked_customer(client)
    await add_role(client, "Looker", ["schedule.view"], "desk@cedar.example")
    client.cookies.clear()
    await as_staff(client, "desk@cedar.example", OTHER_PASSWORD)

    resp = await client.get(f"{CUSTOMERS}/{customer_id}")

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "capability_required"
    assert await access_rows() == []


async def test_a_role_with_only_customers_view_may_open_a_profile_under_its_own_role_name(
    client,
):
    customer_id, _ = await booked_customer(client)
    await add_role(client, "Front desk", ["customers.view"], "desk@cedar.example")
    client.cookies.clear()
    await as_staff(client, "desk@cedar.example", OTHER_PASSWORD)

    resp = await client.get(f"{CUSTOMERS}/{customer_id}")

    assert resp.status_code == 200, resp.text
    rows = await access_rows()
    assert [r["actor_role"] for r in rows] == ["Front desk"]


async def test_anonymous_is_a_401_and_nothing_is_logged(client):
    customer_id, _ = await booked_customer(client)
    client.cookies.clear()

    resp = await client.get(f"{CUSTOMERS}/{customer_id}")

    assert resp.status_code == 401, resp.text
    assert await access_rows() == []


# --- route enumeration (ADR-0002 §2, pre-flight D2) ---------------------------------------


@dataclass(frozen=True)
class Mounted:
    """One API route as the app serves it: its full path, methods, tags, response model and
    the dependant with every router-level dependency folded in."""

    path: str
    methods: frozenset[str]
    tags: frozenset[str]
    dependant: Dependant
    response_model: type | None
    status_code: int | None = None

    @property
    def phi_resource(self) -> str | None:
        """The `phi_resource` of a `LogAccess` dependency, walking sub-dependants too."""
        stack = list(self.dependant.dependencies)
        while stack:
            dependant = stack.pop()
            resource = getattr(dependant.call, "phi_resource", None)
            if resource is not None:
                return resource
            stack.extend(dependant.dependencies)
        return None

    @property
    def customer_scoped(self) -> bool:
        return self.path.startswith("/api/customers/{customer_id}") or "phi" in self.tags

    @property
    def phi_fields(self) -> frozenset[str]:
        return response_phi_fields(self.response_model)


def mounted_routes(app) -> list[Mounted]:
    """Every API route, discovered from the app rather than kept by hand.

    FastAPI (0.141) stores an included router as one `_IncludedRouter` entry in `app.routes`
    and materialises the routes inside it lazily, with the prefix and the router-level
    dependencies applied, through `_iter_routes_with_context` — the same walk the OpenAPI
    generator uses. Leaning on it is what makes this list the served one. If a FastAPI
    upgrade changes the walk, rule (b) fails loudly: `LOGGED` is never empty.

    `route` is the real `APIRoute` in both branches (`context.original_route`, or `route`
    itself when there is no sub-router context), which is where `response_model` lives —
    rule (a) needs it to see what a route actually discloses, not just where it is mounted.
    """
    found = []
    for route, context in _iter_routes_with_context(app.routes):
        response_model = getattr(route, "response_model", None)
        status_code = getattr(route, "status_code", None)
        if context is not None and context.dependant is not None:
            found.append(
                Mounted(
                    context.path,
                    frozenset(context.methods),
                    frozenset(str(t) for t in context.tags),
                    context.dependant,
                    response_model,
                    status_code,
                )
            )
        elif isinstance(route, APIRoute):
            found.append(
                Mounted(
                    route.path,
                    frozenset(route.methods),
                    frozenset(str(t) for t in route.tags),
                    route.dependant,
                    response_model,
                    status_code,
                )
            )
    return found


def unlogged_phi_routes(app) -> set[tuple[str, str]]:
    """Rule (a): customer-scoped routes — any method — whose response actually carries a PHI
    field, with neither `LogAccess` nor a `NOT_PHI` entry. Not GET-only: a mutation that
    echoes PHI back (a `PATCH` returning the profile it just edited, say) is exactly as
    undetected an access as a `GET` would be, and the fix round that added this rule found
    real one (`PATCH /customers/{id}` used to do exactly that)."""
    return {
        (method, r.path)
        for r in mounted_routes(app)
        if r.customer_scoped and r.phi_fields and r.phi_resource is None
        for method in r.methods
        if (method, r.path) not in NOT_PHI
    }


def unmodelled_routes(app, allowed=UNMODELLED) -> set[tuple[str, str]]:
    """Rule (c): customer-scoped routes whose response model contains no Pydantic model
    anywhere — none at all, `dict`, `dict[str, str]`, `Any`, `list[dict]` — and no
    `UNMODELLED` entry. Rule (a) reads PHI off model fields, so a route rule (a) cannot judge
    is a failure here rather than a pass there. A `204` route has no body to disclose and
    no model to declare, so it is not a gap.

    What no static check sees: a handler that declares a model but returns a `JSONResponse`
    itself bypasses that model, and sends whatever it built. Code review is the control
    for that one."""
    return {
        (method, r.path)
        for r in mounted_routes(app)
        if r.customer_scoped and not declares_a_model(r.response_model) and r.status_code != 204
        for method in r.methods
        if (method, r.path) not in allowed
    }


def logged_routes(app) -> set[tuple[str, str]]:
    """Rule (b): every route carrying `LogAccess`, whatever its method or path."""
    return {
        (method, r.path)
        for r in mounted_routes(app)
        for method in r.methods
        if r.phi_resource is not None
    }


class _ProbeOut(BaseModel):
    """A response model with one PHI field, for rule (a)'s probes — the smallest shape a
    route could accidentally disclose a customer's chart through."""

    ok: bool
    notes: str | None = None


class _NoPhiOut(BaseModel):
    ok: bool


@pytest.fixture
def probe():
    """Mount one probe at `PROBE` for the test, and take it down again afterwards so the
    real-app assertions in this file never see it."""
    from main import app

    def mount(
        dependencies: list,
        *,
        methods: list[str] | None = None,
        response_model: type | None = None,
    ) -> None:
        app.add_api_route(
            PROBE,
            lambda customer_id: {"ok": True},
            dependencies=dependencies,
            methods=methods or ["GET"],
            response_model=response_model,
        )

    yield mount
    app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", None) != PROBE]


def test_every_phi_route_in_the_real_app_is_logged_and_named():
    from main import app

    routes = mounted_routes(app)
    # The walk really reached inside the included routers, not just the top level.
    assert len(routes) > 50, [r.path for r in routes]
    assert unlogged_phi_routes(app) == set()
    assert unmodelled_routes(app) == set()
    assert logged_routes(app) == LOGGED
    # The allowlists may only name routes that exist, or they are stale exemptions.
    served = {(method, r.path) for r in routes for method in r.methods}
    assert NOT_PHI <= served
    assert set(UNMODELLED) <= served
    assert all(reason.strip() for reason in UNMODELLED.values())
    # The access report names a customer but is administration, not the chart: it is not
    # customer-scoped, carries no PHI field, and does not log (pre-flight §8, Task 8).
    report = ("GET", "/api/admin/customers/{customer_id}/access-log")
    assert report in served
    assert report not in logged_routes(app)


def test_a_customer_scoped_get_returning_phi_without_log_access_fails_rule_a(probe):
    from auth.capabilities import Requires
    from main import app

    probe([Depends(Requires("customers.view"))], response_model=_ProbeOut)

    assert ("GET", PROBE) in unlogged_phi_routes(app)


def test_a_customer_scoped_get_returning_no_phi_field_never_needed_log_access(probe):
    """Rule (a) checks what the response actually carries, not just the path — a
    customer-scoped route that discloses nothing PHI-shaped (like `CustomerOut`) is not a
    gap, and must not have to declare `LogAccess` it does not need."""
    from auth.capabilities import Requires
    from main import app

    probe([Depends(Requires("customers.view"))], response_model=_NoPhiOut)

    assert ("GET", PROBE) not in unlogged_phi_routes(app)
    assert ("GET", PROBE) not in unmodelled_routes(app)


def test_a_customer_scoped_route_with_no_response_model_fails_rule_c(probe):
    """The blind spot rule (c) closes: a handler returning a `JSONResponse` or a dict has no
    `response_model`, so rule (a) has nothing to read PHI fields off and passes it — whatever
    it actually sends."""
    from auth.capabilities import Requires
    from main import app

    probe([Depends(Requires("customers.view"))])  # response_model=None

    assert ("GET", PROBE) not in unlogged_phi_routes(app)  # rule (a) is blind to it...
    assert ("GET", PROBE) in unmodelled_routes(app)  # ...rule (c) is not


def test_an_unmodelled_route_named_with_a_reason_passes_rule_c(probe):
    from auth.capabilities import Requires
    from main import app

    probe([Depends(Requires("customers.view"))])

    allowed = {("GET", PROBE): "A probe: returns only {'ok': true}."}
    assert ("GET", PROBE) not in unmodelled_routes(app, allowed)


def _local_app(endpoint, **route):
    """A throwaway app with one customer-scoped route — never the real one, never `src/`."""
    from fastapi import FastAPI

    from auth.capabilities import Requires

    local = FastAPI()
    local.add_api_route(
        PROBE, endpoint, dependencies=[Depends(Requires("customers.view"))], **route
    )
    return local


def test_a_dict_annotated_handler_under_a_customer_is_unmodelled_rule_c():
    """`-> dict[str, str]` infers a response model, but one with no fields to read: rule (a)
    sees nothing, so rule (c) must refuse it. The annotation is real in this codebase
    (`scheduling/staff.py`)."""

    async def notes(customer_id: uuid.UUID) -> dict[str, str]:
        return {"notes": "PHI behind a dict"}

    local = _local_app(notes)

    assert ("GET", PROBE) in unmodelled_routes(local)


def test_a_list_of_phi_models_without_log_access_fails_rule_a():
    """The Phase 8/9 shape: `GET /customers/{id}/notes -> list[NoteOut]`. The walk has to go
    through the top-level `list`, not only into fields of a top-level model."""
    local = _local_app(lambda customer_id: [], response_model=list[_ProbeOut])

    assert ("GET", PROBE) in unlogged_phi_routes(local)
    assert ("GET", PROBE) not in unmodelled_routes(local)


def test_a_customer_scoped_patch_returning_phi_without_log_access_fails_rule_a(probe):
    """The bug this rule closed: a mutation whose response echoes PHI back is exactly as
    untraced an access as a `GET` would be — rule (a) used to only ever look at GET."""
    from auth.capabilities import Requires
    from main import app

    probe([Depends(Requires("customers.manage"))], methods=["PATCH"], response_model=_ProbeOut)

    assert ("PATCH", PROBE) in unlogged_phi_routes(app)


def test_a_logged_route_missing_from_the_expected_set_fails_rule_b(probe):
    from auth.capabilities import Requires
    from core.access_log import LogAccess
    from main import app

    probe(
        [Depends(Requires("customers.view")), Depends(LogAccess("probe"))],
        response_model=_ProbeOut,
    )

    assert ("GET", PROBE) not in unlogged_phi_routes(app)
    assert logged_routes(app) != LOGGED
    assert logged_routes(app) - LOGGED == {("GET", PROBE)}


# --- S5: append-only, partitioned -----------------------------------------------------------


async def _partitions() -> list[str]:
    async with session_scope() as db:
        return sorted(
            await db.scalars(
                text(
                    "SELECT inhrelid::regclass::text FROM pg_inherits "
                    f"WHERE inhparent = '{TABLE}'::regclass"
                )
            )
        )


async def test_partitions_exist_for_this_year_and_next(database):
    year = date.today().year
    assert await _partitions() == [f"{TABLE}_{year}", f"{TABLE}_{year + 1}"]


async def test_a_row_dated_next_july_lands_in_next_years_partition(database):
    year = date.today().year + 1
    async with session_scope() as db:
        row_id = await db.scalar(
            text(
                f"INSERT INTO {TABLE} (occurred_at, actor_user_id, actor_role, customer_id, "
                "resource_type, resource_id, action) VALUES (:at, :u, 'Staff', :c, "
                "'customer_profile', :r, 'view') RETURNING id"
            ),
            {
                "at": datetime(year, 7, 1, tzinfo=UTC),
                "u": uuid.uuid4(),
                "c": (customer := uuid.uuid4()),
                "r": str(customer),
            },
        )
        landed = await db.scalar(
            text(f"SELECT tableoid::regclass::text FROM {TABLE} WHERE id = :id"), {"id": row_id}
        )
        await db.rollback()
    assert landed == f"{TABLE}_{year}"


@pytest.mark.parametrize("statement", [f"UPDATE {TABLE} SET action = 'x'", f"DELETE FROM {TABLE}"])
async def test_the_app_role_may_neither_update_nor_delete(database, statement):
    async with session_scope() as db:
        with pytest.raises(DBAPIError) as refused:
            await db.execute(text(statement))
        await db.rollback()
    # 42501 insufficient_privilege: the grant refused it before the trigger had to.
    assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value


async def test_the_append_only_trigger_is_attached_and_enabled(database):
    async with session_scope() as db:
        enabled = await db.scalar(
            text(
                "SELECT tgenabled::text FROM pg_trigger "
                f"WHERE tgrelid = '{TABLE}'::regclass AND tgname = '{TRIGGER}'"
            )
        )
    assert enabled == "O"

"""S1: capability-scoped RBAC — custom roles, per-route checks, and the lockout guards.

The rule this file exists to prove is that a capability is refused *by the server*. Hiding a
button is a courtesy; a role that lacks a capability must be refused when the endpoint is
called directly, which is what every test here does — no UI in sight.

The other half is that the answer arrives without a round trip through the login screen.
Capabilities are read from the database on every request, so a capability taken away from a
role is gone from every session that role is serving, immediately. `test_revoking_a_capability_
takes_effect_on_a_live_session` is the one that would fail if anybody ever cached them into
the token.
"""

import asyncio
import json

import pytest
from fastapi import Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from auth.capabilities import CAPABILITIES, Requires
from core.db import get_purge_engine, session_scope
from core.redis import get_redis
from core.security import hash_password
from main import app as main_app
from tests.conftest import add_account

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"
OTHER_PASSWORD = "several unrelated words"

SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": "Owner@Cedar.example",
    "admin_password": PASSWORD,
}

COOKIE = "linsuite_session"
USERS_ENDPOINT = "/api/admin/users"
ROLES_ENDPOINT = "/api/admin/roles"

# Two capabilities that between them open every administrative door this task ships.
FULL_ADMIN = ["admin", "roles.manage", "users.manage"]


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async with get_purge_engine().begin() as purge:
        await purge.execute(text("DELETE FROM audit_events"))
    async with session_scope() as db:
        for table in ("users", "businesses", "setup_token"):
            await db.execute(text(f"DELETE FROM {table}"))
        # The two system roles are seeded by migration 0006 and are part of the schema, not
        # of any test's setup — clearing them would leave the wizard with no role to assign,
        # which is a state no real instance can be in. Only roles a test made go.
        await db.execute(text("DELETE FROM roles WHERE NOT is_system"))
        await db.commit()
    await get_redis().flushdb()

    from auth import setup

    async with session_scope() as db:
        token = await setup.bootstrap_setup_token(db)
    resp = await client.post("/api/setup", json={**SETUP, "token": token})
    assert resp.status_code == 201, resp.text
    # These suites predate the second factor and are about other rules, so the instance they
    # set up has the MFA policy off. Task 8 defaults it on, which would otherwise put every
    # administrator here behind the enrolment gate before the rule under test is reached;
    # `tests/test_mfa.py` is where the policy itself is exercised.
    async with session_scope() as db:
        await db.execute(text("UPDATE businesses SET mfa_required_for_admin = false"))
        await db.commit()
    client.cookies.clear()
    yield


# --- a route that exists only to be refused -------------------------------------------------
#
# `schedule.view` gates nothing yet — the calendar is a later ticket — so the one place an
# operational capability can be exercised end to end is a route mounted here. It is the whole
# proof that a capability the registry does not mark administrative works in Staff Mode.

OPERATIONAL_PROBE = "/api/test-probe/schedule"
# `catalog.manage` is administrative and gates no shipped route yet, so this probe's *only*
# guard is `Requires(...)`. That is the point: it cannot pass because something else on the
# route happened to demand Admin Mode, so it is the honest test of whether `Requires` itself
# enforces the window. A fail-open `Requires` shows up here and nowhere else.
ADMIN_PROBE = "/api/test-probe/catalog"


@pytest.fixture(autouse=True)
def probes():
    from main import app

    mounted = {getattr(r, "path", None) for r in app.routes}
    if OPERATIONAL_PROBE not in mounted:
        app.add_api_route(
            OPERATIONAL_PROBE,
            lambda: {"ok": True},
            dependencies=[Depends(Requires("schedule.view"))],
        )
    if ADMIN_PROBE not in mounted:
        app.add_api_route(
            ADMIN_PROBE,
            lambda: {"ok": True},
            dependencies=[Depends(Requires("catalog.manage"))],
        )
    yield


# --- helpers --------------------------------------------------------------------------------


async def login(client, email=EMAIL, password=PASSWORD):
    resp = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def enter_admin(client, password=PASSWORD):
    resp = await client.post("/api/auth/mode", json={"mode": "admin", "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def as_admin(client):
    await login(client)
    await enter_admin(client)


async def me(client):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def create_role(client, name, capabilities, description="A role."):
    return await client.post(
        ROLES_ENDPOINT,
        json={"name": name, "description": description, "capabilities": capabilities},
    )


async def delete_role(client, role_id):
    # The JSON-only CSRF guard in `main.py` covers every mutating method, DELETE included.
    # The frontend's one `send()` helper sets this header for the same reason.
    return await client.delete(
        f"{ROLES_ENDPOINT}/{role_id}", headers={"Content-Type": "application/json"}
    )


async def make_role(client, name, capabilities):
    resp = await create_role(client, name, capabilities)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def add_user(email, role_id, password=OTHER_PASSWORD) -> str:
    return await add_account(email, await hash_password(password), role=role_id)


async def role_id_named(name: str) -> str:
    async with session_scope() as db:
        return str(await db.scalar(text("SELECT id FROM roles WHERE name = :n"), {"n": name}))


async def audit() -> list[tuple]:
    async with session_scope() as db:
        return list(
            (
                await db.execute(
                    text(
                        "SELECT event_type, metadata::text, actor_user_id IS NOT NULL "
                        "FROM audit_events ORDER BY occurred_at, id"
                    )
                )
            ).all()
        )


# --- the registry ---------------------------------------------------------------------------


async def test_the_capability_registry_is_listed_with_a_description_and_a_group(client):
    await as_admin(client)

    resp = await client.get("/api/admin/capabilities")

    assert resp.status_code == 200
    listed = resp.json()["capabilities"]
    assert {c["key"] for c in listed} == {c.key for c in CAPABILITIES}
    for entry in listed:
        assert entry["description"].strip()
        assert entry["group"].strip()
        assert isinstance(entry["requires_admin_mode"], bool)


async def test_the_override_availability_capability_exists_and_is_assignable(client):
    """Nothing gates on it yet — Task 12 does — but a role must be able to hold it now."""
    await as_admin(client)

    role = await create_role(
        client, "Senior stylist", ["schedule.view", "schedule.override_availability"]
    )

    assert role.status_code == 201, role.text
    assert "schedule.override_availability" in role.json()["capabilities"]


async def test_a_capability_outside_the_registry_is_refused(client):
    """The registry is the vocabulary. A free-form string would be a permission nothing
    checks, which reads as granted in the UI and is refused everywhere else."""
    await as_admin(client)

    resp = await create_role(client, "Wishful", ["billing.embezzle"])

    assert resp.status_code == 422
    assert await client.get(ROLES_ENDPOINT)
    assert [r["name"] for r in (await client.get(ROLES_ENDPOINT)).json()["roles"]] == [
        "Administrator",
        "Staff",
    ]


# --- what the migration left behind ---------------------------------------------------------


async def test_the_two_system_roles_are_seeded_and_marked(client):
    await as_admin(client)

    roles = {r["name"]: r for r in (await client.get(ROLES_ENDPOINT)).json()["roles"]}

    assert roles["Administrator"]["is_system"] is True
    assert roles["Staff"]["is_system"] is True
    assert set(roles["Administrator"]["capabilities"]) == {c.key for c in CAPABILITIES}
    assert set(roles["Staff"]["capabilities"]) == {
        "schedule.view",
        "schedule.manage",
        "customers.view",
        "customers.manage",
        # Sending a form is front-desk work (0028, #46).
        "forms.issue",
    }


async def test_the_setup_wizard_puts_the_first_user_on_the_administrator_role(client):
    body = await login(client)

    assert body["role"] == "Administrator"
    assert body["can_switch_modes"] is True
    assert "admin" in body["capabilities"]


async def test_the_migration_maps_existing_accounts_by_their_old_admin_flag():
    """0006 runs against a database that still had `is_admin`, and the rows it produced are
    what the suite has been signing in as ever since. `users.is_admin` is gone; a role is the
    only authorisation signal left."""
    async with session_scope() as db:
        columns = list(
            await db.scalars(
                text("SELECT column_name FROM information_schema.columns WHERE table_name='users'")
            )
        )
        assert "is_admin" not in columns
        assert "role_id" in columns
        nullable = await db.scalar(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name='users' AND column_name='role_id'"
            )
        )
        assert nullable == "NO"


# --- the refusal, at the API ------------------------------------------------------------------


async def test_a_role_without_the_capability_is_refused_at_the_api(client):
    """Not hidden in the UI — refused by the server, for a request that names the endpoint
    directly. This is acceptance criterion 2, and the reason the toggles are worth anything."""
    await as_admin(client)
    role = await make_role(client, "Rota keeper", ["admin", "roles.manage"])
    await add_user("rota@cedar.example", role)
    client.cookies.clear()
    await login(client, email="rota@cedar.example", password=OTHER_PASSWORD)
    await enter_admin(client, OTHER_PASSWORD)

    resp = await client.get(USERS_ENDPOINT)

    assert resp.status_code == 403
    assert resp.json()["code"] == "capability_required"


async def test_the_same_request_is_allowed_once_the_capability_is_toggled_on(client):
    await as_admin(client)
    role = await make_role(client, "Rota keeper", ["admin", "roles.manage"])
    await add_user("rota@cedar.example", role)
    await client.patch(f"{ROLES_ENDPOINT}/{role}", json={"capabilities": [*FULL_ADMIN]})
    client.cookies.clear()
    await login(client, email="rota@cedar.example", password=OTHER_PASSWORD)
    await enter_admin(client, OTHER_PASSWORD)

    assert (await client.get(USERS_ENDPOINT)).status_code == 200


async def test_revoking_a_capability_takes_effect_on_a_live_session_without_re_login(client):
    """Acceptance criterion 5. The capability set is read from the database per request, so
    there is no window in which a revoked permission is still being honoured — which is
    exactly what putting it in the session token would have created."""
    await as_admin(client)
    role = await make_role(client, "Deputy", [*FULL_ADMIN])
    await add_user("deputy@cedar.example", role)
    admin_cookie = client.cookies[COOKIE]

    client.cookies.clear()
    await login(client, email="deputy@cedar.example", password=OTHER_PASSWORD)
    await enter_admin(client, OTHER_PASSWORD)
    deputy_cookie = client.cookies[COOKIE]
    assert (await client.get(USERS_ENDPOINT)).status_code == 200

    # The administrator takes it away while the deputy's session is still open.
    client.cookies.set(COOKIE, admin_cookie)
    patched = await client.patch(
        f"{ROLES_ENDPOINT}/{role}", json={"capabilities": ["admin", "roles.manage"]}
    )
    assert patched.status_code == 200, patched.text

    # Same cookie, same admin window, no sign-in in between.
    client.cookies.set(COOKIE, deputy_cookie)
    resp = await client.get(USERS_ENDPOINT)

    assert resp.status_code == 403
    assert resp.json()["code"] == "capability_required"


async def test_an_administrative_capability_is_refused_in_staff_mode_even_when_held(client):
    """Holding the capability is not the same as having it active (PRD §1). The registry says
    which capabilities carry that rule, so no route has to remember it."""
    await login(client)

    resp = await client.get(USERS_ENDPOINT)

    assert resp.status_code == 403
    assert resp.json()["code"] == "admin_mode_required"
    await enter_admin(client)
    assert (await client.get(USERS_ENDPOINT)).status_code == 200


async def test_requires_alone_enforces_admin_mode_for_an_administrative_capability(client):
    """The regression guard for a fail-open `Requires`.

    Every other administrative route in this task is reachable only through `Requires`, so a
    `Requires` that checked the capability but forgot the window would still *look* correct
    everywhere — the refusals would all still happen, for the other reason. This probe holds
    `catalog.manage` and nothing else, in Staff Mode, and the only thing that can refuse it
    is the rule under test.
    """
    await as_admin(client)
    role = await make_role(client, "Stock keeper", ["admin", "catalog.manage"])
    await add_user("stock@cedar.example", role)
    client.cookies.clear()
    await login(client, email="stock@cedar.example", password=OTHER_PASSWORD)

    # Staff Mode, and the capability is genuinely held.
    assert (await me(client))["mode"] == "staff"
    assert "catalog.manage" in (await me(client))["capabilities"]
    refused = await client.get(ADMIN_PROBE)

    assert refused.status_code == 403
    assert refused.json()["code"] == "admin_mode_required"

    # And it is the window, not the role, that was missing.
    await enter_admin(client, OTHER_PASSWORD)
    assert (await client.get(ADMIN_PROBE)).status_code == 200


async def test_an_operational_capability_works_in_staff_mode(client):
    """The other half of the same rule: a capability the registry does not mark administrative
    must not drag Admin Mode in with it, or a receptionist could not open the calendar."""
    await as_admin(client)
    role = await make_role(client, "Receptionist", ["schedule.view"])
    await add_user("desk@cedar.example", role)
    client.cookies.clear()
    await login(client, email="desk@cedar.example", password=OTHER_PASSWORD)

    assert (await me(client))["mode"] == "staff"
    assert (await client.get(OPERATIONAL_PROBE)).status_code == 200


async def test_an_operational_capability_is_still_refused_when_the_role_lacks_it(client):
    await as_admin(client)
    role = await make_role(client, "Book-keeper", ["customers.view"])
    await add_user("books@cedar.example", role)
    client.cookies.clear()
    await login(client, email="books@cedar.example", password=OTHER_PASSWORD)

    resp = await client.get(OPERATIONAL_PROBE)

    assert resp.status_code == 403
    assert resp.json()["code"] == "capability_required"


async def test_an_anonymous_request_to_a_capability_route_is_a_401(client):
    """A 403 would say the endpoint exists and the caller merely lacks a permission. Nobody
    who is not signed in has a permission to lack."""
    assert (await client.get(USERS_ENDPOINT)).status_code == 401
    assert (await client.get(OPERATIONAL_PROBE)).status_code == 401


async def test_the_business_endpoint_now_asks_for_the_admin_capability(client):
    await as_admin(client)
    role = await make_role(client, "Rota keeper", ["roles.manage", "users.manage"])
    await add_user("rota@cedar.example", role)
    client.cookies.clear()
    await login(client, email="rota@cedar.example", password=OTHER_PASSWORD)

    # No `admin` capability, so there is no Admin Mode to enter in the first place.
    assert (await me(client))["can_switch_modes"] is False
    resp = await client.get("/api/admin/business")

    assert resp.status_code == 403
    assert resp.json()["code"] == "admin_mode_required"


# --- the mode switch is a capability too -------------------------------------------------------


async def test_a_user_without_the_admin_capability_may_not_switch_modes(client):
    await as_admin(client)
    role = await make_role(client, "Receptionist", ["schedule.view"])
    await add_user("desk@cedar.example", role)
    client.cookies.clear()
    await login(client, email="desk@cedar.example", password=OTHER_PASSWORD)

    body = await me(client)
    assert body["can_switch_modes"] is False

    resp = await client.post("/api/auth/mode", json={"mode": "admin", "password": OTHER_PASSWORD})

    assert resp.status_code == 403
    assert (await me(client))["mode"] == "staff"


async def test_granting_the_admin_capability_puts_the_switcher_back(client):
    await as_admin(client)
    role = await make_role(client, "Receptionist", ["schedule.view"])
    user_cookie_email = "desk@cedar.example"
    await add_user(user_cookie_email, role)
    await client.patch(
        f"{ROLES_ENDPOINT}/{role}", json={"capabilities": ["schedule.view", "admin"]}
    )
    client.cookies.clear()
    await login(client, email=user_cookie_email, password=OTHER_PASSWORD)

    assert (await me(client))["can_switch_modes"] is True


# --- the guardrails ----------------------------------------------------------------------------


async def test_a_system_role_refuses_a_capability_edit(client):
    """Editing `Administrator` is how an instance locks itself out of its own administration,
    and the two seeded roles are the floor the rest of the guards stand on."""
    await as_admin(client)
    administrator = await role_id_named("Administrator")

    resp = await client.patch(
        f"{ROLES_ENDPOINT}/{administrator}", json={"capabilities": ["schedule.view"]}
    )

    assert resp.status_code == 409
    assert "built-in" in resp.json()["detail"].lower()
    async with session_scope() as db:
        held = await db.scalar(
            text("SELECT count(*) FROM role_capabilities WHERE role_id = :r"),
            {"r": administrator},
        )
    assert held == len(CAPABILITIES)


async def test_a_system_role_cannot_be_deleted(client):
    await as_admin(client)

    resp = await delete_role(client, await role_id_named("Staff"))

    assert resp.status_code == 409
    assert "built-in" in resp.json()["detail"].lower()


async def test_a_role_in_use_cannot_be_deleted(client):
    await as_admin(client)
    role = await make_role(client, "Receptionist", ["schedule.view"])
    await add_user("desk@cedar.example", role)

    resp = await delete_role(client, role)

    assert resp.status_code == 409
    assert "1" in resp.json()["detail"]


async def test_an_unused_custom_role_is_deleted(client):
    await as_admin(client)
    role = await make_role(client, "Receptionist", ["schedule.view"])

    assert (await delete_role(client, role)).status_code == 204
    assert [r["name"] for r in (await client.get(ROLES_ENDPOINT)).json()["roles"]] == [
        "Administrator",
        "Staff",
    ]


async def test_the_last_administrator_cannot_be_moved_off_the_role(client):
    """Acceptance criterion 6, reached from the assignment side. Nothing may leave this
    instance with nobody who can administer it — the state has no way back."""
    await as_admin(client)
    staff = await role_id_named("Staff")
    ((admin_id,),) = [(str(u),) for u in (await _user_ids())]

    resp = await client.patch(f"{USERS_ENDPOINT}/{admin_id}/role", json={"role_id": staff})

    assert resp.status_code == 409
    assert "administrator" in resp.json()["detail"].lower()
    assert (await me(client))["role"] == "Administrator"


async def test_a_second_administrator_makes_the_first_one_demotable(client):
    await as_admin(client)
    administrator = await role_id_named("Administrator")
    staff = await role_id_named("Staff")
    await add_user("second@cedar.example", administrator)
    (admin_id,) = [u for u in await _user_ids() if str(u) != await _id_of("second@cedar.example")]

    resp = await client.patch(f"{USERS_ENDPOINT}/{admin_id}/role", json={"role_id": staff})

    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "Staff"


async def test_a_custom_role_may_carry_the_administration_instead(client):
    """The guard is about the capability, not the name: moving the last administrator onto a
    role that administers just as well is allowed, and is how a business renames the job."""
    await as_admin(client)
    owner = await make_role(client, "Owner", [c.key for c in CAPABILITIES])
    (admin_id,) = await _user_ids()

    resp = await client.patch(f"{USERS_ENDPOINT}/{admin_id}/role", json={"role_id": owner})

    assert resp.status_code == 200, resp.text
    assert (await me(client))["role"] == "Owner"


async def _user_ids() -> list:
    async with session_scope() as db:
        return list(await db.scalars(text("SELECT id FROM users ORDER BY created_at")))


async def _id_of(email: str) -> str:
    async with session_scope() as db:
        return str(await db.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": email}))


async def test_two_administrators_cannot_be_demoted_at_the_same_time(client, monkeypatch):
    """The write skew the guard exists to survive.

    Two requests demoting two *different* administrators touch no row in common, so under
    READ COMMITTED neither blocks the other and each still sees the other's administrator
    administering. Both counts pass, both commit, and the instance is left with nobody who
    can administer it — which on a single-tenant deployment has no way back short of a
    database console. Serialising the check is the whole of the fix, so this is the test
    that fails without the advisory lock rather than merely looking concurrent.
    """
    await as_admin(client)
    administrator = await role_id_named("Administrator")
    staff = await role_id_named("Staff")
    second = await add_user("second@cedar.example", administrator)
    first = await _id_of(EMAIL)
    cookie = client.cookies[COOKIE]

    # The window between counting and committing is real but narrow — narrow enough that two
    # requests through one ASGI transport miss it by luck rather than by design, which would
    # make this test pass against the very bug it is here to catch. Holding each transaction
    # open for a moment after its count makes the interleaving certain instead of lucky. It
    # widens the existing window; it does not invent one.
    #
    # With the advisory lock in place the second request never reaches the count until the
    # first has committed and released it, so the pause costs the guard nothing.
    from auth import admin_users

    real_guard = admin_users.assert_an_administrator_remains

    async def slow_guard(db):
        await real_guard(db)
        await asyncio.sleep(0.25)

    monkeypatch.setattr(admin_users, "assert_an_administrator_remains", slow_guard)

    async def demote(user_id: str):
        # A client each: one `AsyncClient` serialises its own requests, so sharing one would
        # test the guard against a queue rather than against a race.
        async with AsyncClient(transport=ASGITransport(app=main_app), base_url="http://test") as c:
            c.cookies.set(COOKIE, cookie)
            return await c.patch(f"{USERS_ENDPOINT}/{user_id}/role", json={"role_id": staff})

    results = await asyncio.gather(demote(first), demote(second))

    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], [r.text for r in results]
    # The point of the exercise: somebody is still administering.
    async with session_scope() as db:
        left = await db.scalar(
            text(
                "SELECT count(*) FROM users u WHERE EXISTS (SELECT 1 FROM role_capabilities "
                "rc WHERE rc.role_id = u.role_id AND rc.capability = 'admin') AND EXISTS "
                "(SELECT 1 FROM role_capabilities rc WHERE rc.role_id = u.role_id AND "
                "rc.capability = 'roles.manage')"
            )
        )
    assert left >= 1


# --- users ---------------------------------------------------------------------------------


async def test_the_user_list_names_each_account_and_its_role(client):
    await as_admin(client)
    role = await make_role(client, "Receptionist", ["schedule.view"])
    await add_user("desk@cedar.example", role)

    listed = (await client.get(USERS_ENDPOINT)).json()["users"]

    by_email = {u["email"]: u for u in listed}
    assert by_email["desk@cedar.example"]["role"] == "Receptionist"
    assert by_email["desk@cedar.example"]["role_id"] == role
    assert by_email[EMAIL]["role"] == "Administrator"
    assert by_email["desk@cedar.example"]["locked_until"] is None


async def test_the_user_list_shows_a_locked_account(client):
    from auth import throttle

    await as_admin(client)
    role = await make_role(client, "Receptionist", ["schedule.view"])
    await add_user("desk@cedar.example", role)
    await get_redis().set(throttle.lock_key("desk@cedar.example"), "1", ex=900)

    listed = {u["email"]: u for u in (await client.get(USERS_ENDPOINT)).json()["users"]}

    assert listed["desk@cedar.example"]["locked_until"] is not None
    assert listed[EMAIL]["locked_until"] is None


async def test_unlocking_now_lives_under_the_users_capability(client):
    from auth import throttle

    await as_admin(client)
    role = await make_role(client, "Receptionist", ["schedule.view"])
    user_id = await add_user("desk@cedar.example", role)
    await get_redis().set(throttle.lock_key("desk@cedar.example"), "1", ex=900)

    resp = await client.post(f"{USERS_ENDPOINT}/{user_id}/unlock", json={})

    assert resp.status_code == 204
    assert await get_redis().exists(throttle.lock_key("desk@cedar.example")) == 0


async def test_unlocking_is_refused_without_the_users_capability(client):
    await as_admin(client)
    role = await make_role(client, "Rota keeper", ["admin", "roles.manage"])
    target = await add_user("desk@cedar.example", await role_id_named("Staff"))
    await add_user("rota@cedar.example", role)
    client.cookies.clear()
    await login(client, email="rota@cedar.example", password=OTHER_PASSWORD)
    await enter_admin(client, OTHER_PASSWORD)

    resp = await client.post(f"{USERS_ENDPOINT}/{target}/unlock", json={})

    assert resp.status_code == 403
    assert resp.json()["code"] == "capability_required"


# --- the 403 codes ---------------------------------------------------------------------------


async def test_the_forced_password_change_403_carries_its_own_code(client):
    """Three different 403s reach the browser, and the frontend has to tell them apart without
    reading prose: one re-authenticates, one apologises, one routes elsewhere."""
    await login(client)
    async with session_scope() as db:
        await db.execute(text("UPDATE users SET must_change_password = true"))
        await db.commit()

    resp = await client.get("/api/admin/business")

    assert resp.status_code == 403
    assert resp.json()["code"] == "password_change_required"
    assert isinstance(resp.json()["detail"], str)


async def test_every_403_the_api_emits_names_its_kind(client):
    await as_admin(client)
    role = await make_role(client, "Rota keeper", ["admin", "roles.manage"])
    await add_user("rota@cedar.example", role)
    client.cookies.clear()
    await login(client, email="rota@cedar.example", password=OTHER_PASSWORD)

    staff_mode = await client.get(USERS_ENDPOINT)
    await enter_admin(client, OTHER_PASSWORD)
    no_capability = await client.get(USERS_ENDPOINT)

    assert staff_mode.json()["code"] == "admin_mode_required"
    assert no_capability.json()["code"] == "capability_required"


async def test_every_403_the_api_emits_carries_a_code(client):
    """The invariant, kept honest in one place.

    "Some 403s are coded" is the state that invites a test fake to invent a code the server
    never sends — which is how `Incorrect password` came to be labelled `admin_mode_required`
    in the frontend harness, one step from telling somebody who mistyped that their Admin
    Mode had expired. Every 403 below is a different code path, and every one must name its
    kind.
    """
    # The setup wizard, before anybody is signed in at all.
    async with session_scope() as db:
        await db.execute(text("DELETE FROM businesses"))
        await db.commit()
    from auth import setup as setup_mod

    async with session_scope() as db:
        await setup_mod.bootstrap_setup_token(db)
    bad_token = await client.post("/api/setup", json={**SETUP, "token": "not the token"})

    await login(client)
    # No live window, and no password offered: the expected lapsed-grant race.
    reauth_needed = await client.post("/api/auth/mode", json={"mode": "admin"})
    # A password that was actually tried, and was wrong.
    wrong_password = await client.post(
        "/api/auth/mode", json={"mode": "admin", "password": "not the password"}
    )
    # Both wrong passwords feed the one per-account counter, so the second attempt is
    # inside the progressive delay by design. The user coming back a second later is the
    # case under test here; the delay itself is `test_lockout.py`'s subject.
    from auth import throttle

    await get_redis().delete(throttle.delay_key(EMAIL))
    wrong_current = await client.post(
        "/api/auth/password/change",
        json={"current_password": "also not it", "new_password": "a brand new passphrase"},
    )
    # The one 403 that is not raised but built: the upload middleware runs outside the
    # exception handler, so it has to carry its own `code` and this list is what proves it
    # does. An enumerated invariant only covers what somebody remembered to enumerate.
    no_origin = await client.post(
        "/api/admin/business/logo", files={"file": ("logo.png", b"\x89PNG\r\n\x1a\n", "image/png")}
    )

    for resp, code in (
        (bad_token, "invalid_setup_token"),
        (reauth_needed, "admin_mode_required"),
        (wrong_password, "invalid_password"),
        (wrong_current, "invalid_password"),
        (no_origin, "upload_origin_required"),
    ):
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == code, resp.text
        # `detail` stays the sentence a person reads, whatever the code says.
        assert isinstance(resp.json()["detail"], str)


# --- the audit log ---------------------------------------------------------------------------


async def test_role_and_assignment_changes_are_audited_with_the_administrator(client):
    await as_admin(client)
    role = await make_role(client, "Receptionist", ["schedule.view"])
    await client.patch(
        f"{ROLES_ENDPOINT}/{role}", json={"capabilities": ["schedule.view", "schedule.manage"]}
    )
    (admin_id,) = await _user_ids()
    owner = await make_role(client, "Owner", [c.key for c in CAPABILITIES])
    await client.patch(f"{USERS_ENDPOINT}/{admin_id}/role", json={"role_id": owner})
    await delete_role(client, role)

    events = [(event, has_actor) for event, _, has_actor in await audit()]

    assert ("role.created", True) in events
    assert ("role.updated", True) in events
    assert ("user.role_assigned", True) in events
    assert ("role.deleted", True) in events


async def test_a_capability_change_is_recorded_as_what_changed(client):
    await as_admin(client)
    role = await make_role(client, "Receptionist", ["schedule.view"])

    await client.patch(
        f"{ROLES_ENDPOINT}/{role}", json={"capabilities": ["customers.view", "schedule.view"]}
    )

    metadata = json.loads([m for e, m, _ in await audit() if e == "role.updated"][0])
    assert sorted(metadata["capabilities"]) == ["customers.view", "schedule.view"]
    assert metadata["name"] == "Receptionist"


# --- naming ----------------------------------------------------------------------------------


async def test_two_roles_cannot_share_a_name(client):
    await as_admin(client)
    await make_role(client, "Receptionist", ["schedule.view"])

    resp = await create_role(client, "Receptionist", ["customers.view"])

    assert resp.status_code == 409


async def test_a_role_endpoint_needs_the_roles_capability(client):
    await as_admin(client)
    role = await make_role(client, "Desk lead", ["admin", "users.manage"])
    await add_user("lead@cedar.example", role)
    client.cookies.clear()
    await login(client, email="lead@cedar.example", password=OTHER_PASSWORD)
    await enter_admin(client, OTHER_PASSWORD)

    assert (await client.get(USERS_ENDPOINT)).status_code == 200
    assert (await client.get(ROLES_ENDPOINT)).status_code == 403
    assert (await create_role(client, "Nope", ["schedule.view"])).status_code == 403

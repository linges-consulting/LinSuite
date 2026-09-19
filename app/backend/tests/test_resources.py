"""S1: spaces and equipment — the physical things services are delivered in and with.

Both are rows in the same `resources` table, told apart by `kind`. This ticket only creates
and manages them; the exclusion constraint that refuses double-booking one arrives with the
booking ticket (tech-stack §15, §20) — a chair cannot be in two places, and that rule is
absolute rather than configurable.

**Names are unique per kind, case-insensitively.** "Room 1" and "room 1" are the same space;
"Room 1" the space and "Room 1" the equipment are not the same thing, so the same name is
allowed once per kind.

**Deactivation, never deletion.** The row stays so a historical appointment that referenced
it is never orphaned; only later tickets' pickers care that it is inactive.
"""

import pytest
from sqlalchemy import text

from core.db import get_purge_engine, session_scope

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"

SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": EMAIL,
    "admin_password": PASSWORD,
}

RESOURCES = "/api/admin/resources"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async with get_purge_engine().begin() as purge:
        await purge.execute(text("DELETE FROM audit_events"))
    async with session_scope() as db:
        for table in (
            "resources",
            "staff",
            "password_reset_tokens",
            "users",
            "businesses",
            "setup_token",
        ):
            await db.execute(text(f"DELETE FROM {table}"))
        await db.execute(text("DELETE FROM roles WHERE NOT is_system"))
        await db.commit()

    from auth import setup

    async with session_scope() as db:
        token = await setup.bootstrap_setup_token(db)
    resp = await client.post("/api/setup", json={**SETUP, "token": token})
    assert resp.status_code == 201, resp.text
    async with session_scope() as db:
        await db.execute(text("UPDATE businesses SET mfa_required_for_admin = false"))
        await db.commit()
    client.cookies.clear()
    yield


# --- helpers --------------------------------------------------------------------------------


async def as_admin(client, email=EMAIL, password=PASSWORD):
    login = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    resp = await client.post("/api/auth/mode", json={"mode": "admin", "password": password})
    assert resp.status_code == 200, resp.text


def draft(**overrides) -> dict:
    body = {"kind": "space", "name": "Room 1", "description": "Treatment room", "colour": "blue"}
    body.update(overrides)
    return body


async def create(client, **overrides):
    return await client.post(RESOURCES, json=draft(**overrides))


async def make(client, **overrides) -> dict:
    resp = await create(client, **overrides)
    assert resp.status_code == 201, resp.text
    return resp.json()


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


async def role_id_named(name: str) -> str:
    async with session_scope() as db:
        return str(await db.scalar(text("SELECT id FROM roles WHERE name = :n"), {"n": name}))


# --- creating -------------------------------------------------------------------------------


async def test_creating_a_space(client):
    await as_admin(client)

    created = await make(client, kind="space", name="Treatment Room A")

    assert created["kind"] == "space"
    assert created["name"] == "Treatment Room A"
    assert created["active"] is True
    assert created["colour"] == "blue"


async def test_creating_equipment(client):
    await as_admin(client)

    created = await make(client, kind="equipment", name="Laser Unit 1", colour=None)

    assert created["kind"] == "equipment"
    assert created["name"] == "Laser Unit 1"
    assert created["colour"] is None


async def test_names_are_unique_per_kind_case_insensitively(client):
    await as_admin(client)
    await make(client, kind="space", name="Room 1")

    dupe = await create(client, kind="space", name="room 1")

    assert dupe.status_code == 409, dupe.text


async def test_the_same_name_is_allowed_across_kinds(client):
    await as_admin(client)
    await make(client, kind="space", name="Room 1")

    equipment = await create(client, kind="equipment", name="Room 1")

    assert equipment.status_code == 201, equipment.text


async def test_renaming_to_an_existing_name_in_the_same_kind_is_refused(client):
    await as_admin(client)
    await make(client, kind="space", name="Room 1")
    other = await make(client, kind="space", name="Room 2")

    resp = await client.patch(f"{RESOURCES}/{other['id']}", json={"name": "room 1"})

    assert resp.status_code == 409, resp.text


async def test_a_colour_outside_the_palette_is_refused(client):
    await as_admin(client)

    resp = await create(client, colour="chartreuse")

    assert resp.status_code == 422, resp.text


# --- editing --------------------------------------------------------------------------------


async def test_editing_records_only_the_fields_that_changed(client):
    await as_admin(client)
    resource = await make(client, name="Room 1")

    resp = await client.patch(f"{RESOURCES}/{resource['id']}", json={"sort_order": 3})

    assert resp.status_code == 200, resp.text
    assert resp.json()["sort_order"] == 3
    updated = [row for row in await audit() if row[0] == "resource.updated"]
    assert len(updated) == 1
    assert "sort_order" in updated[0][1]
    assert "name" not in updated[0][1]


@pytest.mark.parametrize("field,value", [("name", None), ("sort_order", None), ("name", "   ")])
async def test_emptying_a_required_field_is_refused_rather_than_a_five_hundred(
    client, field, value
):
    await as_admin(client)
    resource = await make(client, name="Room 1")

    resp = await client.patch(f"{RESOURCES}/{resource['id']}", json={field: value})

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", field]
    async with session_scope() as db:
        assert (
            await db.scalar(text("SELECT name FROM resources WHERE id = :i"), {"i": resource["id"]})
            == "Room 1"
        )


async def test_description_and_colour_may_be_cleared(client):
    await as_admin(client)
    resource = await make(client, name="Room 1")

    resp = await client.patch(
        f"{RESOURCES}/{resource['id']}", json={"description": None, "colour": None}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] is None
    assert resp.json()["colour"] is None


# --- deactivation ---------------------------------------------------------------------------


async def test_deactivate_and_reactivate_flip_the_flag_and_preserve_the_row(client):
    await as_admin(client)
    resource = await make(client, name="Room 1", colour="teal")

    deactivated = await client.post(f"{RESOURCES}/{resource['id']}/deactivate", json={})
    assert deactivated.status_code == 200, deactivated.text
    assert deactivated.json()["active"] is False

    async with session_scope() as db:
        kept = await db.execute(
            text("SELECT name, colour FROM resources WHERE id = :i"), {"i": resource["id"]}
        )
    assert kept.one() == ("Room 1", "teal")

    reactivated = await client.post(f"{RESOURCES}/{resource['id']}/reactivate", json={})
    assert reactivated.status_code == 200, reactivated.text
    assert reactivated.json()["active"] is True


async def test_deactivating_twice_is_a_no_op(client):
    await as_admin(client)
    resource = await make(client, name="Room 1")
    await client.post(f"{RESOURCES}/{resource['id']}/deactivate", json={})

    resp = await client.post(f"{RESOURCES}/{resource['id']}/deactivate", json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["active"] is False


# --- listing --------------------------------------------------------------------------------


async def test_listing_filters_by_kind_and_hides_inactive_by_default(client):
    await as_admin(client)
    space = await make(client, kind="space", name="Room 1")
    equipment = await make(client, kind="equipment", name="Laser 1")
    await client.post(f"{RESOURCES}/{space['id']}/deactivate", json={})

    spaces = (await client.get(f"{RESOURCES}?kind=space")).json()["resources"]
    every_space = (await client.get(f"{RESOURCES}?kind=space&include_inactive=true")).json()[
        "resources"
    ]
    equipment_list = (await client.get(f"{RESOURCES}?kind=equipment")).json()["resources"]

    assert spaces == []
    assert [r["id"] for r in every_space] == [space["id"]]
    assert [r["id"] for r in equipment_list] == [equipment["id"]]


async def test_listing_with_no_kind_returns_both(client):
    await as_admin(client)
    space = await make(client, kind="space", name="Room 1")
    equipment = await make(client, kind="equipment", name="Laser 1")

    listed = (await client.get(RESOURCES)).json()["resources"]

    assert {r["id"] for r in listed} == {space["id"], equipment["id"]}


# --- who may do any of this -----------------------------------------------------------------


async def test_every_resource_endpoint_needs_catalog_manage(client):
    from core.security import hash_password
    from tests.conftest import add_account

    await as_admin(client)
    resource = await make(client, name="Room 1")
    role = await client.post(
        "/api/admin/roles",
        json={"name": "Deputy", "description": "Nearly.", "capabilities": ["admin"]},
    )
    assert role.status_code == 201, role.text
    deputy_password = "correct horse battery 2"
    await add_account(
        "deputy@cedar.example", await hash_password(deputy_password), role=role.json()["id"]
    )
    client.cookies.clear()
    await as_admin(client, "deputy@cedar.example", deputy_password)

    refusals = [
        await client.get(RESOURCES),
        await client.post(RESOURCES, json=draft(name="nope")),
        await client.patch(f"{RESOURCES}/{resource['id']}", json={"sort_order": 3}),
        await client.post(f"{RESOURCES}/{resource['id']}/deactivate", json={}),
        await client.post(f"{RESOURCES}/{resource['id']}/reactivate", json={}),
    ]

    assert [r.status_code for r in refusals] == [403] * 5
    assert {r.json()["code"] for r in refusals} == {"capability_required"}


async def test_resources_are_refused_outside_admin_mode(client):
    await as_admin(client)
    client.cookies.clear()
    login = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200  # Staff Mode

    resp = await client.get(RESOURCES)

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "admin_mode_required"


# --- the trail ------------------------------------------------------------------------------


async def test_the_trail_records_the_whole_life_of_a_resource(client):
    await as_admin(client)
    resource = await make(client, name="Room 1")
    await client.patch(f"{RESOURCES}/{resource['id']}", json={"sort_order": 2})
    await client.post(f"{RESOURCES}/{resource['id']}/deactivate", json={})
    await client.post(f"{RESOURCES}/{resource['id']}/reactivate", json={})

    recorded = await events()

    for event in (
        "resource.created",
        "resource.updated",
        "resource.deactivated",
        "resource.reactivated",
    ):
        assert event in recorded, recorded

"""S1: package & bundle definitions (#60) — admin CRUD, no purchase flow yet.

A "package" is a definition naming one service; a "bundle" names several. The schema does not
distinguish them (`billing/models.py`) — this suite exercises the count-of-one and
count-of-several cases through the same endpoints. `billing.manage` gates all of it.
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

PACKAGES = "/api/admin/packages"
SERVICES = "/api/admin/services"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async with get_purge_engine().begin() as purge:
        await purge.execute(text("DELETE FROM audit_events"))
    async with session_scope() as db:
        for table in (
            "package_definition_services",
            "package_definitions",
            "service_requirements",
            "service_staff",
            "services",
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
    assert login.status_code == 200, login.text
    resp = await client.post("/api/auth/mode", json={"mode": "admin", "password": password})
    assert resp.status_code == 200, resp.text


async def make_service(client, **overrides) -> dict:
    body = {"name": "Massage", "duration_minutes": 60, "price_cents": 12000}
    body.update(overrides)
    resp = await client.post(SERVICES, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def draft(services: list[dict], **overrides) -> dict:
    body = {
        "name": "10-Session Massage Pack",
        "description": "Ten sessions, save 20%.",
        "price_cents": 96000,
        "services": services,
    }
    body.update(overrides)
    return body


async def create(client, services: list[dict], **overrides):
    return await client.post(PACKAGES, json=draft(services, **overrides))


async def make(client, services: list[dict], **overrides) -> dict:
    resp = await create(client, services, **overrides)
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


# --- creating: a package (one service) -------------------------------------------------------


async def test_creating_a_package_with_one_service(client):
    await as_admin(client)
    massage = await make_service(client)

    created = await make(client, [{"service_id": massage["id"], "credits": 10}])

    assert created["name"] == "10-Session Massage Pack"
    assert created["price_cents"] == 96000
    assert created["active"] is True
    # Off by default (CLAUDE.md): no expiry, not transferable.
    assert created["expires_after_days"] is None
    assert created["transferable"] is False
    assert created["services"] == [
        {
            "service_id": massage["id"],
            "service_name": massage["name"],
            "service_active": True,
            "credits": 10,
        }
    ]
    types = [row[0] for row in await audit()]
    assert [t for t in types if t.startswith(("service.", "package_definition."))] == [
        "service.created",
        "package_definition.created",
    ]


# --- creating: a bundle (several named services) ----------------------------------------------


async def test_creating_a_bundle_with_several_services(client):
    await as_admin(client)
    cut = await make_service(client, name="Haircut", price_cents=6000)
    colour = await make_service(client, name="Colour", price_cents=12000)

    created = await make(
        client,
        [
            {"service_id": cut["id"], "credits": 1},
            {"service_id": colour["id"], "credits": 2},
        ],
        name="Cut & Colour Bundle",
        price_cents=15000,
    )

    assert len(created["services"]) == 2
    by_name = {row["service_name"]: row["credits"] for row in created["services"]}
    assert by_name == {"Haircut": 1, "Colour": 2}


async def test_an_expiry_and_transferability_may_be_opted_into(client):
    await as_admin(client)
    massage = await make_service(client)

    created = await make(
        client,
        [{"service_id": massage["id"], "credits": 5}],
        expires_after_days=365,
        transferable=True,
    )

    assert created["expires_after_days"] == 365
    assert created["transferable"] is True


async def test_a_definition_with_no_services_is_refused(client):
    await as_admin(client)

    resp = await create(client, [])

    assert resp.status_code == 422, resp.text


async def test_an_unknown_service_is_refused(client):
    await as_admin(client)

    resp = await create(
        client, [{"service_id": "00000000-0000-0000-0000-000000000000", "credits": 1}]
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "services"]


async def test_an_inactive_service_may_not_be_packaged(client):
    await as_admin(client)
    massage = await make_service(client)
    await client.post(f"{SERVICES}/{massage['id']}/deactivate", json={})

    resp = await create(client, [{"service_id": massage["id"], "credits": 1}])

    assert resp.status_code == 422, resp.text


async def test_the_same_service_twice_in_one_definition_is_refused(client):
    await as_admin(client)
    massage = await make_service(client)

    resp = await create(
        client,
        [
            {"service_id": massage["id"], "credits": 1},
            {"service_id": massage["id"], "credits": 2},
        ],
    )

    assert resp.status_code == 422, resp.text


async def test_names_are_unique_case_insensitively(client):
    await as_admin(client)
    massage = await make_service(client)
    await make(client, [{"service_id": massage["id"], "credits": 1}], name="Massage Pack")

    dupe = await create(client, [{"service_id": massage["id"], "credits": 1}], name="massage pack")

    assert dupe.status_code == 409, dupe.text


# --- editing ------------------------------------------------------------------------------


async def test_editing_records_only_the_fields_that_changed(client):
    await as_admin(client)
    massage = await make_service(client)
    package = await make(client, [{"service_id": massage["id"], "credits": 10}])

    resp = await client.patch(f"{PACKAGES}/{package['id']}", json={"price_cents": 90000})

    assert resp.status_code == 200, resp.text
    assert resp.json()["price_cents"] == 90000
    updated = [row for row in await audit() if row[0] == "package_definition.updated"]
    assert len(updated) == 1
    assert "price_cents" in updated[0][1]
    assert "name" not in updated[0][1]


async def test_expiry_may_be_turned_off_again_by_sending_null(client):
    await as_admin(client)
    massage = await make_service(client)
    package = await make(
        client, [{"service_id": massage["id"], "credits": 10}], expires_after_days=90
    )

    resp = await client.patch(f"{PACKAGES}/{package['id']}", json={"expires_after_days": None})

    assert resp.status_code == 200, resp.text
    assert resp.json()["expires_after_days"] is None


@pytest.mark.parametrize("field", ["name", "price_cents", "transferable"])
async def test_emptying_a_required_field_is_refused_rather_than_a_five_hundred(client, field):
    await as_admin(client)
    massage = await make_service(client)
    package = await make(client, [{"service_id": massage["id"], "credits": 10}])

    resp = await client.patch(f"{PACKAGES}/{package['id']}", json={field: None})

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", field]


async def test_renaming_into_an_existing_name_is_refused(client):
    await as_admin(client)
    massage = await make_service(client)
    await make(client, [{"service_id": massage["id"], "credits": 1}], name="Massage Pack")
    other = await make(client, [{"service_id": massage["id"], "credits": 1}], name="Other Pack")

    resp = await client.patch(f"{PACKAGES}/{other['id']}", json={"name": "massage pack"})

    assert resp.status_code == 409, resp.text


# --- replacing the service set -----------------------------------------------------------------


async def test_replacing_the_service_set(client):
    await as_admin(client)
    cut = await make_service(client, name="Haircut", price_cents=6000)
    colour = await make_service(client, name="Colour", price_cents=12000)
    package = await make(client, [{"service_id": cut["id"], "credits": 1}])

    resp = await client.put(
        f"{PACKAGES}/{package['id']}/services",
        json={"services": [{"service_id": colour["id"], "credits": 3}]},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["services"] == [
        {
            "service_id": colour["id"],
            "service_name": "Colour",
            "service_active": True,
            "credits": 3,
        }
    ]


async def test_the_service_set_may_not_be_emptied(client):
    await as_admin(client)
    massage = await make_service(client)
    package = await make(client, [{"service_id": massage["id"], "credits": 10}])

    resp = await client.put(f"{PACKAGES}/{package['id']}/services", json={"services": []})

    assert resp.status_code == 422, resp.text


# --- leaving, and coming back -----------------------------------------------------------------


async def test_deactivating_and_reactivating(client):
    await as_admin(client)
    massage = await make_service(client)
    package = await make(client, [{"service_id": massage["id"], "credits": 10}])

    deactivated = await client.post(f"{PACKAGES}/{package['id']}/deactivate", json={})
    assert deactivated.status_code == 200, deactivated.text
    assert deactivated.json()["active"] is False

    listed = await client.get(PACKAGES)
    assert listed.json()["packages"] == []
    listed_all = await client.get(PACKAGES, params={"include_inactive": True})
    assert len(listed_all.json()["packages"]) == 1

    reactivated = await client.post(f"{PACKAGES}/{package['id']}/reactivate", json={})
    assert reactivated.status_code == 200, reactivated.text
    assert reactivated.json()["active"] is True


# --- who may do any of this --------------------------------------------------------------------


async def test_every_package_endpoint_needs_billing_manage(client):
    from core.security import hash_password
    from tests.conftest import add_account

    await as_admin(client)
    massage = await make_service(client)
    package = await make(client, [{"service_id": massage["id"], "credits": 10}])
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
        await client.get(PACKAGES),
        await client.post(PACKAGES, json=draft([{"service_id": massage["id"], "credits": 1}])),
        await client.patch(f"{PACKAGES}/{package['id']}", json={"price_cents": 1}),
        await client.put(f"{PACKAGES}/{package['id']}/services", json={"services": []}),
        await client.post(f"{PACKAGES}/{package['id']}/deactivate", json={}),
        await client.post(f"{PACKAGES}/{package['id']}/reactivate", json={}),
    ]

    assert [r.status_code for r in refusals] == [403] * len(refusals)
    assert {r.json()["code"] for r in refusals} == {"capability_required"}


async def test_packages_are_refused_outside_admin_mode(client):
    await as_admin(client)
    massage = await make_service(client)
    await make(client, [{"service_id": massage["id"], "credits": 10}])
    client.cookies.clear()
    login = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200  # Staff Mode

    resp = await client.get(PACKAGES)

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "admin_mode_required"

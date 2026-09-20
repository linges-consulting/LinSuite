"""S1: the service catalog — what the business sells, and what delivering it needs.

A service carries the four numbers the availability engine slides across a day (tech-stack
§19): `duration_minutes`, the two buffers, and — through `service_requirements` — the rooms
and devices whose free intervals it has to be intersected with. `price_cents` is an integer
(§21); nothing here ever sees a float.

**Two sets hang off a service and both are replaced whole.** Eligible staff
(`PUT /{id}/staff`) and resource requirements (`PUT /{id}/requirements`) are what a screen
saves as one decision, so a per-row API would let a half-applied set exist between two
requests — the same argument as the weekly matrix in `test_hours.py`.

**A requirement with no `resource_id` means "any active resource of that kind".** A named one
means that exact room or device, and its kind has to agree with the row's.

**`GET /api/catalog/services`** is the read side Task 14's engine and Task 15's booking screen
consume: active services only, in the shape they need, reachable by anybody signed in —
booking is staff work, not administration.
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

SERVICES = "/api/admin/services"
RESOURCES = "/api/admin/resources"
CATALOG = "/api/catalog/services"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async with get_purge_engine().begin() as purge:
        await purge.execute(text("DELETE FROM audit_events"))
    async with session_scope() as db:
        for table in (
            "service_requirements",
            "service_staff",
            "services",
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
    assert login.status_code == 200, login.text
    resp = await client.post("/api/auth/mode", json={"mode": "admin", "password": password})
    assert resp.status_code == 200, resp.text


def draft(**overrides) -> dict:
    body = {
        "name": "Swedish Massage",
        "description": "Sixty minutes, full body.",
        "duration_minutes": 60,
        "buffer_before_minutes": 5,
        "buffer_after_minutes": 15,
        "price_cents": 12000,
    }
    body.update(overrides)
    return body


async def create(client, **overrides):
    return await client.post(SERVICES, json=draft(**overrides))


async def make(client, **overrides) -> dict:
    resp = await create(client, **overrides)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def make_resource(client, **overrides) -> dict:
    body = {"kind": "space", "name": "Room 1"}
    body.update(overrides)
    resp = await client.post(RESOURCES, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def staff_role_id() -> str:
    async with session_scope() as db:
        return str(await db.scalar(text("SELECT id FROM roles WHERE name = 'Staff'")))


async def make_staff(client, email: str, first_name: str) -> dict:
    resp = await client.post(
        "/api/admin/staff",
        json={
            "email": email,
            "first_name": first_name,
            "last_name": "Rossi",
            "role_id": await staff_role_id(),
        },
    )
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


# --- creating -------------------------------------------------------------------------------


async def test_creating_a_service(client):
    await as_admin(client)

    created = await make(client)

    assert created["name"] == "Swedish Massage"
    assert created["duration_minutes"] == 60
    assert created["buffer_before_minutes"] == 5
    assert created["buffer_after_minutes"] == 15
    assert created["price_cents"] == 12000
    # Sold online unless somebody says otherwise: the common case, and the one a business
    # that never opens a portal is unaffected by.
    assert created["bookable_online"] is True
    assert created["active"] is True
    # A brand-new service is deliverable by nobody and needs nothing, and both are real
    # answers rather than missing keys.
    assert created["staff_ids"] == []
    assert created["requirements"] == []


async def test_a_service_may_be_staff_only(client):
    await as_admin(client)

    created = await make(client, bookable_online=False)

    assert created["bookable_online"] is False


async def test_buffers_and_price_default_to_nothing(client):
    await as_admin(client)

    created = await client.post(
        SERVICES, json={"name": "Consultation", "duration_minutes": 15, "price_cents": 0}
    )

    assert created.status_code == 201, created.text
    assert created.json()["buffer_before_minutes"] == 0
    assert created.json()["buffer_after_minutes"] == 0
    assert created.json()["price_cents"] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("duration_minutes", 7),
        ("duration_minutes", 0),
        ("duration_minutes", 4),
        ("buffer_before_minutes", 3),
        ("buffer_after_minutes", -5),
        ("price_cents", -1),
    ],
)
async def test_the_numbers_the_engine_reads_are_refused_when_they_are_nonsense(
    client, field, value
):
    await as_admin(client)

    resp = await create(client, **{field: value})

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"][-1] == field


async def test_names_are_unique_case_insensitively(client):
    await as_admin(client)
    await make(client, name="Swedish Massage")

    dupe = await create(client, name="swedish massage")

    assert dupe.status_code == 409, dupe.text


async def test_renaming_into_an_existing_name_is_refused(client):
    await as_admin(client)
    await make(client, name="Swedish Massage")
    other = await make(client, name="Deep Tissue")

    resp = await client.patch(f"{SERVICES}/{other['id']}", json={"name": "swedish massage"})

    assert resp.status_code == 409, resp.text


# --- editing --------------------------------------------------------------------------------


async def test_editing_records_only_the_fields_that_changed(client):
    await as_admin(client)
    service = await make(client)

    resp = await client.patch(f"{SERVICES}/{service['id']}", json={"price_cents": 13500})

    assert resp.status_code == 200, resp.text
    assert resp.json()["price_cents"] == 13500
    updated = [row for row in await audit() if row[0] == "service.updated"]
    assert len(updated) == 1
    assert "price_cents" in updated[0][1]
    assert "duration_minutes" not in updated[0][1]


@pytest.mark.parametrize(
    "field",
    [
        "name",
        "duration_minutes",
        "buffer_before_minutes",
        "buffer_after_minutes",
        "price_cents",
        "bookable_online",
        "sort_order",
    ],
)
async def test_emptying_a_required_field_is_refused_rather_than_a_five_hundred(client, field):
    await as_admin(client)
    service = await make(client)

    resp = await client.patch(f"{SERVICES}/{service['id']}", json={field: None})

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", field]
    async with session_scope() as db:
        assert (
            await db.scalar(
                text("SELECT duration_minutes FROM services WHERE id = :i"), {"i": service["id"]}
            )
            == 60
        )


async def test_the_description_may_be_cleared(client):
    await as_admin(client)
    service = await make(client)

    resp = await client.patch(f"{SERVICES}/{service['id']}", json={"description": None})

    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] is None


async def test_editing_keeps_the_sets_it_was_not_asked_about(client):
    await as_admin(client)
    service = await make(client)
    ana = await make_staff(client, "ana@cedar.example", "Ana")
    room = await make_resource(client, name="Room 1")
    await client.put(f"{SERVICES}/{service['id']}/staff", json={"staff_ids": [ana["id"]]})
    await client.put(
        f"{SERVICES}/{service['id']}/requirements",
        json={"requirements": [{"kind": "space", "resource_id": room["id"]}]},
    )

    resp = await client.patch(f"{SERVICES}/{service['id']}", json={"price_cents": 9900})

    assert resp.status_code == 200, resp.text
    assert resp.json()["staff_ids"] == [ana["id"]]
    assert len(resp.json()["requirements"]) == 1


# --- who may deliver it -----------------------------------------------------------------------


async def test_replacing_the_eligible_staff(client):
    await as_admin(client)
    service = await make(client)
    ana = await make_staff(client, "ana@cedar.example", "Ana")
    bo = await make_staff(client, "bo@cedar.example", "Bo")

    resp = await client.put(
        f"{SERVICES}/{service['id']}/staff", json={"staff_ids": [ana["id"], bo["id"]]}
    )

    assert resp.status_code == 200, resp.text
    assert set(resp.json()["staff_ids"]) == {ana["id"], bo["id"]}

    # Replaced, not added to: the screen saves a set, and dropping somebody has to be
    # expressible by leaving them out.
    narrowed = await client.put(f"{SERVICES}/{service['id']}/staff", json={"staff_ids": [bo["id"]]})
    assert narrowed.json()["staff_ids"] == [bo["id"]]

    emptied = await client.put(f"{SERVICES}/{service['id']}/staff", json={"staff_ids": []})
    assert emptied.json()["staff_ids"] == []


async def test_an_unknown_staff_member_is_refused(client):
    await as_admin(client)
    service = await make(client)

    resp = await client.put(
        f"{SERVICES}/{service['id']}/staff",
        json={"staff_ids": ["00000000-0000-0000-0000-000000000000"]},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "staff_ids"]


async def test_an_inactive_staff_member_may_not_be_made_eligible(client):
    await as_admin(client)
    service = await make(client)
    ana = await make_staff(client, "ana@cedar.example", "Ana")
    await client.post(f"/api/admin/staff/{ana['id']}/deactivate", json={})

    resp = await client.put(f"{SERVICES}/{service['id']}/staff", json={"staff_ids": [ana["id"]]})

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "staff_ids"]


async def test_a_refused_staff_replace_leaves_the_old_set_exactly_where_it_was(client):
    """The DELETE and the INSERTs share a transaction. A set that came back half-applied
    would be a service briefly deliverable by the wrong people."""
    await as_admin(client)
    service = await make(client)
    ana = await make_staff(client, "ana@cedar.example", "Ana")
    await client.put(f"{SERVICES}/{service['id']}/staff", json={"staff_ids": [ana["id"]]})

    refused = await client.put(
        f"{SERVICES}/{service['id']}/staff",
        json={"staff_ids": [ana["id"], "00000000-0000-0000-0000-000000000000"]},
    )

    assert refused.status_code == 422, refused.text
    assert (await client.get(SERVICES)).json()["services"][0]["staff_ids"] == [ana["id"]]


async def test_the_same_staff_member_twice_is_one_row(client):
    await as_admin(client)
    service = await make(client)
    ana = await make_staff(client, "ana@cedar.example", "Ana")

    resp = await client.put(
        f"{SERVICES}/{service['id']}/staff", json={"staff_ids": [ana["id"], ana["id"]]}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["staff_ids"] == [ana["id"]]


# --- what delivering it needs ------------------------------------------------------------------


async def test_requiring_any_space_and_one_particular_device(client):
    await as_admin(client)
    service = await make(client)
    laser = await make_resource(client, kind="equipment", name="Laser Unit 1")

    resp = await client.put(
        f"{SERVICES}/{service['id']}/requirements",
        json={
            "requirements": [
                {"kind": "space", "resource_id": None},
                {"kind": "equipment", "resource_id": laser["id"]},
            ]
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["requirements"] == [
        {"kind": "space", "resource_id": None, "resource_name": None, "resource_active": None},
        {
            "kind": "equipment",
            "resource_id": laser["id"],
            "resource_name": "Laser Unit 1",
            "resource_active": True,
        },
    ]


async def test_requirements_are_replaced_whole(client):
    await as_admin(client)
    service = await make(client)
    room = await make_resource(client, name="Room 1")
    await client.put(
        f"{SERVICES}/{service['id']}/requirements",
        json={"requirements": [{"kind": "space", "resource_id": room["id"]}]},
    )

    resp = await client.put(f"{SERVICES}/{service['id']}/requirements", json={"requirements": []})

    assert resp.status_code == 200, resp.text
    assert resp.json()["requirements"] == []


async def test_a_requirement_whose_kind_disagrees_with_the_resource_is_refused(client):
    await as_admin(client)
    service = await make(client)
    laser = await make_resource(client, kind="equipment", name="Laser Unit 1")

    resp = await client.put(
        f"{SERVICES}/{service['id']}/requirements",
        json={"requirements": [{"kind": "space", "resource_id": laser["id"]}]},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "requirements"]


async def test_an_inactive_resource_may_not_be_required(client):
    await as_admin(client)
    service = await make(client)
    room = await make_resource(client, name="Room 1")
    await client.post(f"{RESOURCES}/{room['id']}/deactivate", json={})

    resp = await client.put(
        f"{SERVICES}/{service['id']}/requirements",
        json={"requirements": [{"kind": "space", "resource_id": room["id"]}]},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "requirements"]


async def test_an_unknown_resource_is_refused(client):
    await as_admin(client)
    service = await make(client)

    resp = await client.put(
        f"{SERVICES}/{service['id']}/requirements",
        json={
            "requirements": [
                {"kind": "space", "resource_id": "00000000-0000-0000-0000-000000000000"}
            ]
        },
    )

    assert resp.status_code == 422, resp.text


async def test_a_refused_requirements_replace_leaves_the_old_set_exactly_where_it_was(client):
    await as_admin(client)
    service = await make(client)
    room = await make_resource(client, name="Room 1")
    laser = await make_resource(client, kind="equipment", name="Laser Unit 1")
    await client.put(
        f"{SERVICES}/{service['id']}/requirements",
        json={"requirements": [{"kind": "space", "resource_id": room["id"]}]},
    )

    refused = await client.put(
        f"{SERVICES}/{service['id']}/requirements",
        json={
            "requirements": [
                {"kind": "equipment", "resource_id": laser["id"]},
                # The kind disagrees, and it is the second row: the first must not survive.
                {"kind": "space", "resource_id": laser["id"]},
            ]
        },
    )

    assert refused.status_code == 422, refused.text
    kept = (await client.get(SERVICES)).json()["services"][0]["requirements"]
    assert [(r["kind"], r["resource_id"]) for r in kept] == [("space", room["id"])]


async def test_a_requirement_survives_its_resource_being_deactivated(client):
    """No cascade, no silent drop: the row stays and the editing screen is told the resource
    is gone, because somebody has to decide whether to point it elsewhere or remove it."""
    await as_admin(client)
    service = await make(client)
    laser = await make_resource(client, kind="equipment", name="Laser Unit 1")
    await client.put(
        f"{SERVICES}/{service['id']}/requirements",
        json={"requirements": [{"kind": "equipment", "resource_id": laser["id"]}]},
    )

    await client.post(f"{RESOURCES}/{laser['id']}/deactivate", json={})

    listed = (await client.get(SERVICES)).json()["services"][0]
    assert listed["requirements"] == [
        {
            "kind": "equipment",
            "resource_id": laser["id"],
            "resource_name": "Laser Unit 1",
            "resource_active": False,
        }
    ]


async def test_the_same_requirement_twice_is_one_row(client):
    await as_admin(client)
    service = await make(client)

    resp = await client.put(
        f"{SERVICES}/{service['id']}/requirements",
        json={
            "requirements": [
                {"kind": "space", "resource_id": None},
                {"kind": "space", "resource_id": None},
            ]
        },
    )

    assert resp.status_code == 200, resp.text
    assert len(resp.json()["requirements"]) == 1


# --- deactivation ------------------------------------------------------------------------------


async def test_deactivate_and_reactivate_flip_the_flag_and_preserve_the_row(client):
    await as_admin(client)
    service = await make(client)

    deactivated = await client.post(f"{SERVICES}/{service['id']}/deactivate", json={})
    assert deactivated.status_code == 200, deactivated.text
    assert deactivated.json()["active"] is False

    async with session_scope() as db:
        kept = await db.execute(
            text("SELECT name, price_cents FROM services WHERE id = :i"), {"i": service["id"]}
        )
    assert kept.one() == ("Swedish Massage", 12000)

    reactivated = await client.post(f"{SERVICES}/{service['id']}/reactivate", json={})
    assert reactivated.status_code == 200, reactivated.text
    assert reactivated.json()["active"] is True


async def test_deactivating_twice_is_a_no_op(client):
    await as_admin(client)
    service = await make(client)
    await client.post(f"{SERVICES}/{service['id']}/deactivate", json={})

    resp = await client.post(f"{SERVICES}/{service['id']}/deactivate", json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["active"] is False


# --- listing -------------------------------------------------------------------------------------


async def test_listing_hides_inactive_unless_asked(client):
    await as_admin(client)
    kept = await make(client, name="Swedish Massage")
    gone = await make(client, name="Deep Tissue")
    await client.post(f"{SERVICES}/{gone['id']}/deactivate", json={})

    live = (await client.get(SERVICES)).json()["services"]
    everything = (await client.get(f"{SERVICES}?include_inactive=true")).json()["services"]

    assert [s["id"] for s in live] == [kept["id"]]
    assert {s["id"] for s in everything} == {kept["id"], gone["id"]}


async def test_listing_carries_the_sets(client):
    await as_admin(client)
    service = await make(client)
    ana = await make_staff(client, "ana@cedar.example", "Ana")
    room = await make_resource(client, name="Room 1")
    await client.put(f"{SERVICES}/{service['id']}/staff", json={"staff_ids": [ana["id"]]})
    await client.put(
        f"{SERVICES}/{service['id']}/requirements",
        json={"requirements": [{"kind": "space", "resource_id": room["id"]}]},
    )

    listed = (await client.get(SERVICES)).json()["services"]

    assert listed[0]["staff_ids"] == [ana["id"]]
    assert listed[0]["requirements"][0]["resource_name"] == "Room 1"


# --- the read side the engine consumes -------------------------------------------------------


async def test_the_catalog_read_endpoint_is_the_shape_the_engine_needs(client):
    await as_admin(client)
    service = await make(client)
    ana = await make_staff(client, "ana@cedar.example", "Ana")
    await make_resource(client, name="Room 1")
    laser = await make_resource(client, kind="equipment", name="Laser Unit 1")
    await client.put(f"{SERVICES}/{service['id']}/staff", json={"staff_ids": [ana["id"]]})
    await client.put(
        f"{SERVICES}/{service['id']}/requirements",
        json={
            "requirements": [
                {"kind": "space", "resource_id": None},
                {"kind": "equipment", "resource_id": laser["id"]},
            ]
        },
    )

    resp = await client.get(CATALOG)

    assert resp.status_code == 200, resp.text
    assert resp.json()["services"] == [
        {
            "id": service["id"],
            "name": "Swedish Massage",
            "description": "Sixty minutes, full body.",
            "duration_minutes": 60,
            "buffer_before_minutes": 5,
            "buffer_after_minutes": 15,
            "price_cents": 12000,
            "bookable_online": True,
            "sort_order": 0,
            "staff_ids": [ana["id"]],
            "bookable": True,
            "unbookable_reasons": [],
            "requirements": [
                {
                    "kind": "space",
                    "resource_id": None,
                    "resource_name": None,
                    "resource_active": None,
                },
                {
                    "kind": "equipment",
                    "resource_id": laser["id"],
                    "resource_name": "Laser Unit 1",
                    "resource_active": True,
                },
            ],
        }
    ]


async def test_a_departed_practitioner_is_not_offered_to_the_engine(client):
    """The one thing `staff_ids` means to Task 14 is "whose day to look at".

    A practitioner who has left is not somebody a slot can be offered against, and a caller
    that had to know to filter would be a caller that eventually forgot. The admin endpoint
    still shows the row, because that is the screen where somebody removes it.
    """
    await as_admin(client)
    service = await make(client)
    ana = await make_staff(client, "ana@cedar.example", "Ana")
    bo = await make_staff(client, "bo@cedar.example", "Bo")
    await client.put(f"{SERVICES}/{service['id']}/staff", json={"staff_ids": [ana["id"], bo["id"]]})
    await client.post(f"/api/admin/staff/{ana['id']}/deactivate", json={})

    catalog = (await client.get(CATALOG)).json()["services"][0]
    admin = (await client.get(SERVICES)).json()["services"][0]

    assert catalog["staff_ids"] == [bo["id"]]
    assert catalog["bookable"] is True
    assert set(admin["staff_ids"]) == {ana["id"], bo["id"]}


async def test_a_service_nobody_active_can_deliver_is_unbookable(client):
    await as_admin(client)
    service = await make(client)
    ana = await make_staff(client, "ana@cedar.example", "Ana")
    await client.put(f"{SERVICES}/{service['id']}/staff", json={"staff_ids": [ana["id"]]})
    await client.post(f"/api/admin/staff/{ana['id']}/deactivate", json={})

    listed = (await client.get(CATALOG)).json()["services"][0]

    assert listed["staff_ids"] == []
    assert listed["bookable"] is False
    assert listed["unbookable_reasons"] == ["Nobody active can deliver this."]


async def test_a_requirement_on_a_deactivated_resource_makes_the_service_unbookable(client):
    """Never unconstrained. Dropping the dead requirement would let the engine offer a laser
    treatment with no laser, which is the one failure this flag exists to prevent."""
    await as_admin(client)
    service = await make(client)
    ana = await make_staff(client, "ana@cedar.example", "Ana")
    laser = await make_resource(client, kind="equipment", name="Laser Unit 1")
    await client.put(f"{SERVICES}/{service['id']}/staff", json={"staff_ids": [ana["id"]]})
    await client.put(
        f"{SERVICES}/{service['id']}/requirements",
        json={"requirements": [{"kind": "equipment", "resource_id": laser["id"]}]},
    )
    await client.post(f"{RESOURCES}/{laser['id']}/deactivate", json={})

    listed = (await client.get(CATALOG)).json()["services"][0]

    assert listed["bookable"] is False
    assert listed["unbookable_reasons"] == ["“Laser Unit 1” is deactivated."]
    # Still reported, flagged rather than hidden.
    assert listed["requirements"] == [
        {
            "kind": "equipment",
            "resource_id": laser["id"],
            "resource_name": "Laser Unit 1",
            "resource_active": False,
        }
    ]


async def test_an_any_requirement_with_no_active_resource_of_that_kind_is_unbookable(client):
    await as_admin(client)
    service = await make(client)
    ana = await make_staff(client, "ana@cedar.example", "Ana")
    room = await make_resource(client, name="Room 1")
    await client.put(f"{SERVICES}/{service['id']}/staff", json={"staff_ids": [ana["id"]]})
    await client.put(
        f"{SERVICES}/{service['id']}/requirements",
        json={"requirements": [{"kind": "space", "resource_id": None}]},
    )
    assert (await client.get(CATALOG)).json()["services"][0]["bookable"] is True

    await client.post(f"{RESOURCES}/{room['id']}/deactivate", json={})

    listed = (await client.get(CATALOG)).json()["services"][0]
    assert listed["bookable"] is False
    assert listed["unbookable_reasons"] == ["There is no active space."]


async def test_every_reason_a_service_cannot_be_booked_is_reported_at_once(client):
    """One at a time would mean fixing one thing, reloading, and finding another."""
    await as_admin(client)
    service = await make(client)
    laser = await make_resource(client, kind="equipment", name="Laser Unit 1")
    await client.put(
        f"{SERVICES}/{service['id']}/requirements",
        json={
            "requirements": [
                {"kind": "space", "resource_id": None},
                {"kind": "equipment", "resource_id": laser["id"]},
            ]
        },
    )
    await client.post(f"{RESOURCES}/{laser['id']}/deactivate", json={})

    listed = (await client.get(CATALOG)).json()["services"][0]

    assert listed["bookable"] is False
    assert listed["unbookable_reasons"] == [
        "Nobody active can deliver this.",
        "There is no active space.",
        "“Laser Unit 1” is deactivated.",
    ]


async def test_the_catalog_read_endpoint_leaves_out_inactive_services(client):
    await as_admin(client)
    kept = await make(client, name="Swedish Massage")
    gone = await make(client, name="Deep Tissue")
    await client.post(f"{SERVICES}/{gone['id']}/deactivate", json={})

    listed = (await client.get(CATALOG)).json()["services"]

    assert [s["id"] for s in listed] == [kept["id"]]


async def test_somebody_who_only_books_may_read_the_catalog(client):
    from core.security import hash_password
    from tests.conftest import add_account

    await as_admin(client)
    await make(client)
    booker_password = "correct horse battery 2"
    await add_account("front@cedar.example", await hash_password(booker_password))
    client.cookies.clear()
    login = await client.post(
        "/api/auth/login", json={"email": "front@cedar.example", "password": booker_password}
    )
    assert login.status_code == 200  # Staff Mode, and no `catalog.manage`

    resp = await client.get(CATALOG)

    assert resp.status_code == 200, resp.text
    assert [s["name"] for s in resp.json()["services"]] == ["Swedish Massage"]


async def test_the_catalog_read_endpoint_refuses_a_stranger(client):
    resp = await client.get(CATALOG)

    assert resp.status_code == 401, resp.text


# --- who may do any of this --------------------------------------------------------------------


async def test_every_service_endpoint_needs_catalog_manage(client):
    from core.security import hash_password
    from tests.conftest import add_account

    await as_admin(client)
    service = await make(client)
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
        await client.get(SERVICES),
        await client.post(SERVICES, json=draft(name="nope")),
        await client.patch(f"{SERVICES}/{service['id']}", json={"sort_order": 3}),
        await client.put(f"{SERVICES}/{service['id']}/staff", json={"staff_ids": []}),
        await client.put(f"{SERVICES}/{service['id']}/requirements", json={"requirements": []}),
        await client.post(f"{SERVICES}/{service['id']}/deactivate", json={}),
        await client.post(f"{SERVICES}/{service['id']}/reactivate", json={}),
    ]

    assert [r.status_code for r in refusals] == [403] * 7
    assert {r.json()["code"] for r in refusals} == {"capability_required"}


async def test_services_are_refused_outside_admin_mode(client):
    await as_admin(client)
    service = await make(client)
    client.cookies.clear()
    login = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200  # Staff Mode

    refusals = [
        await client.get(SERVICES),
        await client.post(SERVICES, json=draft(name="nope")),
        await client.patch(f"{SERVICES}/{service['id']}", json={"sort_order": 3}),
        await client.put(f"{SERVICES}/{service['id']}/staff", json={"staff_ids": []}),
        await client.put(f"{SERVICES}/{service['id']}/requirements", json={"requirements": []}),
        await client.post(f"{SERVICES}/{service['id']}/deactivate", json={}),
        await client.post(f"{SERVICES}/{service['id']}/reactivate", json={}),
    ]

    assert [r.status_code for r in refusals] == [403] * 7
    assert {r.json()["code"] for r in refusals} == {"admin_mode_required"}


# --- the trail ---------------------------------------------------------------------------------


async def test_the_trail_records_the_whole_life_of_a_service(client):
    await as_admin(client)
    service = await make(client)
    ana = await make_staff(client, "ana@cedar.example", "Ana")
    await client.patch(f"{SERVICES}/{service['id']}", json={"sort_order": 2})
    await client.put(f"{SERVICES}/{service['id']}/staff", json={"staff_ids": [ana["id"]]})
    await client.put(
        f"{SERVICES}/{service['id']}/requirements",
        json={"requirements": [{"kind": "space", "resource_id": None}]},
    )
    await client.post(f"{SERVICES}/{service['id']}/deactivate", json={})
    await client.post(f"{SERVICES}/{service['id']}/reactivate", json={})

    recorded = await events()

    for event in (
        "service.created",
        "service.updated",
        "service.staff_replaced",
        "service.requirements_replaced",
        "service.deactivated",
        "service.reactivated",
    ):
        assert event in recorded, recorded


# --- the model and the database describe the same schema ---------------------------------------

CATALOG_TABLES = ("services", "service_staff", "service_requirements")


def _ours(names: set[str]) -> set[str]:
    return {name for name in names if not name.endswith("_pkey")}


async def test_the_models_declare_every_constraint_the_database_actually_has(client):
    """The same drift check `test_hours.py` runs, over this ticket's three tables.

    A CHECK that exists only in migration 0012 is invisible to SQLAlchemy's model of the
    schema: the next `alembic revision --autogenerate` would helpfully emit a DROP for it,
    and any `create_all` path would build `services` with no five-minute rule at all.
    """
    from core.db import Base

    async with session_scope() as db:
        for table in CATALOG_TABLES:
            constraints = set(
                await db.scalars(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = cast(:t AS regclass) AND contype IN ('c', 'u', 'x')"
                    ),
                    {"t": table},
                )
            )
            indexes = set(
                await db.scalars(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = :t"), {"t": table}
                )
            )
            database = _ours(constraints | indexes)

            metadata = Base.metadata.tables[table]
            declared = _ours(
                {c.name for c in metadata.constraints if c.name}
                | {i.name for i in metadata.indexes if i.name}
            )

            assert database == declared, (
                f"{table}: only in the database {sorted(database - declared)}, "
                f"only in the model {sorted(declared - database)}"
            )

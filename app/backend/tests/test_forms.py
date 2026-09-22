"""S1/S5: form templates — drafts, numbered frozen versions, retire and delete (#45).

A template is a stable identity with one editable draft; publishing copies the draft into
`form_template_versions` as the next number, and that row is never rewritten — the database
refuses it (0027), not just this API. Field keys are the builder's UUIDs and survive
rewording, so v2's reworded field is still v1's field.
"""

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from core.db import get_purge_engine, session_scope

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"
SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": EMAIL,
    "admin_password": PASSWORD,
}
FORMS = "/api/admin/forms"

PREGNANT = "11111111-1111-4111-8111-111111111111"
WEEKS = "22222222-2222-4222-8222-222222222222"
SIGNATURE = "33333333-3333-4333-8333-333333333333"


def schema(label: str = "Are you pregnant?") -> dict:
    return {
        "fields": [
            {"key": PREGNANT, "type": "yes_no", "label": label, "required": True},
            {
                "key": WEEKS,
                "type": "short_text",
                "label": "How many weeks?",
                "required": True,
                "show_if": {"key": PREGNANT, "equals": ["yes"]},
            },
            {"key": SIGNATURE, "type": "signature", "label": "Signature", "required": True},
        ]
    }


async def _wipe_forms() -> None:
    """Versions are append-only for both runtime roles; only the owner may clear them."""
    owner = create_async_engine(os.environ["DATABASE_URL_MIGRATE"])
    try:
        async with owner.begin() as conn:
            await conn.execute(text("DELETE FROM form_links"))  # they pin a version
            await conn.execute(text("DELETE FROM form_template_versions"))
            await conn.execute(text("DELETE FROM form_templates"))
    finally:
        await owner.dispose()


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    await _wipe_forms()
    async with get_purge_engine().begin() as purge:
        await purge.execute(text("DELETE FROM audit_events"))
    async with session_scope() as db:
        for table in ("staff", "password_reset_tokens", "users", "businesses", "setup_token"):
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
    # Other suites delete `users`, which a version's publisher row would otherwise pin.
    await _wipe_forms()


async def as_admin(client, email=EMAIL, password=PASSWORD):
    login = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    resp = await client.post("/api/auth/mode", json={"mode": "admin", "password": password})
    assert resp.status_code == 200, resp.text


async def make(client, **overrides) -> dict:
    body = {"name": "Prenatal intake", "kind": "intake", **overrides}
    resp = await client.post(FORMS, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def save(client, template: dict, **overrides) -> dict:
    body = {
        "name": template["name"],
        "kind": template["kind"],
        "schema": schema(),
        "is_health_form": True,
        "is_mandatory": False,
        **overrides,
    }
    resp = await client.put(f"{FORMS}/{template['id']}/draft", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def publish(client, template_id: str, requires_resignature: bool = False):
    return await client.post(
        f"{FORMS}/{template_id}/publish", json={"requires_resignature": requires_resignature}
    )


async def delete(client, template_id: str):
    # Every mutating request is JSON (main.py's CSRF guard), DELETE included.
    return await client.delete(
        f"{FORMS}/{template_id}", headers={"Content-Type": "application/json"}
    )


async def audit() -> list[tuple]:
    async with session_scope() as db:
        return list(
            (
                await db.execute(
                    text(
                        "SELECT event_type, target_id, metadata FROM audit_events "
                        "WHERE event_type LIKE 'form_template.%' ORDER BY occurred_at, id"
                    )
                )
            ).all()
        )


async def stored_schema(template_id: str, number: int) -> str:
    async with session_scope() as db:
        return await db.scalar(
            text(
                "SELECT schema::text FROM form_template_versions "
                "WHERE template_id = :t AND number = :n"
            ),
            {"t": template_id, "n": number},
        )


# --- building and publishing -----------------------------------------------------------------


async def test_a_new_template_starts_with_an_empty_draft_and_no_version(client):
    await as_admin(client)
    created = await make(client, is_health_form=True, is_mandatory=True)

    assert created["draft"] == {
        "schema": {"fields": []},
        "is_health_form": True,
        "is_mandatory": True,
    }
    assert created["latest_version"] is None
    assert created["retired_at"] is None
    listed = (await client.get(FORMS)).json()["templates"]
    assert [t["id"] for t in listed] == [created["id"]]


async def test_rewording_keeps_the_key_and_leaves_v1_byte_identical(client):
    await as_admin(client)
    template = await make(client)
    await save(client, template)
    first = await publish(client, template["id"])
    assert first.status_code == 201, first.text
    assert first.json()["number"] == 1
    before = await stored_schema(template["id"], 1)

    await save(client, template, schema=schema(label="Is there any chance you are pregnant?"))
    second = await publish(client, template["id"], requires_resignature=True)
    assert second.status_code == 201, second.text

    v2 = (await client.get(f"{FORMS}/{template['id']}/versions/2")).json()
    assert v2["number"] == 2
    assert v2["requires_resignature"] is True
    assert v2["is_health_form"] is True
    reworded = next(f for f in v2["schema"]["fields"] if f["key"] == PREGNANT)
    assert reworded["label"] == "Is there any chance you are pregnant?"
    assert await stored_schema(template["id"], 1) == before
    v1 = (await client.get(f"{FORMS}/{template['id']}/versions/1")).json()
    assert v1["schema"]["fields"][0]["label"] == "Are you pregnant?"

    history = (await client.get(f"{FORMS}/{template['id']}/versions")).json()["versions"]
    assert [v["number"] for v in history] == [2, 1]


async def test_publishing_an_unchanged_draft_is_refused(client):
    await as_admin(client)
    template = await make(client)
    await save(client, template)
    assert (await publish(client, template["id"])).status_code == 201

    again = await publish(client, template["id"])
    assert again.status_code == 409
    assert again.json()["code"] == "draft_unchanged"

    # A flag is part of the version, so flipping one is a change worth publishing.
    await save(client, template, is_mandatory=True)
    assert (await publish(client, template["id"])).status_code == 201


async def test_a_rename_alone_is_a_new_version_and_the_old_one_keeps_its_title(client):
    """The title is part of what a client signed: it lives on the version, not the template."""
    await as_admin(client)
    template = await make(client)
    await save(client, template)
    assert (await publish(client, template["id"])).status_code == 201

    await save(client, {**template, "name": "Pregnancy intake", "kind": "consent"})
    (row,) = (await client.get(FORMS)).json()["templates"]
    assert row["has_unpublished_changes"] is True
    second = await publish(client, template["id"])
    assert second.status_code == 201, second.text

    v1 = (await client.get(f"{FORMS}/{template['id']}/versions/1")).json()
    v2 = (await client.get(f"{FORMS}/{template['id']}/versions/2")).json()
    assert (v1["name"], v1["kind"]) == ("Prenatal intake", "intake")
    assert (v2["name"], v2["kind"]) == ("Pregnancy intake", "consent")
    history = (await client.get(f"{FORMS}/{template['id']}/versions")).json()["versions"]
    assert [v["name"] for v in history] == ["Pregnancy intake", "Prenatal intake"]


async def test_the_first_version_cannot_require_re_signature(client):
    await as_admin(client)
    template = await make(client)
    await save(client, template)
    resp = await publish(client, template["id"], requires_resignature=True)
    assert resp.status_code == 422
    assert resp.json()["code"] == "nothing_to_resign"


async def test_a_consent_cannot_make_an_acknowledgement_conditional(client):
    await as_admin(client)
    template = await make(client)
    conditional = schema()
    conditional["fields"][1]["type"] = "acknowledgement"
    conditional["fields"][1]["label"] = "I accept the risks."
    # Fine on an intake form ...
    await save(client, template, schema=conditional)
    # ... refused once the same draft is a consent.
    resp = await client.put(
        f"{FORMS}/{template['id']}/draft",
        json={"name": "Prenatal intake", "kind": "consent", "schema": conditional},
    )
    assert resp.status_code == 422, resp.text


async def test_an_empty_draft_cannot_be_published(client):
    await as_admin(client)
    template = await make(client)
    resp = await publish(client, template["id"])
    assert resp.status_code == 422
    assert resp.json()["code"] == "draft_empty"


async def test_the_list_says_whether_the_draft_differs_from_the_latest_version(client):
    await as_admin(client)
    template = await make(client)
    await save(client, template)
    await publish(client, template["id"])
    (row,) = (await client.get(FORMS)).json()["templates"]
    assert row["latest_version"] == 1
    assert row["has_unpublished_changes"] is False

    await save(client, template, schema=schema(label="Pregnant?"))
    (row,) = (await client.get(FORMS)).json()["templates"]
    assert row["has_unpublished_changes"] is True


async def test_an_invalid_draft_is_refused_and_the_old_draft_kept(client):
    await as_admin(client)
    template = await make(client)
    await save(client, template)
    two_signatures = schema()
    two_signatures["fields"].append(
        {"key": str(uuid.uuid4()), "type": "signature", "label": "Again", "required": True}
    )
    resp = await client.put(
        f"{FORMS}/{template['id']}/draft",
        json={"name": "X", "kind": "intake", "schema": two_signatures},
    )
    assert resp.status_code == 422
    kept = (await client.get(f"{FORMS}/{template['id']}")).json()
    assert kept["name"] == "Prenatal intake"
    assert len(kept["draft"]["schema"]["fields"]) == 3


async def test_a_published_key_cannot_change_its_type(client):
    """Same key, same question: a key re-used as another type would make v1's answers and
    v2's answers look comparable when they are not."""
    await as_admin(client)
    template = await make(client)
    await save(client, template)
    await publish(client, template["id"])
    # Dropped in v2 ...
    changed = schema()
    changed["fields"][1].pop("show_if")
    changed["fields"] = changed["fields"][1:]
    await save(client, template, schema=changed)
    assert (await publish(client, template["id"])).status_code == 201
    # ... and brought back as another type in the next draft.
    changed = schema()
    changed["fields"][0]["type"] = "short_text"
    changed["fields"][1].pop("show_if")
    resp = await client.put(
        f"{FORMS}/{template['id']}/draft",
        json={"name": "Prenatal intake", "kind": "intake", "schema": changed},
    )
    assert resp.status_code == 422, resp.text


# --- retire and delete ------------------------------------------------------------------------


async def test_a_published_template_is_retired_not_deleted(client):
    await as_admin(client)
    template = await make(client)
    await save(client, template)
    await publish(client, template["id"])

    refused = await delete(client, template["id"])
    assert refused.status_code == 409
    assert refused.json()["code"] == "template_published"

    retired = await client.post(f"{FORMS}/{template['id']}/retire", json={})
    assert retired.status_code == 200, retired.text
    (row,) = (await client.get(FORMS)).json()["templates"]
    assert row["retired_at"] is not None
    # Its versions are history and stay readable.
    assert (await client.get(f"{FORMS}/{template['id']}/versions/1")).status_code == 200


async def test_a_retired_template_takes_no_new_draft_or_version(client):
    await as_admin(client)
    template = await make(client)
    await save(client, template)
    await publish(client, template["id"])
    await client.post(f"{FORMS}/{template['id']}/retire", json={})

    draft = await client.put(
        f"{FORMS}/{template['id']}/draft",
        json={"name": "X", "kind": "intake", "schema": schema("Pregnant?")},
    )
    assert draft.status_code == 409
    assert draft.json()["code"] == "template_retired"
    again = await publish(client, template["id"])
    assert again.status_code == 409
    assert again.json()["code"] == "template_retired"


async def test_a_never_published_template_can_be_deleted(client):
    await as_admin(client)
    template = await make(client)
    await save(client, template)
    resp = await delete(client, template["id"])
    assert resp.status_code == 204
    assert (await client.get(FORMS)).json()["templates"] == []
    assert (await client.get(f"{FORMS}/{template['id']}")).status_code == 404


# --- the trail --------------------------------------------------------------------------------


async def test_publish_records_one_event_with_the_template_id_and_number_only(client):
    await as_admin(client)
    template = await make(client)
    await save(client, template)
    await publish(client, template["id"])

    published = [row for row in await audit() if row[0] == "form_template.published"]
    assert published == [("form_template.published", template["id"], {"number": 1})]


async def test_every_mutation_is_on_the_trail(client):
    await as_admin(client)
    kept = await make(client)
    await save(client, kept)
    await publish(client, kept["id"])
    await client.post(f"{FORMS}/{kept['id']}/retire", json={})
    dropped = await make(client, name="Scratch")
    await delete(client, dropped["id"])

    assert [row[0] for row in await audit()] == [
        "form_template.created",
        "form_template.draft_saved",
        "form_template.published",
        "form_template.retired",
        "form_template.created",
        "form_template.deleted",
    ]
    # Identifiers and numbers only: no label, no name, no schema.
    assert all(set(row[2]) <= {"number"} for row in await audit())


# --- who may do any of this -------------------------------------------------------------------


async def _every_route(client, template_id: str) -> list:
    return [
        await client.get(FORMS),
        await client.post(FORMS, json={"name": "nope", "kind": "other"}),
        await client.get(f"{FORMS}/{template_id}"),
        await client.put(
            f"{FORMS}/{template_id}/draft",
            json={"name": "nope", "kind": "other", "schema": {"fields": []}},
        ),
        await client.post(f"{FORMS}/{template_id}/publish", json={"requires_resignature": False}),
        await client.get(f"{FORMS}/{template_id}/versions"),
        await client.get(f"{FORMS}/{template_id}/versions/1"),
        await client.post(f"{FORMS}/{template_id}/retire", json={}),
        await delete(client, template_id),
    ]


async def test_every_route_needs_forms_manage(client):
    from core.security import hash_password
    from tests.conftest import add_account

    await as_admin(client)
    template = await make(client)
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

    refusals = await _every_route(client, template["id"])
    assert [r.status_code for r in refusals] == [403] * 9
    assert {r.json()["code"] for r in refusals} == {"capability_required"}


async def test_every_route_is_refused_in_staff_mode(client):
    await as_admin(client)
    template = await make(client)
    client.cookies.clear()
    login = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200  # Staff Mode

    refusals = await _every_route(client, template["id"])
    assert [r.status_code for r in refusals] == [403] * 9
    assert {r.json()["code"] for r in refusals} == {"admin_mode_required"}


# --- S5: a published version is frozen by the database ----------------------------------------


async def test_the_app_role_holds_only_select_and_insert_on_versions(database):
    async with session_scope() as db:
        held = {
            p
            for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
            if await db.scalar(
                text("SELECT has_table_privilege('linsuite_app', 'form_template_versions', :p)"),
                {"p": p},
            )
        }
    assert held == {"SELECT", "INSERT"}


async def _published(client) -> str:
    await as_admin(client)
    template = await make(client)
    await save(client, template)
    assert (await publish(client, template["id"])).status_code == 201
    return template["id"]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE form_template_versions SET schema = '{\"fields\": []}'::jsonb",
        "UPDATE form_template_versions SET name = 'Renamed'",
        "DELETE FROM form_template_versions",
    ],
)
async def test_the_trigger_refuses_the_app_role_even_with_the_grant_restored(client, statement):
    await _published(client)
    owner = create_async_engine(os.environ["DATABASE_URL_MIGRATE"])
    try:
        async with owner.connect() as conn:
            await conn.begin()
            try:
                await conn.execute(
                    text("GRANT UPDATE, DELETE ON form_template_versions TO linsuite_app")
                )
                await conn.execute(text("SET ROLE linsuite_app"))
                with pytest.raises(DBAPIError) as refused:
                    await conn.execute(text(statement))
                assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value
                assert "append-only" in str(refused.value)
            finally:
                await conn.rollback()
    finally:
        await owner.dispose()


async def test_the_purge_role_cannot_delete_a_version(client):
    template_id = await _published(client)
    with pytest.raises(DBAPIError) as refused:
        async with get_purge_engine().begin() as purge:
            await purge.execute(text("DELETE FROM form_template_versions"))
    assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value
    assert await stored_schema(template_id, 1) is not None

"""S1 session-note workflow, with real PostgreSQL; S5 guards are tested separately."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.db import session_scope
from tests.test_appointments import (  # noqa: F401 — real business fixture
    as_admin,
    at,
    book,
    claimed_instance,
    make_customer,
    make_service,
    me_staff_id,
    put_hours,
)

TEMPLATES = "/api/note-templates"
SOAP = {
    "name": "SOAP",
    "fields": [
        {"key": "subjective", "label": "Subjective", "required": True},
        {"key": "objective", "label": "Objective", "required": False},
    ],
    "diagram_ids": ["body_front", "body_back", "layout"],
    "active": True,
}


async def appointment_and_template(client):
    await as_admin(client)
    staff = await me_staff_id(client)
    await put_hours(client, staff, [(0, 540, 1020)])
    service = await make_service(client, [staff])
    customer = await make_customer(client)
    visit = await book(client, service, staff, at("10:00"), customer_id=customer)
    assert visit.status_code == 201, visit.text
    template = await client.post(TEMPLATES, json=SOAP)
    assert template.status_code == 201, template.text
    return customer, visit.json()["id"], template.json()["id"]


def contents():
    return {
        "answers": {"subjective": "Left shoulder feels stiff"},
        "annotations": [
            {
                "id": str(uuid.uuid4()),
                "kind": "pin",
                "diagram_id": "body_back",
                "x": 0.3,
                "y": 0.24,
                "colour": "#DC2626",
                "text": "Tender point",
                "timestamp": "2026-09-27T14:32:00Z",
            },
            {
                "id": str(uuid.uuid4()),
                "kind": "zone",
                "diagram_id": "layout",
                "x": 0.1,
                "y": 0.2,
                "width": 0.4,
                "height": 0.25,
                "colour": "#2563EB",
                "text": "Work area",
                "timestamp": "2026-09-27T14:33:00Z",
            },
        ],
    }


@pytest.mark.asyncio
async def test_practitioner_authors_and_opens_an_appointment_note(client):
    customer, appointment, template = await appointment_and_template(client)
    path = f"/api/customers/{customer}/session-notes"
    payload = contents()
    created = await client.post(
        path,
        json={"appointment_id": appointment, "template_id": template, "template": SOAP, **payload},
    )
    assert created.status_code == 201, created.text
    metadata = created.json()
    assert "answers" not in metadata
    opened = await client.get(f"{path}/{metadata['id']}")
    assert opened.status_code == 200, opened.text
    assert opened.json()["answers"] == payload["answers"]
    for actual, expected in zip(opened.json()["annotations"], payload["annotations"], strict=True):
        assert {k: actual[k] for k in expected} == expected
    assert opened.json()["template"]["name"] == "SOAP"
    assert opened.headers["cache-control"] == "no-store"


async def draft(client, *, content=None):
    customer, appointment, template = await appointment_and_template(client)
    base = f"/api/customers/{customer}/session-notes"
    response = await client.post(
        base,
        json={
            "appointment_id": appointment,
            "template_id": template,
            "template": SOAP,
            **(content if content is not None else contents()),
        },
    )
    assert response.status_code == 201, response.text
    return customer, f"{base}/{response.json()['id']}", response.json()


async def test_draft_edits_are_versioned_and_lock_final_content(client):
    _, path, _ = await draft(client)
    edited = {"answers": {"subjective": "Improved range of motion"}, "annotations": []}
    saved = await client.put(path, json={"revision": 1, **edited})
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 2
    assert (await client.put(path, json={"revision": 1, **edited})).status_code == 409
    locked = await client.post(f"{path}/lock", json={"revision": 2})
    assert locked.status_code == 200, locked.text
    assert locked.json()["locked_at"] is not None
    assert locked.json()["can_edit"] is False
    assert (await client.put(path, json={"revision": 3, **edited})).status_code == 409
    assert (await client.get(path)).json()["answers"] == edited["answers"]


async def test_database_role_cannot_rewrite_or_unlock_a_locked_note(client):
    _, path, note = await draft(client)
    assert (await client.post(f"{path}/lock", json={"revision": 1})).status_code == 200
    with pytest.raises(DBAPIError):
        async with session_scope() as db:
            await db.execute(
                text(
                    "UPDATE session_notes SET locked_at = NULL, locked_by_user_id = NULL, "
                    "content_sealed = '\\x00'::bytea, revision = revision + 1 WHERE id = :id"
                ),
                {"id": uuid.UUID(note["id"])},
            )
            await db.commit()
    assert (await client.get(path)).json()["locked_at"] is not None


async def test_template_changes_leave_existing_history_intact(client):
    customer, path, note = await draft(client)
    changed = await client.put(
        f"{TEMPLATES}/{note['template_id']}", json={**SOAP, "name": "Revised SOAP", "active": False}
    )
    assert changed.status_code == 200, changed.text
    history = await client.get(f"/api/customers/{customer}/session-notes")
    assert history.status_code == 200, history.text
    assert history.json()[0]["id"] == note["id"]
    assert "answers" not in history.json()[0]
    assert history.json()[0]["template"]["name"] == "SOAP"
    assert (await client.get(path)).json()["template"]["name"] == "SOAP"
    choices = await client.get(f"/api/customers/{customer}/session-note-appointments")
    assert choices.status_code == 200, choices.text
    assert note["appointment_id"] in [a["id"] for a in choices.json()]


async def test_note_opens_are_logged_and_entries_extend_the_retention_hold(client):
    from datetime import date, timedelta

    from tests.test_retention_api import set_dob, switch

    customer, appointment, template = await appointment_and_template(client)
    await set_dob(client, customer, date(1990, 5, 1))
    await switch(client, "regulated_health")
    path = f"/api/customers/{customer}/session-notes"
    before = (await client.get(f"/api/admin/customers/{customer}/access-log")).json()
    created = await client.post(
        path,
        json={
            "appointment_id": appointment,
            "template_id": template,
            "template": SOAP,
            **contents(),
        },
    )
    assert created.status_code == 201, created.text
    await client.get(path)  # Metadata doesn't disclose the content.
    await client.get(f"{path}/{created.json()['id']}")
    after = (await client.get(f"/api/admin/customers/{customer}/access-log")).json()
    assert len(after["entries"]) == len(before["entries"]) + 1
    assert after["entries"][0]["resource_type"] == "session_note"
    profile = (await client.get(f"/api/customers/{customer}")).json()
    assert profile["customer"]["retention"]["status"] == "held"
    assert date.fromisoformat(
        profile["customer"]["retention"]["expires_on"]
    ) >= date.today() + timedelta(days=3650)


async def test_unheld_notes_are_removed_before_their_key_is_shredded(client):
    from core.db import get_purge_engine
    from customers.tasks import _shred
    from tests.test_retention_api import switch

    customer, path, _ = await draft(client)
    await switch(client, "general_business")
    assert await _shred(get_purge_engine(), customer, None)
    assert (await client.get(path)).status_code == 404
    assert (await client.get(f"/api/customers/{customer}/session-notes")).json() == []


async def test_database_refuses_a_lock_without_its_actor(client):
    _, _, note = await draft(client)
    with pytest.raises(DBAPIError):
        async with session_scope() as db:
            await db.execute(
                text(
                    "UPDATE session_notes SET locked_at = now(), "
                    "revision = revision + 1 WHERE id = :id"
                ),
                {"id": uuid.UUID(note["id"])},
            )
            await db.commit()


async def test_only_the_appointment_practitioner_can_author_or_change_a_note(client):
    from tests.test_appointments import OTHER_PASSWORD, add_colleague, as_staff

    customer, path, note = await draft(client)
    await add_colleague(client, "rae@cedar.example", OTHER_PASSWORD)
    await as_staff(client, "rae@cedar.example", OTHER_PASSWORD)
    assert (await client.get(path)).json()["can_edit"] is False
    for method, url, body in (
        ("PUT", path, {"revision": 1, **contents()}),
        ("POST", f"{path}/lock", {"revision": 1}),
        (
            "POST",
            f"/api/customers/{customer}/session-notes",
            {
                "appointment_id": note["appointment_id"],
                "template_id": note["template_id"],
                "template": SOAP,
                **contents(),
            },
        ),
    ):
        response = await client.request(method, url, json=body)
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "capability_required"
    assert (await client.get(f"/api/customers/{customer}/session-note-appointments")).json() == []
    assert (await client.post(TEMPLATES, json=SOAP)).status_code == 403


@pytest.mark.parametrize(
    "change",
    [
        {"x": -0.1},
        {"x": 1.1},
        {"colour": "javascript:red"},
        {"timestamp": "2026-09-27T14:32:00"},
        {"diagram_id": "unknown"},
        {"kind": "text", "text": " "},
        {"kind": "zone", "width": 0.2},
        {"kind": "zone", "x": 0.9, "width": 0.2, "height": 0.1},
    ],
)
async def test_invalid_markup_cannot_replace_a_draft(client, change):
    _, path, _ = await draft(client)
    content = contents()
    content["annotations"][0].update(change)
    response = await client.put(path, json={"revision": 1, **content})
    assert response.status_code == 422, response.text
    assert (await client.get(path)).json()["revision"] == 1


async def test_incomplete_note_can_be_saved_but_cannot_be_locked(client):
    _, path, _ = await draft(client, content={"answers": {}, "annotations": []})
    response = await client.post(f"{path}/lock", json={"revision": 1})
    assert response.status_code == 422, response.text
    assert (await client.get(path)).json()["locked_at"] is None


async def test_edit_and_lock_race_cannot_overwrite_finalized_content(client):
    import asyncio

    _, path, _ = await draft(client)
    edit, lock = await asyncio.gather(
        client.put(
            path, json={"revision": 1, "answers": {"subjective": "New findings"}, "annotations": []}
        ),
        client.post(f"{path}/lock", json={"revision": 1}),
    )
    assert sorted([edit.status_code, lock.status_code]) == [200, 409]
    final = (await client.get(path)).json()
    assert final["revision"] == 2
    assert final["answers"]["subjective"] == (
        "New findings" if edit.status_code == 200 else "Left shoulder feels stiff"
    )


async def test_note_id_cannot_be_opened_under_another_clients_chart(client):
    _, path, note = await draft(client)
    other = await make_customer(client)
    assert (
        await client.get(f"/api/customers/{other}/session-notes/{note['id']}")
    ).status_code == 404
    assert (await client.get(path)).status_code == 200


async def test_erasure_keeps_held_notes_and_refuses_new_entries(client):
    from core.db import get_purge_engine
    from customers.tasks import _shred
    from tests.test_retention_api import switch

    customer, path, note = await draft(client)
    await switch(client, "regulated_health")
    cancelled = await client.post(f"/api/appointments/{note['appointment_id']}/cancel", json={})
    assert cancelled.status_code == 200, cancelled.text
    erased = await client.post(f"/api/customers/{customer}/erasure", json={})
    assert erased.status_code == 201, erased.text
    assert "Session notes" in erased.json()["retained"]
    assert await _shred(get_purge_engine(), customer, None) is False
    assert (await client.get(path)).json()["answers"]["subjective"] == "Left shoulder feels stiff"
    for method, url, body in (
        ("PUT", path, {"revision": 1, **contents()}),
        ("POST", f"{path}/lock", {"revision": 1}),
        (
            "POST",
            f"/api/customers/{customer}/session-notes",
            {
                "appointment_id": note["appointment_id"],
                "template_id": note["template_id"],
                "template": SOAP,
                **contents(),
            },
        ),
    ):
        response = await client.request(method, url, json=body)
        assert response.status_code == 409, response.text


async def test_changed_template_wording_cannot_receive_answers_to_the_old_wording(client):
    customer, appointment, template = await appointment_and_template(client)
    revised = {
        **SOAP,
        "fields": [{"key": "subjective", "label": "Different question", "required": True}],
    }
    assert (await client.put(f"{TEMPLATES}/{template}", json=revised)).status_code == 200
    path = f"/api/customers/{customer}/session-notes"
    response = await client.post(
        path,
        json={
            "appointment_id": appointment,
            "template_id": template,
            "template": SOAP,
            "answers": {"subjective": "Old question answer"},
            "annotations": [],
        },
    )
    assert response.status_code == 409, response.text
    assert (await client.get(path)).json() == []

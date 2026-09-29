"""S1/S2/S5: submitting a form — sealed, immutable, single use, and the retention hold (#47).

What these pin down:

- one public POST, one transaction: the link is consumed, the answers are sealed under the
  client's key (no answer text anywhere in the row), and for a *health* form the client's
  hold moves — all in the same commit, or none of it (ADR-0001 rule 7);
- a submission references the version it was filled against; republishing changes nothing;
- a retry with the same id is "already received", any other reuse is the uniform 404, and
  two concurrent submits of one link produce exactly one submission;
- signed = a drawn, non-blank PNG *and* a typed name, checked on the server;
- an erased client cannot submit, and nothing is ever recorded against their chart;
- the row is immutable for the app role (grant and trigger), the purge role deletes it only
  when the client is not held, and `_shred` removes it with the documents before the key;
- staff read of the answers is logged; the list of submissions is metadata and is not.
"""

import asyncio
import base64
import io
import logging
import os
import uuid
from datetime import UTC, date, datetime

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from core.config import get_settings
from core.db import get_purge_engine, session_scope
from customers import retention, tasks
from forms import submissions
from forms.models import FormSubmission
from tests.conftest import get_owner_engine
from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    CUSTOMERS,
    OTHER_PASSWORD,
    add_colleague,
    as_admin,
    as_staff,
    claimed_instance,
    make_customer,
)
from tests.test_document_keys import key_rows, sqlstate
from tests.test_documents import document_rows, key_destroyed_events, store
from tests.test_form_links import (  # noqa: F401 — the autouse fixture comes along
    BASE,
    INVALID,
    as_owner,
    forms_wiped,
    issue,
    published_template,
    token_of,
)
from tests.test_forms import PREGNANT, SIGNATURE, WEEKS, publish, save, schema
from tests.test_retention_api import set_dob, switch

SUBMIT = "/api/public/forms/submit"
DOB = date(1990, 5, 1)


# --- helpers ----------------------------------------------------------------------------------


def png(*, ink: bool = True, background: str | None = None, size=(600, 200)) -> str:
    """A signature as the pad sends it: a PNG data URL. `ink=False` is a blank pad."""
    image = Image.new("RGBA", size, background or (0, 0, 0, 0))
    if ink:
        ImageDraw.Draw(image).line([(40, 150), (200, 40), (380, 160), (560, 60)], "#111", 4)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


SIGNED = {"name": "Priya Nair", "image": png()}


def answers(**overrides) -> dict:
    return {PREGNANT: "yes", WEEKS: "twelve weeks along", SIGNATURE: SIGNED, **overrides}


async def submit(client, token: str, version_id: str, body_answers: dict, submission_id=None):
    return await client.post(
        SUBMIT,
        json={
            "token": token,
            "submission_id": str(submission_id or uuid.uuid4()),
            "version_id": version_id,
            "answers": body_answers,
        },
        headers={"Origin": BASE},
    )


async def sent_form(client, *, health: bool = True, dob: date | None = DOB, **draft):
    """Admin signed in; a published form; a client; one link. (customer_id, token, version_id,
    template)."""
    await as_admin(client)
    template = await published_template(client, is_health_form=health, **draft)
    customer_id = await make_customer(client)
    if dob:
        await set_dob(client, customer_id, dob)
    token = token_of((await issue(client, customer_id, template["id"])).json()["url"])
    version_id = await pinned_version(token)
    return customer_id, token, version_id, template


async def pinned_version(token: str) -> str:
    from forms.links import digest

    async with session_scope() as db:
        return str(
            await db.scalar(
                text("SELECT version_id FROM form_links WHERE token_sha256 = :d"),
                {"d": digest(token)},
            )
        )


async def rows(customer_id: str) -> list:
    async with session_scope() as db:
        return list(
            (
                await db.execute(
                    text(
                        "SELECT s.*, row_to_json(s)::text AS whole FROM form_submissions s "
                        "WHERE customer_id = :c ORDER BY submitted_at"
                    ),
                    {"c": customer_id},
                )
            ).all()
        )


async def hold(customer_id: str) -> tuple[datetime | None, datetime | None]:
    async with session_scope() as db:
        row = (
            await db.execute(
                text(
                    "SELECT last_clinical_entry_at, retention_expires_at FROM customers "
                    "WHERE id = :id"
                ),
                {"id": customer_id},
            )
        ).one()
    return row[0], row[1]


async def link_consumed(token: str) -> bool:
    from forms.links import digest

    async with session_scope() as db:
        return (
            await db.scalar(
                text("SELECT consumed_at FROM form_links WHERE token_sha256 = :d"),
                {"d": digest(token)},
            )
            is not None
        )


async def opened(submission_id: str) -> dict | None:
    async with session_scope() as db:
        submission = await db.get(FormSubmission, uuid.UUID(submission_id))
        return await submissions.open_answers(db, submission)


async def events(kind: str) -> list:
    async with get_purge_engine().connect() as purge:
        return list(
            (
                await purge.execute(
                    text(
                        "SELECT target_id, actor_user_id, metadata, row_to_json(a)::text AS whole "
                        "FROM audit_events a WHERE event_type = :k ORDER BY id"
                    ),
                    {"k": kind},
                )
            ).all()
        )


# --- S2: the signature ------------------------------------------------------------------------


def test_a_drawn_png_with_a_typed_name_is_a_signature():
    assert submissions.is_signed(SIGNED)
    # Ink on a white page, as a pad that paints its background would send it.
    assert submissions.is_signed({"name": "Priya Nair", "image": png(background="white")})


@pytest.mark.parametrize(
    "image",
    [
        png(ink=False),  # fully transparent: nothing was drawn
        png(ink=False, background="white"),  # a white page is not a signature either
        "not a data url",
        "data:image/jpeg;base64," + png().split(",", 1)[1],
        "data:image/png;base64,***",
        "data:image/png;base64," + base64.b64encode(b"GIF89a" + b"\0" * 64).decode(),
        "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 40).decode(),
        png(size=(5000, 100)),  # past the pixel cap: refused before it is decoded
        "data:image/png;base64," + "A" * (submissions.SIGNATURE_MAX_BYTES * 2),  # over the cap
    ],
    ids=[
        "transparent",
        "white",
        "not_data_url",
        "jpeg_prefix",
        "bad_base64",
        "gif_bytes",
        "truncated_png",
        "too_wide",
        "too_large",
    ],
)
def test_a_blank_undecodable_or_oversized_image_is_not_a_signature(image):
    assert not submissions.is_signed({"name": "Priya Nair", "image": image})


def _shape(draw) -> str:
    image = Image.new("RGBA", (600, 200), (0, 0, 0, 0))
    draw(ImageDraw.Draw(image))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


@pytest.mark.parametrize(
    ("image", "signed"),
    [
        (_shape(lambda d: d.rectangle([300, 100, 304, 103], fill="#111")), False),  # 5 x 4 blob
        (_shape(lambda d: d.rectangle([0, 0, 599, 199], fill="#000")), False),  # all black
        (_shape(lambda d: d.point((300, 100), fill="#000")), False),  # one dot
        (_shape(lambda d: None), False),  # blank
        (
            _shape(lambda d: d.line([(60, 140), (140, 60), (220, 150), (320, 70)], "#111", 3)),
            True,
        ),  # a realistic stroke
    ],
    ids=["blob_5x4", "all_black", "one_dot", "blank", "stroke"],
)
def test_ink_must_span_the_pad_without_filling_it(image, signed):
    assert submissions.is_signed({"name": "Priya Nair", "image": image}) is signed


@pytest.mark.parametrize("name", ["", "   ", "x" * (submissions.MAX_SIGNED_NAME + 1)])
def test_a_signature_needs_a_sane_typed_name(name):
    assert not submissions.is_signed({"name": name, "image": png()})


# --- S1: the submission, the link and the hold, in one commit --------------------------------


async def test_a_submission_is_sealed_consumes_the_link_and_extends_the_hold(client, monkeypatch):
    rendered: list[str] = []
    from forms import tasks as form_tasks

    monkeypatch.setattr(form_tasks.render_submission, "delay", rendered.append)
    customer_id, token, version_id, template = await sent_form(client)
    submission_id = str(uuid.uuid4())

    resp = await submit(client, token, version_id, answers(), submission_id)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "received"}
    [row] = await rows(customer_id)
    assert str(row.id) == submission_id
    assert (str(row.version_id), str(row.template_id)) == (version_id, template["id"])
    assert row.method == "link" and str(row.source_ip) == "127.0.0.1"
    sealed = bytes(row.answers_sealed)
    for plaintext in (b"twelve weeks", b"Priya Nair", SIGNED["image"][30:60].encode()):
        assert plaintext not in sealed
    assert "twelve weeks" not in row.whole and "Priya Nair" not in row.whole
    assert await link_consumed(token)
    # The hold moved in the same commit: read back after the one request.
    entry, expires = await hold(customer_id)
    assert entry == row.submitted_at
    assert expires is not None and expires.year >= entry.year + 10
    assert await opened(submission_id) == answers()
    assert rendered == [submission_id]


async def test_republishing_does_not_alter_an_existing_submission(client):
    customer_id, token, v1, template = await sent_form(client)
    submission_id = str(uuid.uuid4())
    assert (await submit(client, token, v1, answers(), submission_id)).status_code == 200
    async with session_scope() as db:
        v1_schema = await db.scalar(
            text("SELECT schema FROM form_template_versions WHERE id = :v"), {"v": v1}
        )

    await save(client, template, schema=schema("Could you be pregnant?"))
    assert (await publish(client, template["id"])).status_code == 201

    [row] = await rows(customer_id)
    assert str(row.version_id) == v1
    async with session_scope() as db:
        assert (
            await db.scalar(
                text("SELECT schema FROM form_template_versions WHERE id = :v"), {"v": v1}
            )
            == v1_schema
        )
    assert await opened(submission_id) == answers()
    # Staff see it with *its* version's wording, never the latest.
    shown = (await client.get(f"{CUSTOMERS}/{customer_id}/forms/{submission_id}")).json()
    assert shown["version"] == 1
    assert shown["fields"][0]["label"] == "Are you pregnant?"


async def test_a_retry_is_already_received_and_any_other_reuse_is_the_uniform_404(client):
    customer_id, token, version_id, template = await sent_form(client)
    submission_id = uuid.uuid4()
    assert (await submit(client, token, version_id, answers(), submission_id)).status_code == 200

    again = await submit(client, token, version_id, answers(), submission_id)
    assert again.status_code == 200 and again.json() == {"status": "already_received"}
    assert len(await rows(customer_id)) == 1
    assert len(await events("form.submitted")) == 1

    other = await submit(client, token, version_id, answers())
    assert other.status_code == 404 and other.json() == INVALID

    # A version other than the pinned one: 409, and the link is still usable.
    fresh = token_of((await issue(client, customer_id, template["id"])).json()["url"])
    wrong = await submit(client, fresh, str(uuid.uuid4()), answers())
    assert wrong.status_code == 409 and wrong.json()["code"] == "version_mismatch"
    assert not await link_consumed(fresh)
    assert len(await rows(customer_id)) == 1


async def test_a_submission_id_filed_on_another_link_is_409_and_changes_nothing(client, caplog):
    customer_id, token, version_id, template = await sent_form(client)
    submission_id = uuid.uuid4()
    assert (await submit(client, token, version_id, answers(), submission_id)).status_code == 200
    second = token_of((await issue(client, customer_id, template["id"])).json()["url"])
    before = await hold(customer_id)

    resp = await submit(client, second, version_id, answers(), submission_id)

    assert resp.status_code == 409 and resp.json()["code"] == "try_again"
    assert not await link_consumed(second)
    assert len(await rows(customer_id)) == 1
    assert len(await events("form.submitted")) == 1
    assert await hold(customer_id) == before
    assert "form_submissions_pkey" in caplog.text
    assert second not in caplog.text and "twelve weeks" not in caplog.text


def test_only_the_expected_constraints_read_as_try_again():
    from forms import public

    assert public._try_again("form_submissions_pkey")
    assert public._try_again("form_submissions_customer_id_fkey")
    assert not public._try_again("form_submissions_link_id_key")
    assert not public._try_again("ck_form_submissions_method")
    assert not public._try_again(None)


async def test_retiring_a_form_revokes_its_open_links_and_its_link_cannot_submit(client):
    customer_id, token, version_id, template = await sent_form(client)

    retire = await client.post(f"/api/admin/forms/{template['id']}/retire", json={})
    assert retire.status_code == 200, retire.text

    async with session_scope() as db:
        revoked = await db.scalar(
            text("SELECT count(*) FROM form_links WHERE revoked_at IS NOT NULL")
        )
    assert revoked == 1
    async with get_purge_engine().connect() as purge:
        metadata = await purge.scalar(
            text("SELECT metadata FROM audit_events WHERE event_type = 'form_template.retired'")
        )
    assert metadata == {"links_revoked": 1}
    resp = await submit(client, token, version_id, answers())
    assert resp.status_code == 404 and resp.json() == INVALID
    assert await rows(customer_id) == []


async def test_a_form_retired_after_the_lookup_is_refused_at_the_consume(client, monkeypatch):
    """Retired straight in the table (no revocation) between the unlocked lookup and the
    guarded UPDATE: the UPDATE's own WHERE refuses it."""
    from forms import public

    customer_id, token, version_id, template = await sent_form(client, health=False)
    real_lock = public._lock_the_chart

    async def lock_after_retiring(*args, **kwargs):
        await as_owner(
            "UPDATE form_templates SET retired_at = now() WHERE id = :id", id=template["id"]
        )
        return await real_lock(*args, **kwargs)

    monkeypatch.setattr(public, "_lock_the_chart", lock_after_retiring)

    resp = await submit(client, token, version_id, answers())

    assert resp.status_code == 404 and resp.json() == INVALID
    assert await rows(customer_id) == []
    assert not await link_consumed(token)


async def test_answers_are_validated_against_the_pinned_version(client):
    customer_id, token, version_id, _ = await sent_form(client)

    async def refused(body: dict) -> dict:
        resp = await submit(client, token, version_id, body)
        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "invalid_answers"
        return resp.json()["errors"]

    assert await refused({SIGNATURE: SIGNED}) == {PREGNANT: "required"}
    assert await refused(answers(**{WEEKS: ""})) == {WEEKS: "required"}
    assert await refused(answers(**{SIGNATURE: {"name": "Priya Nair"}})) == {SIGNATURE: "invalid"}
    assert await refused(answers(**{SIGNATURE: {"name": "", "image": png()}})) == {
        SIGNATURE: "invalid"
    }
    assert await refused(answers(**{SIGNATURE: {"name": "Priya", "image": png(ink=False)}})) == {
        SIGNATURE: "invalid"
    }
    assert await refused(answers(**{SIGNATURE: None})) == {SIGNATURE: "required"}
    assert not await link_consumed(token)
    assert await rows(customer_id) == []

    # Hidden by "no": not demanded, and a leftover answer to it is dropped, never stored.
    submission_id = str(uuid.uuid4())
    body = answers(**{PREGNANT: "no", WEEKS: "left over"})
    assert (await submit(client, token, version_id, body, submission_id)).status_code == 200
    assert await opened(submission_id) == {PREGNANT: "no", SIGNATURE: SIGNED}


async def test_a_health_form_extends_the_hold_and_a_consent_without_the_flag_does_not(client):
    customer_id, token, version_id, _ = await sent_form(client, health=False, kind="consent")

    assert (await submit(client, token, version_id, answers())).status_code == 200

    assert len(await rows(customer_id)) == 1
    assert await hold(customer_id) == (None, None)


async def test_a_general_business_submission_leaves_the_hold_null(client):
    await as_admin(client)
    await switch(client, "general_business")
    customer_id, token, version_id, _ = await sent_form(client)

    assert (await submit(client, token, version_id, answers())).status_code == 200

    entry, expires = await hold(customer_id)
    assert entry is not None and expires is None


async def test_a_client_erased_after_the_link_was_issued_cannot_submit(client):
    customer_id, token, version_id, template = await sent_form(client)
    assert (await client.post(f"{CUSTOMERS}/{customer_id}/erasure", json={})).status_code == 201

    resp = await submit(client, token, version_id, answers())

    assert resp.status_code == 404 and resp.json() == INVALID
    assert await rows(customer_id) == []


@pytest.mark.parametrize("health", [True, False], ids=["health", "not_health"])
async def test_a_suppressed_client_is_refused_even_before_the_revocation(client, health):
    """Suppressed but the link not (yet) revoked — the window an erasure's own transaction
    closes. Refused the same way, and nothing is recorded against the chart."""
    customer_id, token, version_id, _ = await sent_form(client, health=health)
    await as_owner("UPDATE customers SET suppressed_at = now() WHERE id = :id", id=customer_id)

    resp = await submit(client, token, version_id, answers())

    assert resp.status_code == 404 and resp.json() == INVALID
    assert await rows(customer_id) == []
    assert await hold(customer_id) == (None, None)
    assert not await link_consumed(token)


@pytest.mark.parametrize("health", [True, False], ids=["health", "not_health"])
async def test_an_erasure_committed_after_the_lookup_is_seen_under_the_lock(
    client, monkeypatch, health
):
    """The link lookup is unlocked; the suppression that matters is the one read under the
    client's row lock. Suppress the client between the two (as the owner, committed)."""
    from forms import public

    customer_id, token, version_id, _ = await sent_form(client, health=health)
    real_lock = public._lock_the_chart
    suppressed: list[bool] = []

    async def lock_after_suppressing(*args, **kwargs):
        await as_owner("UPDATE customers SET suppressed_at = now() WHERE id = :id", id=customer_id)
        suppressed.append(True)
        return await real_lock(*args, **kwargs)

    monkeypatch.setattr(public, "_lock_the_chart", lock_after_suppressing)

    resp = await submit(client, token, version_id, answers())

    assert suppressed == [True]
    assert resp.status_code == 404 and resp.json() == INVALID
    assert await rows(customer_id) == []
    assert await hold(customer_id) == (None, None)
    assert not await link_consumed(token)


async def test_record_clinical_entry_refuses_a_suppressed_client_and_changes_nothing(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    await set_dob(client, customer_id, DOB)
    await as_owner("UPDATE customers SET suppressed_at = now() WHERE id = :id", id=customer_id)

    async with session_scope() as db:
        with pytest.raises(retention.CustomerSuppressed):
            await retention.record_clinical_entry(db, uuid.UUID(customer_id), datetime.now(UTC))
        await db.commit()

    assert await hold(customer_id) == (None, None)


# --- the race: two submits of one link --------------------------------------------------------


async def _lock_waiters() -> int:
    owner = create_async_engine(get_settings().database_url_migrate)
    try:
        async with owner.connect() as conn:
            return await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE usename = 'linsuite_app' AND wait_event_type = 'Lock'"
                )
            )
    finally:
        await owner.dispose()


@pytest.mark.parametrize("health", [True, False], ids=["health", "not_health"])
async def test_two_concurrent_submits_of_one_link_make_exactly_one_submission(client, health):
    """The link row is held by the owner until both submits are queued behind it (on two real
    connections); then it is released and they race for the guarded UPDATE."""
    customer_id, token, version_id, _ = await sent_form(client, health=health)
    from forms.links import digest

    owner = create_async_engine(get_settings().database_url_migrate)
    try:
        async with owner.connect() as conn:
            await conn.begin()
            await conn.execute(
                text("SELECT 1 FROM form_links WHERE token_sha256 = :d FOR UPDATE"),
                {"d": digest(token)},
            )
            racers = [
                asyncio.create_task(submit(client, token, version_id, answers())) for _ in range(2)
            ]
            for _ in range(200):
                if await _lock_waiters() >= 2 or any(r.done() for r in racers):
                    break
                await asyncio.sleep(0.05)
            assert not any(r.done() for r in racers), "a submit did not wait for the link"
            await conn.rollback()
    finally:
        await owner.dispose()
    results = await asyncio.gather(*racers)

    assert sorted(r.status_code for r in results) == [200, 404]
    assert [r.json() for r in results if r.status_code == 404] == [INVALID]
    assert len(await rows(customer_id)) == 1
    assert len(await events("form.submitted")) == 1


# --- the public surface -----------------------------------------------------------------------


async def test_the_submit_needs_json_from_this_origin_and_logs_no_token_or_answer(client, caplog):
    customer_id, token, version_id, _ = await sent_form(client)
    body = {
        "token": token,
        "submission_id": str(uuid.uuid4()),
        "version_id": version_id,
        "answers": answers(),
    }
    assert (await client.post(SUBMIT, json=body)).status_code == 403
    elsewhere = await client.post(SUBMIT, json=body, headers={"Origin": "https://evil.example"})
    assert elsewhere.status_code == 403
    client.cookies.clear()

    caplog.set_level(logging.DEBUG)
    resp = await submit(client, token, version_id, answers(), body["submission_id"])

    assert resp.status_code == 200, resp.text
    assert "set-cookie" not in resp.headers
    assert resp.headers["cache-control"] == "no-store"
    for secret in (token, "twelve weeks", "Priya Nair"):
        assert secret not in caplog.text
        for record in caplog.records:
            assert secret not in record.getMessage() and secret not in str(record.args)
    [submitted] = await events("form.submitted")
    assert submitted.target_id == body["submission_id"] and submitted.actor_user_id is None
    [row] = await rows(customer_id)
    assert submitted.metadata == {
        "link_id": str(row.link_id),
        "template_id": str(row.template_id),
        "version": 1,
    }
    assert "twelve weeks" not in submitted.whole and token not in submitted.whole


# --- S5: immutable ----------------------------------------------------------------------------

REWRITES = [
    "UPDATE form_submissions SET method = 'scan' WHERE id = :id",
    "DELETE FROM form_submissions WHERE id = :id",
]


async def submitted(client, *, method="link", **kwargs) -> tuple[str, str]:
    customer_id, token, version_id, template = await sent_form(client, **kwargs)
    submission_id = str(uuid.uuid4())
    if method == "scan":
        from tests.test_form_scans import page

        resp = await client.post(
            f"/api/customers/{customer_id}/forms/scans",
            json={
                "submission_id": submission_id,
                "template_id": template["id"],
                "version_number": 1,
                "pages": [page()],
            },
        )
        assert resp.status_code == 201, resp.text
        return customer_id, submission_id
    resp = await submit(client, token, version_id, answers(), submission_id)
    assert resp.status_code == 200, resp.text
    return customer_id, submission_id


async def set_hold(customer_id: str, expires: str | None) -> None:
    await as_owner(
        "UPDATE customers SET retention_expires_at = cast(cast(:at AS text) AS timestamptz) "
        "WHERE id = :id",
        at=expires,
        id=customer_id,
    )


@pytest.mark.parametrize("method", ["link", "scan"])
@pytest.mark.parametrize("statement", REWRITES)
async def test_the_app_role_holds_no_grant_to_rewrite_or_delete_a_submission(
    client, statement, method
):
    customer_id, submission_id = await submitted(client, method=method)

    async with session_scope() as db:
        with pytest.raises(DBAPIError) as refused:
            await db.execute(text(statement), {"id": submission_id})
        await db.rollback()

    assert sqlstate(refused.value) == "42501"
    assert len(await rows(customer_id)) == 1


@pytest.mark.parametrize("method", ["link", "scan"])
@pytest.mark.parametrize("statement", REWRITES)
async def test_the_guard_refuses_the_app_role_even_with_the_grant_restored(
    client, statement, method
):
    customer_id, submission_id = await submitted(client, method=method)

    owner = create_async_engine(get_settings().database_url_migrate)
    try:
        async with owner.connect() as conn:
            await conn.begin()
            try:
                await conn.execute(text("GRANT UPDATE, DELETE ON form_submissions TO linsuite_app"))
                await conn.execute(text("SET ROLE linsuite_app"))
                with pytest.raises(DBAPIError) as refused:
                    await conn.execute(text(statement), {"id": submission_id})
            finally:
                await conn.rollback()
    finally:
        await owner.dispose()

    assert sqlstate(refused.value) == "42501"
    assert "form_submissions: " in str(refused.value)
    assert len(await rows(customer_id)) == 1


async def purge_delete(customer_id: str) -> int:
    async with get_purge_engine().begin() as purge:
        result = await purge.execute(
            text("DELETE FROM form_submissions WHERE customer_id = :id"), {"id": customer_id}
        )
    return result.rowcount


async def test_the_purge_role_deletes_a_submission_only_when_the_client_is_not_held(client):
    customer_id, _ = await submitted(client)  # regulated, health, DOB: held for years

    with pytest.raises(DBAPIError) as refused:
        await purge_delete(customer_id)
    assert "form_submissions: DELETE is not permitted" in str(refused.value)
    await set_hold(customer_id, "infinity")
    with pytest.raises(DBAPIError):
        await purge_delete(customer_id)
    assert len(await rows(customer_id)) == 1

    await set_hold(customer_id, "2001-01-01T00:00:00Z")
    assert await purge_delete(customer_id) == 1


async def test_a_key_with_a_submission_cannot_be_deleted_even_by_the_owner(client):
    customer_id, _ = await submitted(client)

    with pytest.raises(DBAPIError) as refused:
        await as_owner("DELETE FROM customer_document_keys WHERE customer_id = :id", id=customer_id)

    assert sqlstate(refused.value) == "23503"
    assert await key_rows(customer_id) == 1


async def test_the_shred_hook_knows_every_foreign_key_to_the_key_row(database):
    """The purge treats a foreign-key refusal of the key DELETE as "a sealed row arrived
    mid-purge" only for these constraints; a new sealed table must be added to both."""
    async with session_scope() as db:
        found = set(
            await db.scalars(
                text(
                    "SELECT conname FROM pg_constraint WHERE contype = 'f' "
                    "AND confrelid = 'customer_document_keys'::regclass"
                )
            )
        )
    assert found == set(tasks.KEY_REFERENCES)


# --- S1: the purge ----------------------------------------------------------------------------


async def test_shredding_an_unheld_client_deletes_submissions_and_documents_then_the_key(client):
    customer_id, _ = await submitted(client)
    await store(customer_id)
    await set_hold(customer_id, None)

    assert await tasks._shred(get_purge_engine(), customer_id, None) is True

    assert await rows(customer_id) == []
    assert await document_rows(customer_id) == 0
    assert await key_rows(customer_id) == 0
    assert await key_destroyed_events(customer_id) == 1


async def test_shredding_a_held_client_deletes_nothing(client):
    customer_id, _ = await submitted(client)
    await store(customer_id)

    assert await tasks._shred(get_purge_engine(), customer_id, None) is False

    assert len(await rows(customer_id)) == 1
    # The eagerly generated archive and the additional fixture document are both held.
    assert await document_rows(customer_id) == 2
    assert await key_rows(customer_id) == 1
    assert await key_destroyed_events(customer_id) == 0


async def test_the_nightly_job_shreds_an_expired_clients_submissions(client):
    customer_id, _ = await submitted(client)
    await set_hold(customer_id, "2001-01-01T00:00:00Z")

    tasks.purge_expired.delay()

    assert await rows(customer_id) == []
    assert await key_rows(customer_id) == 0


async def test_erasing_an_unheld_client_destroys_their_answers(client):
    await as_admin(client)
    await switch(client, "general_business")
    customer_id, _ = await submitted(client)

    resp = await client.post(f"{CUSTOMERS}/{customer_id}/erasure", json={})

    assert resp.status_code == 201, resp.text
    assert resp.json()["purged_at"] is not None
    assert await rows(customer_id) == []
    assert await key_rows(customer_id) == 0


async def test_erasing_a_held_client_keeps_their_health_form(client):
    customer_id, submission_id = await submitted(client)

    resp = await client.post(f"{CUSTOMERS}/{customer_id}/erasure", json={})

    assert resp.status_code == 201, resp.text
    assert resp.json()["held"] is True
    assert len(await rows(customer_id)) == 1
    assert await opened(submission_id) == answers()


async def test_a_submission_inserted_while_the_purge_runs_defers_the_shred(client, caplog):
    """Rule 7's race through `form_submissions`' own FK: the insert holds `FOR KEY SHARE` on
    the key row, the purge's key DELETE queues behind it, and once the insert commits the FK
    refuses the DELETE — "try again next night", not an exception out of the task."""
    from customers import keys
    from tests.test_documents import _wait_for_the_purge_to_block

    customer_id, _, version_id, template = await sent_form(client, health=False)
    await set_hold(customer_id, None)
    purge = get_purge_engine()
    async with session_scope() as db:
        await keys.data_key(db, uuid.UUID(customer_id))
        db.add(
            FormSubmission(
                id=uuid.uuid4(),
                customer_id=uuid.UUID(customer_id),
                version_id=uuid.UUID(version_id),
                template_id=uuid.UUID(template["id"]),
                method="scan",
            )
        )
        await db.flush()
        shred = asyncio.create_task(tasks._shred(purge, customer_id, None))
        await _wait_for_the_purge_to_block(shred)
        await db.commit()

    assert await shred is False
    assert customer_id in caplog.text
    assert len(await rows(customer_id)) == 1 and await key_rows(customer_id) == 1

    assert await tasks._shred(purge, customer_id, None) is True
    assert await rows(customer_id) == [] and await key_rows(customer_id) == 0


def test_only_a_foreign_key_to_the_key_row_reads_as_a_mid_purge_insert():
    deferred = tasks._inserted_mid_purge
    assert deferred("23503", "form_submissions_customer_id_fkey")
    assert deferred("23503", "documents_customer_id_fkey")
    assert not deferred("23503", "some_other_fkey")
    assert not deferred("23503", None)
    assert not deferred("42501", "documents_customer_id_fkey")


# --- staff: the list and the read -------------------------------------------------------------


async def access_rows() -> list:
    async with session_scope() as db:
        return list(
            (
                await db.execute(
                    text("SELECT resource_type, resource_id, customer_id FROM audit_access_log")
                )
            ).all()
        )


async def test_the_list_is_metadata_and_unlogged_and_the_read_is_logged(client):
    customer_id, submission_id = await submitted(client)
    # #83: the access log is not among the purge role's four permitted tables — reset as the
    # schema owner instead.
    async with get_owner_engine().begin() as owner:
        await owner.execute(text("DELETE FROM audit_access_log"))

    listed = await client.get(f"{CUSTOMERS}/{customer_id}/forms")
    assert listed.status_code == 200, listed.text
    [item] = listed.json()["submissions"]
    assert item["id"] == submission_id
    assert (item["template_name"], item["version"], item["method"]) == (
        "Prenatal intake",
        1,
        "link",
    )
    assert "answers" not in item and "twelve weeks" not in listed.text
    assert await access_rows() == []

    read = await client.get(f"{CUSTOMERS}/{customer_id}/forms/{submission_id}")
    assert read.status_code == 200, read.text
    assert read.json()["answers"] == answers()
    assert [(r.resource_type, r.resource_id, str(r.customer_id)) for r in await access_rows()] == [
        ("form_submission", submission_id, customer_id)
    ]

    # Another client's path to this submission finds nothing.
    other = await make_customer(client)
    assert (await client.get(f"{CUSTOMERS}/{other}/forms/{submission_id}")).status_code == 404


async def test_reading_forms_needs_forms_view(client):
    customer_id, submission_id = await submitted(client)
    from tests.test_access_log import add_role

    await add_role(client, "Viewer", ["customers.view"], "viewer@cedar.example")
    await add_colleague(client, "desk@cedar.example", OTHER_PASSWORD)  # the seeded Staff role

    client.cookies.clear()
    await as_staff(client, "viewer@cedar.example", OTHER_PASSWORD)
    for path in (
        f"{CUSTOMERS}/{customer_id}/forms",
        f"{CUSTOMERS}/{customer_id}/forms/{submission_id}",
    ):
        resp = await client.get(path)
        assert resp.status_code == 403 and resp.json()["code"] == "capability_required"

    client.cookies.clear()
    await as_staff(client, "desk@cedar.example", OTHER_PASSWORD)
    assert (await client.get(f"{CUSTOMERS}/{customer_id}/forms")).status_code == 200


def test_every_sealed_answer_is_bound_to_its_row():
    """AD = submission id + customer id: a blob moved to another row does not open."""
    key = os.urandom(32)
    sid, cid = uuid.uuid4(), uuid.uuid4()
    blob = submissions.seal_answers({PREGNANT: "no"}, key, sid, cid)
    assert submissions.unseal_answers(blob, key, sid, cid) == {PREGNANT: "no"}
    from cryptography.exceptions import InvalidTag

    with pytest.raises(InvalidTag):
        submissions.unseal_answers(blob, key, uuid.uuid4(), cid)
    with pytest.raises(InvalidTag):
        submissions.unseal_answers(blob, key, sid, uuid.uuid4())

"""S1/S5: generated archives are immutable, isolated to their client and audited on open."""

import asyncio
import uuid

from sqlalchemy import text

from core.db import session_scope
from tests.test_appointments import claimed_instance  # noqa: F401
from tests.test_form_links import forms_wiped  # noqa: F401
from tests.test_form_submissions import answers, sent_form, submit


async def test_periodic_reconciliation_repairs_a_committed_form_after_enqueue_failure(
    client, monkeypatch
):
    from forms.tasks import reconcile_archives, render_submission

    customer, token, version, _ = await sent_form(client)
    submission_id = uuid.uuid4()

    def unavailable(*args, **kwargs):
        raise RuntimeError("broker unavailable")

    with monkeypatch.context() as queue_failure:
        queue_failure.setattr(render_submission, "delay", unavailable)
        response = await submit(client, token, version, answers(), submission_id)
    assert response.status_code == 200
    path = f"/api/customers/{customer}/forms"
    assert (await client.get(path)).json()["submissions"][0]["pdf_ready"] is False
    await asyncio.to_thread(reconcile_archives)
    assert (await client.get(path)).json()["submissions"][0]["pdf_ready"] is True
    await asyncio.to_thread(reconcile_archives)
    async with session_scope() as db:
        assert (
            await db.scalar(
                text("SELECT count(*) FROM documents WHERE source_id=:id"), {"id": submission_id}
            )
            == 1
        )


async def test_submitted_form_has_one_archive_and_viewing_it_logs_the_submission(client):
    customer, token, version, _ = await sent_form(client)
    submission_id = uuid.uuid4()
    assert (await submit(client, token, version, answers(), submission_id)).status_code == 200
    path = f"/api/customers/{customer}/forms"
    listing = (await client.get(path)).json()["submissions"]
    assert listing[0]["pdf_ready"] is True
    response = await client.get(f"{path}/{submission_id}/pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    from forms.tasks import render_submission

    await asyncio.to_thread(render_submission, str(submission_id))
    async with session_scope() as db:
        assert (
            await db.scalar(
                text("SELECT count(*) FROM documents WHERE source_id = :id"), {"id": submission_id}
            )
            == 1
        )
        logs = (
            await db.execute(
                text(
                    "SELECT resource_type, resource_id FROM audit_access_log "
                    "WHERE resource_type = 'form_document' AND resource_id = :id"
                ),
                {"id": str(submission_id)},
            )
        ).all()
    assert logs == [("form_document", str(submission_id))]


async def test_an_archive_cannot_be_opened_under_another_customer_path(client):
    from tests.test_appointments import make_customer

    customer, token, version, _ = await sent_form(client)
    submission_id = uuid.uuid4()
    await submit(client, token, version, answers(), submission_id)
    other = await make_customer(client)
    result = await client.get(f"/api/customers/{other}/forms/{submission_id}/pdf")
    assert result.status_code == 404
    async with session_scope() as db:
        log = (
            await db.execute(
                text(
                    "SELECT customer_id, resource_id FROM audit_access_log "
                    "WHERE resource_type='form_document' AND resource_id=:id"
                ),
                {"id": str(submission_id)},
            )
        ).one()
    assert log == (uuid.UUID(other), str(submission_id))


async def test_republishing_and_retrying_does_not_rewrite_an_archived_record(client):
    from forms.tasks import render_submission
    from tests.test_forms import publish, save, schema

    customer, token, version, template = await sent_form(client)
    submission_id = uuid.uuid4()
    await submit(client, token, version, answers(), submission_id)
    async with session_scope() as db:
        before = (
            await db.execute(
                text("SELECT digest, ciphertext FROM documents WHERE source_id=:id"),
                {"id": submission_id},
            )
        ).one()
    await save(client, template, schema=schema("Reworded question"))
    assert (await publish(client, template["id"])).status_code == 201
    await asyncio.to_thread(render_submission, str(submission_id))
    async with session_scope() as db:
        after = (
            await db.execute(
                text("SELECT digest, ciphertext FROM documents WHERE source_id=:id"),
                {"id": submission_id},
            )
        ).one()
    assert before == after
    assert (await client.get(f"/api/customers/{customer}/forms")).json()["submissions"][0][
        "version"
    ] == 1


async def test_a_tampered_archive_fails_closed_and_logs_only_its_identity(client, caplog):
    from httpx import ASGITransport, AsyncClient

    from main import app
    from tests.test_form_links import as_owner

    customer, token, version, _ = await sent_form(client)
    submission_id = uuid.uuid4()
    await submit(client, token, version, answers(), submission_id)
    async with session_scope() as db:
        document_id = await db.scalar(
            text("SELECT id FROM documents WHERE source_id=:id"), {"id": submission_id}
        )
    await as_owner(
        "UPDATE documents SET ciphertext=set_byte(ciphertext, 12, get_byte(ciphertext,12) # 1) "
        "WHERE id=:id",
        id=document_id,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        cookies=client.cookies,
    ) as transport:
        response = await transport.get(f"/api/customers/{customer}/forms/{submission_id}/pdf")
    assert response.status_code == 500
    assert "twelve weeks" not in response.text and "Priya" not in response.text
    assert str(document_id) in caplog.text
    assert "twelve weeks" not in caplog.text and "Priya Nair" not in caplog.text


async def test_a_worker_retry_after_erasure_does_not_recreate_a_key_or_archive(client):
    from core.db import get_purge_engine
    from customers.tasks import _shred
    from forms.tasks import render_submission

    customer, token, version, _ = await sent_form(client, health=False)
    submission_id = uuid.uuid4()
    await submit(client, token, version, answers(), submission_id)
    assert await _shred(get_purge_engine(), customer, None)
    await asyncio.to_thread(render_submission, str(submission_id))
    async with session_scope() as db:
        assert (
            await db.scalar(
                text("SELECT count(*) FROM customer_document_keys WHERE customer_id=:id"),
                {"id": customer},
            )
            == 0
        )
        assert (
            await db.scalar(
                text("SELECT count(*) FROM documents WHERE source_id=:id"), {"id": submission_id}
            )
            == 0
        )


async def test_an_unrendered_submission_reports_pending_without_returning_answers(client):
    from tests.test_form_links import as_owner

    customer, token, version, _ = await sent_form(client)
    submission_id = uuid.uuid4()
    await submit(client, token, version, answers(), submission_id)
    # Simulate the real interval between committing a submission and the worker archiving it.
    await as_owner("DELETE FROM documents WHERE source_id=:id", id=submission_id)
    assert (await client.get(f"/api/customers/{customer}/forms")).json()["submissions"][0][
        "pdf_ready"
    ] is False
    response = await client.get(f"/api/customers/{customer}/forms/{submission_id}/pdf")
    assert response.status_code == 202 and response.json() == {"status": "rendering"}
    assert response.headers["cache-control"] == "no-store"

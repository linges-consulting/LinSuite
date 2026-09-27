"""S1/S5: paper forms are archived atomically under the same record protections."""

import base64
import io
import struct
import uuid
import zlib

import pytest
from PIL import Image
from sqlalchemy import text

from core.db import session_scope
from tests.test_appointments import claimed_instance  # noqa: F401
from tests.test_form_links import forms_wiped  # noqa: F401
from tests.test_form_submissions import sent_form


def page(mode="RGB", fmt="JPEG", size=(1800, 2400)) -> str:
    image = Image.new(mode, size, "white")
    out = io.BytesIO()
    image.save(out, fmt)
    return base64.b64encode(out.getvalue()).decode()


async def test_two_scanned_pages_create_one_ready_record_and_extend_the_chart_hold(client):
    customer, _, _, template = await sent_form(client)
    body = {
        "submission_id": str(uuid.uuid4()),
        "template_id": template["id"],
        "version_number": 1,
        "pages": [page(), page()],
    }
    response = await client.post(f"/api/customers/{customer}/forms/scans", json=body)
    assert response.status_code == 201, response.text
    listing = (await client.get(f"/api/customers/{customer}/forms")).json()["submissions"]
    assert [(s["method"], s["version"], s["pdf_ready"]) for s in listing] == [("scan", 1, True)]
    pdf = await client.get(f"/api/customers/{customer}/forms/{body['submission_id']}/pdf")
    assert pdf.headers["content-type"] == "application/pdf"
    # Structural page/image metadata, not a generated-PDF byte snapshot.
    assert pdf.content.count(b"/Type /Page\n") == 2
    assert pdf.content.count(b"/ColorSpace /DeviceGray") == 2
    async with session_scope() as db:
        assert await db.scalar(
            text("SELECT retention_expires_at IS NOT NULL FROM customers WHERE id=:id"),
            {"id": customer},
        )
    retry = await client.post(f"/api/customers/{customer}/forms/scans", json=body)
    assert retry.status_code == 200 and retry.json()["status"] == "already_received"
    assert len((await client.get(f"/api/customers/{customer}/forms")).json()["submissions"]) == 1


async def test_printing_a_published_version_returns_blank_lines_and_its_own_wording(client):
    customer, _, _, template = await sent_form(client)
    response = await client.get(f"/api/admin/forms/{template['id']}/versions/1/print")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    assert "Version 1" in response.text and "Client: ________" in response.text
    assert "Are you pregnant?" in response.text and "How many weeks?" in response.text
    assert customer not in response.text and "Priya" not in response.text


@pytest.mark.parametrize("pages", [[], [page()] * 9, ["not base64"], [page(fmt="GIF")]])
async def test_invalid_scan_pages_are_refused_without_creating_a_record(client, pages):
    customer, _, _, template = await sent_form(client)
    response = await client.post(
        f"/api/customers/{customer}/forms/scans",
        json={
            "submission_id": str(uuid.uuid4()),
            "template_id": template["id"],
            "version_number": 1,
            "pages": pages,
        },
    )
    assert response.status_code == 422
    assert (await client.get(f"/api/customers/{customer}/forms")).json()["submissions"] == []


async def test_scans_cannot_be_added_to_a_suppressed_chart(client):
    from tests.test_form_links import as_owner

    customer, _, _, template = await sent_form(client)
    await as_owner("UPDATE customers SET suppressed_at=now() WHERE id=:id", id=customer)
    response = await client.post(
        f"/api/customers/{customer}/forms/scans",
        json={
            "submission_id": str(uuid.uuid4()),
            "template_id": template["id"],
            "version_number": 1,
            "pages": [page()],
        },
    )
    assert response.status_code == 422 and response.json()["code"] == "customer_suppressed"


async def test_a_reused_scan_id_cannot_attach_another_customers_record(client):
    from tests.test_appointments import make_customer

    customer, _, _, template = await sent_form(client)
    other = await make_customer(client)
    body = {
        "submission_id": str(uuid.uuid4()),
        "template_id": template["id"],
        "version_number": 1,
        "pages": [page()],
    }
    assert (
        await client.post(f"/api/customers/{customer}/forms/scans", json=body)
    ).status_code == 201
    assert (await client.post(f"/api/customers/{other}/forms/scans", json=body)).status_code == 409
    assert (await client.get(f"/api/customers/{other}/forms")).json()["submissions"] == []


async def test_a_failed_scan_transaction_leaves_no_document_submission_key_or_hold(client):
    from httpx import ASGITransport, AsyncClient

    from main import app
    from tests.test_form_links import as_owner

    customer, _, _, template = await sent_form(client)
    # Customer creation reserves a key. Remove it first so this request must create one.
    await as_owner("DELETE FROM customer_document_keys WHERE customer_id=:id", id=customer)
    await as_owner("""CREATE FUNCTION public.reject_scan_audit() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        IF NEW.event_type = 'form.scan_uploaded' THEN
            RAISE EXCEPTION 'injected scan audit failure';
        END IF; RETURN NEW; END $$""")
    await as_owner("""CREATE TRIGGER reject_scan_audit BEFORE INSERT ON public.audit_events
        FOR EACH ROW EXECUTE FUNCTION public.reject_scan_audit()""")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
            cookies=client.cookies,
        ) as transport:
            result = await transport.post(
                f"/api/customers/{customer}/forms/scans",
                json={
                    "submission_id": str(uuid.uuid4()),
                    "template_id": template["id"],
                    "version_number": 1,
                    "pages": [page()],
                },
            )
        assert result.status_code == 500
        assert (await client.get(f"/api/customers/{customer}/forms")).json()["submissions"] == []
        async with session_scope() as db:
            assert (
                await db.scalar(
                    text("SELECT retention_expires_at FROM customers WHERE id=:id"),
                    {"id": customer},
                )
                is None
            )
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM documents WHERE customer_id=:id"), {"id": customer}
                )
                == 0
            )
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM customer_document_keys WHERE customer_id=:id"),
                    {"id": customer},
                )
                == 0
            )
    finally:
        await as_owner("DROP TRIGGER reject_scan_audit ON public.audit_events")
        await as_owner("DROP FUNCTION public.reject_scan_audit()")


async def test_a_tiny_png_with_bomb_dimensions_is_refused_before_decode(client):
    customer, _, _, template = await sent_form(client)
    data = bytearray(base64.b64decode(page(fmt="PNG", size=(1, 1))))
    data[16:24] = struct.pack(">II", 8000, 8000)
    data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]))
    response = await client.post(
        f"/api/customers/{customer}/forms/scans",
        json={
            "submission_id": str(uuid.uuid4()),
            "template_id": template["id"],
            "version_number": 1,
            "pages": [base64.b64encode(data).decode()],
        },
    )
    assert response.status_code == 422 and "size limit" in response.text
    assert (await client.get(f"/api/customers/{customer}/forms")).json()["submissions"] == []


async def test_scanning_and_printing_require_the_issue_capability(client):
    from tests.test_access_log import add_role
    from tests.test_appointments import OTHER_PASSWORD

    customer, _, _, template = await sent_form(client)
    await add_role(client, "Read only clients", ["customers.view"], "reader@cedar.example")
    await client.post("/api/auth/logout")
    await client.post(
        "/api/auth/login", json={"email": "reader@cedar.example", "password": OTHER_PASSWORD}
    )
    response = await client.post(
        f"/api/customers/{customer}/forms/scans",
        json={
            "submission_id": str(uuid.uuid4()),
            "template_id": template["id"],
            "version_number": 1,
            "pages": [page()],
        },
    )
    assert response.status_code == 403 and response.json()["code"] == "capability_required"
    assert (
        await client.get(f"/api/admin/forms/{template['id']}/versions/1/print")
    ).status_code == 403

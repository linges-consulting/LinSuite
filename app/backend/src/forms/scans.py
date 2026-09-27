"""Paper fallback: bounded images become one immutable record in the chart transaction."""

import asyncio
import base64
import binascii
import io
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from PIL import Image
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from auth.capabilities import Requires
from auth.models import User
from core.audit import record_event
from core.db import SessionDep
from core.documents import store_document
from core.models import Business
from customers.keys import data_key
from customers.models import Customer
from customers.retention import CustomerSuppressed, record_clinical_entry
from forms.models import FormSubmission, FormTemplateVersion
from forms.render import render_html
from forms.routes import VersionSummary
from settings.images import Rejected, normalise

router = APIRouter(tags=["forms"])
Issuer = Annotated[User, Depends(Requires("forms.issue"))]
log = logging.getLogger(__name__)
# Leaves room for the JSON wrapper beneath the 2 MiB edge limit, including eight pages.
MAX_ENCODED = 1_900_000


class ScanIn(BaseModel):
    submission_id: uuid.UUID
    template_id: uuid.UUID
    version_number: int = Field(ge=1)
    pages: Annotated[
        list[Annotated[str, Field(max_length=MAX_ENCODED)]], Field(min_length=1, max_length=8)
    ]

    @model_validator(mode="after")
    def bounded_body(self) -> "ScanIn":
        if sum(map(len, self.pages)) > MAX_ENCODED:
            raise ValueError("The scan is too large. Resize the pages or use fewer pages.")
        return self


class ScanResult(BaseModel):
    status: str
    submission_id: str


def scan_pdf(pages: list[str]) -> bytes:
    """Sniff and bound pixels before decoding; strip metadata and archive at 150 DPI."""
    images = []
    for encoded in pages:
        try:
            data = base64.b64decode(encoded, validate=True)
            clean, _, _ = normalise(data, allowed=("jpeg", "png"), max_px=1650)
        except (ValueError, binascii.Error, Rejected):
            raise HTTPException(
                status_code=422, detail="Use JPEG or PNG pages within the image size limit."
            ) from None
        with Image.open(io.BytesIO(clean)) as source:
            canvas = Image.new("RGBA", source.size, "white")
            canvas.alpha_composite(source.convert("RGBA"))
            image = canvas.convert("L")
            image.thumbnail((1275, 1650))
            images.append(image)
    result = io.BytesIO()
    images[0].save(
        result, "PDF", save_all=True, append_images=images[1:], resolution=150, quality=70
    )
    return result.getvalue()


def _retry(existing: FormSubmission, payload: ScanIn, customer_id: uuid.UUID) -> ScanResult:
    if (existing.customer_id, existing.template_id, existing.method) != (
        customer_id,
        payload.template_id,
        "scan",
    ):
        raise HTTPException(status_code=409, detail="This submission id is already in use.")
    return ScanResult(status="already_received", submission_id=str(existing.id))


@router.post("/customers/{customer_id}/forms/scans", status_code=201, response_model=ScanResult)
async def upload_scan(
    customer_id: uuid.UUID, payload: ScanIn, request: Request, user: Issuer, db: SessionDep
):
    from fastapi.responses import JSONResponse

    version = await db.scalar(
        select(FormTemplateVersion).where(
            FormTemplateVersion.template_id == payload.template_id,
            FormTemplateVersion.number == payload.version_number,
        )
    )
    if version is None:
        raise HTTPException(status_code=404, detail="No such published version.")
    existing = await db.get(FormSubmission, payload.submission_id)
    if existing:
        if existing.version_id != version.id:
            raise HTTPException(status_code=409, detail="This submission id is already in use.")
        result = _retry(existing, payload, customer_id)
        return JSONResponse(result.model_dump(), status_code=200)
    customer = await db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="No such client.")
    content = await asyncio.to_thread(scan_pdf, payload.pages)
    at = datetime.now(UTC)
    try:
        if version.is_health_form:
            await record_clinical_entry(db, customer_id, at)
        else:
            customer = await db.scalar(
                select(Customer)
                .where(Customer.id == customer_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if customer.suppressed_at is not None:
                raise CustomerSuppressed(str(customer_id))
    except CustomerSuppressed:
        await db.rollback()
        return JSONResponse(
            {"code": "customer_suppressed", "detail": "This client has been erased."},
            status_code=422,
        )
    # A concurrent retry waited on the chart lock above. Recheck before creating any row.
    existing = await db.get(FormSubmission, payload.submission_id)
    if existing:
        if existing.version_id != version.id:
            await db.rollback()
            raise HTTPException(status_code=409, detail="This submission id is already in use.")
        result = _retry(existing, payload, customer_id)
        await db.rollback()
        return JSONResponse(result.model_dump(), status_code=200)
    key = await data_key(db, customer_id)
    db.add(
        FormSubmission(
            id=payload.submission_id,
            customer_id=customer_id,
            version_id=version.id,
            template_id=version.template_id,
            method="scan",
            answers_sealed=None,
            submitted_at=at,
            source_ip=request.client.host if request.client else None,
        )
    )
    try:
        await db.flush()
        await store_document(
            db,
            key=key,
            customer_id=customer_id,
            kind="form_submission",
            source_id=payload.submission_id,
            content=content,
            content_type="application/pdf",
        )
        record_event(
            db,
            "form.scan_uploaded",
            actor_user_id=user.id,
            target_type="form_submission",
            target_id=str(payload.submission_id),
            metadata={
                "template_id": str(version.template_id),
                "version": version.number,
                "pages": len(payload.pages),
            },
        )
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        constraint = getattr(getattr(error.orig, "__cause__", None), "constraint_name", None)
        if constraint not in {
            "form_submissions_pkey",
            "form_submissions_customer_id_fkey",
            "documents_customer_id_fkey",
        }:
            raise
        log.warning("scan submission refused by %s", constraint)
        return JSONResponse(
            {"code": "try_again", "detail": "That did not go through. Please try again."},
            status_code=409,
        )
    return ScanResult(status="received", submission_id=str(payload.submission_id))


@router.get("/forms/templates/{template_id}/versions")
async def paper_versions(
    template_id: uuid.UUID, _: Issuer, db: SessionDep
) -> dict[str, list[VersionSummary]]:
    versions = await db.scalars(
        select(FormTemplateVersion)
        .where(FormTemplateVersion.template_id == template_id)
        .order_by(FormTemplateVersion.number.desc())
    )
    return {"versions": [VersionSummary.model_validate(v, from_attributes=True) for v in versions]}


@router.get("/admin/forms/{template_id}/versions/{number}/print")
async def print_blank(
    template_id: uuid.UUID, number: int, _: Issuer, db: SessionDep
) -> HTMLResponse:
    version = await db.scalar(
        select(FormTemplateVersion).where(
            FormTemplateVersion.template_id == template_id,
            FormTemplateVersion.number == number,
        )
    )
    if version is None:
        raise HTTPException(status_code=404, detail="No such published version.")
    business = await db.get(Business, 1)
    html = render_html(version, {}, business, blank=True)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})

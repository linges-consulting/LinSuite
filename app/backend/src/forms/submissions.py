"""Completed forms: what counts as signed, how answers are sealed, and the staff read (#47).

The public submit itself is `forms/public.py` (it shares the link lookup and the throttle).

**Sealed, never plain** (owner ruling Q8, pre-flight C1). A submission's answers — the drawn
signature and the typed name included — are one JSON document, sealed with AES-256-GCM under
the client's DEK into `form_submissions.answers_sealed`. The associated data is the
submission id and the customer id, so a blob moved onto another row, or a row re-pointed at
another client, does not open. Nothing queries answers in SQL; nothing logs or audits them.
When the client's key is shredded the answers go with it.

**Signed** (owner ruling Q3) = a drawn signature *and* a typed full name. The shape check is
the shared twin's (`schema.validate_answers`); this file adds what only a server can be trusted
with: the image is a PNG data URL, at most `SIGNATURE_MAX_BYTES`, within `SIGNATURE_MAX_SIDE`
on each side (checked from the header, before any pixel is decoded), decodes, and has ink on
it — spanning a minimum share of the pad each way and covering at most `MAX_INK_RATIO` of it,
so a transparent, white, all-black, dotted or blotted pad is not a signature. The name is
non-blank and at most `MAX_SIGNED_NAME` characters.

**Staff read** (`forms.view`): the list is metadata — form name, version, method, when — and
is not an access (pre-flight C5). Opening one returns the answers, so it is logged per open,
naming the submission (`LogAccess(..., resource_param="submission_id")`). Answers are always
shown with the labels of the version they were given against, never the latest.
"""

import base64
import binascii
import io
import json
import uuid
from datetime import datetime
from typing import Any

from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from PIL import Image
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.capabilities import Requires
from core import crypto
from core.access_log import LogAccess
from core.db import SessionDep
from core.documents import fetch_document
from core.errors import DocumentIntegrityError
from core.models import Document
from customers.keys import existing_key
from forms.models import FormSubmission, FormTemplateVersion
from forms.schema import FormSchema, validate_answers
from settings.images import sniff

SIGNATURE_MAX_BYTES = 200 * 1024
# The pad draws at 600 x 200; anything much larger is not from it.
SIGNATURE_MAX_SIDE = 2000
MAX_SIGNED_NAME = 200
# "Something was drawn": dark pixels (luminance < 128 on white) that span at least this
# share of the pad each way — a tap, a dot or a small blob is not a signature — and cover no
# more than `MAX_INK_RATIO` of it, so a filled or all-black image is not one either.
#
# `signature-pad.tsx` mirrors these four numbers (fix round 1, #51). A runtime shared-source
# file was tried and reverted: the frontend's dev/prod containers only bind-mount
# `app/frontend` (`infra/compose.yaml`), so a cross-directory import that works when this
# suite runs against the full checkout on the host 404s inside the real container. Instead
# `app/shared/signature-ink-constants.json` is the canonical reference value, and
# `tests/test_signature_ink_constants.py` parses both source files and fails if either drifts
# from it — the same "shared JSON fixture, not shared runtime code" shape
# `form-schema-cases.json` already uses for the schema twins.
MIN_INK_PIXELS = 20
MIN_INK_WIDTH = 0.05  # 30 px of the 600 px pad
MIN_INK_HEIGHT = 0.03  # 6 px of the 200 px pad
MAX_INK_RATIO = 0.60
_DATA_URL = "data:image/png;base64,"
_MAX_ENCODED = len(_DATA_URL) + (SIGNATURE_MAX_BYTES * 4) // 3 + 4


# --- the signature (S2) ----------------------------------------------------------------------


def _has_ink(png: bytes) -> bool:
    try:
        image = Image.open(io.BytesIO(png))
        if max(image.size) > SIGNATURE_MAX_SIDE:
            return False
        image.load()
        page = Image.new("RGBA", image.size, "white")
        page.alpha_composite(image.convert("RGBA"))
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        return False
    ink = page.convert("L").point(lambda level: 255 if level < 128 else 0)
    dark = ink.histogram()[255]
    box = ink.getbbox()
    if box is None or dark < MIN_INK_PIXELS or dark > MAX_INK_RATIO * page.width * page.height:
        return False
    left, top, right, bottom = box
    return (
        right - left >= MIN_INK_WIDTH * page.width and bottom - top >= MIN_INK_HEIGHT * page.height
    )


def is_signed(value: dict[str, Any]) -> bool:
    """A signature answer (shape already checked by `validate_answers`) that is really signed."""
    name, image = value.get("name"), value.get("image")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_SIGNED_NAME:
        return False
    if not isinstance(image, str) or not image.startswith(_DATA_URL) or len(image) > _MAX_ENCODED:
        return False
    try:
        png = base64.b64decode(image.removeprefix(_DATA_URL), validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(png) <= SIGNATURE_MAX_BYTES and sniff(png) == "png" and _has_ink(png)


def answer_errors(schema: FormSchema, answers: dict[str, Any]) -> dict[str, str]:
    """The twin's per-field codes, plus "invalid" for a signature the server does not accept."""
    errors = validate_answers(schema, answers)
    for field in schema.fields:
        value = answers.get(field.key)
        if field.type == "signature" and field.key not in errors and isinstance(value, dict):
            if not is_signed(value):
                errors[field.key] = "invalid"
    return errors


# --- sealing ---------------------------------------------------------------------------------


def _associated_data(submission_id: uuid.UUID, customer_id: uuid.UUID) -> bytes:
    return submission_id.bytes + customer_id.bytes


def seal_answers(
    answers: dict[str, Any], key: bytes, submission_id: uuid.UUID, customer_id: uuid.UUID
) -> bytes:
    plaintext = json.dumps(answers, ensure_ascii=False, separators=(",", ":")).encode()
    return crypto.seal(plaintext, key, _associated_data(submission_id, customer_id))


def unseal_answers(
    blob: bytes, key: bytes, submission_id: uuid.UUID, customer_id: uuid.UUID
) -> dict[str, Any]:
    return json.loads(crypto.open_sealed(blob, key, _associated_data(submission_id, customer_id)))


async def open_answers(db: AsyncSession, submission: FormSubmission) -> dict[str, Any] | None:
    """The answers as submitted, or None when there are none to open — a scan, or a client
    whose key is gone. Never creates a key (`existing_key`). A blob that does not authenticate
    raises `DocumentIntegrityError`: no partial read, no fallback."""
    if submission.answers_sealed is None:
        return None
    key = await existing_key(db, submission.customer_id)
    if key is None:
        return None
    try:
        return unseal_answers(submission.answers_sealed, key, submission.id, submission.customer_id)
    except (InvalidTag, ValueError):
        raise DocumentIntegrityError(str(submission.id)) from None


# --- staff: list and read ----------------------------------------------------------------------

router = APIRouter(prefix="/customers", tags=["forms"])
Viewer = [Depends(Requires("forms.view"))]


class SubmissionSummary(BaseModel):
    id: str
    template_id: str
    # The version's own name, as the client saw it.
    template_name: str
    version: int
    method: str
    submitted_at: datetime
    pdf_ready: bool = False


class Submissions(BaseModel):
    submissions: list[SubmissionSummary]


class SubmittedField(BaseModel):
    key: str
    type: str
    label: str


class SubmissionOut(SubmissionSummary):
    # The version's fields, in order — the labels the answers were given against.
    fields: list[SubmittedField]
    # PHI (`core.access_log.PHI_FIELDS`): only ever through a logged route.
    answers: dict[str, Any]


def _query(customer_id: uuid.UUID):
    return (
        select(FormSubmission, FormTemplateVersion)
        .join(FormTemplateVersion, FormTemplateVersion.id == FormSubmission.version_id)
        .where(FormSubmission.customer_id == customer_id)
    )


def _summary(submission: FormSubmission, version: FormTemplateVersion) -> dict[str, Any]:
    return {
        "id": str(submission.id),
        "template_id": str(submission.template_id),
        "template_name": version.name,
        "version": version.number,
        "method": submission.method,
        "submitted_at": submission.submitted_at,
    }


@router.get("/{customer_id}/forms", dependencies=Viewer)
async def list_submissions(customer_id: uuid.UUID, db: SessionDep) -> Submissions:
    """Metadata only, newest first: no answers, so no access row (pre-flight C5)."""
    rows = await db.execute(
        _query(customer_id)
        .add_columns(Document.id)
        .outerjoin(
            Document,
            (Document.source_id == FormSubmission.id)
            & (Document.kind == "form_submission")
            & (Document.customer_id == customer_id),
        )
        .order_by(FormSubmission.submitted_at.desc())
    )
    return Submissions(
        submissions=[
            SubmissionSummary(**_summary(s, v), pdf_ready=document_id is not None)
            for s, v, document_id in rows
        ]
    )


@router.get(
    "/{customer_id}/forms/{submission_id}/pdf",
    dependencies=[*Viewer, Depends(LogAccess("form_document", resource_param="submission_id"))],
)
async def view_pdf(customer_id: uuid.UUID, submission_id: uuid.UUID, db: SessionDep) -> Response:
    submission = await db.scalar(
        select(FormSubmission.id).where(
            FormSubmission.id == submission_id, FormSubmission.customer_id == customer_id
        )
    )
    key = await existing_key(db, customer_id)
    if submission is None or key is None:
        raise HTTPException(status_code=404, detail="No such form.")
    document_id = await db.scalar(
        select(Document.id).where(
            Document.source_id == submission_id,
            Document.kind == "form_submission",
            Document.customer_id == customer_id,
        )
    )
    if document_id is None:
        return JSONResponse(
            {"status": "rendering"}, status_code=202, headers={"Cache-Control": "no-store"}
        )
    content, _ = await fetch_document(
        db, key=key, key_owner="customer", customer_id=customer_id, document_id=document_id
    )
    return Response(
        content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": "inline; filename=form.pdf",
            "Cache-Control": "no-store",
        },
    )


@router.get(
    "/{customer_id}/forms/{submission_id}",
    dependencies=[*Viewer, Depends(LogAccess("form_submission", resource_param="submission_id"))],
)
async def read_submission(
    customer_id: uuid.UUID, submission_id: uuid.UUID, db: SessionDep
) -> SubmissionOut:
    row = (await db.execute(_query(customer_id).where(FormSubmission.id == submission_id))).first()
    if row is None:
        raise HTTPException(status_code=404, detail="No such form.")
    submission, version = row
    answers = await open_answers(db, submission)
    if answers is None:
        raise HTTPException(status_code=404, detail="No answers to show for this form.")
    fields = [
        SubmittedField(key=f["key"], type=f["type"], label=f["label"])
        for f in version.schema["fields"]
    ]
    return SubmissionOut(**_summary(submission, version), fields=fields, answers=answers)

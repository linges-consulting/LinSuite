"""The public form page's API: `POST /api/public/forms/lookup` (Task 3, #46; pre-flight C6).

The product's first unauthenticated surface. Its rules:

- **The token never sits in a request path.** A path is what every access log writes —
  uvicorn's default access log would put `GET /api/public/forms/<token>` in the container
  logs, a live link for anyone who can read them. So the lookup is a POST with the token in
  a JSON body (fix round 1). As a POST it goes through the JSON-only guard, and `main.py`
  adds the `Origin` check the upload paths have: only this deployment's page may ask.

- **No auth dependency, no cookie.** Nothing here reads the staff session or could renew it,
  so a signed-in tablet and a client's phone get the same answer, and the response never
  carries `Set-Cookie`. `main.py` adds `Cache-Control: no-store` and `Referrer-Policy:
  no-referrer` to everything under `/api/public/`, so the token in the URL reaches neither a
  cache nor a third party.
- **One answer for every dead link.** Unknown, expired, consumed, revoked, client erased,
  form retired: all the same 404 `link_invalid` body. Every condition is in the one `WHERE`,
  so a dead link and an unknown token take the same path — the database finds no row — and
  there is no second query whose timing could tell them apart. The digest is then compared
  with `hmac.compare_digest`, belt to the index's braces.
- **Guessing is infeasible and throttled.** 256-bit tokens; 30 lookups a minute per source
  address in Redis (`PUBLIC_LOOKUPS_PER_MINUTE`), the same figure Traefik's `public` router
  applies at the edge. The address is the `X-Forwarded-For` Traefik overwrites; without
  Traefik in front it is caller-chosen and this count is bypassable. Redis down is a 503,
  never an unthrottled lookup.
- **Minimal.** The pinned version's name and schema, the business's name and logo, the
  client's first name (for "Hi Priya") and the expiry. Nothing else of the profile.

**Not an access-log row** (ADR-0002, Global Constraints): the access log records staff
opening a chart; the link holder is the client, shown their own first name. The open is still
recorded — one `form.link_opened` audit event with the link id and no actor — so "was it
opened, and when" is answerable without writing PHI anywhere.
"""

import asyncio
import hmac
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from core.audit import record_event
from core.db import SessionDep
from core.errors import TRY_AGAIN
from core.redis import get_redis
from customers.keys import data_key
from customers.models import Customer
from customers.retention import CustomerSuppressed, record_clinical_entry
from forms.links import PUBLIC_LOOKUPS_PER_MINUTE, digest
from forms.models import FormLink, FormSubmission, FormTemplate, FormTemplateVersion
from forms.schema import FormSchema, kept_answers
from forms.submissions import answer_errors, seal_answers
from settings.routes import branding_document

log = logging.getLogger(__name__)

router = APIRouter(prefix="/public", tags=["public"])

# Longer than any token this app mints; anything longer is refused before it is hashed.
_MAX_TOKEN = 128


class LookupIn(BaseModel):
    token: str


class PublicBusiness(BaseModel):
    name: str
    logo_url: str | None


class PublicForm(BaseModel):
    version_id: str
    template_name: str
    schema_: dict = Field(serialization_alias="schema")
    business: PublicBusiness
    client_first_name: str
    expires_at: datetime


def _invalid() -> JSONResponse:
    return JSONResponse(
        status_code=404, content={"detail": "This link is no longer valid.", "code": "link_invalid"}
    )


async def _live_link(
    db: AsyncSession, token: str
) -> tuple[FormLink, FormTemplateVersion, str] | None:
    """(link, its pinned version, the client's first name) for a link that can still be used,
    else None. Every condition is in the one `WHERE`, so every dead link takes the same path."""
    if len(token) > _MAX_TOKEN:
        return None
    wanted = digest(token)
    row = (
        await db.execute(
            select(FormLink, FormTemplateVersion, Customer.first_name)
            .join(FormTemplateVersion, FormTemplateVersion.id == FormLink.version_id)
            .join(FormTemplate, FormTemplate.id == FormTemplateVersion.template_id)
            .join(Customer, Customer.id == FormLink.customer_id)
            .where(
                FormLink.token_sha256 == wanted,
                FormLink.expires_at > func.now(),
                FormLink.consumed_at.is_(None),
                FormLink.revoked_at.is_(None),
                Customer.suppressed_at.is_(None),
                FormTemplate.retired_at.is_(None),
            )
        )
    ).first()
    if row is None or not hmac.compare_digest(bytes(row[0].token_sha256), wanted):
        return None
    return row[0], row[1], row[2]


async def throttle(request: Request) -> None:
    """A dependency, so it runs before the body is validated: a malformed request counts too."""
    address = request.client.host if request.client else "unknown"
    key = f"public:forms:{address}"
    redis = get_redis()
    count = await redis.incr(key)
    await redis.expire(key, 60, nx=True)  # never permanent, even if the first expire was lost
    if count > PUBLIC_LOOKUPS_PER_MINUTE:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Try again in a minute.",
            headers={"Retry-After": str(max(1, await redis.ttl(key)))},
        )


@router.post(
    "/forms/lookup",
    response_model=PublicForm,
    response_model_by_alias=True,
    dependencies=[Depends(throttle)],
)
async def public_form(payload: LookupIn, request: Request, db: SessionDep):
    token = payload.token
    row = await _live_link(db, token)
    if row is None:
        return _invalid()

    link, version, first_name = row
    link_id, expires = link.id, link.expires_at
    # Where it was opened from — the one fact that tells a forwarded link from the client's own.
    record_event(
        db,
        "form.link_opened",
        target_type="form_link",
        target_id=str(link_id),
        metadata={"ip": request.client.host if request.client else None},
    )
    business = await branding_document(db)
    await db.commit()
    return PublicForm(
        version_id=str(version.id),
        template_name=version.name,
        schema_=version.schema,
        business=PublicBusiness(name=business.name, logo_url=business.logo_url),
        client_first_name=first_name,
        expires_at=expires,
    )


# --- submitting (#47) ------------------------------------------------------------------------


class SubmitIn(BaseModel):
    token: str
    # Minted by the page once and resent on every retry, so a retry is recognisable.
    submission_id: uuid.UUID
    # The version the page rendered; it must be the link's pinned one.
    version_id: uuid.UUID
    answers: dict[str, Any]


# The two refusals a submit can meet through no fault of its own, each leaving the link open:
# the id is already filed (through another link — this one's retry is `already_received`), or
# the client's key was purged between its read and this insert (ADR-0001 rule 7: the FK
# refuses; a retry makes a new key). Any other integrity error is a bug and raises.
_TRY_AGAIN_CONSTRAINTS = frozenset({"form_submissions_pkey", "form_submissions_customer_id_fkey"})


def _try_again(constraint: str | None) -> bool:
    return constraint in _TRY_AGAIN_CONSTRAINTS


def _coded(status: int, code: str, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, "code": code, **extra})


async def _already_received(db: AsyncSession, token: str, submission_id: uuid.UUID) -> bool:
    """This submission, through this link, is already filed — a retry after a lost answer."""
    if len(token) > _MAX_TOKEN:
        return False
    found = await db.scalar(
        select(FormSubmission.id)
        .join(FormLink, FormLink.id == FormSubmission.link_id)
        .where(FormSubmission.id == submission_id, FormLink.token_sha256 == digest(token))
    )
    return found is not None


async def _lock_the_chart(
    db: AsyncSession, customer_id: uuid.UUID, health: bool, at: datetime
) -> None:
    """Take the client's row before the link's, in the erasure's own order, and refuse a
    suppressed client under that lock (`CustomerSuppressed`). A health form is a clinical
    entry (owner ruling Q1), recorded here, staged in this transaction. Anything else only
    waits for an erasure in flight: `FOR KEY SHARE` conflicts with its `FOR UPDATE`."""
    if health:
        await record_clinical_entry(db, customer_id, at)
        return
    suppressed = await db.scalar(
        select(Customer.suppressed_at)
        .where(Customer.id == customer_id)
        .with_for_update(key_share=True)
    )
    if suppressed is not None:
        raise CustomerSuppressed(str(customer_id))


@router.post("/forms/submit", dependencies=[Depends(throttle)])
async def submit_form(payload: SubmitIn, request: Request, db: SessionDep):
    """One transaction (ADR-0001 rule 7): the client's row (and, for a health form, the new
    hold), the link consumed by a guarded `UPDATE`, the key (made if missing), the sealed
    submission and its audit row commit together — or none of it does.

    200 `received`; 200 `already_received` for a retry of a filed submission; 404
    `link_invalid` for every dead link, another submission on a used one, and an erased
    client; 409 `version_mismatch`; 422 `invalid_answers` with `{key: code}`."""
    token, submission_id = payload.token, payload.submission_id
    if await _already_received(db, token, submission_id):
        return {"status": "already_received"}
    row = await _live_link(db, token)
    if row is None:
        return _invalid()
    link, version, _ = row
    if payload.version_id != version.id:
        return _coded(409, "version_mismatch", "This form has changed. Please ask for a new link.")
    schema = FormSchema.model_validate(version.schema)
    errors = answer_errors(schema, payload.answers)
    if errors:
        return _coded(422, "invalid_answers", "Some answers need attention.", errors=errors)

    submitted_at = datetime.now(UTC)
    customer_id = link.customer_id
    try:
        await _lock_the_chart(db, customer_id, version.is_health_form, submitted_at)
    except CustomerSuppressed:
        await db.rollback()
        return _invalid()
    # Single use: the statement is the lock. A concurrent submit of this link waits on the
    # row here, then finds it consumed.
    consumed = await db.scalar(
        update(FormLink)
        .where(
            FormLink.id == link.id,
            FormLink.consumed_at.is_(None),
            FormLink.revoked_at.is_(None),
            FormLink.expires_at > func.now(),
            # Retiring revokes open links; this closes the window before that commits.
            ~select(FormTemplate.id)
            .where(FormTemplate.id == version.template_id, FormTemplate.retired_at.is_not(None))
            .exists(),
        )
        .values(consumed_at=func.now())
        .returning(FormLink.id)
    )
    if consumed is None:
        await db.rollback()
        if await _already_received(db, token, submission_id):
            return {"status": "already_received"}
        return _invalid()

    key = await data_key(db, customer_id)
    db.add(
        FormSubmission(
            id=submission_id,
            customer_id=customer_id,
            version_id=version.id,
            template_id=version.template_id,
            link_id=link.id,
            method="link",
            # Visible, non-empty answers only: a hidden field's leftover is never filed.
            answers_sealed=seal_answers(
                kept_answers(schema, payload.answers), key, submission_id, customer_id
            ),
            submitted_at=submitted_at,
            source_ip=request.client.host if request.client else None,
        )
    )
    record_event(
        db,
        "form.submitted",
        target_type="form_submission",
        target_id=str(submission_id),
        # Identifiers only — never an answer, a name or the token.
        metadata={
            "link_id": str(link.id),
            "template_id": str(version.template_id),
            "version": version.number,
        },
    )
    try:
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        constraint = getattr(getattr(error.orig, "__cause__", None), "constraint_name", None)
        if not _try_again(constraint):
            raise
        # The constraint's name only — never the payload, the token or an answer.
        log.warning("form submission refused by %s; nothing written", constraint)
        return _coded(409, TRY_AGAIN, "That did not go through. Please try again.")

    try:
        from forms.tasks import render_submission

        await asyncio.to_thread(render_submission.delay, str(submission_id))
    except Exception:
        # Committed already; the PDF can be rendered later (Task 5).
        log.exception("render_submission could not be enqueued for %s", submission_id)
    return {"status": "received"}

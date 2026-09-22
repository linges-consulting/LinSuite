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

import hmac
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from starlette.requests import Request

from core.audit import record_event
from core.db import SessionDep
from core.redis import get_redis
from customers.models import Customer
from forms.links import PUBLIC_LOOKUPS_PER_MINUTE, digest
from forms.models import FormLink, FormTemplate, FormTemplateVersion
from settings.routes import branding_document

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
    if len(token) > _MAX_TOKEN:
        return _invalid()
    wanted = digest(token)
    row = (
        await db.execute(
            select(FormLink.id, FormLink.token_sha256, FormLink.expires_at, FormTemplateVersion)
            .add_columns(Customer.first_name)
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
    if row is None or not hmac.compare_digest(bytes(row.token_sha256), wanted):
        return _invalid()

    link_id, _, expires, version, first_name = row
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

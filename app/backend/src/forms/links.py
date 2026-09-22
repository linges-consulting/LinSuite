"""Secure form links, staff side: issue, list, revoke (Task 3, #46; CLAUDE.md, tech-stack §14).

**The token.** `secrets.token_urlsafe(32)` — 256 bits, 43 url-safe characters. It exists in
exactly two places: the `url` in the issue response (the one time it is returned) and the
email. The row keeps `sha256(token)`; a database dump, a backup or an audit query yields no
usable link. The public page looks a link up by that digest (`forms/public.py`).

**Pinned.** A link names one client and one *version* — the latest published when it was
issued. Republishing later does not move it: the client fills in what staff sent.

**48 hours, fixed** (owner ruling Q5). Single use is Task 4's guarded `UPDATE`; the checks
that decide whether a link is still good live in `public.py`.

**Staff Mode** (`forms.issue`): the front desk sends forms. The owner's in-clinic flow is a
tablet that is never signed in as staff — staff show the QR code from the issue dialog and
the tablet scans it — so the URL in the response is what gets handed over.

**Erased clients** (pre-flight C9): no link is issued (422 `customer_suppressed`), and
`revoke_open_links` — called by `customers/erasure.py` inside the erasure transaction —
closes every link still open.

Audit rows carry the link id, customer id, template id and version number — never the token.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth.capabilities import Requires
from auth.models import User
from core.audit import record_event
from core.config import get_settings
from core.db import SessionDep
from customers.models import Customer
from forms.models import FormLink, FormTemplate, FormTemplateVersion
from notifications.tasks import send_email

LIFETIME = timedelta(hours=48)
# Per source address, per minute, on the public lookup (`public.py`). Traefik's `public`
# router applies the same number at the edge; this one holds wherever the edge does not.
PUBLIC_LOOKUPS_PER_MINUTE = 30

router = APIRouter(tags=["forms"])
Issuer = Annotated[User, Depends(Requires("forms.issue"))]


# --- the token (S2) --------------------------------------------------------------------------


def digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def new_token() -> tuple[str, bytes]:
    """(token, its SHA-256). Only the second is ever stored."""
    token = secrets.token_urlsafe(32)
    return token, digest(token)


def expires_at(issued_at: datetime) -> datetime:
    return issued_at + LIFETIME


def url_of(token: str) -> str:
    return f"{get_settings().app_base_url.rstrip('/')}/f/{token}"


# --- the erasure hook --------------------------------------------------------------------------


async def revoke_open_links(db: AsyncSession, customer_id: uuid.UUID) -> int:
    """Close every link still open for this client, in the caller's transaction. Returns how
    many. The public page also refuses a suppressed client's link on its own; this makes the
    link row say so as well, for the open-links list and for Task 4's submit."""
    result = await db.execute(
        update(FormLink)
        .where(
            FormLink.customer_id == customer_id,
            FormLink.revoked_at.is_(None),
            FormLink.consumed_at.is_(None),
        )
        .values(revoked_at=func.now())
    )
    return result.rowcount


# --- what goes over the wire -------------------------------------------------------------------


class SendableForm(BaseModel):
    template_id: str
    # The latest version's own name and kind — what the client will see.
    name: str
    kind: str
    version: int


class IssueIn(BaseModel):
    template_id: uuid.UUID


class IssuedLink(BaseModel):
    id: str
    # The only time the plaintext URL exists outside the email.
    url: str
    expires_at: datetime
    emailed_to: str | None


class OpenLink(BaseModel):
    id: str
    template_name: str
    version: int
    issued_at: datetime
    expires_at: datetime


class OpenLinks(BaseModel):
    links: list[OpenLink]


def _coded(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, "code": code})


def _latest_versions():
    return (
        select(FormTemplateVersion)
        .distinct(FormTemplateVersion.template_id)
        .order_by(FormTemplateVersion.template_id, FormTemplateVersion.number.desc())
    )


# --- routes ------------------------------------------------------------------------------------


@router.get("/forms/templates")
async def sendable_forms(_: Issuer, db: SessionDep) -> dict[str, list[SendableForm]]:
    """What the "Send form" dialog offers: every form that is published and not retired, at
    its latest version. Staff Mode's read of the templates; the builder stays `forms.manage`."""
    latest = _latest_versions().subquery()
    rows = await db.execute(
        select(latest.c.template_id, latest.c.name, latest.c.kind, latest.c.number)
        .join(FormTemplate, FormTemplate.id == latest.c.template_id)
        .where(FormTemplate.retired_at.is_(None))
        .order_by(func.lower(latest.c.name))
    )
    return {
        "templates": [
            SendableForm(template_id=str(t), name=n, kind=k, version=v) for t, n, k, v in rows
        ]
    }


@router.post("/customers/{customer_id}/form-links", status_code=201)
async def issue_link(
    customer_id: uuid.UUID, payload: IssueIn, issuer: Issuer, db: SessionDep
) -> IssuedLink:
    # `FOR KEY SHARE`, like booking: waits for an erasure's `FOR UPDATE`, then sees its
    # suppression — so a link cannot be issued in the gap before its revocation commits.
    customer = await db.scalar(
        select(Customer).where(Customer.id == customer_id).with_for_update(key_share=True)
    )
    if customer is None:
        raise HTTPException(status_code=404, detail="No such customer.")
    if customer.suppressed_at is not None:
        return _coded(
            422, "customer_suppressed", "This client asked to be erased; forms cannot be sent."
        )
    template = await db.get(FormTemplate, payload.template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="No such form.")
    if template.retired_at is not None:
        return _coded(409, "template_retired", "This form is retired and cannot be sent.")
    version = await db.scalar(
        _latest_versions().where(FormTemplateVersion.template_id == template.id)
    )
    if version is None:
        return _coded(409, "template_unpublished", "This form has not been published yet.")

    token, token_digest = new_token()
    now = datetime.now(UTC)
    link = FormLink(
        id=uuid.uuid4(),
        token_sha256=token_digest,
        customer_id=customer.id,
        version_id=version.id,
        issued_by_user_id=issuer.id,
        issued_at=now,
        expires_at=expires_at(now),
    )
    db.add(link)
    record_event(
        db,
        "form.link_issued",
        target_type="form_link",
        target_id=str(link.id),
        actor_user_id=issuer.id,
        metadata={
            "customer_id": str(customer.id),
            "template_id": str(template.id),
            "version": version.number,
        },
    )
    email = customer.email
    await db.commit()

    url = url_of(token)
    if email:
        # After the commit, like the reset link: a fast worker must not send a link the
        # public page cannot find yet. Console until Phase 12, with no change here.
        send_email.delay(email, f"A form to fill in: {version.name}", _message(version.name, url))
    return IssuedLink(id=str(link.id), url=url, expires_at=link.expires_at, emailed_to=email)


def _message(form_name: str, url: str) -> str:
    return (
        f"You have been sent a form to fill in: {form_name}.\n\n"
        f"Open it here:\n{url}\n\n"
        "The link works once and expires in 48 hours. If you were not expecting this, you "
        "can ignore it."
    )


@router.get("/customers/{customer_id}/form-links")
async def open_links(customer_id: uuid.UUID, _: Issuer, db: SessionDep) -> OpenLinks:
    """Links still usable: not consumed, not revoked, not expired. Never the token."""
    rows = await db.execute(
        select(FormLink, FormTemplateVersion.name, FormTemplateVersion.number)
        .join(FormTemplateVersion, FormTemplateVersion.id == FormLink.version_id)
        .where(
            FormLink.customer_id == customer_id,
            FormLink.consumed_at.is_(None),
            FormLink.revoked_at.is_(None),
            FormLink.expires_at > func.now(),
        )
        .order_by(FormLink.issued_at.desc())
    )
    return OpenLinks(
        links=[
            OpenLink(
                id=str(link.id),
                template_name=name,
                version=number,
                issued_at=link.issued_at,
                expires_at=link.expires_at,
            )
            for link, name, number in rows
        ]
    )


@router.post("/customers/{customer_id}/form-links/{link_id}/revoke", status_code=204)
async def revoke_link(
    customer_id: uuid.UUID, link_id: uuid.UUID, issuer: Issuer, db: SessionDep
) -> Response:
    """One guarded `UPDATE`: an open link of *this* client, or 404."""
    revoked = await db.scalar(
        update(FormLink)
        .where(
            FormLink.id == link_id,
            FormLink.customer_id == customer_id,
            FormLink.revoked_at.is_(None),
            FormLink.consumed_at.is_(None),
        )
        .values(revoked_at=func.now())
        .returning(FormLink.id)
    )
    if revoked is None:
        raise HTTPException(status_code=404, detail="No open link.")
    record_event(
        db,
        "form.link_revoked",
        target_type="form_link",
        target_id=str(link_id),
        actor_user_id=issuer.id,
        metadata={"customer_id": str(customer_id)},
    )
    await db.commit()
    return Response(status_code=204)

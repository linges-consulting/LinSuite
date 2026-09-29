"""Bill review authority (#64): the two override paths for whatever staff can't resolve alone
on #63's bill review screen with a predefined, enabled discount.

**(a) Staff-request review** (`POST/GET .../override-requests`, `POST .../decision`): staff
submit an ad hoc discount or price override for admin/owner review; the admin/owner (in Admin
Mode, holding `billing.manage` — the exact `Requires(...)`/`AdminUser` machinery every other
administrative route already uses, nothing new) approves it as-is or revises it, and the
decision applies immediately so staff can resume ordinary billing with no second step.
`billing/models.py::BillOverrideRequest`'s own docstring has the schema design, including the
stale-approval guard this ticket's acceptance criteria centre on.

**(b) Inline admin edit** (`POST .../inline-admin/authenticate`, `PUT .../inline-admin/
override`, `GET`/`POST .../inline-admin/release`): an admin/owner authenticates with their own
credentials — the same password + `mfa.accept_code` verification `auth/login.py`'s Admin Mode
re-authentication already performs, not a second auth mechanism — directly on the *staff's own
session*, and edits the bill under their own identity. No session is minted and the staff
cookie never changes; a short Redis-held window (`_grant_inline_admin`, below) names which
admin authorized which bill, the same idle/hard-limit shape `auth/modes.py::grant_admin`
already uses (same settings, same TTL math), scoped to the bill rather than to a session `jti`
— "ends on save, on leaving the bill, or when the window expires" needs a place to hang
"leaving the bill" that a session-keyed grant would not have. One save consumes the grant
outright (simpler than sliding it, and "ends on save" literally does not need more than that);
an explicit release ends it early; the TTL ends it on its own otherwise.

**Both paths are independently gated by a business-setting toggle** (`enable_bill_override_
requests`/`enable_inline_admin_bill_edit` on `Business`, both default enabled), enforced here,
not only hidden by the frontend. Only *new* use is refused: `enable_bill_override_requests`
gates the submit route alone, never the decision route on an already-pending request, and
neither toggle ever touches #63's ordinary `bill_review.py` routes — an admin/owner's own
billing access from their own screen in Admin Mode is untouched by either flag (#54's
"Override configuration" decision, verbatim; `billing/models.py`'s module section above has
the full reasoning).
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select

from auth import mfa as mfa_mod
from auth import throttle
from auth.capabilities import Requires
from auth.models import User
from billing.bill_review import (
    BillOut,
    BillViewer,
    LineConflict,
    business_or_404,
    compute_bill,
    load_bill,
    persisted_selection,
    price_bill,
)
from billing.models import BillOverrideRequest
from core.audit import record_event
from core.config import get_settings
from core.db import SessionDep
from core.errors import (
    ACCOUNT_INACTIVE,
    BILL_OVERRIDE_REQUESTS_DISABLED,
    CAPABILITY_REQUIRED,
    INLINE_ADMIN_AUTHORITY_REQUIRED,
    INLINE_ADMIN_BILLING_DISABLED,
    INVALID_PASSWORD,
    Forbidden,
)
from core.redis import get_redis
from core.security import verify_password
from scheduling.models import Staff

router = APIRouter(prefix="/bills", tags=["billing"])

AdminReviewer = Annotated[User, Depends(Requires("billing.manage"))]

_OVERRIDE_REQUESTS_DISABLED = Forbidden(
    BILL_OVERRIDE_REQUESTS_DISABLED, "Staff override requests are turned off for this business."
)
_INLINE_ADMIN_DISABLED = Forbidden(
    INLINE_ADMIN_BILLING_DISABLED, "Inline admin editing is turned off for this business."
)
_NO_INLINE_AUTHORITY = Forbidden(
    INLINE_ADMIN_AUTHORITY_REQUIRED,
    "An admin/owner must authenticate on this bill before making this edit.",
)
_INLINE_AUTH_REFUSED = Forbidden(INVALID_PASSWORD, "Incorrect email or password")


# --- what goes over the wire -----------------------------------------------------------------


TaxConventionIn = Literal["inclusive", "exclusive"]
CommissionBasisIn = Literal["reduces", "absorbed"]


class OverrideRequestIn(BaseModel):
    kind: Literal["discount", "price_override"]
    requested_total_cents: Annotated[int, Field(ge=0)]
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    # Review R2/R5: the total is a tax-inclusive bill total (default, #64's meaning) or a
    # pre-tax amount; commission follows the revised price unless "absorbed" (spec §142).
    tax_convention: TaxConventionIn = "inclusive"
    commission_basis: CommissionBasisIn = "reduces"


class DecisionIn(BaseModel):
    decision: Literal["approved", "rejected"]
    # The admin/owner's revision. Omitted (or equal to the request's own ask) is "approved
    # as-is"; any other value is the revision the acceptance criteria call for.
    decided_total_cents: Annotated[int, Field(ge=0)] | None = None
    note: Annotated[str, Field(max_length=2000)] | None = None
    # Omitted = the request's own.
    tax_convention: TaxConventionIn | None = None
    commission_basis: CommissionBasisIn | None = None


class OverrideRequestOut(BaseModel):
    id: str
    bill_id: str
    kind: str
    reason: str
    requested_total_cents: int
    tax_convention: str
    requested_by_email: str
    requested_at: datetime
    bill_revision_as_of: datetime
    status: str
    decided_by_email: str | None
    decided_at: datetime | None
    decision_note: str | None
    decided_total_cents: int | None


class InlineAdminAuthIn(BaseModel):
    email: EmailStr
    password: str
    totp: str | None = None


class InlineAdminStatusOut(BaseModel):
    active: bool
    admin_email: str | None = None
    hard_limit_at: datetime | None = None


class InlineAdminEditIn(BaseModel):
    total_cents: Annotated[int, Field(ge=0)]
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    tax_convention: TaxConventionIn = "inclusive"
    commission_basis: CommissionBasisIn = "reduces"


# --- helpers -----------------------------------------------------------------------------------


async def _load_request(
    db: SessionDep, bill_id: uuid.UUID, request_id: uuid.UUID
) -> BillOverrideRequest:
    request = await db.get(BillOverrideRequest, request_id)
    if request is None or request.bill_id != bill_id:
        raise HTTPException(status_code=404, detail="No such override request.")
    return request


async def _email_of(db: SessionDep, user_id: uuid.UUID | None) -> str | None:
    if user_id is None:
        return None
    return await db.scalar(select(User.email).where(User.id == user_id))


async def _refuse_unbillable(db: SessionDep, bill) -> None:
    """An override the bill's lines cannot carry (below what package credits already
    prepaid, review R6) is refused before anything commits — nothing is persisted."""
    try:
        await price_bill(
            db, await business_or_404(db), bill, selected_ids=await persisted_selection(db, bill.id)
        )
    except LineConflict as error:
        raise HTTPException(status_code=422, detail=error.detail) from error


def _request_out(
    request: BillOverrideRequest, requested_by: str, decided_by: str | None
) -> OverrideRequestOut:
    return OverrideRequestOut(
        id=str(request.id),
        bill_id=str(request.bill_id),
        kind=request.kind,
        reason=request.reason,
        requested_total_cents=request.requested_total_cents,
        tax_convention=request.tax_convention,
        requested_by_email=requested_by,
        requested_at=request.requested_at,
        bill_revision_as_of=request.bill_revision_as_of,
        status=request.status,
        decided_by_email=decided_by,
        decided_at=request.decided_at,
        decision_note=request.decision_note,
        decided_total_cents=request.decided_total_cents,
    )


# --- (a) staff-request review -------------------------------------------------------------


@router.post("/{bill_id}/override-requests", status_code=201)
async def request_override(
    bill_id: uuid.UUID, payload: OverrideRequestIn, actor: BillViewer, db: SessionDep
) -> OverrideRequestOut:
    business = await business_or_404(db)
    if not business.enable_bill_override_requests:
        raise _OVERRIDE_REQUESTS_DISABLED
    bill = await load_bill(db, bill_id)
    if bill.status != "draft":
        raise HTTPException(
            status_code=422, detail="Only a draft bill can have an override requested."
        )

    request = BillOverrideRequest(
        bill_id=bill_id,
        requested_by=actor.id,
        kind=payload.kind,
        reason=payload.reason,
        requested_total_cents=payload.requested_total_cents,
        tax_convention=payload.tax_convention,
        commission_basis=payload.commission_basis,
        # The stale-approval guard's "as of" — the bill's own state at the moment of the ask.
        bill_revision_as_of=bill.updated_at,
    )
    db.add(request)
    await db.flush()
    record_event(
        db,
        "bill.override_requested",
        target_type="service_bill",
        target_id=str(bill_id),
        actor_user_id=actor.id,
        metadata={
            "request_id": str(request.id),
            "kind": payload.kind,
            "requested_total_cents": payload.requested_total_cents,
        },
    )
    await db.commit()
    return _request_out(request, actor.email, None)


@router.get("/{bill_id}/override-requests")
async def list_override_requests(
    bill_id: uuid.UUID, _: BillViewer, db: SessionDep
) -> dict[str, list[OverrideRequestOut]]:
    await load_bill(db, bill_id)
    requests = list(
        await db.scalars(
            select(BillOverrideRequest)
            .where(BillOverrideRequest.bill_id == bill_id)
            .order_by(BillOverrideRequest.requested_at.desc())
        )
    )
    out = []
    for request in requests:
        requested_by = await _email_of(db, request.requested_by) or "—"
        decided_by = await _email_of(db, request.decided_by)
        out.append(_request_out(request, requested_by, decided_by))
    return {"requests": out}


@router.post("/{bill_id}/override-requests/{request_id}/decision")
async def decide_override_request(
    bill_id: uuid.UUID,
    request_id: uuid.UUID,
    payload: DecisionIn,
    actor: AdminReviewer,
    db: SessionDep,
) -> OverrideRequestOut:
    """Reviewing and deciding is the admin/owner's own ordinary billing access, never gated
    by `enable_bill_override_requests` — that toggle refuses only *new* submissions (module
    docstring; #54's "Override configuration" decision). A stale request (`bill_revision_as_of`
    no longer matches `bill.updated_at`) is refused outright: the acceptance criterion this
    ticket exists for is that a stale approval must never authorize a *different* exceptional
    change than the one actually reviewed, and the simplest way to guarantee that is to never
    let one be decided at all — staff resubmits against the bill's current state instead."""
    bill = await load_bill(db, bill_id)
    request = await _load_request(db, bill_id, request_id)
    if request.status != "pending":
        raise HTTPException(status_code=409, detail="This request has already been decided.")
    if request.bill_revision_as_of != bill.updated_at:
        raise HTTPException(
            status_code=409,
            detail="The bill has changed since this request was made. Ask staff to resubmit.",
        )

    request.status = payload.decision
    request.decided_by = actor.id
    request.decided_at = datetime.now(UTC)
    request.decision_note = payload.note

    if payload.decision == "approved":
        decided = (
            payload.decided_total_cents
            if payload.decided_total_cents is not None
            else request.requested_total_cents
        )
        request.decided_total_cents = decided
        request.tax_convention = payload.tax_convention or request.tax_convention
        request.commission_basis = payload.commission_basis or request.commission_basis
        # Applied here, not in a second step: this *is* "staff can resume ordinary billing on
        # the same draft" (acceptance criterion) — there is no approved-but-unapplied state
        # left for a second stale window to open on.
        now = datetime.now(UTC)
        bill.manual_override_cents = decided
        bill.manual_override_reason = payload.note or request.reason
        bill.override_tax_convention = request.tax_convention
        bill.override_commission_basis = request.commission_basis
        bill.updated_at = now
        # #65's stale-approval checkpoint: the exact same instant as `updated_at` above, so
        # issue can tell "the bill hasn't moved since this override was authorized"
        # (`bill.override_applied_revision == bill.updated_at`) from "it has"
        # (a sibling appointment completing into this bill afterward bumps `updated_at` again
        # without touching this column).
        bill.override_applied_revision = now
        await _refuse_unbillable(db, bill)

    await db.flush()
    record_event(
        db,
        "bill.override_decided",
        target_type="service_bill",
        target_id=str(bill_id),
        actor_user_id=actor.id,
        metadata={"request_id": str(request.id), "decision": payload.decision},
    )
    await db.commit()
    requested_by = await _email_of(db, request.requested_by) or "—"
    return _request_out(request, requested_by, actor.email)


# --- (b) inline admin edit ------------------------------------------------------------------
#
# A short Redis-held window naming which admin authorized which bill — the same idle/hard-
# limit shape `auth/modes.py::grant_admin`/`read_state` already use (same settings, same TTL
# math), a new key namespace because this authority belongs to *a bill*, never to the staff
# session whose screen it was typed on. See the module docstring for the full reasoning.

_INLINE_ADMIN_PREFIX = "bill:inline_admin:"


def _inline_admin_key(bill_id: uuid.UUID) -> str:
    return f"{_INLINE_ADMIN_PREFIX}{bill_id}"


@dataclass(frozen=True)
class InlineAdminGrant:
    admin_user_id: uuid.UUID
    hard_limit_at: datetime


async def _grant_inline_admin(bill_id: uuid.UUID, admin_user_id: uuid.UUID) -> InlineAdminGrant:
    settings = get_settings()
    hard_limit = datetime.now(UTC) + timedelta(minutes=settings.admin_hard_limit_minutes)
    ttl = min(settings.admin_idle_minutes * 60, settings.admin_hard_limit_minutes * 60)
    value = json.dumps(
        {"admin_user_id": str(admin_user_id), "hard_limit_at": hard_limit.timestamp()}
    )
    await get_redis().set(_inline_admin_key(bill_id), value, ex=max(1, ttl))
    return InlineAdminGrant(admin_user_id=admin_user_id, hard_limit_at=hard_limit)


async def _read_inline_admin(bill_id: uuid.UUID) -> InlineAdminGrant | None:
    redis = get_redis()
    key = _inline_admin_key(bill_id)
    async with redis.pipeline(transaction=False) as pipe:
        pipe.get(key)
        pipe.ttl(key)
        raw, ttl = await pipe.execute()
    if raw is None or ttl is None or ttl <= 0:
        return None
    data = json.loads(raw)
    return InlineAdminGrant(
        admin_user_id=uuid.UUID(data["admin_user_id"]),
        hard_limit_at=datetime.fromtimestamp(data["hard_limit_at"], UTC),
    )


async def _end_inline_admin(bill_id: uuid.UUID) -> None:
    await get_redis().delete(_inline_admin_key(bill_id))


async def _inline_admin_status(db: SessionDep, bill_id: uuid.UUID) -> InlineAdminStatusOut:
    grant = await _read_inline_admin(bill_id)
    if grant is None:
        return InlineAdminStatusOut(active=False)
    email = await _email_of(db, grant.admin_user_id)
    return InlineAdminStatusOut(active=True, admin_email=email, hard_limit_at=grant.hard_limit_at)


@router.get("/{bill_id}/inline-admin")
async def read_inline_admin_status(
    bill_id: uuid.UUID, _: BillViewer, db: SessionDep
) -> InlineAdminStatusOut:
    await load_bill(db, bill_id)
    return await _inline_admin_status(db, bill_id)


@router.post("/{bill_id}/inline-admin/authenticate")
async def authenticate_inline_admin(
    bill_id: uuid.UUID, payload: InlineAdminAuthIn, _: BillViewer, db: SessionDep
) -> InlineAdminStatusOut:
    """The admin/owner's own credentials, verified exactly the way `auth/login.py`'s Admin
    Mode re-authentication verifies them — same password check, same `mfa.accept_code` second
    factor, same throttle — reached from the staff session's own request rather than a
    `/auth/login` call, because this must never replace the staff cookie or mint a session for
    the admin (module docstring: "no session is minted and the staff cookie never changes").
    """
    business = await business_or_404(db)
    if not business.enable_inline_admin_bill_edit:
        raise _INLINE_ADMIN_DISABLED
    await load_bill(db, bill_id)

    email = payload.email.lower()
    await throttle.guard(email)
    admin_user = await db.scalar(select(User).where(func.lower(User.email) == email))

    password_ok = await verify_password(
        admin_user.password_hash if admin_user else None, payload.password
    )
    if not password_ok:
        record_event(
            db,
            "login.failed",
            target_type="user",
            target_id=str(admin_user.id) if admin_user else None,
            actor_user_id=None,
            metadata={"email": email, "reason": "inline_bill_admin"},
        )
        locked = await throttle.record_failure(db, email, admin_user)
        await db.commit()
        raise locked or _INLINE_AUTH_REFUSED

    if "billing.manage" not in admin_user.capabilities:
        raise Forbidden(CAPABILITY_REQUIRED, "This account cannot make administrative bill edits.")
    if not await db.scalar(select(Staff.active).where(Staff.user_id == admin_user.id)):
        raise Forbidden(ACCOUNT_INACTIVE, "This account has been deactivated.")

    if admin_user.mfa_method is not None:
        if payload.totp is None:
            raise mfa_mod.TOTP_REQUIRED
        await mfa_mod.accept_code(db, admin_user, payload.totp, reason="inline_bill_admin")

    await throttle.clear(email)
    grant = await _grant_inline_admin(bill_id, admin_user.id)
    record_event(
        db,
        "bill.inline_admin_authenticated",
        target_type="service_bill",
        target_id=str(bill_id),
        actor_user_id=admin_user.id,
        metadata={"email": email},
    )
    await db.commit()
    return InlineAdminStatusOut(
        active=True, admin_email=admin_user.email, hard_limit_at=grant.hard_limit_at
    )


@router.post("/{bill_id}/inline-admin/release", status_code=204)
async def release_inline_admin(bill_id: uuid.UUID, _: BillViewer) -> None:
    """Ends inline authority on "leaving the bill" — the frontend calls this on navigating
    away. Never gated by the toggle: releasing authority is not new use of the path, and
    refusing it while the toggle happens to be off would strand a window nobody can end
    early."""
    await _end_inline_admin(bill_id)


@router.put("/{bill_id}/inline-admin/override")
async def apply_inline_admin_edit(
    bill_id: uuid.UUID, payload: InlineAdminEditIn, _: BillViewer, db: SessionDep
) -> BillOut:
    """The actual supervised edit. Gated by the Redis grant, not by the calling (staff)
    session's own capabilities — the whole point is that this authority belongs to whichever
    admin/owner authenticated, not to the session physically making the request. One save
    consumes the grant outright ("ends on save"): the acceptance criterion asks for an ending,
    not a budget of edits, and re-authenticating for a second correction is one password away.
    """
    business = await business_or_404(db)
    if not business.enable_inline_admin_bill_edit:
        raise _INLINE_ADMIN_DISABLED
    bill = await load_bill(db, bill_id)

    grant = await _read_inline_admin(bill_id)
    if grant is None:
        raise _NO_INLINE_AUTHORITY
    admin_user = await db.get(User, grant.admin_user_id)
    if admin_user is None or "billing.manage" not in admin_user.capabilities:
        # The window outlived the authority it granted — a role change mid-window, say.
        await _end_inline_admin(bill_id)
        raise _NO_INLINE_AUTHORITY

    now = datetime.now(UTC)
    bill.manual_override_cents = payload.total_cents
    bill.manual_override_reason = payload.reason
    bill.override_tax_convention = payload.tax_convention
    bill.override_commission_basis = payload.commission_basis
    bill.updated_at = now
    # #65's stale-approval checkpoint — see `decide_override_request`'s own comment above.
    bill.override_applied_revision = now
    await _refuse_unbillable(db, bill)
    await db.flush()

    record_event(
        db,
        "bill.inline_admin_edited",
        target_type="service_bill",
        target_id=str(bill_id),
        # The admin's identity, never the staff session's — the ticket's own attribution
        # requirement.
        actor_user_id=admin_user.id,
        metadata={"total_cents": payload.total_cents, "reason": payload.reason},
    )
    await db.commit()
    await _end_inline_admin(bill_id)

    selected_ids = await persisted_selection(db, bill_id)
    try:
        return await compute_bill(db, business, bill, selected_ids=selected_ids)
    except LineConflict as error:
        raise HTTPException(status_code=409, detail=error.detail) from error

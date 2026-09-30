"""Settings → Onboarding: the "Get your business ready" checklist (#116, spec #113).

A read of seven steps plus a dismiss action, both behind `Requires("admin")` — the same
Admin-Mode-only gate every other settings surface here already sits behind
(`auth/capabilities.py`: `admin` is `requires_admin_mode=True`). Every `done` flag is computed
from data that already lives somewhere; this file writes nothing except the dismissal
timestamp, which is why it is a router of its own rather than more fields on `settings/routes.py`
or `settings/notifications_routes.py`.

**Tax's rule (#118): confirmed, or already the owner's own.** Done when `businesses.
tax_confirmed_at` is set (the "Looks right" on the Tax step, `billing/tax_routes.py`), **or**
when at least one active `tax_components` row has `origin = 'manual'` — an owner who configured
tax by hand, whether before pre-fill ever ran or alongside it, has already taken responsibility
for it and is never asked to additionally click "Looks right" for rows they did not create
themselves. A business with only unconfirmed `'prefill'` rows, and nothing else, reads as not
done — spec story 24, "unreviewed rates are never assumed correct."

**Email reuses `notifications.providers.email_ready`** rather than re-deriving "a sender is
configured and its latest test send succeeded" — the one function the notifications panel and
the trigger layer already agree on. The gap that function depended on — a failed test send
leaving a stale `*_verified_at` from an earlier success — is fixed in
`settings/notifications_routes.py::send_test_email`, not here.

**Dismissal is shared, not per-user.** `businesses.onboarding_dismissed_at` lives on the one
business row (CLAUDE.md: single-tenant, one business), so any administrator dismissing it is
every administrator no longer seeing it — spec user story 9.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from auth.capabilities import Requires
from auth.models import User
from billing.models import TaxComponent, TaxComponentRate
from core.audit import record_event
from core.db import SessionDep
from core.models import Business
from notifications.providers import email_ready
from scheduling.models import Service, Staff, WorkingHours
from settings.models import LOGO, BrandingAsset

AdminCapability = Annotated[User, Depends(Requires("admin"))]

router = APIRouter(prefix="/admin", tags=["settings"], dependencies=[Depends(Requires("admin"))])


class OnboardingStep(BaseModel):
    key: str
    done: bool
    optional: bool = False


class OnboardingStatus(BaseModel):
    steps: list[OnboardingStep]
    dismissed_at: datetime | None


async def _business(db: SessionDep) -> Business:
    business = await db.scalar(select(Business).where(Business.id == 1))
    if business is None:
        raise HTTPException(status_code=404, detail="This instance has not been set up.")
    return business


def _business_details_done(business: Business) -> bool:
    """Story 12: an address with a province, and a phone number. Retention profile is part of
    the step's *screen*, not its `done` rule — it always has a default (`core/models.py`), so
    gating on it would mark this step undone for every fresh install regardless of what the
    owner actually entered."""
    return bool(business.address_line1 and business.province and business.phone)


async def _hours_done(db: SessionDep) -> bool:
    """At least one open day, anywhere — hours are per-staff (`working_hours.staff_id`, no
    business-wide matrix exists), and this is single-tenant, so any row at all answers it."""
    return await db.scalar(select(WorkingHours.id).limit(1)) is not None


async def _tax_done(db: SessionDep, business: Business) -> bool:
    if business.tax_confirmed_at is not None:
        return True
    row = await db.scalar(
        select(TaxComponent.id)
        .join(TaxComponentRate, TaxComponentRate.component_id == TaxComponent.id)
        .where(TaxComponent.active.is_(True), TaxComponent.origin == "manual")
        .limit(1)
    )
    return row is not None


async def _services_done(db: SessionDep) -> bool:
    return await db.scalar(select(Service.id).where(Service.active.is_(True)).limit(1)) is not None


async def _staff_done(db: SessionDep) -> bool:
    return await db.scalar(select(Staff.id).where(Staff.active.is_(True)).limit(1)) is not None


async def _branding_done(db: SessionDep) -> bool:
    return await db.get(BrandingAsset, LOGO) is not None


async def _steps(business: Business, db: SessionDep) -> list[OnboardingStep]:
    return [
        OnboardingStep(key="business", done=_business_details_done(business)),
        OnboardingStep(key="hours", done=await _hours_done(db)),
        OnboardingStep(key="tax", done=await _tax_done(db, business)),
        OnboardingStep(key="services", done=await _services_done(db)),
        OnboardingStep(key="staff", done=await _staff_done(db)),
        OnboardingStep(key="email", done=email_ready(business)),
        OnboardingStep(key="branding", done=await _branding_done(db), optional=True),
    ]


@router.get("/onboarding")
async def read_onboarding_status(db: SessionDep) -> OnboardingStatus:
    business = await _business(db)
    steps = await _steps(business, db)
    return OnboardingStatus(steps=steps, dismissed_at=business.onboarding_dismissed_at)


@router.post("/onboarding/dismiss")
async def dismiss_onboarding(admin: AdminCapability, db: SessionDep) -> OnboardingStatus:
    business = await _business(db)
    business.onboarding_dismissed_at = datetime.now(UTC)
    # Fact only (spec: "dismissing the checklist to be audited, so the record shows who
    # declared setup finished") — no metadata beyond who and when, both of which `record_event`
    # already carries as `actor_user_id`/`occurred_at`.
    record_event(
        db,
        "business.onboarding_dismissed",
        target_type="business",
        target_id=str(business.id),
        actor_user_id=admin.id,
    )
    await db.commit()
    steps = await _steps(business, db)
    return OnboardingStatus(steps=steps, dismissed_at=business.onboarding_dismissed_at)

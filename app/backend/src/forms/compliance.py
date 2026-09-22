"""Essential forms and compliance (Task 8, #51; PRD §2 "Compliance & Validation Flagging").

**One rule, read-time, in SQL** (pre-flight item 13: a background job is YAGNI until the query
is slow — there is no alerting stack to feed anyway). For client C and essential, non-retired
template T that *applies* to C (`applies_to_all`, or C has a confirmed appointment from today
onward for a service mapped to T — a cancelled appointment never counts), C is **compliant**
iff there is a submission for T — any version, because this asks about the template's
identity, not one of its versions — whose validity has not lapsed (`valid_for_months`, or
never) and no version of T with `requires_resignature` was published after that submission's
version. Only the most recent submission is considered: a client cannot file an older version
after a newer one exists through the normal link flow.

**"Essential" is `is_mandatory` on the latest published version**, not a new column — the
owner's ruling (progress.md) folds the "essential forms" concept into the flag Task 2 already
built and Task 8's builder settings panel already labels "Mandatory (essential form)". The
template-8 brief's migration sketch names a fourth column (`essential`); this module does not
add one, to avoid two ways to ask the same question drifting apart. `applies_to_all` and
`valid_for_months` *are* new — they describe *when* a form is required, which nothing else
already models.

**No N+1.** Both endpoints below share `_compliance`, which loads the essential-template
configuration, the service-mapped applicability and the latest submissions in one bulk query
each — never one query per client.

**Not a PHI route.** A status (`missing`/`expired`/`resign_required`) and a template name are
metadata (CLAUDE.md, docs/adr/0002 — "a list of which mandatory forms a client is missing is
metadata; do not return answers"), so neither endpoint here writes the access log.
"""

import calendar
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.capabilities import Requires
from auth.models import User
from core.db import SessionDep
from customers.models import Customer
from forms.models import FormSubmission, FormTemplate, FormTemplateService, FormTemplateVersion
from scheduling.models import Appointment
from scheduling.time_off import business_zone

Status = Literal["missing", "expired", "resign_required"]
MISSING: Status = "missing"
EXPIRED: Status = "expired"
RESIGN_REQUIRED: Status = "resign_required"
OK = "ok"

MAX_DAYS = 60


# --- S2: the date arithmetic, in the business timezone ----------------------------------------


def add_months(day: date, months: int) -> date:
    """`day`, `months` on, clamped to the shorter month — 31 Jan + 1 month is 28/29 Feb, never
    3 Mar. `scheduling.clock.localize` still does the DST-safe conversion to an instant; this
    is only the calendar-day arithmetic feeding it."""
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def valid_until(submitted_at: datetime, valid_for_months: int, zone: str) -> datetime:
    """The instant a submission's validity lapses: local midnight, `valid_for_months` after
    the submission's own local calendar day, converted back with `scheduling.clock.localize`
    (DST-safe — the whole reason this is business-local rather than "submitted_at + N*30d")."""
    from scheduling.clock import localize  # local import: avoids a forms → scheduling cycle

    if submitted_at.tzinfo is None:
        raise ValueError("submitted_at is an instant: pass an aware datetime")
    local_day = submitted_at.astimezone(ZoneInfo(zone)).date()
    return localize(datetime.combine(add_months(local_day, valid_for_months), time.min), zone)


def status_for(
    *,
    now: datetime,
    zone: str,
    valid_for_months: int | None,
    submission: tuple[int, datetime] | None,
    resign_after: int | None,
) -> Status | None:
    """`None` means compliant (`ok`) — the caller only keeps what is not. `submission` is
    (version number, submitted_at) of the client's most recent submission of this template, or
    `None` for none at all. `resign_after` is the highest version number of this template that
    `requires_resignature`, or `None` if no version ever has."""
    if submission is None:
        return MISSING
    number, submitted_at = submission
    if resign_after is not None and resign_after > number:
        return RESIGN_REQUIRED
    if valid_for_months is not None and now >= valid_until(submitted_at, valid_for_months, zone):
        return EXPIRED
    return None


# --- the bulk reads --------------------------------------------------------------------------


@dataclass(frozen=True)
class _EssentialTemplate:
    id: uuid.UUID
    name: str
    applies_to_all: bool
    valid_for_months: int | None
    service_ids: frozenset[uuid.UUID]
    resign_after: int | None


def _latest_versions():
    return (
        select(FormTemplateVersion)
        .distinct(FormTemplateVersion.template_id)
        .order_by(FormTemplateVersion.template_id, FormTemplateVersion.number.desc())
    )


async def _essential_templates(db: AsyncSession) -> list[_EssentialTemplate]:
    """Every non-retired template whose *latest published version* is mandatory — "essential"
    — with its identity-level settings and, per template, the highest version number that
    ever required re-signature."""
    latest = _latest_versions().subquery()
    resign = (
        select(
            FormTemplateVersion.template_id,
            func.max(FormTemplateVersion.number).label("resign_after"),
        )
        .where(FormTemplateVersion.requires_resignature.is_(True))
        .group_by(FormTemplateVersion.template_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(
                FormTemplate.id,
                latest.c.name,
                FormTemplate.applies_to_all,
                FormTemplate.valid_for_months,
                resign.c.resign_after,
            )
            .join(latest, latest.c.template_id == FormTemplate.id)
            .outerjoin(resign, resign.c.template_id == FormTemplate.id)
            .where(FormTemplate.retired_at.is_(None), latest.c.is_mandatory.is_(True))
        )
    ).all()
    if not rows:
        return []
    mapped = await db.execute(
        select(FormTemplateService.template_id, FormTemplateService.service_id).where(
            FormTemplateService.template_id.in_([r[0] for r in rows])
        )
    )
    services: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for template_id, service_id in mapped:
        services[template_id].add(service_id)
    return [
        _EssentialTemplate(
            id=template_id,
            name=name,
            applies_to_all=applies_to_all,
            valid_for_months=valid_for_months,
            service_ids=frozenset(services.get(template_id, ())),
            resign_after=resign_after,
        )
        for template_id, name, applies_to_all, valid_for_months, resign_after in rows
    ]


async def _applicable_services(
    db: AsyncSession, customer_ids: Sequence[uuid.UUID], service_ids: frozenset[uuid.UUID]
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """customer_id → the services (among `service_ids`) they have a *confirmed* appointment
    from today onward for. A cancelled, completed or no-show appointment never counts."""
    if not customer_ids or not service_ids:
        return {}
    rows = await db.execute(
        select(Appointment.customer_id, Appointment.service_id).where(
            Appointment.customer_id.in_(customer_ids),
            Appointment.service_id.in_(service_ids),
            Appointment.status == "confirmed",
            Appointment.starts_at >= func.now(),
        )
    )
    found: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for customer_id, service_id in rows:
        found[customer_id].add(service_id)
    return found


async def _latest_submissions(
    db: AsyncSession, customer_ids: Sequence[uuid.UUID], template_ids: Sequence[uuid.UUID]
) -> dict[tuple[uuid.UUID, uuid.UUID], tuple[int, datetime]]:
    """(customer_id, template_id) → (version number, submitted_at) of the most recent
    submission — any version, because compliance asks about the template's identity."""
    if not customer_ids or not template_ids:
        return {}
    rows = await db.execute(
        select(
            FormSubmission.customer_id,
            FormSubmission.template_id,
            FormTemplateVersion.number,
            FormSubmission.submitted_at,
        )
        .join(FormTemplateVersion, FormTemplateVersion.id == FormSubmission.version_id)
        .where(
            FormSubmission.customer_id.in_(customer_ids),
            FormSubmission.template_id.in_(template_ids),
        )
        .distinct(FormSubmission.customer_id, FormSubmission.template_id)
        .order_by(
            FormSubmission.customer_id,
            FormSubmission.template_id,
            FormSubmission.submitted_at.desc(),
        )
    )
    return {(c, t): (n, s) for c, t, n, s in rows}


class ComplianceEntry(BaseModel):
    template_id: str
    name: str
    status: Status


async def _compliance(
    db: AsyncSession, customer_ids: Sequence[uuid.UUID], *, now: datetime, zone: str
) -> dict[uuid.UUID, list[ComplianceEntry]]:
    """customer_id → the essential templates that apply to them and are not `ok`. Four bulk
    reads total, whatever `len(customer_ids)` is: the essential-template configuration, its
    service mapping, applicable appointments and the latest submissions."""
    templates = await _essential_templates(db)
    out: dict[uuid.UUID, list[ComplianceEntry]] = defaultdict(list)
    if not templates or not customer_ids:
        return out
    all_service_ids: frozenset[uuid.UUID] = frozenset().union(*(t.service_ids for t in templates))
    applicable = await _applicable_services(db, customer_ids, all_service_ids)
    submissions = await _latest_submissions(db, customer_ids, [t.id for t in templates])
    for customer_id in customer_ids:
        theirs = applicable.get(customer_id, set())
        for template in templates:
            if not (template.applies_to_all or theirs & template.service_ids):
                continue
            status = status_for(
                now=now,
                zone=zone,
                valid_for_months=template.valid_for_months,
                submission=submissions.get((customer_id, template.id)),
                resign_after=template.resign_after,
            )
            if status is not None:
                out[customer_id].append(
                    ComplianceEntry(template_id=str(template.id), name=template.name, status=status)
                )
    return out


# --- routes ------------------------------------------------------------------------------------

router = APIRouter(tags=["forms"])


class CustomerComplianceOut(BaseModel):
    templates: list[ComplianceEntry]


@router.get("/customers/{customer_id}/compliance")
async def customer_compliance(
    customer_id: uuid.UUID,
    _: Annotated[User, Depends(Requires("customers.view"))],
    db: SessionDep,
) -> CustomerComplianceOut:
    """The profile's alert banner: template names and statuses only — no `PHI_FIELDS`, so
    this is metadata (docs/adr/0002) and writes no access-log row."""
    zone = await business_zone(db)
    found = await _compliance(db, [customer_id], now=datetime.now(UTC), zone=str(zone))
    return CustomerComplianceOut(templates=found.get(customer_id, []))


class DashboardEntry(BaseModel):
    customer_id: str
    customer_name: str
    next_appointment_at: datetime
    templates: list[ComplianceEntry]


class ComplianceDashboardOut(BaseModel):
    clients: list[DashboardEntry]


@router.get("/forms/compliance")
async def compliance_dashboard(
    _: Annotated[User, Depends(Requires("forms.issue"))],
    db: SessionDep,
    days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = 14,
) -> ComplianceDashboardOut:
    """The "Forms needed" card: clients with a confirmed appointment in the next `days` who
    have any non-`ok` essential template, each listed once with their next appointment.
    Suppressed clients are excluded. Not customer-scoped and not PHI (see module docstring):
    no access-log row."""
    now = datetime.now(UTC)
    zone = await business_zone(db)
    horizon = now + timedelta(days=days)
    rows = await db.execute(
        select(
            Appointment.customer_id,
            func.min(Appointment.starts_at).label("next_at"),
            Customer.first_name,
            Customer.last_name,
        )
        .join(Customer, Customer.id == Appointment.customer_id)
        .where(
            Appointment.status == "confirmed",
            Appointment.starts_at >= now,
            Appointment.starts_at < horizon,
            Customer.suppressed_at.is_(None),
        )
        .group_by(Appointment.customer_id, Customer.first_name, Customer.last_name)
    )
    candidates = {c: (next_at, f"{first} {last}") for c, next_at, first, last in rows}
    compliance = await _compliance(db, list(candidates), now=now, zone=str(zone))
    clients = [
        DashboardEntry(
            customer_id=str(customer_id),
            customer_name=candidates[customer_id][1],
            next_appointment_at=candidates[customer_id][0],
            templates=entries,
        )
        for customer_id, entries in compliance.items()
    ]
    clients.sort(key=lambda c: c.next_appointment_at)
    return ComplianceDashboardOut(clients=clients)

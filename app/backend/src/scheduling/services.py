"""Settings → Services: what the business sells, and what delivering it needs.

**This is the availability engine's input** (tech-stack §19). A service carries the duration
and the two buffers that get slid across a staff member's free intervals, the set of staff
who may deliver it, and the rooms and devices whose free intervals it has to be intersected
with. `GET /catalog/services` is the shape Task 14 reads and Task 15's booking screen picks
from — active services only, and reachable by anybody signed in, because booking is staff
work rather than administration. Everything under `/admin/services` is `catalog.manage`,
which the registry marks administrative, so an Admin Mode window is required on top of the
capability and `Requires` applies that without this module mentioning it.

**Two sets, each replaced whole** — eligible staff and resource requirements. Both are what a
screen saves as one decision (the same argument as the weekly matrix in `hours.py`): a
per-row API would let a half-applied set exist between two requests, and a service that is
briefly deliverable by nobody is a booking screen that is briefly wrong.

**The rules the database is not asked to enforce.** That a named requirement's `kind` agrees
with its resource's, and that the resource is still active, are checked here — the second is
a rule no constraint can express (a resource deactivated tomorrow must not retroactively
invalidate a requirement written today), and splitting the pair across two mechanisms would
mean two places to look when one of them lets something through. The five-minute steps, the
non-negative price and the unique name *are* the database's, mirrored here only so the
caller gets a sentence instead of a 500.

**Money is integer cents** (CLAUDE.md, tech-stack §21). The screen shows dollars and converts
before it gets here.
"""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from auth.capabilities import Requires
from auth.models import User
from auth.session import CurrentUser
from core.audit import record_event
from core.db import SessionDep
from scheduling._admin_forms import blank_to_none, refuse, refuse_emptied_field
from scheduling.models import (
    MIN_DURATION,
    MINUTE_STEP,
    Resource,
    Service,
    ServiceRequirement,
    ServiceStaff,
    Staff,
)

router = APIRouter(prefix="/admin/services", tags=["services"])
# The read side. Its own router rather than a route on the one above, because the prefix is
# what says who it is for: `/admin` is administration, `/catalog` is the shop floor.
public = APIRouter(prefix="/catalog", tags=["services"])

CatalogManager = Annotated[User, Depends(Requires("catalog.manage"))]

Kind = Literal["space", "equipment"]


# --- what goes over the wire ------------------------------------------------------------


class RequirementOut(BaseModel):
    """One thing delivering this service needs. `resource_id` null is "any active resource of
    this kind"; `resource_name` is carried so a screen that cannot list resources — booking
    holds no `catalog.manage` — still has something to print."""

    kind: Kind
    resource_id: str | None
    resource_name: str | None


class CatalogServiceOut(BaseModel):
    """The engine's shape (tech-stack §19): how long, how much turnaround either side, who
    may deliver it, what it needs. `active` is absent because everything here is active."""

    id: str
    name: str
    description: str | None
    duration_minutes: int
    buffer_before_minutes: int
    buffer_after_minutes: int
    price_cents: int
    bookable_online: bool
    sort_order: int
    staff_ids: list[str]
    requirements: list[RequirementOut]


class ServiceOut(CatalogServiceOut):
    """What the administrator's table draws — the engine's shape plus the one fact only this
    screen cares about."""

    active: bool


def _requirements(service: Service) -> list[RequirementOut]:
    """Spaces before equipment, "any" before a named one, then by name.

    A stable order, decided here rather than by the planner: the table prints this as one
    line of prose and a list that reshuffles between reloads reads as a change.
    """
    rows = sorted(
        service.requirements,
        key=lambda r: (
            r.kind != "space",
            r.resource_id is not None,
            r.resource and r.resource.name or "",
        ),
    )
    return [
        RequirementOut(
            kind=r.kind,
            resource_id=str(r.resource_id) if r.resource_id else None,
            resource_name=r.resource.name if r.resource else None,
        )
        for r in rows
    ]


def _staff_ids(service: Service) -> list[str]:
    # Sorted so the response is stable; the screen re-orders by its own roster anyway.
    return sorted(str(link.staff_id) for link in service.eligible_staff)


def _out(service: Service) -> ServiceOut:
    return ServiceOut(
        id=str(service.id),
        name=service.name,
        description=service.description,
        duration_minutes=service.duration_minutes,
        buffer_before_minutes=service.buffer_before_minutes,
        buffer_after_minutes=service.buffer_after_minutes,
        price_cents=service.price_cents,
        bookable_online=service.bookable_online,
        active=service.active,
        sort_order=service.sort_order,
        staff_ids=_staff_ids(service),
        requirements=_requirements(service),
    )


# --- what comes in ------------------------------------------------------------------------

Name = Annotated[str, Field(min_length=1, max_length=200)]
# The floor and the step are the model's, so a change lands in one place. `le` keeps a typo
# from becoming a service that fills a fortnight.
Duration = Annotated[int, Field(ge=MIN_DURATION, le=1440)]
Buffer = Annotated[int, Field(ge=0, le=1440)]
# Integer cents, never negative. Ten million dollars is not a service, it is a typo.
Price = Annotated[int, Field(ge=0, le=1_000_000_00)]


def _stepped(value: int | None) -> int | None:
    if value is not None and value % MINUTE_STEP:
        raise ValueError(f"times go in {MINUTE_STEP}-minute steps")
    return value


class ServiceFields(BaseModel):
    """Everything an administrator sets, on create and on edit alike. The two sets — who may
    deliver it, what it needs — are their own endpoints and never part of this body."""

    name: Name
    description: Annotated[str | None, Field(max_length=2000)] = None
    duration_minutes: Duration
    buffer_before_minutes: Buffer = 0
    buffer_after_minutes: Buffer = 0
    price_cents: Price = 0
    bookable_online: bool = True
    sort_order: int = 0

    @field_validator("name", mode="after")
    @classmethod
    def _real_name(cls, value: str) -> str:
        # `min_length` lets a string of spaces through, and " " is not a name.
        if not value.strip():
            raise ValueError("this cannot be blank")
        return value.strip()

    @field_validator("description", mode="after")
    @classmethod
    def _trimmed(cls, value: str | None) -> str | None:
        return blank_to_none(value)

    @field_validator("duration_minutes", "buffer_before_minutes", "buffer_after_minutes")
    @classmethod
    def _five_minute_steps(cls, value: int) -> int:
        return _stepped(value)


# The columns with no "absent" state. `None` on one of these is a request to delete a fact the
# row cannot be without — refused at the boundary, where the answer can name the field, rather
# than reaching the database as a NOT NULL violation and a 500. `active` is not here because
# it is not patchable at all: deactivate and reactivate are its only two doors.
_NOT_NULLABLE = (
    "name",
    "duration_minutes",
    "buffer_before_minutes",
    "buffer_after_minutes",
    "price_cents",
    "bookable_online",
    "sort_order",
)


class ServicePatch(BaseModel):
    """Every field optional, so "leave it alone" and "set it" can be told apart."""

    name: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    description: Annotated[str | None, Field(max_length=2000)] = None
    duration_minutes: Duration | None = None
    buffer_before_minutes: Buffer | None = None
    buffer_after_minutes: Buffer | None = None
    price_cents: Price | None = None
    bookable_online: bool | None = None
    sort_order: int | None = None

    @field_validator("name", "description", mode="after")
    @classmethod
    def _trimmed(cls, value: str | None) -> str | None:
        # A string of spaces passes `min_length` and is not a value. It becomes null, which
        # the handler then refuses for `name`.
        return blank_to_none(value)

    @field_validator("duration_minutes", "buffer_before_minutes", "buffer_after_minutes")
    @classmethod
    def _five_minute_steps(cls, value: int | None) -> int | None:
        return _stepped(value)


class EligibleStaffIn(BaseModel):
    """The whole set. Empty is a real answer — a service nobody is cleared for yet."""

    staff_ids: list[uuid.UUID] = []


class RequirementIn(BaseModel):
    """`resource_id` absent or null is "any active resource of this kind"."""

    kind: Kind
    resource_id: uuid.UUID | None = None


class RequirementsIn(BaseModel):
    requirements: list[RequirementIn] = []


def _duplicate(name: str) -> HTTPException:
    return HTTPException(status_code=409, detail=f"A service named “{name}” already exists.")


# --- reading ------------------------------------------------------------------------------


async def _roster(db: SessionDep, *, include_inactive: bool) -> list[Service]:
    rows = await db.scalars(
        select(Service)
        .where(*([] if include_inactive else [Service.active]))
        .order_by(Service.active.desc(), Service.sort_order, Service.name)
    )
    return list(rows)


@router.get("")
async def list_services(
    _: CatalogManager, db: SessionDep, include_inactive: bool = False
) -> dict[str, list[ServiceOut]]:
    return {"services": [_out(s) for s in await _roster(db, include_inactive=include_inactive)]}


@public.get("/services")
async def catalog(_: CurrentUser, db: SessionDep) -> dict[str, list[CatalogServiceOut]]:
    """What the availability engine and the booking screen read.

    Any signed-in account, no capability: refusing this to somebody holding `schedule.manage`
    would mean a booking screen with nothing to book. Inactive services are absent rather
    than flagged — nothing downstream has a use for one, and leaving them in would make
    every caller filter for itself.
    """
    services = await _roster(db, include_inactive=False)
    return {
        "services": [CatalogServiceOut(**_out(s).model_dump(exclude={"active"})) for s in services]
    }


# --- creating -----------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_service(
    payload: ServiceFields, admin: CatalogManager, db: SessionDep
) -> ServiceOut:
    service = Service(**payload.model_dump(), active=True)
    db.add(service)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise _duplicate(payload.name) from None

    record_event(
        db,
        "service.created",
        target_type="service",
        target_id=str(service.id),
        actor_user_id=admin.id,
        metadata={"name": service.name, "duration_minutes": service.duration_minutes},
    )
    await db.commit()
    return _out(await _load(db, service.id))


# --- editing ------------------------------------------------------------------------------


@router.patch("/{service_id}")
async def update_service(
    service_id: uuid.UUID, payload: ServicePatch, admin: CatalogManager, db: SessionDep
) -> ServiceOut:
    service = await _load(db, service_id)
    sent = payload.model_dump(exclude_unset=True)

    # Before anything is set — see `_admin_forms.refuse_emptied_field`.
    refuse_emptied_field(sent, _NOT_NULLABLE)

    changed = []
    for field, value in sent.items():
        if getattr(service, field) != value:
            setattr(service, field, value)
            changed.append(field)

    if changed:
        # Read before the flush that might fail it: a rolled-back session expires every
        # attribute on the object, and re-reading one afterwards is a second round trip this
        # transaction no longer has.
        name = service.name
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            raise _duplicate(name) from None
        # Field names, not values — what an audit needs, without a second copy of the data.
        # The price *change* is the interesting one here, and it is already in `services`:
        # what protects last month's takings is Task 15 snapshotting the price onto the
        # appointment, not this log.
        record_event(
            db,
            "service.updated",
            target_type="service",
            target_id=str(service.id),
            actor_user_id=admin.id,
            metadata={"changed": sorted(changed)},
        )
    await db.commit()
    return _out(await _load(db, service_id))


# --- who may deliver it, and what it needs ---------------------------------------------------


@router.put("/{service_id}/staff")
async def replace_eligible_staff(
    service_id: uuid.UUID, payload: EligibleStaffIn, admin: CatalogManager, db: SessionDep
) -> ServiceOut:
    """The whole set, in one transaction. Nothing is half-saved."""
    service = await _load(db, service_id)
    # Order preserved, repeats collapsed: the same person listed twice is one eligibility,
    # and the composite key would otherwise refuse the whole save over a UI slip.
    wanted = list(dict.fromkeys(payload.staff_ids))

    if wanted:
        found = {
            row.id: row.active
            for row in await db.execute(select(Staff.id, Staff.active).where(Staff.id.in_(wanted)))
        }
        if missing := [s for s in wanted if s not in found]:
            raise refuse("staff_ids", f"No such staff member: {missing[0]}.")
        if any(not found[s] for s in wanted):
            raise refuse(
                "staff_ids",
                "That staff member is deactivated. Restore them before assigning work.",
            )

    # DELETE then INSERT rather than reassigning the collection: the same pair may be on both
    # sides of the save, and a mapper-ordered insert-before-delete would collide on the
    # composite key. One transaction, so a refusal leaves the old set exactly where it was.
    await db.execute(delete(ServiceStaff).where(ServiceStaff.service_id == service_id))
    db.add_all([ServiceStaff(service_id=service_id, staff_id=s) for s in wanted])
    await db.flush()

    record_event(
        db,
        "service.staff_replaced",
        target_type="service",
        target_id=str(service.id),
        actor_user_id=admin.id,
        # A count, not the ids: the set is already stored, and copying it into the audit log
        # would be a second copy with a different retention horizon.
        metadata={"staff": len(wanted)},
    )
    await db.commit()
    return _out(await _load(db, service_id))


@router.put("/{service_id}/requirements")
async def replace_requirements(
    service_id: uuid.UUID, payload: RequirementsIn, admin: CatalogManager, db: SessionDep
) -> ServiceOut:
    """The whole set. "Any treatment room plus laser unit 2" is two rows saved together."""
    service = await _load(db, service_id)
    wanted = list(dict.fromkeys((r.kind, r.resource_id) for r in payload.requirements))

    named = [resource_id for _, resource_id in wanted if resource_id is not None]
    if named:
        found = {
            row.id: row
            for row in await db.execute(
                select(Resource.id, Resource.kind, Resource.active).where(Resource.id.in_(named))
            )
        }
        for kind, resource_id in wanted:
            if resource_id is None:
                continue
            resource = found.get(resource_id)
            if resource is None:
                raise refuse("requirements", f"No such resource: {resource_id}.")
            if resource.kind != kind:
                raise refuse(
                    "requirements",
                    f"That resource is {resource.kind}, not {kind}.",
                )
            if not resource.active:
                raise refuse(
                    "requirements",
                    "That resource is deactivated. Restore it before requiring it.",
                )

    await db.execute(delete(ServiceRequirement).where(ServiceRequirement.service_id == service_id))
    db.add_all(
        [
            ServiceRequirement(service_id=service_id, kind=kind, resource_id=resource_id)
            for kind, resource_id in wanted
        ]
    )
    await db.flush()

    record_event(
        db,
        "service.requirements_replaced",
        target_type="service",
        target_id=str(service.id),
        actor_user_id=admin.id,
        metadata={"requirements": len(wanted)},
    )
    await db.commit()
    return _out(await _load(db, service_id))


# --- leaving, and coming back ---------------------------------------------------------------


@router.post("/{service_id}/deactivate")
async def deactivate_service(
    service_id: uuid.UUID, admin: CatalogManager, db: SessionDep
) -> ServiceOut:
    return await _set_active(db, service_id, admin, active=False)


@router.post("/{service_id}/reactivate")
async def reactivate_service(
    service_id: uuid.UUID, admin: CatalogManager, db: SessionDep
) -> ServiceOut:
    return await _set_active(db, service_id, admin, active=True)


async def _set_active(
    db: SessionDep, service_id: uuid.UUID, admin: User, *, active: bool
) -> ServiceOut:
    """No hard delete, ever: an appointment that already claimed this service must never
    point at nothing. The eligible staff and the requirements are kept too — a service that
    comes back should come back as it was, not as a blank."""
    service = await _load(db, service_id)
    if service.active == active:
        return _out(service)

    service.active = active
    record_event(
        db,
        "service.reactivated" if active else "service.deactivated",
        target_type="service",
        target_id=str(service.id),
        actor_user_id=admin.id,
        metadata={"name": service.name},
    )
    await db.commit()
    return _out(await _load(db, service_id))


async def _load(db: SessionDep, service_id: uuid.UUID) -> Service:
    """The service with both its sets, always freshly read.

    `populate_existing` rather than `db.get`, and it is load-bearing rather than tidy. The
    sessions here are `expire_on_commit=False`, so `get` hands back whatever is already in the
    identity map without touching the database — which after a `DELETE`/`INSERT` of one of the
    sets is the collection as it was *before* the save, and for a service this request just
    created is a collection that was never loaded at all. An unloaded collection on an async
    session is not a second query, it is a `MissingGreenlet`.
    """
    service = await db.scalar(
        select(Service).where(Service.id == service_id).execution_options(populate_existing=True)
    )
    if service is None:
        raise HTTPException(status_code=404, detail="No such service.")
    return service

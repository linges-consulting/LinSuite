"""Settings → Resources: the spaces and equipment services are delivered in and with.

Everything here is `catalog.manage`, which the registry marks administrative — so an Admin
Mode window is required on top of the capability, and `Requires` applies that without this
module mentioning it (same shape as `scheduling/staff.py`).

**One table, one router, two kinds.** A space and a piece of equipment carry the same facts
and are managed the same way; splitting them into two routers would double every handler for
no rule that actually differs. `kind` is fixed at creation and never patched — turning a room
into a laser is not a rename, it is a different resource.

**No hard delete (tech-stack §15, §20).** `POST /{id}/deactivate` and `/reactivate` are the
only way this row's story ends or resumes; a historical appointment that claimed it must
never point at nothing. Later tickets' pickers read `active` to leave it off a list — this
router never refuses a write because of it.

**Duplicate names are a 409, per kind.** `ux_resources_kind_name` (migration 0010) is the
database's half of the rule; this module's half is turning the `IntegrityError` it raises
into an answer that names the field before the caller has to guess.
"""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from auth.capabilities import Requires
from auth.models import User
from core.audit import record_event
from core.db import SessionDep
from scheduling._admin_forms import blank_to_none, known_colour, refuse_emptied_field
from scheduling.models import Resource

router = APIRouter(prefix="/admin/resources", tags=["resources"])

ResourceManager = Annotated[User, Depends(Requires("catalog.manage"))]

Kind = Literal["space", "equipment"]


# --- what goes over the wire ------------------------------------------------------------


class ResourceOut(BaseModel):
    id: str
    kind: Kind
    name: str
    description: str | None
    colour: str | None
    active: bool
    sort_order: int


def _out(resource: Resource) -> ResourceOut:
    return ResourceOut(
        id=str(resource.id),
        kind=resource.kind,
        name=resource.name,
        description=resource.description,
        colour=resource.colour,
        active=resource.active,
        sort_order=resource.sort_order,
    )


# --- what comes in ------------------------------------------------------------------------

Name = Annotated[str, Field(min_length=1, max_length=200)]


class ResourceFields(BaseModel):
    """Everything an administrator sets, on create and on edit alike — `kind` excepted,
    which is only ever chosen once."""

    name: Name
    description: Annotated[str | None, Field(max_length=2000)] = None
    colour: str | None = None
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

    @field_validator("colour")
    @classmethod
    def _colour(cls, value: str | None) -> str | None:
        return known_colour(value)


class ResourceCreate(ResourceFields):
    kind: Kind


# The two columns with no "absent" state. `None` on either is a request to delete a fact the
# row cannot be without — refused at the boundary, where the answer can name the field,
# rather than reaching the database as a NOT NULL violation and a 500.
_NOT_NULLABLE = ("name", "sort_order")


class ResourcePatch(BaseModel):
    """Every field optional, so "leave it alone" and "set it" can be told apart. `kind` is
    not here at all — turning a space into equipment is not an edit this screen offers."""

    name: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    description: Annotated[str | None, Field(max_length=2000)] = None
    colour: str | None = None
    sort_order: int | None = None

    @field_validator("name", "description", mode="after")
    @classmethod
    def _trimmed(cls, value: str | None) -> str | None:
        # A string of spaces passes `min_length` and is not a value. It becomes null, which
        # the handler then refuses for `name` — the one required field here.
        return blank_to_none(value)

    @field_validator("colour")
    @classmethod
    def _colour(cls, value: str | None) -> str | None:
        return known_colour(value)


def _duplicate(kind: str, name: str) -> HTTPException:
    return HTTPException(status_code=409, detail=f"A {kind} named “{name}” already exists.")


# --- reading ------------------------------------------------------------------------------


@router.get("")
async def list_resources(
    _: ResourceManager, db: SessionDep, kind: Kind | None = None, include_inactive: bool = False
) -> dict[str, list[ResourceOut]]:
    clauses = [] if include_inactive else [Resource.active]
    if kind is not None:
        clauses.append(Resource.kind == kind)
    rows = await db.scalars(
        select(Resource)
        .where(*clauses)
        .order_by(Resource.kind, Resource.active.desc(), Resource.sort_order, Resource.name)
    )
    return {"resources": [_out(r) for r in rows]}


# --- creating -----------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_resource(
    payload: ResourceCreate, admin: ResourceManager, db: SessionDep
) -> ResourceOut:
    resource = Resource(
        kind=payload.kind,
        name=payload.name,
        description=payload.description,
        colour=payload.colour,
        sort_order=payload.sort_order,
        active=True,
    )
    db.add(resource)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise _duplicate(payload.kind, payload.name) from None

    record_event(
        db,
        "resource.created",
        target_type="resource",
        target_id=str(resource.id),
        actor_user_id=admin.id,
        metadata={"kind": resource.kind, "name": resource.name},
    )
    await db.commit()
    return _out(resource)


# --- editing ------------------------------------------------------------------------------


@router.patch("/{resource_id}")
async def update_resource(
    resource_id: uuid.UUID, payload: ResourcePatch, admin: ResourceManager, db: SessionDep
) -> ResourceOut:
    resource = await _load(db, resource_id)
    sent = payload.model_dump(exclude_unset=True)

    # Before anything is set — see `_admin_forms.refuse_emptied_field`.
    refuse_emptied_field(sent, _NOT_NULLABLE)

    changed = []
    for field, value in sent.items():
        if getattr(resource, field) != value:
            setattr(resource, field, value)
            changed.append(field)

    if changed:
        # Read before the flush that might fail it: a rolled-back session expires every
        # attribute on the object, and re-reading one afterwards is a second round trip this
        # transaction no longer has.
        kind, name = resource.kind, resource.name
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            raise _duplicate(kind, name) from None
        # Field names, not values — what an audit needs, without a second copy of the data.
        record_event(
            db,
            "resource.updated",
            target_type="resource",
            target_id=str(resource.id),
            actor_user_id=admin.id,
            metadata={"changed": sorted(changed)},
        )
    await db.commit()
    return _out(resource)


# --- leaving, and coming back ---------------------------------------------------------------


@router.post("/{resource_id}/deactivate")
async def deactivate_resource(
    resource_id: uuid.UUID, admin: ResourceManager, db: SessionDep
) -> ResourceOut:
    resource = await _load(db, resource_id)
    if not resource.active:
        return _out(resource)

    resource.active = False
    record_event(
        db,
        "resource.deactivated",
        target_type="resource",
        target_id=str(resource.id),
        actor_user_id=admin.id,
        metadata={"kind": resource.kind, "name": resource.name},
    )
    await db.commit()
    return _out(resource)


@router.post("/{resource_id}/reactivate")
async def reactivate_resource(
    resource_id: uuid.UUID, admin: ResourceManager, db: SessionDep
) -> ResourceOut:
    resource = await _load(db, resource_id)
    if resource.active:
        return _out(resource)

    resource.active = True
    record_event(
        db,
        "resource.reactivated",
        target_type="resource",
        target_id=str(resource.id),
        actor_user_id=admin.id,
        metadata={"kind": resource.kind, "name": resource.name},
    )
    await db.commit()
    return _out(resource)


async def _load(db: SessionDep, resource_id: uuid.UUID) -> Resource:
    resource = await db.get(Resource, resource_id)
    if resource is None:
        raise HTTPException(status_code=404, detail="No such resource.")
    return resource

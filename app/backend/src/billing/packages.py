"""Settings → Packages & bundles (#60): admin-defined prepaid credit definitions.

Definitions only — no purchase flow (#71) and no redemption (#72) yet, so this module is
just the CRUD surface, the same "settings-panel pattern" `scheduling/services.py` already
established for the catalog (diff/`record_event`/commit backend). `billing.manage` gates all
of it, the same shape as `catalog.manage`: Administrator-only, Admin Mode required.

**A definition always names at least one service.** Unlike a freshly created `Service`, which
may briefly have no eligible staff, a package or bundle with zero services is not a smaller
version of a real one — it is not representable as a real one at all, so `services` is
required (and non-emptiable) on both create and the whole-set replace, rather than allowed to
start empty and be filled in afterward.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from auth.capabilities import Requires
from auth.models import User
from billing._admin_forms import blank_to_none, refuse, refuse_emptied_field
from billing.models import PackageDefinition, PackageDefinitionService
from billing.tax_routes import TaxConvention, normalize_tax_component_keys
from core.audit import record_event
from core.db import SessionDep
from scheduling.models import Service

router = APIRouter(prefix="/admin/packages", tags=["packages"])

BillingManager = Annotated[User, Depends(Requires("billing.manage"))]


# --- what goes over the wire ----------------------------------------------------------------


class DefinitionServiceOut(BaseModel):
    service_id: str
    service_name: str
    service_active: bool
    credits: int


class PackageDefinitionOut(BaseModel):
    id: str
    name: str
    description: str | None
    price_cents: int
    expires_after_days: int | None
    transferable: bool
    # Review R1/R2: the tax components this package toggles on, and its price convention.
    tax_component_keys: list[str]
    tax_convention: str
    active: bool
    services: list[DefinitionServiceOut]


# --- what comes in -------------------------------------------------------------------------

Name = Annotated[str, Field(min_length=1, max_length=200)]
# Integer cents, never negative. Ten million dollars is not a package, it is a typo — the
# same ceiling `scheduling/services.py` puts on a service's price.
Price = Annotated[int, Field(ge=0, le=1_000_000_00)]
Credits = Annotated[int, Field(ge=1, le=9999)]
ExpiresAfterDays = Annotated[int, Field(ge=1, le=3650)]


class DefinitionServiceIn(BaseModel):
    service_id: uuid.UUID
    credits: Credits = 1


class PackageDefinitionFields(BaseModel):
    """Everything an administrator sets, on create and on edit alike — `services` excepted,
    which is its own endpoint (`PUT /{id}/services`), exactly as `scheduling.services
    .ServiceFields` keeps eligible staff and resource requirements off the scalar body."""

    name: Name
    description: Annotated[str | None, Field(max_length=2000)] = None
    price_cents: Price
    expires_after_days: ExpiresAfterDays | None = None
    transferable: bool = False
    tax_component_keys: list[str] = []
    tax_convention: TaxConvention = "exclusive"

    @field_validator("name", mode="after")
    @classmethod
    def _real_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("this cannot be blank")
        return value.strip()

    @field_validator("description", mode="after")
    @classmethod
    def _trimmed(cls, value: str | None) -> str | None:
        return blank_to_none(value)


class PackageDefinitionCreate(PackageDefinitionFields):
    """The scalar fields plus the whole `services` set — a package or bundle with none is not
    representable at all (see the module docstring), so unlike editing, creation carries both
    in one body."""

    services: Annotated[list[DefinitionServiceIn], Field(min_length=1)]

    @field_validator("services", mode="after")
    @classmethod
    def _no_repeats(cls, value: list[DefinitionServiceIn]) -> list[DefinitionServiceIn]:
        seen = {row.service_id for row in value}
        if len(seen) != len(value):
            raise ValueError("the same service cannot appear twice in one definition")
        return value


# `expires_after_days` and `description` are deliberately absent: both are naturally nullable
# (an explicit `null` is "no expiry" / "clear the note"), so a PATCH sending `null` for either
# must reach the row, not be refused here. `active` is not patchable at all — deactivate and
# reactivate are its only two doors.
_NOT_NULLABLE = ("name", "price_cents", "transferable", "tax_component_keys", "tax_convention")


class PackageDefinitionPatch(BaseModel):
    """Every field optional, so "leave it alone" and "set it" can be told apart."""

    name: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    description: Annotated[str | None, Field(max_length=2000)] = None
    price_cents: Price | None = None
    expires_after_days: ExpiresAfterDays | None = None
    transferable: bool | None = None
    tax_component_keys: list[str] | None = None
    tax_convention: TaxConvention | None = None

    @field_validator("name", "description", mode="after")
    @classmethod
    def _trimmed(cls, value: str | None) -> str | None:
        return blank_to_none(value)


class DefinitionServicesIn(BaseModel):
    """The whole set. At least one — see the module docstring."""

    services: Annotated[list[DefinitionServiceIn], Field(min_length=1)]

    @field_validator("services", mode="after")
    @classmethod
    def _no_repeats(cls, value: list[DefinitionServiceIn]) -> list[DefinitionServiceIn]:
        seen = {row.service_id for row in value}
        if len(seen) != len(value):
            raise ValueError("the same service cannot appear twice in one definition")
        return value


def _duplicate(name: str) -> HTTPException:
    return HTTPException(status_code=409, detail=f"A package named “{name}” already exists.")


# The unique index the migration creates on `lower(name)`.
_NAME_INDEX = "ux_package_definitions_name"


def _is_duplicate_name(error: IntegrityError) -> bool:
    """See `scheduling.services._is_duplicate_name` — same reasoning, same shape."""
    for candidate in (error.orig, getattr(error.orig, "__cause__", None)):
        if getattr(candidate, "constraint_name", None) == _NAME_INDEX:
            return True
    return _NAME_INDEX in str(error.orig)


# --- reading ---------------------------------------------------------------------------------


async def _service_names(db: SessionDep, ids: set[uuid.UUID]) -> dict[uuid.UUID, tuple[str, bool]]:
    if not ids:
        return {}
    rows = await db.execute(
        select(Service.id, Service.name, Service.active).where(Service.id.in_(ids))
    )
    return {row.id: (row.name, row.active) for row in rows}


async def _out(db: SessionDep, definition: PackageDefinition) -> PackageDefinitionOut:
    names = await _service_names(db, {row.service_id for row in definition.services})
    services = [
        DefinitionServiceOut(
            service_id=str(row.service_id),
            service_name=names[row.service_id][0],
            service_active=names[row.service_id][1],
            credits=row.credits,
        )
        # By service name: the same stable-order argument `scheduling.services._requirements`
        # makes — a list that reshuffles between reloads reads as a change nobody made.
        for row in sorted(definition.services, key=lambda r: names[r.service_id][0])
    ]
    return PackageDefinitionOut(
        id=str(definition.id),
        name=definition.name,
        description=definition.description,
        price_cents=definition.price_cents,
        expires_after_days=definition.expires_after_days,
        transferable=definition.transferable,
        tax_component_keys=list(definition.tax_component_keys),
        tax_convention=definition.tax_convention,
        active=definition.active,
        services=services,
    )


@router.get("")
async def list_package_definitions(
    _: BillingManager, db: SessionDep, include_inactive: bool = False
) -> dict[str, list[PackageDefinitionOut]]:
    rows = await db.scalars(
        select(PackageDefinition)
        .where(*([] if include_inactive else [PackageDefinition.active]))
        .order_by(PackageDefinition.active.desc(), PackageDefinition.name)
    )
    return {"packages": [await _out(db, row) for row in rows]}


# --- creating --------------------------------------------------------------------------------


async def _validate_services(db: SessionDep, services: list[DefinitionServiceIn]) -> None:
    found = await _service_names(db, {row.service_id for row in services})
    for row in services:
        entry = found.get(row.service_id)
        if entry is None:
            raise refuse("services", f"No such service: {row.service_id}.")
        if not entry[1]:
            raise refuse(
                "services",
                f"“{entry[0]}” is deactivated. Reactivate it before including it in a package.",
            )


@router.post("", status_code=201)
async def create_package_definition(
    payload: PackageDefinitionCreate, admin: BillingManager, db: SessionDep
) -> PackageDefinitionOut:
    await _validate_services(db, payload.services)

    fields = payload.model_dump(exclude={"services"})
    fields["tax_component_keys"] = await normalize_tax_component_keys(
        db, payload.tax_component_keys
    )
    definition = PackageDefinition(**fields, active=True)
    definition.services = [
        PackageDefinitionService(service_id=row.service_id, credits=row.credits)
        for row in payload.services
    ]
    db.add(definition)
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        if not _is_duplicate_name(error):
            raise
        raise _duplicate(payload.name) from None

    record_event(
        db,
        "package_definition.created",
        target_type="package_definition",
        target_id=str(definition.id),
        actor_user_id=admin.id,
        metadata={"name": definition.name, "services": len(payload.services)},
    )
    await db.commit()
    return await _out(db, await _load(db, definition.id))


# --- editing ---------------------------------------------------------------------------------


@router.patch("/{definition_id}")
async def update_package_definition(
    definition_id: uuid.UUID, payload: PackageDefinitionPatch, admin: BillingManager, db: SessionDep
) -> PackageDefinitionOut:
    definition = await _load(db, definition_id)
    sent = payload.model_dump(exclude_unset=True)

    refuse_emptied_field(sent, _NOT_NULLABLE)
    if "tax_component_keys" in sent:
        sent["tax_component_keys"] = await normalize_tax_component_keys(
            db, sent["tax_component_keys"]
        )

    changed = []
    for field, value in sent.items():
        if getattr(definition, field) != value:
            setattr(definition, field, value)
            changed.append(field)

    if changed:
        name = definition.name
        try:
            await db.flush()
        except IntegrityError as error:
            await db.rollback()
            if not _is_duplicate_name(error):
                raise
            raise _duplicate(name) from None
        record_event(
            db,
            "package_definition.updated",
            target_type="package_definition",
            target_id=str(definition.id),
            actor_user_id=admin.id,
            metadata={"changed": sorted(changed)},
        )
    await db.commit()
    return await _out(db, await _load(db, definition_id))


@router.put("/{definition_id}/services")
async def replace_services(
    definition_id: uuid.UUID, payload: DefinitionServicesIn, admin: BillingManager, db: SessionDep
) -> PackageDefinitionOut:
    """The whole set, in one transaction. Nothing is half-saved."""
    definition = await _load(db, definition_id)
    await _validate_services(db, payload.services)

    await db.execute(
        delete(PackageDefinitionService).where(
            PackageDefinitionService.package_definition_id == definition_id
        )
    )
    db.add_all(
        [
            PackageDefinitionService(
                package_definition_id=definition_id, service_id=row.service_id, credits=row.credits
            )
            for row in payload.services
        ]
    )
    await db.flush()

    record_event(
        db,
        "package_definition.services_replaced",
        target_type="package_definition",
        target_id=str(definition.id),
        actor_user_id=admin.id,
        metadata={"services": len(payload.services)},
    )
    await db.commit()
    return await _out(db, await _load(db, definition_id))


# --- leaving, and coming back -----------------------------------------------------------------


@router.post("/{definition_id}/deactivate")
async def deactivate_package_definition(
    definition_id: uuid.UUID, admin: BillingManager, db: SessionDep
) -> PackageDefinitionOut:
    return await _set_active(db, definition_id, admin, active=False)


@router.post("/{definition_id}/reactivate")
async def reactivate_package_definition(
    definition_id: uuid.UUID, admin: BillingManager, db: SessionDep
) -> PackageDefinitionOut:
    return await _set_active(db, definition_id, admin, active=True)


async def _set_active(
    db: SessionDep, definition_id: uuid.UUID, admin: User, *, active: bool
) -> PackageDefinitionOut:
    """No hard delete, ever: a customer already holding credits against this definition must
    still be able to redeem them. Only new sales of it stop."""
    definition = await _load(db, definition_id)
    if definition.active == active:
        return await _out(db, definition)

    definition.active = active
    record_event(
        db,
        "package_definition.reactivated" if active else "package_definition.deactivated",
        target_type="package_definition",
        target_id=str(definition.id),
        actor_user_id=admin.id,
        metadata={"name": definition.name},
    )
    await db.commit()
    return await _out(db, await _load(db, definition_id))


async def _load(db: SessionDep, definition_id: uuid.UUID) -> PackageDefinition:
    """See `scheduling.services._load` for why `populate_existing` rather than `db.get`: the
    session is `expire_on_commit=False`, and a `DELETE`/`INSERT` of the services set must be
    re-read, not served from the identity map as it stood before the save."""
    definition = await db.scalar(
        select(PackageDefinition)
        .where(PackageDefinition.id == definition_id)
        .execution_options(populate_existing=True)
    )
    if definition is None:
        raise HTTPException(status_code=404, detail="No such package.")
    return definition

"""Custom roles: the administrator's surface onto the capability registry.

Everything here is `roles.manage`, which the registry marks administrative — so an Admin Mode
window is required on top of the capability, and `Requires` applies that without this module
mentioning it.

**The two guards, and why they are refusals rather than warnings.** A system role cannot be
edited or deleted, and nothing may leave this instance with nobody who can administer it.
Both protect against the same accident, which has no undo: there is no second way into a
single-tenant deployment, so an administrator who removes the last administrative role has
locked the business out of its own software until somebody opens a database console.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, exists, func, select, text
from sqlalchemy.exc import IntegrityError

from auth.capabilities import CAPABILITIES, Requires, unknown
from auth.models import Role, RoleCapability, User
from core.audit import record_event
from core.db import SessionDep
from scheduling.models import Staff

router = APIRouter(prefix="/admin", tags=["admin"])

RolesManager = Annotated[User, Depends(Requires("roles.manage"))]

# What an administrator needs to undo their own last change. A role holding `admin` but not
# `roles.manage` can reach Admin Mode and not the screen that hands out capabilities, which
# is a lockout with extra steps.
ADMIN_FLOOR = ("admin", "roles.manage")

# The advisory-lock key that serialises every check of the floor above. An arbitrary
# constant, chosen once: what matters is only that every caller of
# `assert_an_administrator_remains` names the same number, since two callers using different
# keys would not exclude each other and the guard would be back to being skewable.
_ADMIN_FLOOR_LOCK = 0x11B5_017E  # "linsuite" floor, as a memorable constant

_SYSTEM_ROLE_EDIT = (
    "Administrator and Staff are built-in roles, so what they can do is fixed. "
    "Create a role of your own to choose its capabilities."
)
_SYSTEM_ROLE_DELETE = (
    "Administrator and Staff are built-in roles and cannot be deleted. Every account needs "
    "a role, and these two are what a new instance starts with."
)


class CapabilityOut(BaseModel):
    key: str
    description: str
    group: str
    requires_admin_mode: bool


class RoleOut(BaseModel):
    id: str
    name: str
    description: str
    is_system: bool
    capabilities: list[str]
    # How many accounts hold it — what the interface disables the delete button from, and
    # the reason it can give for doing so.
    user_count: int

    @classmethod
    def of(cls, role: Role, user_count: int) -> "RoleOut":
        return cls(
            id=str(role.id),
            name=role.name,
            description=role.description,
            capabilities=sorted(role.capability_keys),
            is_system=role.is_system,
            user_count=user_count,
        )


def _validate(keys: list[str]) -> list[str]:
    strange = unknown(keys)
    if strange:
        # Shaped like FastAPI's own 422 so the frontend reads one error format. A capability
        # nothing checks would render as a granted permission and be refused everywhere.
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "type": "value_error",
                    "loc": ["body", "capabilities"],
                    "msg": f"Not a capability: {', '.join(strange)}",
                }
            ],
        )
    return sorted(set(keys))


class RoleIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=200)
    capabilities: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a role needs a name")
        return value.strip()


class RolePatch(BaseModel):
    """Every field optional: a rename and a capability change are separate acts, and sending
    the whole role to do one of them would let a stale screen silently undo the other."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=200)
    capabilities: list[str] | None = None


async def _counts(db: SessionDep) -> dict[uuid.UUID, int]:
    rows = await db.execute(select(User.role_id, func.count()).group_by(User.role_id))
    return dict(rows.all())


async def _load(db: SessionDep, role_id: uuid.UUID) -> Role:
    role = await db.scalar(select(Role).where(Role.id == role_id))
    if role is None:
        raise HTTPException(status_code=404, detail="No such role.")
    return role


async def assert_an_administrator_remains(db: SessionDep) -> None:
    """Refuse a change that would leave nobody able to administer this instance.

    Called after the change is staged and before it is committed, so the question asked is
    "would this be true afterwards" rather than "was it true before" — which is the only
    version that catches the change actually being made.

    It counts capabilities rather than membership of a role named `Administrator`. A business
    that renames the job, or splits it across a custom role, is doing something legitimate;
    what must never happen is the capability itself going unheld.

    **Why the lock.** This is a write skew, and READ COMMITTED does not prevent it: two
    requests demoting two *different* administrators touch no common row, so neither blocks
    the other, and each counts the other's administrator as still administering. Both pass,
    both commit, and the instance is left with nobody who can administer it — a state that
    has no way back on a single-tenant deployment short of a database console.

    The count is over rows the change does not touch, so there is nothing to `SELECT ... FOR
    UPDATE`: the conflict is with a row that must keep *not* changing. A transaction-scoped
    advisory lock is the thing that serialises check-and-write here, and it is released by
    commit or rollback, so no path has to remember to give it back.
    """
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADMIN_FLOOR_LOCK})
    await db.flush()

    def holds(capability: str):
        return exists().where(
            RoleCapability.role_id == User.role_id, RoleCapability.capability == capability
        )

    # Joined to `staff`, because a deactivated administrator cannot sign in and so cannot
    # administer anything. Counting them would let the last one be deactivated — a lockout
    # this guard exists to make impossible, arriving through a different door.
    remaining = await db.scalar(
        select(func.count())
        .select_from(User)
        .join(Staff, Staff.user_id == User.id)
        .where(Staff.active, *[holds(c) for c in ADMIN_FLOOR])
    )
    if not remaining:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "This would leave nobody who can administer this instance. Give another "
                "account an Administrator role first."
            ),
        )


@router.get("/capabilities")
async def list_capabilities(_: RolesManager) -> dict[str, list[CapabilityOut]]:
    """The registry, so the interface draws a toggle per capability with its own description
    rather than a hard-coded list that drifts from the one the server enforces."""
    return {"capabilities": [CapabilityOut(**vars(c)) for c in CAPABILITIES]}


@router.get("/roles")
async def list_roles(_: RolesManager, db: SessionDep) -> dict[str, list[RoleOut]]:
    roles = await db.scalars(select(Role).order_by(Role.is_system.desc(), Role.name))
    counts = await _counts(db)
    return {"roles": [RoleOut.of(r, counts.get(r.id, 0)) for r in roles]}


@router.post("/roles", status_code=201)
async def create_role(payload: RoleIn, admin: RolesManager, db: SessionDep) -> RoleOut:
    capabilities = _validate(payload.capabilities)
    role = Role(name=payload.name, description=payload.description, is_system=False)
    role.capabilities = [RoleCapability(capability=c) for c in capabilities]
    db.add(role)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="A role with that name already exists."
        ) from None

    record_event(
        db,
        "role.created",
        target_type="role",
        target_id=str(role.id),
        actor_user_id=admin.id,
        metadata={"name": role.name, "capabilities": capabilities},
    )
    await db.commit()
    return RoleOut.of(role, 0)


@router.patch("/roles/{role_id}")
async def update_role(
    role_id: uuid.UUID, payload: RolePatch, admin: RolesManager, db: SessionDep
) -> RoleOut:
    role = await _load(db, role_id)
    if role.is_system:
        raise HTTPException(status_code=409, detail=_SYSTEM_ROLE_EDIT)

    if payload.name is not None:
        role.name = payload.name.strip()
    if payload.description is not None:
        role.description = payload.description
    if payload.capabilities is not None:
        wanted = _validate(payload.capabilities)
        # Replace the collection rather than the rows: `delete-orphan` turns the difference
        # into the DELETEs and INSERTs that actually changed.
        role.capabilities = [RoleCapability(capability=c) for c in wanted]

    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="A role with that name already exists."
        ) from None

    await assert_an_administrator_remains(db)
    record_event(
        db,
        "role.updated",
        target_type="role",
        target_id=str(role.id),
        actor_user_id=admin.id,
        metadata={"name": role.name, "capabilities": sorted(role.capability_keys)},
    )
    await db.commit()
    return RoleOut.of(role, (await _counts(db)).get(role.id, 0))


@router.delete("/roles/{role_id}", status_code=204)
async def delete_role(role_id: uuid.UUID, admin: RolesManager, db: SessionDep) -> None:
    role = await _load(db, role_id)
    if role.is_system:
        raise HTTPException(status_code=409, detail=_SYSTEM_ROLE_DELETE)

    held_by = (await _counts(db)).get(role.id, 0)
    if held_by:
        # The database says the same thing through ON DELETE RESTRICT; this is the version
        # that can name the number, which is what the administrator needs in order to act.
        raise HTTPException(
            status_code=409,
            detail=(
                f"{held_by} account{'s' if held_by != 1 else ''} still "
                f"{'have' if held_by != 1 else 'has'} this role. Move them to another role first."
            ),
        )

    name = role.name
    await db.execute(delete(Role).where(Role.id == role_id))
    record_event(
        db,
        "role.deleted",
        target_type="role",
        target_id=str(role_id),
        actor_user_id=admin.id,
        metadata={"name": name},
    )
    try:
        await db.commit()
    except IntegrityError:
        # The count above and this delete are two statements, so somebody can be assigned
        # the role in between. `ON DELETE RESTRICT` then refuses, and without this the
        # administrator would get a 500 for losing a race — the same answer as the counted
        # case is the honest one, since the fact is identical: somebody holds it.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Somebody was given this role a moment ago. Move them off it and try again.",
        ) from None

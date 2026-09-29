"""The PHI access log's one writer: a per-route dependency (ADR-0002 §2, §3).

**Declared on the route, never inferred.** Middleware that guessed which responses carry PHI
would silently miss the next endpoint; a dependency in the route definition is a decision
somebody can see in a diff, and `tests/test_access_log.py` enumerates the app's routes so
a customer-scoped GET without one is a failing build rather than an audit finding.

**Log before read.** The row is flushed *and committed* before the handler runs, so no PHI
is ever returned without a row. Two consequences, both intended: a 404 for an unknown id
still leaves a record that somebody tried to open it, which is exactly what an investigation
wants to know; and if the insert fails — a missing partition, a lost connection — the
exception propagates, the handler never runs, and the request is a 500 with no PHI in it.
Fail closed: ADR-0002 says a dropped entry is precisely the entry that matters, so a read the
log cannot record is a read that does not happen.

**`Requires` runs first.** Route dependencies resolve in the order they are declared, so a
route declares `[Depends(Requires("customers.view")), Depends(LogAccess(...))]` and a 401 or
403 never reaches this — a refused request is not an access.

**Why this lives in `core/` and imports `auth`.** Forms and session notes will declare the
same dependency, so it sits beside `core.audit` rather than in `customers/`. It is the one
place `core/` reaches into a domain module: the actor *is* the session, and there is no
access to log without one.
"""

import uuid
from collections.abc import Callable
from typing import Any, get_args

from fastapi import Request
from pydantic import BaseModel
from sqlalchemy import select

from auth.session import CurrentUser
from core.db import SessionDep
from core.models import AccessLogEntry

VIEW = "view"

# Field names that make a response PHI, declared once so every route's response model can be
# checked against the same list (`tests/test_access_log.py`, rule a). Not every field named
# "notes" is PHI — `Appointment.notes` is a front-desk booking note, not a customer's chart —
# so this check is only ever applied to customer-scoped routes; see `Mounted.customer_scoped`
# in the test. Extend this set, never bypass the routes it gates.
PHI_FIELDS: frozenset[str] = frozenset(
    {
        "date_of_birth",
        "emergency_contact_name",
        "emergency_contact_phone",
        "emergency_contact_relationship",
        "secondary_contact_name",
        "secondary_contact_phone",
        "secondary_contact_email",
        "notes",
        # The retention hold: derived from the date of birth, so it discloses it (Task 4).
        "retention",
        # An erasure's hold: its end date and the sentence stating it disclose the DOB too.
        "held_until",
        "held_reason",
        # A completed form's answers — signature included (#47).
        "answers",
        "annotations",
    }
)


def response_phi_fields(model: type | None, _seen: frozenset[type] | None = None) -> frozenset[str]:
    """Every name in `PHI_FIELDS` that `model` actually puts on the wire — its own fields,
    and recursively through nested models, lists, dicts and `Optional`/`Union` wrappers. A
    route's `response_model` is what FastAPI actually serialises; a field the handler happens
    to read but never returns is not a disclosure, so this walks the model, not the code.
    """
    if model is None:
        return frozenset()
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        # `list[NoteOut]`, `NoteOut | None`, `dict[str, NoteOut]`: walk the wrapper too.
        return _phi_fields_of_annotation(model, _seen or frozenset())
    seen = (_seen or frozenset()) | {model}
    found: set[str] = set()
    for name, field in model.model_fields.items():
        if name in PHI_FIELDS:
            found.add(name)
        found |= _phi_fields_of_annotation(field.annotation, seen)
    return frozenset(found)


def _phi_fields_of_annotation(annotation: object, seen: frozenset[type]) -> frozenset[str]:
    """Drills into `list[X]`, `dict[K, V]`, `X | None` and nested models; a plain type with
    no type arguments (`str`, `UUID`, `Literal["a", "b"]`) contributes nothing further."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return frozenset() if annotation in seen else response_phi_fields(annotation, seen)
    found: set[str] = set()
    for arg in get_args(annotation):
        found |= _phi_fields_of_annotation(arg, seen)
    return frozenset(found)


def declares_a_model(annotation: object) -> bool:
    """Whether a response annotation contains a Pydantic model anywhere — the only shape
    `response_phi_fields` can read field names off. `None`, `dict`, `dict[str, str]`, `Any`
    and `list[dict]` do not: what they carry is invisible to the check."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return True
    return any(declares_a_model(arg) for arg in get_args(annotation))


def LogAccess(resource_type: str, resource_param: str | None = None) -> Callable:  # noqa: N802
    """`dependencies=[..., Depends(LogAccess("customer_profile"))]` on a route whose path
    carries `{customer_id}`. One row, `action="view"`, committed before the handler runs.

    `resource_param` names another path parameter to record as `resource_id` — a submission
    id rather than the client's (pre-flight C3). The handler must then check that the resource
    belongs to `{customer_id}`, or the row names the wrong chart. A dependency factory, like
    `Requires`."""

    async def dependency(
        customer_id: uuid.UUID, user: CurrentUser, request: Request, db: SessionDep
    ) -> None:
        resource_id = str(request.path_params[resource_param]) if resource_param else None
        await _record(db, user, request, customer_id, resource_type, resource_id)

    # What the route-enumeration test looks for.
    dependency.phi_resource = resource_type
    return dependency


async def _record(
    db: SessionDep,
    user: CurrentUser,
    request: Request,
    customer_id: uuid.UUID,
    resource_type: str,
    resource_id: str | None,
) -> None:
    """The one insert every factory here shares: flushed and committed before the handler."""
    db.add(
        AccessLogEntry(
            actor_user_id=user.id,
            actor_role=user.role.name,
            customer_id=customer_id,
            resource_type=resource_type,
            resource_id=resource_id or str(customer_id),
            action=VIEW,
            # The caller's address, provided uvicorn trusts the proxy's forwarded header
            # (`FORWARDED_ALLOW_IPS` on the `app` service). None only where there is
            # truly no socket; httpx's `ASGITransport` supplies a loopback address
            # (127.0.0.1) by default, so the test suite exercises a real value here too.
            ip=request.client.host if request.client else None,
        )
    )
    await db.commit()


def LogAccessOf(resource_type: str, resource_param: str, owner: Any) -> Callable:  # noqa: N802
    """`LogAccess` for a record whose path has no `{customer_id}` — `/invoices/{invoice_id}`.
    `owner` is the record's customer column (`Invoice.customer_id`); the dependency reads it
    for the id in `resource_param` and logs against that client, still before the handler.

    No row when there is no client to name: an unknown or malformed id (the handler 404s or
    422s and discloses nothing) or an anonymous retail invoice (`customer_id` NULL). Declare
    the capability check before this, as with `LogAccess` — a refusal is not an access."""

    async def dependency(user: CurrentUser, request: Request, db: SessionDep) -> None:
        try:
            record_id = uuid.UUID(str(request.path_params[resource_param]))
        except ValueError:
            return
        customer_id = await db.scalar(select(owner).where(owner.class_.id == record_id))
        if customer_id is not None:
            await _record(db, user, request, customer_id, resource_type, str(record_id))

    dependency.phi_resource = resource_type
    return dependency


def LogAccessIfFiltered(resource_type: str) -> Callable:  # noqa: N802
    """For a list that narrows to one client's history with `?customer_id=`. Unfiltered it is
    a list render (ADR-0002 §4) and writes nothing; filtered, it is that client's financial
    history opened, and logs one row against them."""

    async def dependency(
        user: CurrentUser, request: Request, db: SessionDep, customer_id: uuid.UUID | None = None
    ) -> None:
        if customer_id is not None:
            await _record(db, user, request, customer_id, resource_type, None)

    dependency.phi_resource = resource_type
    return dependency

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

from fastapi import Request

from auth.session import CurrentUser
from core.db import SessionDep
from core.models import AccessLogEntry

VIEW = "view"


def LogAccess(resource_type: str) -> Callable:  # noqa: N802 — a dependency factory, like `Requires`
    """`dependencies=[..., Depends(LogAccess("customer_profile"))]` on a route whose path
    carries `{customer_id}`. One row, `action="view"`, committed before the handler runs."""

    async def dependency(
        customer_id: uuid.UUID, user: CurrentUser, request: Request, db: SessionDep
    ) -> None:
        db.add(
            AccessLogEntry(
                actor_user_id=user.id,
                actor_role=user.role.name,
                customer_id=customer_id,
                resource_type=resource_type,
                resource_id=str(customer_id),
                action=VIEW,
                # The caller's address, provided uvicorn trusts the proxy's forwarded header
                # (`FORWARDED_ALLOW_IPS` on the `app` service); None where there is no
                # socket, as under the ASGI test transport.
                ip=request.client.host if request.client else None,
            )
        )
        await db.commit()

    # What the route-enumeration test looks for.
    dependency.phi_resource = resource_type
    return dependency

"""Stock movements: the append-only ledger behind `ProductVariant.quantity_on_hand` (#61).

`record_movement` is the one writer. Every receiving delivery, sale, return and manual
adjustment goes through it, and it is also the one place CLAUDE.md's "Concurrency" section's
atomic non-negative guard lives: a single `UPDATE ... WHERE quantity_on_hand + :delta >= 0`
tests and applies the change together, so two concurrent sales for the last unit on the shelf
cannot both succeed — the row's own `UPDATE` is the lock, never an app-level one.

**No capability check here, deliberately.** #75 (sale-driven deduction, not yet built) will
call this directly with `kind="sale"`, the same shape `billing/completion.py::
record_draft_bill_line` is called from inside `complete_appointment` — never through a
capability-gated route. Enforcing `inventory.receive`/`inventory.adjust` is
`inventory/stock_routes.py`'s job, on the two routes that expose this to a human; the function
itself takes an actor id to record, not to authorize.
"""

import uuid
from typing import Literal

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from inventory.models import ProductVariant, StockMovement

MovementKind = Literal["receipt", "sale", "return", "adjustment"]


class VariantNotFound(LookupError):
    """No `ProductVariant` with this id (`record_movement`)."""


class InsufficientStock(Exception):
    """This movement would take `quantity_on_hand` negative (`record_movement`)."""


async def record_movement(
    db: AsyncSession,
    *,
    variant_id: uuid.UUID,
    kind: MovementKind,
    quantity_delta: int,
    actor_user_id: uuid.UUID | None,
    reason: str | None = None,
) -> ProductVariant:
    """Atomically apply `quantity_delta` to a variant's stock and record the movement that
    caused it.

    Raises `VariantNotFound` for an unknown variant, `InsufficientStock` when the delta would
    take `quantity_on_hand` negative, and `ValueError` for a zero delta or a manual adjustment
    given no reason — translating any of those into an HTTP response is the caller's job.

    Flushes but does not commit: a route calls this inside its own transaction, alongside its
    own `record_event`, and commits once — the same shape `record_draft_bill_line` follows.
    """
    if quantity_delta == 0:
        raise ValueError("quantity_delta must not be zero")
    if kind == "adjustment" and not reason:
        raise ValueError("reason is required for a manual adjustment")

    result = await db.execute(
        update(ProductVariant)
        .where(
            ProductVariant.id == variant_id,
            ProductVariant.quantity_on_hand + quantity_delta >= 0,
        )
        .values(quantity_on_hand=ProductVariant.quantity_on_hand + quantity_delta)
    )
    if result.rowcount == 0:
        variant = await db.get(ProductVariant, variant_id, populate_existing=True)
        if variant is None:
            raise VariantNotFound(str(variant_id))
        raise InsufficientStock(
            f"variant {variant_id} has {variant.quantity_on_hand} on hand, "
            f"cannot apply a delta of {quantity_delta}"
        )

    db.add(
        StockMovement(
            variant_id=variant_id,
            kind=kind,
            quantity_delta=quantity_delta,
            reason=reason,
            actor_user_id=actor_user_id,
        )
    )
    await db.flush()

    variant = await db.get(ProductVariant, variant_id, populate_existing=True)
    assert variant is not None  # the UPDATE above just matched this row
    return variant

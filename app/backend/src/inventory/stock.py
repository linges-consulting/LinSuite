"""Stock movements: the append-only ledger behind `ProductVariant.quantity_on_hand` (#61), and
the armed/already-alerted low-stock crossing that rides the same transaction (#62).

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

**The low-stock crossing (#62) piggybacks on the same row lock, not a second query.** The first
`UPDATE` above already serializes concurrent movements against this exact row: a second
concurrent caller's own `UPDATE` blocks on Postgres's row lock until the first commits, then
reads that committed `quantity_on_hand` *and* `low_stock_alerted`. So reading
`ProductVariant.low_stock_alerted`/`low_stock_threshold` back into Python here (via the
`db.get(..., populate_existing=True)` reload the ledger insert already needed) and deciding the
transition in plain Python is safe — nothing else can interleave and see or change this row
between that read and this function's own flush, because this session is still holding the
lock the first `UPDATE` took. Two concurrent sales that each independently look "crossing" in
isolation still serialize into "first one applies, second one observes it already alerted" —
exactly one `MovementResult.low_stock_alert_armed=True` per crossing.
"""

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from inventory.models import ProductVariant, StockMovement

MovementKind = Literal["receipt", "sale", "return", "adjustment"]


class VariantNotFound(LookupError):
    """No `ProductVariant` with this id (`record_movement`)."""


class InsufficientStock(Exception):
    """This movement would take `quantity_on_hand` negative (`record_movement`)."""


def is_below_threshold(quantity_on_hand: int, low_stock_threshold: int) -> bool:
    """Pure predicate (#62): is this variant currently in a low-stock state? Below its own
    threshold, strictly — sitting exactly at the threshold is still "enough to reorder at",
    not yet "below it". Shared by `record_movement`'s crossing check and
    `inventory/routes.py`'s always-shown in-app `is_low_stock` field, so the two can never
    disagree about what "low" means."""
    return quantity_on_hand < low_stock_threshold


@dataclass(frozen=True)
class MovementResult:
    """`record_movement`'s return: the variant as it now stands, and whether *this* call is
    the one that just armed a low-stock alert (#62) — `True` only on the exact call that flips
    `low_stock_alerted` from false to true, never on a later call that finds it already true.
    The caller (a route, or a future #75 sale transaction) checks this after its own commit and
    queues the email only then — see `notifications/triggers.py::notify_low_stock`."""

    variant: ProductVariant
    low_stock_alert_armed: bool


async def record_movement(
    db: AsyncSession,
    *,
    variant_id: uuid.UUID,
    kind: MovementKind,
    quantity_delta: int,
    actor_user_id: uuid.UUID | None,
    reason: str | None = None,
) -> MovementResult:
    """Atomically apply `quantity_delta` to a variant's stock, record the movement that caused
    it, and evaluate the low-stock crossing (#62) against the same locked row.

    Raises `VariantNotFound` for an unknown variant, `InsufficientStock` when the delta would
    take `quantity_on_hand` negative, and `ValueError` for a zero delta or a manual adjustment
    given no reason — translating any of those into an HTTP response is the caller's job.

    Flushes but does not commit: a route calls this inside its own transaction, alongside its
    own `record_event`, and commits once — the same shape `record_draft_bill_line` follows. The
    caller queues `notifications/triggers.py::notify_low_stock` itself, *after* that commit,
    when the returned `MovementResult.low_stock_alert_armed` is true — never from in here, since
    a task queued before the transaction actually commits could race a rollback (CLAUDE.md:
    Celery workers, never inline; ticket #62's own "queued after commit" requirement).
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

    # Still the same locked row (module docstring): `quantity_on_hand` above is this call's own
    # committed-within-transaction write; `low_stock_alerted` is whatever the last call to cross
    # this row left it at. Armed -> alerted is the only transition that queues a send.
    now_low = is_below_threshold(variant.quantity_on_hand, variant.low_stock_threshold)
    low_stock_alert_armed = now_low and not variant.low_stock_alerted
    if now_low != variant.low_stock_alerted:
        variant.low_stock_alerted = now_low
        await db.flush()

    return MovementResult(variant=variant, low_stock_alert_armed=low_stock_alert_armed)

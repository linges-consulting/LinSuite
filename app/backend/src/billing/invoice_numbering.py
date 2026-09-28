"""Gapless per-business invoice numbering (#65).

A per-business counter *row*, not a Postgres `SEQUENCE` — a `SEQUENCE` still burns a value on
a rolled-back transaction, which is exactly the gap CLAUDE.md's "Concurrency" section and this
ticket's own acceptance criterion ("gapless per business... survives concurrent issue attempts
without a collision or a gap") both rule out. `allocate_invoice_number` locks the row
(`SELECT ... FOR UPDATE`) and increments it in the *same* transaction as the caller's own
`Invoice` insert; if that transaction rolls back for any reason, the increment rolls back with
it, so the number that would have been burned is simply handed to the next caller instead. The
row lock is the serialization point for two concurrent issue attempts on the same business —
the same "the statement is the lock" philosophy `inventory/stock.py::record_movement` already
applies to `quantity_on_hand`.

Flushes but does not commit, the same shape `record_movement` and `billing/completion.py::
record_draft_bill_line` already follow: the caller holds the transaction and commits once,
alongside inserting the `Invoice` this number belongs to.
"""

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from billing.models import BusinessInvoiceCounter


async def allocate_invoice_number(db: AsyncSession, *, business_id: int = 1) -> int:
    """The next gapless invoice number for `business_id`, incrementing the counter row in the
    same transaction. Creates the row lazily (`ON CONFLICT DO NOTHING`, safe under concurrent
    first-ever-issue callers — whichever loses the insert race simply finds the row the other
    one made when it goes on to lock it) the same way `billing/keys.py::business_key` lazily
    creates `business_document_keys`' own single row (#55)."""
    await db.execute(
        pg_insert(BusinessInvoiceCounter)
        .values(business_id=business_id, next_number=1)
        .on_conflict_do_nothing(index_elements=["business_id"])
    )
    next_number = await db.scalar(
        select(BusinessInvoiceCounter.next_number)
        .where(BusinessInvoiceCounter.business_id == business_id)
        .with_for_update()
    )
    assert next_number is not None  # the insert above guarantees the row exists
    await db.execute(
        update(BusinessInvoiceCounter)
        .where(BusinessInvoiceCounter.business_id == business_id)
        .values(next_number=next_number + 1)
    )
    return next_number

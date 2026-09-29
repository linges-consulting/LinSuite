"""The current holder of a package purchase's credits (#111, spec #96): derived, never
stored — the `to` client of the purchase's *latest* `PackageTransfer`, or `package_purchases.
customer_id` (the purchaser, permanently) if it has none. One shared helper so checkout
eligibility (`billing/redemption.py::_eligible`), the client package-purchases list
(`billing/package_purchase.py::list_customer_package_purchases`) and the package-liability
report (`billing/package_liability.py::_report`) can never drift out of step on who holds a
purchase's credits — the exact drift `billing/package_transfer.py`'s own row lock exists to
prevent at write time, mirrored here at read time.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.engine import ScalarResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from billing.models import PackagePurchase, PackageTransfer


def current_holder_id() -> ColumnElement[uuid.UUID]:
    """A scalar subquery, correlated to whichever `PackagePurchase` row the enclosing query is
    iterating — drop this straight into a `select()`/`.where()`/`.join()` alongside
    `PackagePurchase`. Never evaluate this against a `PackagePurchase` that is not already in
    the same query's `FROM`."""
    latest_to = (
        select(PackageTransfer.to_customer_id)
        .where(PackageTransfer.package_purchase_id == PackagePurchase.id)
        .order_by(PackageTransfer.transferred_at.desc())
        .limit(1)
        .correlate(PackagePurchase)
        .scalar_subquery()
    )
    return func.coalesce(latest_to, PackagePurchase.customer_id)


async def holder_id_for(db: AsyncSession, purchase: PackagePurchase) -> uuid.UUID:
    """The plain scalar version, for a single purchase already loaded in Python (the transfer
    endpoint, which holds the purchase row lock and wants one value, not a correlated
    subquery)."""
    latest = await db.scalar(
        select(PackageTransfer.to_customer_id)
        .where(PackageTransfer.package_purchase_id == purchase.id)
        .order_by(PackageTransfer.transferred_at.desc())
        .limit(1)
    )
    return latest if latest is not None else purchase.customer_id


async def transfer_chain(db: AsyncSession, purchase_ids: list[uuid.UUID]) -> dict:
    """Every transfer row for `purchase_ids`, oldest first per purchase — the whole chain a
    purchase's history needs (`purchased by A -> transferred to B -> transferred to C`)."""
    if not purchase_ids:
        return {}
    rows: ScalarResult[PackageTransfer] = await db.scalars(
        select(PackageTransfer)
        .where(PackageTransfer.package_purchase_id.in_(purchase_ids))
        .order_by(PackageTransfer.package_purchase_id, PackageTransfer.transferred_at)
    )
    chains: dict[uuid.UUID, list[PackageTransfer]] = {}
    for row in rows:
        chains.setdefault(row.package_purchase_id, []).append(row)
    return chains

"""Discount definitions (#58, M4 spec #54 stories 18/19/21/25).

Admin-defined, reusable discounts: fixed-amount or percentage, eligible against all or
selected catalog items, a stackable flag, and a commission-basis choice. This module is the
definition only — no route applies a discount to a real bill yet (#63's job); the pure
resolver that will do the amount math lives in `billing/discount_resolver.py`, deliberately
apart from these ORM classes so it stays importable with no database at all.

**Percentages reuse `Staff.commission_rate_services_bp`'s convention** (`scheduling/models.
py:101-102`) rather than inventing a second one: an integer 0-10000 in basis points, CHECK'd
the same way.

**No hard delete**, the same rule every other catalog-like row in this app follows (`Service`,
`Staff`, `Resource`): an already-issued invoice line will need to keep pointing at the
discount that applied to it (once #63/#65 exist), so `enabled` going false is the only way a
discount stops being offered — the row, and its resolved history, never disappear.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base

DISCOUNT_KINDS = ("percentage", "fixed")
ELIGIBILITY_SCOPES = ("all", "selected")
COMMISSION_BASES = ("reduces", "absorbed")
# The three catalog families a discount can be scoped to (#54). Not a foreign key on any of
# them — `products`/`packages` don't exist in this branch yet (#56/#60 are siblings in the
# same wave), and even once they do, this follows `AuditEvent.target_type`/`target_id`'s own
# precedent: a polymorphic reference across domains that must never block a `catalog.manage`
# admin from deactivating an item a discount happens to name.
ELIGIBLE_ITEM_TYPES = ("service", "product", "package")


class Discount(Base):
    """One reusable discount an administrator has defined.

    **Exactly one of `percentage_bp`/`amount_cents`, matching `kind`** — enforced by
    `ck_discounts_amount_matches_kind` below, the same `num_nonnulls`-flavoured invariant
    `queue_entries` uses for its own either/or column pair, spelled out explicitly here
    because the two branches also carry different range checks.

    **`eligibility_scope`** — `"all"` means every service/product/package; `"selected"` means
    only the rows in `DiscountEligibleItem`, replaced as a whole set through its own endpoint
    (the same shape `scheduling/services.py::replace_eligible_staff` already establishes) so a
    save is never half-applied.

    **`commission_basis`** — `"reduces"` means the discount comes off what commission is
    calculated on; `"absorbed"` means the business eats it and commission is unaffected. Which
    one applies is read from here by #69's posting logic; this ticket only stores the choice.
    """

    __tablename__ = "discounts"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in DISCOUNT_KINDS) + ")",
            name="ck_discounts_kind",
        ),
        CheckConstraint(
            "eligibility_scope IN (" + ", ".join(f"'{s}'" for s in ELIGIBILITY_SCOPES) + ")",
            name="ck_discounts_eligibility_scope",
        ),
        CheckConstraint(
            "commission_basis IN (" + ", ".join(f"'{b}'" for b in COMMISSION_BASES) + ")",
            name="ck_discounts_commission_basis",
        ),
        CheckConstraint(
            "(kind = 'percentage' AND percentage_bp IS NOT NULL "
            "AND percentage_bp BETWEEN 0 AND 10000 AND amount_cents IS NULL) "
            "OR (kind = 'fixed' AND amount_cents IS NOT NULL AND amount_cents >= 0 "
            "AND percentage_bp IS NULL)",
            name="ck_discounts_amount_matches_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16))
    # Basis points, 0-10000 — `Staff.commission_rate_services_bp`'s own convention. Set iff
    # `kind == "percentage"`.
    percentage_bp: Mapped[int | None] = mapped_column(Integer)
    # Integer cents (CLAUDE.md, tech-stack §21). Set iff `kind == "fixed"`.
    amount_cents: Mapped[int | None] = mapped_column(Integer)
    eligibility_scope: Mapped[str] = mapped_column(String(16), server_default=text("'all'"))
    stackable: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    commission_basis: Mapped[str] = mapped_column(String(16))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # `selectin`, not the default lazy load: an unloaded relationship on an async session is
    # an error rather than a second query, and every reader of this table wants the set
    # (`scheduling/models.py::Service.eligible_staff` establishes the same pattern).
    eligible_items: Mapped[list["DiscountEligibleItem"]] = relationship(
        back_populates="discount",
        cascade="all, delete-orphan",
        lazy="selectin",
        passive_deletes=True,
    )


class DiscountEligibleItem(Base):
    """One catalog item a `"selected"`-scope discount applies to.

    Composite key rather than a surrogate `id`: the same `(item_type, item_id)` named twice
    for one discount is one eligibility, not two, and a composite primary key says that
    without a separate unique index to keep in sync with it.
    """

    __tablename__ = "discount_eligible_items"
    __table_args__ = (
        CheckConstraint(
            "item_type IN (" + ", ".join(f"'{t}'" for t in ELIGIBLE_ITEM_TYPES) + ")",
            name="ck_discount_items_item_type",
        ),
    )

    discount_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("discounts.id", ondelete="CASCADE"), primary_key=True
    )
    item_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)

    discount: Mapped[Discount] = relationship(back_populates="eligible_items")

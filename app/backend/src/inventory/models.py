"""The retail catalog: products, their variants, and what each variant costs and holds in
stock right now (M4 #56; #54 stories 69, 70, 83).

**Two tables, not one.** A product ("Shampoo") is a name a client recognises; a variant
("500ml", "1L") is the thing that actually carries a SKU, a barcode, a price and a stock
count. Retail sells the variant — the same reasoning `scheduling/models.py::Service` gives
for booking being against a service rather than a category of one.

**Stock here is a starting count, not a ledger.** This ticket is the catalog shape only — no
stock *movements* yet (that is #61's `stock_movements` table and the atomic
`UPDATE ... WHERE quantity_on_hand >= :qty` CLAUDE.md's concurrency section requires for the
sale/return path). `quantity_on_hand` on this row is what that statement will read and write;
here it is only ever set directly, by whoever is receiving or correcting the count.

**Tax settings are a set of component keys, not yet a foreign key.** #57 (tax components,
effective-dated rates) is a sibling Wave 1 ticket with no ordering against this one, so the
rate table does not exist while this migration runs. `tax_component_keys` stores the
free-form keys a variant is subject to — JSONB, following `Service`'s sibling table
(`QueueEntry.overridden_rules`) precedent for a small string list on this codebase — so #57
and #63 (bill review + tax totals) can validate and rate them once that table lands, without
this one waiting on it.

**No hard delete**, matching every other catalog row here (`Service`, `Resource`): `active`
going false takes a product or a variant off every picker while an invoice already issued
against it keeps pointing at something real. A product may be inactive while carrying active
variants and vice versa — deliberately: a discontinued line still needs its existing stock
sold down, and a single dead variant should not have to drag its siblings off the shelf.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base


class Product(Base):
    """What a client recognises on a shelf or a receipt. Carries no price or stock of its
    own — every `ProductVariant` does — because "Shampoo" is not a thing anybody buys;
    "Shampoo, 500ml" is.

    **Names are unique case-insensitively** (`ux_products_name`), the same reasoning
    `Service.name` gives: "Shampoo" and "shampoo" are one product entered twice.
    """

    __tablename__ = "products"
    __table_args__ = (
        Index("ux_products_name", text("lower(name)"), unique=True),
        Index("ix_products_active_sort", "active", "sort_order", "name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Always loaded with the product: every caller of this table — the admin table, the
    # catalog read, #61's receiving screen — wants the variants too, and an unloaded
    # collection on an async session is an error rather than a second query.
    variants: Mapped[list["ProductVariant"]] = relationship(
        back_populates="product",
        lazy="selectin",
        order_by="ProductVariant.sort_order, ProductVariant.name",
    )


class ProductVariant(Base):
    """One sellable unit: a SKU, an optional barcode, a price, a whole-unit stock count and
    its own low-stock threshold — independent of every sibling variant, because "500ml" can
    run out while "1L" is fully stocked (acceptance criteria: "each variant carries its own
    low-stock threshold, independent of siblings").

    **Whole units only.** `quantity_on_hand` and `low_stock_threshold` are `Integer`, and the
    CHECK constraints below refuse a negative count the same way `Service.price_cents`
    refuses a negative price — a database rule, not only a 422 an import or a psql session
    could route around.

    **SKU and barcode are unique across the whole catalog**, not just within a product: they
    are what a barcode scanner or a receiving sheet looks up cold, with no product context to
    narrow the search. A variant's *name* ("500ml") is unique only within its own product —
    two different products may each have a "Large".
    """

    __tablename__ = "product_variants"
    __table_args__ = (
        CheckConstraint("price_cents >= 0", name="ck_product_variants_price"),
        CheckConstraint("quantity_on_hand >= 0", name="ck_product_variants_quantity"),
        CheckConstraint("low_stock_threshold >= 0", name="ck_product_variants_low_stock_threshold"),
        Index("ux_product_variants_product_name", "product_id", text("lower(name)"), unique=True),
        Index("ux_product_variants_sku", text("lower(sku)"), unique=True),
        # Postgres treats every NULL in a unique index as distinct from every other, so this
        # needs no partial predicate to let more than one variant carry no barcode.
        Index("ux_product_variants_barcode", text("lower(barcode)"), unique=True),
        Index(
            "ix_product_variants_product_active_sort",
            "product_id",
            "active",
            "sort_order",
            "name",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    sku: Mapped[str] = mapped_column(String(64))
    barcode: Mapped[str | None] = mapped_column(String(64))
    # Integer cents (CLAUDE.md). The screen shows dollars and converts before it gets here.
    price_cents: Mapped[int] = mapped_column(Integer, server_default="0")
    quantity_on_hand: Mapped[int] = mapped_column(Integer, server_default="0")
    low_stock_threshold: Mapped[int] = mapped_column(Integer, server_default="0")
    # See the module docstring: free-form keys until #57's tax_components table lands.
    tax_component_keys: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    product: Mapped[Product] = relationship(back_populates="variants")


class StockMovement(Base):
    """The append-only ledger behind `ProductVariant.quantity_on_hand` (#61; #54 stories
    78-80). Every change to stock — a receiving delivery, a completed sale, a return, or an
    administrator's manual count correction — writes one row here; `inventory/stock.py::
    record_movement` is the only writer, and the only thing that ever touches
    `quantity_on_hand` directly.

    **Signed, whole-unit, never zero.** `quantity_delta` is positive for anything that adds
    stock (`receipt`, `return`) and negative for anything that removes it (`sale`, or an
    `adjustment` correcting a count downward) — one column rather than a magnitude-plus-
    direction pair, the same shape a ledger's signed amount takes over separate debit/credit
    columns.

    **`reason` is required for a manual `adjustment`, optional everywhere else**
    (`ck_stock_movements_adjustment_reason`): a receiving delivery explains itself, a
    correction to what is actually on the shelf does not.

    **Append-only by DB grant and trigger** (`0050_stock_movements.py`), the same
    `audit_events`/`documents` precedent CLAUDE.md's "Document storage and immutability"
    section names — `linsuite_app` keeps SELECT/INSERT only. A movement, once written, is
    never edited or replaced by the application.

    **No FK to whatever caused it.** A sale's invoice line, a return's credit — later tickets
    (#75 and beyond) decide that shape; this ledger only needs to know a movement happened,
    by whom, and why, not what triggered it.
    """

    __tablename__ = "stock_movements"
    __table_args__ = (
        CheckConstraint("quantity_delta <> 0", name="ck_stock_movements_quantity_delta"),
        CheckConstraint(
            "kind IN ('receipt', 'sale', 'return', 'adjustment')", name="ck_stock_movements_kind"
        ),
        CheckConstraint(
            "kind <> 'adjustment' OR reason IS NOT NULL",
            name="ck_stock_movements_adjustment_reason",
        ),
        Index("ix_stock_movements_variant_created", "variant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("product_variants.id"))
    kind: Mapped[str] = mapped_column(String(16))
    quantity_delta: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text)
    # Not a foreign key: the ledger outlives the account that made the entry — the same
    # reasoning `AuditEvent.actor_user_id` gives.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    variant: Mapped[ProductVariant] = relationship()

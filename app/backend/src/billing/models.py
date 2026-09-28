"""Package & bundle definitions (#60): admin-configured prepaid credit for one or more
services — a "package" grants N credits of one service, a "bundle" grants credits of several
named ones. One table for both shapes, because nothing behaves differently between them:
`PackageDefinitionService` numbering one row is a package, several is a bundle, and every
other rule (expiry, transferability, activation, name uniqueness) already applies the same
way regardless of the count. A `kind` column would only restate a fact this table can already
tell from its own child rows, so there is not one.

**Definitions only, this ticket.** No purchase flow (#71) and no redemption (#72) yet.

**Snapshot contract, for #71 to read.** A purchase must copy this row's shape onto its own
immutable line, never join it live — the same rule `Service` already states for a booked
appointment (`scheduling/models.py`'s "SNAPSHOT CONTRACT") and for the same reason: editing a
definition after somebody has bought against it must not rewrite what they were sold.
`forms.models`'s `FormTemplate`/`FormTemplateVersion` pair is the *other* precedent for "a
definition that gets snapshotted, not live-referenced, once used" this ticket was pointed at,
and it is deliberately not the shape used here. A form's numbered version exists because many
distinct *historical* submissions must each stay individually rendered against the exact
schema they were signed with — there is a reader, indefinitely far in the future, that needs
version 3 specifically. A package purchase has no such reader: #71 needs this definition's
*current* numbers exactly once, at the moment of sale, and then owns its own frozen copy
forever after — nobody ever re-reads "package definition as it stood on this old purchase".
So this stays a single mutable admin-edited row, exactly like `Service`, and it is `#71`'s
purchase row that does the freezing, not a version table here that would have no other reader.

Money is integer cents (CLAUDE.md, tech-stack §21).

---

Discount definitions (#58, M4 spec #54 stories 18/19/21/25).

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
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base


class PackageDefinition(Base):
    __tablename__ = "package_definitions"
    __table_args__ = (
        CheckConstraint("price_cents >= 0", name="ck_package_definitions_price"),
        CheckConstraint(
            "expires_after_days IS NULL OR expires_after_days >= 1",
            name="ck_package_definitions_expiry",
        ),
        # Case-insensitive uniqueness, `ux_services_name`'s own shape — "Massage Package" and
        # "massage package" are one definition entered twice.
        Index("ux_package_definitions_name", text("lower(name)"), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Unique case-insensitively via `ux_package_definitions_name` (the migration), the same
    # functional-index shape as `ux_services_name`.
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    # What the customer pays for the whole definition. For a single-service package this is
    # just that service's price for N credits; for a bundle, `billing.allocation
    # .allocate_bundle_price` divides it across the constituent services' *current* regular
    # prices — a computation #71 runs once, at purchase, and freezes onto its own row.
    price_cents: Mapped[int] = mapped_column(Integer)
    # NULL (the default) is "never expires" — an explicit per-definition opt-in, in days
    # counted from the purchase date. Computing an actual expiry timestamp is #71's job; this
    # column is only the policy the admin chose.
    expires_after_days: Mapped[int | None] = mapped_column(SmallInteger)
    # Off by default (CLAUDE.md): a credit belongs to the customer who bought it unless an
    # administrator explicitly allows moving it to another.
    transferable: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # No hard delete (tech-stack §15, §20), same as `Service`: a customer already holding
    # credits against a retired definition must still be able to redeem them; only new sales
    # stop.
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    services: Mapped[list["PackageDefinitionService"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", passive_deletes=True
    )


class PackageDefinitionService(Base):
    """One eligible service and how many credits buying the definition grants against it.

    Composite key, replaced whole on edit (`PUT /admin/packages/{id}/services`) — the same
    shape as `scheduling.models.ServiceStaff`/`ServiceRequirement`, and for the same reason:
    the set is one decision, and a per-row API would let a half-applied set exist between two
    requests.

    **FKs to `services` only, never to a product.** CLAUDE.md: "no mixed service/product
    package and no retail credit bundle can be defined... the schema should not make this
    representable" — there is no column here a product id could go in, so it is not a rule
    this module has to remember to enforce, only one it cannot violate. `service_id` is a
    plain FK column rather than an ORM relationship: `billing` reads a service's name and
    price through its own query (`billing/packages.py`), the way `forms.models
    .FormTemplateService` already references `services.id` with no relationship object —
    cross-domain reads go through an explicit join at the call site, not a mapped attribute,
    so this module never imports `scheduling`.
    """

    __tablename__ = "package_definition_services"
    __table_args__ = (
        CheckConstraint("credits >= 1", name="ck_package_definition_services_credits"),
    )

    package_definition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("package_definitions.id", ondelete="CASCADE"), primary_key=True
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="RESTRICT"), primary_key=True
    )
    credits: Mapped[int] = mapped_column(SmallInteger)


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

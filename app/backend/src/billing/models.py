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

**Money is integer cents** (CLAUDE.md, tech-stack §21).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
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

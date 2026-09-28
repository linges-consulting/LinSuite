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

---

Tax components and their effective-dated rates (#57; #54 stories 30-33).

**One row per component, one table for its whole rate history.** A component (GST, PST, HST,
QST, VAT — whatever a jurisdiction calls it) is a stable thing with a `code` a catalog item
will later reference (#63, #65); its *rate* is not stable, so it lives in a child table
instead of a column on the component, following the effective-dating shape CLAUDE.md asks
for: "Rates are effective-dated data; invoices snapshot them." Editing a rate in place would
retroactively change every already-issued invoice's numbers the moment #65 starts snapshotting
them — this table exists so that never happens: a rate change is always a new
`TaxComponentRate` row, never an `UPDATE` of `rate_bp` on an old one.

**Jurisdiction, not hardcoding.** `province` ties a component to the field
`core/models.py::Business` already reserved for this ("the field a later rate table joins
on") — `NULL` means federal (applies regardless of the business's own province, e.g. GST),
a two-letter code restricts it to a business whose own `province` matches (e.g. BC's PST).
Which components actually apply to *one catalog item* is #63's job; this table only holds the
definitions the jurisdiction selects from.

**No overlapping rates for one component**, enforced in the database rather than only in the
route that writes it (CLAUDE.md "enforce in the DB, not app locks") — the same `EXCLUDE USING
gist` shape `working_hours`/`time_off`/`AppointmentResource` already use for a different kind
of non-overlapping range (`scheduling/models.py`), applied here to a date range instead of a
timestamp one. `effective_to` null means "still in effect"; closing one rate and opening the
next is `billing/routes.py`'s job, done as one PATCH+INSERT so a rate is never left open past
the day its successor starts.

---

The business-owned document key tier (#55, ADR-0003): one wrapped key for every financial
document, independent of any customer's own crypto-shred.

`billing/keys.py`'s `business_key` wraps and unwraps it exactly as `customers/keys.py` does
for a client's DEK, except there is exactly one row — `business_id` is always 1, the same
value `businesses.id`'s own CHECK pins it to — and it never shreds: a business's financial
documents are retained for the CRA's six-year rule regardless of what happens to any one
customer's clinical key. Migration `0048_business_document_keys.py`'s
`business_document_keys_guard` refuses UPDATE and DELETE to every runtime role,
unconditionally — there is no purge-eligible branch like `customer_document_keys`'s, because
nothing in v1 ever destroys this key.

---

The service bill: what `scheduling/appointments.py::complete_appointment` (#59) creates or
appends to, one line per completed appointment, grouped onto one bill per visit.

**Why "service bill" and not "invoice".** CLAUDE.md ("Domain rules"): services and retail
invoice separately, and an invoice is a *voidable* document — never edited in place, issued
once, cancelled and replaced rather than mutated (tech-stack, document immutability). A draft
is the opposite of that on purpose: it is appended to, line by line, as sibling appointments in
one visit complete, right up until #65 issues it. Calling the mutable, pre-issue thing a
"bill" and the immutable, post-issue thing an "invoice" keeps those two document classes from
sharing a name while #65 (which reads this table and writes the issued document) hasn't landed
yet.

**Visit grouping reuses `Appointment.booking_group_id` directly** (m4.md "Reusable patterns") —
no new grouping concept. `ServiceBill.booking_group_id` is nullable for the same reason the
appointment column is: an appointment booked alone has no group, and its bill never needs to
be found by anything but its own id, so nothing here forces one.

**Commission rate is snapshotted onto the line, not the bill, at completion — not at invoice
issue (#54's explicit M4 reversal of the older assumption, m4.md).** `Staff.commission_rate_
services_bp` can change between completion and issue; the line is what a later commission
report reads, and it must keep reading what was true the moment the work was delivered.

**Status is `draft`/`issued` even though nothing in this ticket ever sets `issued`** — #65 is
what does that. The column exists now because #59's own acceptance criteria require it: a
later completion for a visit whose bill has already moved past `draft` must open a new one
rather than appending to a document that's supposed to be immutable from that point on.
"""

import uuid
from datetime import date as Date
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
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy import (
    Date as DateColumn,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base
from core.models import PROVINCE_CODES


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


# The `Staff.commission_rate_*_bp` convention (CLAUDE.md, scheduling/models.py): integer
# basis points, 10000 the ceiling. A tax rate above 100% is a typo, the same reasoning
# `MAX_BASIS_POINTS` there already gives for a commission rate.
MAX_TAX_RATE_BP = 10_000


class TaxComponent(Base):
    __tablename__ = "tax_components"
    __table_args__ = (
        UniqueConstraint("code", name="uq_tax_components_code"),
        CheckConstraint(
            "province IS NULL OR province IN (" + ", ".join(f"'{p}'" for p in PROVINCE_CODES) + ")",
            name="ck_tax_components_province",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Short and stable — a later catalog item (#56) and bill review (#63) reference this, not
    # the row id, the same way `auth/capabilities.py`'s keys are never renamed once shipped.
    code: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(100))
    province: Mapped[str | None] = mapped_column(String(2))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    rates: Mapped[list["TaxComponentRate"]] = relationship(
        back_populates="component",
        order_by="TaxComponentRate.effective_from",
        cascade="all, delete-orphan",
    )


class TaxComponentRate(Base):
    __tablename__ = "tax_component_rates"
    __table_args__ = (
        CheckConstraint(
            f"rate_bp BETWEEN 0 AND {MAX_TAX_RATE_BP}", name="ck_tax_component_rates_bp"
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_tax_component_rates_range",
        ),
        ExcludeConstraint(
            ("component_id", "="),
            (text("daterange(effective_from, effective_to)"), "&&"),
            name="ex_tax_component_rates_no_overlap",
            using="gist",
        ),
        Index("ix_tax_component_rates_component", "component_id", "effective_from"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    component_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tax_components.id", ondelete="CASCADE")
    )
    rate_bp: Mapped[int] = mapped_column(Integer)
    effective_from: Mapped[Date] = mapped_column(DateColumn)
    # NULL = still in effect (open-ended). See the module docstring: a new rate closes this
    # one rather than replacing it, so an invoice snapshot taken while this was current keeps
    # reading the same number forever.
    effective_to: Mapped[Date | None] = mapped_column(DateColumn)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    component: Mapped["TaxComponent"] = relationship(back_populates="rates")


class BusinessDocumentKey(Base):
    __tablename__ = "business_document_keys"

    business_id: Mapped[int] = mapped_column(Integer, ForeignKey("businesses.id"), primary_key=True)
    # `core.crypto` format: base64 of nonce || ciphertext+tag. The nonce lives inside it.
    wrapped_key: Mapped[str] = mapped_column(Text)
    # Same purpose as `CustomerDocumentKey.master_key_version`: always 1 until master-key
    # rotation exists (out of scope).
    master_key_version: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ServiceBill(Base):
    """One visit's draft (or, later, issued) service bill. `customer_id` is copied off the
    first appointment that creates it — every appointment in one `booking_group_id` shares a
    customer (a visit is for one client), so there is nothing to reconcile across lines."""

    __tablename__ = "service_bills"
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'issued')", name="ck_service_bills_status"),
        # The lookup #59's completion hook runs on every grouped completion: "the open draft
        # for this visit, if one exists yet."
        Index("ix_service_bills_group_status", "booking_group_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    # Task 18's visit tag (scheduling/models.py) — null for a single, ungrouped appointment's
    # own bill, which nothing else is ever grouped onto.
    booking_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(16), server_default=text("'draft'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    lines: Mapped[list["ServiceBillLine"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", passive_deletes=True
    )


class ServiceBillLine(Base):
    """One completed appointment's contribution to its visit's bill. `appointment_id` is
    unique — an appointment appears on at most one line, ever — belt-and-braces alongside the
    real guarantee, which is `complete_appointment`'s own `_lock`/status-guard already
    refusing a retried completion before the hook that inserts this row ever runs (m4.md
    "the completion transaction to hook into"; this table adds no second dedup mechanism of
    its own, because it does not need one)."""

    __tablename__ = "service_bill_lines"
    __table_args__ = (
        CheckConstraint("price_cents >= 0", name="ck_service_bill_lines_price"),
        CheckConstraint(
            "commission_rate_bp BETWEEN 0 AND 10000", name="ck_service_bill_lines_commission_bp"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    bill_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("service_bills.id", ondelete="CASCADE"))
    appointment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("appointments.id"), unique=True)
    service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("services.id"))
    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id"))
    # The appointment's own price snapshot, copied rather than referenced — the same
    # SNAPSHOT CONTRACT `Appointment.price_cents` itself follows against `Service` (CLAUDE.md,
    # scheduling/models.py): a later price change on the service must never rewrite a bill
    # line for work already delivered.
    price_cents: Mapped[int] = mapped_column(Integer)
    # The snapshot this whole ticket exists for: `Staff.commission_rate_services_bp` *at
    # completion*, never re-read at invoice issue (#65) or report time (#69).
    commission_rate_bp: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ServiceBillDiscount(Base):
    """One discount (#58) staff has chosen to apply to a draft bill, from the bill review
    screen (#63) — the combination, not a per-line record. Composite key, replaced whole on
    `PUT /api/bills/{id}/discounts` — `DiscountEligibleItem`'s own reasoning: one discount
    named twice against one bill is one application, not two.

    **Bill-level, not line-level, on purpose.** Which lines a discount actually reduces is
    derived at read time from `discount_resolver.is_eligible` against each line's
    `service_id` (`billing/bill_review.py`), never stored — a draft bill still accepts new
    lines as sibling appointments complete (#59's own contract: "appended to, line by line,
    right up until #65 issues it"), and a bill-level selection means a discount staff already
    applied keeps applying to whatever line shows up next, with no reapply step, rather than
    going stale the moment a second appointment on the same visit finishes.

    Persisted rather than recomputed per request — #64 (bill review authority, blocked by
    this ticket) reads whatever is here to decide what it is approving, so a staff member's
    selection has to survive a page reload and outlive the request that made it. `discount_id`
    is `ON DELETE RESTRICT`: `Discount` is never hard-deleted (`enabled` going false is the
    only retirement path), so a bill can always keep naming what applied to it.
    """

    __tablename__ = "service_bill_discounts"

    bill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("service_bills.id", ondelete="CASCADE"), primary_key=True
    )
    discount_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("discounts.id", ondelete="RESTRICT"), primary_key=True
    )
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

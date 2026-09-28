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
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
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

    # --- bill review authority (#64) --------------------------------------------------------
    # An admin/owner-authorized exception to the computed `grand_total_cents` — either an
    # approved `BillOverrideRequest` (staff-request path) or a direct inline admin edit
    # (`billing/bill_authority.py`). Deliberately a single absolute final-total override, not
    # a second per-line/tax calculation path beside `bill_review.py::_compute`: an owner
    # authorizing an exception is authorizing a number, not asking the system to re-derive
    # one under a different formula. `_compute` reports it alongside the ordinarily-computed
    # total (`BillOut.override_total_cents`) rather than replacing `grand_total_cents`, so
    # nothing that already reads that field changes meaning.
    manual_override_cents: Mapped[int | None] = mapped_column(Integer)
    manual_override_reason: Mapped[str | None] = mapped_column(Text)
    # #65's own stale-approval checkpoint: stamped with the exact same `datetime.now(UTC)`
    # value as `updated_at`, in the same statement, by both of #64's write sites (an approved
    # `BillOverrideRequest` decision, an inline admin edit) — see `billing/models.py`'s
    # `## invoice issue (#65)` section far below for the full mechanism. `None` whenever
    # `manual_override_cents` is `None` (`bill_review.py::apply_discounts` clears both
    # together); the two are otherwise always written together, never one without the other.
    override_applied_revision: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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


# ---------------------------------------------------------------------------------------------
#
# Bill review authority (#64, M4 spec #54 stories 9-17, 27-29): the two override paths for
# whatever staff can't resolve on #63's bill review screen with a predefined, enabled discount.
#
# **(a) Staff-request review**, this table. **(b) Inline admin edit** has no ORM row of its
# own — `billing/bill_authority.py` grants it as a short-lived Redis window (the same idle/
# hard-limit shape `auth/modes.py::grant_admin` already uses, reused settings, new key
# namespace scoped to one bill rather than one session) and applies it directly, attributing
# the resulting `ServiceBill.manual_override_cents` write to the admin who authenticated —
# never to the staff session whose screen it happened to be typed on.
#
# **Unifies "ad hoc discount" and "price override" as one number**: `requested_total_cents`/
# `decided_total_cents` is a proposed absolute `grand_total_cents` for the whole bill, not a
# second per-line calculation path beside `bill_review.py::_compute`. `kind` is kept only for
# what the review screen displays ("discount requested" vs. "price override requested");
# nothing that computes money reads it.
#
# **Stale-approval invalidation (the ticket's central requirement)**: `bill_revision_as_of`
# pins `ServiceBill.updated_at` at the moment of request — this ticket's own "optimistic
# concurrency" marker, the ticket body's own suggestion. `apply_discounts` (#63,
# `bill_review.py`) and a sibling appointment completing (`billing/completion.py`) both now
# bump `updated_at` explicitly for exactly this reason (neither touched the bill row itself
# before #64: one only wrote `service_bill_discounts`, the other only inserted a
# `ServiceBillLine`). `bill_authority.py`'s decision route refuses (409) to approve or reject
# once `bill.updated_at` no longer matches what was pinned — a stale request must be
# resubmitted against the bill's current state, never silently re-pointed at it.
#
# **Applied at approval, not in a second step.** Approving *is* "staff can resume ordinary
# billing on the same draft" (the acceptance criterion): the decided total lands on
# `ServiceBill.manual_override_cents` in the same transaction as the decision, so there is
# never an approved-but-unapplied state for a second stale window to open on.
#
# ---------------------------------------------------------------------------------------------

BILL_OVERRIDE_REQUEST_KINDS = ("discount", "price_override")
BILL_OVERRIDE_REQUEST_STATUSES = ("pending", "approved", "rejected")


class BillOverrideRequest(Base):
    """One staff-submitted ask for an exceptional discount or price override on a draft bill,
    pending, approved (as submitted or revised) or rejected by an admin/owner holding
    `billing.manage` in Admin Mode. See the module section above for the design choices —
    the unified total-override shape and the stale-approval guard — this table exists for.

    No hard delete, and no `ON DELETE CASCADE` erasing it either: `bill_id` cascades with its
    bill (a draft is genuinely gone once nothing points at it, same as `ServiceBillLine`), but
    `requested_by`/`decided_by` are `RESTRICT` — the same call every other actor FK in this
    app makes, so a request's own history is never explained by a row that no longer exists.
    """

    __tablename__ = "bill_override_requests"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in BILL_OVERRIDE_REQUEST_KINDS) + ")",
            name="ck_bill_override_requests_kind",
        ),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in BILL_OVERRIDE_REQUEST_STATUSES) + ")",
            name="ck_bill_override_requests_status",
        ),
        CheckConstraint(
            "requested_total_cents >= 0", name="ck_bill_override_requests_requested_total"
        ),
        CheckConstraint(
            "decided_total_cents IS NULL OR decided_total_cents >= 0",
            name="ck_bill_override_requests_decided_total",
        ),
        # `list_override_requests`'s own lookup: every request for one bill, and the decision
        # route's "is there already a pending one" question the review screen will ask.
        Index("ix_bill_override_requests_bill_status", "bill_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    bill_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("service_bills.id", ondelete="CASCADE"))
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    # The staff member's ask: what they want the bill's final `grand_total_cents` to become.
    requested_total_cents: Mapped[int] = mapped_column(Integer)
    # `ServiceBill.updated_at` at the moment of request — the stale-approval guard's "as of".
    bill_revision_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    # Null until decided. Set on approval — equal to `requested_total_cents` when approved
    # "as-is", a different value when the admin/owner revised it. Never set on rejection.
    decided_total_cents: Mapped[int | None] = mapped_column(Integer)


# ---------------------------------------------------------------------------------------------
#
# ## invoice issue (#65, M4 spec #54 stories 10-11, 29, 33)
#
# Turns a reviewed, approved draft `ServiceBill` into an issued, immutable `Invoice` — a new
# table pair, deliberately, not `ServiceBill.status` flipping to `"issued"` with a parallel
# snapshot table. Two reasons, both from CLAUDE.md directly: (1) "invoice" is the *voidable*
# document class (never edited in place; cancel sets status + links a replacement) while
# "service bill" is exactly the opposite on purpose (`ServiceBill`'s own docstring, above) —
# one row cannot honestly be both at once as its own status flips underneath it; (2) reading an
# issued invoice must never re-join `services`/`discounts`/`tax_components` live, which means
# every line's resolved numbers need their own frozen columns/rows, not a second read path
# grafted onto `service_bill_lines`. `ServiceBill`/`ServiceBillLine`/`ServiceBillDiscount` are
# untouched by this ticket (bar the one `override_applied_revision` checkpoint column above) —
# every route #63/#64 built keeps working exactly as before.
#
# **Gapless invoice numbering** (`business_invoice_counters`, `billing/invoice_numbering.py`):
# a per-business counter *row*, never a `SEQUENCE` — a `SEQUENCE` still burns a value on a
# rolled-back transaction, which is precisely the gap this ticket must never produce.
# `allocate_invoice_number` locks the row (`SELECT ... FOR UPDATE`) and increments it in the
# *same* transaction as the `Invoice` insert it numbers; a rollback anywhere in that
# transaction rolls the counter back with it. The row lock is what serializes two concurrent
# issue attempts on the same business — CLAUDE.md's "Concurrency: enforce in the DB, not app
# locks," the same statement-is-the-lock philosophy `inventory/stock.py::record_movement`
# already applies to `quantity_on_hand`, applied here to a counter instead of a quantity.
# `uq_invoices_business_number` is the defense-in-depth unique index on top, the same role
# `ex_tax_component_rates_no_overlap` plays beside `TaxComponentRate`'s own effective-dating
# discipline — the lock should already make a collision impossible; the constraint is what
# turns "should" into "does," and what a migration, an import or a stray script can't defeat.
#
# **Snapshot contract, exact shape.** At issue, `billing/invoices.py` calls `bill_review.py`'s
# own `_compute` one final time (never reimplements its math — the same rule `bill_review.py`'s
# own docstring already states for itself) against whatever `service_bill_discounts` currently
# holds, and freezes every number it returns:
#
# - `InvoiceLine` — one row per `ServiceBillLine`, `price_cents`/`commission_rate_bp` copied
#   straight off it (already themselves frozen at completion time, #59); `discounted_cents`/
#   `pretax_cents`/`tax_cents`/`line_total_cents` copied off that line's `_compute`-computed
#   `BillLineOut`/`LineTax`. `service_id`/`staff_id`/`appointment_id` are carried for display
#   only — an FK to a never-hard-deleted table, never read back into a money calculation.
# - `InvoiceLineDiscount` — one row per discount that actually applied to that line
#   (`BillLineOut.applied_discount_ids`), `discount_name`/`discount_kind`/`commission_basis`
#   copied off the live `Discount` row *at this exact moment* (never re-read after). This is
#   the "commission-basis choice each applied discount carried" the ticket asks frozen — #69
#   reads `commission_basis` off here, never off `discounts.commission_basis`, so a later rate
#   or basis change on the definition can never rewrite an already-earned commission.
# - `InvoiceLineTax` — one row per tax component that taxed that line, `component_code` a
#   frozen `Text` copy (not an FK to `tax_components.id` — independence from the live table is
#   the point), `rate_bp` the exact resolved rate `_compute`'s own `_applicable_components` (and
#   under it, `tax.py::resolve_rate_bp`) used, `amount_cents` the component's contribution to
#   that line (`LineTax.component_cents[code]`). A component at `rate_bp == 0` still gets a row,
#   the same "never dropped, just zero" rule `tax.py::LineTax` already states for itself.
#
# **The override total, reported alongside, never conflated** (mirrors `BillOut`'s own shape):
# `Invoice.computed_grand_total_cents` is always the ordinarily-computed number (subtotal minus
# discounts plus tax, summed off the frozen lines); `Invoice.override_applied_cents`/
# `override_reason` are non-null only when an admin/owner-authorized override (#64) was
# authoritative at issue, copied off `ServiceBill.manual_override_cents`/`manual_override_
# reason`; `Invoice.grand_total_cents` is the one number actually billed — `override_applied_
# cents` when present, `computed_grand_total_cents` otherwise. The per-line/discount/tax
# snapshot rows always hold the *computed* breakdown regardless of an override, because an
# override is a single absolute total for the whole bill with no per-line shape of its own
# (`BillOverrideRequest`'s own docstring, above) — there is no honest way to re-derive a
# per-line breakdown from one arbitrary number, so this ticket does not invent one. A reader
# who needs "what was actually charged" reads `grand_total_cents`; a reader who needs "what the
# system computed, and why" reads the frozen lines plus `computed_grand_total_cents`.
#
# **Refusal conditions, all checked in `billing/invoices.py` before any number is frozen or any
# invoice number allocated:**
#
# 1. A `pending` `BillOverrideRequest` exists for this bill — undecided, so nothing about
#    whether its ask is authorized is known yet.
# 2. `bill.manual_override_cents is not None and bill.override_applied_revision != bill.
#    updated_at` — a *stale* override: it was authorized once, but the bill has moved on since
#    (typically a sibling appointment completing into the same visit, `billing/completion.py`,
#    which bumps `updated_at` without touching the override fields at all) without being
#    redecided. Also covers "authorized but the checkpoint was never actually stamped"
#    (`override_applied_revision is None` while `manual_override_cents` is set) — the ticket's
#    "required admin authorization... hasn't actually happened" case; unreachable through the
#    two existing write paths today (both always stamp the checkpoint in the same statement as
#    the override fields), kept as a direct read of "was this actually authorized" rather than
#    an inference from those paths staying correct.
# 3. The bill is not `status == "draft"` (already issued, or — unreachable today, but checked —
#    some other status) or has no lines at all.
#
# **Capability: `billing.view`, reused, not a new key.** Issuing is checkout, not an admin
# action — the same front-desk-reachable call #63 already made for the rest of this screen
# (`billing.view`'s own docstring: "front-desk work... the same call `queue.manage`/`forms.
# issue` already make"). Reading an issued invoice is gated the same way. Nothing here needs
# `billing.manage`/Admin Mode: by the time a bill reaches issue, either nothing exceptional
# ever happened to it, or whatever did was already authorized through #64's own admin-gated
# paths — issue itself only checks that authorization is still valid, it does not grant one.
#
# ---------------------------------------------------------------------------------------------

INVOICE_STATUSES = ("issued", "cancelled")


class BusinessInvoiceCounter(Base):
    """The gapless numbering row (module section above). Exactly one row per business — in
    this single-tenant deployment, exactly one row, `business_id == 1`, created lazily by
    `billing/invoice_numbering.py::allocate_invoice_number` on the first invoice ever issued
    (`billing/keys.py::business_key`'s own "created lazily on first call" precedent, #55).
    Ordinary app-role DML — UPDATE is the whole point of this table, unlike every other new
    table this ticket adds, so no grant is revoked and no trigger guards it."""

    __tablename__ = "business_invoice_counters"
    __table_args__ = (
        CheckConstraint("next_number >= 1", name="ck_business_invoice_counters_next_number"),
    )

    business_id: Mapped[int] = mapped_column(Integer, ForeignKey("businesses.id"), primary_key=True)
    # The number the *next* issued invoice will receive, not the last one issued — so a fresh
    # row starts at 1 and the first invoice really is numbered 1.
    next_number: Mapped[int] = mapped_column(Integer, server_default=text("1"))


class Invoice(Base):
    """One issued invoice — the voidable document class (CLAUDE.md), created once, at issue,
    and never edited in place afterward except the one cancel transition
    `invoices_voidable_guard` (migration 0054) permits. See the module section above for the
    full snapshot contract and refusal conditions; `billing/invoices.py` is the only writer.

    No `"draft"` status exists here — unlike `ServiceBill`, a row is never created except
    already `"issued"`. `service_bill_id` is unique: one draft bill issues at most one invoice,
    ever (re-issuing a `service_bills.status == "issued"` bill is refused before an invoice
    number is even allocated).
    """

    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("business_id", "invoice_number", name="uq_invoices_business_number"),
        UniqueConstraint("service_bill_id", name="uq_invoices_service_bill_id"),
        CheckConstraint("status IN ('issued', 'cancelled')", name="ck_invoices_status"),
        CheckConstraint(
            "(status = 'issued' AND cancelled_at IS NULL AND cancelled_by IS NULL "
            "AND cancel_reason IS NULL) OR "
            "(status = 'cancelled' AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL "
            "AND cancel_reason IS NOT NULL)",
            name="ck_invoices_cancel_fields",
        ),
        CheckConstraint(
            "(override_applied_cents IS NULL) = (override_reason IS NULL)",
            name="ck_invoices_override_fields",
        ),
        Index("ix_invoices_customer", "customer_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    business_id: Mapped[int] = mapped_column(Integer, ForeignKey("businesses.id"))
    invoice_number: Mapped[int] = mapped_column(Integer)
    service_bill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("service_bills.id", ondelete="RESTRICT")
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'issued'"))
    computed_subtotal_cents: Mapped[int] = mapped_column(Integer)
    computed_discount_total_cents: Mapped[int] = mapped_column(Integer)
    computed_tax_total_cents: Mapped[int] = mapped_column(Integer)
    computed_grand_total_cents: Mapped[int] = mapped_column(Integer)
    # `JSONB`, not `JSON` — the plain `json` type has no equality operator in Postgres, and
    # `invoices_voidable_guard` (migration 0054) needs to compare this column bit-for-bit
    # between `OLD`/`NEW` on the one permitted UPDATE.
    tax_totals_by_component: Mapped[dict] = mapped_column(JSONB)
    override_applied_cents: Mapped[int | None] = mapped_column(Integer)
    override_reason: Mapped[str | None] = mapped_column(Text)
    # The one number actually billed — see the module section above.
    grand_total_cents: Mapped[int] = mapped_column(Integer)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    issued_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    replaces_invoice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoices.id"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    cancel_reason: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list["InvoiceLine"]] = relationship(lazy="selectin")


class InvoiceLine(Base):
    """One frozen line — see the module section above for exactly what is copied and why.
    Append-only from the app role's own grant (migration 0054): even the invoice's own cancel
    transition never touches a line."""

    __tablename__ = "invoice_lines"
    __table_args__ = (
        CheckConstraint("price_cents >= 0", name="ck_invoice_lines_price"),
        CheckConstraint(
            "commission_rate_bp BETWEEN 0 AND 10000", name="ck_invoice_lines_commission_bp"
        ),
        UniqueConstraint("service_bill_line_id", name="uq_invoice_lines_service_bill_line_id"),
        Index("ix_invoice_lines_invoice", "invoice_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))
    service_bill_line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("service_bill_lines.id", ondelete="RESTRICT")
    )
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("appointments.id", ondelete="RESTRICT")
    )
    service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("services.id", ondelete="RESTRICT"))
    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id", ondelete="RESTRICT"))
    price_cents: Mapped[int] = mapped_column(Integer)
    commission_rate_bp: Mapped[int] = mapped_column(Integer)
    discounted_cents: Mapped[int] = mapped_column(Integer)
    pretax_cents: Mapped[int] = mapped_column(Integer)
    tax_cents: Mapped[int] = mapped_column(Integer)
    line_total_cents: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    discounts: Mapped[list["InvoiceLineDiscount"]] = relationship(lazy="selectin")
    taxes: Mapped[list["InvoiceLineTax"]] = relationship(lazy="selectin")


class InvoiceLineDiscount(Base):
    """One predefined discount (#58) that applied to one frozen line, with the commission-
    basis choice it carried at the moment of issue — #69 reads `commission_basis` from here,
    never from the live `Discount` row. Composite key: one discount named twice against one
    line is one application, `ServiceBillDiscount`'s own precedent."""

    __tablename__ = "invoice_line_discounts"
    __table_args__ = (
        CheckConstraint(
            "discount_kind IN ('percentage', 'fixed')", name="ck_invoice_line_discounts_kind"
        ),
        CheckConstraint(
            "commission_basis IN ('reduces', 'absorbed')",
            name="ck_invoice_line_discounts_commission_basis",
        ),
    )

    invoice_line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoice_lines.id", ondelete="CASCADE"), primary_key=True
    )
    discount_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("discounts.id", ondelete="RESTRICT"), primary_key=True
    )
    discount_name: Mapped[str] = mapped_column(Text)
    discount_kind: Mapped[str] = mapped_column(Text)
    commission_basis: Mapped[str] = mapped_column(Text)


class InvoiceLineTax(Base):
    """One tax component's frozen contribution to one line — `component_code` is a frozen
    `Text` copy of `TaxComponent.code`, never an FK to `tax_components.id` (module section
    above: independence from the live table is the point). `rate_bp` is the exact resolved
    rate `tax.py::resolve_rate_bp` returned at issue; `amount_cents` is that component's own
    share (`tax.py::LineTax.component_cents[code]`)."""

    __tablename__ = "invoice_line_taxes"
    __table_args__ = (
        CheckConstraint("rate_bp BETWEEN 0 AND 10000", name="ck_invoice_line_taxes_rate_bp"),
    )

    invoice_line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoice_lines.id", ondelete="CASCADE"), primary_key=True
    )
    component_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    rate_bp: Mapped[int] = mapped_column(Integer)
    amount_cents: Mapped[int] = mapped_column(Integer)


# ---------------------------------------------------------------------------------------------
#
# ## manual payment ledger + checkout gate (#66, M4 spec #54 stories 34-42)
#
# Recording manual payments (cash, e-transfer, external card, insurer) against an *issued*
# invoice (#65), and the gate that decides whether checkout can complete. Two new tables, both
# children of `invoices`, neither ever revisiting an already-frozen invoice's own money columns.
#
# **Outstanding balance is never a stored flag — CLAUDE.md's own instruction, and this
# ticket's own acceptance criterion.** There is no `is_paid` column anywhere in this module.
# `billing/payments.py::outstanding_cents`/`is_checkout_complete` compute it live, every time,
# from `invoice_payments` (`grand_total_cents` minus the sum of every *received* payment —
# a `pending` insurer row never counts) and from whether an `InvoiceBalanceAuthorization` row
# exists for the invoice. The gate checks the *client* portion only — still-pending approved
# insurer money stays visibly outstanding but never blocks checkout (spec #54). "Checkout
# complete" is exactly that predicate, not a second stored
# state: `is_checkout_complete(db, invoice) -> bool` in `billing/payments.py` is the one place
# a later ticket (#70's receipt-release gate, most directly) should import and call rather than
# re-deriving any part of this — see that module's own docstring for the exact shape.
#
# **`InvoicePayment`**: append-only (migration 0055's unconditional shape, `invoice_lines`'s
# own precedent) — a payment entry is a financial record like an invoice line, never edited or
# voided in place. A correction is a new entry, #67's job. `payer_type`/`method` are locked
# together by a DB constraint (`method = 'insurer'` if and only if `payer_type = 'insurer'`) so
# the two facts the acceptance criteria ask recorded separately can never disagree, without
# actually letting them vary independently — in this app's simplified model (CLAUDE.md: no
# direct-billing integration) an insurer payment has exactly one method. `status` is `pending`
# only for an insurer row: an externally-approved-but-unpaid amount gets recorded the moment
# it's approved (so it's visible for follow-up) without ever counting as money collected; when
# it actually arrives, a *second*, `status = 'received'` row records that — approval alone
# never flips the first row, because nothing here is ever edited in place. `collected_by` is
# always the authenticated caller's own id, never client-supplied, the same integrity rule
# `BillOverrideRequest.requested_by` already follows.
#
# **`InvoiceBalanceAuthorization`**: the admin/owner exception — deliberately not
# `BillOverrideRequest`'s (#64) staff-request/admin-decide two-step, since the ticket asks for
# "a single admin action with a reason". One `billing.manage` (Admin Mode) call creates the
# row directly; its mere existence for an invoice is what the gate checks, nothing about its
# `outstanding_cents_at_authorization` (kept only as the audit trail's own record of what was
# actually authorized) is re-read by the gate itself. No staleness guard the way #64's needs
# one: `grand_total_cents` is frozen at issue and payments only ever accumulate, so the balance
# an authorization was granted against can only shrink afterward, never grow past what an
# admin/owner actually saw.
#
# **Capabilities, both reused, no new key.** `billing.view` (front-desk checkout) records
# payments — recording a payment is checkout, the same call #65's own issue route already
# made for issuing the invoice being paid. `billing.manage` (Admin Mode) authorizes the
# outstanding-balance exception — deciding to let money go uncollected is an administrative
# decision in exactly the shape `bill_authority.py`'s own `AdminReviewer` already gates (#64's
# staff-request decision), never front-desk-reachable.
#
# ---------------------------------------------------------------------------------------------

PAYER_TYPES = ("client", "insurer")
PAYMENT_STATUSES = ("pending", "received")
PAYMENT_METHODS = ("cash", "e_transfer", "card", "insurer")


class InvoicePayment(Base):
    """One recorded manual payment against an issued invoice. See the module section above
    for the full schema/append-only rationale; `billing/payments.py` is the only writer."""

    __tablename__ = "invoice_payments"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_invoice_payments_amount"),
        CheckConstraint(
            "payer_type IN ('client', 'insurer')", name="ck_invoice_payments_payer_type"
        ),
        CheckConstraint("status IN ('pending', 'received')", name="ck_invoice_payments_status"),
        CheckConstraint(
            "method IN ('cash', 'e_transfer', 'card', 'insurer')", name="ck_invoice_payments_method"
        ),
        CheckConstraint(
            "(payer_type = 'insurer') = (method = 'insurer')",
            name="ck_invoice_payments_payer_method_match",
        ),
        CheckConstraint(
            "status = 'received' OR payer_type = 'insurer'",
            name="ck_invoice_payments_pending_only_insurer",
        ),
        Index("ix_invoice_payments_invoice", "invoice_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))
    payer_type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'received'"))
    method: Mapped[str] = mapped_column(Text)
    amount_cents: Mapped[int] = mapped_column(Integer)
    reference: Mapped[str | None] = mapped_column(Text)
    collected_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InvoiceBalanceAuthorization(Base):
    """One admin/owner action authorizing an invoice to complete checkout with money still
    outstanding, with a recorded reason. See the module section above — its mere existence for
    an invoice is what `billing/payments.py::is_checkout_complete` checks; append-only
    (migration 0055), same unconditional shape as `InvoicePayment`."""

    __tablename__ = "invoice_balance_authorizations"
    __table_args__ = (
        CheckConstraint(
            "outstanding_cents_at_authorization > 0",
            name="ck_invoice_balance_authorizations_outstanding",
        ),
        Index("ix_invoice_balance_authorizations_invoice", "invoice_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))
    authorized_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    reason: Mapped[str] = mapped_column(Text)
    outstanding_cents_at_authorization: Mapped[int] = mapped_column(Integer)
    authorized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

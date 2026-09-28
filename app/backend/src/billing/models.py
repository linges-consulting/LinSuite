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
    ForeignKeyConstraint,
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
            "prepaid_cents >= 0 AND prepaid_cents <= price_cents",
            name="ck_service_bill_lines_prepaid",
        ),
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
    # #72: non-zero only when completion redeemed a package credit for this visit — then
    # `price_cents` *is* that credit's frozen per-session value and this equals it: a prepaid
    # settlement, never a new amount to collect (no discount, no second tax on it).
    prepaid_cents: Mapped[int] = mapped_column(Integer, server_default=text("0"))
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
        # #68: one *live* invoice per bill — a cancelled predecessor stays beside its
        # replacement (migration 0059); and an original is replaced at most once.
        Index(
            "ux_invoices_service_bill_live",
            "service_bill_id",
            unique=True,
            postgresql_where=text("status = 'issued'"),
        ),
        UniqueConstraint("replaces_invoice_id", name="uq_invoices_replaces_invoice_id"),
        UniqueConstraint("package_purchase_id", name="uq_invoices_package_purchase_id"),
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
        # #71: an invoice is issued for exactly one of a service bill or a package purchase,
        # never neither and never both — see the `## package/bundle purchase (#71)` section far
        # below for why this stays one `Invoice`/numbering-counter pair rather than forking a
        # second one.
        CheckConstraint(
            "(service_bill_id IS NOT NULL AND package_purchase_id IS NULL) OR "
            "(service_bill_id IS NULL AND package_purchase_id IS NOT NULL)",
            name="ck_invoices_source_xor",
        ),
        Index("ix_invoices_customer", "customer_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    business_id: Mapped[int] = mapped_column(Integer, ForeignKey("businesses.id"))
    invoice_number: Mapped[int] = mapped_column(Integer)
    # Nullable as of #71: a package-purchase invoice has no `ServiceBill` at all. Exactly one
    # of this and `package_purchase_id` is set — `ck_invoices_source_xor` above.
    service_bill_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("service_bills.id", ondelete="RESTRICT")
    )
    package_purchase_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("package_purchases.id", ondelete="RESTRICT")
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
    # Empty for a service invoice, populated for a package-purchase one (#71) — the two source
    # kinds are mutually exclusive (`ck_invoices_source_xor`), so exactly one of `lines`/
    # `package_purchase` is ever non-empty/non-null for a given invoice.
    package_purchase: Mapped["PackagePurchase | None"] = relationship(lazy="selectin")


class InvoiceLine(Base):
    """One frozen line — see the module section above for exactly what is copied and why.
    Append-only from the app role's own grant (migration 0054): even the invoice's own cancel
    transition never touches a line."""

    __tablename__ = "invoice_lines"
    __table_args__ = (
        CheckConstraint("price_cents >= 0", name="ck_invoice_lines_price"),
        CheckConstraint(
            "prepaid_cents >= 0 AND prepaid_cents <= line_total_cents",
            name="ck_invoice_lines_prepaid",
        ),
        CheckConstraint(
            "commission_rate_bp BETWEEN 0 AND 10000", name="ck_invoice_lines_commission_bp"
        ),
        # Per invoice, not global (#68): a replacement freezes the same bill lines again.
        UniqueConstraint(
            "invoice_id", "service_bill_line_id", name="uq_invoice_lines_invoice_bill_line"
        ),
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
    # #72: frozen off `ServiceBillLine.prepaid_cents` — the part of `line_total_cents` a
    # redeemed package credit already settled. `billing/payments.py::balances` subtracts it.
    prepaid_cents: Mapped[int] = mapped_column(Integer, server_default=text("0"))
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
# ## package/bundle purchase (#71, M4 spec #54 stories 52, 54)
#
# Buying a `PackageDefinition` (#60) is its own invoice, issued through the *exact same*
# machinery as a service invoice (#65, section above): the same `business_invoice_counters`
# row (`billing/invoice_numbering.py::allocate_invoice_number` is called with no changes at
# all — not forked, not parameterized differently), the same `Invoice` table, the same
# `billing.view`/checkout capability gate. `Invoice.service_bill_id` is now nullable and
# `Invoice.package_purchase_id` is its sibling; `ck_invoices_source_xor` (the migration) pins
# exactly one of the two to be set — an invoice always has exactly one source, service bill or
# package purchase, never both, never neither.
#
# **Why not `InvoiceLine`.** `InvoiceLine` is shaped around one completed appointment:
# `appointment_id`/`staff_id` are `NOT NULL` FKs, `service_bill_line_id` is a `NOT NULL UNIQUE`
# FK to the thing it freezes. A package purchase has none of those — no appointment, no single
# staff member, no `ServiceBillLine` — so forcing it through `InvoiceLine` would mean either
# nullable columns on a table whose whole contract today is "always exactly this shape" (weakening
# every existing check for every service invoice, forever, to accommodate a kind of row that
# never has staff/appointment at all), or sentinel/placeholder FKs, which is worse. `billing/
# packages.py`'s own module docstring already flagged this exact fork in the road ("a package
# purchase has no such reader"); this ticket takes the distinct-pair branch: `PackagePurchase`
# (one row per purchase, playing the role `ServiceBill`'s frozen shape plays for a service
# invoice) and `PackagePurchaseCredit` (one row per credited service, playing `InvoiceLine`'s
# role) sit beside `invoices`, referenced by it, never by `invoice_lines`.
#
# **Frozen snapshot, exactly what the acceptance criteria ask for.** At purchase,
# `billing/package_purchase.py` reads `PackageDefinition`'s *current* row and its
# `PackageDefinitionService` children exactly once and copies every number onto
# `PackagePurchase`/`PackagePurchaseCredit` — never re-joined afterward (`PackageDefinition`'s
# own "Snapshot contract" docstring, and the same rule `InvoiceLine` already follows against
# `ServiceBillLine`/`Service`):
#
# - `PackagePurchase.name`/`price_cents` — the definition's `name`/`price_cents` at the moment
#   of sale (the "paid price" the criteria ask frozen).
# - `PackagePurchase.expires_after_days`/`expires_at` — the definition's expiry *policy*,
#   frozen, plus the actual computed calendar date it resolves to for *this* purchase
#   (`purchased_at`'s date, in the business's own timezone, plus `expires_after_days` — the same
#   `_today_in(business)` helper `bill_review.py`/`invoices.py` already use for tax-rate
#   resolution). `NULL` for both together (`ck_package_purchases_expiry_pair`) iff the
#   definition never expires.
# - `PackagePurchaseCredit.credits_total` — each `PackageDefinitionService.credits`, frozen.
# - `PackagePurchaseCredit.allocated_price_cents` — `billing/allocation.py::
#   allocate_bundle_price`, called **once**, here, against each named service's *current*
#   `Service.price_cents` and the purchase's own `price_cents`. For a single-service package
#   this trivially allocates the whole price to that one service; for a bundle it is the
#   Hamilton-apportioned split CLAUDE.md calls for ("proportional value allocation for bundles
#   (frozen at purchase time, not recomputed later)"). Never recomputed after this call — the
#   whole reason #60 built that function pure and apart from any ORM object.
#
# `service_id` on `PackagePurchaseCredit` is a plain FK, display/reference only, the same
# `InvoiceLine.service_id` precedent (never hard-deleted, so joining live for a name is always
# safe; never re-read for money). No `service_name` column duplicate for the same reason
# `InvoiceLine` does not carry one.
#
# **Tax, same convention as a service line.** `Service.price_cents` is treated as the pre-tax
# "catalog default" (`billing/bill_review.py`'s own documented v1 scope decision: exclusive
# convention, every active business-applicable component applies). `PackageDefinition.
# price_cents` gets the identical treatment for consistency — `billing/package_purchase.py`
# calls the same `tax.py::compute_line_tax`/`bill_review.py::_applicable_components` this
# ticket does not reimplement, one "line" (the whole purchase), no per-service tax breakdown
# needed since `Invoice.tax_totals_by_component` already *is* that one line's own totals.
#
# **No discounts.** Nothing in #71's acceptance criteria asks a predefined discount (#58) to
# apply to a package purchase, and `bill_review.py`'s discount-selection screen has no
# equivalent for a purchase with no draft/review stage — `Invoice.computed_discount_total_cents`
# is always `0` here, `computed_grand_total_cents == grand_total_cents` always (no override
# path either: a package purchase has no `BillOverrideRequest`-shaped review step to authorize
# one from). A later ticket can add either without touching this shape.
#
# **No draft/review stage at all — one atomic action.** Unlike a service visit, which
# accumulates across sibling appointments before being reviewed and issued (#59 -> #63/#64 ->
# #65), a package purchase has nothing to accumulate: picking a definition and a customer *is*
# the whole transaction. `PackagePurchase` is therefore created and immediately issued as an
# `Invoice` in the same request, the same transaction — there is no earlier "draft" row a
# `ServiceBill` would otherwise be. `PackagePurchase` intentionally carries no `invoice_id`
# column of its own (the same one-directional shape `ServiceBill` already has: `Invoice.
# service_bill_id`/`package_purchase_id` are the only links, never a column pointing back), so
# there is no ordering hazard creating both rows in one transaction.
#
# **Credit activation — the ticket's central rule: "a partial payment activates nothing."**
# `PackagePurchase.credits_activated` (default `false`) is the single source of truth for
# whether the credits this purchase names are usable at all; `PackagePurchaseCredit.
# credits_total` is *always* the frozen entitlement, activated or not — "zero usable
# entitlement until fully paid" is enforced by never reading `credits_total` as spendable while
# `credits_activated` is `false`, not by zeroing the column itself (there is nothing else for a
# later reconciliation to compare against if the frozen total were itself mutated).
#
# #66 (payment ledger, sibling M4 ticket, not yet merged as of this one) is what will someday
# know "this invoice is fully paid, not just issued." Until it exists, this ticket cannot
# correctly decide when to flip `credits_activated` — approximating it as "issued == paid" was
# explicitly rejected for this ticket (unlike #70/receipts, which were told that
# approximation was acceptable): "a partial payment activates nothing" is a testable acceptance
# criterion, and getting it wrong would mean credits silently work when they should not. So:
#
# - `billing/package_purchase.py::activate_credits(db, purchase)` is the one integration point
#   — flips `credits_activated` true and stamps `activated_at`, idempotent (a no-op if already
#   activated). It is exercised only by `tests/test_package_purchase.py`'s own direct call,
#   proving the transition works; **nothing in this ticket's own routes ever calls it.**
# - The purchase route always leaves a freshly created `PackagePurchase` at
#   `credits_activated = false`. This is deliberately never approximated as "issued means
#   activated" — the acceptance criterion is that an issued-but-unpaid (or partially paid)
#   purchase invoice grants zero usable credits, and until #66's real payment state exists
#   there is no correct moment before "somebody manually proves full payment" to flip it.
# - **What #66 (or a small follow-up reconciliation ticket) must call, once it exists**: for
#   each `Invoice` whose `package_purchase_id IS NOT NULL` and whose payment ledger shows the
#   invoice fully paid (`grand_total_cents` fully collected — #66's own definition of "fully
#   paid", not redefined here) and whose `package_purchase.credits_activated` is still `false`,
#   call `activate_credits(db, purchase)` inside that same payment-recording transaction. #72
#   (credit redemption) is the eventual reader of `credits_activated`/`credits_total`, and must
#   refuse to spend a credit whose purchase is not yet activated.
#
# **Immutability — the same voidable-adjacent DB-enforced shape `invoices` itself gets** (the
# migration): `package_purchases_activation_guard` permits exactly one transition
# (`credits_activated` `false` -> `true`, `activated_at` newly set, every other column held
# bit-for-bit identical) and refuses every other `UPDATE`/`DELETE`, the same "the ticket's own
# central rule is enforced in the database, not only in application code" argument CLAUDE.md
# makes for every other financial/document table in this app.
# `package_purchase_credits` is fully append-only, `invoice_lines`'s own precedent — nothing
# about a credited service's frozen total or allocated value is ever rewritten (spending one,
# #72, is a different table's job, not a mutation of this frozen row).
#
# **Capability: `billing.view`, reused, not a new key** — purchasing a package at the front
# desk is checkout, the identical reasoning `billing/invoices.py::issue_invoice`'s own
# docstring already gives for issuing a service invoice.
#
# ---------------------------------------------------------------------------------------------


class PackagePurchase(Base):
    """One purchase of a `PackageDefinition` — the frozen snapshot `billing/package_purchase.py`
    writes once, at purchase, and the row `Invoice.package_purchase_id` points at. See the
    module section above for the full snapshot contract, why this is a distinct pair rather
    than reusing `InvoiceLine`, and the credit-activation design."""

    __tablename__ = "package_purchases"
    __table_args__ = (
        CheckConstraint("price_cents >= 0", name="ck_package_purchases_price"),
        CheckConstraint(
            "expires_after_days IS NULL OR expires_after_days >= 1",
            name="ck_package_purchases_expiry_days",
        ),
        CheckConstraint(
            "(expires_after_days IS NULL) = (expires_at IS NULL)",
            name="ck_package_purchases_expiry_pair",
        ),
        CheckConstraint(
            "(credits_activated = false AND activated_at IS NULL) OR "
            "(credits_activated = true AND activated_at IS NOT NULL)",
            name="ck_package_purchases_activation_fields",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Display/reference only, `ON DELETE RESTRICT` — `PackageDefinition` is never hard-deleted
    # (`packages.py`'s own docstring), so this is always safe to join for a name, and it is
    # never re-read for money (module section above: the freeze happened once, onto the columns
    # below).
    package_definition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("package_definitions.id", ondelete="RESTRICT")
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"))
    # --- frozen snapshot, off `PackageDefinition` at the exact moment of purchase -------------
    name: Mapped[str] = mapped_column(String(200))
    price_cents: Mapped[int] = mapped_column(Integer)
    expires_after_days: Mapped[int | None] = mapped_column(SmallInteger)
    expires_at: Mapped[Date | None] = mapped_column(DateColumn)
    # --------------------------------------------------------------------------------------
    purchased_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # The credit-activation flag (module section above). Always `false` at insert; flipped only
    # by `billing/package_purchase.py::activate_credits`, never by this ticket's own routes.
    credits_activated: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    credits: Mapped[list["PackagePurchaseCredit"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", passive_deletes=True
    )


class PackagePurchaseCredit(Base):
    """One credited service and its frozen entitlement/value — `InvoiceLine`'s role, for a
    package purchase. Composite key: one service named twice on one purchase is one credit
    grant, not two, `PackageDefinitionService`'s own precedent. Append-only from the app role's
    own grant (the migration): never rewritten, not even to track redemption — spending a
    credit (#72) is a different table's job."""

    __tablename__ = "package_purchase_credits"
    __table_args__ = (
        CheckConstraint("credits_total >= 1", name="ck_package_purchase_credits_total"),
        CheckConstraint("allocated_price_cents >= 0", name="ck_package_purchase_credits_price"),
    )

    package_purchase_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("package_purchases.id", ondelete="CASCADE"), primary_key=True
    )
    # Display/reference only — never re-joined for money (module section above).
    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="RESTRICT"), primary_key=True
    )
    credits_total: Mapped[int] = mapped_column(SmallInteger)
    allocated_price_cents: Mapped[int] = mapped_column(Integer)


# ---------------------------------------------------------------------------------------------
#
# ## retail sale: draft -> atomic stock-deducting issue (#75, M4 spec #54 stories 5, 69-77)
#
# A retail sale of `ProductVariant`s (#56) is always its own invoice, never combined with a
# service bill/invoice (CLAUDE.md "Domain rules": "Services and retail invoice separately").
# So this is a second, independent draft/issued table pair — `RetailSale`/`RetailSaleLine`
# mirror `ServiceBill`/`ServiceBillLine`'s draft/mutable shape, `RetailInvoice`/
# `RetailInvoiceLine` mirror `Invoice`/`InvoiceLine`'s issued/frozen shape — rather than adding
# nullable retail columns onto the existing service-shaped tables: `invoices.service_bill_id`
# and `invoice_lines.appointment_id`/`service_id` are NOT NULL for a reason (an issued service
# invoice is never a hybrid), and a retail sale never has a `ServiceBill` behind it (#59's
# completion hook is the only thing that creates one, and it only fires for appointments).
#
# **No completion hook creates this one.** Unlike `ServiceBill`, which only ever comes from
# `complete_appointment`, `RetailSale` is created directly by a staff action ("start a retail
# sale") — `billing/retail_sales.py::start_retail_sale`.
#
# **A draft never touches stock, at all** — no reservation, no soft-lock (#75's own first
# acceptance criterion). `RetailSaleLine.unit_price_cents` is a snapshot of `ProductVariant.
# price_cents` taken when the line is added (the same "copied rather than referenced" contract
# `ServiceBillLine.price_cents` already follows against `Service.price_cents`) — a later price
# change on the variant must not rewrite a line already sitting in somebody's cart. Quantity
# available is never checked at draft time either; issue is the one and only moment that
# matters (below).
#
# **Anonymous or linked.** `customer_id` is nullable — the opposite of `ServiceBill.
# customer_id`, which is required because a visit is always for one client. A walk-up retail
# sale with no customer record is explicitly allowed (#75's own acceptance criteria); when a
# customer *is* linked, the sale (and later its invoice) is visible in their history the same
# way an issued service `Invoice` already is (`ix_invoices_customer`'s own precedent).
#
# **"Sold by" vs. the payment collector — two distinct staff attributions, never conflated.**
# `sold_by_staff_id` defaults to the acting staff member (`billing/retail_sales.py::
# _acting_staff_id`, the same `Staff.user_id == actor.id` lookup `scheduling/appointments.py`
# already makes) but is staff-selectable — an admin/manager ringing up a sale on behalf of a
# colleague, or reassigning attribution, via `PATCH /api/retail-sales/{id}`.
# `payment_collector_staff_id` is a second, independent, nullable column: whoever actually
# processed the payment. Changing one never touches the other — two separate columns, not one
# field with an override flag. **No payment ledger exists yet** (#66 is running in parallel) —
# this column is only the place a future payment-collector reference attaches once it does;
# nothing here wires actual payment collection, per this ticket's own scope. Both are nullable
# on `RetailSale`; frozen onto `RetailInvoice` at issue, where `sold_by_staff_id` becomes NOT
# NULL (issue refuses a sale with no attribution at all) and `payment_collector_staff_id`
# stays nullable, since #66's wiring is still pending.
#
# **The atomic multi-line stock deduction, composed from #61's own primitive.** `billing/
# retail_sales.py::issue_retail_sale` calls `inventory/stock.py::record_movement` once per
# line, `kind="sale"`, a negative `quantity_delta` — the exact call `m4.md`'s own #61 ledger
# entry names as the one this ticket must use, never a second competing `UPDATE`. Every call
# happens inside the *one* transaction that also inserts the `RetailInvoice`/
# `RetailInvoiceLine` rows; nothing commits until every line's `record_movement` has
# succeeded. `record_movement`'s own atomic `UPDATE ... WHERE quantity_on_hand + :delta >= 0`
# (CLAUDE.md "Concurrency: stock") is what "revalidates availability" means here — there is no
# separate read-then-check step for a second buyer to race. If any one line's variant has
# insufficient stock, `record_movement` raises `InsufficientStock`; the route lets that
# propagate as a 409 naming the variant, and — because nothing was committed —
# `SessionDep`'s own request-scoped session discards every write this attempt made when
# FastAPI tears it down, the same reliance `inventory/stock_routes.py`'s own routes already
# place on that teardown (no explicit `db.rollback()` needed). So a whole invoice is never
# partially stock-deducted: either every line's movement lands and the invoice commits, or
# none of it does. Lines are processed in `variant_id` order (not insertion order) so two
# concurrent multi-line sales sharing more than one variant can never deadlock against each
# other by taking the same two row locks in opposite orders.
#
# **Numbering reuses `business_invoice_counters`, the same series `Invoice` draws from**
# (`billing/invoice_numbering.py::allocate_invoice_number`, unchanged) — a service invoice and
# a retail invoice for the same business never collide on a number, because both draw from the
# one counter row, even though each keeps its own `uq_*_business_number` uniqueness *within*
# its own table (the same defense-in-depth role `uq_invoices_business_number` already plays:
# the row lock should already make a cross-table collision impossible; the per-table
# constraint is what a stray script or a future bug can't defeat).
#
# **No discount or tax computation in this ticket** — #75's acceptance criteria are about the
# draft/issue/stock/attribution machinery only, and this ticket is blocked by #61/#65, not
# #57/#58. `RetailInvoice.subtotal_cents`/`grand_total_cents` are the plain sum of each line's
# `unit_price_cents * quantity`, with no tax or discount applied — the same scope line #65
# already drew for its own "no cancel route... not in this ticket's acceptance criteria." A
# later ticket that wants retail tax/discounts extends this table the way #65 extended
# `ServiceBill`'s neighbourhood, without touching what's built here.
#
# **Commission field: reused, and snapshotted at issue — the ticket's own decision point.**
# `RetailInvoiceLine.commission_rate_bp` is the same `InvoiceLine.commission_rate_bp`-shaped
# column #65 already established, so #69's posting logic can read either line shape
# uniformly. It is populated from `Staff.commission_rate_retail_bp` (already exists,
# `scheduling/models.py:102`) belonging to the invoice's own `sold_by_staff_id` — read fresh
# **at issue**, not at draft-line-add time. That is the opposite moment from the service side
# (`ServiceBillLine.commission_rate_bp` freezes at appointment *completion*, CLAUDE.md
# "commission earns on delivery, not sale") but it is still the same rule applied to what
# "delivery" means for a retail item: nothing has left the shelf until stock is actually
# deducted, which only happens at issue — issue *is* the retail delivery moment, so that is
# when the rate is read and frozen. #75 does **not** post a commission entry anywhere — no
# such ledger exists yet (#69 is running in parallel and is expected to add the posting hook,
# symmetrically, once both tickets merge); this column only makes the rate available for #69
# to read, snapshotted so a later rate change on `Staff` can never rewrite an already-issued
# line's numbers.
#
# ---------------------------------------------------------------------------------------------

RETAIL_SALE_STATUSES = ("draft", "issued")
RETAIL_INVOICE_STATUSES = ("issued", "cancelled")


class RetailSale(Base):
    """One retail sale's draft — a cart of `ProductVariant` lines built up by a staff action,
    never touching stock until #75's own atomic issue. See the module section above."""

    __tablename__ = "retail_sales"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in RETAIL_SALE_STATUSES) + ")",
            name="ck_retail_sales_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Nullable, unlike `ServiceBill.customer_id` — a walk-up retail sale may have no client
    # record at all (module section above).
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(16), server_default=text("'draft'"))
    # Defaults to the acting staff member at creation, reassignable while still a draft
    # (`PATCH /api/retail-sales/{id}`) — module section above.
    sold_by_staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id", ondelete="RESTRICT"))
    # Nullable: who actually processed the payment, independent of `sold_by_staff_id` and
    # unwired to any real payment flow yet (#66, running in parallel) — module section above.
    payment_collector_staff_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("staff.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    lines: Mapped[list["RetailSaleLine"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", passive_deletes=True
    )


class RetailSaleLine(Base):
    """One variant/quantity in a draft retail sale's cart. `unit_price_cents` is a snapshot of
    `ProductVariant.price_cents` taken when the line is added — see the module section above
    for why. No ORM relationship to `ProductVariant` (only a plain `variant_id` column):
    `billing` never imports `inventory`'s ORM classes, the same cross-domain boundary
    `PackageDefinitionService.service_id` already draws against `scheduling`."""

    __tablename__ = "retail_sale_lines"
    __table_args__ = (
        CheckConstraint("quantity >= 1", name="ck_retail_sale_lines_quantity"),
        CheckConstraint("unit_price_cents >= 0", name="ck_retail_sale_lines_price"),
        Index("ix_retail_sale_lines_sale", "sale_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    sale_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("retail_sales.id", ondelete="CASCADE"))
    # No `ondelete` — a variant is never hard-deleted (`StockMovement.variant_id`'s own
    # precedent, `inventory/models.py`).
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("product_variants.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_cents: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RetailInvoice(Base):
    """One issued retail invoice — the voidable document class (CLAUDE.md), created once, at
    #75's own atomic issue, and never combined with a service invoice (module section above).
    No cancel route is built by this ticket (the same scope line #65 drew for its own cancel
    transition) — the voidable shape and its trigger are ready for #76 (retail returns)."""

    __tablename__ = "retail_invoices"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "invoice_number", name="uq_retail_invoices_business_number"
        ),
        UniqueConstraint("retail_sale_id", name="uq_retail_invoices_retail_sale_id"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in RETAIL_INVOICE_STATUSES) + ")",
            name="ck_retail_invoices_status",
        ),
        CheckConstraint(
            "(status = 'issued' AND cancelled_at IS NULL AND cancelled_by IS NULL "
            "AND cancel_reason IS NULL) OR "
            "(status = 'cancelled' AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL "
            "AND cancel_reason IS NOT NULL)",
            name="ck_retail_invoices_cancel_fields",
        ),
        Index("ix_retail_invoices_customer", "customer_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    business_id: Mapped[int] = mapped_column(Integer, ForeignKey("businesses.id"))
    invoice_number: Mapped[int] = mapped_column(Integer)
    retail_sale_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("retail_sales.id", ondelete="RESTRICT")
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(Text, server_default=text("'issued'"))
    subtotal_cents: Mapped[int] = mapped_column(Integer)
    # The one number actually billed. Always equal to `subtotal_cents` in this ticket (no tax
    # or discount computation yet, module section above) — a separate column anyway, so a
    # later ticket that adds either never has to rename what every reader already calls "the
    # total".
    grand_total_cents: Mapped[int] = mapped_column(Integer)
    sold_by_staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id", ondelete="RESTRICT"))
    payment_collector_staff_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("staff.id", ondelete="RESTRICT")
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    issued_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    replaces_invoice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("retail_invoices.id"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    cancel_reason: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list["RetailInvoiceLine"]] = relationship(lazy="selectin")


class RetailInvoiceLine(Base):
    """One frozen line of an issued retail invoice. Append-only from the app role's own grant
    (migration 0056) — even the invoice's own cancel transition never touches a line, the same
    shape `InvoiceLine` already follows."""

    __tablename__ = "retail_invoice_lines"
    __table_args__ = (
        CheckConstraint("quantity >= 1", name="ck_retail_invoice_lines_quantity"),
        CheckConstraint("unit_price_cents >= 0", name="ck_retail_invoice_lines_price"),
        CheckConstraint("line_total_cents >= 0", name="ck_retail_invoice_lines_line_total"),
        CheckConstraint(
            "commission_rate_bp BETWEEN 0 AND 10000", name="ck_retail_invoice_lines_commission_bp"
        ),
        UniqueConstraint("retail_sale_line_id", name="uq_retail_invoice_lines_retail_sale_line_id"),
        Index("ix_retail_invoice_lines_invoice", "invoice_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("retail_invoices.id", ondelete="CASCADE")
    )
    retail_sale_line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("retail_sale_lines.id", ondelete="RESTRICT")
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("product_variants.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_cents: Mapped[int] = mapped_column(Integer)
    line_total_cents: Mapped[int] = mapped_column(Integer)
    # `sold_by_staff_id`'s value at issue, copied per line for the same reason `InvoiceLine.
    # staff_id` is copied per line (module section above): every line in one retail sale
    # shares one "sold by", but #69's posting logic reads a uniform per-line shape either way.
    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id", ondelete="RESTRICT"))
    # `Staff.commission_rate_retail_bp`, read fresh and frozen at issue — module section above.
    commission_rate_bp: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------------------------
#
# ## commission posting + report (#69, M4 spec #54 stories 94, 97-100)
#
# One append-only ledger entry per invoice line, posted in the same transaction as invoice
# issue (`billing/invoices.py::issue_invoice`) — "one more thing before the commit," the exact
# shape `billing/completion.py::record_draft_bill_line`/`scheduling/appointments.py
# ::_clear_queue_entry` already established. The rate is always `InvoiceLine.commission_rate_bp`
# (itself frozen off `ServiceBillLine.commission_rate_bp` at completion, #59) — never
# `Staff.commission_rate_services_bp` read live. The basis is `billing/commission.py::
# commission_basis_cents`'s own formula, reading `InvoiceLineDiscount.commission_basis` per
# applied discount, never the live `Discount` row — see that module's docstring for the exact
# composition rule.
#
# **Append-only, `stock_movements`/`invoice_lines`'s exact shape** (CLAUDE.md, refunds: "post
# as dated reversing entries in the period they occur, not by rewriting the original
# posting"). The app role may only INSERT and SELECT (migration 0057's grant + trigger pair).
# A correction is never an `UPDATE` of `amount_cents` — it is a second row, `kind="reversal"`,
# a negative `amount_cents`, and its own `posted_at` (the date the reversal actually happens,
# never backdated to the original invoice's date) — the shape #67/#68/#73 (payment
# correction/refund, cancel & replace, package refunds) are expected to follow once they exist.
# `reverses_posting_id` is a self-FK for a reversal to name what it corrects; #69 never writes
# it (every row this ticket posts is `kind="earned"`), it exists only so a later reversal has
# somewhere to point.
#
# **Package-service commission constraint, restated here for whoever reads the schema first**
# (full reasoning in `billing/commission.py`'s own docstring): `staff_id` is always copied from
# `InvoiceLine.staff_id` — the appointment's own delivering staff member — never inferred from
# who sold a package. #72 (credit redemption at completion) must keep `ServiceBillLine.staff_id`
# pointed at the delivering staff member for this to keep holding.
#
# **Capability: `commission.view`, new, Administrator-only, Admin Mode** — `audit.view`'s own
# shape (#22): commission data is sensitive (m4.md's own note already suggested this exact
# key). Never granted to the seeded Staff role. No staff-facing payload in this app (bill
# review, the roster, an issued invoice's own line output) may include a commission rate,
# basis or computed amount — see `tests/test_commission_leakage.py`.
#
# ---------------------------------------------------------------------------------------------

COMMISSION_POSTING_KINDS = ("earned", "reversal")


class CommissionPosting(Base):
    """One append-only ledger entry: what one staff member earned in commission on one invoice
    line. See the module section above for the full design — the composition formula lives in
    `billing/commission.py`, not here; this table only stores what that formula returned."""

    __tablename__ = "commission_postings"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in COMMISSION_POSTING_KINDS) + ")",
            name="ck_commission_postings_kind",
        ),
        CheckConstraint(
            "commission_rate_bp BETWEEN 0 AND 10000", name="ck_commission_postings_rate_bp"
        ),
        CheckConstraint("basis_cents >= 0", name="ck_commission_postings_basis"),
        CheckConstraint(
            "(kind = 'earned' AND amount_cents >= 0) OR (kind = 'reversal' AND amount_cents <= 0)",
            name="ck_commission_postings_amount_sign",
        ),
        Index("ix_commission_postings_staff_posted", "staff_id", "posted_at"),
        Index("ix_commission_postings_invoice", "invoice_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    invoice_line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoice_lines.id", ondelete="RESTRICT")
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id", ondelete="RESTRICT"))
    # The delivering staff member — always `InvoiceLine.staff_id`, never a package seller.
    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(16), server_default=text("'earned'"))
    # Copied straight off `InvoiceLine.commission_rate_bp` — never re-read live.
    commission_rate_bp: Mapped[int] = mapped_column(Integer)
    # `billing/commission.py::commission_basis_cents`'s own output, frozen.
    basis_cents: Mapped[int] = mapped_column(Integer)
    # Positive for an "earned" posting, negative for a "reversal" — never zero-clamped or
    # rewritten; see the module section above.
    amount_cents: Mapped[int] = mapped_column(Integer)
    # Null for an "earned" posting; set on a "reversal" to the posting it corrects. Not read by
    # anything #69 builds — kept for #67/#68/#73 to find "what does this correct."
    reverses_posting_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("commission_postings.id", ondelete="RESTRICT")
    )
    # The ledger date: issue time for an "earned" posting, the actual reversal date for a
    # "reversal" — never backdated to the original invoice (CLAUDE.md "in the period they
    # occur"). What the report's date-range filter reads.
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
# **`InvoicePayment`**: append-only (migration 0058's unconditional shape, `invoice_lines`'s
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
        # A correction (#67) may zero out an entry recorded by mistake; a fresh payment may not.
        CheckConstraint(
            "amount_cents > 0 OR (corrects_payment_id IS NOT NULL AND amount_cents = 0)",
            name="ck_invoice_payments_amount",
        ),
        CheckConstraint(
            "(corrects_payment_id IS NULL) = (correction_reason IS NULL)",
            name="ck_invoice_payments_correction_reason",
        ),
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
    # #67: a correction is a new row superseding the entry it names; the original is never
    # touched. Unique, so an entry is superseded at most once — correcting again means
    # correcting the correction. `balances()` counts only rows nothing supersedes.
    corrects_payment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("invoice_payments.id", ondelete="CASCADE"), unique=True
    )
    correction_reason: Mapped[str | None] = mapped_column(Text)


class InvoiceRefund(Base):
    """Money actually returned to the client (#67) — admin/owner-approved, append-only, never a
    side effect of a correction. The cap (refunds never exceed received money across the
    invoice's replacement lineage) is enforced by `billing/payments.py::record_refund` under a
    `SELECT ... FOR UPDATE` on every invoice in the lineage."""

    __tablename__ = "invoice_refunds"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_invoice_refunds_amount"),
        Index("ix_invoice_refunds_invoice", "invoice_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    approved_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    refunded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InvoiceBalanceAuthorization(Base):
    """One admin/owner action authorizing an invoice to complete checkout with money still
    outstanding, with a recorded reason. See the module section above — its mere existence for
    an invoice is what `billing/payments.py::is_checkout_complete` checks; append-only
    (migration 0058), same unconditional shape as `InvoicePayment`."""

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


class InvoicePaymentTransfer(Base):
    """Cancel & replace (#68): the original's whole ledger position — received, received from
    an insurer, pending insurer — carried onto its replacement at the replacement's issue, so
    money already collected is never charged again nor counted twice. One row per replacement
    (both ends unique); append-only (migration 0059). The payment rows themselves stay on the
    original, never edited — `billing/payments.py::balances()` nets these sums in and out."""

    __tablename__ = "invoice_payment_transfers"
    __table_args__ = (
        UniqueConstraint("from_invoice_id", name="uq_invoice_payment_transfers_from"),
        UniqueConstraint("to_invoice_id", name="uq_invoice_payment_transfers_to"),
        CheckConstraint(
            "received_cents >= 0 AND received_insurer_cents >= 0 AND pending_insurer_cents >= 0",
            name="ck_invoice_payment_transfers_amounts",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    from_invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoices.id", ondelete="RESTRICT")
    )
    to_invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id", ondelete="RESTRICT"))
    received_cents: Mapped[int] = mapped_column(Integer)
    received_insurer_cents: Mapped[int] = mapped_column(Integer)
    pending_insurer_cents: Mapped[int] = mapped_column(Integer)
    transferred_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    transferred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PackageCreditRedemption(Base):
    """One package credit spent on one completed appointment (#72) — the append-only
    redemption ledger (migration 0061). Written only by `billing/redemption.py::redeem_credit`,
    inside `complete_appointment`'s own transaction. Remaining credits for a
    `PackagePurchaseCredit` = its `credits_total` minus its rows here; never a stored counter.

    `sequence` is this credit's 1-based spend number: unique per `(package_purchase_id,
    service_id)` and, by the insert trigger, never past `credits_total` nor against an
    unactivated purchase — so the database alone makes the last credit unspendable twice.
    `value_cents` is the frozen per-session share of the credit's `allocated_price_cents`
    (`allocate_bundle_price` across `credits_total` equal weights), the attributable value the
    visit's bill line, invoice line, receipt and commission all carry."""

    __tablename__ = "package_credit_redemptions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["package_purchase_id", "service_id"],
            [
                "package_purchase_credits.package_purchase_id",
                "package_purchase_credits.service_id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint("sequence >= 1", name="ck_package_credit_redemptions_sequence"),
        CheckConstraint("value_cents >= 0", name="ck_package_credit_redemptions_value"),
        UniqueConstraint(
            "package_purchase_id",
            "service_id",
            "sequence",
            name="uq_package_credit_redemptions_sequence",
        ),
        UniqueConstraint("appointment_id", name="uq_package_credit_redemptions_appointment"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    package_purchase_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    service_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    sequence: Mapped[int] = mapped_column(SmallInteger)
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("appointments.id", ondelete="RESTRICT")
    )
    value_cents: Mapped[int] = mapped_column(Integer)
    redeemed_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    redeemed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PackageCreditVoid(Base):
    """A package purchase's unspent credits cancelled by a refund (#73, migration 0062). One
    row per purchase, append-only; its existence makes every remaining credit unredeemable
    (`billing/redemption.py::_eligible`, and the redemption insert guard in the database).
    Credits already redeemed stay redeemed — voiding only reaches what is left."""

    __tablename__ = "package_credit_voids"

    package_purchase_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("package_purchases.id", ondelete="RESTRICT"), primary_key=True
    )
    reason: Mapped[str] = mapped_column(Text)
    voided_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    voided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

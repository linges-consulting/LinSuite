"""Tax components and their effective-dated rates (#57; #54 stories 30-33).

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
    String,
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

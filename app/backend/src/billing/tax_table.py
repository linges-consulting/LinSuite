"""The built-in table of Canadian sales-tax components (#118, spec #113 "Tax pre-fill").

Pure data plus pure lookups — no database, no `Business` — so the table itself is provable at
S2 (`tests/test_billing_tax_table.py`), the same split `billing/tax.py` already draws for the
arithmetic. `billing/tax_prefill.py` is the only caller that turns a lookup here into
`TaxComponent`/`TaxComponentRate` rows.

**Every rate below was checked against its government source on 2026-09-29** (the date this
file was written) — see each `TableComponent.source`. No discrepancy was found against the
spec's own list (#113, #118): GST 5% (AB, BC, MB, NT, NU, QC, SK, YT); HST 13% (ON), 15% (NB,
NL, PE), 14% in NS from 2025-04-01 (15% before); BC PST 7%; SK PST 6%; MB RST 7%; QC QST
9.975%.

**QST is exact.** `billing/models.py::TaxComponentRate.rate_ppm` is an `Integer` in parts per
million (`MAX_TAX_RATE_PPM`, #119) — 9.975% is exactly 99_750 ppm, no rounding needed. Before
#119 this table (and the existing admin CRUD, `billing/tax_routes.py`) could only store whole
basis points, which rounded QST's legal rate to 9.98%; the ppm scale removes that discrepancy
entirely.

**Out of scope (spec #113):** tax outside Canada, and automatic updates for an existing
tenant's own components when a rate in this table changes — the table only changes in a
release, and `billing/tax_routes.py`'s own status read is what prompts an already-provisioned
business, never rewrites it.
"""

from dataclasses import dataclass
from datetime import date as Date


@dataclass(frozen=True)
class TableRate:
    """One dated rate in a table component's own history. Unlike
    `billing/models.py::TaxComponentRate`, there is no `effective_to` — the last entry (highest
    `effective_from`) is simply the one in force until a future release appends another."""

    effective_from: Date
    rate_ppm: int


@dataclass(frozen=True)
class TableComponent:
    code: str
    name: str
    provinces: frozenset[str]
    rates: tuple[TableRate, ...]  # ascending by effective_from
    source: str


CANADIAN_TAX_COMPONENTS: tuple[TableComponent, ...] = (
    TableComponent(
        code="GST",
        name="GST",
        provinces=frozenset({"AB", "BC", "MB", "NT", "NU", "QC", "SK", "YT"}),
        rates=(TableRate(Date(2008, 1, 1), 50_000),),
        source=(
            'Canada Revenue Agency, "Charge and collect the GST/HST — Which rate to charge", '
            "https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/"
            "gst-hst-businesses/charge-collect-which-rate.html — 5% federal rate since "
            "2008-01-01 (checked 2026-09-29)"
        ),
    ),
    TableComponent(
        code="HST",
        name="HST",
        provinces=frozenset({"ON"}),
        rates=(TableRate(Date(2010, 7, 1), 130_000),),
        source=(
            'Canada Revenue Agency / Ontario Ministry of Finance, "Harmonized Sales Tax", '
            "https://www.ontario.ca/document/harmonized-sales-tax-hst — 13% since 2010-07-01 "
            "(checked 2026-09-29)"
        ),
    ),
    TableComponent(
        code="HST",
        name="HST",
        provinces=frozenset({"NB", "NL"}),
        rates=(TableRate(Date(2016, 7, 1), 150_000),),
        source=(
            'Canada Revenue Agency, "New Brunswick and Newfoundland and Labrador HST rate '
            'increases", https://www.canada.ca/en/revenue-agency/services/forms-publications/'
            "publications/gi-189/new-brunswick-newfoundland-labrador-hst-rate-increases-"
            "information-non-registrant-builders.html — 13% to 15% effective 2016-07-01 "
            "(checked 2026-09-29)"
        ),
    ),
    TableComponent(
        code="HST",
        name="HST",
        provinces=frozenset({"PE"}),
        rates=(TableRate(Date(2016, 10, 1), 150_000),),
        source=(
            "Canada Gazette, SOR/2016-212, https://gazette.gc.ca/rp-pr/p2/2016/2016-07-27/html/"
            "sor-dors212-eng.html — Prince Edward Island HST 14% to 15% effective 2016-10-01 "
            "(checked 2026-09-29)"
        ),
    ),
    TableComponent(
        code="HST",
        name="HST",
        provinces=frozenset({"NS"}),
        rates=(
            TableRate(Date(2010, 7, 1), 150_000),
            TableRate(Date(2025, 4, 1), 140_000),
        ),
        source=(
            "Canada Revenue Agency GST/HST rate table and Government of Nova Scotia budget "
            "announcement, https://www.canada.ca/en/revenue-agency/services/tax/businesses/"
            "topics/gst-hst-businesses/charge-collect-which-rate.html — 15% from 2010-07-01, "
            "reduced to 14% effective 2025-04-01 (checked 2026-09-29)"
        ),
    ),
    TableComponent(
        code="PST",
        name="BC PST",
        provinces=frozenset({"BC"}),
        rates=(TableRate(Date(2013, 4, 1), 70_000),),
        source=(
            'Province of British Columbia, "B.C. provincial sales tax (PST)", '
            "https://www2.gov.bc.ca/gov/content/taxes/sales-taxes/pst — 7% general rate, in "
            "effect since PST's 2013-04-01 reinstatement (checked 2026-09-29)"
        ),
    ),
    TableComponent(
        code="PST",
        name="SK PST",
        provinces=frozenset({"SK"}),
        rates=(TableRate(Date(2017, 3, 23), 60_000),),
        source=(
            'Government of Saskatchewan, "Provincial Sales Tax", '
            "https://www.saskatchewan.ca/business/taxes-licensing-and-reporting/"
            "provincial-taxes-policies-and-bulletins/provincial-sales-tax — 6% since 2017-03-23 "
            "(checked 2026-09-29)"
        ),
    ),
    TableComponent(
        code="RST",
        name="MB RST",
        provinces=frozenset({"MB"}),
        rates=(TableRate(Date(2019, 7, 1), 70_000),),
        source=(
            'Government of Manitoba, "Retail Sales Tax", '
            "https://www.gov.mb.ca/finance/taxation/taxes/retail.html — 7% since 2019-07-01 "
            "(checked 2026-09-29)"
        ),
    ),
    TableComponent(
        code="QST",
        name="QST",
        provinces=frozenset({"QC"}),
        rates=(TableRate(Date(2013, 1, 1), 99_750),),  # 9.975%, exact in ppm
        source=(
            'Revenu Québec, "Basic Rules for Applying the GST/HST and QST — Tables of GST and '
            'QST Rates", https://www.revenuquebec.ca/en/businesses/consumption-taxes/'
            "gsthst-and-qst/basic-rules-for-applying-the-gsthst-and-qst/tables-of-gst-and-"
            "qst-rates/ — 9.975% since 2013-01-01, stored here as 99_750 ppm, exact "
            "(checked 2026-09-29)"
        ),
    ),
)


def rate_as_of(component: TableComponent, as_of: Date) -> TableRate | None:
    """The rate in force on `as_of` — the last entry whose `effective_from` is not after it.
    `None` if the table's history does not reach back that far."""
    in_force = [r for r in component.rates if r.effective_from <= as_of]
    return in_force[-1] if in_force else None


def components_for_province(province: str, as_of: Date) -> list[tuple[TableComponent, TableRate]]:
    """Every table component this province is subject to, each paired with the rate in force on
    `as_of` — what `billing/tax_prefill.py` turns into rows. A component with no rate yet in
    force on `as_of` is left out rather than created with no price."""
    resolved = []
    for component in CANADIAN_TAX_COMPONENTS:
        if province not in component.provinces:
            continue
        rate = rate_as_of(component, as_of)
        if rate is not None:
            resolved.append((component, rate))
    return resolved


def find_component(code: str, province: str) -> TableComponent | None:
    """The one table entry (if any) naming `code` that this `province` is subject to — used to
    check a business's own component against the table's current rate (`billing/tax_routes.py`'s
    prompt logic), never to create anything itself."""
    for component in CANADIAN_TAX_COMPONENTS:
        if component.code == code and province in component.provinces:
            return component
    return None

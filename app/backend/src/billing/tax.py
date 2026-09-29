"""Per-line tax arithmetic: integer cents, half-up rounding, inclusive/exclusive reconciled
exactly to the cent (#57; CLAUDE.md "Money is integer cents... Tax per line, half-up, then
summed"; #54 story 33).

Pure — no database, no ORM, no `Business`. `billing/models.py` resolves which components
apply and what each one's rate is *as of a date*; this module only turns already-resolved
`ComponentRate`s into cents. That split is what makes this seam S2 (plain pytest) rather than
needing a database for a rounding rule.

Nothing here reads or writes an invoice — #65 is what snapshots a `LineTax` onto one.
"""

from dataclasses import dataclass, field
from datetime import date as Date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

BASIS_POINTS = 10_000

TaxConvention = Literal["inclusive", "exclusive"]


@dataclass(frozen=True)
class ComponentRate:
    """One tax component's rate, already resolved for the line's effective date — what
    `resolve_rate_bp` below returns, paired with the component it came from. `code` is
    carried through so a caller can label each amount without a second lookup."""

    code: str
    rate_bp: int


@dataclass(frozen=True)
class LineTax:
    """One line's resolved amounts, all integer cents.

    `pretax_cents + tax_cents == total_cents` always — for an inclusive line, `total_cents`
    is exactly the amount that was entered, not an approximation of it (the acceptance
    criterion: "inclusive-price extraction... reconciles exactly to the cent"). Every
    component the line was given appears in `component_cents`, including an exempt
    (`rate_bp == 0`) one at 0, so a receipt never has to guess whether a component was
    skipped or genuinely zero.
    """

    pretax_cents: int
    component_cents: dict[str, int] = field(default_factory=dict)
    tax_cents: int = 0
    total_cents: int = 0


def _round_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def compute_line_tax(
    amount_cents: int, components: list[ComponentRate], convention: TaxConvention
) -> LineTax:
    """Tax on one catalog/override line (#54 stories 30-33).

    **Exclusive** — `amount_cents` is the pre-tax price. Each component is
    `round_half_up(amount_cents * rate_bp / 10000)`, independently; the total is the pretax
    amount plus the sum of those. CLAUDE.md's "tax per line, half-up, then summed", applied
    per component rather than once for the line, so a multi-component charge (GST+PST)
    reconciles component-by-component and not only in aggregate.

    **Inclusive** — `amount_cents` is the final, tax-included price the catalog/override
    actually states. The pre-tax price is backed out as
    `round_half_up(amount_cents * 10000 / (10000 + combined_rate_bp))`, and the tax is the
    *residual* (`amount_cents - pretax_cents`), not a second rounded computation — that
    residual is what guarantees `pretax_cents + tax_cents == amount_cents` exactly, instead of
    off by the odd cent a naive per-component inclusive extraction would accumulate. The
    residual is then split across components by `_allocate`, proportional to each `rate_bp`,
    so the per-component amounts still sum to it exactly.

    A component with `rate_bp == 0` (an exempt component explicitly listed) contributes
    nothing to `combined_rate_bp` and is reported at 0 — it is never inferred from anything
    about the item or the practitioner delivering it (CLAUDE.md, #54's own acceptance
    criterion), only from the rate the caller resolved and handed in.
    """
    if amount_cents < 0:
        raise ValueError("amount_cents must not be negative")

    live = [c for c in components if c.rate_bp != 0]
    combined_bp = sum(c.rate_bp for c in live)

    if convention == "exclusive":
        pretax = amount_cents
        component_cents = {
            c.code: _round_half_up(Decimal(pretax) * c.rate_bp / BASIS_POINTS) for c in live
        }
        tax_cents = sum(component_cents.values())
        total = pretax + tax_cents
    else:
        total = amount_cents
        if combined_bp == 0:
            pretax = amount_cents
            component_cents = {}
            tax_cents = 0
        else:
            pretax = _round_half_up(
                Decimal(amount_cents) * BASIS_POINTS / (BASIS_POINTS + combined_bp)
            )
            tax_cents = amount_cents - pretax
            component_cents = _allocate(tax_cents, live)

    for component in components:
        component_cents.setdefault(component.code, 0)
    return LineTax(
        pretax_cents=pretax,
        component_cents=component_cents,
        tax_cents=tax_cents,
        total_cents=total,
    )


def _allocate(total_cents: int, components: list[ComponentRate]) -> dict[str, int]:
    """Split `total_cents` across `components` proportional to `rate_bp`, largest-remainder
    method, so the parts sum to exactly `total_cents` — the reconciliation criterion applied
    to each component, not just the line total."""
    combined_bp = sum(c.rate_bp for c in components)
    shares = {c.code: Decimal(total_cents) * c.rate_bp / combined_bp for c in components}
    floors = {code: int(share) for code, share in shares.items()}  # truncation, not rounding
    remainder = total_cents - sum(floors.values())
    # The largest fractional remainder gets the leftover cent(s) first; `code` is the
    # tiebreak, so two equal fractions always resolve to the same component.
    order = sorted(shares, key=lambda code: (shares[code] - floors[code], code), reverse=True)
    for code in order[:remainder]:
        floors[code] += 1
    return floors


def invoice_tax_totals(lines: list[LineTax]) -> dict[str, int]:
    """Per-component totals across a whole invoice: the sum of each line's own already-rounded
    amount, never a re-derivation from the invoice grand total (CLAUDE.md "per line...
    then summed")."""
    totals: dict[str, int] = {}
    for line in lines:
        for code, cents in line.component_cents.items():
            totals[code] = totals.get(code, 0) + cents
    return totals


def resolve_rate_bp(rates: list[tuple[Date, Date | None, int]], as_of: Date) -> int | None:
    """The rate in effect on `as_of`, from a component's full rate history as
    `(effective_from, effective_to, rate_bp)` tuples.

    `effective_from` inclusive, `effective_to` exclusive — matching the `daterange(
    effective_from, effective_to)` the database's own exclusion constraint uses
    (`billing/models.py::TaxComponentRate`), so this function and the constraint that keeps
    the rows non-overlapping never disagree about a boundary date. Returns `None` when no row
    covers `as_of` (the component did not exist yet, or its history has a gap).
    """
    for effective_from, effective_to, rate_bp in rates:
        if effective_from <= as_of and (effective_to is None or as_of < effective_to):
            return rate_bp
    return None

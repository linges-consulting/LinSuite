"""One priced line, and an admin override spread across a bill's lines (S2, no database).

The single place service bills, retail sales and package purchases turn "entered price +
discounts + this item's tax components/convention" into cents, so the three never disagree.

**Discounts apply to the entered amount, in the item's own convention**: a tax-inclusive
price is discounted as entered, then tax is backed out of what is left (`tax.compute_line_tax`
"inclusive"); a tax-exclusive price is discounted, then taxed on top. Commission basis is
always pre-tax (spec §142 "Tax is excluded from commission").

**An override is a whole-bill total, distributed over the collectible lines** (review R5):
prepaid lines keep their frozen package value (so `balances()`' prepaid subtraction stays
exact, R6); the rest of the total is split in proportion to each line's computed amount
(largest remainder, `allocation.allocate_bundle_price`), then each share is re-taxed in the
override's own convention. Lines, per-component tax rows and totals then all sum to the
override exactly (inclusive entry) or to override + the tax on it (exclusive entry).
"""

from collections.abc import Sequence
from dataclasses import dataclass

from billing.allocation import allocate_bundle_price
from billing.commission import CommissionDiscountInput, commission_basis_cents
from billing.discount_resolver import DiscountInput, resolve_discount_amounts
from billing.tax import ComponentRate, LineTax, TaxConvention, compute_line_tax


@dataclass(frozen=True)
class PricedLine:
    amount_cents: int  # as entered (unit price x quantity), before discounts
    convention: TaxConvention
    discounted_cents: int  # after discounts, same convention as `amount_cents`
    tax: LineTax
    # Cents each applied discount took off, summing exactly to amount - discounted.
    discount_amounts: dict
    # Pre-tax; "absorbed" discounts left out (`commission.commission_basis_cents`).
    commission_basis_cents: int


def price_line(
    amount_cents: int,
    convention: TaxConvention,
    components: Sequence[ComponentRate],
    discounts: Sequence[CommissionDiscountInput],
) -> PricedLine:
    """Raises `discount_resolver.DiscountConflict` for a combination that cannot apply."""
    inputs = [
        DiscountInput(
            id=d.id,
            kind=d.kind,
            stackable=d.stackable,
            percentage_bp=d.percentage_bp,
            amount_cents=d.amount_cents,
        )
        for d in discounts
    ]
    amounts = resolve_discount_amounts(amount_cents, inputs)
    discounted = amount_cents - sum(amounts.values())
    tax = compute_line_tax(discounted, list(components), convention)
    basis = commission_basis_cents(amount_cents, list(discounts))
    if convention == "inclusive":
        basis = compute_line_tax(basis, list(components), "inclusive").pretax_cents
    return PricedLine(
        amount_cents=amount_cents,
        convention=convention,
        discounted_cents=discounted,
        tax=tax,
        discount_amounts=amounts,
        commission_basis_cents=basis,
    )


class OverrideConflict(ValueError):
    """An override total the bill's lines cannot carry (below what is already prepaid)."""


def distribute_override(
    total_cents: int,
    convention: TaxConvention,
    lines: Sequence[tuple[LineTax, Sequence[ComponentRate], bool]],
) -> list[LineTax]:
    """Each line's billed `LineTax` under a whole-bill override. `lines` is
    `(computed LineTax, that line's components, is_prepaid)` in bill order.

    `total_cents` is tax-inclusive or pre-tax per `convention`. Prepaid lines are returned
    unchanged (untaxed, so their pre-tax and total are the same number); the remainder is
    split over the other lines by their computed total (inclusive) or pre-tax (exclusive)."""
    prepaid_total = sum(tax.total_cents for tax, _, prepaid in lines if prepaid)
    collectible = [i for i, (_, _, prepaid) in enumerate(lines) if not prepaid]
    remaining = total_cents - prepaid_total
    if remaining < 0:
        raise OverrideConflict(
            f"The override total cannot be less than the {prepaid_total} cents already "
            "prepaid by package credits."
        )
    if not collectible:
        if remaining:
            raise OverrideConflict("Every line is prepaid; there is nothing to override.")
        return [tax for tax, _, _ in lines]

    weights = [
        lines[i][0].total_cents if convention == "inclusive" else lines[i][0].pretax_cents
        for i in collectible
    ]
    shares = allocate_bundle_price(weights, remaining)
    billed = [tax for tax, _, _ in lines]
    for index, share in zip(collectible, shares, strict=True):
        billed[index] = compute_line_tax(share, list(lines[index][1]), convention)
    return billed

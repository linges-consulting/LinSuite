"""Pure money math: splitting a bundle's purchase price across its constituent services.

**S2** (plain pytest, no DB). Nothing here reads or writes anything — #71 (bundle purchase)
is the caller that will feed it `Service.price_cents` for a `PackageDefinition`'s
`PackageDefinitionService` rows and freeze the result onto the purchase's own lines, per
CLAUDE.md's package/bundle rule: "proportional value allocation for bundles (frozen at
purchase time, not recomputed later)".

**The problem integer division always has**: `purchase_price_cents * weight / total_weight`
is not itself an integer, and rounding each share independently (even half-up, per line) can
land one cent short of or over the whole. Splitting `100` three ways at `33.33` each is the
textbook case. The largest-remainder method (Hamilton apportionment) is the standard
deterministic fix: floor every share, then hand the leftover cents out one at a time to the
shares that lost the most to flooring, highest-remainder first. Ties break on position, so
the same input always allocates the same way — no `Decimal` context, no float, ever.
"""

from collections.abc import Sequence


def allocate_bundle_price(
    regular_prices_cents: Sequence[int], purchase_price_cents: int
) -> list[int]:
    """Split `purchase_price_cents` across `regular_prices_cents` in proportion to each price.

    Worked example (#60's acceptance criteria): regular prices 120/60/60, purchase price 200
    -> 100/50/50 (each price is exactly 5/6 of its regular one; no remainder to distribute).

    Returns one allocated amount per input price, in the same order, always summing exactly
    to `purchase_price_cents` — the one property a caller building an invoice depends on.
    A price of `0` is a legal weight (a comped constituent gets nothing allocated to it,
    unless every price is `0`, in which case the purchase price is split evenly).
    """
    if not regular_prices_cents:
        raise ValueError("need at least one service to allocate a price across")
    if any(price < 0 for price in regular_prices_cents):
        raise ValueError("a regular price cannot be negative")
    if purchase_price_cents < 0:
        raise ValueError("a purchase price cannot be negative")

    total = sum(regular_prices_cents)
    # Every constituent priced at zero: proportion-to-price has nothing to divide by, so fall
    # back to an even split (still by the same largest-remainder rule below) rather than a
    # ZeroDivisionError or an arbitrary "first item gets everything".
    weights = regular_prices_cents if total > 0 else [1] * len(regular_prices_cents)
    total = total if total > 0 else len(regular_prices_cents)

    shares = [(weight * purchase_price_cents) // total for weight in weights]
    remainders = [(weight * purchase_price_cents) % total for weight in weights]

    short_by = purchase_price_cents - sum(shares)
    # `short_by` is always < len(weights): flooring each share loses less than one whole unit
    # per term, so the total lost across every term is less than the number of terms.
    for index in sorted(range(len(weights)), key=lambda i: (-remainders[i], i))[:short_by]:
        shares[index] += 1

    return shares

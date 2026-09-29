"""The pure discount-stacking math (#58, S2). No database, no FastAPI — a route or a Celery
task in a later ticket (#63) is what will call this against a real bill; this module only
knows about numbers and the two small facts about a discount its math needs.

**All percentage discounts apply first, as one combined rate, then every fixed amount is
subtracted from what's left** — and the combined rate is a product of factors, which is
commutative, so the result never depends on the order discounts were selected in. Rounding
happens once, after the percentages and before the fixed amounts, rather than compounding a
rounded result off a rounded result: 100 with a 10% and a 20% discount is 72 cents, however
they were picked (#58 acceptance criterion; CLAUDE.md "tax per line, half-up" — the same
half-up rule, applied here to a discount instead of a tax line).

**Never clamps to zero.** A combination that would take a charge below zero is rejected with
`DiscountConflict`, a plain reason a caller can show verbatim — silently flooring it would
hide a definition mistake (a $50 fixed discount nobody meant to allow on a $20 service) behind
an invoice that looks merely deep-discounted rather than wrong.
"""

import uuid
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

Kind = Literal["percentage", "fixed"]


@dataclass(frozen=True)
class DiscountInput:
    """The three facts `resolve_stacked_discounts` needs about one discount in the set.
    Everything else a `Discount` row carries (name, eligibility, commission basis) is the
    caller's business, not this function's."""

    id: uuid.UUID
    kind: Kind
    stackable: bool
    # Basis points 0-10000, set iff `kind == "percentage"`.
    percentage_bp: int | None = None
    # Integer cents >= 0, set iff `kind == "fixed"`.
    amount_cents: int | None = None


class DiscountConflict(ValueError):
    """A set of discounts can't combine as given — two non-stackable discounts together, or a
    combination that would take the charge below zero. `str(error)` is a reason fit to show
    the person who picked them; nothing here resolves it to a smaller or zero amount instead.
    """


def resolve_stacked_discounts(charge_cents: int, discounts: list[DiscountInput]) -> int:
    """The charge, in cents, after every discount in `discounts` has been applied.

    Raises `DiscountConflict` — never returns a negative amount or silently floors to zero —
    when more than one discount is present and at least one isn't `stackable`, or when the
    combination would exceed `charge_cents`.
    """
    if len(discounts) > 1 and not all(d.stackable for d in discounts):
        culprits = sorted(str(d.id) for d in discounts if not d.stackable)
        raise DiscountConflict(
            "These discounts are not stackable and can't be combined: " + ", ".join(culprits)
        )

    percentage_factor = Decimal(1)
    for d in discounts:
        if d.kind == "percentage":
            assert d.percentage_bp is not None  # noqa: S101 — DB CHECK guarantees this
            percentage_factor *= Decimal(10_000 - d.percentage_bp) / Decimal(10_000)

    after_percentage = (Decimal(charge_cents) * percentage_factor).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    )

    fixed_total = sum((d.amount_cents or 0 for d in discounts if d.kind == "fixed"), 0)

    final_cents = int(after_percentage) - fixed_total
    if final_cents < 0:
        raise DiscountConflict("These discounts together exceed the eligible charge.")
    return final_cents


def resolve_discount_amounts(
    charge_cents: int, discounts: list[DiscountInput]
) -> dict[uuid.UUID, int]:
    """The cents each discount took off, frozen on an issued line (spec §140 "persist the
    selected rule and resolved amounts"). Sums exactly to `charge_cents -
    resolve_stacked_discounts(...)`, so it raises the same `DiscountConflict`.

    Percentages are taken sequentially in id order (deterministic, independent of selection
    order), each half-up on the running amount; the last percentage absorbs the difference to
    the resolver's own once-rounded result. Fixed amounts are their face value."""
    final_cents = resolve_stacked_discounts(charge_cents, discounts)
    percentages = sorted((d for d in discounts if d.kind == "percentage"), key=lambda d: d.id)
    fixed = {d.id: d.amount_cents or 0 for d in discounts if d.kind == "fixed"}
    percentage_total = charge_cents - final_cents - sum(fixed.values())

    amounts: dict[uuid.UUID, int] = {}
    running = Decimal(charge_cents)
    for d in percentages[:-1]:
        cents = int(
            (running * (d.percentage_bp or 0) / Decimal(10_000)).quantize(
                Decimal(1), rounding=ROUND_HALF_UP
            )
        )
        amounts[d.id] = cents
        running -= cents
    if percentages:
        amounts[percentages[-1].id] = percentage_total - sum(amounts.values())
    return amounts | fixed


@dataclass(frozen=True)
class DiscountEligibility:
    """The two facts an eligibility check needs off a `Discount` row: whether it applies to
    everything, and — when it doesn't — the exact `(item_type, item_id)` pairs it does."""

    applies_to_all: bool
    eligible_items: frozenset[tuple[str, uuid.UUID]] = field(default_factory=frozenset)


def is_eligible(discount: DiscountEligibility, item_type: str, item_id: uuid.UUID) -> bool:
    """Whether a discount scoped by `discount` may be applied to this catalog item."""
    return discount.applies_to_all or (item_type, item_id) in discount.eligible_items

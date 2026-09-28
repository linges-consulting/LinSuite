"""S2: the pure discount-stacking resolver (#58). No database, no client fixture — plain
pytest against `billing/discount_resolver.py`, per CLAUDE.md's own testing-seams split.
"""

import uuid

import pytest

from billing.discount_resolver import (
    DiscountConflict,
    DiscountEligibility,
    DiscountInput,
    is_eligible,
    resolve_stacked_discounts,
)


def _pct(bp: int, *, stackable: bool = True) -> DiscountInput:
    return DiscountInput(id=uuid.uuid4(), kind="percentage", stackable=stackable, percentage_bp=bp)


def _fixed(cents: int, *, stackable: bool = True) -> DiscountInput:
    return DiscountInput(id=uuid.uuid4(), kind="fixed", stackable=stackable, amount_cents=cents)


# --- the worked example (#58's own acceptance criterion) ------------------------------------


def test_ten_percent_then_twenty_percent_on_100_is_72_hand_computed():
    # By hand, not by calling the resolver first: 100 * (1 - 0.10) = 90; 90 * (1 - 0.20) = 72.
    # $1.00 -> 90c -> 72c.
    ten, twenty = _pct(1000), _pct(2000)
    assert resolve_stacked_discounts(100, [ten, twenty]) == 72


def test_result_is_independent_of_selection_order():
    ten, twenty = _pct(1000), _pct(2000)
    forward = resolve_stacked_discounts(100, [ten, twenty])
    reversed_order = resolve_stacked_discounts(100, [twenty, ten])
    assert forward == reversed_order == 72


def test_percentages_compound_before_fixed_amounts_apply_regardless_of_list_order():
    # By hand: $10.00 (1000c) * 0.90 * 0.80 = 720c after the two percentages, then -500c for
    # the $5 fixed discount = 220c. Computed independently of the implementation, and checked
    # with the fixed discount sandwiched between the two percentages in the input list, to
    # prove list position — not "percentage vs. fixed" — is what's irrelevant.
    ten, twenty, five_dollars = _pct(1000), _pct(2000), _fixed(500)
    assert resolve_stacked_discounts(1000, [ten, five_dollars, twenty]) == 220
    assert resolve_stacked_discounts(1000, [five_dollars, twenty, ten]) == 220


# --- stackable conflicts ----------------------------------------------------------------------


def test_a_lone_nonstackable_discount_applies_fine():
    assert resolve_stacked_discounts(100, [_pct(1000, stackable=False)]) == 90


def test_two_discounts_where_one_is_not_stackable_is_rejected():
    stackable = _pct(1000, stackable=True)
    not_stackable = _pct(2000, stackable=False)
    with pytest.raises(DiscountConflict):
        resolve_stacked_discounts(100, [stackable, not_stackable])


def test_two_stackable_discounts_combine_without_complaint():
    resolve_stacked_discounts(100, [_pct(1000, stackable=True), _pct(2000, stackable=True)])


# --- reject rather than clamp to zero ----------------------------------------------------------


def test_a_fixed_discount_larger_than_the_charge_is_rejected_not_clamped():
    with pytest.raises(DiscountConflict):
        resolve_stacked_discounts(50, [_fixed(100)])


def test_a_full_percentage_plus_any_fixed_amount_is_rejected_not_clamped():
    # 100% off leaves 0c; a fixed discount on top of that would go negative.
    with pytest.raises(DiscountConflict):
        resolve_stacked_discounts(100, [_pct(10_000), _fixed(1)])


def test_a_combination_that_exactly_zeroes_the_charge_is_allowed():
    assert resolve_stacked_discounts(100, [_pct(10_000)]) == 0


# --- eligibility -------------------------------------------------------------------------------


def test_a_discount_scoped_to_all_items_is_eligible_for_anything():
    discount = DiscountEligibility(applies_to_all=True)
    assert is_eligible(discount, "service", uuid.uuid4())
    assert is_eligible(discount, "product", uuid.uuid4())


def test_a_selected_scope_discount_is_only_eligible_for_its_named_items():
    haircut = uuid.uuid4()
    shampoo = uuid.uuid4()
    discount = DiscountEligibility(
        applies_to_all=False, eligible_items=frozenset({("service", haircut)})
    )
    assert is_eligible(discount, "service", haircut)
    assert not is_eligible(discount, "product", shampoo)
    assert not is_eligible(discount, "service", uuid.uuid4())


def test_a_selected_scope_discount_with_no_items_is_eligible_for_nothing():
    discount = DiscountEligibility(applies_to_all=False)
    assert not is_eligible(discount, "service", uuid.uuid4())

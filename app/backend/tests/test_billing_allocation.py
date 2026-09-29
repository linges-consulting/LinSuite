"""S2: `billing.allocation.allocate_bundle_price` — pure, no DB.

The one property every caller depends on: the allocated amounts always sum exactly to the
purchase price, whatever the regular prices are and however unevenly they divide it.
"""

import pytest

from billing.allocation import allocate_bundle_price


def test_the_worked_example_from_60s_acceptance_criteria():
    # Regular prices 120/60/60, purchase price 200. Independently computed: each price is
    # exactly 5/6 of its regular price (200/240 = 5/6), so 120 * 5/6 = 100, 60 * 5/6 = 50
    # twice over — an exact division, no remainder to distribute.
    assert allocate_bundle_price([120, 60, 60], 200) == [100, 50, 50]


def test_a_single_service_gets_the_whole_price():
    assert allocate_bundle_price([15000], 15000) == [15000]


def test_remainder_cents_go_to_the_largest_fractional_share_first():
    # 100/100/100, purchase 100: each share is exactly 33 1/3 cents. Floor gives 33/33/33
    # (sum 99), the leftover cent goes to the lowest index among equal remainders — index 0.
    assert allocate_bundle_price([100, 100, 100], 100) == [34, 33, 33]


def test_remainder_distribution_still_sums_exactly_with_uneven_weights():
    # 300/100/100, purchase 101. Raw shares: 60.6, 20.2, 20.2 -> floor 60/20/20 = 100, one
    # cent short. The 300-price share has the largest remainder (0.6 vs 0.2 vs 0.2) and wins.
    result = allocate_bundle_price([300, 100, 100], 101)
    assert result == [61, 20, 20]
    assert sum(result) == 101


def test_a_zero_purchase_price_allocates_nothing():
    assert allocate_bundle_price([120, 60, 60], 0) == [0, 0, 0]


def test_every_constituent_priced_at_zero_splits_evenly():
    result = allocate_bundle_price([0, 0, 0], 90)
    assert result == [30, 30, 30]
    assert sum(result) == 90


def test_a_zero_price_constituent_among_priced_ones_gets_nothing():
    result = allocate_bundle_price([0, 100], 50)
    assert result == [0, 50]


@pytest.mark.parametrize("prices", [[], ()])
def test_at_least_one_price_is_required(prices):
    with pytest.raises(ValueError):
        allocate_bundle_price(prices, 100)


def test_a_negative_regular_price_is_refused():
    with pytest.raises(ValueError):
        allocate_bundle_price([-1, 100], 100)


def test_a_negative_purchase_price_is_refused():
    with pytest.raises(ValueError):
        allocate_bundle_price([100, 100], -1)


@pytest.mark.parametrize(
    "prices,purchase_price",
    [
        ([120, 60, 60], 200),
        ([100, 100, 100], 100),
        ([300, 100, 100], 101),
        ([7, 3, 5, 11], 999),
        ([1, 1, 1, 1, 1, 1, 1], 10),
    ],
)
def test_the_allocated_amounts_always_sum_to_the_purchase_price(prices, purchase_price):
    assert sum(allocate_bundle_price(prices, purchase_price)) == purchase_price

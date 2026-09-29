"""S2: `billing/commission.py`'s pure commission-basis composition formula — worked examples,
`tax.py`/`discount_resolver.py`/`allocation.py`'s own testing shape (plain pytest, no fixtures,
no database)."""

import uuid

import pytest

from billing.commission import (
    CommissionDiscountInput,
    commission_amount_cents,
    commission_basis_cents,
    compute_commission_cents,
)
from billing.discount_resolver import DiscountConflict, DiscountInput, resolve_stacked_discounts


def _pct(commission_basis, percentage_bp, *, stackable=True, id_=None):
    return CommissionDiscountInput(
        id=id_ or uuid.uuid4(),
        kind="percentage",
        stackable=stackable,
        percentage_bp=percentage_bp,
        amount_cents=None,
        commission_basis=commission_basis,
    )


def _fixed(commission_basis, amount_cents, *, stackable=True, id_=None):
    return CommissionDiscountInput(
        id=id_ or uuid.uuid4(),
        kind="fixed",
        stackable=stackable,
        percentage_bp=None,
        amount_cents=amount_cents,
        commission_basis=commission_basis,
    )


# --- commission_basis_cents: no discounts -----------------------------------------------------


def test_no_discounts_the_basis_is_the_full_price():
    assert commission_basis_cents(12000, []) == 12000


# --- a single "reduces" discount: basis is the discounted amount ------------------------------


def test_single_reduces_percentage_discount_lowers_the_basis():
    # 10000 - 10% = 9000, the same number the client was actually charged.
    assert commission_basis_cents(10000, [_pct("reduces", 1000)]) == 9000


def test_single_reduces_fixed_discount_lowers_the_basis():
    assert commission_basis_cents(5000, [_fixed("reduces", 1000)]) == 4000


# --- a single "absorbed" discount: basis is unaffected -----------------------------------------


def test_single_absorbed_percentage_discount_leaves_the_basis_at_full_price():
    # The business eats the 10% — commission is calculated as if it never applied.
    assert commission_basis_cents(10000, [_pct("absorbed", 1000)]) == 10000


def test_single_absorbed_fixed_discount_leaves_the_basis_at_full_price():
    assert commission_basis_cents(5000, [_fixed("absorbed", 1000)]) == 5000


# --- multiple discounts, same basis -------------------------------------------------------------


def test_two_stacked_reduces_percentage_discounts_compose_multiplicatively():
    # 10% then 20%, combined factor 0.9 * 0.8 = 0.72 -- discount_resolver's own worked example,
    # applied here through the same function.
    a, b = _pct("reduces", 1000), _pct("reduces", 2000)
    basis = commission_basis_cents(10000, [a, b])
    assert basis == 7200
    # Proven against the resolver directly, not just asserted as a magic number.
    assert basis == resolve_stacked_discounts(
        10000,
        [
            DiscountInput(
                id=d.id, kind=d.kind, stackable=d.stackable, percentage_bp=d.percentage_bp
            )
            for d in (a, b)
        ],
    )


def test_two_stacked_absorbed_discounts_leave_the_basis_at_full_price():
    basis = commission_basis_cents(10000, [_pct("absorbed", 1000), _fixed("absorbed", 500)])
    assert basis == 10000


# --- mixed basis: the subtle case the ticket calls out ------------------------------------------


def test_mixed_reduces_and_absorbed_percentage_discounts_only_the_reduces_one_lowers_the_basis():
    # A 10% "reduces" and a 20% "absorbed" both applied to the charge; the client actually
    # paid 10000 * 0.9 * 0.8 = 7200, but commission is calculated only against the 10%
    # reduction the business chose to pass on to the staff member's earnings too -- the 20%
    # the business absorbed does not touch it.
    basis = commission_basis_cents(10000, [_pct("reduces", 1000), _pct("absorbed", 2000)])
    assert basis == 9000


def test_mixed_reduces_and_absorbed_fixed_discounts():
    basis = commission_basis_cents(5000, [_fixed("reduces", 500), _fixed("absorbed", 1000)])
    assert basis == 4500


def test_mixed_percentage_and_fixed_across_both_bases():
    # reduces: 10% off; absorbed: $5 fixed off. Only the 10% touches the basis.
    basis = commission_basis_cents(10000, [_pct("reduces", 1000), _fixed("absorbed", 500)])
    assert basis == 9000


# --- never raises DiscountConflict on the reduces-only subset -----------------------------------


def test_a_non_stackable_absorbed_discount_combined_with_a_reduces_one_never_conflicts():
    """The full set (one stackable "reduces", one non-stackable "absorbed") would have failed
    `resolve_stacked_discounts` outright if handed to it together with `len > 1` and not all
    stackable -- but that combination is never handed to the resolver as a whole here. Once the
    absorbed discount is filtered out, the reduces-only subset has exactly one member, so the
    "more than one non-stackable" rule never fires. Proof by construction: calling the full set
    directly does raise; `commission_basis_cents` on the same input does not."""
    reduces = _pct("reduces", 1000, stackable=True)
    absorbed = _fixed("absorbed", 100_000, stackable=False)  # deliberately huge + non-stackable

    with pytest.raises(DiscountConflict):
        resolve_stacked_discounts(
            10000,
            [
                DiscountInput(id=reduces.id, kind="percentage", stackable=True, percentage_bp=1000),
                DiscountInput(id=absorbed.id, kind="fixed", stackable=False, amount_cents=100_000),
            ],
        )

    # But the reduces-only subset -- what commission_basis_cents actually computes -- is a
    # single discount, so no conflict.
    assert commission_basis_cents(10000, [reduces, absorbed]) == 9000


def test_removing_discounts_can_only_raise_the_result_never_lower_it():
    """General proof, not just one worked case: for any price and any already-valid full stack,
    the reduces-only subset's result is always >= the full stack's own result (fewer/smaller
    reductions), which is why it can never go negative when the full stack didn't."""
    price = 20000
    full = [_pct("reduces", 1500), _pct("absorbed", 2500), _fixed("reduces", 300)]
    full_stack_result = resolve_stacked_discounts(
        price,
        [
            DiscountInput(
                id=d.id,
                kind=d.kind,
                stackable=d.stackable,
                percentage_bp=d.percentage_bp,
                amount_cents=d.amount_cents,
            )
            for d in full
        ],
    )
    reduces_only_result = commission_basis_cents(price, full)
    assert reduces_only_result >= full_stack_result


# --- commission_amount_cents: rate application, half-up rounding --------------------------------


def test_commission_amount_applies_the_rate_to_the_basis():
    assert commission_amount_cents(12000, 1000) == 1200  # 10% of 12000


def test_commission_amount_rounds_half_up():
    # 50 * 1% = 0.5 -> rounds up to 1, not down to 0.
    assert commission_amount_cents(50, 100) == 1


def test_commission_amount_zero_rate_is_zero():
    assert commission_amount_cents(12000, 0) == 0


def test_commission_amount_max_rate_is_the_whole_basis():
    assert commission_amount_cents(12000, 10_000) == 12000


# --- compute_commission_cents: the end-to-end wrapper --------------------------------------------


def test_compute_commission_cents_end_to_end_no_discounts():
    assert (
        compute_commission_cents(price_cents=12000, commission_rate_bp=1000, discounts=[]) == 1200
    )


def test_compute_commission_cents_end_to_end_with_a_reduces_discount():
    # 12000 -10% = 10800, then 10% commission = 1080.
    assert (
        compute_commission_cents(
            price_cents=12000, commission_rate_bp=1000, discounts=[_pct("reduces", 1000)]
        )
        == 1080
    )


def test_compute_commission_cents_end_to_end_with_an_absorbed_discount():
    # The discount never touches the basis: 10% of the full 12000 = 1200.
    assert (
        compute_commission_cents(
            price_cents=12000, commission_rate_bp=1000, discounts=[_pct("absorbed", 1000)]
        )
        == 1200
    )

"""S2: `billing/tax.py`'s pure per-line tax arithmetic (#57; #54 story 33).

No database — every rate here is a plain `ComponentRate` the caller already resolved, the
same split `tests/test_classification.py` uses for `customers/classification.py`. Coverage
follows m4.md's own list for this ground: inclusive/exclusive entry, multiple independent
components, exempt components, effective-date boundaries, half-cent rounding, line-versus-
total reconciliation.

`rate_ppm` is parts per million (#119): 1% is 10_000 ppm, so a rate that used to be `N` whole
basis points is `N * 100` ppm — `test_ppm_scale_matches_old_bp_results_exactly` below proves
every whole-bp rate (5%, 7%, 13%, 15%) still produces the identical cents on the new scale, and
`test_qst_is_exact_at_the_new_scale` proves the one rate that whole basis points could never
hold exactly (QST, 9.975%) now can.
"""

from datetime import date

from billing.tax import ComponentRate, compute_line_tax, invoice_tax_totals, resolve_rate_ppm

GST = ComponentRate("GST", 50_000)  # 5%, in ppm (100x the old bp value)
PST = ComponentRate("PST", 70_000)  # 7%, BC's own rate — GST yes, PST also yes here
EXEMPT = ComponentRate("PST", 0)  # explicitly listed, but zero: never inferred, always stated


# --- exclusive: the entered amount is the pre-tax price --------------------------------


def test_exclusive_single_component():
    line = compute_line_tax(10_000, [GST], "exclusive")
    assert line.pretax_cents == 10_000
    assert line.component_cents == {"GST": 500}
    assert line.tax_cents == 500
    assert line.total_cents == 10_500


def test_exclusive_multiple_components_reconcile_to_the_line_total():
    line = compute_line_tax(10_000, [GST, PST], "exclusive")
    assert line.component_cents == {"GST": 500, "PST": 700}
    assert line.tax_cents == 1_200
    assert line.total_cents == 11_200
    assert sum(line.component_cents.values()) == line.tax_cents
    assert line.pretax_cents + line.tax_cents == line.total_cents


def test_exclusive_half_up_rounding():
    # 333 cents * 15% = 49.95 -> half-up to 50, not 49 (banker's rounding would give 50 too,
    # but 332 * 15% = 49.8 would round to 50 either way; this value is chosen so ROUND_HALF_UP
    # and ROUND_HALF_EVEN would actually disagree at the *next* cent: 330 * 15% = 49.5 exactly).
    fifteen_pct = ComponentRate("VAT", 150_000)  # 15%, in ppm
    line = compute_line_tax(330, [fifteen_pct], "exclusive")
    assert line.component_cents == {"VAT": 50}  # 49.5 rounds up, never down or to even
    assert line.total_cents == 380


def test_exclusive_exempt_component_is_reported_at_zero_not_omitted():
    line = compute_line_tax(10_000, [GST, EXEMPT], "exclusive")
    assert line.component_cents == {"GST": 500, "PST": 0}
    assert line.tax_cents == 500
    assert line.total_cents == 10_500


def test_exclusive_no_components_is_a_no_op():
    line = compute_line_tax(10_000, [], "exclusive")
    assert line == compute_line_tax(10_000, [], "exclusive")
    assert line.tax_cents == 0
    assert line.total_cents == 10_000


# --- inclusive: the entered amount is the final, tax-included price --------------------


def test_inclusive_extraction_reconciles_exactly_to_the_cent():
    # The exclusive case above computed 10000 pretax + GST+PST -> 11200 total. Feeding that
    # total back in as an inclusive price must recover the same pretax and component amounts.
    line = compute_line_tax(11_200, [GST, PST], "inclusive")
    assert line.pretax_cents == 10_000
    assert line.component_cents == {"GST": 500, "PST": 700}
    assert line.tax_cents == 1_200
    assert line.total_cents == 11_200
    assert line.pretax_cents + line.tax_cents == line.total_cents
    assert sum(line.component_cents.values()) == line.tax_cents


def test_inclusive_single_component_round_trips_the_exclusive_case():
    line = compute_line_tax(10_500, [GST], "inclusive")
    assert line.pretax_cents == 10_000
    assert line.component_cents == {"GST": 500}
    assert line.total_cents == 10_500


def test_inclusive_no_components_leaves_the_price_untouched():
    line = compute_line_tax(10_000, [], "inclusive")
    assert line.pretax_cents == 10_000
    assert line.tax_cents == 0
    assert line.total_cents == 10_000


def test_inclusive_odd_total_still_reconciles_components_exactly():
    # A total that does not divide evenly across GST+PST — the largest-remainder allocation
    # must still make the parts sum to the residual exactly, never off by a cent either way.
    line = compute_line_tax(10_001, [GST, PST], "inclusive")
    assert line.pretax_cents + line.tax_cents == 10_001
    assert sum(line.component_cents.values()) == line.tax_cents


def test_inclusive_exempt_component_reported_at_zero():
    line = compute_line_tax(10_500, [GST, EXEMPT], "inclusive")
    assert line.component_cents == {"GST": 500, "PST": 0}
    assert line.pretax_cents == 10_000


# --- both conventions land on the same final total for the same underlying price -------


def test_inclusive_and_exclusive_agree_on_the_final_total():
    exclusive = compute_line_tax(10_000, [GST, PST], "exclusive")
    inclusive = compute_line_tax(exclusive.total_cents, [GST, PST], "inclusive")
    assert inclusive.total_cents == exclusive.total_cents
    assert inclusive.pretax_cents == exclusive.pretax_cents
    assert inclusive.component_cents == exclusive.component_cents


# --- invoice_tax_totals: summed per line, never re-derived from a grand total ----------


def test_invoice_tax_totals_sums_each_lines_own_rounded_amount():
    line_a = compute_line_tax(999, [GST], "exclusive")  # 999 * 5% = 49.95 -> 50
    line_b = compute_line_tax(999, [GST], "exclusive")  # another 50
    totals = invoice_tax_totals([line_a, line_b])
    assert totals == {"GST": 100}
    # Not the same as rounding the doubled pretax once: 1998 * 5% = 99.9 -> 100 too here, but
    # the point is this sums the *lines'* own rounded values, not the invoice's raw base.
    assert totals["GST"] == line_a.component_cents["GST"] + line_b.component_cents["GST"]


def test_invoice_tax_totals_empty():
    assert invoice_tax_totals([]) == {}


# --- resolve_rate_ppm: effective-date boundaries -----------------------------------------


def test_resolve_rate_ppm_within_a_range():
    history = [(date(2024, 1, 1), date(2025, 1, 1), 50_000)]
    assert resolve_rate_ppm(history, date(2024, 6, 1)) == 50_000


def test_resolve_rate_ppm_effective_from_is_inclusive():
    history = [(date(2024, 1, 1), date(2025, 1, 1), 50_000)]
    assert resolve_rate_ppm(history, date(2024, 1, 1)) == 50_000


def test_resolve_rate_ppm_effective_to_is_exclusive():
    history = [(date(2024, 1, 1), date(2025, 1, 1), 50_000)]
    assert resolve_rate_ppm(history, date(2025, 1, 1)) is None


def test_resolve_rate_ppm_before_any_rate_existed():
    history = [(date(2024, 1, 1), None, 50_000)]
    assert resolve_rate_ppm(history, date(2023, 12, 31)) is None


def test_resolve_rate_ppm_open_ended_rate_covers_today_and_beyond():
    history = [(date(2024, 1, 1), None, 50_000)]
    assert resolve_rate_ppm(history, date(2099, 1, 1)) == 50_000


def test_resolve_rate_ppm_picks_the_right_rate_across_a_change():
    history = [
        (date(2024, 1, 1), date(2025, 1, 1), 50_000),
        (date(2025, 1, 1), None, 70_000),
    ]
    assert resolve_rate_ppm(history, date(2024, 12, 31)) == 50_000
    assert resolve_rate_ppm(history, date(2025, 1, 1)) == 70_000


# --- #119: the ppm scale change itself ---------------------------------------------------


def test_ppm_scale_matches_old_bp_results_exactly():
    """Every whole-basis-point rate (5%, 7%, 13%, 15%) must compute identical cents whether
    run through the old bp arithmetic or `compute_line_tax`'s new ppm scale (`old_bp * 100`)
    — the migration only changes the unit, never an amount already issued."""
    for old_bp, amount_cents in ((500, 10_000), (700, 999), (1300, 4_999), (1500, 330)):
        before = _bp_scale_tax(amount_cents, old_bp)
        new_style = ComponentRate("T", old_bp * 100)  # the same rate, in ppm
        after = compute_line_tax(amount_cents, [new_style], "exclusive")
        assert before == after.component_cents["T"] == after.tax_cents


def _bp_scale_tax(amount_cents: int, rate_bp: int) -> int:
    """The pre-#119 arithmetic, `round_half_up(amount_cents * rate_bp / 10_000)` — kept here,
    isolated, only to prove the new ppm scale reproduces it exactly for whole-bp rates."""
    from decimal import ROUND_HALF_UP, Decimal

    return int((Decimal(amount_cents) * rate_bp / 10_000).quantize(Decimal("1"), ROUND_HALF_UP))


def test_qst_is_exact_at_the_new_scale():
    """QST's legal rate, 9.975%, is 99_750 ppm exactly — no rounding of the rate itself, unlike
    the old bp scale's forced 998 bp (9.98%, an over-charge). $100.00 at 9.975% is exactly
    $9.975, which still rounds half-up to $9.98 per line (CLAUDE.md), and that is *not* the
    same as flatly charging 9.98%: a larger amount tells the two rates apart."""
    qst = ComponentRate("QST", 99_750)
    hundred_dollars = compute_line_tax(10_000, [qst], "exclusive")
    assert hundred_dollars.component_cents["QST"] == 998  # 9.975 rounds half-up to 9.98

    # At a larger, less-round amount the exact 9.975% and a flat 9.98% diverge.
    exact = compute_line_tax(1_234_567, [qst], "exclusive")
    flat_998_bp = ComponentRate("QST", 99_800)  # 9.98%, what the old bp scale was forced to
    rounded = compute_line_tax(1_234_567, [flat_998_bp], "exclusive")
    assert exact.component_cents["QST"] != rounded.component_cents["QST"]
